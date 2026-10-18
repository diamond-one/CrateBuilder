"""Native Instruments expansion discovery and conservative sample metadata.

Discovery and iteration inspect filenames, directory structure and file stats
only. Audio is opened exclusively by read_media_sample for a selected item.
Nothing is written to installed expansion folders.
"""
import hashlib
import os
from pathlib import Path, PurePosixPath
import re

from .catalogue import canonical_path


PROVIDER = "Native Instruments"
MEDIA_EXTENSIONS = frozenset({".wav", ".wave", ".aif", ".aiff", ".flac", ".rx2"})
_CATEGORIES = {"drums": "Drums", "oneshots": "One Shots", "loops": "Loops", "instruments": "Instruments"}
_DRUM_KINDS = {
    "kick": "kick", "kicks": "kick", "snare": "snare", "snares": "snare",
    "clap": "clap", "claps": "clap", "rim": "rim", "rimshot": "rim",
    "hihat": "hat", "hihats": "hat", "hat": "hat", "hats": "hat", "hh": "hat",
    "hihatclosed": "hat", "closedhihat": "hat", "closedhh": "hat",
    "hihatopen": "open_hat", "openhihat": "open_hat", "openhh": "open_hat",
    "cymbal": "cymbal", "cymbals": "cymbal", "crash": "cymbal", "ride": "cymbal",
    "percussion": "perc", "perc": "perc", "tom": "perc", "toms": "perc",
    "shaker": "perc", "handdrum": "perc", "malletdrum": "perc", "wooden": "perc",
    "metallic": "perc", "tambourine": "perc", "combo": "perc",
}
_MUSICAL_ONE_SHOTS = frozenset({
    "acousticnote", "bass", "bowedstrings", "brass", "chord", "chords", "flute",
    "guitar", "keys", "lick", "mallet", "organ", "pianokeys", "pluckedstrings",
    "reed", "singlenote", "stabhit", "impactsstabs", "synth", "synthlead", "synthmisc",
    "synthnote", "synthpad", "vocal", "vocals", "wind",
})


def _fold(text):
    return re.sub(r"[^a-z0-9]", "", str(text).casefold())


def _pack_name(folder):
    return re.sub(r"\s+library$", "", folder, flags=re.I).strip()


def _source_id(samples_root):
    return "ni:" + canonical_path(samples_root)[1].casefold()


def _context(path):
    # Lexical parsing also handles Windows paths in cross-platform tests, and
    # never probes a disconnected drive or opens a source file.
    pure = PurePosixPath(str(path).replace("\\", "/"))
    parts = pure.parts
    for index in range(len(parts) - 1, 0, -1):
        if parts[index].casefold() != "samples" or not parts[index - 1].casefold().endswith(" library"):
            continue
        relative = parts[index + 1:]
        category = _CATEGORIES.get(_fold(relative[0]), relative[0]) if len(relative) > 1 else "Samples"
        subtype = relative[1] if len(relative) > 2 else ""
        samples_root = canonical_path(str(PurePosixPath(*parts[:index + 1])))[0]
        return {"pack": _pack_name(parts[index - 1]), "root": canonical_path(str(PurePosixPath(*parts[:index])))[0],
                "samples_root": samples_root, "source": _source_id(samples_root),
                "category": category, "subtype": subtype, "name": pure.name}
    return None


def classify_ni_path(path):
    """Return (kind, confidence, explanation), or None outside known NI labels.

    OpenHH/ClosedHH and DrumBreak/DrumFill work even when a caller supplied a
    path relative to Drums. NI musical/loop categories take precedence over
    filename guesses and stay outside automatic drum-kit generation.
    """
    context = _context(path)
    pure = PurePosixPath(str(path).replace("\\", "/"))
    category = context["category"] if context else None
    subtype = _fold(context["subtype"]) if context else ""
    if not context and len(pure.parts) > 1:
        category = _CATEGORIES.get(_fold(pure.parts[0]))
        subtype = _fold(pure.parts[1]) if len(pure.parts) > 2 else ""
    if category in {"Loops", "Instruments"}:
        return "skip", 1.0, f"NI {category} category; browse or preview separately"
    if category == "One Shots" and subtype in _MUSICAL_ONE_SHOTS:
        return "skip", 1.0, "NI musical one-shot category; use by explicit selection"
    title = _fold(pure.stem)
    if title.startswith(("drumbreak", "drumloop", "breakbeat")):
        return "skip", .99, "NI drum break/loop filename"
    if title.startswith(("drumfill", "snareroll", "snarefill", "snareflam")):
        return "fill", .99, "NI drum-fill/snare-roll filename"
    if title.startswith(("openhh", "openhihat", "openhat", "hhopen", "hihatopen")):
        return "open_hat", .99, "NI open hi-hat filename"
    if title.startswith(("closedhh", "closedhihat", "closedhat", "hhclosed", "hihatclosed")):
        return "hat", .99, "NI closed hi-hat filename"
    if category == "Drums" and subtype in _DRUM_KINDS:
        return _DRUM_KINDS[subtype], .98, "NI drum category: " + (context["subtype"] if context else pure.parts[1])
    if category == "One Shots":
        if subtype in {"click", "impact", "hit", "strike", "drums", "metal"}:
            return "perc", .85, "NI percussive one-shot category"
        return "unknown", .25, "NI one-shot; retain its category and review by ear"
    return None


