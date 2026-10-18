"""Verified per-kit WAV exports, prepared privately then committed as a complete folder."""
import csv
import hashlib
import json
import re
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from .audio import load_audio
from .matching import MATCHING_VERSION, refresh_warnings
from .model import is_sp_target, kit_count, role_names, sp_pad_number


def kit_folder_name(kit_index):
    if not 0 <= kit_index < 8:
        raise ValueError("Kit index must be 0–7.")
    return f"kit{kit_index + 1:03d}"


def kit_export_filename(pad_number, source_path, used, zero_pad=False):
    if not 1 <= pad_number <= 16:
        raise ValueError("Pad number must be 1–16.")
    stem = Path(source_path).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", stem).strip(" .") or "sample"
    # Keep the complete path usable on common sampler/computer filesystems.
    stem = stem[:140]
    prefix = f"{pad_number:02d}" if zero_pad else str(pad_number)
    candidate = f"{prefix}_{stem}.wav"
    number = 1
    while candidate.casefold() in used:
        candidate = f"{prefix}_{stem}_{number:02d}.wav"
        number += 1
    used.add(candidate.casefold())
    return candidate


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish(staging, folder, validation):
    """Prefer one directory rename; a completion marker supports restrictive sync volumes."""
    completion = json.dumps({"complete": True, **validation}, indent=2)
    (staging / "COMPLETE.json").write_text(completion, encoding="utf-8")
    try:
        staging.rename(folder)
        return
    except PermissionError:
        # Some Windows security/sync providers prohibit renaming a directory of files.
        # Keep an explicit incomplete marker until the final completion file commits.
        folder.mkdir()
        incomplete = folder / "INCOMPLETE.txt"
        incomplete.write_text("Export in progress. Do not import this folder.\n", encoding="utf-8")
        for path in staging.iterdir():
            if path.name != "COMPLETE.json":
                path.replace(folder / path.name)
        (staging / "COMPLETE.json").replace(folder / "COMPLETE.json")
        incomplete.unlink()
        staging.rmdir()


def export_kit(session, kit_index, destination, progress=lambda message, value: None):
    """Export one logical 8/16-pad kit; SP files use the device's top-down numbering."""
    if not 0 <= kit_index < kit_count(session):
        raise ValueError("Kit index is outside this session.")
    size, sp = session.kit_size, is_sp_target(session.target)
    rate = 48000 if sp else 44100
    start = kit_index * size
    pads = session.pads[start:start + size]
    assigned = [(slot, pad) for slot, pad in enumerate(pads) if pad.sample_id]
    if not assigned:
        raise ValueError("This kit has no samples to export.")
    refresh_warnings(session)
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    base = kit_folder_name(kit_index)
    folder = destination / base
    count = 1
    while folder.exists():
        folder = destination / f"{base}_{count:02d}"
        count += 1
    staging = destination / f".{folder.name}-{uuid.uuid4().hex[:10]}.partial"
    staging.mkdir()
    try:
        used, rows, padded = set(), [], 0
        samples = session.by_id
        for i, (slot, pad) in enumerate(assigned):
            sample = samples[pad.sample_id]
            if sample.kind == "excluded":
                raise ValueError(f"{Path(sample.path).name} is excluded by a folder rule. Replace this pad before exporting.")
            progress(f"Exporting {i + 1}/{len(assigned)}: {Path(sample.path).name}", (i + 1) / (len(assigned) + 1))
            if _file_hash(sample.path) != sample.fingerprint:
                raise ValueError(f"{Path(sample.path).name} changed since analysis. Rescan before exporting.")
            data = load_audio(sample, rate=rate)
            padding = max(0, round(rate * .1) - len(data)) if sp else 0
            if padding:
                data = np.pad(data, ((0, padding), (0, 0)))
                padded += 1
            number = sp_pad_number(slot) if sp else slot + 1
            name = kit_export_filename(number, sample.path, used, zero_pad=sp)
            path = staging / name
            sf.write(path, data, rate, subtype="PCM_16", format="WAV")
            info = sf.info(path)
            if (info.samplerate, info.subtype, info.channels, info.frames) != (rate, "PCM_16", sample.channels, len(data)):
                raise ValueError(f"Exported WAV validation failed: {name}")
            rows.append({"pad": number, "logical_slot": slot + 1, "role": role_names(size)[slot],
                         "file": name, "source": sample.path, "sample_id": sample.id,
                         "source_sha256": sample.fingerprint, "audio_hash": sample.features.get("audio_hash", ""),
                         "instrument": sample.kind, "confidence": sample.confidence,
                         "tags": ", ".join(sample.tags), "reason": pad.reason, "warning": pad.warning,
                         "trailing_silence_added_ms": round(padding / rate * 1000, 3),
                         "output_sha256": _file_hash(path)})
        with (staging / "PAD_MAP.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda row: row["pad"]))
        recipe = {"schema": 1, "matching_version": MATCHING_VERSION, "kit": kit_index + 1,
                  "kit_size": size, "target": session.target, "seed": session.seed,
                  "style": session.style, "surprise": session.surprise, "preview_bpm": session.bpm,
                  "library_revision": session.library_revision, "sources": session.sources,
                  "pads": rows}
        (staging / "KIT_RECIPE.json").write_text(json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8")
        validation = {"kit": kit_index + 1, "files": len(assigned), "skipped_empty": size - len(assigned),
                      "sample_rate": rate, "subtype": "PCM_16", "kit_size": size,
                      "target": session.target, "padded_short_samples": padded,
                      "validated": True, "hardware_tested": False}
        (staging / "VALIDATION.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
        instructions = ("SP-404MKII" if sp else "SAMPLER") + " KIT\n\n"
        instructions += ("WAV prefixes match SP pad numbers: top row 01–04, bottom row 13–16.\n"
                         "Import the WAVs and assign them to the corresponding pads.\n" if sp else
                         "WAV prefixes follow the kit's bottom-up logical pad order.\n")
        instructions += (f"Audio: {rate} Hz / 16-bit PCM; original mono/stereo preserved.\n"
                         "PAD_MAP.csv lists assignments; KIT_RECIPE.json records the exact chosen sources.\n"
                         "No mute groups, playback settings or pad links are carried by WAV files.\n"
                         "The preview's hi-hat choking is only an audition convenience.\n"
                         "Set any desired mute groups on your sampler.\n"
                         f"Empty pads: {size - len(assigned)}. Short samples padded with trailing silence: {padded}.\n"
                         "Original files were not modified. Computer validation passed; hardware loading is untested.\n")
        instructions += "\nPAD MAP\n" + "\n".join(f"{r['pad']:02d}  {r['role']}: {r['file']}" for r in sorted(rows, key=lambda r: r["pad"]))
        notes = [f"Pad {r['pad']:02d}: {r['warning']}" for r in rows if r["warning"]]
        if notes:
            instructions += "\n\nNOTES\n" + "\n".join(notes)
        (staging / "LOAD_ME.txt").write_text(instructions + "\n", encoding="utf-8")
        # A final kit name becomes visible only when all audio and companion files validate.
        _publish(staging, folder, validation)
        progress("Kit export complete", 1)
        return folder, validation
    except Exception as exc:
        if staging.exists():
            (staging / "EXPORT_FAILED.txt").write_text(f"Incomplete export. Do not import this folder.\n{exc}\n", encoding="utf-8")
        raise
