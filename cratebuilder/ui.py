import copy
import json
import os
import queue
import sys
import threading
import tkinter as tk
from collections import Counter
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .audio import scan_folder, merge_library_session, apply_folder_rules, project_dict
from .catalogue import Catalogue
from .converter import convert_folder
from .matching import Matcher, ROLE_POOLS, STYLES, overview, refresh_warnings, audio_identity
from .model import Assignment, Session, hardware_pad_name, role_names, role_index, kit_count
from .kit_export import export_kit
from .pgm import export_program
from .preview import Player, list_audio_outputs, simple_audio_outputs, audio_output_label
from .editing import regenerate_indices, set_bank_locked
from .live_controls import LivePreviewMixin
from .library import LibraryStore, apply_library_marks
from .library_ui import LibraryBrowserMixin
from .ni_controls import NILibraryMixin
from .live_midi import MIDIController, list_input_ports, load_settings, save_settings

from . import desk_theme as T
from .desk_theme import (BG, PANEL, INSET, TEXT, MUTED, GREEN, AMBER, CONTROL,
                         CONTROL_HOVER, BORDER, CORE_PAD, CREATIVE_PAD, SELECTED)

def app_dir():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


def snapshot_session(session):
    """Keep worker assignments isolated without copying the entire analysis cache."""
    result = copy.copy(session)
    result.pads = copy.deepcopy(session.pads)
    # Library changes replace the list; samples are read-only during worker jobs.
    # Sharing it also preserves the matcher's prepared library statistics.
    result.samples = session.samples
    result.scan_notes = list(session.scan_notes)
    result.sources = list(session.sources)
    result.history = list(session.history)
    result.locked_banks = list(session.locked_banks)
    result.generation_sources = None if session.generation_sources is None else list(session.generation_sources)
    return result


def update_sources(roots, previous, cache_dir, catalogue, progress, cancel):
    """An offline source must not prevent healthy folders from being updated."""
    result, notes, healthy, recovered = previous, [], 0, 0
    for number, root in enumerate(roots):
        def report(message, value, n=number):
            progress(message, (n + value) / len(roots))
        try:
            fresh = scan_folder(root, cache_dir, report, cancel, catalogue=catalogue)
            healthy += 1
        except InterruptedError:
            raise
        except (OSError, ValueError) as exc:
            detail = str(exc)
            catalogue.record_scan_status(root, "unavailable", detail, root=root)
            known = {sample.id: sample for sample in catalogue.cached_samples(root)}
            prefix = os.path.normcase(os.path.abspath(str(root))).rstrip("\\/") + os.sep
            for sample in previous.samples if previous else []:
                if os.path.normcase(os.path.abspath(sample.path)).startswith(prefix):
                    known.setdefault(sample.id, sample)
            cached = []
            for sample in known.values():
                sample = copy.copy(sample)
                sample.features = {**sample.features, "available": False}
                cached.append(sample)
            recovered += len(cached)
            fresh = Session(str(root), cached, sources=[str(root)],
                            scan_notes=[f"Unavailable source {root}: {detail}. Retained {len(cached)} catalogue records."])
            report(f"Source unavailable; retained {len(cached)} cached sounds", 1)
        notes.extend(fresh.scan_notes)
        result = merge_library_session(result, fresh) if result else fresh
    if not healthy and not recovered:
        raise ValueError("No sample sources are available and no cached sounds could be restored. Reconnect a source and update again.")
    result.scan_notes = notes
    return result


class WorkspaceTabs(ctk.CTkTabview):
    def set(self, name):
        # CTkTabview 5.2 schedules a hide with the old tab name after 100 ms.
        # Fast navigation can therefore hide the current page. Switch atomically.
        if name not in self._tab_dict:
            raise ValueError(f"Unknown workspace tab: {name}")
        self._current_name = name
        self._segmented_button.set(name)
        self._grid_forget_all_tabs(exclude_name=name)
        self._set_grid_current_tab()


