"""NI support uses synthetic expansion trees; no installed audio is analysed."""
from pathlib import Path
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cratebuilder.audio import (ANALYSIS_VERSION, apply_folder_rules, classify_name,
                                scan_folder)
from cratebuilder.catalogue import Catalogue, canonical_path
from cratebuilder.ni import (classify_ni_path, discover_expansions,
                             iter_expansion_items, load_media_audio,
                             metadata_for_path, read_media_sample)


class NITests(unittest.TestCase):
    def setUp(self):
        test_parent = Path(__file__).resolve().parents[2] / "test-temp"
        test_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="ni-", dir=test_parent)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.assertTrue(self.work.is_relative_to(test_parent.resolve()))
        self.public = self.work / "Public Documents"
        self.samples = self.public / "Aquarius Earth Library" / "Samples"
        (self.samples / "Drums").mkdir(parents=True)
        self.cache = self.work / "cache"
        self.catalogue = Catalogue(self.cache / "catalogue.sqlite3")

    def write_audio(self, relative, duration=.125, rate=48000, amplitude=.3):
        path = self.samples / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        time = np.arange(round(duration * rate)) / rate
        # Low-frequency audio deliberately disagrees with hat labels and would
        # be guessed as kick without the explicit NI context.
        data = amplitude * np.sin(2 * np.pi * 70 * time)
        sf.write(path, data, rate, subtype="FLOAT")
        return path

    def item(self, path):
        stat = path.stat()
        return {**metadata_for_path(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def test_discovery_uses_only_immediate_expansions_with_samples_and_drums(self):
        (self.public / "Kontakt Instrument Library" / "Samples").mkdir(parents=True)
        (self.public / "Unrelated" / "Samples" / "Drums").mkdir(parents=True)
        (self.public / "Nested" / "Hidden Library" / "Samples" / "Drums").mkdir(parents=True)
        with patch("soundfile.info", side_effect=AssertionError("Discovery opened audio")):
            with patch("soundfile.read", side_effect=AssertionError("Discovery opened audio")):
                expansions = discover_expansions(self.public)
        self.assertEqual(len(expansions), 1)
        expansion = expansions[0]
        self.assertEqual(expansion["name"], "Aquarius Earth")
        self.assertEqual(expansion["samples_root"], str(self.samples))
        self.assertEqual(expansion["drums_root"], str(self.samples / "Drums"))
        self.assertEqual(expansion["id"], "ni:" + canonical_path(self.samples)[1].casefold())
        self.assertEqual(discover_expansions(self.work / "unavailable"), [])

    def test_metadata_iteration_lists_all_categories_and_rx2_without_audio_decode(self):
        paths = ["Drums/Kick/Kick Example.wav", "Drums/Hihat/OpenHH Example.aiff",
                 "One Shots/Chord/Chord Example.flac", "Loops/Drums/DrumLoop 120BPM Cm.rx2",
                 "Instruments/Bass/Bass Note.wav"]
        for relative in paths:
            path = self.samples / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"metadata only, deliberately not valid audio")
        hidden = self.samples / "Drums" / "Kick" / "._Kick Example.wav"
        hidden.write_bytes(b"Mac metadata")
        secret = self.samples / ".hidden" / "ignored.wav"
        secret.parent.mkdir()
        secret.write_bytes(b"hidden metadata")
        expansion = discover_expansions(self.public)[0]
        with patch("soundfile.info", side_effect=AssertionError("Indexing opened audio")):
            with patch("soundfile.read", side_effect=AssertionError("Indexing opened audio")):
                items = list(iter_expansion_items(expansion))
        self.assertEqual(len(items), 5)
        self.assertEqual({item["category"] for item in items}, {"Drums", "One Shots", "Loops", "Instruments"})
        self.assertEqual({item["provider"] for item in items}, {"Native Instruments"})
        self.assertEqual({item["pack"] for item in items}, {"Aquarius Earth"})
        self.assertEqual({item["source"] for item in items}, {expansion["id"]})
        loop = next(item for item in items if item["path"].endswith(".rx2"))
        self.assertEqual((loop["kind"], loop["subtype"], loop["bpm"], loop["key"]), ("skip", "Drums", 120.0, "Cm"))
        self.assertTrue(all(item["size"] > 0 and item["mtime_ns"] > 0 for item in items))

    def test_ni_classification_handles_actual_hat_and_percussion_prefixes(self):
        cases = {
            "Drums/Hihat/OpenHH BabyMayka.wav": "open_hat",
            "Drums/Hihat/ClosedHH BabyMayka.wav": "hat",
            "Drums/HiHat Open/Example.wav": "open_hat",
            "Drums/Percussion/DrumBreak MoMoney 1.wav": "skip",
            "Drums/Percussion/DrumFill JazzyDrums 1.wav": "fill",
            "Drums/Snare/SnareRoll Example.wav": "fill",
            "Drums/Tom/Tom Example.wav": "perc",
            "Drums/Shaker/Example.wav": "perc",
            "Drums/Percussion/Example.wav": "perc",
            "Loops/Drums/OpenHH Misleading.wav": "skip",
            "Instruments/Keys/Kick Misleading.wav": "skip",
            "One Shots/Chord/Kick Misleading.wav": "skip",
            "One Shots/Single Note/Example.wav": "skip",
            "One Shots/Stab & Hit/Example.wav": "skip",
            "One Shots/Vocal/Example.wav": "skip",
            "One Shots/SFX/Example.wav": "unknown",
        }
        for relative, expected in cases.items():
            with self.subTest(relative=relative):
                full = self.samples / relative
                self.assertEqual(classify_ni_path(full)[0], expected)
                self.assertEqual(classify_name(full)[0], expected)
                self.assertEqual(classify_ni_path(relative)[0], expected)
        self.assertIsNone(classify_ni_path("Unsorted/mystery.wav"))
        self.assertIsNone(metadata_for_path("C:/Unsorted/mystery.wav"))
        self.assertEqual(classify_name(Path("SNARES/HAT SLAP.wav"))[0], "snare")

    def test_explicit_bpm_and_key_are_read_but_sample_variation_numbers_are_not_guessed(self):
        labelled = metadata_for_path(self.samples / "Loops" / "Keys" / "Theme 95BPM F#min.wav")
        self.assertEqual((labelled["bpm"], labelled["key"]), (95.0, "F#min"))
        unknown = metadata_for_path(self.samples / "Drums" / "Kick" / "Kick Example 95.wav")
        self.assertIsNone(unknown["bpm"])
        self.assertIsNone(unknown["key"])

    def test_ni_bracket_tempo_and_immediately_following_key_labels_are_recognized(self):
        for name, bpm, key in (("Bass[89] A Brinks.wav", 89.0, "A"),
                               ("Synth[86] Dm.wav", 86.0, "Dm"),
                               ("Keys[120] F# Sunrise.wav", 120.0, "F#"),
                               ("Bass[95] Bb Night.wav", 95.0, "Bb"),
                               ("Synth[100] C#min Example.wav", 100.0, "C#min")):
            with self.subTest(name=name):
                item = metadata_for_path(self.samples / "Loops" / "Construction" / name)
                self.assertEqual((item["bpm"], item["key"]), (bpm, key))

    def test_bare_letters_outside_explicit_ni_key_positions_are_not_guessed(self):
        for name in ("Kit A Brinks.wav", "Bass Brinks A.wav", "Synth[86] Another A Kit.wav",
                     "Bass[89] Apple Brinks.wav", "Bass[89] A2 Brinks.wav", "Bass[5] A Brinks.wav"):
            with self.subTest(name=name):
                item = metadata_for_path(self.samples / "Loops" / "Construction" / name)
                self.assertIsNone(item["key"])
        item = metadata_for_path(self.samples / "Loops" / "Construction" / "Bass[5] A Brinks.wav")
        self.assertIsNone(item["bpm"])

    def test_iterator_cancellation_and_redirected_directories_are_safe(self):
        self.write_audio("Drums/Kick/Kick.wav")
        redirected = self.samples / "Drums" / "Redirected"
        redirected.mkdir()
        (redirected / "not-indexed.wav").write_bytes(b"metadata")
        expansion = discover_expansions(self.public)[0]
        with patch("cratebuilder.ni._is_link", side_effect=lambda path: Path(path) == redirected):
            items = list(iter_expansion_items(expansion))
        self.assertEqual(len(items), 1)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            list(iter_expansion_items(expansion, cancel=stop))
        invalid = {**expansion, "samples_root": str(self.work)}
        with self.assertRaisesRegex(ValueError, "outside"):
            list(iter_expansion_items(invalid))

    def test_scanning_drums_uses_ni_roles_and_caches_stable_provenance(self):
        opened = self.write_audio("Drums/Hihat/OpenHH Example.wav")
        self.write_audio("Drums/Hihat/ClosedHH Example.wav")
        self.write_audio("Drums/Percussion/DrumFill Example.wav")
        self.write_audio("Drums/Percussion/DrumBreak Example.wav")
        first = scan_folder(self.samples / "Drums", self.cache, catalogue=self.catalogue)
        self.assertEqual(len(first.samples), 3)
        by_name = {Path(sample.path).name: sample for sample in first.samples}
        self.assertEqual(by_name[opened.name].kind, "open_hat")
        self.assertEqual({sample.kind for sample in first.samples}, {"open_hat", "hat", "fill"})
        expected_source = discover_expansions(self.public)[0]["id"]
        for item in first.samples:
            self.assertEqual(item.source, expected_source)
            self.assertEqual(item.features["ni_source"], expected_source)
            self.assertEqual(item.features["provider"], "Native Instruments")
            self.assertEqual(item.features["pack"], "Aquarius Earth")
            self.assertEqual(item.features["category"], "Drums")
            self.assertTrue({"ni", "aquarius earth", "drums"}.issubset(item.tags))
        with patch("cratebuilder.audio.sf.read", side_effect=AssertionError("Cached scan reopened audio")):
            second = scan_folder(self.samples / "Drums", self.cache, catalogue=self.catalogue)
            broader = scan_folder(self.samples, self.cache, catalogue=self.catalogue)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual({item.source for item in broader.samples}, {expected_source})

    def test_folder_rule_edits_preserve_ni_tags_and_baseline(self):
        opened = self.write_audio("Drums/Hihat/OpenHH Example.wav")
        session = scan_folder(self.samples / "Drums", self.cache, catalogue=self.catalogue)
        source = session.samples[0].source
        self.catalogue.set_folder_rule(opened.parent, ["favorite"], "foley")
        with patch("cratebuilder.audio.sf.read", side_effect=AssertionError("Tags opened audio")):
            apply_folder_rules(session, self.catalogue)
            self.assertEqual(session.samples[0].kind, "foley")
            self.assertTrue({"ni", "aquarius earth", "drums", "hihat", "favorite"}.issubset(session.samples[0].tags))
            self.catalogue.delete_folder_rule(opened.parent)
            apply_folder_rules(session, self.catalogue)
        self.assertEqual(session.samples[0].kind, "open_hat")
        self.assertEqual(session.samples[0].source, source)
        self.assertNotIn("favorite", session.samples[0].tags)

    def test_manual_drum_sample_reuses_analysis_and_preserves_original_audio(self):
        path = self.write_audio("Drums/Hihat/OpenHH Example.wav")
        before = path.read_bytes()
        item = self.item(path)
        first = read_media_sample(item, self.catalogue)
        self.assertEqual(first.kind, "open_hat")
        self.assertFalse(first.features["manual_only"])
        self.assertFalse(first.features["preview_only"])
        self.assertEqual(first.source, item["source"])
        self.assertEqual(len(first.fingerprint), 64)
        with patch("soundfile.read", side_effect=AssertionError("Cached preview reopened audio")):
            with patch("soundfile.info", side_effect=AssertionError("Cached preview reopened header")):
                cached = read_media_sample(item, self.catalogue)
        self.assertEqual(first.to_dict(), cached.to_dict())
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNotNone(self.catalogue.get_sample(path, first.size, first.mtime_ns, ANALYSIS_VERSION))

    def test_manual_ambiguous_and_musical_one_shots_stay_unknown(self):
        for relative in ("One Shots/SFX/SFX Example.wav", "One Shots/Chord/Chord Example.wav"):
            with self.subTest(relative=relative):
                path = self.write_audio(relative)
                selected = read_media_sample(self.item(path), self.catalogue)
                self.assertEqual(selected.kind, "unknown")
                self.assertTrue(selected.features["manual_only"])
                self.assertFalse(selected.features["preview_only"])
                self.assertEqual(selected.features["category"], "One Shots")

    def test_loops_instruments_and_long_drums_are_preview_only_with_bounded_analysis(self):
        for relative in ("Loops/Drums/Loop 120BPM.wav", "Instruments/Keys/Note.wav"):
            path = self.write_audio(relative)
            selected = read_media_sample(self.item(path))
            self.assertTrue(selected.features["preview_only"])
            self.assertTrue(selected.features["manual_only"])
        long_path = self.write_audio("Drums/Kick/Long Kick.wav", duration=16)
        with patch("soundfile.read", wraps=sf.read) as read:
            selected = read_media_sample(self.item(long_path))
        self.assertEqual(read.call_args.kwargs["frames"], 15 * 48000)
        self.assertEqual(selected.duration, 16)
        self.assertTrue(selected.features["preview_only"])
        self.assertEqual(selected.features["preview_frames"], 15 * 48000)

    def test_rx2_is_discoverable_but_does_not_attempt_unsupported_audio_preview(self):
        path = self.samples / "Loops" / "Drums" / "Break.rx2"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"RX2 metadata")
        with patch("soundfile.info", side_effect=AssertionError("RX2 header opened")):
            with patch("soundfile.read", side_effect=AssertionError("RX2 decoded")):
                with self.assertRaisesRegex(ValueError, "convert them to WAV"):
                    read_media_sample(self.item(path))

    def test_browser_audio_is_bounded_resampled_stereo_and_quiet_samples_stay_quiet(self):
        path = self.write_audio("Loops/Drums/Loop.wav", duration=.5, amplitude=.1)
        selected = read_media_sample(self.item(path))
        with patch("soundfile.read", wraps=sf.read) as read:
            data = load_media_audio(selected, seconds=.025, rate=44100)
        self.assertEqual(read.call_args.kwargs["frames"], 1200)
        self.assertEqual(data.shape, (1103, 2))
        self.assertEqual(data.dtype, np.float32)
        self.assertTrue(np.isfinite(data).all())
        np.testing.assert_allclose(data[:, 0], data[:, 1])
        # A short truncated preview can have a small resampler edge overshoot;
        # it should retain its quiet level rather than being normalized.
        self.assertLess(float(np.abs(data).max()), .12)
        self.assertGreater(float(np.abs(data).max()), .09)

    def test_browser_audio_attenuates_overs_and_rejects_changed_sources_before_reading(self):
        path = self.write_audio("One Shots/SFX/Loud.wav", amplitude=1.4)
        selected = read_media_sample(self.item(path))
        data = load_media_audio(selected, seconds=.05)
        self.assertLessEqual(float(np.abs(data).max()), 1)
        self.write_audio("One Shots/SFX/Loud.wav", amplitude=.2)
        os.utime(path, ns=(selected.mtime_ns + 1_000_000_000, selected.mtime_ns + 1_000_000_000))
        with patch("soundfile.read", side_effect=AssertionError("Changed audio was opened")):
            with self.assertRaisesRegex(ValueError, "changed since analysis"):
                load_media_audio(selected)


if __name__ == "__main__":
    unittest.main()
