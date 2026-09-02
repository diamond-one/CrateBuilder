from pathlib import Path

import numpy as np

from .audio import load_audio


class Player:
    def __init__(self):
        self._cache = {}

    def data(self, sample):
        stat = Path(sample.path).stat()
        if stat.st_size != sample.size or stat.st_mtime_ns != sample.mtime_ns:
            raise ValueError("Source file changed. Scan the folder again before auditioning.")
        if sample.id not in self._cache:
            data = load_audio(sample)
            if data.shape[1] == 1:
                data = np.repeat(data, 2, axis=1)
            if len(self._cache) > 100:
                self._cache.clear()
            self._cache[sample.id] = data
        return self._cache[sample.id]

    def play(self, sample):
        import sounddevice as sd
        sd.play(self.data(sample) * .65, 44100)

    def stop(self):
        import sounddevice as sd
        sd.stop()

    def groove(self, session, kit, loop=True):
        import sounddevice as sd
        sd.play(render_groove(session, kit, self.data), 44100, loop=loop)


def render_groove(session, kit, loader=None):
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
    hats = sorted(t for t, slot, _ in hits if slot in (6, 7))
    for tick, slot, gain in hits:
        sample_id = session.pads[kit * 8 + slot].sample_id
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
