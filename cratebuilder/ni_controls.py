"""Desktop orchestration for NI archives, active packs, crates and personal marks."""
import copy
from dataclasses import replace
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

import customtkinter as ctk

from .audio import scan_folder, merge_library_session
from .catalogue import canonical_path
from .library import apply_library_marks, export_collection
from .model import Assignment, Session
from .ni import discover_expansions, iter_expansion_items, metadata_for_path, read_media_sample, load_media_audio

from .desk_theme import TEXT, MUTED, GREEN, PANEL, CONTROL, CONTROL_HOVER as HOVER


def item_for_sample(sample):
    f = sample.features
    native = f.get("provider") == "Native Instruments"
    origin = metadata_for_path(sample.path) or {}
    return {**origin, "path": sample.path, "name": Path(sample.path).name,
            "provider": f.get("provider", "Your samples"),
            "pack": f.get("pack") or sample.source or "Your samples",
            "source": f.get("ni_source") if native else "__user__",
            "category": f.get("category") or ("Drums" if sample.kind != "unknown" else "One Shots"),
            "kind": sample.kind, "subtype": f.get("subtype", sample.kind),
            "bpm": sample.bpm if sample.bpm is not None else origin.get("bpm"),
            "key": f.get("key") or origin.get("key"), "size": sample.size, "mtime_ns": sample.mtime_ns,
            "fingerprint": sample.fingerprint}


def _snapshot(session):
    if session is None:
        return None
    result = copy.copy(session)
    result.pads = copy.deepcopy(session.pads)
    for name in ("sources", "history", "scan_notes", "locked_banks", "generation_sources"):
        value = getattr(session, name)
        setattr(result, name, None if value is None else list(value))
    return result


