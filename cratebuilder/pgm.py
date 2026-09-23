"""MPC1000 binary PGM 1.00 writer using Stephen Norum's default program.

The unmodified default template and its zlib licence are included. The public
format's prose/struct examples disagree about velocity bound labels; preserve
the original default bytes (lower=0, upper=127) at offsets 0x12/0x13.
"""
import csv
import hashlib
import json
import re
import struct
import unicodedata
from pathlib import Path

import soundfile as sf

from .audio import load_audio
from .matching import refresh_warnings
from .model import ROLES, pad_name

FILE_SIZE = 0x2A04
PAD_BASE = 0x18
PAD_SIZE = 0xA4


def safe_name(name, length=16):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9_]+", "_", name).strip("_")[:length] or "PROGRAM"


def sample_names(session):
    names, used = {}, {}
    for pad in session.pads:
        if pad.sample_id is None or pad.sample_id in names:
            continue
        s = session.by_id[pad.sample_id]
        stem = safe_name(Path(s.path).stem, 8)
        name = f"{stem}_{s.fingerprint[:7].upper()}"
        # Handle even a rare shortened-hash collision, including case folding.
        attempt = 0
        while name in used and used[name] != s.fingerprint:
            attempt += 1
            suffix = hashlib.sha256(f"{s.fingerprint}:{attempt}".encode()).hexdigest()[:7].upper()
            name = f"{stem}_{suffix}"
        names[s.id] = name
        used[name] = s.fingerprint
    return names


def program_bytes(session, names):
    if len(session.pads) != 64:
        raise ValueError("The MPC1000 needs 64 pad assignments.")
    data = bytearray((Path(__file__).parent / "assets" / "EMPTY.PGM").read_bytes())
    if len(data) != FILE_SIZE or data[4:20].rstrip(b"\0") != b"MPC1000 PGM 1.00":
        raise ValueError("Invalid program template")
    for i, pad in enumerate(session.pads):
        base = PAD_BASE + i * PAD_SIZE
        for layer in range(4):
            pos = base + layer * 24
            data[pos:pos + 16] = bytes(16)
        if pad.sample_id:
            name = names[pad.sample_id].encode("ascii")
            if not 0 < len(name) <= 16:
                raise ValueError("Sample names must contain 1–16 ASCII characters")
            data[base:base + 16] = name.ljust(16, b"\0")
            data[base + 17] = 70  # MPC default sample level, retaining mixer headroom.
            data[base + 18:base + 20] = bytes([0, 127])
            data[base + 22] = 0  # One shot.
        data[base + 0x63] = i // 8 + 1 if i % 8 in (6, 7) else 0
    return bytes(data)


def validate_program(path):
    """Independent offset checks plus referenced WAV format and existence checks."""
    path = Path(path)
    data = path.read_bytes()
    if len(data) != FILE_SIZE or struct.unpack_from("<H", data)[0] != FILE_SIZE:
        raise ValueError("PGM size/header mismatch")
    if data[4:20] != b"MPC1000 PGM 1.00":
        raise ValueError("Not an MPC1000 binary program")
    names, notes = [], list(data[0x2918:0x2958])
    if len(set(notes)) != 64:
        raise ValueError("MIDI pad notes are not unique")
    memory_bytes = 0
    seen = set()
    for i in range(64):
        if data[0x2958 + notes[i]] != i:
            raise ValueError("MIDI note/pad mapping mismatch")
        base = 24 + i * 164
        name = data[base:base + 16].rstrip(b"\0").decode("ascii")
        names.append(name)
        if not name:
            continue
        if not re.fullmatch(r"[A-Z0-9_]{1,16}", name):
            raise ValueError(f"Unsafe sample name: {name}")
        if data[base + 18:base + 20] != bytes([0, 127]) or data[base + 22] != 0:
            raise ValueError("Invalid velocity bounds or sample play mode")
        if any(data[base + layer * 24:base + layer * 24 + 16] != bytes(16) for layer in (1, 2, 3)):
            raise ValueError("Unexpected layered sample")
        info = sf.info(path.parent / (name + ".WAV"))
        if info.samplerate != 44100 or info.subtype != "PCM_16" or info.channels not in (1, 2):
            raise ValueError(f"WAV format is not MPC1000-compatible: {name}")
        if name not in seen:
            memory_bytes += info.frames * info.channels * 2
            seen.add(name)
    return {"format": "MPC1000 PGM 1.00", "bytes": len(data), "assigned_pads": sum(bool(n) for n in names),
            "unique_wavs": len(seen), "sample_memory_mb": round(memory_bytes / 1024 ** 2, 2),
            "hardware_tested": False}


