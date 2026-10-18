"""Paged, metadata-only library browsing with background SQLite queries.

The application owns audio loading and exports. Opening or searching this
browser never opens sample audio or adds it to the active working crate.
"""
import math
from pathlib import Path
import threading
import tkinter as tk

import customtkinter as ctk


from .desk_theme import BG, PANEL, INSET, TEXT, MUTED, GREEN, AMBER, CONTROL, CONTROL_HOVER, BORDER
PAGE_SIZE = 200
CATEGORIES = ("All", "Drums", "One Shots", "Loops", "Instruments")
AUDIO_FORMATS = frozenset({".wav", ".wave", ".aif", ".aiff", ".flac"})
FILTER_KEYS = ("search", "source", "category", "favourites_only", "hidden_only", "unused_only")


def query_library_page(store, filters, offset=0, page_size=PAGE_SIZE):
    """Fetch one bounded page; clamp an invalidated final page after removals."""
    page_size = max(1, min(PAGE_SIZE, int(page_size)))
    filters = {key: value for key, value in filters.items() if key in FILTER_KEYS}
    total = max(0, int(store.count_items(**filters)))
    last_offset = ((total - 1) // page_size) * page_size if total else 0
    offset = min(max(0, int(offset) // page_size * page_size), last_offset)
    items = list(store.query_items(**filters, limit=page_size, offset=offset))[:page_size]
    return {"items": items, "total": total, "offset": offset,
            "page_size": page_size, "sources": list(store.list_sources())}


def library_item_actions(item):
    """Expose safe actions without opening, decoding or changing the file."""
    if not item:
        return {"preview": False, "use": False, "reason": "Select one sound to audition or use on a pad."}
    extension = Path(str(item.get("path", ""))).suffix.casefold()
    category = str(item.get("category", "")).casefold().replace(" ", "")
    preview = extension in AUDIO_FORMATS
    if extension == ".rx2":
        reason = "REX2 files can be browsed and exported. Convert to WAV before using one on a pad."
    elif category in {"loops", "instruments"}:
        reason = "Loops and instruments are available to explore and export. Choose a drum or one-shot for a kit pad."
    elif not preview:
        reason = "This file can be browsed and exported. Pad audition supports WAV, AIFF and FLAC audio."
    elif category not in {"drums", "oneshots"}:
        reason = "Choose a sound in Drums or One Shots to use it on a kit pad."
    else:
        return {"preview": True, "use": True, "reason": "Ready to use on the selected pad."}
    return {"preview": preview, "use": False, "reason": reason}


def selected_library_items(items, indices):
    """Return only the explicitly selected visible rows, in their display order."""
    selected = sorted({index for index in indices if isinstance(index, int) and not isinstance(index, bool)
                       and 0 <= index < len(items)})
    return [dict(items[index]) for index in selected]


def _display_name(item):
    return str(item.get("name") or Path(str(item.get("path", ""))).name or "Unnamed sound")


def _row_text(item):
    flags = "Favourite · " if item.get("favourite") else ""
    if item.get("hidden"):
        flags += "Hidden · "
    kind = str(item.get("subtype") or item.get("kind") or item.get("category") or "Sound").replace("_", " ")
    pack = str(item.get("pack") or item.get("provider") or "Your samples")
    return f"{flags}{_display_name(item)[:72]}   ·   {kind[:24]}   ·   {pack[:36]}"


class LibraryBrowserMixin:
    def _library_worker_store(self):
        """Each worker owns its store and its SQLite connections."""
        return type(self.library_store)(self.data_dir / "library.sqlite3")

    def browse_library(self):
        current = getattr(self, "_library_browser", None)
        if current and current["active"] and current["window"].winfo_exists():
            current["window"].focus()
            return
        window = self.library_window = ctk.CTkToplevel(self)
        window.title("Sample library")
        window.geometry("1120x760")
        window.minsize(900, 650)
        window.configure(fg_color=BG)
        window.transient(self)
        window.grab_set()
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(3, weight=1)
        state = self._library_browser = {
            "active": True, "window": window, "token": object(), "serial": 0,
            "after": None, "offset": 0, "total": 0, "items": [], "source_map": {},
            "source_id": None, "updating_filters": False, "marking": False,
        }
        if not hasattr(self, "_library_query_lock"):
            self._library_query_lock = threading.Lock()
        head = ctk.CTkFrame(window, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 12))
        head.grid_columnconfigure(0, weight=1)
        self.label(head, "SAMPLE LIBRARY", 23, GREEN, True).grid(row=0, column=0, sticky="w")
        self.label(head, "Find a sound, audition it, or select a few to export.", 12, MUTED).grid(row=1, column=0, sticky="w")
        self.button(head, "Import NI expansions…", self.import_ni_expansions, width=190).grid(row=0, column=1, rowspan=2, padx=(10, 0))
        filters = ctk.CTkFrame(window, fg_color=PANEL)
        filters.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))
        filters.grid_columnconfigure(0, weight=3)
        filters.grid_columnconfigure(1, weight=2)
        filters.grid_columnconfigure(2, weight=1)
        state["search"] = tk.StringVar(value="")
        state["source"] = tk.StringVar(value="All packs")
        state["category"] = tk.StringVar(value="All")
        state["favourites"] = tk.BooleanVar(value=False)
        state["hidden"] = tk.BooleanVar(value=False)
        state["unused"] = tk.BooleanVar(value=False)
        for column, text in enumerate(("Search sounds", "Pack / source", "Category")):
            self.label(filters, text, 11, MUTED).grid(row=0, column=column, sticky="w", padx=12, pady=(10, 4))
        search_entry = ctk.CTkEntry(filters, textvariable=state["search"], placeholder_text="Search names or packs…")
        search_entry.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        state["source_menu"] = ctk.CTkOptionMenu(filters, variable=state["source"], values=["All packs"], dynamic_resizing=False,
            fg_color=CONTROL, button_color=CONTROL_HOVER)
        state["source_menu"].grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(0, 10))
        ctk.CTkOptionMenu(filters, variable=state["category"], values=list(CATEGORIES), dynamic_resizing=False,
            fg_color=CONTROL, button_color=CONTROL_HOVER).grid(row=1, column=2, sticky="ew", padx=(0, 12), pady=(0, 10))
        flags = ctk.CTkFrame(window, fg_color="transparent")
        flags.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 10))
        for text, key in (("Favourites only", "favourites"), ("Hidden only", "hidden"), ("Not used recently", "unused")):
            ctk.CTkCheckBox(flags, text=text, variable=state[key], checkbox_width=18, checkbox_height=18,
                            font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 22))
        self.button(flags, "Clear filters", self._clear_library_filters, width=105).pack(side="right")

        body = ctk.CTkFrame(window, fg_color="transparent")
        body.grid(row=3, column=0, sticky="nsew", padx=20)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        result_frame = ctk.CTkFrame(body, fg_color=INSET)
        result_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        result_frame.grid_columnconfigure(0, weight=1)
        result_frame.grid_rowconfigure(0, weight=1)
        listing = state["listing"] = tk.Listbox(result_frame, selectmode=tk.EXTENDED, exportselection=False,
            bg=INSET, fg=TEXT, selectbackground=GREEN, selectforeground=BG, activestyle="none",
            highlightthickness=1, highlightbackground=CONTROL_HOVER, highlightcolor=GREEN, relief="flat",
            font=("Segoe UI", 11), borderwidth=0, height=16)
        listing.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        scrollbar = ctk.CTkScrollbar(result_frame, command=listing.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", pady=8)
        listing.configure(yscrollcommand=scrollbar.set)
        listing.bind("<<ListboxSelect>>", lambda _: self.sync_library_controls())
        listing.bind("<Double-1>", lambda _: self._library_preview())
        listing.bind("<space>", lambda _: self._library_preview() or "break")
        listing.bind("<Return>", lambda _: self._library_preview() or "break")
        listing.bind("<Control-Return>", lambda _: self._library_use() or "break")
        detail = ctk.CTkFrame(body, width=305, fg_color=PANEL)
        detail.grid(row=0, column=1, sticky="nsew")
        detail.grid_columnconfigure(0, weight=1)
        detail.grid_rowconfigure(0, weight=1)
        detail.grid_propagate(False)
        information = ctk.CTkScrollableFrame(detail, width=285, fg_color="transparent")
        information.grid(row=0, column=0, sticky="nsew", padx=0, pady=(2, 0))
        state["detail_title"] = self.label(information, "Choose a sound", 19, TEXT, True, wraplength=260, justify="left")
        state["detail_title"].pack(anchor="w", padx=12, pady=(12, 8))
        state["detail_metadata"] = self.label(information, "", 12, MUTED, wraplength=260, justify="left")
        state["detail_metadata"].pack(anchor="w", padx=12, pady=(0, 8))
        state["detail_path"] = self.label(information, "", 11, MUTED, wraplength=260, justify="left")
        state["detail_path"].pack(anchor="w", padx=12, pady=(0, 10))
        state["detail_reason"] = self.label(detail, "Select one sound to audition or use on a pad.", 12, AMBER, wraplength=260, justify="left")
        state["detail_reason"].grid(row=1, column=0, sticky="w", padx=12, pady=(6, 8))
        actions_frame = ctk.CTkFrame(detail, fg_color="transparent")
        actions_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        actions_frame.grid_columnconfigure((0, 1), weight=1)
        for key, text, command, primary, row, column, span in (
                ("preview_button", "Audition", self._library_preview, False, 0, 0, 1),
                ("use_button", "Use on pad", self._library_use, True, 0, 1, 1),
                ("favourite_button", "Favourite", lambda: self._library_mark("favourite"), False, 1, 0, 1),
                ("hide_button", "Hide", lambda: self._library_mark("hidden"), False, 1, 1, 1),
                ("export_button", "Export selected…", self._library_export, False, 2, 0, 2)):
            state[key] = self.button(actions_frame, text, command, primary=primary, width=120)
            state[key].grid(row=row, column=column, columnspan=span, sticky="ew",
                            padx=(0, 6 if column == 0 and span == 1 else 0), pady=(0, 6))
        self.label(actions_frame, "Favourites and hiding never change the original audio.",
                   11, MUTED, wraplength=260, justify="left").grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))

        footer = ctk.CTkFrame(window, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=20, pady=(10, 8))
        footer.grid_columnconfigure(0, weight=1)
        state["page_label"] = self.label(footer, "Loading library…", 12, MUTED)
        state["page_label"].grid(row=0, column=0, sticky="w")
        state["previous_button"] = self.button(footer, "Previous", lambda: self._library_page(-1), width=95)
        state["previous_button"].grid(row=0, column=1, padx=(0, 8))
        state["next_button"] = self.button(footer, "Next", lambda: self._library_page(1), width=95)
        state["next_button"].grid(row=0, column=2, padx=(0, 8))
        self.button(footer, "Refresh", lambda: self.refresh_library_browser(), width=95).grid(row=0, column=3)
        state["status"] = self.label(window, "Searching…", 12, MUTED, anchor="w", justify="left", wraplength=1060)
        state["status"].grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 16))
        for key in ("search", "source", "category", "favourites", "hidden", "unused"):
            state[key].trace_add("write", lambda *_: self._library_filters_changed())
        window.protocol("WM_DELETE_WINDOW", self.close_library_browser)
        window.bind("<Escape>", lambda _: self.close_library_browser())
        window.bind("<Destroy>", lambda event: self._library_destroyed(state) if event.widget is window else None)
        self.sync_library_controls()
        self._schedule_library_query(delay=0, reset_page=True)
        search_entry.focus_set()

    def _library_state(self):
        state = getattr(self, "_library_browser", None)
        return state if state and state["active"] else None

    def _library_destroyed(self, state):
        state["active"] = False
        state["serial"] += 1
        if state["after"] is not None:
            try:
                self.after_cancel(state["after"])
            except Exception:
                pass
            state["after"] = None
        if getattr(self, "_library_browser", None) is state:
            self._library_browser = None
            self.library_window = None

    def close_library_browser(self):
        state = self._library_state()
        if state:
            self._library_destroyed(state)
            state["window"].destroy()

    def _library_filters_changed(self):
        state = self._library_state()
        if state and not state["updating_filters"]:
            state["source_id"] = state["source_map"].get(state["source"].get())
            self._schedule_library_query(reset_page=True)

    def _clear_library_filters(self):
        state = self._library_state()
        if not state:
            return
        state["updating_filters"] = True
        try:
            state["search"].set("")
            state["source"].set("All packs")
            state["category"].set("All")
            for key in ("favourites", "hidden", "unused"):
                state[key].set(False)
            state["source_id"] = None
        finally:
            state["updating_filters"] = False
        self._schedule_library_query(delay=0, reset_page=True)

    def _schedule_library_query(self, delay=250, reset_page=False):
        state = self._library_state()
        if not state:
            return
        if reset_page:
            state["offset"] = 0
        state["serial"] += 1
        if state["after"] is not None:
            self.after_cancel(state["after"])
        state["status"].configure(text="Searching the library…")
        serial = state["serial"]
        state["after"] = self.after(delay, lambda: self._start_library_query(state, serial))

    def refresh_library_browser(self, reset_page=False):
        self._schedule_library_query(delay=0, reset_page=reset_page)

    def _start_library_query(self, state, serial):
        if not state["active"] or state["serial"] != serial:
            return
        state["after"] = None
        filters = {"search": state["search"].get().strip(), "source": state["source_id"],
                   "category": None if state["category"].get() == "All" else state["category"].get(),
                   "favourites_only": state["favourites"].get(), "hidden_only": state["hidden"].get(),
                   "unused_only": state["unused"].get()}
        offset, token = state["offset"], state["token"]
        def worker():
            with self._library_query_lock:
                if not state["active"] or state["serial"] != serial:
                    return
                try:
                    store = self._library_worker_store()
                    result = query_library_page(store, filters, offset)
                    self.events.put(("library_results", {"token": token, "serial": serial, "result": result}))
                except Exception as exc:
                    self.events.put(("library_query_error", {"token": token, "serial": serial, "error": str(exc)}))
        threading.Thread(target=worker, daemon=True).start()

    def _library_page(self, direction):
        state = self._library_state()
        if not state:
            return
        offset = max(0, state["offset"] + direction * PAGE_SIZE)
        if offset == state["offset"] or offset >= state["total"]:
            return
        state["offset"] = offset
        self._schedule_library_query(delay=0)

    def _library_selection(self):
        state = self._library_state()
        return selected_library_items(state["items"], state["listing"].curselection()) if state else []

    def sync_library_controls(self):
        state = self._library_state()
        if not state:
            return
        selected = self._library_selection()
        item = selected[0] if len(selected) == 1 else None
        actions = library_item_actions(item)
        busy = bool(getattr(self, "busy", False))
        if item:
            state["detail_title"].configure(text=_display_name(item))
            details = [str(item.get("category") or "Sound"), str(item.get("pack") or item.get("provider") or "Your samples")]
            kind = str(item.get("subtype") or item.get("kind") or "").replace("_", " ")
            if kind:
                details.append(kind.title())
            if item.get("bpm"):
                details.append(f"{item['bpm']} BPM")
            if item.get("key"):
                details.append(f"Key: {item['key']}")
            size = item.get("size")
            if isinstance(size, (int, float)) and size >= 0:
                details.append(f"{size / 1024 ** 2:.2f} MB")
            details.append(f"Used {item.get('used_count', 0)} time(s)")
            state["detail_metadata"].configure(text="\n".join(details))
            state["detail_path"].configure(text=str(item.get("path", "")))
            state["detail_reason"].configure(text=actions["reason"])
        else:
            state["detail_title"].configure(text=f"{len(selected)} sounds selected" if selected else "Choose a sound")
            state["detail_metadata"].configure(text="Favourite, hide or export these selected rows." if selected else "")
            state["detail_path"].configure(text="")
            state["detail_reason"].configure(text="Select one sound to audition or use on a pad." if selected else
                "Search by name, choose a pack, or import NI expansions to add sounds.")
        for key, enabled in (("preview_button", actions["preview"] and not busy),
                             ("use_button", actions["use"] and not busy),
                             ("favourite_button", bool(selected) and not state["marking"]),
                             ("hide_button", bool(selected) and not state["marking"]),
                             ("export_button", bool(selected) and not busy)):
            state[key].configure(state="normal" if enabled else "disabled")
        favourite = bool(selected) and all(bool(item.get("favourite")) for item in selected)
        hidden = bool(selected) and all(bool(item.get("hidden")) for item in selected)
        state["favourite_button"].configure(text="Remove favourite" if favourite else "Favourite selected")
        state["hide_button"].configure(text="Unhide selected" if hidden else "Hide selected")
        state["previous_button"].configure(state="normal" if state["offset"] > 0 else "disabled")
        state["next_button"].configure(state="normal" if state["offset"] + PAGE_SIZE < state["total"] else "disabled")

    def _library_preview(self):
        selected = self._library_selection()
        if len(selected) == 1 and library_item_actions(selected[0])["preview"] and not getattr(self, "busy", False):
            self.preview_library_item(selected[0])

    def _library_use(self):
        selected = self._library_selection()
        if len(selected) == 1 and library_item_actions(selected[0])["use"] and not getattr(self, "busy", False):
            self.use_library_item(selected[0])

    def _library_export(self):
        selected = self._library_selection()
        if selected and not getattr(self, "busy", False):
            self.export_library_items(selected)

    def _library_mark(self, field):
        state = self._library_state()
        selected = self._library_selection()
        if not state or not selected or state["marking"] or field not in {"favourite", "hidden"}:
            return
        value = not all(bool(item.get(field)) for item in selected)
        token = state["token"]
        state["marking"] = True
        state["status"].configure(text="Saving library marks…")
        self.sync_library_controls()
        def worker():
            completed, error = 0, None
            try:
                store = self._library_worker_store()
                for item in selected:
                    store.set_mark(item["path"], **{field: value})
                    completed += 1
            except Exception as exc:
                error = str(exc)
            self.events.put(("library_marks_done", {"token": token, "completed": completed,
                "count": len(selected), "field": field, "value": value, "error": error}))
        threading.Thread(target=worker, daemon=True).start()

    def handle_library_event(self, event, payload):
        if not event.startswith("library_"):
            return False
        state = self._library_state()
        if event == "library_marks_done":
            # Marks remain intentional even when the browser was closed while
            # SQLite was saving them. Refresh automatic selection metadata too.
            if payload["completed"] and not getattr(self, "_closed", False):
                self.library_marks_changed()
            if state and state["token"] is payload["token"]:
                state["marking"] = False
                if payload["error"]:
                    state["status"].configure(text=f"Saved {payload['completed']} of {payload['count']} marks: {payload['error']}")
                    self.sync_library_controls()
                else:
                    self.refresh_library_browser()
            return True
        if (not state or state["token"] is not payload.get("token")
                or state["serial"] != payload.get("serial") or getattr(self, "_closed", False)):
            return True
        if event == "library_query_error":
            state["status"].configure(text="Could not search the library: " + payload["error"] + ". Refresh to try again.")
            self.sync_library_controls()
            return True
        if event != "library_results":
            return False
        result = payload["result"]
        state.update(items=result["items"], total=result["total"], offset=result["offset"])
        source_map = {}
        for source in result["sources"]:
            label = f"{source['name']} ({source.get('count', 0):,})"
            if label in source_map:
                label = f"{source['name']} · {source.get('provider', '')} ({source.get('count', 0):,})"
            base, duplicate = label, 2
            while label in source_map:
                label, duplicate = f"{base} · {duplicate}", duplicate + 1
            source_map[label] = source["id"]
        selected_source = next((label for label, value in source_map.items() if value == state["source_id"]), "All packs")
        state["source_map"] = source_map
        state["updating_filters"] = True
        try:
            state["source_menu"].configure(values=["All packs", *source_map])
            state["source"].set(selected_source)
        finally:
            state["updating_filters"] = False
        if state["source_id"] is not None and selected_source == "All packs":
            state["source_id"] = None
            self.refresh_library_browser(reset_page=True)
            return True
        listing = state["listing"]
        listing.delete(0, "end")
        for item in state["items"]:
            listing.insert("end", _row_text(item))
        total, offset = state["total"], state["offset"]
        if total:
            end = min(total, offset + len(state["items"]))
            state["page_label"].configure(text=f"{offset + 1:,}–{end:,} of {total:,} sounds · Page {offset // PAGE_SIZE + 1} of {math.ceil(total / PAGE_SIZE)}")
            state["status"].configure(text="Select one sound to audition, or Ctrl-click / Shift-click several to export.")
        else:
            state["page_label"].configure(text="No matching sounds")
            state["status"].configure(text="Try a shorter search, clear the filters, or use Import NI expansions to add sounds.")
        self.sync_library_controls()
        return True
