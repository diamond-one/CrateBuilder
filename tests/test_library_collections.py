"""Persistent library, strict generation sources and non-destructive collection copies."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from cratebuilder.catalogue import canonical_path
from cratebuilder.editing import regenerate_indices
from cratebuilder.library import LibraryStore, apply_library_marks, export_collection
from cratebuilder.matching import Matcher, generation_source, pack_identity
from cratebuilder.model import Assignment, Sample, Session


def metadata(path, source="ni:c:/samples/pack-a", pack="Pack A", category="Drums", **extra):
    return {"path": str(path), "name": Path(path).name, "provider": "Native Instruments" if source.startswith("ni:") else "User",
            "source": source, "pack": pack, "category": category, "kind": "kick", "subtype": "Acoustic",
            "bpm": 92, "key": "C minor", **extra}


def analyzed(name, kind, source="ni:c:/samples/pack-a", value=0, **features):
    ni = source.startswith("ni:")
    hints = dict(centroid=300 + value * 60, decay=.1 + value * .004, crest=10, flatness=.02,
                 leading=0, clipped=0, active_duration=.2, waveform=[.1, .3], available=True,
                 audio_hash="audio-" + name, provider="Native Instruments" if ni else "User")
    if ni:
        hints["ni_source"] = source
    hints.update(features)
    return Sample(name, f"C:/samples/{source.replace(':', '_').replace('/', '_')}/{name}.wav", name + ".wav",
                  kind, 1, "Known category", .2, 48000, 1, "bytes-" + name, 100, 1,
                  hints, source=source if ni else source, tags=["short", "click", "low-hit", "rattle", "ringing", "textural"])


def complete_pack(source):
    samples = []
    for kind in ("kick", "snare", "rim", "clap", "fill", "hat", "open_hat", "cymbal", "foley", "perc"):
        for index in range(10):
            samples.append(analyzed(f"{source}-{kind}-{index}", kind, source, index))
    return samples


class LibraryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LibraryStore(self.root / "library.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_metadata_index_never_needs_existing_audio_and_keeps_musical_key(self):
        missing = self.root / "not-mounted" / "Kick 100%.wav"
        self.assertFalse(missing.exists())
        self.assertEqual(self.store.upsert_items([metadata(missing)]), 1)
        rows = self.store.query_items(search="kick C minor")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "C minor")
        self.assertEqual(rows[0]["bpm"], 92)
        self.assertFalse(rows[0]["favourite"])
        with self.store._connection() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertGreaterEqual(len(db.execute("PRAGMA index_list(metadata_items)").fetchall()), 4)

    def test_upserts_are_checkpointed_in_small_batches_and_do_not_prune_other_items(self):
        def interrupted():
            for index in range(1001):
                if index == 1000:
                    raise RuntimeError("Indexer interrupted")
                yield metadata(self.root / f"sound-{index}.wav")
        with self.assertRaisesRegex(RuntimeError, "Indexer interrupted"):
            self.store.upsert_items(interrupted())
        self.assertEqual(self.store.count_items(), 1000)
        self.store.upsert_items([metadata(self.root / "sound-0.wav", name="Updated title")])
        self.assertEqual(self.store.count_items(), 1000)
        self.assertEqual(self.store.query_items(search="Updated title")[0]["name"], "Updated title")

    def test_marks_and_usage_survive_metadata_refresh_and_database_reopening(self):
        item = metadata(self.root / "Kick.wav")
        self.store.upsert_items([item])
        self.store.set_mark(item["path"], favourite=True, hidden=True)
        self.store.record_used([item["path"], item["path"]])
        self.store.upsert_items([{**item, "name": "Changed metadata title"}])
        reopened = LibraryStore(self.store.path)
        self.assertEqual(reopened.query_items(), [])
        row = reopened.query_items(hidden_only=True, favourites_only=True)[0]
        self.assertEqual(row["used_count"], 2)
        self.assertIsNotNone(row["last_used"])
        self.assertEqual(row["name"], "Changed metadata title")
        self.assertEqual(reopened.list_sources()[0]["hidden_count"], 1)
        mark = reopened.marks_for_paths([item["path"].upper()])[canonical_path(item["path"])[1]]
        self.assertTrue(mark["favourite"])
        self.assertTrue(mark["hidden"])

    def test_source_category_search_and_pagination_are_consistent_with_counts(self):
        items = [metadata(self.root / "Kick_100%.wav"),
                 metadata(self.root / "Kick 1000.wav", source="ni:c:/samples/pack-b", pack="Pack B"),
                 metadata(self.root / "Drum loop.wav", category="Loops"),
                 metadata(self.root / "Kitchen hit.wav", source="__user__", pack="Kitchen", category="One Shots")]
        self.store.upsert_items(items)
        self.assertEqual(self.store.count_items(search="100%"), 1)
        self.assertEqual(self.store.count_items(search="_100"), 1)
        self.assertEqual(self.store.count_items(source="__user__"), 1)
        self.assertEqual(self.store.count_items(source="ni:c:/samples/pack-a", category="Drums"), 1)
        all_rows = self.store.query_items(limit=100)
        self.assertEqual(self.store.query_items(limit=2, offset=1), all_rows[1:3])
        self.assertEqual(self.store.count_items(), len(all_rows))
        sources = {row["id"]: row for row in self.store.list_sources()}
        self.assertEqual(sources["__user__"]["name"], "Your samples")
        self.assertEqual(sources["ni:c:/samples/pack-a"]["root"], "c:/samples/pack-a")
        self.assertEqual(sources["ni:c:/samples/pack-a"]["count"], 2)

    def test_not_used_recently_includes_old_uses_but_excludes_the_last_seven_days(self):
        items = [metadata(self.root / (name + ".wav")) for name in ("never", "old", "recent")]
        self.store.upsert_items(items)
        self.store.record_used([items[1]["path"], items[2]["path"]])
        old_key = canonical_path(items[1]["path"])[1]
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with self.store._connection() as db:
            db.execute("UPDATE library_marks SET last_used=? WHERE path_key=?", (old, old_key))
        self.store.set_mark(items[1]["path"], favourite=True)
        self.assertEqual({row["name"] for row in self.store.query_items(unused_only=True)}, {"never.wav", "old.wav"})
        self.assertEqual(self.store.query_items(unused_only=True, favourites_only=True)[0]["name"], "old.wav")
        self.assertEqual(self.store.count_items(unused_only=True), 2)

    def test_crates_save_filters_without_changing_metadata_or_marks(self):
        settings = {"generation_sources": [], "source_mode": "same_pack", "favourites_only": True, "search": "kitchen"}
        self.store.save_crate("My kitchen", settings)
        self.store.save_crate("my kitchen", {**settings, "generation_sources": ["__user__"]})
        rows = self.store.list_crates()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["settings"]["generation_sources"], ["__user__"])
        self.assertEqual(self.store.delete_crate("MY KITCHEN"), 1)
        self.assertEqual(self.store.list_crates(), [])
        with self.assertRaises(ValueError):
            self.store.save_crate(" ", settings)

    def test_concurrent_read_mark_and_index_operations_use_independent_connections(self):
        def index():
            return self.store.upsert_items(metadata(self.root / f"item-{i}.wav") for i in range(1100))
        def marks():
            for index in range(20):
                self.store.set_mark(self.root / f"item-{index}.wav", favourite=True)
            return True
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(index), pool.submit(marks), pool.submit(lambda: [self.store.count_items() for _ in range(15)])]
            self.assertEqual(jobs[0].result(), 1100)
            self.assertTrue(jobs[1].result())
            jobs[2].result()
        self.assertEqual(self.store.count_items(favourites_only=True), 20)
        paths = [self.root / f"item-{i}.wav" for i in range(1100)]
        self.assertEqual(len(self.store.marks_for_paths(paths)), 1100)


class GenerationFilterTests(unittest.TestCase):
    def test_filter_fields_round_trip_and_legacy_defaults(self):
        session = Session("root", [], generation_sources=[], source_mode="same_pack", favourites_only=True, unused_only=True)
        restored = Session.from_dict(session.to_dict())
        self.assertEqual(restored.generation_sources, [])
        self.assertEqual((restored.source_mode, restored.favourites_only, restored.unused_only), ("same_pack", True, True))
        old = session.to_dict()
        for field in ("generation_sources", "source_mode", "favourites_only", "unused_only"):
            old.pop(field)
        restored = Session.from_dict(old)
        self.assertIsNone(restored.generation_sources)
        self.assertEqual((restored.source_mode, restored.favourites_only, restored.unused_only), ("mix", False, False))

    def test_none_all_empty_none_and_explicit_sources_invalidate_prepared_statistics(self):
        samples = [analyzed("a", "kick"), analyzed("b", "kick", "ni:c:/samples/pack-b"), analyzed("user", "kick", "Kitchen")]
        session = Session("root", samples)
        first = Matcher(session)
        self.assertEqual(len(first.ordered), 3)
        session.generation_sources = []
        self.assertEqual(Matcher(session).ordered, [])
        session.generation_sources = ["ni:c:/samples/pack-a"]
        self.assertEqual([s.id for s in Matcher(session).ordered], ["a"])
        session.generation_sources = ["__user__"]
        self.assertEqual([s.id for s in Matcher(session).ordered], ["user"])
        session.generation_sources = None
        self.assertEqual(len(Matcher(session).ordered), 3)

    def test_hidden_preview_unavailable_excluded_and_manual_only_have_distinct_rules(self):
        samples = [analyzed("good", "kick"), analyzed("hidden", "kick", hidden=True),
                   analyzed("preview", "kick", preview_only=True), analyzed("offline", "kick", available=False),
                   analyzed("excluded", "excluded"), analyzed("manual", "kick", manual_only=True)]
        matcher = Matcher(Session("root", samples))
        self.assertEqual({s.id for s in matcher.ordered}, {"good", "manual"})
        self.assertEqual({s.id for s in matcher.pool(4)}, {"good"})
        self.assertEqual({s.id for s in matcher.pool(4, browse=True)}, {"good", "manual"})

    def test_marks_are_copied_onto_samples_and_favourite_filter_is_strict(self):
        samples = [analyzed("first", "kick"), analyzed("second", "kick")]
        session = Session("root", samples)
        original_features = copy.deepcopy(samples[0].features)
        with tempfile.TemporaryDirectory() as temp:
            store = LibraryStore(Path(temp) / "library.sqlite3")
            store.set_mark(samples[1].path, favourite=True)
            store.record_used([samples[1].path])
            apply_library_marks(session, store)
        self.assertIsNot(session.samples[0], samples[0])
        self.assertEqual(samples[0].features, original_features)
        self.assertEqual(session.library_revision, 1)
        session.favourites_only = True
        self.assertEqual([s.id for s in Matcher(session).ordered], ["second"])
        self.assertEqual(session.samples[1].features["used_count"], 1)

    def test_unused_filter_observes_seven_day_dates_and_in_session_audio_history(self):
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        samples = [analyzed("old", "kick", used_count=3, last_used=old),
                   analyzed("recent", "kick", used_count=1, last_used=recent),
                   analyzed("never", "kick"), analyzed("alias", "kick", audio_hash="audio-never")]
        session = Session("root", samples, unused_only=True)
        self.assertEqual({s.id for s in Matcher(session).ordered}, {"old", "never", "alias"})
        session.history.append("audio-never")
        self.assertEqual({s.id for s in Matcher(session).ordered}, {"old"})

    def test_same_pack_prefers_full_role_coverage_and_is_reproducible(self):
        source = "ni:c:/samples/complete"
        samples = complete_pack(source) + [analyzed(f"limited-{i}", "foley", "ni:c:/samples/limited", i) for i in range(20)]
        first = Session("root", samples, kit_size=16, source_mode="same_pack")
        second = Session.from_dict(first.to_dict())
        Matcher(first).build(0)
        Matcher(second).build(0)
        self.assertEqual([p.sample_id for p in first.pads], [p.sample_id for p in second.pads])
        self.assertEqual({generation_source(first.by_id[p.sample_id]) for p in first.pads[:16]}, {source})

    def test_same_pack_kept_kick_anchors_new_choices_and_selected_regeneration(self):
        first, second = "ni:c:/samples/pack-a", "ni:c:/samples/pack-b"
        samples = complete_pack(first) + complete_pack(second)
        session = Session("root", samples, kit_size=16, source_mode="same_pack")
        kick = next(s for s in samples if generation_source(s) == second and s.kind == "kick")
        session.pads[4] = Assignment(kick.id, "Keep my anchor", locked=True)
        Matcher(session).build(0)
        self.assertEqual({generation_source(session.by_id[p.sample_id]) for p in session.pads[:16]}, {second})
        before = copy.deepcopy(session.pads)
        regenerate_indices(session, [6])
        self.assertEqual(generation_source(session.by_id[session.pads[6].sample_id]), second)
        self.assertEqual([p for i, p in enumerate(session.pads) if i != 6], [p for i, p in enumerate(before) if i != 6])

    def test_same_pack_user_group_still_distinguishes_actual_user_packs(self):
        session = Session("root", complete_pack("User drums") + complete_pack("Kitchen"), kit_size=16,
                          generation_sources=["__user__"], source_mode="same_pack")
        Matcher(session).build(0)
        self.assertEqual(len({pack_identity(session.by_id[p.sample_id]) for p in session.pads[:16]}), 1)

    def test_filters_preserve_kept_assignments_and_do_not_fall_back_to_excluded_sources(self):
        samples = complete_pack("ni:c:/samples/pack-a") + complete_pack("ni:c:/samples/pack-b")
        session = Session("root", samples, generation_sources=[], kit_size=16)
        session.pads[4] = Assignment(samples[0].id, "Kept outside active sources", locked=True)
        Matcher(session).build(0)
        self.assertEqual(session.pads[4].sample_id, samples[0].id)
        self.assertEqual(session.pads[4].reason, "Kept outside active sources")
        self.assertTrue(all(p.sample_id is None for i, p in enumerate(session.pads[:16]) if i != 4))


class CollectionExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "originals"
        self.source.mkdir()
        self.items = []
        for number in range(2):
            directory = self.source / str(number)
            directory.mkdir()
            path = directory / "Original Name.wav"
            path.write_bytes(b"original-audio-" + bytes([number]) * 120)
            stat = path.stat()
            self.items.append(metadata(path, source="__user__", pack="Kitchen", category="Drums",
                                       size=stat.st_size, mtime_ns=stat.st_mtime_ns, source_root=str(self.source)))
        self.before = {item["path"]: Path(item["path"]).read_bytes() for item in self.items}

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_collection_preserves_names_bytes_and_provenance_without_overwriting(self):
        folder = export_collection(self.items + [self.items[0]], self.root / "exports", "My crate")
        copies = list(folder.rglob("*.wav"))
        self.assertEqual(len(copies), 2)
        self.assertEqual({p.name for p in copies}, {"Original Name.wav"})
        manifest = json.loads((folder / "COLLECTION_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["complete"])
        self.assertEqual(len(manifest["files"]), 2)
        for row in manifest["files"]:
            copy_path = folder / row["file"]
            self.assertEqual(copy_path.read_bytes(), self.before[row["source_path"]])
            self.assertEqual(hashlib.sha256(copy_path.read_bytes()).hexdigest(), row["sha256"])
            self.assertEqual(row["key"], "C minor")
        self.assertTrue((folder / "COMPLETE.json").exists())
        self.assertFalse((folder / "INCOMPLETE.txt").exists())
        second = export_collection(self.items, self.root / "exports", "My crate")
        self.assertNotEqual(folder, second)
        self.assertEqual({path: Path(path).read_bytes() for path in self.before}, self.before)

    def test_destination_inside_source_and_changed_indexed_files_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the source"):
            export_collection(self.items, self.source / "nested export", "No")
        changed = {**self.items[0], "size": self.items[0]["size"] + 1}
        with self.assertRaisesRegex(ValueError, "changed since indexing"):
            export_collection([changed], self.root / "exports", "No")
        self.assertFalse((self.root / "exports").exists())

    def test_cancellation_keeps_an_explicit_incomplete_report_and_no_complete_collection(self):
        cancel = threading.Event()
        def progress(message, value):
            cancel.set()
        with self.assertRaises(InterruptedError):
            export_collection(self.items, self.root / "exports", "Cancelled", progress, cancel)
        self.assertFalse((self.root / "exports" / "Cancelled").exists())
        self.assertFalse(list((self.root / "exports").rglob("COMPLETE.json")))
        reports = list((self.root / "exports").rglob("INCOMPLETE.txt"))
        self.assertEqual(len(reports), 1)
        self.assertEqual({path: Path(path).read_bytes() for path in self.before}, self.before)

    def test_copy_validation_failure_never_publishes_success(self):
        with patch("cratebuilder.library._hash_file", return_value="wrong"):
            with self.assertRaisesRegex(ValueError, "Copy validation failed"):
                export_collection(self.items, self.root / "exports", "Broken")
        self.assertFalse((self.root / "exports" / "Broken").exists())
        self.assertTrue(list((self.root / "exports").rglob("INCOMPLETE.txt")))
        self.assertFalse(list((self.root / "exports").rglob("COMPLETE.json")))


if __name__ == "__main__":
    unittest.main()
