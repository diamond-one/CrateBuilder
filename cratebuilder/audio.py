"""Local audio analysis. No samples or features are sent to a service."""
import hashlib
import json
import math
import os
import re
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .model import Sample, Session
from .ni import classify_ni_path, metadata_for_path

EXTENSIONS = {".wav", ".wave", ".aif", ".aiff", ".flac"}
ANALYSIS_VERSION = 5
MAX_SECONDS = 15


def words(text):
    return re.findall(r"[a-z]+|[0-9]+", text.lower())


def classify_name(path: Path):
    ni_classification = classify_ni_path(path)
    if ni_classification is not None:
        return ni_classification
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
    # A small envelope gives useful physical hints, not guesses at the sound's source.
    hop = max(1, round(rate * .01))
    envelope = np.array([np.sqrt(np.mean(chunk.astype(np.float64) ** 2))
                         for chunk in np.array_split(active, max(1, math.ceil(len(active) / hop)))])
    env_peak = float(np.max(envelope))
    rise = np.flatnonzero(envelope >= env_peak * .8)
    attack_ms = float(rise[0] * 10) if len(rise) else 0.0
    separated = np.diff(np.r_[False, envelope > env_peak * .45, False].astype(np.int8))
    pulses = int(np.count_nonzero(separated == 1))
    return dict(centroid=centroid, decay=decay, crest=float(20 * np.log10(peak / max(rms, 1e-12))),
                flatness=flatness, low=float(spectrum[freqs < 200].sum() / total),
                high=float(spectrum[freqs > 4000].sum() / total),
                peak=peak, rms=rms, clipped=float(np.mean(np.abs(data) >= .9999)),
                active_duration=(end - start) / rate, leading=start / rate,
                waveform=waveform, attack_ms=attack_ms, pulses=pulses)


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


def shape_tags(features):
    """Conservative audio hints; these never claim to identify an object or material."""
    tags = []
    duration = features.get("active_duration", 0)
    decay = features.get("decay", 0)
    if duration < .3:
        tags.append("short")
    if duration < .14:
        tags.append("click")
    if decay > .5:
        tags.append("ringing" if features.get("flatness", 1) < .015 else "textural")
    if features.get("pulses", 0) >= 3:
        tags.append("rattle")
    if features.get("low", 0) > .4:
        tags.append("low-hit")
    if features.get("centroid", 0) > 3500:
        tags.append("bright")
    elif features.get("centroid", 0) < 1000:
        tags.append("dark")
    tags.append("noisy" if features.get("flatness", 0) > .04 else "tonal")
    return tags


def seed_user_rules(catalogue):
    """This folder's provenance was explicitly supplied by its owner."""
    folder = Path("C:/Users/thisi/Music/SAMPLES_ALL/Folly/kitchenSamples")
    if folder.is_dir():
        catalogue.ensure_folder_rule(folder, ["kitchen", "utensils", "percussion"], kind="foley")


def _analysis_tags(features):
    tags = shape_tags(features)
    if features.get("ni_source"):
        tags.append("ni")
        tags.extend(str(features[key]).casefold() for key in ("pack", "category", "subtype") if features.get(key))
    return sorted(set(tags))


def _decorate_ni(sample, info):
    """Preserve expansion identity even when a single category is scanned."""
    if not info or not str(info.get("source", "")).startswith("ni:"):
        return sample
    features = {**sample.features, "ni_source": info["source"], "provider": info.get("provider", "Native Instruments"),
                "pack": info.get("pack", ""), "category": info.get("category", ""),
                "subtype": info.get("subtype", ""), "key": info.get("key")}
    if info.get("category") in {"Loops", "Instruments"}:
        features.update(preview_only=True, manual_only=True)
    elif info.get("category") == "One Shots" and info.get("kind") in {"skip", "unknown"}:
        features["manual_only"] = True
    return replace(sample, source=info["source"], bpm=sample.bpm if sample.bpm is not None else info.get("bpm"),
                   features=features, tags=_analysis_tags(features))


def _apply_rule(sample, rule):
    # Always start with the analysed baseline, so deleting a folder rule undoes it.
    baseline = sample.base_kind or sample.kind
    confidence = sample.base_confidence if sample.base_confidence is not None else sample.confidence
    kind = rule.get("kind")
    if kind:
        effective = "excluded" if kind == "exclude" else kind
        return replace(sample, base_kind=baseline, base_confidence=confidence,
                       kind=effective, confidence=1.0,
                       classification="Folder rule: " + str(rule.get("folder", "")),
                       tags=sorted(set(_analysis_tags(sample.features) + rule.get("tags", []))))
    reason = ("Instrument label" if confidence >= .7 else "Audio hint; review by ear")
    return replace(sample, base_kind=baseline, base_confidence=confidence,
                   kind=baseline, confidence=confidence, classification=reason,
                   tags=sorted(set(_analysis_tags(sample.features) + rule.get("tags", []))))


def apply_folder_rules(session, catalogue):
    """Apply folder edits to the current catalogue without opening or renaming audio."""
    rules = catalogue.list_folder_rules()
    session.samples = [_apply_rule(sample, catalogue.rules_for(sample.path, rules=rules))
                       for sample in session.samples]
    session.library_revision += 1
    return session


