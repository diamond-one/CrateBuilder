import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Build eight drum kits for the MPC1000, entirely locally.")
    parser.add_argument("--scan", help="Scan a sample folder without opening the desktop app")
    parser.add_argument("--session", help="Open a saved session (or write the result of --scan here)")
    parser.add_argument("--export", help="Export into a new subfolder of this directory")
    parser.add_argument("--name", default="BOOMCRATES", help="MPC program name")
    parser.add_argument("--seed", type=int, default=1000)
    parser.add_argument("--style", default="Balanced")
    parser.add_argument("--bpm", type=int, default=90)
    parser.add_argument("--strict", action="store_true", help="Leave missing categories empty instead of substituting")
    parser.add_argument("--cache", default=str(Path(__file__).resolve().parents[1] / ".cache"))
    parser.add_argument("--validate", help="Validate an exported PGM and all referenced WAVs")
    args = parser.parse_args()
    if args.validate:
        from .pgm import validate_program
        print(json.dumps(validate_program(args.validate), indent=2))
        return
    if args.scan:
        from .audio import scan_folder
        from .matching import Matcher, overview
        from .pgm import export_program
        session = scan_folder(args.scan, args.cache)
        session.seed, session.style, session.bpm = args.seed, args.style, args.bpm
        session.allow_substitutes = not args.strict
        Matcher(session).build()
        if args.session:
            Path(args.session).write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
        if args.export:
            folder, validation = export_program(session, args.export, args.name)
            print(json.dumps({"folder": str(folder), **validation}, indent=2))
        from collections import Counter
        print("Instrument counts:", dict(Counter(s.kind for s in session.samples)))
        print("Assigned / unique audio / substitutions:", overview(session))
        print("\n".join(session.scan_notes))
        return
    from .ui import App
    app = App(session_path=args.session)
    app.mainloop()


if __name__ == "__main__":
    main()