class App(NILibraryMixin, LibraryBrowserMixin, LivePreviewMixin, ctk.CTk):
    def __init__(self, session_path=None, catalogue_path=None, default_kit_size=16, default_target="sp404"):
        T.initialise()
        super().__init__()
        self.title("CrateBuilder · Kit workspace")
        self.geometry("1440x960")
        self.minsize(1200, 760)
        self.configure(fg_color=BG)
        self.session = None
        self.bank, self.selected = 0, 4
        self.selected_pads = set()
        self.busy = False
        self.closing = False
        self._closed = False
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.player = Player()
        self.live_engine = None
        self.midi_controller = None
        self.live_signature = None
        self.live_failed_signature = None
        self._live_reload_after = None
        self.connection_window = None
        self.live_colors = {"on": GREEN, "off": CONTROL, "pending": AMBER, "dark": BG, "text": TEXT}
        self.preview_serial = 0
        self.preview_lock = threading.Lock()
        self.rank_lock = threading.Lock()
        self.rank_serial = 0
        self._autosave_after = None
        self.data_dir = Path(os.environ["CRATEBUILDER_DATA_DIR"]) if os.environ.get("CRATEBUILDER_DATA_DIR") else Path(os.environ.get("LOCALAPPDATA", str(app_dir()))) / "CrateBuilder"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.connection_settings = load_settings(self.data_dir / "connections.json")
        self.catalogue = Catalogue(catalogue_path or self.data_dir / "catalogue.sqlite3")
        self.library_store = LibraryStore(self.data_dir / "library.sqlite3")
        kitchen = Path.home() / "Music/SAMPLES_ALL/Folly/kitchenSamples"
        if kitchen.is_dir():
            self.catalogue.ensure_folder_rule(kitchen, ["kitchen", "utensils", "percussion"], kind="foley")
        initial_samples = Path.home() / "Music/SAMPLES_ALL"
        self.path_var = tk.StringVar(value=str(initial_samples) if initial_samples.is_dir() else "")
        self.style_var = tk.StringVar(value="Balanced")
        self.bpm_var = tk.StringVar(value="90")
        self.name_var = tk.StringVar(value="")
        self.kit_size_var = tk.StringVar(value=f"{default_kit_size} pads")
        self.target_var = tk.StringVar(value="MPC1000" if default_target == "mpc" else "SP-404MKII")
        self.surprise_var = tk.DoubleVar(value=.35)
        self.sub_var = tk.BooleanVar(value=True)
        self.scope_var = tk.StringVar(value="Current bank")
        self.candidate_var = tk.StringVar(value="Update library first")
        self.candidates = {}
        self.candidate_keys = []
        self._suppress_candidate_preview = False
        self._layout()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.bind("<space>", self.key_preview)
        self.bind("<Left>", self.key_step_candidate)
        self.bind("<Right>", self.key_step_candidate)
        self.bind("<Escape>", lambda _: self.stop())
        self.bind("<Control-g>", lambda _: self.generate_scope())
        self.bind("<Control-s>", lambda _: self.save_session())
        self.after(80, self.poll)
        starter = Path(session_path) if session_path else self.data_dir / "last-session.json"
        if starter.is_file():
            self.after(150, lambda: self.open_session(starter))
        else:
            self.refresh()
            self.workspace_tabs.set("Library")

    def label(self, parent, text, size=13, color=TEXT, bold=False, **kw):
        return ctk.CTkLabel(parent, text=text, text_color=color,
                            font=ctk.CTkFont(family="Segoe UI", size=size, weight="bold" if bold else "normal"), **kw)

    def button(self, parent, text, command, primary=False, **kw):
        button = ctk.CTkButton(parent, text=text, command=command, height=kw.pop("height", 34),
            fg_color=GREEN if primary else CONTROL, text_color=T.ON_ACTION if primary else TEXT,
            hover_color="#f0c9be" if primary else CONTROL_HOVER, corner_radius=1,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold" if primary else "normal"), **kw)
        T.focus_ring(button)
        return button

    def _layout(self):
        from .workspace import build
        build(self, WorkspaceTabs)

    def set_status(self, message):
        self.status_label.configure(text=message[:155])

    def progress(self, message, value):
        self.events.put(("progress", (message, value)))

    def job_controls(self):
        return (self.scan_button, self.convert_button, self.shuffle_button, self.export_button,
                self.tag_button, self.complete_button, self.upper_button, self.target_menu,
                self.size_menu, self.surprise_slider, self.style_menu, self.substitute_check,
                self.bpm_entry, self.path_entry, self.connection_button, self.scope_menu,
                self.bank_lock_button, self.send_button, self.ni_import_button,
                self.library_browser_button, self.sources_button, self.favourite_button, self.hide_button, self.keep_sound_button)

    def run_job(self, work, done, cancellable=False, close_safe=True, cancel_label="Cancel"):
        if self.busy:
            return
        self.busy = True
        self.cancel_event.clear()
        self.rank_serial += 1
        for widget in self.job_controls():
            widget.configure(state="disabled")
        self.cancel_button.configure(state="normal" if cancellable else "disabled")
        self.cancel_button.configure(text=cancel_label)
        for check in self.pad_lock_checks.values():
            check.configure(state="disabled")
        for check in self.pad_select_checks.values():
            check.configure(state="disabled")
        self.sync_library_controls()
        def worker():
            try:
                result = work()
                self.events.put(("done", (done, result, close_safe)))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress":
                    self.set_status(payload[0])
                    self.progress_bar.set(payload[1])
                elif event in ("done", "error"):
                    self.busy = False
                    for widget in self.job_controls():
                        widget.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    for check in self.pad_lock_checks.values():
                        check.configure(state="normal" if self.session else "disabled")
                    if event == "error":
                        self.refresh()
                        self.set_status(payload)
                        if payload != "Scan cancelled" and not self.closing:
                            messagebox.showerror("Crate Builder", payload, parent=self)
                    else:
                        callback, result, close_safe = payload
                        if not self.closing or close_safe:
                            callback(result)
                    if not self.closing:
                        self.update_generation_controls()
                        self.paint_pads()
                        self.sync_library_controls()
                    if self.closing:
                        self.close_app()
                        return
                elif event == "audio_error":
                    self.set_status("Audio preview unavailable: " + payload)
                elif event == "candidates":
                    serial, index, candidates, library, cache = payload
                    if serial == self.rank_serial and index == self.selected and not self.busy:
                        if self.session and self.session.samples is library:
                            self.session._matching_cache = cache
                        self.set_candidates(candidates)
                elif event == "midi_hit":
                    engine, local = payload
                    if engine is self.live_engine and engine.loaded_bank == self.bank:
                        self.flash_pad(local)
                elif event == "midi_event":
                    self.handle_midi_event(payload)
                elif event == "midi_error":
                    self.set_status("MIDI connection: " + payload)
                elif event.startswith("live_"):
                    self.handle_live_result(event, payload)
                elif event.startswith("library_"):
                    self.handle_library_event(event, payload)
        except queue.Empty:
            pass
        except Exception as exc:
            self.set_status("Could not finish this operation: " + str(exc))
            if self.closing:
                self.close_app()
            else:
                messagebox.showerror("Crate Builder", "Could not finish this operation:\n" + str(exc), parent=self)
        finally:
            if not self._closed:
                self.after(70, self.poll)

    def open_output_folder(self, output):
        try:
            os.startfile(output)
        except OSError as exc:
            self.set_status(f"Files saved to {output}. Could not open the folder: {exc}")

    def save_connections(self):
        save_settings(self.data_dir / "connections.json", self.connection_settings)

    def flash_pad(self, local):
        if not 0 <= local < 16:
            return
        bank = self.bank
        button = self.pad_buttons[local]
        button.configure(border_color=AMBER, border_width=3)
        def restore():
            if self._closed or self.bank != bank:
                return
            index = self.bank * 16 + local
            pad = self.session.pads[index] if self.session else Assignment()
            chosen = index in self.selected_pads or index == self.selected
            button.configure(border_color=GREEN if chosen else AMBER if pad.warning else BORDER,
                             border_width=2 if chosen else 1)
        self.after(110, restore)

    def handle_midi_event(self, event):
        if event.get("type") == "learned":
            self.connection_settings["mapping"] = event["mapping"]
            self.save_connections()
            target = hardware_pad_name(self.bank * 16 + event['pad'], self.session.target if self.session else "sp404")
            text = f"Learned note {event['note']} on channel {event['channel']} for {target}."
            self.set_status(text)
        elif event.get("type") == "note":
            local = event["pad"]
            label = hardware_pad_name(self.bank * 16 + local, self.session.target if self.session else "sp404")
            text = f"Channel {event['channel']} · note {event['note']} → {label} · velocity {event['velocity']}"
        else:
            return
        if self.connection_window and self.connection_window.winfo_exists():
            self.midi_activity_label.configure(text=text)



    def audio_device(self, settings, outputs):
        for output in outputs:
            if output["name"] == settings.get("audioDeviceName") and output["hostapi_name"] == settings.get("hostApiName"):
                return output["index"]
        if settings.get("audioDeviceName"):
            raise ValueError("The saved audio output is unavailable. Choose an available output in Audio / MIDI settings.")
        return None

    def audio_midi_settings(self):
        if self.connection_window and self.connection_window.winfo_exists():
            self.connection_window.lift()
            return
        if self.busy:
            return
        try:
            outputs, inputs = list_audio_outputs(), list_input_ports()
        except Exception as exc:
            messagebox.showerror("Audio settings", "Could not list connections:\n" + str(exc), parent=self)
            return
        window = self.connection_window = ctk.CTkToplevel(self)
        window.title("Audio & MIDI")
        window.geometry("690x610")
        window.minsize(620, 540)
        window.transient(self)
        window.grab_set()
        window.bind("<Escape>", lambda _: window.destroy())
        actions = ctk.CTkFrame(window, fg_color="transparent")
        actions.pack(side="bottom", fill="x", padx=24, pady=(8, 20))
        form = ctk.CTkScrollableFrame(window, fg_color=PANEL)
        form.pack(fill="both", expand=True, padx=16, pady=(16, 0))
        self.label(form, "Hear your kit", 22, TEXT, True).pack(anchor="w", padx=12, pady=(12, 6))
        self.label(form, "Choose an output, then use MIDI preview on the main screen to play from your SP pads.",
            13, MUTED, wraplength=585, justify="left").pack(anchor="w", padx=12, pady=(0, 16))
        audio_var, midi_var = tk.StringVar(), tk.StringVar()
        advanced_var = tk.BooleanVar(value=False)
        output_lookup = {}
        self.label(form, "Audio output", 13, TEXT, True).pack(anchor="w", padx=12, pady=(4, 6))
        audio_menu = ctk.CTkOptionMenu(form, variable=audio_var, values=["Computer default"],
            dynamic_resizing=False, fg_color=CONTROL, button_color=CONTROL_HOVER, height=36)
        audio_menu.pack(fill="x", padx=12)
        self.audio_output_menu = audio_menu
        self.audio_choices_label = self.label(form, "", 12, MUTED, wraplength=585, justify="left")
        self.audio_choices_label.pack(anchor="w", padx=12, pady=(6, 16))
        saved_output = next((o for o in outputs if o["name"] == self.connection_settings.get("audioDeviceName")
            and o["hostapi_name"] == self.connection_settings.get("hostApiName")), None)
        if not self.connection_settings.get("audioDeviceName"):
            saved_output = next((o for o in simple_audio_outputs(outputs) if "404" in o["name"]), None)
        def populate_outputs(initial=None):
            previous = initial or output_lookup.get(audio_var.get())
            available = outputs if advanced_var.get() else simple_audio_outputs(outputs)
            output_lookup.clear()
            for output in available:
                label = audio_output_label(output, advanced=advanced_var.get())
                if "404" in output["name"] and not advanced_var.get():
                    label = "SP-404MKII · USB audio"
                if label in output_lookup:
                    label += f" [{output['index']}]"
                output_lookup[label] = output
            names = ["Computer default", *output_lookup]
            selected = next((label for label, o in output_lookup.items() if previous and
                o["index"] == previous["index"]), None)
            if selected is None and previous:
                selected = next((label for label, o in output_lookup.items() if o["name"] == previous["name"]), None)
            audio_menu.configure(values=names)
            audio_var.set(selected or "Computer default")
            self.audio_choices_label.configure(text=(f"{len(available)} driver endpoints. Choose an alternative driver only if needed."
                if advanced_var.get() else f"{len(available)} outputs · duplicate Windows drivers hidden. Advanced shows the full list."))
        populate_outputs(saved_output)
        midi_default = self.connection_settings.get("midiInputName")
        if midi_default not in inputs:
            midi_default = next((name for name in inputs if "404" in name), None)
        midi_var.set(midi_default or "None · mouse only")
        self.label(form, "MIDI controller", 13, TEXT, True).pack(anchor="w", padx=12, pady=(0, 6))
        ctk.CTkOptionMenu(form, variable=midi_var, values=["None · mouse only", *inputs],
            dynamic_resizing=False, fg_color=CONTROL, button_color=CONTROL_HOVER, height=36).pack(fill="x", padx=12)
        self.label(form, "SP setup: MIDI Mode A · PAD Note Out ON\nUSB In → MIX OUT lets you hear the computer through the SP.\nUse an empty SP bank to avoid playing its stored sounds too.",
            12, MUTED, wraplength=585, justify="left").pack(anchor="w", padx=12, pady=16)
        advanced = ctk.CTkFrame(form, fg_color=INSET)
        def toggle_advanced():
            populate_outputs()
            if advanced_var.get():
                advanced.pack(fill="x", padx=12, pady=(8, 12), before=self.midi_activity_label)
            else:
                advanced.pack_forget()
        ctk.CTkCheckBox(form, text="Advanced · all drivers & MIDI mapping", variable=advanced_var,
            command=toggle_advanced, font=ctk.CTkFont(size=12), checkbox_width=18, checkbox_height=18,
            fg_color=GREEN, text_color=TEXT).pack(anchor="w", padx=12, pady=(0, 8))
        channel = self.connection_settings.get("channel")
        channel_var = tk.StringVar(value=str(channel) if channel else "Any · banks A–J")
        offset_var = tk.StringVar(value=str(self.connection_settings.get("noteOffset", 0)))
        self.label(advanced, "MIDI channel", 12, MUTED).pack(anchor="w", padx=12, pady=(12, 4))
        ctk.CTkOptionMenu(advanced, variable=channel_var, values=["Any · banks A–J", *map(str, range(1, 17))],
            fg_color=CONTROL, button_color=CONTROL_HOVER).pack(fill="x", padx=12)
        offset_row = ctk.CTkFrame(advanced, fg_color="transparent")
        offset_row.pack(fill="x", padx=12, pady=12)
        self.label(offset_row, "Note offset · SP Mode A: 0", 12, MUTED).pack(side="left")
        ctk.CTkEntry(offset_row, textvariable=offset_var, width=70).pack(side="right")
        self.label(advanced, "If USB In is LINE IN, enable EXT SOURCE to hear the USB return.",
            12, MUTED, wraplength=550, justify="left").pack(anchor="w", padx=12, pady=(0, 12))
        self.midi_activity_label = self.label(form, "MIDI preview is controlled from the main screen.",
            12, GREEN, wraplength=585, justify="left")
        self.midi_activity_label.pack(anchor="w", padx=12, pady=(10, 12))
        learn_row = ctk.CTkFrame(advanced, fg_color="transparent")
        learn_row.pack(fill="x", padx=12, pady=(0, 12))
        def learn():
            if not self.midi_controller or not self.midi_controller.connected:
                self.midi_activity_label.configure(text="Turn MIDI preview on first, then reopen settings to learn a pad.")
                return
            local = self.selected % 16
            self.midi_controller.learn(local)
            target = hardware_pad_name(self.bank * 16 + local, self.session.target if self.session else "sp404")
            self.midi_activity_label.configure(text=f"Learning {target}: tap its controller pad.")
        def reset_mapping():
            self.connection_settings.update(mapping={}, channel=None, noteOffset=0)
            self.save_connections()
            self.disable_live_preview(quiet=True)
            channel_var.set("Any · banks A–J")
            offset_var.set("0")
            self.midi_activity_label.configure(text="SP Mode A mapping restored. Save, then turn MIDI preview on.")
        self.button(learn_row, "Learn selected pad", learn, width=180).pack(side="left")
        self.button(learn_row, "Reset SP mapping", reset_mapping, width=170).pack(side="left", padx=8)
        def apply():
            try:
                offset = int(offset_var.get())
                if not -127 <= offset <= 127:
                    raise ValueError("Note offset must be between -127 and 127.")
                output = output_lookup.get(audio_var.get())
                was_live = bool(self.live_engine or getattr(self, "_live_connecting", False))
                self.connection_settings.update(audioDeviceName=output["name"] if output else None,
                    hostApiName=output["hostapi_name"] if output else None,
                    midiInputName=midi_var.get() if midi_var.get() in inputs else None,
                    channel=None if channel_var.get().startswith("Any") else int(channel_var.get()), noteOffset=offset)
                self.save_connections()
                self.disable_live_preview(quiet=True)
                self.player.set_output_device(output["index"] if output else None)
                window.destroy()
                if was_live and self.connection_settings.get("midiInputName"):
                    self.enable_live_preview()
                else:
                    self.set_status("Audio settings saved. Use MIDI preview to play from your controller.")
            except (OSError, ValueError) as exc:
                self.midi_activity_label.configure(text=str(exc), text_color=AMBER)
        self.button(actions, "Cancel", window.destroy, width=110).pack(side="left")
        self.button(actions, "Save settings", apply, primary=True, width=160).pack(side="right")



    def send_to_sp(self, auto_probe=True):
        if self.busy or not self.session:
            self.set_status("Build a kit before sending it to the SP.")
            return
        from . import sp_transfer
        if getattr(self, "transfer_window", None) and self.transfer_window.winfo_exists():
            self.transfer_window.focus()
            return
        snapshot = snapshot_session(self.session)
        snapshot.target = "sp404"
        kit = self.kit_for_group(1 if snapshot.kit_size == 8 and self.selected % 16 >= 8 else 0)
        window = self.transfer_window = ctk.CTkToplevel(self)
        window.title("Send kit to SP · review destination")
        window.geometry("880x800")
        window.minsize(760, 660)
        window.transient(self)
        window.grab_set()
        transfer_actions = ctk.CTkFrame(window, fg_color=INSET, corner_radius=0)
        transfer_actions.pack(side="bottom", fill="x", padx=16, pady=(0, 16))
        form = ctk.CTkScrollableFrame(window, fg_color=PANEL)
        form.pack(fill="both", expand=True, padx=16, pady=16)
        ctk.CTkLabel(form, text=f"Send kit {kit + 1} / {snapshot.kit_size} pads", font=T.display(26),
                    text_color=TEXT).pack(anchor="w", padx=12, pady=(10, 8))
        self.label(form, "Choose a destination bank, check the pad map, then send the kit. The app prepares the audio automatically.",
                   12, MUTED, wraplength=755, justify="left").pack(anchor="w", padx=12, pady=(0, 12))
        cli_found = sp_transfer.discover_cli(self.connection_settings.get("cliPath"))
        cli_var = tk.StringVar(value=str(cli_found or self.connection_settings.get("cliPath") or ""))
        port_var = tk.StringVar(value=self.connection_settings.get("port") or "Automatic")
        bank_var = tk.StringVar(value="Choose target bank")
        review_var, overwrite_var = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        backup_var = tk.StringVar(value=str(self.data_dir / "device-backups"))
        state = {"probe": None, "plan": None, "staged": None, "connection": None, "sending": False, "close_requested": False}
        advanced = ctk.CTkFrame(form, fg_color=INSET)
        self.transfer_advanced = advanced
        def toggle_transfer_options():
            if advanced.winfo_manager():
                advanced.pack_forget()
            else:
                advanced.pack(fill="x", padx=12, pady=(0, 12), after=options_toggle)
        options_toggle = self.button(form, "Connection options…", toggle_transfer_options, width=180, height=30)
        options_toggle.pack(anchor="w", padx=12, pady=(0, 10))
        self.label(advanced, "USB transfer helper · normally found automatically", 12, MUTED).pack(anchor="w", padx=12)
        cli_row = ctk.CTkFrame(advanced, fg_color="transparent")
        cli_row.pack(fill="x", padx=12, pady=(4, 10))
        ctk.CTkEntry(cli_row, textvariable=cli_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        def browse_cli():
            chosen = filedialog.askopenfilename(parent=window, title="Choose Sparky CLI", filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
            if chosen:
                cli_var.set(chosen)
        self.button(cli_row, "Browse…", browse_cli, width=90).pack(side="right")
        row = ctk.CTkFrame(form, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 10))
        self.label(advanced, "SP connection · automatic finds the only connected device", 12, MUTED).pack(anchor="w", padx=12)
        port_menu = ctk.CTkOptionMenu(advanced, variable=port_var, values=list(dict.fromkeys(["Automatic", port_var.get()])),
                                      fg_color=CONTROL, button_color=CONTROL_HOVER, width=165)
        port_menu.pack(fill="x", padx=12, pady=(4, 12))
        status = self.label(form, "No device checked yet. This step only reads the SP.", 12, GREEN, wraplength=755, justify="left")
        review_text = ctk.CTkTextbox(form, height=220, font=ctk.CTkFont(family="Consolas", size=12), wrap="word")

        def set_review(text):
            review_text.configure(state="normal")
            review_text.delete("1.0", "end")
            review_text.insert("1.0", text)
            review_text.configure(state="disabled")

        def update_send(*_):
            plan = state["plan"]
            ready = bool(plan and state["staged"]
                         and (not plan.overwrites or overwrite_var.get() and backup_var.get().strip()))
            send_button.configure(state="normal" if ready and not self.busy and not state["sending"] else "disabled")

        def invalidate(*_):
            state.update(probe=None, plan=None, staged=None)
            review_var.set(False)
            overwrite_var.set(False)
            set_review("Connection changed. Check the device again.")
            update_send()

        def build_plan(*_):
            state.update(plan=None, staged=None)
            review_var.set(False)
            overwrite_var.set(False)
            if not state["probe"] or bank_var.get() not in "ABCDEFGHIJ" or len(bank_var.get()) != 1:
                set_review("Check the device and choose a destination bank A–J.")
                prepare_button.configure(state="disabled")
                update_send()
                return
            try:
                plan = sp_transfer.make_transfer_plan(snapshot, kit, bank_var.get(), state["probe"])
                state["plan"] = plan
                lines = [f"PROJECT {plan.project}: {plan.project_name}", f"PORT: {plan.port}   DESTINATION BANK: {plan.bank}", ""]
                for entry in plan.entries:
                    existing = f"  REPLACES {entry.previous_name}" if entry.occupied else "  empty"
                    lines.append(f"{entry.target_pad:4s}  {entry.role}: {entry.name}{existing}")
                lines.extend(["", f"{len(plan.entries)} files · {len(plan.overwrites)} occupied pads will be replaced.",
                              "Check this destination and pad map before clicking Send."])
                set_review("\n".join(lines))
                overwrite_check.configure(text=f"Replace {len(plan.overwrites)} occupied pads",
                                          state="normal" if plan.overwrites else "disabled")
                prepare_button.configure(state="normal")
                status.configure(text="Destination read successfully. No samples have been written.")
                prepare()
            except Exception as exc:
                set_review(str(exc))
                prepare_button.configure(state="disabled")
            update_send()

        def probe_device():
            if self.busy:
                return
            cli = sp_transfer.discover_cli(cli_var.get().strip())
            if not cli:
                status.configure(text="Choose a working Sparky CLI executable first.")
                return
            connection = (str(cli), port_var.get())
            state.update(probe=None, plan=None, staged=None, connection=connection)
            review_var.set(False)
            update_send()
            status.configure(text="Reading the connected SP project and pad occupancy…")
            def work():
                ports = sp_transfer.list_ports(cli)
                details = sp_transfer.probe(cli, port=None if connection[1] == "Automatic" else connection[1])
                return ports, details
            def done(result):
                if not window.winfo_exists():
                    return
                if (str(sp_transfer.discover_cli(cli_var.get().strip())), port_var.get()) != connection:
                    invalidate()
                    return
                ports, details = result
                values = ["Automatic", *[item["port"] for item in ports]]
                port_menu.configure(values=list(dict.fromkeys(values)))
                state["probe"] = details
                automatic_cli = sp_transfer.discover_cli()
                saved_cli = "" if automatic_cli and Path(cli).resolve() == automatic_cli.resolve() else str(cli)
                self.connection_settings.update(cliPath=saved_cli, port=None if connection[1] == "Automatic" else connection[1])
                self.save_connections()
                status.configure(text=f"Connected: project {details['project']} · {details['project_name']} · {details['port']}. Choose a target bank.")
                build_plan()
            self.run_job(work, done, close_safe=False)

        self.button(row, "Check connected SP", probe_device, width=185).pack(side="left")
        ctk.CTkOptionMenu(row, variable=bank_var, values=["Choose target bank", *"ABCDEFGHIJ"],
                         command=build_plan, fg_color=CONTROL, button_color=CONTROL_HOVER, width=185).pack(side="right")
        status.pack(anchor="w", padx=12, pady=(2, 10))
        review_text.pack(fill="x", padx=12, pady=(0, 10))
        set_review("Check the SP to read its current project. Sending never changes which project is selected.")

        def prepare():
            if not window.winfo_exists() or not state["plan"]:
                return
            if self.busy:
                self.after(100, prepare)
                return
            plan = state["plan"]
            status.configure(text="Preparing and validating local SP WAV copies…")
            def done(result):
                if not window.winfo_exists() or state["plan"] is not plan:
                    return
                state["staged"] = result[0]
                status.configure(text=f"Kit ready. Send will write {len(plan.entries)} pads to project {plan.project}, bank {plan.bank}.")
                send_button.configure(text=f"Send kit to bank {plan.bank}")
                update_send()
            self.run_job(lambda: export_kit(snapshot, kit, self.data_dir / "transfer-staging", self.progress), done, close_safe=False)

        prepare_button = self.button(form, "Prepare WAVs for this plan", prepare, width=240)
        prepare_button.configure(state="disabled")
        overwrite_check = ctk.CTkCheckBox(form, text="Replace occupied pads", variable=overwrite_var,
                                         command=update_send, checkbox_width=18, checkbox_height=18)
        overwrite_check.pack(anchor="w", padx=12, pady=(0, 10))
        overwrite_check.configure(state="disabled")
        self.label(form, "Backup folder · used before replacing occupied pads", 11, MUTED).pack(anchor="w", padx=12)
        backup_row = ctk.CTkFrame(form, fg_color="transparent")
        backup_row.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkEntry(backup_row, textvariable=backup_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        def browse_backup():
            parent = filedialog.askdirectory(parent=window, title="Choose a backup parent folder")
            if parent:
                backup_var.set(str(Path(parent) / ("SP-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S"))))
        self.button(backup_row, "Browse…", browse_backup, width=90).pack(side="right")
        actions = ctk.CTkFrame(transfer_actions, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=(2, 14))

        def send():
            if self.busy or not state["plan"] or not state["staged"]:
                return
            plan, staged, cli = state["plan"], state["staged"], state["connection"][0]
            overwrite, backup = overwrite_var.get(), backup_var.get().strip()
            if plan.overwrites and (not overwrite or not backup):
                return
            state["sending"] = True
            update_send()
            status.configure(text="Sending reviewed kit. Closing this dialog requests a stop after the current pad.")
            def work():
                try:
                    report = sp_transfer.send(plan, cli, staged, reviewed=True, allow_overwrite=overwrite,
                        backup_dir=backup or None, progress=self.progress, cancel=self.cancel_event)
                    return report, None
                except Exception as exc:
                    return getattr(exc, "report", {}), str(exc)
            def done(result):
                report, error = result
                state["sending"] = False
                state["staged"] = None
                state["probe"] = None
                state["plan"] = None
                completed = report.get("completed_pads", [])
                message = f"{len(completed)} pads sent. " + (error if error else "Kit transfer complete.")
                if report.get("journal"):
                    message += f" Transfer record: {report['journal']}"
                self.set_status(message)
                if window.winfo_exists():
                    status.configure(text=message)
                    review_var.set(False)
                    update_send()
                    if state["close_requested"]:
                        window.destroy()
                if error and not state["close_requested"] and not self.closing:
                    messagebox.showerror("SP transfer stopped", message, parent=window if window.winfo_exists() else self)
            self.run_job(work, done, cancellable=True, cancel_label="Stop after pad")

        def close():
            if state["sending"]:
                state["close_requested"] = True
                self.cancel_event.set()
                status.configure(text="Stopping after the current pad finishes. Completed pads stay recorded in the transfer journal.")
            else:
                window.destroy()
        window.protocol("WM_DELETE_WINDOW", close)
        self.button(actions, "Close", close, width=110).pack(side="left")
        send_button = self.button(actions, "Send reviewed kit to SP", send, primary=True, width=240)
        send_button.pack(side="right")
        send_button.configure(state="disabled")
        cli_var.trace_add("write", invalidate)
        port_var.trace_add("write", invalidate)
        backup_var.trace_add("write", update_send)
        if auto_probe:
            self.after(100, lambda: probe_device() if window.winfo_exists() else None)

    def browse(self):
        if self.busy:
            return
        path = filedialog.askdirectory(title="Choose a sample folder", parent=self)
        if path:
            self.path_var.set(path)
            self.folder_label.configure(text=Path(path).name)

    def convert_audio_folder(self):
        if self.busy:
            return
        source = self.path_var.get().strip().strip('"')
        if not Path(source).is_dir():
            source = filedialog.askdirectory(title="Choose the folder of sounds to convert", parent=self)
        if not source:
            return
        destination = filedialog.askdirectory(title="Choose where to save the converted MPC folder", parent=self)
        if not destination:
            return
        self.path_var.set(source)
        self.folder_label.configure(text=Path(source).name)
        self.stop()
        def done(result):
            output, report = result
            self.set_status(f"Converted {report['converted']}/{report['found']} sounds to MPC WAV. Originals were unchanged.")
            messagebox.showinfo("MPC WAV conversion complete",
                f"Saved to:\n{output}\n\nConverted: {report['converted']}\nFailed: {report['failed']}\n\n"
                "All successful files are 44.1 kHz, 16-bit PCM WAV. See FILE_MAP.csv and CONVERSION_REPORT.txt for details.",
                parent=self)
            self.open_output_folder(output)
        self.run_job(lambda: convert_folder(source, destination, self.progress, self.cancel_event), done, cancellable=True, close_safe=False)

    def settings_changed(self):
        if self.session and not self.busy:
            self.session.style = self.style_var.get()
            self.session.allow_substitutes = self.sub_var.get()
            self.session.surprise = self.surprise_var.get()
            try:
                self.session.bpm = max(40, min(200, int(self.bpm_var.get())))
            except ValueError:
                self.session.bpm = 90
            self.bpm_var.set(str(self.session.bpm))
            self.set_status("Settings updated. Regenerate unlocked pads to apply matching changes.")

    def scan(self):
        if self.busy:
            return
        path, style, substitutes = self.path_var.get().strip().strip('"'), self.style_var.get(), self.sub_var.get()
        if not path:
            self.set_status("Choose a sample folder first.")
            return
        previous = snapshot_session(self.session) if self.session else None
        size = 16 if self.kit_size_var.get() == "16 pads" else 8
        target = "sp404" if self.target_var.get() == "SP-404MKII" else "mpc"
        surprise = self.surprise_var.get()
        try:
            bpm = max(40, min(200, int(self.bpm_var.get())))
        except ValueError:
            bpm = 90
        self.stop()
        roots = list(dict.fromkeys([path, *(previous.sources or [previous.root] if previous else [])]))
        if previous and previous.generation_sources is not None:
            from .ni import metadata_for_path
            active = set(previous.generation_sources)
            # Explicitly chosen folders still update; inactive installed packs
            # do not silently restart their audio analysis.
            roots = [root for root in roots if root == path or
                     not (info := metadata_for_path(Path(root) / "_source.wav")) or info["source"] in active]
        def work():
            result = update_sources(roots, previous, self.data_dir / "cache", self.catalogue, self.progress, self.cancel_event)
            if not previous:
                result.style, result.bpm, result.allow_substitutes = style, bpm, substitutes
                result.kit_size, result.target, result.surprise = size, target, surprise
            return self.register_library_samples(result)
        self.run_job(work, self.scan_done, cancellable=True)

    def scan_done(self, session):
        self.session = session
        self.player._cache.clear()
        self.refresh()
        self.workspace_tabs.set("Kit")
        self.set_status(f"Library updated: {len(session.samples):,} samples. Pad choices and locks preserved. Use Regenerate to build kits.")
        self.autosave()

    def add_folder(self):
        if self.busy:
            return
        if not self.session:
            self.browse()
            return
        folder = filedialog.askdirectory(title="Add more sounds (for example, a cymbal folder)", parent=self)
        if not folder:
            return
        previous = snapshot_session(self.session)
        def work():
            fresh = scan_folder(folder, self.data_dir / "cache", self.progress, self.cancel_event, catalogue=self.catalogue)
            return self.register_library_samples(merge_library_session(previous, fresh))
        self.run_job(work, self.scan_done, cancellable=True)

    def shuffle(self, kit=None):
        if not self.session:
            return
        size = self.session.kit_size
        indices = range(64) if kit is None else range(kit * size, (kit + 1) * size)
        self.generate_indices(indices, "whole crate" if kit is None else f"kit {kit + 1}")

    def scope_changed(self, _value=None):
        self.update_generation_controls()

    def update_generation_controls(self):
        scope = self.scope_var.get()
        selected = sorted(i for i in self.selected_pads if i // 16 == self.bank)
        protected = sum(bool(self.session and (self.session.pads[i].locked or self.bank in self.session.locked_banks)) for i in selected)
        text = f"{len(selected)} pads selected"
        if protected:
            text += f" · {protected} locked"
        self.selection_label.configure(text=text)
        labels = {"Current bank": f"Generate bank {'ABCD'[self.bank]}", "Selected pads": f"Generate {len(selected)} selected",
                  "Upper 8": "Generate upper 8", "Whole crate": "Generate all banks"}
        eligible = bool(self.session and self.session.samples and not self.busy)
        if scope == "Selected pads":
            eligible = eligible and len(selected) > protected
        elif scope != "Whole crate":
            eligible = eligible and self.bank not in (self.session.locked_banks if self.session else [])
        elif self.session:
            eligible = eligible and len(self.session.locked_banks) < 4
        self.shuffle_button.configure(text=labels[scope], state="normal" if eligible else "disabled")
        bank_locked = bool(self.session and self.bank in self.session.locked_banks)
        self.bank_lock_button.configure(text="Unlock bank" if bank_locked else "Lock bank",
            fg_color=GREEN if bank_locked else CONTROL, text_color=BG if bank_locked else TEXT,
            state="normal" if self.session and not self.busy else "disabled")

    def toggle_pad_selection(self, local):
        if self.busy or not self.session:
            return
        index = self.bank * 16 + local
        if self.pad_select_vars[local].get():
            self.selected_pads.add(index)
        else:
            self.selected_pads.discard(index)
        self.scope_var.set("Selected pads")
        self.paint_pads()
        self.update_generation_controls()

    def ctrl_select_pad(self, local):
        if not self.busy and self.session:
            self.pad_select_vars[local].set(not self.pad_select_vars[local].get())
            self.toggle_pad_selection(local)
        return "break"

    def select_all_pads(self, selected):
        if self.busy or not self.session:
            return
        self.selected_pads = set(range(self.bank * 16, self.bank * 16 + 16)) if selected else set()
        self.scope_var.set("Selected pads")
        self.paint_pads()
        self.update_generation_controls()

    def toggle_bank_lock(self):
        if self.busy or not self.session:
            return
        locked = self.bank not in self.session.locked_banks
        set_bank_locked(self.session, self.bank, locked)
        self.bank_buttons[self.bank].configure(text=f"{'ABCD'[self.bank]} · LOCKED" if locked else "BANK " + "ABCD"[self.bank])
        self.paint_pads()
        self.update_generation_controls()
        self.autosave()
        self.set_status(f"Bank {'ABCD'[self.bank]} {'locked against generation' if locked else 'unlocked'}. Individual pad locks are preserved.")

    def generate_scope(self):
        if self.busy or not self.session:
            return
        scope, bank = self.scope_var.get(), self.bank
        if scope == "Selected pads":
            indices = sorted(i for i in self.selected_pads if i // 16 == bank)
        elif scope == "Upper 8":
            if self.session.kit_size != 16:
                self.set_status("Choose 16 pads in More to generate percussion and foley in the upper eight.")
                return
            indices = list(range(bank * 16 + 8, bank * 16 + 16))
        elif scope == "Whole crate":
            indices = list(range(64))
        else:
            indices = list(range(bank * 16, bank * 16 + 16))
        self.generate_indices(indices, scope.lower())

    def generate_indices(self, indices, description="selected pads"):
        if self.busy or not self.session:
            return
        indices = tuple(sorted(set(indices)))
        eligible = [i for i in indices if not self.session.pads[i].locked and i // 16 not in self.session.locked_banks]
        if not eligible:
            self.set_status("No unlocked pads in this selection. Select pads or unlock the bank first.")
            return
        self.stop()
        self.settings_changed()
        snapshot = snapshot_session(self.session)
        before = {i: audio_identity(self.session.by_id[self.session.pads[i].sample_id])
                  if self.session.pads[i].sample_id in self.session.by_id else None for i in eligible}
        def work():
            matcher = Matcher(snapshot)
            if not any(matcher.pool(i) for i in eligible):
                raise ValueError("No sounds match the active packs and filters. Choose active packs or turn off the favourites/recent-use filters.")
            result = regenerate_indices(snapshot, indices)
            self.library_store.record_used([result.by_id[result.pads[i].sample_id].path for i in eligible
                                           if result.pads[i].sample_id in result.by_id])
            return apply_library_marks(result, self.library_store)
        def done(session):
            changed = sum((audio_identity(session.by_id[session.pads[i].sample_id])
                           if session.pads[i].sample_id in session.by_id else None) != before[i] for i in eligible)
            self.session = session
            self.refresh()
            if changed == len(eligible):
                message = f"Changed {changed} pads in {description}. Other pads and locks were preserved."
            else:
                message = f"Changed {changed} of {len(eligible)} pads; {len(eligible) - changed} had no unused alternative. Other pads and locks were preserved."
            self.set_status(message)
            self.autosave()
        self.set_status(f"Finding sounds for {len(eligible)} pads…")
        self.run_job(work, done)

    def paint_pads(self):
        size = self.session.kit_size if self.session else (16 if self.kit_size_var.get() == "16 pads" else 8)
        target = self.session.target if self.session else ("sp404" if self.target_var.get() == "SP-404MKII" else "mpc")
        samples = self.session.by_id if self.session else {}
        short_roles = ("ALT KICK", "SHORT FILL", "RIM / CLAP", "CYMBAL", "MAIN KICK", "SNARE", "CLOSED HAT", "OPEN HAT",
                       "LOW PERC", "CLICK / KNOCK", "SHAKE / RATTLE", "RINGING", "FOLEY", "SCRAPE", "CHARACTER", "WILDCARD")
        bank_locked = bool(self.session and self.bank in self.session.locked_banks)
        for local, button in self.pad_buttons.items():
            index = self.bank * 16 + local
            pad = self.session.pads[index] if self.session else Assignment()
            sample = samples.get(pad.sample_id)
            title = Path(sample.path).stem if sample else "Empty"
            if len(title) > 18:
                title = title[:17] + "…"
            chosen = index in self.selected_pads
            mark = " · PICK" if chosen else " · LOCK" if pad.locked or bank_locked else ""
            if pad.warning:
                mark += " !"
            button.configure(text=f"{hardware_pad_name(index, target)}{mark}\n{short_roles[role_index(index, size)]}\n{title}",
                fg_color=SELECTED if index == self.selected else CORE_PAD if local < 8 else CREATIVE_PAD,
                text_color=T.ON_SELECTED if index == self.selected else TEXT,
                border_color=GREEN if chosen or index == self.selected else AMBER if pad.warning else BORDER,
                border_width=2 if chosen or index == self.selected else 1)
            self.pad_lock_vars[local].set(pad.locked)
            self.pad_select_vars[local].set(chosen)
            self.pad_lock_checks[local].configure(state="normal" if self.session and not self.busy and not bank_locked else "disabled")
            self.pad_select_checks[local].configure(state="normal" if self.session and not self.busy else "disabled")


    def kit_for_group(self, group):
        return self.bank if self.session and self.session.kit_size == 16 else self.bank * 2 + group

    def target_changed(self, _value=None):
        if self.busy:
            return
        if self.session:
            self.session.target = "sp404" if self.target_var.get() == "SP-404MKII" else "mpc"
            self.autosave()
        self.refresh()

    def kit_size_changed(self, _value=None):
        if self.busy:
            return
        size = 16 if self.kit_size_var.get() == "16 pads" else 8
        if self.session and self.session.kit_size != size:
            if size == 16:
                self.complete_to_16()
                return
            # Preserve every sample assignment; upper pads become another core kit.
            self.session.kit_size = 8
            refresh_warnings(self.session)
            self.autosave()
            self.set_status("8-pad mode selected. Existing assignments kept; rebuild unlocked pads for their new roles.")
        self.refresh()

    def complete_to_16(self):
        if self.busy or not self.session:
            self.kit_size_var.set("16 pads")
            return
        self.stop()
        snapshot = snapshot_session(self.session)
        changed = snapshot.kit_size != 16
        snapshot.kit_size = 16
        self.kit_size_var.set("16 pads")
        self.build_upper(snapshot, None if changed else self.bank)

    def refresh_upper(self):
        if self.busy or not self.session:
            return
        if self.session.kit_size != 16:
            self.set_status("Choose Complete to 16 first.")
            return
        self.stop()
        self.build_upper(snapshot_session(self.session), self.bank)

    def build_upper(self, snapshot, bank):
        indices = [index for index in range(64) if index % 16 >= 8 and (bank is None or index // 16 == bank)]
        eligible = [i for i in indices if not snapshot.pads[i].locked and i // 16 not in snapshot.locked_banks]
        def work():
            matcher = Matcher(snapshot)
            if eligible and not any(matcher.pool(i) for i in eligible):
                raise ValueError("No percussion matches the active packs and filters. Adjust Active packs & crates first.")
            result = regenerate_indices(snapshot, indices)
            self.library_store.record_used([result.by_id[result.pads[i].sample_id].path for i in eligible
                                           if result.pads[i].sample_id in result.by_id])
            return apply_library_marks(result, self.library_store)
        def done(session):
            self.session = session
            self.refresh()
            self.autosave()
            self.set_status("Upper eight refreshed with percussion and foley. Core drums and locked pads preserved.")
        self.set_status("Finding percussion and foley companions…")
        self.run_job(work, done)

    def manage_folder_tags(self):
        if self.busy:
            return
        if getattr(self, "tag_window", None) and self.tag_window.winfo_exists():
            self.tag_window.focus()
            return
        window = self.tag_window = ctk.CTkToplevel(self)
        window.title("Folder tags · no renaming needed")
        window.geometry("810x570")
        window.minsize(740, 530)
        window.transient(self)
        window.grab_set()
        window.grid_columnconfigure(1, weight=1)
        window.grid_rowconfigure(1, weight=1)
        self.label(window, "Teach the library what a whole folder contains", 19, GREEN, True).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(18, 12))
        rules_list = ctk.CTkScrollableFrame(window, width=260, fg_color=PANEL)
        rules_list.grid(row=1, column=0, sticky="nsew", padx=(20, 12), pady=(0, 16))
        form = ctk.CTkFrame(window, fg_color=PANEL)
        form.grid(row=1, column=1, sticky="nsew", padx=(0, 20), pady=(0, 16))
        folder_var, tags_var = tk.StringVar(), tk.StringVar()
        kind_var, recursive_var = tk.StringVar(value="Auto"), tk.BooleanVar(value=True)
        selected = {"folder": None}
        saved_rules = self.catalogue.list_folder_rules()
        self.label(form, "Folder", 12, MUTED).pack(anchor="w", padx=16, pady=(14, 5))
        ctk.CTkEntry(form, textvariable=folder_var).pack(fill="x", padx=16)
        def browse_rule():
            folder = filedialog.askdirectory(parent=window, title="Choose a folder to tag")
            if folder:
                folder_var.set(folder)
        self.button(form, "Browse folder", browse_rule).pack(fill="x", padx=16, pady=8)
        self.label(form, "Tags · separate with commas", 12, MUTED).pack(anchor="w", padx=16, pady=(8, 5))
        ctk.CTkEntry(form, textvariable=tags_var, placeholder_text="kitchen, utensils, percussion").pack(fill="x", padx=16)
        self.label(form, "Musical role", 12, MUTED).pack(anchor="w", padx=16, pady=(14, 5))
        ctk.CTkOptionMenu(form, variable=kind_var,
            values=["Auto", "foley", "perc", "kick", "snare", "rim", "clap", "hat", "open_hat", "cymbal", "fill", "exclude"],
            fg_color=CONTROL, button_color=CONTROL_HOVER).pack(fill="x", padx=16)
        ctk.CTkCheckBox(form, text="Include subfolders", variable=recursive_var,
                      checkbox_width=18, checkbox_height=18).pack(anchor="w", padx=16, pady=14)
        preview_label = self.label(form, "", 11, MUTED, justify="left", wraplength=365)
        preview_label.pack(anchor="w", padx=16, pady=(0, 10))
        self.label(form, "Tags inherit from parent folders. A more specific role wins. Auto uses the parent role or audio/filename classification. Rules apply to current and future files.",
                   11, MUTED, justify="left", wraplength=365).pack(anchor="w", padx=16, pady=(0, 12))

        def preview(*_):
            path = folder_var.get().strip().strip('"')
            if not path:
                preview_label.configure(text="Example: tag kitchenSamples as foley with kitchen, utensils, percussion.")
                return
            root = os.path.normcase(os.path.abspath(path))
            count = 0
            for sample in self.session.samples if self.session else []:
                parent = os.path.normcase(os.path.abspath(str(Path(sample.path).parent)))
                if parent == root or recursive_var.get() and parent.startswith(root + os.sep):
                    count += 1
            effective = self.catalogue.rules_for(Path(path) / "__preview__.wav", rules=saved_rules)
            inherited = ", ".join(effective["tags"]) or "none"
            preview_label.configure(text=f"Applies to {count:,} indexed sounds. Saved inherited tags: {inherited}. New files inherit this rule on the next update.")

        def select_rule(rule=None):
            selected["folder"] = rule["folder"] if rule else None
            folder_var.set(rule["folder"] if rule else "")
            tags_var.set(", ".join(rule["tags"]) if rule else "")
            kind_var.set(rule["kind"] or "Auto" if rule else "Auto")
            recursive_var.set(rule["recursive"] if rule else True)
            preview()

        def reload_rules():
            for child in rules_list.winfo_children():
                child.destroy()
            self.button(rules_list, "+ New folder rule", lambda: select_rule()).pack(fill="x", pady=(2, 10))
            for rule in self.catalogue.list_folder_rules():
                text = Path(rule["folder"]).name + "\n" + ", ".join(rule["tags"])[:34]
                self.button(rules_list, text, lambda r=rule: select_rule(r), height=54, anchor="w").pack(fill="x", pady=4)

        def apply_rules(message):
            window.destroy()
            if not self.session:
                self.set_status(message)
                return
            snapshot = snapshot_session(self.session)
            snapshot.samples = [copy.copy(sample) for sample in snapshot.samples]
            def done(session):
                self.session = session
                self.refresh()
                self.autosave()
                self.set_status(message + " Existing pad choices preserved; regenerate to use the new roles.")
            self.run_job(lambda: apply_folder_rules(snapshot, self.catalogue), done)

        def save_rule():
            folder = folder_var.get().strip().strip('"')
            if not folder or not Path(folder).is_absolute():
                messagebox.showerror("Folder tags", "Choose or enter an absolute folder path.", parent=window)
                return
            tags = [tag.strip().lower() for tag in tags_var.get().split(",") if tag.strip()]
            try:
                kind = None if kind_var.get() == "Auto" else kind_var.get()
                # A path edit moves this rule; it never changes the sample folder.
                self.catalogue.set_folder_rule(folder, tags, kind=kind, recursive=recursive_var.get())
                if selected["folder"] and os.path.normcase(os.path.abspath(selected["folder"])) != os.path.normcase(os.path.abspath(folder)):
                    self.catalogue.delete_folder_rule(selected["folder"])
                apply_rules("Folder rule saved.")
            except (OSError, ValueError) as exc:
                messagebox.showerror("Folder tags", str(exc), parent=window)

        def delete_rule():
            if selected["folder"]:
                self.catalogue.delete_folder_rule(selected["folder"])
                apply_rules("Folder rule removed.")

        actions = ctk.CTkFrame(form, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 16))
        self.button(actions, "Delete rule", delete_rule, width=100).pack(side="left")
        self.button(actions, "Save rule", save_rule, primary=True, width=120).pack(side="right")
        folder_var.trace_add("write", preview)
        recursive_var.trace_add("write", preview)
        reload_rules()
        select_rule()

    def change_bank(self, bank):
        self.bank = bank
        self.selected_pads.clear()
        self.selected = bank * 16 + self.selected % 16
        self.refresh()

    def select_pad(self, index, play=False):
        self.selected = index
        self.paint_pads()
        self.show_selected()
        if play:
            self.audition()

    def refresh(self):
        size = self.session.kit_size if self.session else (16 if self.kit_size_var.get() == "16 pads" else 8)
        target = self.session.target if self.session else ("sp404" if self.target_var.get() == "SP-404MKII" else "mpc")
        self.kit_size_var.set(f"{size} pads")
        self.target_var.set("MPC1000" if target == "mpc" else "SP-404MKII")
        if self.session and self.session.generation_sources is not None:
            active = self.session.generation_sources
            count = sum(source.startswith("ni:") for source in active)
            text = f"{count} NI packs" + (" + your samples" if "__user__" in active else "")
            if self.session.favourites_only:
                text += " · favourites"
            if self.session.unused_only:
                text += " · not used recently"
            self.active_sources_label.configure(text=text)
        else:
            self.active_sources_label.configure(text="All indexed sources")
        self.header_detail.configure(text=f"{self.target_var.get()} · {64 // size} kits × {size} pads")
        self.export_button.configure(text="Export MPC…" if target == "mpc" else "Export WAVs…")
        if target == "mpc":
            self.send_button.grid_remove()
        else:
            self.send_button.grid()
        self.export_hint.configure(text="SP export: 48 kHz / 16-bit WAV copies, numbered for the physical SP pads. Originals stay unchanged."
                                   if target != "mpc" else "MPC export: 44.1 kHz / 16-bit WAV copies. Closed and open hats choke within each kit.")
        if size == 16:
            self.group_bars[1].grid_remove()
            self.group_bars[0].grid_remove()
        else:
            self.group_bars[1].grid()
            self.group_bars[0].grid()
        for i, b in enumerate(self.bank_buttons):
            locked = bool(self.session and i in self.session.locked_banks)
            b.configure(text=f"{'ABCD'[i]} · LOCKED" if locked else "Bank " + "ABCD"[i],
                        fg_color=SELECTED if i == self.bank else CONTROL, text_color=T.ON_SELECTED if i == self.bank else TEXT)
        for group, lbl in self.kit_labels:
            lbl.configure(text="Core drums / lower 8" if size == 16 else f"KIT {self.bank * 2 + group + 1:02d} / 8 PADS")
        samples = self.session.by_id if self.session else {}
        if self.session:
            filled, unique, substitutions = overview(self.session)
            bank_filled = sum(bool(p.sample_id) for p in self.session.pads[self.bank * 16:self.bank * 16 + 16])
            self.summary_label.configure(text=f"Bank {'ABCD'[self.bank]} · {bank_filled}/16 pads · {len(self.session.samples):,} library sounds")
            counts = Counter(s.kind for s in self.session.samples)
            self.folder_label.configure(text=Path(self.session.root).name)
            self.count_label.configure(text="\n".join([
                f"{counts['kick']:>3} kicks                 {counts['snare']:>3} snares",
                f"{counts['rim'] + counts['clap']:>3} rims / claps       {counts['fill']:>3} fills",
                f"{counts['hat']:>3} closed hats         {counts['open_hat']:>3} open hats",
                f"{counts['cymbal']:>3} cymbals          {counts['perc']:>3} percussion",
                f"{counts['foley']:>3} foley sounds",
                f"\n{len(self.catalogue.list_folder_rules())} saved folder rules"] ))
        self.paint_pads()
        self.update_generation_controls()
        self.sync_live_controls()
        self.show_selected()
        self.schedule_live_reload()

    def show_selected(self):
        i = self.selected
        pad = self.session.pads[i] if self.session else Assignment()
        s = self.session.by_id.get(pad.sample_id) if self.session else None
        self.candidate_preview_label.configure(text="Trying a sound leaves your pad unchanged.", text_color=MUTED)
        size = self.session.kit_size if self.session else (16 if self.kit_size_var.get() == "16 pads" else 8)
        target = self.session.target if self.session else ("sp404" if self.target_var.get() == "SP-404MKII" else "mpc")
        self.pad_title.configure(text=f"{hardware_pad_name(i, target)}  /  {role_names(size)[role_index(i, size)]}", wraplength=248, justify="left")
        self.sample_title.configure(text=Path(s.path).stem if s else "Empty pad")
        source_text = ""
        if s:
            pack = s.features.get("pack")
            source_text = ((pack + " · " + s.features.get("category", "Drums") + "\n") if pack else "") + s.relative
            if s.tags:
                source_text += "\nTags: " + ", ".join(s.tags)
        self.source_label.configure(text=source_text)
        self.reason_label.configure(text=pad.reason)
        self.warning_label.configure(text=pad.warning)
        self.favourite_button.configure(text="Unfavourite" if s and s.features.get("favourite") else "Favourite",
                                        state="normal" if s and not self.busy else "disabled")
        self.hide_button.configure(text="Unhide" if s and s.features.get("hidden") else "Hide sound",
                                  state="normal" if s and not self.busy else "disabled")
        self.waveform.delete("all")
        width = max(200, self.waveform.winfo_width())
        if s:
            self.feature_label.configure(text=f"{s.kind.replace('_', ' ').title()} · {s.duration:.2f}s · {'stereo' if s.channels == 2 else 'mono'}\n"
                f"{'Labelled' if s.confidence >= .7 else 'Audio guess — review'} · {s.features['centroid'] / 1000:.1f} kHz brightness\n"
                f"{s.features['decay']:.2f}s to 95% energy")
            wave = s.features["waveform"]
            maximum = max(max(wave), 1e-8)
            for k, value in enumerate(wave):
                x = (k + .5) * width / len(wave)
                h = max(1, value / maximum * 29)
                self.waveform.create_line(x, 38 - h, x, 38 + h, fill=GREEN, width=1)
        else:
            self.feature_label.configure(text="No sample assigned")
        self.candidates = {}
        self.candidate_keys = []
        self.rank_serial += 1
        serial = self.rank_serial
        if self.session and not self.busy:
            snapshot = snapshot_session(self.session)
            def work():
                with self.rank_lock:
                    if serial != self.rank_serial:
                        return
                    try:
                        candidates = Matcher(snapshot).rank(i, browse=True, limit=200)
                        self.events.put(("candidates", (serial, i, candidates, snapshot.samples, snapshot._matching_cache)))
                    except Exception as exc:
                        self.events.put(("audio_error", str(exc)))
            threading.Thread(target=work, daemon=True).start()
        self.keep_sound_button.configure(state="disabled")
        self.candidate_menu.configure(values=["Finding alternatives…"])
        self.candidate_var.set("Finding alternatives…")
        self.candidate_count_label.configure(text="Finding alternatives in the background…" if self.session else "Update the library first")

    def set_candidates(self, candidates):
        self.candidates, self.candidate_keys = {}, []
        if self.session:
            for n, (_, candidate, reason) in enumerate(candidates):
                kind = candidate.kind.replace("_", " ")
                key = f"{n + 1:03d}  {Path(candidate.path).stem[:22]}  · {kind}"
                self.candidates[key] = (candidate, reason)
                self.candidate_keys.append(key)
        self.keep_sound_button.configure(state="normal" if self.candidate_keys and not self.busy else "disabled")
        values = self.candidate_keys or ["No alternatives for this role"]
        self._suppress_candidate_preview = True
        try:
            self.candidate_menu.configure(values=values)
            self.candidate_var.set(values[0])
        finally:
            self._suppress_candidate_preview = False
        if self.candidate_keys:
            role = role_index(self.selected, self.session.kit_size)
            primary = sum(1 for key in self.candidate_keys if self.candidates[key][0].kind in ROLE_POOLS[role])
            self.candidate_count_label.configure(
                text=f"{len(self.candidate_keys)} sounds · {primary} role matches · ← → previews")
        else:
            self.candidate_count_label.configure(text="No alternatives for this role")

    def audio_job(self, work):
        if self.busy:
            return
        self.preview_serial += 1
        serial = self.preview_serial
        def worker():
            try:
                with self.preview_lock:
                    if serial == self.preview_serial:
                        work()
            except Exception as exc:
                self.events.put(("audio_error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def audition(self):
        if self.session:
            if self.live_engine and self.live_engine.running and self.live_engine.loaded_bank == self.bank:
                self.live_engine.trigger(self.selected % 16, 127)
                self.flash_pad(self.selected % 16)
                return
            sample = self.session.by_id.get(self.session.pads[self.selected].sample_id)
            if sample:
                self.audio_job(lambda: self.player.play(sample))
                self.candidate_preview_label.configure(text="Hearing pad: " + Path(sample.path).stem, text_color=GREEN)
                self.reason_label.configure(text=self.session.pads[self.selected].reason)
                self.warning_label.configure(text=self.session.pads[self.selected].warning)

    def hear_kit(self, group, drums_only=False):
        if self.session and not self.busy:
            self.settings_changed()
            snapshot = snapshot_session(self.session)
            kit = self.kit_for_group(group)
            self.audio_job(lambda: self.player.groove(snapshot, kit, drums_only=drums_only))
            self.set_status(f"Playing kit {kit + 1} at {self.session.bpm} BPM · {'drums only' if drums_only else 'full groove'}. Esc stops.")

    def stop(self):
        if self.live_engine:
            self.live_engine.silence()
        self.preview_serial += 1
        serial = self.preview_serial
        # Serialize Stop with a pending load so a delayed preview cannot restart audio.
        def worker():
            try:
                with self.preview_lock:
                    if serial == self.preview_serial:
                        self.player.stop()
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def key_preview(self, event):
        if isinstance(self.focus_get(), (tk.Entry, tk.Text)):
            return
        self.audition()
        return "break"

    def key_step_candidate(self, event):
        if isinstance(self.focus_get(), (tk.Entry, tk.Text)):
            return
        self.step_candidate(-1 if event.keysym == "Left" else 1)
        return "break"

    def on_candidate_chosen(self, _value=None):
        if self._suppress_candidate_preview:
            return
        self.preview_candidate()

    def step_candidate(self, delta):
        if not self.candidate_keys or self.busy:
            return
        try:
            index = self.candidate_keys.index(self.candidate_var.get())
        except ValueError:
            index = 0
        index = (index + delta) % len(self.candidate_keys)
        self._suppress_candidate_preview = True
        try:
            self.candidate_var.set(self.candidate_keys[index])
        finally:
            self._suppress_candidate_preview = False
        self.preview_candidate()

    def toggle_pad_lock(self, local):
        if self.session and not self.busy and self.bank not in self.session.locked_banks:
            index = self.bank * 16 + local
            self.session.pads[index].locked = self.pad_lock_vars[local].get()
            self.selected = index
            self.paint_pads()
            self.update_generation_controls()
            self.show_selected()
            self.autosave()

    def preview_candidate(self):
        choice = self.candidates.get(self.candidate_var.get())
        if choice:
            sample = choice[0]
            self.audio_job(lambda s=sample: self.player.play(s))
            self.candidate_preview_label.configure(text="Trying: " + Path(sample.path).stem + "\nUse & lock sound to keep it.", text_color=GREEN)
            # Show reason for the hovered alternative without applying it yet.
            self.reason_label.configure(text="Preview: " + choice[1])
            self.warning_label.configure(
                text="" if sample.kind in ROLE_POOLS[role_index(self.selected, self.session.kit_size)]
                else f"Different kind ({sample.kind.replace('_', ' ')}) — still usable if you like it")

    def use_candidate(self):
        if self.session and self.bank in self.session.locked_banks:
            self.set_status("Unlock this bank before changing its sounds.")
            return
        choice = self.candidates.get(self.candidate_var.get())
        if choice and self.session and not self.busy:
            self.session.pads[self.selected] = Assignment(choice[0].id, "Chosen by you. " + choice[1], locked=True)
            self.library_store.record_used([choice[0].path])
            self.session = apply_library_marks(self.session, self.library_store)
            self.refresh()
            self.autosave()
            self.set_status("Replacement applied and locked. Unlock the pad to regenerate it later.")

    def clear_pad(self):
        if self.session and self.bank in self.session.locked_banks:
            self.set_status("Unlock this bank before changing its sounds.")
            return
        if self.session and not self.busy:
            # Leave unlocked so Rebuild / Regenerate can refill this pad.
            # Lock the empty pad if you want it to stay empty.
            self.session.pads[self.selected] = Assignment(None, "Cleared by you", locked=False)
            self.refresh()
            self.autosave()
            self.set_status("Pad cleared. Lock the empty pad if you want it to stay empty.")

    def autosave(self):
        if self._autosave_after:
            self.after_cancel(self._autosave_after)
        self._autosave_after = self.after(200, self.flush_autosave)

    def flush_autosave(self):
        self._autosave_after = None
        if self.session:
            try:
                path = self.data_dir / "last-session.json"
                temporary = self.data_dir / "last-session.tmp"
                temporary.write_text(json.dumps(project_dict(self.session)), encoding="utf-8")
                temporary.replace(path)
            except OSError:
                self.set_status("Session could not be autosaved. Use Save session to choose another location.")

    def save_session(self):
        if not self.session or self.busy:
            return
        path = filedialog.asksaveasfilename(title="Save editable kit session", defaultextension=".json",
                    filetypes=[("Crate Builder session", "*.json")], initialfile="MyCrate.session.json", parent=self)
        if path:
            self.settings_changed()
            try:
                Path(path).write_text(json.dumps(project_dict(self.session), indent=2), encoding="utf-8")
                self.set_status("Session saved. It references your samples in their original locations.")
            except OSError as exc:
                messagebox.showerror("Could not save", str(exc), parent=self)

    def open_session(self, path=None):
        if self.busy:
            return
        if path is None:
            path = filedialog.askopenfilename(title="Open kit session", filetypes=[("Crate Builder session", "*.json")], parent=self)
        if not path:
            return
        try:
            session = Session.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
            self.stop()
            self.session = session
            self.selected_pads.clear()
            self.player._cache.clear()
            self.workspace_tabs.set("Kit")
            self.path_var.set(session.root)
            self.style_var.set(session.style)
            self.bpm_var.set(str(session.bpm))
            self.sub_var.set(session.allow_substitutes)
            self.surprise_var.set(session.surprise)
            self.refresh()
            self.set_status("Session opened. Restoring your indexed library from the local catalogue…")
            def restore():
                restored = snapshot_session(session)
                known = {}
                for source in dict.fromkeys([session.root, *session.sources]):
                    for sample in self.catalogue.cached_samples(source):
                        known[sample.id] = sample
                if known:
                    restored = merge_library_session(restored, Session(session.root, list(known.values()), sources=session.sources))
                return self.register_library_samples(apply_folder_rules(restored, self.catalogue))
            def done(restored):
                self.session = restored
                self.refresh()
                self.set_status(f"Session opened · {len(restored.samples):,} indexed sounds. Update library to check new or changed files.")
            self.run_job(restore, done)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror("Could not open session", str(exc), parent=self)

    def export(self):
        if not self.session or self.busy:
            self.set_status("Analyse a sample folder first.")
            return
        if self.session.target != "mpc":
            self.export_kit(1 if self.session.kit_size == 8 and self.selected % 16 >= 8 else 0)
            return
        self.settings_changed()
        folder = filedialog.askdirectory(title="Choose where to save the MPC program folder", initialdir=str(app_dir()), parent=self)
        if not folder:
            return
        self.stop()
        snapshot = snapshot_session(self.session)
        name = self.name_var.get()
        def done(result):
            output, validation = result
            self.set_status(f"Export verified: {validation['assigned_pads']}/64 pads, {validation['sample_memory_mb']} MB. Copy the whole folder to your MPC.")
            self.autosave()
            messagebox.showinfo("Ready for your MPC", f"Saved to:\n{output}\n\nCopy the whole folder to your MPC card, then load the .PGM with its samples.\n\n"
                                f"{validation['assigned_pads']}/64 pads · {validation['unique_wavs']} WAVs · {validation['sample_memory_mb']} MB\n\n"
                                "Review LOAD_ME.txt for substitutions. Computer checks passed; physical MPC testing is still required.", parent=self)
            self.open_output_folder(output)
        self.run_job(lambda: export_program(snapshot, folder, name, self.progress), done, close_safe=False)

    def export_kit(self, group):
        if not self.session or self.busy:
            self.set_status("Analyse a sample folder first.")
            return
        kit = self.kit_for_group(group)
        size = self.session.kit_size
        if not any(self.session.pads[kit * size + slot].sample_id for slot in range(size)):
            self.set_status(f"Kit {kit + 1} has no samples to export.")
            return
        self.settings_changed()
        folder = filedialog.askdirectory(title=f"Choose where to save kit {kit + 1:03d}", initialdir=str(app_dir()), parent=self)
        if not folder:
            return
        self.stop()
        snapshot = snapshot_session(self.session)
        def done(result):
            output, info = result
            self.set_status(
                f"Exported kit {info['kit']:03d}: {info['files']} WAVs"
                + (f", {info['skipped_empty']} empty pads skipped" if info["skipped_empty"] else "")
                + f". Saved to {output.name}."
            )
            self.autosave()
            messagebox.showinfo(
                "Kit exported",
                f"Saved to:\n{output}\n\n"
                f"{info['files']} WAV file(s), numbered for {self.target_var.get()}.\n"
                "Review the included pad map when loading your sampler. Empty pads were skipped.",
                parent=self,
            )
            self.open_output_folder(output)
        self.run_job(lambda: export_kit(snapshot, kit, folder, self.progress), done, close_safe=False)

    def show_help(self):
        notes = "\n".join(self.session.scan_notes) if self.session else "No folder analysed yet."
        messagebox.showinfo("Crate Builder — how it works", "1. Choose your sample folder, then Update library. New/changed audio is analysed locally.\n"
            "2. Tag folders teaches the library roles and tags without renaming files. Children inherit tags; specific roles win.\n"
            "3. Regenerate builds kits. Library updates and tagging preserve current pad choices and locks.\n"
            "4. 8-pad mode gives two core kits per bank. 16-pad mode gives one kit with percussion and foley above the drums.\n"
            "5. Complete to 16 keeps lower drums; Refresh upper 8 finds new companions in this bank.\n"
            "6. Select your sampler for physical pad labels and export format. SP WAV export uses 48 kHz / 16-bit; MPC uses 44.1 kHz / 16-bit.\n"
            "7. Use sound locks a replacement. Esc stops playback. Drums only and Hear groove compare the foundation and full kit.\n\n"
            "Matching considers labels, tags, brightness, decay and attack. Review audio guesses and amber substitution notes. "
            "It does not extract individual hits from long recordings or time-stretch fills. Exports are new copies; originals are untouched.\n\n" + notes[:2200], parent=self)

    def close_app(self):
        self.cancel_event.set()
        if self.busy:
            self.closing = True
            self.set_status("Finishing the current operation safely, then closing...")
            return
        self.stop()
        self.disable_live_preview(quiet=True)
        if not self.busy:
            if self._autosave_after:
                self.after_cancel(self._autosave_after)
            self.flush_autosave()
        self._closed = True
        self.destroy()