class NILibraryMixin:
    def register_library_samples(self, session):
        self.library_store.upsert_items(item_for_sample(sample) for sample in session.samples)
        return apply_library_marks(session, self.library_store)

    def _new_library_session(self):
        return Session(self.path_var.get() or "C:/Users/Public/Documents", [],
                       kit_size=16 if self.kit_size_var.get() == "16 pads" else 8,
                       target="mpc" if self.target_var.get() == "MPC1000" else "sp404",
                       style=self.style_var.get(), surprise=self.surprise_var.get())

    def _load_active_packs(self, snapshot, source_ids, force=False):
        """Analyse only active NI drums; archive records stay metadata-only."""
        analysed_roots = {canonical_path(root)[1] for root in snapshot.sources}
        known = {s.features.get("ni_source") for s in snapshot.samples if s.features.get("available", True)
                 and s.features.get("category") == "Drums"
                 and s.features.get("ni_source")
                 and canonical_path(Path(s.features["ni_source"][3:]) / "Drums")[1] in analysed_roots}
        notes = []
        for number, source in enumerate(source_ids):
            if not source.startswith("ni:") or (source in known and not force):
                continue
            if self.cancel_event.is_set():
                raise InterruptedError("Scan cancelled")
            drums = Path(source[3:]) / "Drums"
            try:
                fresh = scan_folder(drums, self.data_dir / "cache", self.progress,
                                    self.cancel_event, catalogue=self.catalogue)
                snapshot = merge_library_session(snapshot, fresh)
            except (OSError, ValueError) as exc:
                notes.append(f"Could not load {drums.parent.parent.name}: {exc}")
                self.catalogue.record_scan_status(drums, "unavailable", str(exc), root=drums)
                snapshot.samples = [replace(sample, features={**sample.features, "available": False})
                                    if sample.features.get("ni_source") == source else sample
                                    for sample in snapshot.samples]
                snapshot.library_revision += 1
        snapshot.scan_notes.extend(notes)
        return self.register_library_samples(snapshot), notes

    def _accept_library_session(self, session, message):
        self.session = session
        self.path_var.set(session.root)
        self.refresh()
        self.workspace_tabs.set("Kit")
        self.autosave()
        self.set_status(message)
        if hasattr(self, "refresh_library_browser"):
            self.refresh_library_browser()

    def import_ni_expansions(self, search_root=None):
        if self.busy:
            return
        if getattr(self, "ni_import_window", None) and self.ni_import_window.winfo_exists():
            self.ni_import_window.lift()
            return
        root = search_root or "C:/Users/Public/Documents"
        self.set_status("Finding installed NI expansion folders…")
        def ready(packs):
            if not packs:
                folder = filedialog.askdirectory(title="Choose the folder containing NI expansion libraries", parent=self)
                if folder:
                    self.import_ni_expansions(folder)
                else:
                    self.set_status("No NI expansion Samples/Drums folders found.")
                return
            self._show_ni_import(packs, root)
        self.run_job(lambda: discover_expansions(root), ready, close_safe=False)

    def _show_ni_import(self, packs, root):
        window = self.ni_import_window = ctk.CTkToplevel(self)
        window.title("Import NI expansions")
        window.geometry("820x720")
        window.minsize(720, 590)
        window.transient(self)
        window.grab_set()
        window.bind("<Escape>", lambda _: window.destroy())
        self.label(window, "Your NI collection, without the overload", 21, TEXT, True).pack(anchor="w", padx=24, pady=(20, 8))
        self.label(window, f"Found {len(packs)} expansions in {root}. Catalogue all of them; choose a few active packs for drum generation. Originals stay installed.",
                   13, MUTED, wraplength=650, justify="left").pack(anchor="w", padx=24, pady=(0, 12))
        search = tk.StringVar()
        ctk.CTkEntry(window, textvariable=search, placeholder_text="Find a pack…", height=34).pack(fill="x", padx=24, pady=(0, 10))
        bottom = ctk.CTkFrame(window, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=24, pady=20)
        include_user = tk.BooleanVar(value=self.session is None or self.session.generation_sources is None or "__user__" in self.session.generation_sources)
        ctk.CTkCheckBox(window, text="Include my other samples in generation", variable=include_user,
                       font=ctk.CTkFont(size=13), fg_color=GREEN).pack(side="bottom", anchor="w", padx=24, pady=(10, 0))
        count_label = self.label(window, "", 12, MUTED)
        count_label.pack(side="bottom", anchor="w", padx=24)
        rows = ctk.CTkScrollableFrame(window, fg_color=PANEL)
        rows.pack(fill="both", expand=True, padx=24)
        active = self.session.generation_sources if self.session else None
        preferred = {"Aquarius Earth", "Faded Reels", "Crate Cuts"}
        defaults = {p["id"] for p in packs if p["name"] in preferred}
        if not defaults:
            defaults = {p["id"] for p in packs[:3]}
        variables, widgets = {}, {}
        def update_count():
            selected = sum(v.get() for v in variables.values())
            count_label.configure(text=f"{selected} active packs · all {len(packs)} packs will be searchable. Start with 3–5.")
        for pack in packs:
            value = tk.BooleanVar(value=pack["id"] in (active if active is not None else defaults))
            check = ctk.CTkCheckBox(rows, text=pack["name"], variable=value, command=update_count,
                                   font=ctk.CTkFont(size=13), height=32, fg_color=GREEN)
            check.pack(anchor="w", fill="x", padx=12, pady=4)
            variables[pack["id"]], widgets[pack["id"]] = value, check
        def filter_rows(*_):
            query = search.get().strip().casefold()
            for pack in packs:
                widgets[pack["id"]].pack_forget()
                if query in pack["name"].casefold():
                    widgets[pack["id"]].pack(anchor="w", fill="x", padx=12, pady=4)
        search.trace_add("write", filter_rows)
        update_count()
        def begin():
            chosen = [p["id"] for p in packs if variables[p["id"]].get()]
            active_sources = [*(["__user__"] if include_user.get() else []), *chosen]
            snapshot = _snapshot(self.session) or self._new_library_session()
            if self.session is None and chosen:
                snapshot.root = str(Path(chosen[0][3:]) / "Drums")
                snapshot.sources = [snapshot.root]
            snapshot.generation_sources = active_sources
            window.destroy()
            def work():
                def items():
                    for index, pack in enumerate(packs):
                        if self.cancel_event.is_set():
                            raise InterruptedError("Scan cancelled")
                        self.progress(f"Cataloguing {pack['name']} ({index + 1}/{len(packs)})…", index / len(packs))
                        yield from iter_expansion_items(pack, cancel=self.cancel_event)
                count = self.library_store.upsert_items(items())
                result, notes = self._load_active_packs(snapshot, chosen, force=True)
                return result, count, notes
            def done(result):
                session, count, notes = result
                message = f"Catalogued {count:,} files · {len(chosen)} active NI packs. Generate when ready."
                if notes:
                    message += f" {len(notes)} packs need attention; see scan notes."
                self._accept_library_session(session, message)
            self.run_job(work, done, cancellable=True)
        self.button(bottom, "Cancel", window.destroy, width=110).pack(side="left")
        self.button(bottom, "Catalogue & load active packs", begin, primary=True, width=260).pack(side="right")

    def active_packs_and_crates(self):
        if self.busy:
            return
        if getattr(self, "packs_window", None) and self.packs_window.winfo_exists():
            self.packs_window.lift()
            return
        sources = self.library_store.list_sources()
        if not sources:
            self.import_ni_expansions()
            return
        window = self.packs_window = ctk.CTkToplevel(self)
        window.title("Active packs & working crates")
        window.geometry("800x760")
        window.minsize(700, 650)
        window.transient(self)
        window.grab_set()
        window.bind("<Escape>", lambda _: window.destroy())
        self.label(window, "Choose the sounds you work with", 21, TEXT, True).pack(anchor="w", padx=24, pady=(20, 6))
        self.label(window, "Active packs shape new generation. Existing pads and locks stay as they are.",
                   13, MUTED).pack(anchor="w", padx=24, pady=(0, 12))
        controls = ctk.CTkFrame(window, fg_color="transparent")
        controls.pack(fill="x", padx=24)
        mode = tk.StringVar(value="One pack per kit" if self.session and self.session.source_mode == "same_pack" else "Mix active packs")
        favourite = tk.BooleanVar(value=bool(self.session and self.session.favourites_only))
        unused = tk.BooleanVar(value=bool(self.session and self.session.unused_only))
        ctk.CTkOptionMenu(controls, variable=mode, values=["Mix active packs", "One pack per kit"],
                          fg_color=CONTROL, button_color=HOVER).pack(fill="x", pady=(0, 10))
        ctk.CTkCheckBox(controls, text="Favourites only", variable=favourite, fg_color=GREEN).pack(anchor="w", pady=5)
        ctk.CTkCheckBox(controls, text="Not used recently · 7 days", variable=unused, fg_color=GREEN).pack(anchor="w", pady=(5, 12))
        actions = ctk.CTkFrame(window, fg_color="transparent")
        actions.pack(side="bottom", fill="x", padx=24, pady=(10, 20))
        crate_area = ctk.CTkFrame(window, fg_color=PANEL)
        crate_area.pack(side="bottom", fill="x", padx=24, pady=8)
        self.label(crate_area, "WORKING CRATES · saved pack/filter selections", 12, GREEN, True).pack(anchor="w", padx=12, pady=(10, 8))
        crate_name, crate_choice = tk.StringVar(), tk.StringVar(value="Choose a saved crate")
        crate_menu = ctk.CTkOptionMenu(crate_area, variable=crate_choice, values=["No saved crates"],
                                       fg_color=CONTROL, button_color=HOVER)
        crate_menu.pack(fill="x", padx=12)
        ctk.CTkEntry(crate_area, textvariable=crate_name, placeholder_text="Name this crate, e.g. Soulful drums").pack(fill="x", padx=12, pady=8)
        crate_buttons = ctk.CTkFrame(crate_area, fg_color="transparent")
        crate_buttons.pack(fill="x", padx=12, pady=(0, 12))
        notice = self.label(window, "", 12, MUTED, wraplength=640, justify="left")
        notice.pack(side="bottom", anchor="w", padx=24)
        rows = ctk.CTkScrollableFrame(window, fg_color=PANEL)
        rows.pack(fill="both", expand=True, padx=24)
        active = self.session.generation_sources if self.session else None
        variables = {}
        for source in sources:
            variable = tk.BooleanVar(value=active is None or source["id"] in active)
            title = "Your other samples" if source["id"] == "__user__" else source["name"]
            ctk.CTkCheckBox(rows, text=f"{title} · {source['count']:,} indexed", variable=variable,
                           fg_color=GREEN, height=32, font=ctk.CTkFont(size=13)).pack(anchor="w", padx=12, pady=4)
            variables[source["id"]] = variable
        def settings():
            return {"generation_sources": [source for source, var in variables.items() if var.get()],
                    "source_mode": "same_pack" if mode.get() == "One pack per kit" else "mix",
                    "favourites_only": favourite.get(), "unused_only": unused.get()}
        saved = {}
        def refresh_crates():
            saved.clear()
            saved.update({crate["name"]: crate["settings"] for crate in self.library_store.list_crates()})
            crate_menu.configure(values=list(saved) or ["No saved crates"])
        def load(name):
            if name not in saved:
                return
            value = saved[name]
            for source, var in variables.items():
                var.set(source in value.get("generation_sources", []))
            mode.set("One pack per kit" if value.get("source_mode") == "same_pack" else "Mix active packs")
            favourite.set(value.get("favourites_only", False))
            unused.set(value.get("unused_only", False))
            crate_name.set(name)
            notice.configure(text=f"Loaded {name}. Apply to use these choices.")
        crate_menu.configure(command=load)
        def save():
            try:
                self.library_store.save_crate(crate_name.get(), settings())
                refresh_crates()
                crate_choice.set(crate_name.get().strip())
                notice.configure(text="Working crate saved. It references sources; it does not copy audio.")
            except (ValueError, OSError) as exc:
                notice.configure(text=str(exc))
        def remove():
            name = crate_choice.get()
            if name in saved:
                self.library_store.delete_crate(name)
                refresh_crates()
                crate_choice.set("Choose a saved crate")
                notice.configure(text="Saved crate removed. Its audio is still available in the library.")
        self.button(crate_buttons, "Save crate", save, width=130).pack(side="left")
        self.button(crate_buttons, "Remove saved crate", remove, width=170).pack(side="right")
        refresh_crates()
        def apply():
            value = settings()
            snapshot = _snapshot(self.session) or self._new_library_session()
            for key, setting in value.items():
                setattr(snapshot, key, setting)
            window.destroy()
            def work():
                return self._load_active_packs(snapshot, value["generation_sources"])
            def done(result):
                session, notes = result
                message = "Active sources updated. Generate to explore this working crate."
                if notes:
                    message += " Some sources need attention; see scan notes."
                self._accept_library_session(session, message)
            self.run_job(work, done, cancellable=True)
        self.button(actions, "Cancel", window.destroy, width=110).pack(side="left")
        self.button(actions, "Apply active sources", apply, primary=True, width=210).pack(side="right")

    def library_marks_changed(self):
        if self.session:
            self.session = apply_library_marks(self.session, self.library_store)
            self.refresh()
            self.autosave()
        if hasattr(self, "refresh_library_browser"):
            self.refresh_library_browser()

    def mark_selected_favourite(self):
        if self.busy or not self.session:
            return
        sample = self.session.by_id.get(self.session.pads[self.selected].sample_id)
        if sample:
            self.library_store.set_mark(sample.path, favourite=not sample.features.get("favourite", False))
            self.library_marks_changed()

    def hide_selected_sound(self):
        if self.busy or not self.session:
            return
        sample = self.session.by_id.get(self.session.pads[self.selected].sample_id)
        if sample:
            self.library_store.set_mark(sample.path, hidden=not sample.features.get("hidden", False))
            self.library_marks_changed()
            self.set_status("Generation visibility updated. The current pad still plays; the file stays in place.")

    def preview_library_item(self, item):
        if self.busy:
            return
        if Path(item["path"]).suffix.lower() == ".rx2":
            self.set_status("REX files can be copied from the browser; preview uses WAV, AIFF or FLAC.")
            return
        token = self.preview_serial + 1
        def work():
            import sounddevice as sd
            sample = read_media_sample(item, catalogue=self.catalogue)
            data, rate = self.player._output_data(load_media_audio(sample, seconds=12) * .65)
            if self.preview_serial == token and not self._closed:
                sd.play(data, rate, device=self.player.device)
        self.audio_job(work)
        self.set_status("Previewing up to 12 seconds. Stop or Escape ends playback.")

    def use_library_item(self, item):
        if self.busy:
            return
        if self.session and self.bank in self.session.locked_banks:
            self.set_status("Unlock this bank before changing its sounds.")
            return
        if item.get("category") in ("Loops", "Instruments"):
            self.set_status("Choose a drum or one-shot for a pad. Loops and instruments remain separate.")
            return
        snapshot = _snapshot(self.session) or self._new_library_session()
        index = self.selected
        def work():
            sample = read_media_sample(item, catalogue=self.catalogue)
            if sample.features.get("preview_only") or sample.duration > 15:
                raise ValueError("This file is for preview only. Choose a one-shot up to 15 seconds.")
            root = item["source"][3:] if str(item.get("source", "")).startswith("ni:") else str(Path(sample.path).parent)
            merged = merge_library_session(snapshot, Session(root, [sample], sources=[root]))
            merged.pads[index] = Assignment(sample.id, "Chosen from the library and locked", locked=True)
            self.library_store.record_used([sample.path])
            return self.register_library_samples(merged)
        def done(session):
            self._accept_library_session(session, "Library sound applied and locked to the selected pad.")
        self.run_job(work, done)

    def export_library_items(self, items):
        if self.busy or not items:
            return
        parent = getattr(self, "library_window", None) or self
        destination = filedialog.askdirectory(title="Save organised copies of the selected sounds", parent=parent)
        if not destination:
            return
        name = simpledialog.askstring("Collection name", "Name this portable collection:", initialvalue="NI Favourites", parent=parent)
        if not name:
            return
        selected = [dict(item) for item in items]
        def work():
            return export_collection(selected, destination, name, progress=self.progress, cancel=self.cancel_event)
        def done(result):
            output = result[0] if isinstance(result, tuple) else result
            self.set_status(f"Organised copies saved to {output}. Originals stay in place.")
            self.open_output_folder(output)
        self.run_job(work, done, cancellable=True, close_safe=False)
