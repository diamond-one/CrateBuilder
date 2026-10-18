import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from cratebuilder.catalogue import Catalogue
from cratebuilder.audio import scan_folder
from cratebuilder.library import LibraryStore
from cratebuilder.model import Assignment, Session
from cratebuilder.ni import discover_expansions, iter_expansion_items, read_media_sample
from cratebuilder.ni_controls import NILibraryMixin, _snapshot, item_for_sample
from cratebuilder.ui import App


class Controller(NILibraryMixin):
    def __init__(self, root):
        self.data_dir = root / "data"
        self.library_store = LibraryStore(self.data_dir / "library.sqlite3")
        self.catalogue = Catalogue(self.data_dir / "catalogue.sqlite3")
        self.cancel_event = threading.Event()
        self.progress = lambda *_: None
        self.session, self.busy, self.bank, self.selected = None, False, 0, 12
        self.messages = []
        self.set_status = self.messages.append
        self.run_job = lambda work, done, **_: done(work())

    def _accept_library_session(self, session, message):
        self.session = session
        self.messages.append(message)


class NIControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.controller = Controller(self.root)
        self.samples = self.root / "NI/Test Library/Samples"
        for folder, title in (("Drums/Kick", "Kick Test"), ("Drums/Hihat", "OpenHH Test"),
                              ("One Shots/Synth", "Synth[86] Dm"), ("Loops/Bass", "Bass[89] A Brinks")):
            path = self.samples / folder / (title + ".wav")
            path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(path, np.sin(np.arange(4000) * .05) * np.exp(-np.arange(4000) / 1000), 44100)
        self.pack = discover_expansions(self.root / "NI")[0]
        self.items = list(iter_expansion_items(self.pack))
        self.controller.library_store.upsert_items(self.items)

    def tearDown(self):
        self.temporary.cleanup()

    def test_manual_drum_does_not_make_an_entire_pack_appear_loaded(self):
        drum = next(item for item in self.items if item["kind"] == "kick")
        sound = read_media_sample(drum, catalogue=self.controller.catalogue)
        session = Session(str(self.samples), [sound], sources=[str(self.samples)])
        session.pads[4] = Assignment(sound.id, "keep me", locked=True)
        original = copy.deepcopy(session.pads)
        loaded, notes = self.controller._load_active_packs(session, [self.pack["id"]])
        self.assertFalse(notes)
        self.assertEqual(len(loaded.samples), 2)
        self.assertEqual(loaded.pads, original)
        with patch("cratebuilder.ni_controls.scan_folder", side_effect=AssertionError("unnecessary rescan")):
            self.controller._load_active_packs(loaded, [self.pack["id"]])

    def test_registration_preserves_origin_and_personal_marks(self):
        item = next(item for item in self.items if item["category"] == "One Shots")
        sound = read_media_sample(item, catalogue=self.controller.catalogue)
        self.controller.library_store.set_mark(sound.path, favourite=True, hidden=True)
        session = self.controller.register_library_samples(Session(str(self.samples), [sound]))
        stored = self.controller.library_store.query_items(hidden_only=True)[0]
        self.assertEqual(Path(stored["samples_root"]), self.samples)
        self.assertEqual(stored["fingerprint"], sound.fingerprint)
        self.assertEqual(stored["key"], "Dm")
        self.assertTrue(session.samples[0].features["favourite"])
        self.assertTrue(session.samples[0].features["hidden"])

    def test_manual_one_shot_changes_only_selected_pad_and_locks_it(self):
        controller = self.controller
        item = next(item for item in self.items if item["category"] == "One Shots")
        controller.session = Session(str(self.samples), [], kit_size=16)
        controller.session.pads[3] = Assignment(reason="keep outside", locked=True)
        before = copy.deepcopy(controller.session.pads)
        controller.use_library_item(item)
        self.assertTrue(controller.session.pads[12].locked)
        chosen = controller.session.by_id[controller.session.pads[12].sample_id]
        self.assertTrue(chosen.features["manual_only"])
        self.assertEqual(chosen.kind, "unknown")
        for index in range(64):
            if index != 12:
                self.assertEqual(controller.session.pads[index], before[index])
        self.assertEqual(controller.library_store.query_items(category="One Shots")[0]["used_count"], 1)

    def test_locked_bank_and_loop_actions_do_not_load_audio_or_change_pads(self):
        controller = self.controller
        controller.session = Session(str(self.samples), [], locked_banks=[0])
        with patch("cratebuilder.ni_controls.read_media_sample", side_effect=AssertionError("must not read")):
            controller.use_library_item(self.items[0])
            controller.session.locked_banks = []
            controller.use_library_item(next(item for item in self.items if item["category"] == "Loops"))
        self.assertTrue(all(pad.sample_id is None for pad in controller.session.pads))

    def test_no_matching_generation_keeps_existing_kit(self):
        controller = self.controller
        sound = read_media_sample(next(item for item in self.items if item["kind"] == "kick"))
        controller.session = Session(str(self.samples), [sound], kit_size=16, generation_sources=[])
        controller.session.pads[4] = Assignment(sound.id, "keep existing")
        before = copy.deepcopy(controller.session.pads)
        controller.stop = lambda: None
        controller.settings_changed = lambda: None
        with self.assertRaisesRegex(ValueError, "No sounds match"):
            App.generate_indices(controller, [4])
        self.assertEqual(controller.session.pads, before)

    def test_snapshot_filter_choices_are_isolated(self):
        session = Session(str(self.samples), [], generation_sources=[self.pack["id"]])
        copied = _snapshot(session)
        copied.generation_sources.clear()
        self.assertEqual(session.generation_sources, [self.pack["id"]])

    def test_failed_pack_reload_retains_choices_but_excludes_unavailable_audio(self):
        loaded, _ = self.controller._load_active_packs(Session(str(self.samples), []), [self.pack["id"]])
        loaded.pads[4] = Assignment(loaded.samples[0].id, locked=True)
        before = copy.deepcopy(loaded.pads)
        with patch("cratebuilder.ni_controls.scan_folder", side_effect=ValueError("Network offline")):
            failed, notes = self.controller._load_active_packs(loaded, [self.pack["id"]], force=True)
        self.assertEqual(failed.pads, before)
        self.assertTrue(notes)
        self.assertTrue(all(not sample.features["available"] for sample in failed.samples))
        self.assertEqual(self.controller.catalogue.scan_statuses(self.samples / "Drums")[0]["status"], "unavailable")

    def test_scanned_bracket_bpm_is_preserved_in_browser_metadata(self):
        path = self.samples / "Drums/Percussion/Perc[89] A Brinks.wav"
        path.parent.mkdir(parents=True)
        sf.write(path, np.sin(np.arange(4000) * .05), 44100)
        session = scan_folder(self.samples / "Drums", self.root / "cache")
        sound = next(sample for sample in session.samples if sample.path == str(path))
        self.assertEqual(sound.bpm, 89)
        self.assertEqual(item_for_sample(sound)["bpm"], 89)
        sound.bpm = None
        self.assertEqual(item_for_sample(sound)["bpm"], 89)

    def test_user_samples_keep_their_pack_without_ni_tags(self):
        folder = self.root / "User/Cookin Soul/Kicks"
        folder.mkdir(parents=True)
        path = folder / "Kick Dusty.wav"
        sf.write(path, np.sin(np.arange(4000) * .05), 44100)
        session = scan_folder(folder.parent, self.root / "cache", catalogue=self.controller.catalogue)
        sound = session.samples[0]
        original_source = sound.source
        item = item_for_sample(sound)
        for store in (self.controller.catalogue, None):
            chosen = read_media_sample(item, catalogue=store)
            self.assertEqual(chosen.source, original_source)
            self.assertNotIn("ni_source", chosen.features)
            self.assertNotIn("ni", chosen.tags)


if __name__ == "__main__":
    unittest.main()
