"""Local audio analysis. No samples or features are sent to a service."""
import hashlib
import json
import math
import re
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .model import Sample, Session

EXTENSIONS = {".wav", ".wave", ".aif", ".aiff", ".flac"}
ANALYSIS_VERSION = 3
MAX_SECONDS = 15


def words(text):
    return re.findall(r"[a-z]+|[0-9]+", text.lower())


def classify_name(path: Path):
    title = set(words(path.stem))
    folders = set(words(" ".join(path.parts[:-1])))
    # Folder categories are strong evidence: 'HAT SLAP' in SNARES is a snare.
    if folders & {"loops", "loop", "breaks", "bass", "fx", "musical", "horns", "wah"}:
        return "skip", 1.0, "Non-drum or loop folder"
    if title & {"fill", "fills", "roll", "rolls", "flam", "flams"} or folders & {"fills", "rolls"}:
        return "fill", .98, "Roll/fill name or folder"
    if title & {"loop", "loops", "break", "breaks"}:
        return "skip", .95, "Loop name"
    if folders & {"kicks", "kick"}:
        return "kick", .98, "Kick folder"
    if folders & {"snares", "snare"}:
        if title & {"rim", "rimshot", "stick", "sidestick"}:
            return "rim", .96, "Rim/stick name in snare folder"
        return "snare", .97, "Snare folder"
    if folders & {"claps", "clap", "snaps"}:
        return "clap", .96, "Clap/snap folder"
    if title & {"crash", "ride", "cymbal", "cymbals", "splash", "china"} or folders & {"cymbals", "rides", "crashes"}:
        return "cymbal", .96, "Cymbal name or folder"
    if "oh" in title or "openhat" in title or ("open" in title | folders and (title | folders) & {"hat", "hats", "hihat", "hihats", "hh"}):
        return "open_hat", .97, "Open-hat name or folder"
    if folders & {"hats", "hat", "hihat", "hihats"}:
        return "hat", .94, "Hi-hat folder (no open-hat label)"
    for kind, tokens in [("kick", {"kick", "kicks", "bd", "bassdrum"}),
                         ("rim", {"rim", "rimshot", "sidestick"}),
                         ("clap", {"clap", "claps", "snap", "snaps"}),
                         ("snare", {"snare", "snares", "sd"}),
                         ("hat", {"hat", "hats", "hh", "hihat", "hihats", "ch"})]:
        if title & tokens:
            return kind, .94, "Instrument name"
    if folders & {"perc", "percussion"} or title & {"conga", "shaker", "cowbell", "clave", "cabassa", "tamb", "tambourine"}:
        return "perc", .9, "Percussion name or folder"
    return "unknown", .0, "No instrument label"


def audio_features(data, rate):
    if data.size == 0 or not np.isfinite(data).all():
        raise ValueError("Empty or non-finite audio")
    # Choose the louder channel for analysis to avoid phase cancellation in stereo.
    channel = int(np.argmax(np.mean(data.astype(np.float64) ** 2, axis=0)))
    mono = data[:, channel]
    peak = float(np.max(np.abs(mono)))
    if peak < 1e-6:
        raise ValueError("Silent sample")
    above = np.flatnonzero(np.abs(mono) > peak * .003)
    start, end = int(above[0]), int(above[-1]) + 1
    active = mono[start:end]
    divisor = math.gcd(rate, 22050)
    y = resample_poly(active, 22050 // divisor, rate // divisor).astype(np.float64)
    rms = float(np.sqrt(np.mean(y * y)))
    energy = np.cumsum(y * y)
    decay = float(np.searchsorted(energy, energy[-1] * .95) / 22050)
    padded = np.pad(y, (0, max(0, 2048 - len(y))))
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2048)[::1024]
    spectrum = np.mean(np.abs(np.fft.rfft(windows * np.hanning(2048), axis=1)) ** 2, axis=0)
    freqs = np.fft.rfftfreq(2048, 1 / 22050)
    total = float(spectrum.sum()) + 1e-20
    centroid = float(np.dot(freqs, spectrum) / total)
    flatness = float(np.exp(np.mean(np.log(spectrum + 1e-20))) / (spectrum.mean() + 1e-20))
    waveform = [float(np.max(np.abs(chunk))) for chunk in np.array_split(mono, min(120, len(mono)))]
    return dict(centroid=centroid, decay=decay, crest=float(20 * np.log10(peak / max(rms, 1e-12))),
                flatness=flatness, low=float(spectrum[freqs < 200].sum() / total),
                high=float(spectrum[freqs > 4000].sum() / total),
                peak=peak, rms=rms, clipped=float(np.mean(np.abs(data) >= .9999)),
                active_duration=(end - start) / rate, leading=start / rate,
                waveform=waveform)


