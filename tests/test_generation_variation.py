"""Behavioral regressions for useful generation, rather than seed changes alone.

All samples are metadata fixtures. These tests never open audio or a device.
"""
import copy
import unittest

from cratebuilder.editing import regenerate_indices, set_bank_locked
from cratebuilder.matching import Matcher
from cratebuilder.model import Assignment, Sample, Session


def sample_record(kind, number, *, identity=None, alias="", position=None):
    position = number if position is None else position
    name = f"{kind}-{number}{alias}"
    return Sample(name, f"C:/test-library/{kind}/{name}.wav", f"{kind}/{name}.wav", kind, 1,
                  "Known one-shot role", .12 + position * .01, 48000, 1,
                  "file-bytes-" + name, 1000, 1,
                  {"centroid": 300 + position * 130, "decay": .08 + position * .025,
                   "crest": 10, "flatness": .025, "leading": 0, "clipped": 0,
                   "active_duration": .12 + position * .01, "waveform": [.2, .4],
                   "audio_hash": identity or "sound-" + name, "available": True},
                  tags=["short", "click", "rattle", "ringing", "low-hit", "textural"], source="One sample pack")


def sound_identity(session, index):
    sample = session.by_id[session.pads[index].sample_id]
    return sample.features.get("audio_hash") or sample.fingerprint


def one_role_session(size, kind="kick", slot=4, aliases=0):
    # The middle sound is the clear nearest match to a neutral target. Its two
    # equally usable companions lie beyond the old algorithm's tiny seed jitter.
    sounds = [sample_record(kind, i) for i in range(3)]
    sounds += [sample_record(kind, 1, identity=sounds[1].features["audio_hash"],
                             alias=f"-copy-{i}", position=1) for i in range(aliases)]
    session = Session("C:/test-library", sounds, kit_size=size, target="sp404", surprise=.5,
                      allow_substitutes=False)
    session.pads = [Assignment(None, "Keep other pads empty", locked=True) for _ in range(64)]
    session.pads[slot] = Assignment(sounds[1].id, "Current favourite", locked=False)
    return session


def full_library_session(size):
    sounds = []
    for kind in ("kick", "snare", "rim", "clap", "hat", "open_hat", "cymbal", "fill"):
        sounds.extend(sample_record(kind, i) for i in range(16))
    for kind in ("foley", "perc"):
        sounds.extend(sample_record(kind, i) for i in range(24))
    return Session("C:/test-library", sounds, kit_size=size, target="sp404", surprise=.6,
                   allow_substitutes=False)