def _source_name(root, relative):
    if len(relative.parts) < 2:
        return root.name
    categories = {"kicks", "snares", "claps", "hats", "hi-hats", "open hats", "cymbals", "fills", "perc", "percussion"}
    return root.name if relative.parts[0].lower() in categories else relative.parts[0]


def _audio_digest(data, rate):
    digest = hashlib.sha256(f"{rate}:{data.shape[1]}:".encode())
    digest.update(np.rint(np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())
    return digest.hexdigest()


def scan_folder(root, cache_dir, progress=lambda message, value: None, cancel=None,
                catalogue=None, include_online_only=False):
    """Incrementally analyse one-shots; each completed file is a durable checkpoint."""
    from .catalogue import Catalogue, canonical_path
    root = Path(str(root).strip().strip('"')).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Sample folder is unavailable. Reconnect it and update the library again; saved kits are retained.")
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    catalogue = catalogue or Catalogue(cache_dir / "catalogue.sqlite3")
    catalogue.record_scan_status(root, "available", root=root)
    rules = catalogue.list_folder_rules()
    cached = {canonical_path(sample.path)[1]: sample
              for sample in catalogue.cached_samples(root, ANALYSIS_VERSION)}
    statuses = {canonical_path(item["path"])[1]: item["status"]
                for item in catalogue.scan_statuses(root)}
    files, notes = [], []
    last_progress = 0.0

    def report(message, value, force=False):
        nonlocal last_progress
        now = time.monotonic()
        if force or now - last_progress > .08:
            progress(message, value)
            last_progress = now

    def check_cancel():
        if cancel and cancel.is_set():
            raise InterruptedError("Scan cancelled")

    def walk_error(exc):
        notes.append(f"Folder unavailable: {exc.filename}: {exc.strerror}")

    report("Finding audio files...", 0, True)
    for folder, directories, names in os.walk(root, onerror=walk_error, followlinks=False):
        check_cancel()
        directories[:] = [name for name in directories if not name.startswith(".")]
        for name in names:
            if not name.startswith(".") and Path(name).suffix.lower() in EXTENSIONS:
                files.append(Path(folder) / name)
        report(f"Finding audio files... {len(files):,} found", 0)
    files.sort(key=lambda path: str(path).casefold())
    if not files:
        raise ValueError("No WAV, AIFF or FLAC files found. The folder may be offline or contain only cloud placeholders.")
    samples, skipped, seen = [], 0, set()
    for index, path in enumerate(files):
        check_cancel()
        relative = path.relative_to(root)
        path_key = canonical_path(path)[1]
        seen.add(path_key)
        previous = cached.get(path_key)
        rule = catalogue.rules_for(path, rules=rules)
        ni_info = metadata_for_path(path)
        kind, confidence, why = classify_name(path if ni_info else relative)
        if kind == "unknown":
            kind, confidence, why = classify_name(Path(root.name) / relative)
        if rule.get("kind") == "exclude" or (kind == "skip" and not rule.get("kind")):
            skipped += 1
            if previous:
                samples.append(replace(_apply_rule(previous, rule), kind="excluded"))
            continue
        report(f"Analysing {index + 1:,}/{len(files):,}: {path.name}", (index + 1) / len(files))
        try:
            stat = path.stat()
            sample = previous if previous and (previous.size, previous.mtime_ns) == (stat.st_size, stat.st_mtime_ns) else None
            attributes = getattr(stat, "st_file_attributes", 0)
            if not include_online_only and attributes & (0x1000 | 0x400000):
                detail = "Online-only file: make it available offline, then update the library."
                notes.append(f"Deferred {relative}: {detail}")
                catalogue.record_scan_status(path, "online-only", detail, root=root)
                if previous:
                    offline = replace(previous, features={**previous.features, "available": False})
                    samples.append(_apply_rule(offline, rule))
                continue
            if sample is None:
                info = sf.info(path)
                if info.duration > MAX_SECONDS or info.channels not in (1, 2):
                    detail = f"longer than {MAX_SECONDS}s or more than two channels."
                    notes.append(f"Skipped {relative}: {detail}")
                    catalogue.record_scan_status(path, "not-one-shot", detail, root=root)
                    if previous:
                        unavailable = replace(previous, features={**previous.features, "available": False})
                        samples.append(_apply_rule(unavailable, rule))
                    continue
                check_cancel()
                data, rate = sf.read(path, dtype="float32", always_2d=True)
                features = audio_features(data, rate)
                features["audio_hash"] = _audio_digest(data, rate)
                base_kind, base_confidence = kind, confidence
                if kind == "unknown" and not (ni_info and ni_info.get("category") == "One Shots"):
                    base_kind, base_confidence, why = infer_kind(features)
                elif kind == "skip":
                    base_kind, base_confidence = "excluded", 1.0
                bpm_match = re.search(r"(\d{2,3})\s*BPM", path.stem, re.I)
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        check_cancel()
                        digest.update(chunk)
                after = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("File changed during analysis; update the library when syncing finishes.")
                fingerprint = digest.hexdigest()
                identity = hashlib.sha256(f"{path_key}:{fingerprint}".encode()).hexdigest()[:20]
                sample = Sample(identity, str(path), str(relative),
                                base_kind, base_confidence, why, info.duration, rate, info.channels, fingerprint,
                                stat.st_size, stat.st_mtime_ns, features, family_words(relative),
                                float(bpm_match.group(1)) if bpm_match else None,
                                tags=shape_tags(features), source=_source_name(root, relative),
                                base_kind=base_kind, base_confidence=base_confidence)
                sample = _decorate_ni(sample, ni_info)
                catalogue.save_sample(sample, ANALYSIS_VERSION, root=root)
            # Relative names/source are root-dependent; the cached audio analysis is not.
            sample = replace(sample, relative=str(relative), source=_source_name(root, relative),
                             features={**sample.features, "available": True})
            sample = _decorate_ni(sample, ni_info)
            if previous and statuses.get(path_key) != "available":
                catalogue.record_scan_status(path, "available", root=root)
            samples.append(_apply_rule(sample, rule))
        except (OSError, ValueError, RuntimeError) as exc:
            notes.append(f"Skipped {relative}: {exc}")
            catalogue.record_scan_status(path, "error", str(exc), root=root)
            if previous:
                unavailable = replace(previous, features={**previous.features, "available": False})
                samples.append(_apply_rule(unavailable, rule))
    for path_key, sample in cached.items():
        if path_key not in seen:
            unavailable = replace(sample, features={**sample.features, "available": False})
            samples.append(_apply_rule(unavailable, catalogue.rules_for(sample.path, rules=rules)))
            catalogue.record_scan_status(sample.path, "missing", "Not seen during the latest update; retained in saved kits.", root=root)
    notes.insert(0, f"Found {len(files)} audio files; analysed {len(samples)}; excluded {skipped} labelled loops/non-drums.")
    if not samples:
        raise ValueError("No available one-shots found. Check folder tags, scan notes and cloud download status.")
    report("Analysis complete", 1, True)
    return Session(str(root), samples, sources=[str(root)], scan_notes=notes)


def merge_library_session(existing, fresh):
    """Refresh catalogue records while preserving all pad choices, locks and sources."""
    if existing is None:
        return fresh
    incoming = dict(fresh.by_id)
    by_path = {os.path.normcase(sample.path): sample for sample in incoming.values()}
    by_fingerprint = {}
    for sample in incoming.values():
        if sample.features.get("available", True):
            by_fingerprint.setdefault(sample.fingerprint, []).append(sample)
    assigned = {pad.sample_id for pad in existing.pads if pad.sample_id}
    merged, relinked = [], {}
    for sample in existing.samples:
        replacement = incoming.pop(sample.id, None)
        if replacement:
            identical = by_fingerprint.get(sample.fingerprint, [])
            if not replacement.features.get("available", True) and len(identical) == 1:
                replacement = identical[0]
                incoming.pop(replacement.id, None)
                relinked[sample.id] = replacement.id
            merged.append(replacement)
            continue
        at_location = by_path.get(os.path.normcase(sample.path))
        if at_location and at_location.fingerprint == sample.fingerprint:
            # Upgrade old path-based IDs without changing the selected audio.
            incoming.pop(at_location.id, None)
            relinked[sample.id] = at_location.id
            merged.append(at_location)
        elif at_location and sample.id not in assigned:
            incoming.pop(at_location.id, None)
            merged.append(at_location)
        elif at_location:
            # Preserve the old choice visibly; the new version remains selectable.
            merged.append(replace(sample, features={**sample.features, "available": False}))
        else:
            identical = by_fingerprint.get(sample.fingerprint, [])
            if len(identical) == 1 and not Path(sample.path).is_file():
                replacement = identical[0]
                incoming.pop(replacement.id, None)
                relinked[sample.id] = replacement.id
                merged.append(replacement)
            else:
                merged.append(sample)
    merged.extend(incoming.values())
    # Several old locations may relink to the same exact file; retain one record.
    existing.samples = list({sample.id: sample for sample in merged}.values())
    for pad in existing.pads:
        if pad.sample_id in relinked:
            pad.sample_id = relinked[pad.sample_id]
    existing.sources = list(dict.fromkeys([existing.root, *existing.sources, fresh.root, *fresh.sources]))
    existing.scan_notes = fresh.scan_notes
    existing.library_revision += 1
    return existing


def project_dict(session):
    """Save the selected sounds, not a second copy of the whole catalogue."""
    selected = {pad.sample_id for pad in session.pads if pad.sample_id}
    compact = replace(session, samples=[sample for sample in session.samples if sample.id in selected])
    return compact.to_dict()


def load_audio(sample, rate=44100):
    path = Path(sample.path)
    stat = path.stat()
    if getattr(stat, "st_file_attributes", 0) & (0x1000 | 0x400000):
        raise ValueError(f"{path.name} is online-only. Make it available offline and update the library.")
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