def _musical_labels(name):
    bpm_match = re.search(r"(?<!\d)(\d{2,3})\s*[-_]?\s*bpm\b", name, re.I)
    bpm = float(bpm_match.group(1)) if bpm_match else None
    if bpm is not None and not 30 <= bpm <= 300:
        bpm = None
    # NI loop names commonly use Instrument[tempo] Key KitName. The position
    # immediately after a valid bracketed tempo is explicit key metadata; a
    # lone A/B/etc. elsewhere could simply be part of a sound or kit name.
    bracket = re.match(r"^[^\[\]]+\[(\d{2,3})\]", name)
    bracket_bpm = float(bracket.group(1)) if bracket else None
    if bracket_bpm is not None and not 30 <= bracket_bpm <= 300:
        bracket_bpm = None
    if bpm is None:
        bpm = bracket_bpm
    key_match = None
    if bracket_bpm is not None:
        tail = name[bracket.end():].lstrip(" _-")
        key_match = re.match(r"([A-G](?:#|b)?(?:(?:maj(?:or)?|min(?:or)?|m))?)(?=$|[\s_()\[\]-])", tail, re.I)
    if key_match is None:
        key_match = re.search(r"(?:^|[\s_()\[\]-])([A-G](?:#|b)?(?:maj(?:or)?|min(?:or)?|m))(?=$|[\s_()\[\]-])", name, re.I)
    key = key_match.group(1) if key_match else None
    if key:
        key = key[0].upper() + key[1:]
    return bpm, key


def metadata_for_path(path):
    """Describe NI provenance without reading audio or querying file metadata."""
    context = _context(path)
    if context is None:
        return None
    classification = classify_ni_path(path)
    bpm, key = _musical_labels(PurePosixPath(str(path).replace("\\", "/")).stem)
    return {**context, "path": canonical_path(path)[0], "provider": PROVIDER,
            "kind": classification[0] if classification else "unknown", "bpm": bpm, "key": key}


def _is_link(path):
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _safe_child(parent, name):
    try:
        for child in parent.iterdir():
            if child.name.casefold() == name.casefold() and not _is_link(child) and child.is_dir():
                if child.resolve().is_relative_to(parent.resolve()):
                    return child
    except OSError:
        return None
    return None


def discover_expansions(root="C:/Users/Public/Documents"):
    """List expansion folders having Samples/Drums, without analysing any audio."""
    root = Path(root).expanduser().absolute()
    try:
        children = sorted(root.iterdir(), key=lambda child: child.name.casefold())
    except OSError:
        return []
    result = []
    for folder in children:
        try:
            if not folder.name.casefold().endswith(" library") or _is_link(folder) or not folder.is_dir():
                continue
            if not folder.resolve().is_relative_to(root.resolve()):
                continue
            samples = _safe_child(folder, "Samples")
            drums = _safe_child(samples, "Drums") if samples else None
            if drums is None:
                continue
            result.append({"id": _source_id(samples), "name": _pack_name(folder.name),
                           "root": str(folder), "samples_root": str(samples), "drums_root": str(drums)})
        except OSError:
            continue
    return result


