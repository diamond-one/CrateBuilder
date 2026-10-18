import argparse
import json
import os
from pathlib import Path


def main():
    data_dir = Path(os.environ.get("CRATEBUILDER_DATA_DIR", str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CrateBuilder")))
    parser = argparse.ArgumentParser(description="Build local 8- or 16-pad drum kits for SP-404MKII and MPC1000.")
    parser.add_argument("--scan", help="Index a sample folder and build kits without opening the app")
    parser.add_argument("--session", help="Open a saved session, or save the result of --scan here")
    parser.add_argument("--update-only", action="store_true", help="Update the library and preserve saved pad choices; do not generate")
    parser.add_argument("--export", help="Parent folder for a new export")
    parser.add_argument("--export-kit", type=int, help="Export only this kit as WAVs (numbered from 1); requires --export")
    parser.add_argument("--name", default="PROGRAM", help="MPC program name")
    parser.add_argument("--kit-size", type=int, choices=(8, 16), help="Pads per kit; existing CLI default is 8")
    parser.add_argument("--target", choices=("sp404", "mpc"), help="Physical pad numbering/export target; existing CLI default is mpc")
    parser.add_argument("--surprise", type=float, default=.35, help="How adventurous suitable sound choices can be, from 0 to 1")
    parser.add_argument("--tag-folder", help="Save or replace a folder rule without renaming samples")
    parser.add_argument("--tags", default="", help="Comma-separated folder tags")
    parser.add_argument("--kind", choices=("auto", "foley", "perc", "kick", "snare", "rim", "clap", "hat", "open_hat", "cymbal", "fill", "exclude"), default="auto")
    parser.add_argument("--no-recursive", action="store_true", help="Apply a folder rule only to immediate files")
    parser.add_argument("--list-folder-tags", action="store_true", help="Print saved folder rules")
    parser.add_argument("--remove-folder-tag", help="Delete the rule for this folder")
    parser.add_argument("--catalogue", default=str(data_dir / "catalogue.sqlite3"), help="Local SQLite catalogue")
    parser.add_argument("--convert", help="Convert a complete audio folder to MPC-compatible WAV files")
    parser.add_argument("--convert-destination", help="Parent folder for the converted MPC WAV folder")
    parser.add_argument("--seed", type=int, default=1000)
    parser.add_argument("--style", default="Balanced")
    parser.add_argument("--bpm", type=int, default=90)
    parser.add_argument("--strict", action="store_true", help="Leave missing categories empty instead of substituting")
    parser.add_argument("--cache", default=str(data_dir / "cache"))
    parser.add_argument("--validate", help="Validate an exported MPC PGM and referenced WAVs")
    parser.add_argument("--self-check", metavar="REPORT_PATH", help="Verify the packaged runtime and write a diagnostic report")
    parser.add_argument("--list-connections", action="store_true", help="List MIDI inputs and audio outputs without opening devices")
    parser.add_argument("--sp-status", action="store_true", help="Read the connected SP project and pad occupancy without changing it")
    parser.add_argument("--sparky-cli", help="Path to the Sparky CLI executable used with --sp-status")
    parser.add_argument("--sp-port", help="Specific SP serial port, such as COM3")
    args = parser.parse_args()
    if args.self_check:
        from .diagnostics import self_check
        raise SystemExit(0 if self_check(args.self_check) else 1)
    if args.list_connections:
        from .preview import list_audio_outputs
        from .live_midi import list_input_ports
        print(json.dumps({"audioOutputs": list_audio_outputs(), "midiInputs": list_input_ports()}, indent=2))
        return
    if args.sp_status:
        from .sp_transfer import discover_cli, probe
        cli = discover_cli(args.sparky_cli)
        if not cli:
            parser.error("Sparky CLI was not found. Supply --sparky-cli PATH.")
        print(json.dumps(probe(cli, port=args.sp_port), indent=2))
        return
    if not 0 <= args.surprise <= 1:
        parser.error("--surprise must be between 0 and 1")
    if args.export_kit is not None and not args.export:
        parser.error("--export-kit requires --export")
    if args.update_only and not args.scan:
        parser.error("--update-only requires --scan")
    if args.convert:
        if not args.convert_destination:
            parser.error("--convert requires --convert-destination")
        from .converter import convert_folder
        folder, report = convert_folder(args.convert, args.convert_destination)
        print(json.dumps({"folder": str(folder), **report}, indent=2))
        return
    if args.validate:
        from .pgm import validate_program
        print(json.dumps(validate_program(args.validate), indent=2))
        return
    catalogue = None
    if args.scan or args.tag_folder or args.remove_folder_tag or args.list_folder_tags:
        from .catalogue import Catalogue
        catalogue = Catalogue(args.catalogue)
    if args.tag_folder:
        tags = [tag.strip().lower() for tag in args.tags.split(",") if tag.strip()]
        catalogue.set_folder_rule(args.tag_folder, tags, kind=None if args.kind == "auto" else args.kind, recursive=not args.no_recursive)
        print("Folder rule saved. No source files were renamed or changed.")
    if args.remove_folder_tag:
        catalogue.delete_folder_rule(args.remove_folder_tag)
        print("Folder rule removed.")
    if args.list_folder_tags:
        print(json.dumps(catalogue.list_folder_rules(), indent=2))
    if args.scan:
        from collections import Counter
        from .audio import scan_folder, merge_library_session, project_dict
        from .matching import Matcher, overview
        from .model import Session, kit_count
        session = scan_folder(args.scan, args.cache, catalogue=catalogue)
        if args.update_only and args.session and Path(args.session).is_file():
            previous = Session.from_dict(json.loads(Path(args.session).read_text(encoding="utf-8")))
            session = merge_library_session(previous, session)
        else:
            session.seed, session.style, session.bpm = args.seed, args.style, args.bpm
            session.allow_substitutes = not args.strict
            session.kit_size, session.target, session.surprise = args.kit_size or 8, args.target or "mpc", args.surprise
        if not args.update_only:
            Matcher(session).build()
        if args.session:
            destination = Path(args.session)
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.write_text(json.dumps(project_dict(session), indent=2), encoding="utf-8")
            temporary.replace(destination)
        if args.export:
            if args.export_kit is not None or session.target == "sp404":
                from .kit_export import export_kit
                kits = [args.export_kit - 1] if args.export_kit is not None else range(kit_count(session))
                for kit in kits:
                    if not 0 <= kit < kit_count(session):
                        parser.error(f"--export-kit must be between 1 and {kit_count(session)}")
                    folder, info = export_kit(session, kit, args.export)
                    print(json.dumps({"folder": str(folder), **info}, indent=2))
            else:
                from .pgm import export_program
                folder, validation = export_program(session, args.export, args.name)
                print(json.dumps({"folder": str(folder), **validation}, indent=2))
        print("Instrument counts:", dict(Counter(s.kind for s in session.samples)))
        print("Assigned / unique audio / substitutions:", overview(session))
        print("\n".join(session.scan_notes))
        return
    if args.tag_folder or args.remove_folder_tag or args.list_folder_tags:
        return
    from .ui import App
    app = App(session_path=args.session, catalogue_path=args.catalogue,
              default_kit_size=args.kit_size or 16, default_target=args.target or "sp404")
    app.mainloop()


if __name__ == "__main__":
    main()
