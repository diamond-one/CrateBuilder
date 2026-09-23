"""Folder-wide conversion to MPC1000-compatible WAV files."""
import csv
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .audio import EXTENSIONS
from .pgm import safe_name


def _fresh_folder(parent, name):
    folder = parent / name
    number = 1
    while folder.exists():
        folder = parent / f"{name}_{number:02d}"
        number += 1
    folder.mkdir(parents=True)
    return folder


def _unique_relative(relative, used):
    parents = [safe_name(part, 32) for part in relative.parts[:-1]]
    stem = safe_name(relative.stem, 16)
    candidate = Path(*parents, stem + ".WAV")
    number = 1
    while str(candidate).casefold() in used:
        suffix = f"_{number:02d}"
        candidate = Path(*parents, stem[:16 - len(suffix)] + suffix + ".WAV")
        number += 1
    used.add(str(candidate).casefold())
    return candidate


def _inside_previous_conversion(path, source):
    for parent in path.parents:
        if parent == source:
            return False
        if (parent / "CONVERSION_REPORT.txt").is_file() or (parent / "CONVERSION_INCOMPLETE.txt").is_file():
            return True
    return False


def convert_folder(source, destination, progress=lambda message, value: None, cancel=None):
    """Convert every supported audio file below source into a fresh MPC WAV folder."""
    source = Path(str(source).strip().strip('"')).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("Source folder not found.")
    files = sorted(
        (path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in EXTENSIONS
         and not _inside_previous_conversion(path, source)
         and not any(part.startswith(".") for part in path.relative_to(source).parts)),
        key=lambda path: str(path).casefold(),
    )
    if not files:
        raise ValueError("No WAV, AIFF or FLAC files found in this folder or its subfolders.")
    destination.mkdir(parents=True, exist_ok=True)
    output = _fresh_folder(destination, safe_name(source.name, 24) + "_MPC441")
    rows, errors, used = [], [], set()
    try:
        for index, path in enumerate(files):
            if cancel and cancel.is_set():
                raise InterruptedError("Conversion cancelled")
            relative = path.relative_to(source)
            progress(f"Converting {index + 1}/{len(files)}: {path.name}", index / len(files))
            try:
                data, rate = sf.read(path, dtype="float32", always_2d=True)
                if data.shape[1] not in (1, 2):
                    raise ValueError(f"{data.shape[1]} channels; the MPC1000 supports mono or stereo")
                if not len(data) or not np.isfinite(data).all():
                    raise ValueError("empty or invalid audio")
                if rate != 44100:
                    divisor = math.gcd(int(rate), 44100)
                    data = resample_poly(data, 44100 // divisor, int(rate) // divisor, axis=0)
                peak = float(np.max(np.abs(data)))
                gain = min(1.0, .999 / peak) if peak else 1.0
                data = (data * gain).astype(np.float32)
                target_relative = _unique_relative(relative, used)
                target = output / target_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                sf.write(target, data, 44100, subtype="PCM_16", format="WAV")
                info = sf.info(target)
                if info.samplerate != 44100 or info.subtype != "PCM_16" or info.channels not in (1, 2):
                    raise RuntimeError("written file failed MPC format verification")
                rows.append({
                    "source": str(relative), "output": str(target_relative),
                    "source_rate": int(rate), "channels": info.channels,
                    "duration_seconds": round(info.duration, 6),
                    "attenuated_to_prevent_clipping": "yes" if gain < 1 else "no",
                })
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append({"source": str(relative), "error": str(exc)})
        with (output / "FILE_MAP.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["source", "output", "source_rate", "channels",
                                                                "duration_seconds", "attenuated_to_prevent_clipping"])
            writer.writeheader()
            writer.writerows(rows)
        report = [
            "CRATE BUILDER / MPC WAV CONVERSION", "",
            f"Source: {source}", f"Converted: {len(rows)}", f"Failed: {len(errors)}", "",
            "Output audio is 44.1 kHz, 16-bit PCM WAV. Mono/stereo and original duration are preserved.",
            "No EQ, tuning, time stretching, trimming or normalization was applied.",
            "Only peaks above the PCM limit were attenuated to prevent clipping.",
            "FILE_MAP.csv records every converted source and its MPC-safe output name.",
        ]
        if errors:
            report.extend(["", "FILES NOT CONVERTED"] + [f"{row['source']}: {row['error']}" for row in errors])
        (output / "CONVERSION_REPORT.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
        progress("Conversion complete", 1)
        return output, {"found": len(files), "converted": len(rows), "failed": len(errors), "errors": errors}
    except Exception:
        (output / "CONVERSION_INCOMPLETE.txt").write_text(
            "Conversion did not finish. Original source files were not changed.\n", encoding="utf-8")
        raise
