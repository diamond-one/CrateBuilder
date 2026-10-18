"""Selection edits preserve every unrelated assignment and exact existing lock flags."""
import copy
from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cratebuilder.editing import bank_lock_state, regenerate_indices, set_bank_locked
from cratebuilder.matching import Matcher, audio_identity
from cratebuilder.model import Assignment, Sample, Session


def session_fixture(size=16):
    samples = []
    kinds = ("kick", "snare", "fill", "clap", "hat", "open_hat", "cymbal", "foley", "perc")
    for number in range(108):
        kind = kinds[number % len(kinds)]
        samples.append(Sample(str(number), f"C:/samples/{kind}/{number}.wav", f"{kind}/{number}.wav",
                              kind, 1, "test label", .15, 44100, 1, f"audio-{number}", 100, 1,
                              {"centroid": number * 60 + 100, "decay": .04 + number * .003,
                               "crest": 8, "flatness": .03, "active_duration": .15,
                               "waveform": [.1, .2], "available": True}, source=f"pack-{number % 4}"))
    session = Session("C:/samples", samples, kit_size=size, target="sp404", history=["previous-choice"])
    for index in range(64):
        session.pads[index] = Assignment(str(index), f"Original reason {index}", f"Original warning {index}", locked=index in (2, 9, 31))
    return session


