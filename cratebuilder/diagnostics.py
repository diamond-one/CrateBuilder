"""A self-contained installation check using generated audio and an off-screen UI."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import time
import traceback


def _check_ni_library(root, session, catalogue, report):
    """Exercise the complete NI path using only copies of generated test audio."""
    import soundfile as sf
    from .audio import merge_library_session, scan_folder
    from .library import LibraryStore, apply_library_marks, export_collection
    from .matching import Matcher
    from .model import Session
    from .ni import discover_expansions, iter_expansion_items, read_media_sample

    installed = root / "synthetic-ni"
    layout = [("Kick", "Kick_fixture0.wav", "KICKS", 0),
              ("Kick", "Kick_fixture1.wav", "KICKS", 1),
              ("Snare", "Snare_fixture.wav", "SNARES", 0),
              ("Clap", "Clap_fixture.wav", "CLAPS", 0),
              ("HiHat", "ClosedHH_fixture.wav", "HI-HATS", 0),
              ("HiHat", "OpenHH_fixture.wav", "OPEN HATS", 0),
              ("Cymbal", "Cymbal_fixture.wav", "CYMBALS", 0),
              ("Fill", "DrumFill_fixture.wav", "FILLS", 0)]
    layout.extend(("Percussion", f"Perc_fixture{index}.wav", "Kitchen", index) for index in range(8))
    for name in ("Diagnostic Warm Library", "Diagnostic Crisp Library"):
        samples = installed / name / "Samples"
        for subtype, filename, category, index in layout:
            target = samples / "Drums" / subtype / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / "samples" / category / f"sound{index}.wav", target)
        for category, subtype, filename in (("One Shots", "Chord", "Chord_Cm_fixture.wav"),
                                             ("Loops", "Drums", "DrumBreak_90BPM_fixture.wav"),
                                             ("Instruments", "Bass", "Bass_Cm_fixture.wav")):
            target = samples / category / subtype / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / "samples/KICKS/sound2.wav", target)

    store = LibraryStore(root / "data/library.sqlite3")
    real_read, real_info = sf.read, sf.info
    def no_decode(*args, **kwargs):
        raise AssertionError("NI metadata discovery must not decode sample audio")
    sf.read = sf.info = no_decode
    try:
        packs = discover_expansions(installed)
        items = [item for pack in packs for item in iter_expansion_items(pack)]
        assert len(packs) == 2 and store.upsert_items(items) == 38
    finally:
        sf.read, sf.info = real_read, real_info
    assert store.count_items(category="Drums") == 32
    assert store.count_items(category="Loops") == 2
    assert store.count_items(category="Instruments") == 2
    assert len(store.list_sources()) == 2
    report["checks"].append("NI discovery and metadata-only indexing: 2 synthetic packs, 38 categorized files")

    active = packs[0]
    fresh = scan_folder(active["drums_root"], root / "cache", catalogue=catalogue)
    assert len(fresh.samples) == 16
    # Explicit browser analysis covers manual-only and preview-only category rules.
    musical = next(item for item in items if item["source"] == active["id"] and item["category"] == "One Shots")
    loop = next(item for item in items if item["source"] == active["id"] and item["category"] == "Loops")
    other_kick = next(item for item in items if item["source"] == packs[1]["id"] and item["kind"] == "kick")
    extras = [read_media_sample(item, catalogue=catalogue) for item in (musical, loop, other_kick)]
    assert extras[0].features["manual_only"] and extras[1].features["preview_only"]
    fresh.samples.extend(extras)
    session = merge_library_session(session, fresh)
    session.generation_sources, session.source_mode = [active["id"]], "same_pack"
    favourite = next(sound for sound in fresh.samples if sound.kind == "kick" and sound.features.get("ni_source") == active["id"])
    hidden = next(sound for sound in fresh.samples if sound.kind == "perc")
    store.set_mark(favourite.path, favourite=True)
    store.set_mark(hidden.path, hidden=True)
    apply_library_marks(session, store)
    assert store.count_items(favourites_only=True) == 1
    assert store.count_items(hidden_only=True) == 1
    assert hidden.id not in Matcher(session).vectors
    session.favourites_only = True
    assert [sound.id for sound in Matcher(session).pool(4)] == [favourite.id]
    session.favourites_only = False
    store.set_mark(hidden.path, hidden=False)
    apply_library_marks(session, store)
    settings = {"generation_sources": [active["id"]], "source_mode": "same_pack",
                "favourites_only": False, "unused_only": False}
    store.save_crate("Diagnostic NI crate", settings)
    assert store.list_crates()[0]["settings"] == settings
    Matcher(session).build(0)
    assert all(session.by_id[pad.sample_id].features.get("ni_source") == active["id"] for pad in session.pads[:16])
    assert extras[0].id not in {pad.sample_id for pad in session.pads[:16]}
    assert extras[1].id not in Matcher(session).vectors
    report["checks"].append("Active NI pack generation, safe manual/preview categories, saved crate, favourites and hiding")

    selected = [next(item for item in items if item["source"] == pack["id"] and item["kind"] == "kick") for pack in packs]
    hashes = {item["path"]: hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() for item in selected}
    output = export_collection(selected, root / "collections", "Synthetic NI collection")
    manifest = json.loads((output / "COLLECTION_MANIFEST.json").read_text(encoding="utf-8"))
    assert (output / "COMPLETE.json").is_file() and len(manifest["files"]) == 2
    for row in manifest["files"]:
        assert hashlib.sha256((output / row["file"]).read_bytes()).hexdigest() == hashes[row["source_path"]]
        assert hashlib.sha256(Path(row["source_path"]).read_bytes()).hexdigest() == hashes[row["source_path"]]
    report["checks"].append("Explicit NI collection copies: original filenames, provenance and checksums verified")
    return session, packs


def _wait_for_ui(app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for the off-screen library browser")
        app.update()
        time.sleep(.01)


def self_check(report_path):
    report = {"ok": False, "checks": []}
    previous = os.environ.get("CRATEBUILDER_DATA_DIR")
    try:
        import numpy as np
        import soundfile as sf
        from .audio import scan_folder
        from .catalogue import Catalogue
        from .matching import Matcher
        from .kit_export import export_kit
        from .preview import render_groove
        with tempfile.TemporaryDirectory(prefix="cratebuilder-check-") as temporary:
            root = Path(temporary)
            os.environ["CRATEBUILDER_DATA_DIR"] = str(root / "data")
            catalogue = Catalogue(root / "data/catalogue.sqlite3")
            kitchen = root / "samples/Kitchen"
            catalogue.set_folder_rule(kitchen, ["kitchen", "utensils", "percussion"], kind="foley")
            rng = np.random.default_rng(42)
            for category in ("KICKS", "SNARES", "CLAPS", "HI-HATS", "OPEN HATS", "CYMBALS", "FILLS", "Kitchen"):
                folder = root / "samples" / category
                folder.mkdir(parents=True)
                for index in range(8):
                    time = np.arange(6000 + index * 100) / 48000
                    wave = np.sin(time * (350 + index * 11)) if category == "KICKS" else rng.normal(0, .3, len(time))
                    sf.write(folder / f"sound{index}.wav", wave * np.exp(-time * 20), 48000)
            session = scan_folder(root / "samples", root / "cache", catalogue=catalogue)
            assert len(session.samples) == 64
            assert all("kitchen" in sound.tags for sound in session.samples if sound.kind == "foley")
            report["checks"].append("SQLite catalogue, inherited folder tags and audio decoding")
            session.kit_size, session.target = 16, "sp404"
            Matcher(session).build(0)
            from .editing import regenerate_indices
            from .matching import audio_identity
            for _ in range(4):
                previous_sound = audio_identity(session.by_id[session.pads[4].sample_id])
                regenerate_indices(session, [4])
                assert audio_identity(session.by_id[session.pads[4].sample_id]) != previous_sound
            report["checks"].append("Repeated generation changes suitable sounds while preserving other pads")
            folder, info = export_kit(session, 0, root / "exports")
            assert info["files"] == 16 and info["validated"]
            assert (folder / "PAD_MAP.csv").is_file()
            report["checks"].append("16-pad matching, SP pad mapping and validated 48 kHz / 16-bit export")
            groove = render_groove(session, 0)
            assert np.isfinite(groove).all() and float(np.max(np.abs(groove))) <= .851
            report["checks"].append("Audio preview rendering")
            session, ni_packs = _check_ni_library(root, session, catalogue, report)
            import sounddevice
            sounddevice.get_portaudio_version()
            report["checks"].append("PortAudio playback library loaded")
            from .live_midi import list_input_ports
            list_input_ports()
            report["checks"].append("MIDI input backend loaded")
            from .sp_transfer import discover_cli, _run
            cli = discover_cli()
            if cli is None:
                raise RuntimeError("The bundled SP USB helper is missing.")
            version = _run(cli, ["--version"], timeout=30).strip()
            report["checks"].append("Bundled SP USB helper: " + version)
            from .ui import App
            app = App()
            try:
                app.geometry("1280x900+10000+10000")
                app.update()
                assert "A09" in app.pad_buttons[4].cget("text")
                app.manage_folder_tags()
                app.tag_window.geometry("810x570+10000+10000")
                app.update()
                assert app.tag_window.winfo_exists()
                app.tag_window.destroy()
                report["checks"].append("Desktop interface and folder-tag editor")
                app.session = session
                app.refresh()
                assert app.live_button.cget("text") == "MIDI preview: OFF"
                app.session.pads[4].locked = True
                app.toggle_bank_lock()
                assert app.session.locked_banks == [0]
                app.toggle_bank_lock()
                assert app.session.pads[4].locked and not app.session.locked_banks
                for local in (8, 12):
                    app.pad_select_vars[local].set(True)
                    app.toggle_pad_selection(local)
                assert app.shuffle_button.cget("text") == "Generate 2 selected"
                assert app.selected_pads == {8, 12}
                app.change_bank(1)
                assert not app.selected_pads
                app.change_bank(0)
                report["checks"].append("Selected-pad workflow, persistent independent bank locks and MIDI toggle")
                app.audio_midi_settings()
                app.connection_window.geometry("740x720+10000+10000")
                app.update()
                assert app.connection_window.winfo_exists()
                app.connection_window.destroy()
                app.send_to_sp(auto_probe=False)
                app.transfer_window.geometry("880x800+10000+10000")
                app.update()
                assert app.transfer_window.winfo_exists()
                app.transfer_window.destroy()
                report["checks"].append("Audio/MIDI settings and SP destination review interface")
                app._show_ni_import(ni_packs, str(root / "synthetic-ni"))
                app.ni_import_window.geometry("820x720+10000+10000")
                app.update()
                assert app.ni_import_window.winfo_exists()
                app.ni_import_window.destroy()
                app.active_packs_and_crates()
                app.packs_window.geometry("800x760+10000+10000")
                app.update()
                assert app.packs_window.winfo_exists()
                app.packs_window.destroy()
                app.browse_library()
                app.library_window.geometry("1120x760+10000+10000")
                _wait_for_ui(app, lambda: app._library_browser["total"] == app.library_store.count_items())
                assert len(app._library_browser["items"]) <= 200
                app._library_browser["search"].set(ni_packs[0]["name"])
                _wait_for_ui(app, lambda: app._library_browser["total"] == 19)
                app.close_library_browser()
                assert app._library_browser is None
                report["checks"].append("Off-screen NI import, active-pack/crate dialog and asynchronous searchable library browser")
            finally:
                app.close_app()
            report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if previous is None:
            os.environ.pop("CRATEBUILDER_DATA_DIR", None)
        else:
            os.environ["CRATEBUILDER_DATA_DIR"] = previous
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report["ok"]
