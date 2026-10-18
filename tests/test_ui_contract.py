import contextlib
import copy
import io
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cratebuilder.__main__ import main
from cratebuilder.catalogue import Catalogue
from cratebuilder.library import LibraryStore
from cratebuilder.model import Assignment, Sample, Session
from cratebuilder.ui import App, snapshot_session, update_sources


def sample(index, kind):
    return Sample(str(index), f"C:/samples/{kind}/{index}.wav", f"{kind}/{index}.wav", kind, 1,
                  "test label", .2, 44100, 1, f"audio-{index}", 100, 1,
                  {"centroid": index * 90 + 100, "decay": .1, "crest": 8, "flatness": .3,
                   "waveform": [.1, .2], "available": True}, tags=["kitchen"] if kind == "foley" else [])


class UiContractTests(unittest.TestCase):
    def test_offline_source_retains_its_sounds_and_does_not_block_healthy_source(self):
        old, new = sample(0, "foley"), sample(1, "kick")
        old.path, new.path = "C:/offline/a.wav", "C:/healthy/b.wav"
        previous = Session("C:/offline", [old], sources=["C:/offline", "C:/healthy"])
        previous.pads[9] = Assignment(old.id, locked=True)
        statuses = []
        catalogue = SimpleNamespace(cached_samples=lambda root: [old] if root == "C:/offline" else [],
                                    record_scan_status=lambda *args, **kwargs: statuses.append((args, kwargs)))
        fresh = Session("C:/healthy", [new])
        with patch("cratebuilder.ui.scan_folder", side_effect=[ValueError("Network offline"), fresh]):
            result = update_sources(["C:/offline", "C:/healthy"], previous, "cache", catalogue, lambda *_: None, None)
        self.assertFalse(result.by_id[old.id].features["available"])
        self.assertIn(new.id, result.by_id)
        self.assertEqual(result.pads[9].sample_id, old.id)
        self.assertTrue(result.pads[9].locked)
        self.assertEqual(statuses[0][0][1], "unavailable")
        self.assertTrue(any("Network offline" in note for note in result.scan_notes))

    def poll_state(self, closing=False):
        state = SimpleNamespace(events=queue.Queue(), busy=True, closing=closing, _closed=False,
                                job_controls=lambda: [], pad_lock_checks={}, session=None,
                                cancel_button=SimpleNamespace(configure=lambda **_: None),
                                refresh=lambda: None, statuses=[], scheduled=[], closed=[])
        state.update_generation_controls = lambda: None
        state.sync_library_controls = lambda: None
        state.paint_pads = lambda: None
        state.set_status = state.statuses.append
        state.poll = lambda: App.poll(state)
        state.after = lambda *args: state.scheduled.append(args)
        def close():
            state._closed = True
            state.closed.append(True)
        state.close_app = close
        return state

    def test_callback_failure_keeps_ui_polling(self):
        state = self.poll_state()
        def fail(_):
            raise OSError("Explorer unavailable")
        state.events.put(("done", (fail, None, True)))
        with patch("cratebuilder.ui.messagebox.showerror") as error:
            App.poll(state)
        self.assertFalse(state.busy)
        self.assertEqual(state.scheduled[0][0], 70)
        self.assertIn("Explorer unavailable", state.statuses[-1])
        error.assert_called_once()

    def test_closing_applies_completed_state_and_skips_export_dialog_callbacks(self):
        applied = []
        state = self.poll_state(closing=True)
        state.events.put(("done", (applied.append, "new kit", True)))
        App.poll(state)
        self.assertEqual(applied, ["new kit"])
        self.assertTrue(state.closed)
        self.assertFalse(state.scheduled)
        state = self.poll_state(closing=True)
        state.events.put(("done", (applied.append, "export dialog", False)))
        App.poll(state)
        self.assertEqual(applied, ["new kit"])
        self.assertTrue(state.closed)

    def test_worker_assignments_are_isolated_and_library_can_reuse_statistics(self):
        session = Session("C:/samples", [sample(0, "kick")])
        session.pads[0] = Assignment("0", locked=True)
        session.sources = ["C:/samples"]
        snapshot = snapshot_session(session)
        snapshot.pads[0].locked = False
        snapshot.sources.append("D:/other")
        self.assertTrue(session.pads[0].locked)
        self.assertEqual(session.sources, ["C:/samples"])
        self.assertIs(snapshot.samples, session.samples)

    def test_refresh_upper_preserves_other_banks_lower_drums_and_locks(self):
        kinds = ("kick", "snare", "fill", "rim", "clap", "hat", "open_hat", "cymbal", "perc", "foley")
        samples = [sample(i, kinds[i % len(kinds)]) for i in range(80)]
        session = Session("C:/samples", samples, kit_size=16, target="sp404")
        for i in range(64):
            session.pads[i] = Assignment(str(i), f"saved {i}", locked=i in (3, 9, 30))
        original = copy.deepcopy(session.pads)
        state = SimpleNamespace(refresh=lambda: None, autosave=lambda: None, set_status=lambda _: None)
        state.run_job = lambda work, done: done(work())
        with tempfile.TemporaryDirectory() as temporary:
            state.library_store = LibraryStore(Path(temporary) / "library.sqlite3")
            App.build_upper(state, snapshot_session(session), 0)
        for i in list(range(8)) + list(range(16, 64)) + [9]:
            self.assertEqual(state.session.pads[i].sample_id, original[i].sample_id)
            self.assertEqual(state.session.pads[i].reason, original[i].reason)
        self.assertEqual([p.locked for p in state.session.pads], [p.locked for p in original])
        self.assertTrue(all(p.sample_id for p in state.session.pads[8:16]))

    def test_cli_folder_rule_persists_without_importing_or_renaming_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kitchen = root / "Kitchen"
            kitchen.mkdir()
            audio = kitchen / "FOLIAGE_SOUNDS_103.wav"
            audio.write_bytes(b"original sample placeholder")
            database = root / "catalogue.sqlite3"
            args = ["cratebuilder", "--catalogue", str(database), "--tag-folder", str(kitchen),
                    "--tags", "Kitchen,utensils,percussion", "--kind", "foley"]
            with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(audio.read_bytes(), b"original sample placeholder")
            self.assertEqual(list(kitchen.iterdir()), [audio])
            rule = Catalogue(database).rules_for(audio)
            self.assertEqual(rule["kind"], "foley")
            self.assertCountEqual(rule["tags"], ["kitchen", "percussion", "utensils"])


if __name__ == "__main__":
    unittest.main()