def iter_expansion_items(expansion, cancel=None):
    """Yield filename/stat records, including loops and musical browser material."""
    samples_root = Path(expansion["samples_root"]).expanduser().absolute()
    pack_root = Path(expansion["root"]).expanduser().absolute()
    if _is_link(samples_root) or _is_link(pack_root):
        raise ValueError("Expansion folders must not redirect outside their installed source.")
    resolved_root = samples_root.resolve()
    if not resolved_root.is_relative_to(pack_root.resolve()):
        raise ValueError("The Samples folder is outside the selected expansion.")
    if not samples_root.is_dir():
        raise ValueError("The expansion Samples folder is unavailable.")
    for folder, directories, names in os.walk(samples_root, followlinks=False):
        if cancel and cancel.is_set():
            raise InterruptedError("Expansion indexing cancelled")
        current = Path(folder)
        if not current.resolve().is_relative_to(resolved_root):
            directories[:] = []
            continue
        directories[:] = sorted(name for name in directories
                                if not name.startswith(".") and not _is_link(current / name)
                                and (current / name).resolve().is_relative_to(resolved_root))
        for name in sorted(names, key=str.casefold):
            if cancel and cancel.is_set():
                raise InterruptedError("Expansion indexing cancelled")
            path = current / name
            if name.startswith(".") or path.suffix.casefold() not in MEDIA_EXTENSIONS or _is_link(path):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            item = metadata_for_path(path)
            if item is not None:
                yield {**item, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def read_media_sample(item, catalogue=None):
    """Analyse one explicitly chosen browser item; long audio has a bounded preview.

    Full source duration and a file fingerprint are retained. Only the first
    15 seconds are decoded for analysis/preview when the source is longer.
    Loops, instruments and long files are preview-only; ambiguous and musical
    one-shots keep an unknown role and require explicit manual selection.
    """
    from dataclasses import replace
    import soundfile as sf
    from .audio import (ANALYSIS_VERSION, MAX_SECONDS, _apply_rule, _audio_digest,
                        _decorate_ni, audio_features, family_words, shape_tags)
    from .model import Sample

    path = Path(item["path"]).expanduser().absolute()
    if path.suffix.casefold() == ".rx2":
        raise ValueError("RX2 files are listed for reference; convert them to WAV before previewing or using a pad.")
    if path.suffix.casefold() not in MEDIA_EXTENSIONS:
        raise ValueError("Choose a WAV, AIFF or FLAC sample.")
    stat = path.stat()
    if getattr(stat, "st_file_attributes", 0) & (0x1000 | 0x400000):
        raise ValueError("This sample is online-only. Make it available offline before previewing it.")
    info = metadata_for_path(path) or dict(item)
    cached = catalogue.get_sample(path, stat.st_size, stat.st_mtime_ns, ANALYSIS_VERSION) if catalogue else None
    if cached is not None:
        sample = _decorate_ni(cached, info)
    else:
        audio_info = sf.info(path)
        if audio_info.channels not in (1, 2) or audio_info.frames <= 0:
            raise ValueError("Preview needs non-empty mono or stereo audio.")
        frames = min(audio_info.frames, round(MAX_SECONDS * audio_info.samplerate))
        data, rate = sf.read(path, frames=frames, dtype="float32", always_2d=True)
        features = audio_features(data, rate)
        features["audio_hash"] = _audio_digest(data, rate)
        features["available"] = True
        classification = classify_ni_path(path)
        kind, confidence, why = classification or ("unknown", .15, "Explicitly selected sample; review by ear")
        manual_only = kind in {"skip", "unknown"}
        if kind == "skip":
            kind = "unknown"
        preview_only = info.get("category") in {"Loops", "Instruments"} or audio_info.duration > MAX_SECONDS
        features.update(manual_only=manual_only, preview_only=preview_only,
                        preview_frames=frames, source_duration=audio_info.duration)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("The sample changed during analysis. Wait for syncing to finish and try again.")
        fingerprint = digest.hexdigest()
        key = canonical_path(path)[1]
        identity = hashlib.sha256(f"{key}:{fingerprint}".encode()).hexdigest()[:20]
        relative = str(path.relative_to(Path(info["samples_root"]))) if info.get("samples_root") else path.name
        sample = Sample(identity, str(path), relative, kind, confidence, why,
                        audio_info.duration, rate, audio_info.channels, fingerprint,
                        stat.st_size, stat.st_mtime_ns, features, family_words(path), info.get("bpm"),
                        tags=shape_tags(features), source=(info.get("pack", "") if info.get("source") == "__user__"
                                                         else info.get("source", "")),
                        base_kind=kind, base_confidence=confidence)
        sample = _decorate_ni(sample, info)
        if catalogue:
            catalogue.save_sample(sample, ANALYSIS_VERSION, root=info.get("samples_root", path.parent))
    sample = replace(sample, features={**sample.features, "available": True})
    if catalogue:
        catalogue.record_scan_status(path, "available", root=info.get("samples_root", path.parent))
        sample = _apply_rule(sample, catalogue.rules_for(path))
    return sample


def load_media_audio(sample, seconds=12, rate=44100):
    """Read a bounded browser preview, independent of full-file pad/export loading."""
    import math
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    seconds = float(seconds)
    rate = int(rate)
    if not math.isfinite(seconds) or seconds <= 0 or rate <= 0:
        raise ValueError("Choose a positive preview duration and sample rate.")
    path = Path(sample.path)
    stat = path.stat()
    if getattr(stat, "st_file_attributes", 0) & (0x1000 | 0x400000):
        raise ValueError("This sample is online-only. Make it available offline before previewing it.")
    if (stat.st_size, stat.st_mtime_ns) != (sample.size, sample.mtime_ns):
        raise ValueError("This sample changed since analysis. Refresh the expansion and try again.")
    frames = max(1, math.ceil(seconds * sample.rate))
    data, source_rate = sf.read(path, frames=frames, dtype="float32", always_2d=True)
    if data.size == 0 or data.shape[1] not in (1, 2) or not np.isfinite(data).all():
        raise ValueError("Preview needs valid mono or stereo audio.")
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    if source_rate != rate:
        divisor = math.gcd(source_rate, rate)
        data = resample_poly(data, rate // divisor, source_rate // divisor, axis=0)
    peak = float(np.max(np.abs(data)))
    if peak > .999:
        data = data * (.999 / peak)
    return np.ascontiguousarray(data, dtype=np.float32)
