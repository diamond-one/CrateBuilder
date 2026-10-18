"""End-to-end library updates against synthetic audio in the workspace."""
from dataclasses import asdict
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cratebuilder.audio import (ANALYSIS_VERSION, apply_folder_rules,
                                merge_library_session, scan_folder)
from cratebuilder.catalogue import Catalogue
from cratebuilder.matching import Matcher
from cratebuilder.model import Assignment


class LibraryUpdateTests(unittest.TestCase):
    def setUp(self):
        test_parent = Path(__file__).resolve().parents[2] / "test-temp"
        test_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="library-", dir=test_parent)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.assertTrue(self.work.is_relative_to(test_parent.resolve()))
        self.root = self.work / "samples"
        self.root.mkdir()
        self.cache = self.work / "cache"
        self.catalogue = Catalogue(self.cache / "catalogue.sqlite3")

    def write_audio(self, relative, frequency=70):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        rate = 48000
        time = np.arange(6000) / rate
        data = .45 * np.sin(2 * np.pi * frequency * time) * np.exp(-time * 35)
        sf.write(path, data, rate, subtype="PCM_16")
        return path

    def scan(self, **kwargs):
        return scan_folder(self.root, self.cache, catalogue=self.catalogue, **kwargs)

    def test_unchanged_update_reuses_audio_analysis_and_session_records(self):
        self.write_audio("KICKS/main.wav")
        self.write_audio("SNARES/backbeat.wav", frequency=1700)
        with patch("cratebuilder.audio.sf.read", wraps=sf.read) as read:
            first = self.scan()
        self.assertEqual(read.call_count, 2)
        with patch("cratebuilder.audio.sf.read", side_effect=AssertionError("Cached audio was reread")):
            with patch("cratebuilder.audio.sf.info", side_effect=AssertionError("Cached header was reread")):
                second = self.scan()
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(len(second.samples), 2)

    def test_folder_truth_labels_foliage_without_renaming_or_rereading(self):
        path = self.write_audio("Folly/kitchenSamples/FOLIAGE_SOUNDS_103.wav")
        original_bytes = path.read_bytes()
        session = self.scan()
        baseline = session.samples[0]
        self.assertEqual(baseline.kind, "kick")
        original_id = baseline.id
        self.catalogue.set_folder_rule(path.parent, ["kitchen", "utensils", "percussion"], "foley")
        with patch("cratebuilder.audio.sf.read", side_effect=AssertionError("Tagging opened audio")):
            with patch("cratebuilder.audio.sf.info", side_effect=AssertionError("Tagging opened audio")):
                apply_folder_rules(session, self.catalogue)
        labelled = session.samples[0]
        self.assertEqual(labelled.id, original_id)
        self.assertEqual(labelled.kind, "foley")
        self.assertEqual(labelled.base_kind, "kick")
        self.assertTrue({"kitchen", "utensils", "percussion"}.issubset(labelled.tags))
        self.assertEqual(labelled.path, str(path))
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_tag_edits_and_deletion_restore_baseline_without_audio_reads(self):
        path = self.write_audio("kitchenSamples/FOLIAGE_SOUNDS_002.wav")
        session = self.scan()
        baseline_kind, baseline_confidence = session.samples[0].kind, session.samples[0].confidence
        self.catalogue.set_folder_rule(path.parent, ["kitchen", "utensils"], "foley")
        apply_folder_rules(session, self.catalogue)
        first_revision = session.library_revision
        with patch("cratebuilder.audio.sf.read", side_effect=AssertionError("Tag changes opened audio")):
            with patch("cratebuilder.audio.sf.info", side_effect=AssertionError("Tag changes opened audio")):
                self.catalogue.set_folder_rule(path.parent, ["metal"], "perc")
                apply_folder_rules(session, self.catalogue)
                self.assertEqual(session.samples[0].kind, "perc")
                self.assertIn("metal", session.samples[0].tags)
                self.assertNotIn("kitchen", session.samples[0].tags)
                self.catalogue.delete_folder_rule(path.parent)
                apply_folder_rules(session, self.catalogue)
                fresh = self.scan()
        self.assertEqual(session.samples[0].kind, baseline_kind)
        self.assertEqual(session.samples[0].confidence, baseline_confidence)
        self.assertNotIn("metal", session.samples[0].tags)
        self.assertEqual(session.samples[0].to_dict(), fresh.samples[0].to_dict())
        self.assertGreater(session.library_revision, first_revision)

    def test_saved_folder_rule_automatically_labels_future_nested_samples(self):
        first_path = self.write_audio("Folly/kitchenSamples/FOLIAGE_SOUNDS_001.wav")
        self.catalogue.set_folder_rule(first_path.parent, ["kitchen", "utensils"], "foley")
        first = self.scan()
        second_path = self.write_audio("Folly/kitchenSamples/New/FOLIAGE_SOUNDS_002.wav", frequency=1600)
        with patch("cratebuilder.audio.sf.read", wraps=sf.read) as read:
            second = self.scan()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(second.samples), 2)
        self.assertTrue(all(sample.kind == "foley" for sample in second.samples))
        self.assertTrue(all({"kitchen", "utensils"}.issubset(sample.tags) for sample in second.samples))
        self.assertIn(first.samples[0].id, second.by_id)
        self.assertTrue(any(sample.path == str(second_path) for sample in second.samples))

    def test_explicit_foley_rule_rescues_a_default_excluded_fx_folder(self):
        self.write_audio("KICKS/main.wav")
        path = self.write_audio("FX/Kitchen/FOLIAGE_SOUNDS_103.wav", frequency=1700)
        first = self.scan()
        self.assertEqual(len(first.samples), 1)
        self.catalogue.set_folder_rule(path.parent, ["kitchen"], "foley")
        with patch("cratebuilder.audio.sf.read", wraps=sf.read) as read:
            rescued = self.scan()
        self.assertEqual(read.call_count, 1)
        labelled = next(sample for sample in rescued.samples if sample.path == str(path))
        self.assertEqual(labelled.kind, "foley")
        self.assertEqual(labelled.base_kind, "excluded")
        self.assertIn("kitchen", labelled.tags)
        self.catalogue.delete_folder_rule(path.parent)
        apply_folder_rules(rescued, self.catalogue)
        self.assertEqual(rescued.by_id[labelled.id].kind, "excluded")
        self.assertNotIn(labelled.id, {sample.id for sample in Matcher(rescued).ordered})

    def test_cancelled_scan_keeps_completed_file_and_resumes_only_remaining_audio(self):
        for index in range(3):
            self.write_audio(f"KICKS/{index}.wav", frequency=70 + index * 20)
        stop = threading.Event()
        original_save = self.catalogue.save_sample

        def checkpoint(sample, version, root=None):
            original_save(sample, version, root=root)
            stop.set()

        with patch.object(self.catalogue, "save_sample", side_effect=checkpoint):
            with self.assertRaises(InterruptedError):
                self.scan(cancel=stop)
        self.assertEqual(len(self.catalogue.cached_samples(self.root, ANALYSIS_VERSION)), 1)
        with patch("cratebuilder.audio.sf.read", wraps=sf.read) as read:
            resumed = self.scan()
        self.assertEqual(read.call_count, 2)
        self.assertEqual(len(resumed.samples), 3)
        self.assertEqual(len({sample.id for sample in resumed.samples}), 3)

    def test_new_files_are_only_new_analysis_and_preserve_locked_kit(self):
        self.write_audio("KICKS/main.wav")
        current = self.scan()
        selected = current.samples[0].id
        current.pads[4] = Assignment(selected, "My chosen kick", locked=True)
        current.pads[3] = Assignment(None, "Keep empty", locked=True)
        current.kit_size, current.target, current.seed, current.bpm = 16, "sp404", 4321, 87
        current.surprise = .7
        before = [asdict(pad) for pad in current.pads]
        self.write_audio("Folly/new.wav", frequency=1200)
        with patch("cratebuilder.audio.sf.read", wraps=sf.read) as read:
            fresh = self.scan()
        self.assertEqual(read.call_count, 1)
        merged = merge_library_session(current, fresh)
        self.assertIs(merged, current)
        self.assertEqual([asdict(pad) for pad in merged.pads], before)
        self.assertEqual((merged.kit_size, merged.target, merged.seed, merged.bpm, merged.surprise),
                         (16, "sp404", 4321, 87, .7))
        self.assertEqual(len(merged.samples), 2)
        self.assertGreater(merged.library_revision, 0)

    def test_changed_assigned_audio_retains_old_choice_and_exposes_new_version(self):
        path = self.write_audio("KICKS/main.wav")
        current = self.scan()
        original = current.samples[0]
        current.pads[4] = Assignment(original.id, "Chosen before file changed", locked=True)
        self.write_audio("KICKS/main.wav", frequency=170)
        os.utime(path, ns=(original.mtime_ns + 1_000_000_000, original.mtime_ns + 1_000_000_000))
        fresh = self.scan()
        replacement = fresh.samples[0]
        self.assertNotEqual(replacement.id, original.id)
        self.assertNotEqual(replacement.fingerprint, original.fingerprint)
        merged = merge_library_session(current, fresh)
        self.assertEqual(merged.pads[4].sample_id, original.id)
        self.assertTrue(merged.pads[4].locked)
        self.assertFalse(merged.by_id[original.id].features["available"])
        self.assertTrue(merged.by_id[replacement.id].features["available"])
        self.assertIn(replacement.id, {sample.id for sample in Matcher(merged).pool(4)})
        self.assertNotIn(original.id, {sample.id for sample in Matcher(merged).ordered})

    def test_moved_identical_audio_relinks_locked_pad_to_the_available_file(self):
        original_path = self.write_audio("KICKS/main.wav")
        self.write_audio("SNARES/backbeat.wav", frequency=1400)
        current = self.scan()
        original = next(sample for sample in current.samples if sample.path == str(original_path))
        current.pads[4] = Assignment(original.id, "Chosen kick", locked=True)
        destination = self.root / "KICKS" / "Renamed" / "same-hit.wav"
        destination.parent.mkdir()
        original_path.rename(destination)
        fresh = self.scan()
        moved = next(sample for sample in fresh.samples if sample.path == str(destination))
        self.assertEqual(moved.fingerprint, original.fingerprint)
        merged = merge_library_session(current, fresh)
        selected = merged.by_id[merged.pads[4].sample_id]
        self.assertEqual(selected.path, str(destination))
        self.assertTrue(selected.features["available"])
        self.assertTrue(merged.pads[4].locked)

    def test_ambiguous_identical_destinations_keep_original_pad_unavailable(self):
        original_path = self.write_audio("KICKS/main.wav")
        current = self.scan()
        original = current.samples[0]
        current.pads[4] = Assignment(original.id, "Chosen kick", locked=True)
        data = original_path.read_bytes()
        for name in ("copy-one.wav", "copy-two.wav"):
            (original_path.parent / name).write_bytes(data)
        original_path.unlink()
        merged = merge_library_session(current, self.scan())
        self.assertEqual(merged.pads[4].sample_id, original.id)
        self.assertFalse(merged.by_id[original.id].features["available"])
        self.assertEqual(len([sample for sample in Matcher(merged).pool(4)]), 2)

    def test_mac_sidecars_hidden_folders_and_corrupt_audio_do_not_stop_good_samples(self):
        self.write_audio("KICKS/main.wav")
        (self.root / "._metadata.wav").write_bytes(b"not audio")
        hidden = self.root / "__MACOSX" / "._sidecar.wav"
        hidden.parent.mkdir()
        hidden.write_bytes(b"not audio")
        secret = self.root / ".hidden" / "ignored.wav"
        secret.parent.mkdir()
        secret.write_bytes(b"not audio")
        broken = self.root / "bad.wav"
        broken.write_bytes(b"not audio")
        with patch("cratebuilder.audio.sf.read", wraps=sf.read) as read:
            session = self.scan()
        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(session.samples), 1)
        self.assertIn("Found 2 audio files", session.scan_notes[0])
        self.assertTrue(any("bad.wav" in note for note in session.scan_notes))
        statuses = self.catalogue.scan_statuses(self.root)
        self.assertTrue(any(item["path"] == str(broken) and item["status"] == "error" for item in statuses))
        self.assertFalse(any("._" in item["path"] for item in statuses))

    def test_cloud_placeholder_does_not_read_audio_or_enter_automatic_matching(self):
        path = self.write_audio("KICKS/main.wav")
        initial = self.scan()
        original_stat = Path.stat

        def cloud_stat(candidate, *args, **kwargs):
            value = original_stat(candidate, *args, **kwargs)
            if str(candidate) == str(path):
                return SimpleNamespace(st_size=value.st_size, st_mtime_ns=value.st_mtime_ns,
                                       st_mode=value.st_mode, st_file_attributes=0x400000)
            return value

        with patch.object(Path, "stat", cloud_stat):
            with patch("cratebuilder.audio.sf.read", side_effect=AssertionError("Downloaded placeholder")):
                offline = self.scan()
        self.assertEqual(offline.samples[0].id, initial.samples[0].id)
        self.assertFalse(offline.samples[0].features["available"])
        self.assertEqual(Matcher(offline).ordered, [])
        file_status = next(item for item in self.catalogue.scan_statuses(self.root) if item["path"] == str(path))
        self.assertEqual(file_status["status"], "online-only")
        with patch("cratebuilder.audio.sf.read", side_effect=AssertionError("Unchanged recovery reread audio")):
            available = self.scan()
        self.assertTrue(available.samples[0].features["available"])
        file_status = next(item for item in self.catalogue.scan_statuses(self.root) if item["path"] == str(path))
        self.assertEqual(file_status["status"], "available")


if __name__ == "__main__":
    unittest.main()