def infer_kind(f):
    if f["low"] > .58 and f["centroid"] < 1100:
        return "kick", .57, "Audio guess: strong low-frequency transient; review by ear"
    if f["centroid"] > 3800 and f["low"] < .035:
        if f["decay"] > .24:
            return "open_hat", .42, "Audio guess: sustained bright noise; could be cymbal or open hat"
        return "hat", .52, "Audio guess: short bright noise; review by ear"
    if f["centroid"] > 750 and f["flatness"] > .008 and f["decay"] < .9:
        return "snare", .43, "Audio guess: noisy midrange transient; review by ear"
    return "unknown", .15, "Uncertain sound type; excluded from automatic kits"


def family_words(path: Path):
    stops = set("kick kicks snare snares hat hats hi hihat open closed cymbal ride crash clap claps rim shot fill roll drum drums sample samples one shots oneshot bpm wav dry wet verb room roomy low high punch heavy thick quick short long alt mono stereo perc percussion loops breaks".split())
    return sorted({w for w in words(path.stem) if len(w) > 2 and w not in stops and not w.isdigit()})


def scan_folder(root, cache_dir, progress=lambda message, value: None, cancel=None):
    root = Path(str(root).strip().strip('"')).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Sample folder not found. Choose an existing folder with Browse.")
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / (hashlib.sha256(str(root).encode()).hexdigest()[:20] + ".json")
    cache = {}
    try:
        saved = json.loads(cache_path.read_text(encoding="utf-8"))
        if saved.get("version") == ANALYSIS_VERSION:
            cache = saved["samples"]
    except (OSError, ValueError, KeyError):
        pass
    progress("Finding audio files...", 0)
    files = sorted((p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSIONS
                    and not any(part.startswith(".") for part in p.relative_to(root).parts)), key=lambda p: str(p).lower())
    if not files:
        raise ValueError("No WAV, AIFF or FLAC files found in this folder or its subfolders.")
    samples, notes, updated, skipped = [], [], {}, 0
    for index, path in enumerate(files):
        if cancel and cancel.is_set():
            raise InterruptedError("Scan cancelled")
        relative = path.relative_to(root)
        kind, confidence, why = classify_name(relative)
        # If the selected root is itself 'KICKS', keep that category available.
        if kind == "unknown":
            kind, confidence, why = classify_name(Path(root.name) / relative)
        if kind == "skip":
            skipped += 1
            continue
        progress(f"Analysing {index + 1}/{len(files)}: {path.name}", (index + 1) / len(files))
        try:
            stat = path.stat()
            key = str(relative)
            old = cache.get(key)
            if old and old["size"] == stat.st_size and old["mtime_ns"] == stat.st_mtime_ns and old["path"] == str(path):
                sample = Sample(**old)
            else:
                info = sf.info(path)
                if info.duration > MAX_SECONDS or info.channels not in (1, 2):
                    notes.append(f"Skipped {relative}: longer than {MAX_SECONDS}s or more than two channels.")
                    continue
                data, rate = sf.read(path, dtype="float32", always_2d=True)
                features = audio_features(data, rate)
                if kind == "unknown":
                    kind, confidence, why = infer_kind(features)
                bpm_match = re.search(r"(\d{2,3})\s*BPM", path.stem, re.I)
                fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
                sample = Sample(hashlib.sha256(str(path).encode()).hexdigest()[:20], str(path), str(relative),
                                kind, confidence, why, info.duration, rate, info.channels, fingerprint,
                                stat.st_size, stat.st_mtime_ns, features, family_words(relative),
                                float(bpm_match.group(1)) if bpm_match else None)
            samples.append(sample)
            updated[key] = sample.to_dict()
        except (OSError, ValueError, RuntimeError) as exc:
            notes.append(f"Skipped {relative}: {exc}")
    cache_path.write_text(json.dumps({"version": ANALYSIS_VERSION, "samples": updated}), encoding="utf-8")
    notes.insert(0, f"Found {len(files)} audio files; analysed {len(samples)}; excluded {skipped} labelled loops/non-drums.")
    if not samples:
        raise ValueError("No usable drum samples found. Choose a folder containing drum one-shots.")
    progress("Analysis complete", 1)
    return Session(str(root), samples, scan_notes=notes)


def load_audio(sample, rate=44100):
    path = Path(sample.path)
    stat = path.stat()
    if stat.st_size != sample.size or stat.st_mtime_ns != sample.mtime_ns:
        raise ValueError(f"{path.name} changed since analysis. Scan the folder again.")
    data, source_rate = sf.read(path, dtype="float32", always_2d=True)
    if not np.isfinite(data).all():
        raise ValueError(f"Invalid audio in {path.name}")
    if source_rate != rate:
        divisor = math.gcd(source_rate, rate)
        data = resample_poly(data, rate // divisor, source_rate // divisor, axis=0)
    peak = float(np.max(np.abs(data)))
    # Only attenuate overs; do not normalize quiet samples or alter their character.
    if peak > .999:
        data = data * (.999 / peak)
    return data.astype(np.float32)