class GenerationVariationTests(unittest.TestCase):
    def assert_changes_each_time(self, session, slot, rounds=12):
        seen = {sound_identity(session, slot)}
        for iteration in range(rounds):
            previous = sound_identity(session, slot)
            regenerate_indices(session, [slot])
            current = sound_identity(session, slot)
            self.assertNotEqual(current, previous,
                                f"Generation {iteration + 1} repeated the same audio despite usable alternatives")
            seen.add(current)
        return seen

    def test_core_pad_never_immediately_repeats_with_three_valid_sounds_in_either_mode(self):
        for size in (8, 16):
            for kind, slot in (("kick", 4), ("snare", 5), ("hat", 6), ("open_hat", 7)):
                with self.subTest(size=size, kind=kind):
                    self.assert_changes_each_time(one_role_session(size, kind, slot), slot)

    def test_creative_pad_never_immediately_repeats_with_three_valid_sounds(self):
        self.assert_changes_each_time(one_role_session(16, "foley", 8), 8)

    def test_familiar_surprise_setting_still_generates_different_sounds(self):
        for size, kind, slot in ((8, "kick", 4), (16, "kick", 4), (16, "foley", 8)):
            with self.subTest(size=size, kind=kind):
                session = one_role_session(size, kind, slot)
                session.surprise = 0
                self.assert_changes_each_time(session, slot, rounds=6)

    def test_copies_with_different_paths_and_file_hashes_are_not_fresh_audio_choices(self):
        for size in (8, 16):
            with self.subTest(size=size):
                # More aliases than the generation shortlist used to retain: a
                # path-based shortlist must not crowd both real alternatives out.
                session = one_role_session(size, aliases=160)
                self.assertGreater(len({s.fingerprint for s in session.samples}), 128)
                self.assertEqual(len({s.features["audio_hash"] for s in session.samples}), 3)
                self.assert_changes_each_time(session, 4, rounds=8)

    def test_regenerating_one_pad_reaches_more_than_two_candidates_when_many_are_equally_usable(self):
        for size in (8, 16):
            with self.subTest(size=size):
                session = one_role_session(size)
                session.samples = [sample_record("kick", i) for i in range(9)]
                session.pads[4] = Assignment(session.samples[4].id)
                seen = self.assert_changes_each_time(session, 4, rounds=24)
                self.assertGreater(len(seen), 2, "Generation only alternated two favourite sounds in a larger valid pool")

    def test_full_kit_generation_explores_each_role_and_keeps_audio_unique_when_supplied(self):
        allowed = ({"kick"}, {"fill"}, {"snare", "rim", "clap"}, {"cymbal"},
                   {"kick"}, {"snare"}, {"hat"}, {"open_hat"})
        for size in (8, 16):
            with self.subTest(size=size):
                session = full_library_session(size)
                seen = [set() for _ in range(size)]
                for iteration in range(10):
                    session.seed += 1
                    Matcher(session).build(0)
                    identities = []
                    for slot in range(size):
                        sample = session.by_id[session.pads[slot].sample_id]
                        kinds = allowed[slot] if slot < 8 else {"perc", "foley", "unknown"}
                        self.assertIn(sample.kind, kinds)
                        identity = sound_identity(session, slot)
                        seen[slot].add(identity)
                        identities.append(identity)
                    self.assertEqual(len(set(identities)), size,
                                     f"Kit generation {iteration + 1} repeated audio despite a sufficient library")
                self.assertTrue(all(len(choices) > 1 for choices in seen),
                                "These roles never varied: " + ", ".join(str(i) for i, choices in enumerate(seen) if len(choices) <= 1))

    def test_single_pad_regeneration_is_reproducible_from_identical_saved_state(self):
        for size in (8, 16):
            with self.subTest(size=size):
                left = one_role_session(size, aliases=5)
                left.history = ["older-sound", sound_identity(left, 4)]
                right = Session.from_dict(left.to_dict())
                for _ in range(6):
                    regenerate_indices(left, [4, 4])
                    regenerate_indices(right, [4])
                    self.assertEqual(left.to_dict(), right.to_dict())

    def test_whole_kit_generation_is_reproducible_given_same_start_seed_and_history(self):
        for size in (8, 16):
            with self.subTest(size=size):
                left = full_library_session(size)
                Matcher(left).build(0)
                right = Session.from_dict(left.to_dict())
                for _ in range(4):
                    left.seed += 1
                    right.seed += 1
                    Matcher(left).build(0)
                    Matcher(right).build(0)
                    self.assertEqual(left.to_dict(), right.to_dict())

    def test_repeated_selected_regeneration_preserves_every_outside_field_and_both_lock_types(self):
        for size in (8, 16):
            with self.subTest(size=size):
                session = full_library_session(size)
                Matcher(session).build(0)
                session.pads[9] = Assignment(None, "An intentional locked gap", "Keep this warning", locked=True)
                session.pads[36] = Assignment(session.samples[0].id, "Protected bank sound", "Original warning")
                set_bank_locked(session, 2, True)
                before = copy.deepcopy(session.pads)
                outside_objects = list(session.pads)
                original_locks = [p.locked for p in session.pads]
                for _ in range(8):
                    previous = sound_identity(session, 4)
                    regenerate_indices(session, [4, 9, 36])
                    self.assertNotEqual(sound_identity(session, 4), previous)
                    for index in set(range(64)) - {4}:
                        self.assertIs(session.pads[index], outside_objects[index])
                        self.assertEqual(session.pads[index], before[index])
                    self.assertEqual([p.locked for p in session.pads], original_locks)
                    self.assertEqual(session.locked_banks, [2])

    def test_eight_pad_regeneration_records_history_too(self):
        session = one_role_session(8)
        before = list(session.history)
        regenerate_indices(session, [4])
        self.assertNotEqual(session.history, before, "Eight-pad generation did not remember its chosen sound")
        self.assertIn(sound_identity(session, 4), session.history)

    def test_eight_pad_full_kit_build_records_choices_in_history(self):
        session = full_library_session(8)
        Matcher(session).build(0)
        chosen = {sound_identity(session, slot) for slot in range(8)}
        self.assertTrue(chosen.issubset(set(session.history)))

    def test_one_unique_sound_with_many_file_aliases_is_reused_with_an_explanation(self):
        for size in (8, 16):
            with self.subTest(size=size):
                sounds = [sample_record("kick", i, identity="only-one-real-kick") for i in range(12)]
                session = Session("C:/test-library", sounds, kit_size=size, target="sp404", allow_substitutes=False)
                session.pads = [Assignment(None, locked=True) for _ in range(64)]
                session.pads[4] = Assignment(sounds[0].id)
                for _ in range(3):
                    regenerate_indices(session, [4])
                    self.assertIsNotNone(session.pads[4].sample_id)
                    self.assertEqual(sound_identity(session, 4), "only-one-real-kick")
                    explanation = (session.pads[4].reason + " " + session.pads[4].warning).lower()
                    self.assertRegex(explanation, r"reus|repeat|only|alternative|limited|one (?:unique|distinct)|no (?:different|unused)",
                                     "A necessary repeat needs a clear limited-library explanation")

    def test_prior_audio_is_reused_rather_than_duplicating_a_locked_other_pad_when_no_unused_alternative_exists(self):
        for size in (8, 16):
            with self.subTest(size=size):
                sounds = [sample_record("kick", 1, identity="current-kick", alias=f"-alias-{i}") for i in range(12)]
                locked = sample_record("kick", 0, identity="locked-other-kick")
                session = Session("C:/test-library", sounds + [locked], kit_size=size, target="sp404", allow_substitutes=False)
                session.pads = [Assignment(None, locked=True) for _ in range(64)]
                session.pads[0] = Assignment(locked.id, "Keep my other kick", locked=True)
                session.pads[4] = Assignment(sounds[0].id)
                regenerate_indices(session, [4])
                self.assertEqual(sound_identity(session, 4), "current-kick")
                self.assertEqual(sound_identity(session, 0), "locked-other-kick")

    def test_recent_head_of_a_large_pool_cannot_hide_fresh_suitable_sounds(self):
        session = one_role_session(16, "foley", 8)
        session.samples = [sample_record("foley", i, position=1) for i in range(180)]
        session.history = [sample.features["audio_hash"] for sample in session.samples[:128]]
        candidates = Matcher(session).rank(8, include_current=True, for_generation=True, limit=128)
        self.assertTrue(candidates)
        self.assertTrue(all(sample.features["audio_hash"] not in session.history for _, sample, _ in candidates))

    def test_large_pack_does_not_hide_a_compatible_small_source_before_sampling(self):
        session = one_role_session(16, "foley", 8)
        session.samples = [sample_record("foley", i, position=1) for i in range(180)]
        for sample in session.samples:
            sample.source = "Large pack"
        small = sample_record("foley", 181, position=1)
        small.source = "Small pack"
        # A modest quality difference keeps this compatible sound below the
        # first 128 raw scores, while still inside the musical shortlist band.
        small.features["leading"] = .2
        session.samples.append(small)
        candidates = Matcher(session).rank(8, include_current=True, for_generation=True, limit=128)
        self.assertIn(small.id, [sample.id for _, sample, _ in candidates])


if __name__ == "__main__":
    unittest.main()
