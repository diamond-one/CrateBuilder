"""Catalogue tests use metadata only; no personal audio is accessed."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cratebuilder.catalogue import Catalogue, SCHEMA_VERSION, resolve_folder_rules
from cratebuilder.model import Sample


def sample(path, sample_id="example", size=50, mtime_ns=123):
    return Sample(id=sample_id, path=str(path), relative=Path(path).name,
                  kind="perc", confidence=.9, classification="Test metadata",
                  duration=.1, rate=48000, channels=1, fingerprint="audio-hash",
                  size=size, mtime_ns=mtime_ns, features={"centroid": 1000})


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "local cache" / "catalogue.sqlite3"
        self.catalogue = Catalogue(self.db_path)

    def test_inherited_tags_without_renaming_original_files(self):
        folder = r"C:\Samples\Folly\kitchenSamples"
        original = folder + r"\FOLIAGE_SOUNDS_103.wav"
        self.catalogue.set_folder_rule(folder, ["Kitchen", "utensils", "percussion"], kind="foley")
        result = self.catalogue.rules_for(original)
        self.assertEqual(result["tags"], ["kitchen", "utensils", "percussion"])
        self.assertEqual(result["kind"], "foley")
        self.assertEqual(result["folder"], folder)
        # A synthetic path need not exist to receive catalogue labels.
        self.assertFalse(Path(original).exists())

    def test_ancestors_accumulate_tags_and_closest_explicit_kind_wins(self):
        self.catalogue.set_folder_rule(r"C:\Samples", ["found", "kitchen"], "foley")
        self.catalogue.set_folder_rule(r"C:\Samples\Metal", ["metal", "FOUND"], "perc")
        self.catalogue.set_folder_rule(r"C:\Samples\Metal\Rings", ["ringing"])
        result = self.catalogue.rules_for(r"C:\Samples\Metal\Rings\hit.wav")
        self.assertEqual(result["tags"], ["found", "kitchen", "metal", "ringing"])
        self.assertEqual(result["kind"], "perc")
        self.assertEqual(result["folder"], r"C:\Samples\Metal\Rings")

    def test_excluded_parent_can_be_overridden_by_explicit_child_kind(self):
        self.catalogue.set_folder_rule(r"C:\Samples\FX", ["effects"], "exclude")
        self.catalogue.set_folder_rule(r"C:\Samples\FX\Kitchen", ["utensils"])
        self.assertEqual(self.catalogue.rules_for(r"C:\Samples\FX\Kitchen\a.wav")["kind"], "exclude")
        self.catalogue.set_folder_rule(r"C:\Samples\FX\Kitchen", ["utensils"], "foley")
        self.assertEqual(self.catalogue.rules_for(r"C:\Samples\FX\Kitchen\a.wav")["kind"], "foley")

    def test_windows_case_slashes_and_component_boundaries(self):
        self.catalogue.set_folder_rule(r"C:\Samples\Folly", ["foley"], "foley")
        self.assertEqual(self.catalogue.rules_for("c:/SAMPLES/folly/hit.wav")["kind"], "foley")
        self.assertEqual(self.catalogue.rules_for(r"C:\Samples\FollyExtra\hit.wav")["tags"], [])
        self.assertEqual(self.catalogue.rules_for(r"C:\Samples\Folly\..\Other\hit.wav")["tags"], [])
        self.catalogue.set_folder_rule("c:/samples/FOLLY/", ["replaced"], "perc")
        self.assertEqual(len(self.catalogue.list_folder_rules()), 1)
        self.assertEqual(self.catalogue.rules_for(r"C:\Samples\Folly\hit.wav")["tags"], ["replaced"])

    def test_network_paths_and_nonrecursive_rules(self):
        self.catalogue.set_folder_rule(r"\\server\share\Samples", ["network"], recursive=False)
        self.assertEqual(self.catalogue.rules_for(r"\\SERVER\SHARE\Samples\hit.wav")["tags"], ["network"])
        self.assertEqual(self.catalogue.rules_for(r"\\server\share\Samples\Sub\hit.wav")["tags"], [])
        self.assertEqual(self.catalogue.rules_for(r"\\server\share\SamplesExtra\hit.wav")["tags"], [])

    def test_reopen_preserves_rules_and_ensure_does_not_overwrite_user_changes(self):
        folder = r"C:\Samples\Folly"
        self.assertTrue(self.catalogue.ensure_folder_rule(folder, ["kitchen"], "foley"))
        self.catalogue.set_folder_rule(folder, ["my label"], "perc", recursive=False)
        self.assertFalse(self.catalogue.ensure_folder_rule(folder.upper(), ["kitchen"], "foley"))
        reopened = Catalogue(self.db_path)
        self.assertEqual(reopened.list_folder_rules(), self.catalogue.list_folder_rules())
        self.assertEqual(reopened.list_folder_rules()[0]["tags"], ["my label"])
        self.assertFalse(reopened.list_folder_rules()[0]["recursive"])
        self.assertTrue(reopened.delete_folder_rule(folder.upper()))
        self.assertEqual(reopened.list_folder_rules(), [])
        self.assertFalse(reopened.delete_folder_rule(folder))

    def test_snapshot_resolves_without_reopening_database(self):
        self.catalogue.set_folder_rule(r"C:\Samples", ["kitchen"], "foley")
        snapshot = self.catalogue.list_folder_rules()
        self.catalogue.delete_folder_rule(r"C:\Samples")
        self.assertEqual(resolve_folder_rules(r"C:\Samples\a.wav", snapshot)["tags"], ["kitchen"])
        self.assertEqual(self.catalogue.rules_for(r"C:\Samples\a.wav", snapshot)["kind"], "foley")
        self.assertIsNone(self.catalogue.rules_for(r"C:\Samples\a.wav")["kind"])

    def test_per_file_cache_checks_size_time_and_analysis_version(self):
        original = sample(r"C:\Samples\Folly\FOLIAGE_SOUNDS_103.wav")
        self.catalogue.save_sample(original, 3)
        reopened = Catalogue(self.db_path)
        restored = reopened.get_sample(original.path.upper(), 50, 123, 3)
        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertIsNone(reopened.get_sample(original.path, 51, 123, 3))
        self.assertIsNone(reopened.get_sample(original.path, 50, 124, 3))
        self.assertIsNone(reopened.get_sample(original.path, 50, 123, 4))
        self.assertIsNone(reopened.get_sample(r"C:\Samples\other.wav", 50, 123, 3))

    def test_failed_or_incomplete_scans_do_not_remove_checkpoints_or_tags(self):
        root = r"C:\Samples"
        first, second = sample(root + r"\a.wav", "a"), sample(root + r"\b.wav", "b")
        self.catalogue.set_folder_rule(root, ["kitchen"], "foley")
        self.catalogue.save_sample(first, 3, root=root)
        self.catalogue.save_sample(second, 3, root=root)
        # A later scan completes only one file before cancellation or disconnection.
        self.catalogue.save_sample(first, 4, root=root)
        self.catalogue.record_scan_status(second.path, "unavailable", "Network offline", root=root)
        self.catalogue.record_scan_status(root + r"\bad.wav", "unreadable", "Invalid header", root=root)
        self.catalogue.record_scan_status(root + r"\loop.wav", "excluded", "Loop folder", root=root)
        self.assertEqual({s.id for s in self.catalogue.cached_samples(root)}, {"a", "b"})
        self.assertEqual({s.id for s in self.catalogue.cached_samples(root, 3)}, {"b"})
        self.assertEqual(self.catalogue.rules_for(second.path)["tags"], ["kitchen"])
        statuses = {Path(s["path"]).name: s for s in self.catalogue.scan_statuses(root)}
        self.assertEqual(len(statuses), 4)
        unavailable = next(s for s in statuses.values() if s["status"] == "unavailable")
        self.assertEqual(unavailable["detail"], "Network offline")
        self.catalogue.record_scan_status(second.path, "available")
        restored = next(s for s in self.catalogue.scan_statuses(root) if s["path"] == second.path)
        self.assertEqual(restored["root"], root)
        self.assertEqual(restored["detail"], "")

    def test_cached_roots_use_directory_boundaries(self):
        for path in (r"C:\Samples\a.wav", r"C:\Samples\Sub\b.wav", r"C:\SamplesExtra\c.wav"):
            self.catalogue.save_sample(sample(path, path), 3)
        self.assertEqual(len(self.catalogue.cached_samples("c:/SAMPLES")), 2)
        self.assertEqual(len(self.catalogue.scan_statuses("c:/SAMPLES")), 2)

    def test_cached_availability_uses_persisted_file_status_after_reopen(self):
        root = r"C:\Samples"
        for index, status in enumerate(("online-only", "unavailable", "unreadable", "error", "missing", "not-one-shot", "excluded")):
            item = sample(fr"{root}\{index}.wav", str(index))
            self.catalogue.save_sample(item, 3, root=root)
            self.catalogue.record_scan_status(item.path, status, "Deferred or failed", root=root)
        healthy = sample(root + r"\healthy.wav", "healthy")
        self.catalogue.save_sample(healthy, 3, root=root)
        reopened = Catalogue(self.db_path)
        cached = {item.id: item for item in reopened.cached_samples(root)}
        self.assertEqual(len(cached), 8)
        self.assertTrue(cached["healthy"].features["available"])
        self.assertTrue(all(not item.features["available"] for key, item in cached.items() if key != "healthy"))
        # Overlaying availability must not rewrite the reusable baseline analysis.
        raw = reopened.get_sample(root + r"\0.wav", 50, 123, 3)
        self.assertNotIn("available", raw.features)
        reopened.record_scan_status(raw.path, "available", root=root)
        self.assertTrue(next(item for item in reopened.cached_samples(root) if item.id == "0").features["available"])

    def test_offline_source_overrides_file_availability_until_source_recovers(self):
        root = r"\\Server\Share\Samples"
        healthy = sample(root + r"\Kitchen\hit.wav", "healthy")
        failed = sample(root + r"\Kitchen\broken.wav", "failed")
        neighbour = sample(root + r"Extra\hit.wav", "neighbour")
        for item in (healthy, failed, neighbour):
            self.catalogue.save_sample(item, 3)
        self.catalogue.record_scan_status(failed.path, "error")
        self.catalogue.record_scan_status(root, "unavailable", "Network disconnected")
        reopened = Catalogue(self.db_path)
        cached = {item.id: item for item in reopened.cached_samples(root + r"\Kitchen")}
        self.assertEqual(set(cached), {"healthy", "failed"})
        self.assertTrue(all(not item.features["available"] for item in cached.values()))
        self.assertTrue(reopened.cached_samples(root + "Extra")[0].features["available"])
        # A file check alone must not reactivate an offline source.
        reopened.record_scan_status(healthy.path, "available")
        self.assertFalse(next(item for item in reopened.cached_samples(root) if item.id == "healthy").features["available"])
        reopened.record_scan_status(root, "available")
        recovered = {item.id: item for item in Catalogue(self.db_path).cached_samples(root)}
        self.assertTrue(recovered["healthy"].features["available"])
        self.assertFalse(recovered["failed"].features["available"])
        reopened.record_scan_status(failed.path, "available")
        self.assertTrue(all(item.features["available"] for item in reopened.cached_samples(root)))

    def test_invalid_cache_payload_reanalyses_without_losing_rules(self):
        original = sample(r"C:\Samples\a.wav")
        self.catalogue.save_sample(original, 3)
        self.catalogue.set_folder_rule(r"C:\Samples", ["keep me"])
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE sample_cache SET sample_json=?", (json.dumps({"future": "format"}),))
        self.assertIsNone(self.catalogue.get_sample(original.path, 50, 123, 3))
        self.assertEqual(self.catalogue.cached_samples(r"C:\Samples"), [])
        self.assertEqual(self.catalogue.rules_for(original.path)["tags"], ["keep me"])

    def test_invalid_analysis_is_not_partially_committed(self):
        original = sample(r"C:\Samples\a.wav")
        self.catalogue.save_sample(original, 3)
        original.features["centroid"] = float("nan")
        with self.assertRaises(ValueError):
            self.catalogue.save_sample(original, 4)
        self.assertIsNotNone(self.catalogue.get_sample(original.path, 50, 123, 3))
        self.assertIsNone(self.catalogue.get_sample(original.path, 50, 123, 4))

    def test_ui_and_worker_can_share_catalogue_and_separate_instances(self):
        def save(index):
            catalogue = self.catalogue if index % 2 else Catalogue(self.db_path)
            item = sample(fr"C:\Samples\{index}.wav", str(index))
            catalogue.save_sample(item, 3)
            catalogue.set_folder_rule(fr"C:\Samples\Folder{index}", ["kitchen"], "foley")
            return catalogue.get_sample(item.path, item.size, item.mtime_ns, 3).id

        with ThreadPoolExecutor(max_workers=6) as workers:
            result = list(workers.map(save, range(24)))
        self.assertEqual(len(set(result)), 24)
        self.assertEqual(len(self.catalogue.cached_samples(r"C:\Samples")), 24)
        self.assertEqual(len(self.catalogue.list_folder_rules()), 24)

    def test_schema_version_and_future_schema_protection(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(ValueError, "newer CrateBuilder"):
            Catalogue(self.db_path)

    def test_invalid_rule_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            self.catalogue.set_folder_rule(r"C:\Samples", ["x"], "imaginary")
        with self.assertRaises(ValueError):
            self.catalogue.set_folder_rule(r"C:\Samples", "kitchen")
        with self.assertRaises(ValueError):
            self.catalogue.set_folder_rule(r"C:\Samples", [123])
        self.assertEqual(self.catalogue.list_folder_rules(), [])


if __name__ == "__main__":
    unittest.main()
