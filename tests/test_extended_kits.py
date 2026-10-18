"""Regression checks for 16-pad selection, device mapping and verified WAV exports."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from cratebuilder.kit_export import export_kit
from cratebuilder.matching import Matcher, ROLE_POOLS, audio_identity
from cratebuilder.model import (Assignment, ROLES, Sample, Session, hardware_pad_name,
                               kit_count, role_names, sp_pad_number)
from cratebuilder.pgm import PAD_BASE, PAD_SIZE, program_bytes, sample_names
from cratebuilder.preview import render_groove


def sample_record(name, kind="foley", source="small", value=0, tags=()):
    features = dict(centroid=400 + value * 91, decay=.06 + value * .013, crest=8 + value * .2,
                    flatness=.02 + value * .001, clipped=0, leading=0, active_duration=.1,
                    peak=.4, rms=.1, low=.2, high=.2, waveform=[.1], audio_hash="audio-" + name)
    return Sample(name, str(Path(source) / (name + ".wav")), name + ".wav", kind, .9,
                  "Fixture category", .1, 44100, 1, "bytes-" + name, 1, 1, features,
                  tags=list(tags), source=source)


def library():
    records = []
    for kind in ("kick", "snare", "clap", "hat", "open_hat", "fill", "cymbal"):
        for i in range(8):
            records.append(sample_record(f"{kind}-{i}", kind, "drums", i))
    tags = ("low-hit", "click", "rattle", "ringing", "short", "textural", "bright", "tonal")
    for i in range(32):
        records.append(sample_record(f"foley-{i}", "foley" if i % 3 else "unknown",
                                     f"curated-{i % 4}", i, [tags[i % 8]]))
    return records


class ExtendedKitTests(unittest.TestCase):
    def test_legacy_schema_defaults_and_sp_physical_mapping(self):
        old = Session("root", [sample_record("one")]).to_dict()
        old["schema"] = 1
        for field in ("kit_size", "target", "surprise", "sources", "history", "library_revision"):
            old.pop(field)
        loaded = Session.from_dict(old)
        self.assertEqual((loaded.kit_size, loaded.target, kit_count(loaded)), (8, "mpc", 8))
        self.assertEqual(role_names(16)[:8], ROLES)
        self.assertEqual([sp_pad_number(i) for i in (0, 4, 8, 12)], [13, 9, 5, 1])
        self.assertEqual(set(sp_pad_number(i) for i in range(16)), set(range(1, 17)))
        self.assertEqual(hardware_pad_name(16, "sp404"), "B13")
        self.assertEqual(hardware_pad_name(16, "mpc"), "B01")

    def test_cached_sorted_percentiles_equal_original_midrank_definition(self):
        records = [sample_record(str(i), "kick", value=i // 2) for i in range(12)]
        session = Session("root", records)
        matcher = Matcher(session)
        for s in records:
            expected = []
            for feature in ("centroid", "decay", "crest", "flatness"):
                values = np.array([other.features[feature] for other in records])
                expected.append(((values < s.features[feature]).sum() + .5 * (values == s.features[feature]).sum()) / len(records))
            np.testing.assert_allclose(matcher.vectors[s.id], expected)
        second = Matcher(session)
        self.assertIs(matcher.matrix, second.matrix)
        records[0].kind = "excluded"
        records[1].features["available"] = False
        session.library_revision += 1
        third = Matcher(session)
        self.assertNotIn(records[0].id, third.vectors)
        self.assertNotIn(records[1].id, third.vectors)

    def test_top_k_matches_full_ranking(self):
        session = Session("root", library(), kit_size=16)
        matcher = Matcher(session)
        full = matcher.rank(8, browse=True)
        limited = matcher.rank(8, browse=True, limit=7)
        self.assertEqual([s.id for _, s, _ in limited], [s.id for _, s, _ in full[:7]])
        self.assertEqual(matcher.rank(8, limit=0), [])

    def test_sixteen_pad_build_is_repeatable_and_keeps_core_when_locked(self):
        first = Session("root", library(), kit_size=16, target="sp404")
        second = Session.from_dict(first.to_dict())
        Matcher(first).build(0)
        Matcher(second).build(0)
        self.assertEqual([p.sample_id for p in first.pads], [p.sample_id for p in second.pads])
        for slot in range(8):
            self.assertIn(first.by_id[first.pads[slot].sample_id].kind, ROLE_POOLS[slot])
        for slot in range(8, 16):
            self.assertIn(first.by_id[first.pads[slot].sample_id].kind, ("foley", "perc", "unknown"))
        identities = [audio_identity(first.by_id[p.sample_id]) for p in first.pads[:16]]
        self.assertEqual(len(set(identities)), 16)
        core = [p.sample_id for p in first.pads[:8]]
        for pad in first.pads[:8]:
            pad.locked = True
        first.pads[3] = Assignment(None, "Intentional empty", locked=True)
        first.seed += 1
        Matcher(first).build(0)
        self.assertEqual([p.sample_id for p in first.pads[:3]], core[:3])
        self.assertIsNone(first.pads[3].sample_id)
        self.assertEqual([p.sample_id for p in first.pads[4:8]], core[4:8])
        self.assertEqual(kit_count(first), 4)

    def test_decoded_audio_duplicates_are_avoided_despite_different_file_hashes(self):
        a, b, c = [sample_record(name, "kick", value=i) for i, name in enumerate(("a", "b", "c"))]
        b.features["audio_hash"] = a.features["audio_hash"]
        session = Session("root", [a, b, c])
        session.pads[4] = Assignment(a.id, locked=True)
        Matcher(session).build(0)
        self.assertEqual(session.pads[0].sample_id, c.id)

    def test_sixteen_pad_pgm_chokes_only_core_hats(self):
        session = Session("root", library(), kit_size=16)
        Matcher(session).build()
        data = program_bytes(session, sample_names(session))
        for i in range(64):
            group = data[PAD_BASE + i * PAD_SIZE + 0x63]
            self.assertEqual(group, i // 16 + 1 if i % 16 in (6, 7) else 0)

    def test_preview_can_compare_drums_alone_and_sparse_upper_pads(self):
        sample = sample_record("foley-only")
        session = Session("root", [sample], kit_size=16)
        for i in range(24, 32):
            session.pads[i] = Assignment(sample.id)
        loader = lambda _: np.ones((2205, 2), dtype=np.float32) * .2
        dry = render_groove(session, 1, loader, drums_only=True)
        full = render_groove(session, 1, loader)
        self.assertEqual(float(np.max(np.abs(dry))), 0)
        self.assertGreater(float(np.max(np.abs(full))), 0)
        self.assertTrue(np.isfinite(full).all())
        self.assertLessEqual(float(np.max(np.abs(full))), .85)

    def test_sp_export_has_actual_pad_numbers_minimum_duration_and_recipe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "tiny found hit.wav"
            sf.write(path, np.sin(np.arange(441) * .12) * .4, 44100, subtype="PCM_24")
            record = sample_record("short")
            record.path = str(path)
            record.size, record.mtime_ns = path.stat().st_size, path.stat().st_mtime_ns
            record.fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
            before = path.read_bytes()
            session = Session(str(root), [record], kit_size=16, target="sp404")
            session.pads[:16] = [Assignment(record.id) for _ in range(16)]
            folder, validation = export_kit(session, 0, root / "exports")
            self.assertEqual(validation["padded_short_samples"], 16)
            self.assertTrue((folder / "COMPLETE.json").is_file())
            self.assertFalse((folder / "INCOMPLETE.txt").exists())
            wavs = list(folder.glob("*.wav"))
            self.assertEqual({int(p.name.split("_", 1)[0]) for p in wavs}, set(range(1, 17)))
            for wav in wavs:
                info = sf.info(wav)
                self.assertEqual((info.samplerate, info.subtype, info.channels), (48000, "PCM_16", 1))
                self.assertGreaterEqual(info.frames, 4800)
            recipe = json.loads((folder / "KIT_RECIPE.json").read_text(encoding="utf-8"))
            self.assertEqual(recipe["pads"][0]["pad"], 13)
            self.assertEqual(recipe["pads"][12]["pad"], 1)
            self.assertEqual(path.read_bytes(), before)

    def test_changed_source_never_publishes_a_complete_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "changed.wav"
            sf.write(path, np.ones(1000) * .1, 44100)
            record = sample_record("changed")
            record.path = str(path)
            record.fingerprint = "wrong-original-fingerprint"
            session = Session(str(root), [record], kit_size=16, target="sp404")
            session.pads[0] = Assignment(record.id)
            with self.assertRaisesRegex(ValueError, "changed since analysis"):
                export_kit(session, 0, root / "exports")
            self.assertFalse((root / "exports" / "kit001").exists())
            self.assertFalse(list((root / "exports").rglob("COMPLETE.json")))


if __name__ == "__main__":
    unittest.main()