def export_program(session, destination, name="PROGRAM", progress=lambda message, value: None):
    refresh_warnings(session)
    if not any(p.sample_id for p in session.pads):
        raise ValueError("Build a kit before exporting.")
    names = sample_names(session)
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    program_name = safe_name(name)
    folder = destination / program_name
    count = 1
    while folder.exists():
        suffix = f"_{count:02d}"
        folder = destination / (program_name[:16 - len(suffix)] + suffix)
        count += 1
    folder.mkdir()  # A fresh directory; never touch the original samples or older exports.
    try:
        exported = set()
        for i, (sample_id, wav_name) in enumerate(names.items()):
            progress(f"Preparing {i + 1}/{len(names)}: {wav_name}.WAV", (i + 1) / (len(names) + 1))
            if wav_name in exported:
                continue
            s = session.by_id[sample_id]
            if hashlib.sha256(Path(s.path).read_bytes()).hexdigest() != s.fingerprint:
                raise ValueError(f"{Path(s.path).name} changed since analysis. Rescan before exporting.")
            data = load_audio(s)
            sf.write(folder / (wav_name + ".WAV"), data, 44100, subtype="PCM_16", format="WAV")
            exported.add(wav_name)
        pgm_path = folder / (program_name + ".PGM")
        pgm_path.write_bytes(program_bytes(session, names))
        validation = validate_program(pgm_path)
        rows = []
        for i, pad in enumerate(session.pads):
            s = session.by_id.get(pad.sample_id)
            rows.append({"pad": pad_name(i), "kit": i // 8 + 1, "role": ROLES[i % 8],
                         "file": names[s.id] + ".WAV" if s else "", "source": s.path if s else "",
                         "instrument": s.kind if s else "", "reason": pad.reason, "warning": pad.warning,
                         "hat_mute_group": i // 8 + 1 if i % 8 in (6, 7) else 0})
        with (folder / "PAD_MAP.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        (folder / "MANIFEST.json").write_text(json.dumps({"validation": validation, "seed": session.seed,
            "style": session.style, "preview_bpm": session.bpm, "pads": rows}, indent=2), encoding="utf-8")
        (folder / "SESSION.json").write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
        warnings = [f"{r['pad']}: {r['warning']}" for r in rows if r["warning"]]
        (folder / "LOAD_ME.txt").write_text(
            f"CRATE BUILDER / MPC1000\n\nCopy this whole folder to your MPC card.\n"
            f"On the MPC1000 LOAD page, load {program_name}.PGM with its samples.\n"
            "Banks A-D each contain two kits: pads 01-08 and 09-16.\n"
            "The two hi-hats in each kit share their own mute group.\n"
            "WAV copies are 44.1 kHz / 16-bit PCM, preserving mono/stereo and original lengths.\n"
            "No EQ, time stretch, automatic pitch change, or loudness normalization is applied.\n"
            "Program sample levels use the MPC default of 70; mixer levels are 100.\n"
            "Only overs are attenuated to avoid clipping after conversion.\n"
            f"Sample memory: approximately {validation['sample_memory_mb']} MB, plus MPC overhead.\n\n"
            "Computer validation passed; loading on physical MPC hardware still needs testing.\n"
            "Try this on the MPC before relying on it in a performance.\n\n"
            "NOTES\n" + ("\n".join(warnings) if warnings else "No substitutions or empty pads.") + "\n",
            encoding="utf-8")
        progress("Export verified", 1)
        return folder, validation
    except Exception as exc:
        (folder / "EXPORT_FAILED.txt").write_text(f"Incomplete export. Do not load this folder.\n{exc}\n", encoding="utf-8")
        raise
