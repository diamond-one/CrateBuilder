import hashlib
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cratebuilder.audio import audio_features, classify_name, scan_folder
from cratebuilder.converter import convert_folder
from cratebuilder.matching import Matcher, overview
from cratebuilder.model import Assignment, Session
from cratebuilder.pgm import export_program, program_bytes, sample_names, validate_program
from cratebuilder.preview import render_groove
from reference_mpc1k import Program


class KitBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.pack = cls.root / "test pack"
        rng = np.random.default_rng(1234)
        categories = {"KICKS": 16, "SNARES": 8, "CLAPS": 8, "HI-HATS": 8,
                      "OPEN HATS": 8, "CYMBALS": 8, "FILLS": 8}
        for folder, count in categories.items():
            target = cls.pack / folder
            target.mkdir(parents=True)
            for i in range(count):
                rate = 48000 if i % 2 else 44100
                t = np.arange(int(rate * (.18 + i * .009))) / rate
                tone = np.sin(2 * np.pi * (65 + i * 11) * t)
                data = .5 * (tone if folder == "KICKS" else rng.normal(0, .3, len(t))) * np.exp(-t * (14 + i))
                if i % 2:
                    data = np.column_stack([data, data * .85])
                sf.write(target / f"Tone é {i:02d} with a long name.wav", data, rate, subtype="PCM_24")
        cls.base = scan_folder(cls.pack, cls.root / "cache")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def fresh(self):
        return Session.from_dict(self.base.to_dict())

    def test_name_rules_do_not_mislabel_snare_or_loop(self):
        cases = {"SNARES/HAT SLAP.wav": "snare", "SNARES/SNARE ROLL.wav": "fill",
                 "DRUM BREAKS/91 BPM SOME.wav": "skip", "HAT LOOPS/HAT LOOP.wav": "skip",
                 "FILLS/90 BPM CHEAPS.wav": "fill", "HI-HATS/OPEN HAT CRISP.wav": "open_hat",
                 "KICKS/KICK WITH CYMBAL.wav": "kick", "OPEN HATS/001.wav": "open_hat",
                 "SNARES/RIM SHOT.wav": "rim", "CYMBALS/001.wav": "cymbal"}
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(classify_name(Path(path))[0], expected)

    def test_scan_cache_and_channel_formats(self):
        again = scan_folder(self.pack, self.root / "cache")
        self.assertEqual(self.base.to_dict(), again.to_dict())
        self.assertEqual(len(again.samples), 64)
        self.assertEqual({s.channels for s in again.samples}, {1, 2})
        self.assertEqual({s.rate for s in again.samples}, {44100, 48000})

    def test_full_layout_and_no_repeats_when_supplied(self):
        s = Matcher(self.fresh()).build()
        self.assertEqual(overview(s), (64, 64, 0))
        for i, pad in enumerate(s.pads):
            kind = s.by_id[pad.sample_id].kind
            expected = ("kick", "fill", "clap", "cymbal", "kick", "snare", "hat", "open_hat")[i % 8]
            self.assertEqual(kind, expected)
            self.assertFalse(pad.warning)

    def test_reproducible_and_locked_pads_survive(self):
        first, second = Matcher(self.fresh()).build(), Matcher(self.fresh()).build()
        self.assertEqual([p.sample_id for p in first.pads], [p.sample_id for p in second.pads])
        chosen = first.pads[4].sample_id
        first.pads[4].locked = True
        first.pads[3] = Assignment(None, "Intentional empty", locked=True)
        first.seed += 17
        Matcher(first).build()
        self.assertEqual(first.pads[4].sample_id, chosen)
        self.assertIsNone(first.pads[3].sample_id)
        self.assertTrue(any(a.sample_id != b.sample_id for a, b in zip(first.pads, second.pads)))

    def test_absent_cymbals_are_reported_or_left_empty(self):
        s = self.fresh()
        s.samples = [a for a in s.samples if a.kind != "cymbal"]
        Matcher(s).build()
        self.assertEqual(overview(s)[2], 8)
        self.assertTrue(all("Substitution" in s.pads[i].warning for i in range(3, 64, 8)))
        s.allow_substitutes = False
        Matcher(s).build()
        self.assertEqual(overview(s)[0], 56)
        self.assertTrue(all(s.pads[i].sample_id is None for i in range(3, 64, 8)))

    def test_export_parsed_by_independent_original_implementation(self):
        s = Matcher(self.fresh()).build()
        before = {a.path: hashlib.sha256(Path(a.path).read_bytes()).hexdigest() for a in s.samples}
        folder, validation = export_program(s, self.root / "exports", "a long program name é")
        program_file = next(folder.glob("*.PGM"))
        program = Program(program_file.read_bytes())
        self.assertEqual(program.file_size, 10756)
        self.assertEqual(validation["assigned_pads"], 64)
        self.assertEqual(validation["unique_wavs"], 64)
        names = sample_names(s)
        for i, pad in enumerate(program.pads):
            sample = s.by_id[s.pads[i].sample_id]
            name = pad.samples[0].sample_name.rstrip(b"\0").decode("ascii")
            self.assertEqual(name, names[sample.id])
            self.assertEqual(pad.samples[0].play_mode, 0)
            self.assertEqual(pad.mute_group, i // 8 + 1 if i % 8 in (6, 7) else 0)
            for layer in pad.samples[1:]:
                self.assertEqual(layer.sample_name.rstrip(b"\0"), b"")
            with wave.open(str(folder / (name + ".WAV"))) as wav:
                self.assertEqual(wav.getframerate(), 44100)
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getnchannels(), sample.channels)
                self.assertAlmostEqual(wav.getnframes() / 44100, sample.duration, places=4)
        after = {a.path: hashlib.sha256(Path(a.path).read_bytes()).hexdigest() for a in s.samples}
        self.assertEqual(before, after)
        self.assertEqual(program.data, program_file.read_bytes())
        self.assertEqual(validate_program(program_file), validation)
        # A missing referenced file must never report a valid program.
        next(folder.glob("*.WAV")).unlink()
        with self.assertRaises(Exception):
            validate_program(program_file)

    def test_export_does_not_overwrite_a_previous_program(self):
        s = Matcher(self.fresh()).build()
        first, _ = export_program(s, self.root / "repeat", "TEST")
        second, _ = export_program(s, self.root / "repeat", "TEST")
        self.assertNotEqual(first, second)
        self.assertTrue((first / "TEST.PGM").is_file())

    def test_bad_and_silent_audio_reported_without_crashing_scan(self):
        folder = self.root / "bad" / "KICKS"
        folder.mkdir(parents=True, exist_ok=True)
        sf.write(folder / "silent.wav", np.zeros(1000), 44100)
        (folder / "broken.wav").write_bytes(b"not a wav")
        sf.write(folder / "good.wav", np.sin(np.arange(5000) * .05) * .5, 44100)
        result = scan_folder(folder, self.root / "badcache")
        self.assertEqual(len(result.samples), 1)
        self.assertEqual(result.samples[0].kind, "kick")
        self.assertEqual(len(result.scan_notes), 3)

    def test_groove_preview_is_finite_and_bounded(self):
        s = Matcher(self.fresh()).build()
        data = render_groove(s, 0)
        self.assertEqual(data.shape[1], 2)
        self.assertAlmostEqual(len(data) / 44100, 8 * 60 / 90, places=4)
        self.assertTrue(np.isfinite(data).all())
        self.assertLessEqual(float(np.max(np.abs(data))), .851)
        self.assertGreater(float(np.max(np.abs(data))), .01)

    def test_complete_folder_conversion_is_mpc_compatible_and_non_destructive(self):
        source = self.root / "conversion source"
        nested = source / "Odd folder name é"
        nested.mkdir(parents=True)
        mono = np.sin(np.arange(4800) * .05).astype(np.float32) * .4
        stereo = np.column_stack((mono[:2400], mono[:2400] * .8))
        wav = source / "A very long kick name é.wav"
        aiff = nested / "A very long kick name é.aiff"
        sf.write(wav, mono, 48000, subtype="PCM_24")
        sf.write(aiff, stereo, 24000, subtype="PCM_24")
        broken = nested / "broken.flac"
        broken.write_bytes(b"not audio")
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (wav, aiff, broken)}

        output, report = convert_folder(source, self.root / "converted")

        self.assertEqual(report["found"], 3)
        self.assertEqual(report["converted"], 2)
        self.assertEqual(report["failed"], 1)
        converted = sorted(output.rglob("*.WAV"))
        self.assertEqual(len(converted), 2)
        self.assertTrue(all(len(path.stem) <= 16 and path.stem.isascii() for path in converted))
        for path in converted:
            info = sf.info(path)
            self.assertEqual(info.samplerate, 44100)
            self.assertEqual(info.subtype, "PCM_16")
            self.assertIn(info.channels, (1, 2))
        self.assertTrue((output / "FILE_MAP.csv").is_file())
        self.assertIn("broken.flac", (output / "CONVERSION_REPORT.txt").read_text(encoding="utf-8"))
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (wav, aiff, broken)}
        self.assertEqual(before, after)
        nested_output, nested_report = convert_folder(source, source)
        self.assertEqual(nested_report["found"], 3)
        second_output, second_report = convert_folder(source, source)
        self.assertEqual(second_report["found"], 3)
        self.assertNotEqual(nested_output, second_output)


if __name__ == "__main__":
    unittest.main()
