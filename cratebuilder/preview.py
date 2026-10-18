from pathlib import Path
from collections import OrderedDict
from collections import deque
from dataclasses import dataclass
import threading

import numpy as np

from .audio import load_audio


class Player:
    def __init__(self):
        self._cache = OrderedDict()
        self.device = None

    def set_output_device(self, device=None):
        self.stop()
        self.device = device

    def data(self, sample):
        stat = Path(sample.path).stat()
        if stat.st_size != sample.size or stat.st_mtime_ns != sample.mtime_ns:
            raise ValueError("Source file changed. Scan the folder again before auditioning.")
        if sample.id not in self._cache:
            data = load_audio(sample)
            if data.shape[1] == 1:
                data = np.repeat(data, 2, axis=1)
            while self._cache and sum(v.nbytes for v in self._cache.values()) + data.nbytes > 64 * 1024 ** 2:
                self._cache.popitem(last=False)
            self._cache[sample.id] = data
        self._cache.move_to_end(sample.id)
        return self._cache[sample.id]

    def play(self, sample):
        import sounddevice as sd
        data, rate = self._output_data(self.data(sample) * .65)
        sd.play(data, rate, device=self.device)

    def _output_data(self, data):
        if self.device is None:
            return data, 44100
        import sounddevice as sd
        from scipy.signal import resample_poly
        from math import gcd
        info = sd.query_devices(self.device, "output")
        rate = int(info["default_samplerate"])
        if rate != 44100:
            divisor = gcd(rate, 44100)
            data = resample_poly(data, rate // divisor, 44100 // divisor, axis=0).astype(np.float32)
        # Some SP Windows endpoints expose a fixed four-channel shared format.
        if info.get("max_output_channels", 2) == 4 and data.shape[1] == 2:
            data = np.pad(data, ((0, 0), (0, 2)))
        return data, rate

    def stop(self):
        import sounddevice as sd
        sd.stop()

    def groove(self, session, kit, loop=True, drums_only=False):
        import sounddevice as sd
        data, rate = self._output_data(render_groove(session, kit, self.data, drums_only=drums_only))
        sd.play(data, rate, loop=loop, device=self.device)


def list_audio_outputs():
    import sounddevice as sd
    hosts = sd.query_hostapis()
    result = []
    for index, device in enumerate(sd.query_devices()):
        if device["max_output_channels"] >= 2:
            result.append({"index": index, "name": device["name"],
                           "hostapi_name": hosts[device["hostapi"]]["name"],
                           "samplerate": float(device["default_samplerate"])})
    return result


def _audio_host_priority(host):
    host = str(host).casefold()
    for priority, token in enumerate(("wasapi", "asio", "directsound", "mme", "wdm-ks")):
        if token in host:
            return priority
    return 5


def _generic_audio_alias(name):
    name = " ".join(str(name).casefold().replace("–", "-").replace("—", "-").split())
    return name in {
        "microsoft sound mapper", "microsoft sound mapper - output",
        "primary sound driver", "default", "default output", "default output device",
        "default audio device", "default device", "system default", "system default output",
        "system default audio device", "windows default", "windows default output",
    }


def simple_audio_outputs(outputs=None):
    """Return a short device list while preserving each endpoint's actual index.

    Matching names across Windows driver hosts are represented by WASAPI,
    ASIO, DirectSound, MME, then WDM-KS, in that order. Distinct names are never
    merged, and identically named devices within the preferred host remain
    separate: they may be two real devices with the same manufacturer name.
    Generic mapper/default aliases remain available if they are the only outputs.
    Long MME prefixes are hidden only when one better-driver endpoint matches.
    The SP appears first, followed by other outputs in alphabetical order.
    Pass an inventory to make this a pure operation with no device queries.
    """
    inventory = list(list_audio_outputs() if outputs is None else outputs)
    endpoints = [device for device in inventory if not _generic_audio_alias(device.get("name", ""))]
    inventory = endpoints or inventory
    groups = OrderedDict()
    for device in inventory:
        name = " ".join(str(device.get("name", "")).casefold().split())
        groups.setdefault(name, []).append(device)
    result = []
    for variants in groups.values():
        preferred = min(variants, key=lambda device: _audio_host_priority(device.get("hostapi_name", "")))
        host = str(preferred.get("hostapi_name", "")).casefold()
        seen_indices = set()
        for device in variants:
            if str(device.get("hostapi_name", "")).casefold() != host:
                continue
            index = device.get("index")
            if index in seen_indices:
                continue
            seen_indices.add(index)
            result.append(dict(device))
    compact = []
    for device in result:
        name = " ".join(str(device.get("name", "")).casefold().split())
        host = str(device.get("hostapi_name", ""))
        # WinMM's short device labels can end at 31 characters. Match only a
        # unique longer endpoint on a better driver; a shared prefix is not
        # enough to distinguish two physical outputs.
        if "mme" in host.casefold() and len(str(device.get("name", "")).strip()) >= 30:
            matches = [other for other in result
                       if _audio_host_priority(other.get("hostapi_name", "")) < _audio_host_priority(host)
                       and len(other_name := " ".join(str(other.get("name", "")).casefold().split())) > len(name)
                       and other_name.startswith(name)]
            if len(matches) == 1:
                continue
        compact.append(device)

    def display_order(device):
        name = " ".join(str(device.get("name", "")).casefold().split())
        sp = "sp404" in name.replace("-", "").replace(" ", "")
        return (not sp, name)

    return sorted(compact, key=display_order)


def audio_output_label(device, advanced=False):
    """Use the endpoint's familiar name, with driver information only on request."""
    name = str(device.get("name", "Audio output"))
    host = str(device.get("hostapi_name", ""))
    return f"{name} ({host})" if advanced and host else name


def preferred_audio_output(outputs=None):
    outputs = list_audio_outputs() if outputs is None else outputs
    sp = [device for device in outputs if "sp-404" in device["name"].lower()]
    if sp:
        return next((device for device in sp if "WASAPI" in device["hostapi_name"].upper()), sp[0])["index"]
    return None


@dataclass
class _Voice:
    pad: int
    data: np.ndarray
    position: int
    gain: float
    group: int | None
    release: int | None = None


class LiveSampler:
    """Preloaded, polyphonic pad player. MIDI callbacks never open audio files."""
    def __init__(self, device=None, samplerate=None, blocksize=256, on_error=lambda message: None):
        self.device = device
        if samplerate is None:
            import sounddevice as sd
            info = sd.query_devices(device, "output")
            samplerate = int(info["default_samplerate"])
        self.sample_rate = int(samplerate)
        self.blocksize = int(blocksize)
        self.on_error = on_error
        self.loaded_bank = None
        self._stream = None
        self._pads = {}
        self._groups = {}
        self._voices = []
        self._commands = deque(maxlen=256)
        self._lock = threading.Lock()
        self._last_status = ""
        self.max_voices = 32
        self.output_channels = 2

    @property
    def running(self):
        return self._stream is not None and self._stream.active

    def prepare(self, session, bank=0):
        if not 0 <= bank < len(session.pads) // 16:
            raise ValueError("Choose a valid preview bank.")
        pads, groups, notes = {}, {}, []
        samples = session.by_id
        for local in range(16):
            assignment = session.pads[bank * 16 + local]
            sample = samples.get(assignment.sample_id)
            if sample is None or sample.kind == "excluded":
                continue
            if not sample.features.get("available", True):
                notes.append(f"Pad {local + 1}: source unavailable")
                continue
            try:
                data = load_audio(sample, rate=self.sample_rate)
                if data.shape[1] == 1:
                    data = np.repeat(data, 2, axis=1)
                pads[local] = np.ascontiguousarray(data, dtype=np.float32)
            except (OSError, ValueError, RuntimeError) as exc:
                notes.append(str(exc))
                continue
            if local % session.kit_size in (6, 7):
                groups[local] = local // session.kit_size
        if not pads:
            with self._lock:
                self._pads, self._groups = {}, {}
                self.loaded_bank = bank
                self._commands.clear()
                self._commands.append(("reset", None, None))
            raise ValueError("This bank has no available samples to preview. Generate a kit first.")
        with self._lock:
            self._pads, self._groups = pads, groups
            self.loaded_bank = bank
            self._commands.clear()
            self._commands.append(("reset", None, None))
        return {"loaded": len(pads), "notes": notes, "bank": bank, "samplerate": self.sample_rate}

    def start(self):
        if self.running:
            return
        import sounddevice as sd
        try:
            sd.check_output_settings(device=self.device, channels=2, dtype="float32", samplerate=self.sample_rate)
        except sd.PortAudioError:
            channels = int(sd.query_devices(self.device, "output")["max_output_channels"])
            if channels != 4:
                raise
            sd.check_output_settings(device=self.device, channels=channels, dtype="float32", samplerate=self.sample_rate)
            self.output_channels = channels
        self._stream = sd.OutputStream(device=self.device, samplerate=self.sample_rate, channels=self.output_channels,
                                       dtype="float32", blocksize=self.blocksize, latency="low",
                                       callback=self._callback)
        try:
            self._stream.start()
        except Exception:
            self._stream.close()
            self._stream = None
            raise

    def trigger(self, local_index, velocity=127):
        if not 0 <= local_index < 16 or velocity <= 0:
            return False
        with self._lock:
            data = self._pads.get(local_index)
            if data is None:
                return False
            self._commands.append((local_index, data, min(127, velocity) / 127))
        return True

    def silence(self):
        with self._lock:
            self._commands.clear()
            self._commands.append(("reset", None, None))

    def _render(self, frames):
        """Pure block mixer, also exercised without an attached audio device."""
        with self._lock:
            commands = list(self._commands)
            self._commands.clear()
            groups = self._groups
        for pad, data, velocity in commands:
            if pad == "reset":
                self._voices.clear()
                continue
            group = groups.get(pad)
            if group is not None:
                for voice in self._voices:
                    if voice.group == group:
                        voice.release = min(voice.release or 64, 64)
            self._voices.append(_Voice(pad, data, 0, .65 * velocity, group))
            self._voices = self._voices[-self.max_voices:]
        output = np.zeros((frames, 2), dtype=np.float32)
        alive = []
        for voice in self._voices:
            length = min(frames, len(voice.data) - voice.position)
            if voice.release is not None:
                length = min(length, voice.release)
            if length <= 0:
                continue
            clip = voice.data[voice.position:voice.position + length] * voice.gain
            if voice.release is not None:
                clip = clip * (np.arange(voice.release, voice.release - length, -1, dtype=np.float32) / 64)[:, None]
                voice.release -= length
            output[:length] += clip
            voice.position += length
            if voice.position < len(voice.data) and voice.release != 0:
                alive.append(voice)
        self._voices = alive
        np.clip(output, -.95, .95, out=output)
        return output

    def _callback(self, outdata, frames, timing, status):
        try:
            outdata.fill(0)
            outdata[:, :2] = self._render(frames)
            if status and str(status) != self._last_status:
                self._last_status = str(status)
                self.on_error("Audio preview: " + str(status))
        except Exception as exc:
            outdata.fill(0)
            self.on_error(str(exc))

    def stop(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        with self._lock:
            self._commands.clear()
            self._voices.clear()

    close = stop


def render_groove(session, kit, loader=None, drums_only=False):
    """Two bars for comparing the main kit. Fills are auditioned separately."""
    if loader is None:
        def loader(s):
            data = load_audio(s)
            return np.repeat(data, 2, axis=1) if data.shape[1] == 1 else data
    bpm = max(40, min(200, session.bpm))
    step = 60 / bpm / 4
    frames = round(32 * step * 44100)
    output = np.zeros((frames, 2), dtype=np.float32)
    hits = []
    for tick in [0, 6, 10, 16, 22, 27]:
        hits.append((tick, 4, .78))
    for tick in [4, 12, 20, 28]:
        hits.append((tick, 5, .76))
    hits.extend([(14, 2, .35), (30, 0, .5)])
    for tick in range(0, 32, 2):
        hits.append((tick, 7 if tick in (14, 30) else 6, .38 if tick % 4 == 0 else .28))
    if session.kit_size == 16 and not drums_only:
        # Sparse accents leave space for hearing how the upper pads fit the backbone.
        hits.extend([(3, 9, .23), (7, 10, .20), (11, 8, .27), (15, 11, .18),
                     (19, 12, .24), (23, 13, .16), (26, 14, .20), (31, 15, .18)])
    hats = sorted(t for t, slot, _ in hits if slot in (6, 7))
    for tick, slot, gain in hits:
        sample_id = session.pads[kit * session.kit_size + slot].sample_id
        sample = session.by_id.get(sample_id)
        if sample is None:
            continue
        data = loader(sample)
        start = round((tick * step + (step * .12 if tick % 2 else 0)) * 44100)
        length = min(len(data), frames - start)
        if slot in (6, 7):
            next_hat = next((t for t in hats if t > tick), 32)
            length = min(length, round((next_hat - tick) * step * 44100))
        clip = data[:length].copy() * gain
        if length < len(data) and length > 220:
            clip[-220:] *= np.linspace(1, 0, 220)[:, None]
        output[start:start + length] += clip
    peak = float(np.max(np.abs(output)))
    output *= min(.7, .85 / max(peak, 1e-9))
    return output