class EditingTests(unittest.TestCase):
    def test_one_pad_regeneration_preserves_outside_identity_fields_and_locks(self):
        session = session_fixture()
        original = list(session.pads)
        before = [asdict(pad) for pad in original]
        seed = session.seed
        result = regenerate_indices(session, [4])
        self.assertIs(result, session)
        self.assertEqual(session.seed, seed + 1)
        self.assertEqual(session.by_id[session.pads[4].sample_id].kind, "kick")
        self.assertEqual([pad.locked for pad in session.pads], [pad["locked"] for pad in before])
        for index in set(range(64)) - {4}:
            self.assertIs(session.pads[index], original[index])
            self.assertEqual(asdict(session.pads[index]), before[index])
        self.assertEqual(session.history, ["previous-choice", audio_identity(session.by_id[session.pads[4].sample_id])])

    def test_two_selected_pads_honor_individual_lock_and_locked_empty(self):
        session = session_fixture()
        session.pads[9] = Assignment(None, "Keep this empty", "Saved warning", locked=True)
        before = copy.deepcopy(session.pads)
        regenerate_indices(session, [8, 9])
        self.assertIsNotNone(session.pads[8].sample_id)
        self.assertEqual(session.pads[9], before[9])
        self.assertEqual(session.history[0], "previous-choice")
        self.assertEqual(len(session.history), 2)
        for index in set(range(64)) - {8}:
            self.assertEqual(session.pads[index], before[index])

    def test_cross_bank_selection_increments_seed_once_and_touches_only_requested_pads(self):
        session = session_fixture()
        before = copy.deepcopy(session.pads)
        seed = session.seed
        regenerate_indices(session, [36, 4, 20, 20])
        self.assertEqual(session.seed, seed + 1)
        self.assertEqual(len(session.history), 4)
        for index in set(range(64)) - {4, 20, 36}:
            self.assertEqual(session.pads[index], before[index])
        self.assertTrue(all(session.by_id[session.pads[index].sample_id].kind == "kick" for index in (4, 20, 36)))

    def test_eight_pad_mode_keeps_other_core_kit_and_existing_history(self):
        session = session_fixture(8)
        before = copy.deepcopy(session.pads)
        regenerate_indices(session, [3])
        self.assertEqual(session.by_id[session.pads[3].sample_id].kind, "cymbal")
        self.assertEqual(session.history, ["previous-choice", audio_identity(session.by_id[session.pads[3].sample_id])])
        for index in set(range(64)) - {3}:
            self.assertEqual(session.pads[index], before[index])

    def test_empty_and_entirely_locked_selections_are_exact_noops(self):
        for indices in ([], [2, 9, 31]):
            session = session_fixture()
            before = copy.deepcopy(session.to_dict())
            with patch("cratebuilder.editing.Matcher") as builder:
                self.assertIs(regenerate_indices(session, indices), session)
                builder.assert_not_called()
            self.assertEqual(session.to_dict(), before)

    def test_failed_builder_does_not_change_session_seed_history_or_assignments(self):
        session = session_fixture()
        before = copy.deepcopy(session.to_dict())
        def broken_builder(working):
            self.assertIsNot(working, session)
            working.history.append("must not leak")
            working.pads[0].reason = "must not leak"
            def fail():
                raise RuntimeError("Library unavailable")
            return SimpleNamespace(build=fail)
        with patch("cratebuilder.editing.Matcher", side_effect=broken_builder):
            with self.assertRaisesRegex(RuntimeError, "Library unavailable"):
                regenerate_indices(session, [4])
        self.assertEqual(session.to_dict(), before)

    def test_invalid_indices_are_rejected_without_partial_changes(self):
        for indices in ([4, -1], [4, 64], [4, True], [4, "5"], None, [4, 5.0]):
            with self.subTest(indices=indices):
                session = session_fixture()
                before = copy.deepcopy(session.to_dict())
                with self.assertRaises(ValueError):
                    regenerate_indices(session, indices)
                self.assertEqual(session.to_dict(), before)

    def test_bank_protection_is_independent_and_unlock_preserves_pad_locks(self):
        session = session_fixture()
        self.assertEqual(bank_lock_state(session, 0), "some")
        self.assertEqual(bank_lock_state(session, 2), "none")
        before = copy.deepcopy(session.pads)
        objects = list(session.pads)
        self.assertIs(set_bank_locked(session, 0, True), session)
        self.assertEqual(bank_lock_state(session, 0), "all")
        self.assertEqual(session.locked_banks, [0])
        self.assertEqual(session.pads, before)
        for index, pad in enumerate(session.pads):
            self.assertIs(pad, objects[index])
            old_fields = asdict(before[index])
            self.assertEqual(asdict(pad), old_fields)
        set_bank_locked(session, 0, False)
        self.assertEqual(bank_lock_state(session, 0), "some")
        self.assertEqual(session.locked_banks, [])
        self.assertTrue(session.pads[2].locked)
        self.assertTrue(session.pads[9].locked)
        self.assertTrue(session.pads[31].locked)
        self.assertEqual(session.pads, before)

    def test_bank_flags_round_trip_and_old_sessions_default_to_unprotected(self):
        session = session_fixture()
        set_bank_locked(session, 3, True)
        set_bank_locked(session, 1, True)
        set_bank_locked(session, 1, True)
        restored = Session.from_dict(session.to_dict())
        self.assertEqual(restored.locked_banks, [1, 3])
        self.assertEqual(restored.pads, session.pads)
        legacy = session.to_dict()
        legacy.pop("locked_banks")
        self.assertEqual(Session.from_dict(legacy).locked_banks, [])
        legacy["schema"] = 1
        self.assertEqual(Session.from_dict(legacy).locked_banks, [])

    def test_invalid_saved_bank_flags_are_rejected(self):
        for flags in ([0, 0], [-1], [4], [True], ["1"], [1.0], None, "1"):
            data = session_fixture().to_dict()
            data["locked_banks"] = flags
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "Locked banks"):
                Session.from_dict(data)

    def test_shared_builder_honors_bank_protection_and_skips_its_history(self):
        session = session_fixture()
        set_bank_locked(session, 1, True)
        before = copy.deepcopy(session.pads[16:32])
        locks = [pad.locked for pad in session.pads]
        Matcher(session).build()
        self.assertEqual([p.sample_id for p in session.pads[16:32]], [p.sample_id for p in before])
        self.assertEqual([p.reason for p in session.pads[16:32]], [p.reason for p in before])
        self.assertEqual([pad.locked for pad in session.pads], locks)
        expected = sum(not locked and index // 16 != 1 for index, locked in enumerate(locks))
        self.assertEqual(len(session.history), expected + 1)

    def test_regeneration_of_only_a_protected_bank_is_an_exact_noop(self):
        session = session_fixture()
        set_bank_locked(session, 2, True)
        before = copy.deepcopy(session.to_dict())
        with patch("cratebuilder.editing.Matcher") as builder:
            self.assertIs(regenerate_indices(session, range(32, 48)), session)
            builder.assert_not_called()
        self.assertEqual(session.to_dict(), before)

    def test_locked_bank_survives_regeneration_request_in_both_kit_modes(self):
        for size in (8, 16):
            session = session_fixture(size)
            set_bank_locked(session, 1, True)
            before = copy.deepcopy(session.pads)
            regenerate_indices(session, [4, *range(16, 32)])
            self.assertEqual(session.pads[16:32], before[16:32])
            self.assertEqual([p.locked for p in session.pads], [p.locked for p in before])

    def test_bad_bank_or_lock_value_never_changes_locks(self):
        for bank in (-1, 4, True, "A", 1.0):
            session = session_fixture()
            before = copy.deepcopy(session.to_dict())
            with self.assertRaises(ValueError):
                set_bank_locked(session, bank, True)
            with self.assertRaises(ValueError):
                bank_lock_state(session, bank)
            self.assertEqual(session.to_dict(), before)
        session = session_fixture()
        with self.assertRaises(ValueError):
            set_bank_locked(session, 0, "False")


if __name__ == "__main__":
    unittest.main()
