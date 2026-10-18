"""Polyphonic preview tests use memory-only samples and mocked device discovery."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cratebuilder.model import Assignment, Sample, Session
from cratebuilder.preview import (LiveSampler, Player, list_audio_outputs,
                                   preferred_audio_output)


def sample(name, kind="perc", available=True):
    return Sample(id=name, path=f"C:/synthetic-preview/{name}.wav", relative=f"{name}.wav",
                  kind=kind, confidence=1, classification="Synthetic test sample", duration=.1,
                  rate=48000, channels=1, fingerprint=name, size=100, mtime_ns=1,
                  features={"available": available})


def session_with_pads(assignments, kit_size=16):
    """assignments maps global logical pad index to Sample."""
    samples = list({item.id: item for item in assignments.values()}.values())
    session = Session(root="C:/synthetic-preview", samples=samples, kit_size=kit_size)
    for index, item in assignments.items():
        session.pads[index] = Assignment(item.id)
    return session


class LiveAudioTests(unittest.TestCase):
    def prepare(self, assignments, arrays=None, kit_size=16, bank=0):
        session = session_with_pads(assignments, kit_size=kit_size)
        arrays = arrays or {item.id: np.full((2048, 1), .1, dtype=np.float32)
                            for item in assignments.values()}
        engine = LiveSampler(samplerate=48000)
        loader = Mock(side_effect=lambda item, rate: arrays[item.id])
        with patch("cratebuilder.preview.load_audio", loader):
            result = engine.prepare(session, bank=bank)
        return engine, session, loader, result

    def test_prepare_loads_only_selected_bank_and_mix_callback_never_reads_audio(self):
        selected, other = sample("selected"), sample("other")
        engine, _, loader, result = self.prepare({0: other, 16: selected}, bank=1)
        self.assertEqual(result, {"loaded": 1, "notes": [], "bank": 1, "samplerate": 48000})
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(loader.call_args.args[0].id, "selected")
        self.assertEqual(loader.call_args.kwargs["rate"], 48000)
        self.assertEqual(engine.loaded_bank, 1)
        with patch("cratebuilder.preview.load_audio", side_effect=AssertionError("Audio callback performed file I/O")):
            self.assertTrue(engine.trigger(0, 127))
            first = engine._render(32)
            self.assertTrue(engine.trigger(0, 64))
            output = np.empty((32, 2), dtype=np.float32)
            engine._callback(output, 32, None, None)
        self.assertTrue(np.any(first))
        self.assertTrue(np.any(output))
        self.assertEqual(output.shape, (32, 2))
        np.testing.assert_allclose(output[:, 0], output[:, 1])

    def test_simultaneous_hits_overlap_and_velocity_controls_each_voice(self):
        first, second = sample("first"), sample("second")
        engine, _, _, _ = self.prepare({0: first, 1: second})
        engine.trigger(0, 127)
        first_block = engine._render(32)
        np.testing.assert_allclose(first_block, .1 * .65, atol=1e-7)
        engine.trigger(1, 64)
        overlap = engine._render(32)
        np.testing.assert_allclose(overlap, .1 * .65 * (1 + 64 / 127), atol=1e-7)
        # Retriggering a non-hat is polyphonic too.
        engine.trigger(0, 127)
        np.testing.assert_allclose(engine._render(32), .1 * .65 * (2 + 64 / 127), atol=1e-7)

    def test_sample_ends_inside_block_without_replaying_or_leaving_old_output(self):
        hit = sample("short")
        data = np.full((12, 2), .2, dtype=np.float32)
        engine, _, _, _ = self.prepare({0: hit}, arrays={hit.id: data})
        engine.trigger(0)
        output = engine._render(32)
        np.testing.assert_allclose(output[:12], .13, atol=1e-7)
        np.testing.assert_array_equal(output[12:], 0)
        np.testing.assert_array_equal(engine._render(32), 0)

    def test_open_hat_is_choked_by_closed_hat_with_a_sixty_four_sample_fade(self):
        closed, opened = sample("closed", "hat"), sample("open", "open_hat")
        engine, _, _, _ = self.prepare({6: closed, 7: opened})
        engine.trigger(7)
        engine._render(32)
        engine.trigger(6)
        fade_first = engine._render(16)
        expected = .065 + .065 * np.arange(64, 48, -1) / 64
        np.testing.assert_allclose(fade_first[:, 0], expected, atol=1e-7)
        fade_rest = engine._render(48)
        expected = .065 + .065 * np.arange(48, 0, -1) / 64
        np.testing.assert_allclose(fade_rest[:, 0], expected, atol=1e-7)
        np.testing.assert_allclose(engine._render(32), .065, atol=1e-7)

    def test_eight_pad_kits_have_independent_hat_choke_groups(self):
        first_open = sample("first-open", "open_hat")
        second_closed = sample("second-closed", "hat")
        engine, _, _, _ = self.prepare({7: first_open, 14: second_closed}, kit_size=8)
        engine.trigger(7)
        engine._render(32)
        engine.trigger(14)
        # These pads belong to different eight-pad kits within this bank.
        np.testing.assert_allclose(engine._render(128), .13, atol=1e-7)

    def test_sixteen_pad_creative_slots_do_not_choke_drum_hats(self):
        opened, creative = sample("open", "open_hat"), sample("creative")
        engine, _, _, _ = self.prepare({7: opened, 14: creative}, kit_size=16)
        engine.trigger(7)
        engine._render(16)
        engine.trigger(14)
        np.testing.assert_allclose(engine._render(128), .13, atol=1e-7)

    def test_voice_limit_keeps_newest_thirty_two_hits(self):
        hit = sample("many-hits", "kick")
        engine, _, _, _ = self.prepare({4: hit}, arrays={hit.id: np.full((256, 2), .01, dtype=np.float32)})
        for velocity in range(1, 41):
            engine.trigger(4, velocity)
        output = engine._render(32)
        expected = .01 * .65 * sum(range(9, 41)) / 127
        np.testing.assert_allclose(output, expected, atol=1e-7)

    def test_switching_bank_discards_old_voices_and_queued_old_hits_atomically(self):
        first, second = sample("bank-one"), sample("bank-two")
        arrays = {first.id: np.full((1024, 2), .1, dtype=np.float32),
                  second.id: np.full((1024, 2), .2, dtype=np.float32)}
        engine, session, _, _ = self.prepare({0: first, 16: second}, arrays=arrays)
        engine.trigger(0)
        np.testing.assert_allclose(engine._render(16), .065, atol=1e-7)
        engine.trigger(0)  # Pending old-bank hit must also be discarded.
        with patch("cratebuilder.preview.load_audio", side_effect=lambda item, rate: arrays[item.id]):
            engine.prepare(session, bank=1)
        engine.trigger(0)
        np.testing.assert_allclose(engine._render(16), .13, atol=1e-7)
        self.assertEqual(engine.loaded_bank, 1)
        engine.silence()
        np.testing.assert_array_equal(engine._render(16), 0)
        engine.trigger(0)
        np.testing.assert_allclose(engine._render(16), .13, atol=1e-7)

    def test_same_bank_becoming_offline_clears_preloaded_audio(self):
        first = sample("first")
        engine, session, _, _ = self.prepare({0: first})
        session.samples[0].features["available"] = False
        with self.assertRaises(ValueError):
            engine.prepare(session, bank=0)
        self.assertEqual(engine.loaded_bank, 0)
        self.assertFalse(engine.trigger(0))
        np.testing.assert_array_equal(engine._render(16), 0)
        with self.assertRaises(ValueError):
            engine.prepare(session, bank=4)

    def test_missing_excluded_offline_and_unreadable_pads_are_not_loaded(self):
        good, excluded = sample("good"), sample("excluded", "excluded")
        offline, broken = sample("offline", available=False), sample("broken")
        session = session_with_pads({0: good, 1: excluded, 2: offline, 3: broken})
        engine = LiveSampler(samplerate=48000)

        def load(item, rate):
            if item.id == "broken":
                raise OSError("Disconnected source")
            return np.full((128, 1), .1, dtype=np.float32)

        with patch("cratebuilder.preview.load_audio", side_effect=load) as loader:
            result = engine.prepare(session)
        self.assertEqual(loader.call_count, 2)
        self.assertEqual(result["loaded"], 1)
        self.assertEqual(len(result["notes"]), 2)
        self.assertTrue(engine.trigger(0))
        for index in (1, 2, 3, 4):
            self.assertFalse(engine.trigger(index))

    def test_mixed_output_is_finite_float_stereo_and_bounded_under_extreme_overlap(self):
        hit = sample("loud")
        data = np.column_stack([np.ones(512), -np.ones(512)]).astype(np.float32)
        engine, _, _, _ = self.prepare({0: hit}, arrays={hit.id: data})
        for _ in range(40):
            engine.trigger(0, 127)
        output = engine._render(64)
        self.assertEqual(output.dtype, np.float32)
        self.assertEqual(output.shape, (64, 2))
        self.assertTrue(np.isfinite(output).all())
        self.assertLessEqual(float(np.abs(output).max()), .950001)
        self.assertTrue(np.all(output[:, 0] > 0))
        self.assertTrue(np.all(output[:, 1] < 0))

    def test_zero_velocity_negative_velocity_and_invalid_pad_do_not_trigger(self):
        hit = sample("valid")
        engine, _, _, _ = self.prepare({0: hit})
        for index, velocity in ((0, 0), (0, -1), (-1, 127), (16, 127), (1, 127)):
            self.assertFalse(engine.trigger(index, velocity))
        np.testing.assert_array_equal(engine._render(32), 0)

    def test_callback_reports_status_once_and_zeros_output_if_mix_fails(self):
        errors = []
        engine = LiveSampler(samplerate=48000, on_error=errors.append)
        output = np.ones((32, 2), dtype=np.float32)
        engine._callback(output, 32, None, "underflow")
        engine._callback(output, 32, None, "underflow")
        self.assertEqual(errors, ["Audio preview: underflow"])
        with patch.object(engine, "_render", side_effect=RuntimeError("Mixer unavailable")):
            engine._callback(output, 32, None, None)
        np.testing.assert_array_equal(output, 0)
        self.assertIn("Mixer unavailable", errors[-1])


class LiveAudioDeviceTests(unittest.TestCase):
    def test_device_inventory_and_preference_choose_sp_wasapi_and_ignore_inputs(self):
        sounddevice = SimpleNamespace(
            query_hostapis=Mock(return_value=[{"name": "MME"}, {"name": "Windows WASAPI"}]),
            query_devices=Mock(return_value=[
                {"name": "Microphone", "max_output_channels": 0, "hostapi": 0, "default_samplerate": 44100},
                {"name": "SP-404MKII", "max_output_channels": 2, "hostapi": 0, "default_samplerate": 48000},
                {"name": "Laptop speakers", "max_output_channels": 2, "hostapi": 1, "default_samplerate": 48000},
                {"name": "SP-404MKII", "max_output_channels": 2, "hostapi": 1, "default_samplerate": 48000},
                {"name": "Mono output", "max_output_channels": 1, "hostapi": 0, "default_samplerate": 44100},
            ]))
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            outputs = list_audio_outputs()
            self.assertEqual([item["index"] for item in outputs], [1, 2, 3])
            self.assertEqual(preferred_audio_output(outputs), 3)
            self.assertEqual(preferred_audio_output(), 3)
        self.assertEqual(preferred_audio_output(outputs[:1]), 1)
        self.assertIsNone(preferred_audio_output([outputs[1]]))
        self.assertIsNone(preferred_audio_output([]))

    def test_sampler_uses_selected_device_native_rate_without_opening_hardware(self):
        sounddevice = SimpleNamespace(query_devices=Mock(return_value={"default_samplerate": 48000}))
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            engine = LiveSampler(device=20)
        sounddevice.query_devices.assert_called_once_with(20, "output")
        self.assertEqual(engine.sample_rate, 48000)
        self.assertFalse(engine.running)

    def test_regular_audition_resamples_44100_to_device_48000_preserving_channels_and_pitch(self):
        time = np.arange(44100) / 44100
        left = (.25 * np.sin(2 * np.pi * 1000 * time)).astype(np.float32)
        data = np.column_stack([left, left * .5])
        sounddevice = SimpleNamespace(query_devices=Mock(return_value={"default_samplerate": 48000}))
        player = Player()
        player.device = 20
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            converted, rate = player._output_data(data)
        self.assertEqual(rate, 48000)
        self.assertEqual(converted.shape, (48000, 2))
        self.assertEqual(converted.dtype, np.float32)
        self.assertTrue(np.isfinite(converted).all())
        np.testing.assert_allclose(converted[:, 1], converted[:, 0] * .5, atol=1e-7)
        peak_frequency = np.argmax(np.abs(np.fft.rfft(converted[:, 0])))
        self.assertEqual(peak_frequency, 1000)

    def test_default_and_44100_devices_keep_existing_audio_unchanged(self):
        data = np.ones((64, 2), dtype=np.float32)
        player = Player()
        original, rate = player._output_data(data)
        self.assertIs(original, data)
        self.assertEqual(rate, 44100)
        player.device = 7
        sounddevice = SimpleNamespace(query_devices=Mock(return_value={"default_samplerate": 44100}))
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            original, rate = player._output_data(data)
        self.assertIs(original, data)
        self.assertEqual(rate, 44100)


class FixedEndpointTests(unittest.TestCase):
    def test_four_channel_sp_endpoint_keeps_extra_pair_silent(self):
        engine = LiveSampler(device=20, samplerate=48000)
        hit = sample("four-channel")
        session = session_with_pads({0: hit})
        with patch("cratebuilder.preview.load_audio", return_value=np.ones((512, 2), dtype=np.float32) * .1):
            engine.prepare(session)
        stream = SimpleNamespace(active=False)
        stream.start = lambda: setattr(stream, "active", True)
        stream.stop = lambda: setattr(stream, "active", False)
        stream.close = Mock()
        backend = SimpleNamespace(PortAudioError=ValueError,
            query_devices=Mock(return_value={"max_output_channels": 4}),
            check_output_settings=Mock(side_effect=[ValueError("fixed endpoint"), None]),
            OutputStream=Mock(return_value=stream))
        with patch.dict(sys.modules, {"sounddevice": backend}):
            engine.start()
            self.assertTrue(engine.running)
            self.assertEqual(backend.OutputStream.call_args.kwargs["channels"], 4)
            engine.trigger(0)
            output = np.ones((128, 4), dtype=np.float32)
            engine._callback(output, 128, None, None)
            self.assertTrue(np.any(output[:, :2]))
            np.testing.assert_array_equal(output[:, 2:], 0)
            engine.close()

    def test_unavailable_new_bank_cannot_keep_playing_previous_bank(self):
        engine = LiveSampler(samplerate=48000)
        hit = sample("old-bank")
        with patch("cratebuilder.preview.load_audio", return_value=np.ones((512, 2), dtype=np.float32) * .1):
            engine.prepare(session_with_pads({0: hit}))
        engine.trigger(0)
        self.assertTrue(np.any(engine._render(32)))
        with self.assertRaises(ValueError):
            engine.prepare(Session("empty", []), bank=1)
        self.assertEqual(engine.loaded_bank, 1)
        self.assertFalse(engine.trigger(0))
        np.testing.assert_array_equal(engine._render(32), 0)


if __name__ == "__main__":
    unittest.main()
