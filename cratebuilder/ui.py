import json
import os
import queue
import sys
import threading
import tkinter as tk
from collections import Counter
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .audio import scan_folder
from .converter import convert_folder
from .matching import Matcher, STYLES, overview, refresh_warnings
from .model import Assignment, ROLES, Session, pad_name
from .pgm import export_program
from .preview import Player

BG, PANEL, INSET = "#121512", "#1D231E", "#161B17"
TEXT, MUTED, GREEN, AMBER = "#EDF1E9", "#9AA79B", "#D0ED92", "#E6B989"
def app_dir():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


class App(ctk.CTk):
    def __init__(self, session_path=None):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")
        super().__init__()
        self.title("Crate Builder · MPC1000")
        self.geometry("1280x900")
        self.minsize(1120, 810)
        self.configure(fg_color=BG)
        self.session = None
        self.bank, self.selected = 0, 4
        self.busy = False
        self.closing = False
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.player = Player()
        self.preview_serial = 0
        self.preview_lock = threading.Lock()
        self.data_dir = Path(os.environ["CRATEBUILDER_DATA_DIR"]) if os.environ.get("CRATEBUILDER_DATA_DIR") else Path(os.environ.get("LOCALAPPDATA", str(app_dir()))) / "CrateBuilder"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path_var = tk.StringVar(value="")
        self.style_var = tk.StringVar(value="Balanced")
        self.bpm_var = tk.StringVar(value="90")
        self.name_var = tk.StringVar(value="")
        self.sub_var = tk.BooleanVar(value=True)
        self.candidate_var = tk.StringVar(value="Scan a folder first")
        self.candidates = {}
        self._layout()
        self.protocol("WM_DELETE_WINDOW", self.close_app)
        self.bind("<space>", self.key_preview)
        self.bind("<Escape>", lambda _: self.stop())
        self.after(80, self.poll)
        starter = Path(session_path) if session_path else self.data_dir / "last-session.json"
        if starter.is_file():
            self.after(150, lambda: self.open_session(starter))
        else:
            self.refresh()

    def label(self, parent, text, size=13, color=TEXT, bold=False, **kw):
        return ctk.CTkLabel(parent, text=text, text_color=color,
                            font=ctk.CTkFont(family="Segoe UI", size=size, weight="bold" if bold else "normal"), **kw)

    def button(self, parent, text, command, primary=False, **kw):
        return ctk.CTkButton(parent, text=text, command=command, height=34,
                            fg_color=GREEN if primary else "#303B31", text_color=BG if primary else TEXT,
                            hover_color="#B5D576" if primary else "#435245", corner_radius=7, **kw)

    def _layout(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 16))
        head.grid_columnconfigure(1, weight=1)
        self.label(head, "CRATE BUILDER", 26, GREEN, True).grid(row=0, column=0, sticky="w")
        self.label(head, "MPC1000   /   4 BANKS   /   8 KITS   /   64 PADS", 11, MUTED).grid(row=1, column=0, sticky="w")
        self.label(head, "Program name", 11, MUTED).grid(row=0, column=2, sticky="w", padx=(0, 10))
        ctk.CTkEntry(head, textvariable=self.name_var, width=155, height=34,
                     placeholder_text="e.g. MYKIT").grid(row=1, column=2, padx=(0, 10))
        self.export_button = self.button(head, "Export for MPC  →", self.export, primary=True, width=170)
        self.export_button.grid(row=1, column=3)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=24)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        left = ctk.CTkScrollableFrame(body, width=234, fg_color=PANEL, corner_radius=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self.label(left, "01   YOUR CRATE", 12, GREEN, True).pack(anchor="w", padx=10, pady=(12, 12))
        self.path_entry = ctk.CTkEntry(left, textvariable=self.path_var, height=34, width=212, placeholder_text="Paste sample folder path")
        self.path_entry.pack(fill="x", padx=10)
        self.folder_label = self.label(left, "Choose a folder of drum samples.", 11, MUTED, wraplength=205, justify="left")
        self.folder_label.pack(anchor="w", padx=10, pady=(8, 10))
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=10)
        self.button(row, "Browse", self.browse, width=96).pack(side="left")
        self.button(row, "+ Folder", self.add_folder, width=96).pack(side="right")
        self.scan_button = self.button(left, "Analyse & build 8 kits", self.scan, primary=True)
        self.scan_button.pack(fill="x", padx=10, pady=10)
        self.convert_button = self.button(left, "Convert folder to MPC WAV", self.convert_audio_folder)
        self.convert_button.pack(fill="x", padx=10, pady=(0, 10))
        self.count_label = self.label(left, "WAV  ·  AIFF  ·  FLAC\nSubfolders included. Audio stays local.", 12, MUTED, justify="left", wraplength=208)
        self.count_label.pack(anchor="w", padx=10, pady=(2, 12))
        self.label(left, "02   MATCHING", 12, GREEN, True).pack(anchor="w", padx=10, pady=(16, 10))
        ctk.CTkOptionMenu(left, variable=self.style_var, values=list(STYLES), fg_color="#303B31", button_color="#435245",
                          command=lambda _: self.settings_changed()).pack(fill="x", padx=10)
        tempo = ctk.CTkFrame(left, fg_color="transparent")
        tempo.pack(fill="x", padx=10, pady=12)
        self.label(tempo, "Preview tempo", 12, MUTED).pack(side="left")
        self.bpm_entry = ctk.CTkEntry(tempo, textvariable=self.bpm_var, width=54)
        self.bpm_entry.pack(side="right")
        self.bpm_entry.bind("<FocusOut>", lambda _: self.settings_changed())
        ctk.CTkCheckBox(left, text="Allow missing-role substitutes", variable=self.sub_var, font=ctk.CTkFont(size=11),
                        checkbox_width=17, checkbox_height=17, command=self.settings_changed).pack(anchor="w", padx=10, pady=(2, 8))
        self.label(left, "For example: open hats if cymbals are missing. Every substitution is marked.", 11, MUTED, wraplength=206, justify="left").pack(anchor="w", padx=10)
        self.shuffle_button = self.button(left, "Regenerate unlocked pads", self.shuffle)
        self.shuffle_button.pack(fill="x", padx=10, pady=(16, 8))
        self.label(left, "Matching uses instrument labels, shared names and relative tone / decay / attack. Your ears make the final call.", 11, MUTED, wraplength=206, justify="left").pack(anchor="w", padx=10, pady=(0, 12))
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(12, 6))
        self.button(row, "Save session", self.save_session, width=99).pack(side="left")
        self.button(row, "Open", lambda: self.open_session(), width=89).pack(side="right")
        self.button(left, "Scan notes & help", self.show_help).pack(fill="x", padx=10, pady=(4, 14))

        center = ctk.CTkFrame(body, fg_color="transparent")
        center.grid(row=0, column=1, sticky="nsew")
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(2, weight=1)
        bar = ctk.CTkFrame(center, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.bank_buttons = []
        for i, name in enumerate("ABCD"):
            b = self.button(bar, "BANK " + name, lambda n=i: self.change_bank(n), width=105)
            b.pack(side="left", expand=True, fill="x", padx=(0, 7 if i < 3 else 0))
            self.bank_buttons.append(b)
        self.summary_label = self.label(center, "Build your first crate", 12, MUTED, anchor="w")
        self.summary_label.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.pad_area = ctk.CTkFrame(center, fg_color=PANEL, corner_radius=12)
        self.pad_area.grid(row=2, column=0, sticky="nsew")
        for col in range(4):
            self.pad_area.grid_columnconfigure(col, weight=1, uniform="pads")
        for row_num in (1, 2, 4, 5):
            self.pad_area.grid_rowconfigure(row_num, weight=1, uniform="pads")
        self.kit_labels, self.pad_buttons = [], {}
        self.pad_lock_vars, self.pad_lock_checks = {}, {}
        for group, row_num in ((1, 0), (0, 3)):
            group_bar = ctk.CTkFrame(self.pad_area, fg_color="transparent")
            group_bar.grid(row=row_num, column=0, columnspan=4, sticky="ew", padx=14, pady=(14, 8))
            lbl = self.label(group_bar, "", 12, AMBER if group else GREEN, True)
            lbl.pack(side="left")
            self.kit_labels.append((group, lbl))
            self.button(group_bar, "Hear groove", lambda g=group: self.hear_kit(g), width=95).pack(side="right")
            self.button(group_bar, "Rebuild", lambda g=group: self.shuffle(self.bank * 2 + g), width=65).pack(side="right", padx=7)
        for local in range(16):
            row_num = {3: 1, 2: 2, 1: 4, 0: 5}[local // 4]
            tile = ctk.CTkFrame(self.pad_area, fg_color="#252D26", corner_radius=8)
            tile.grid(row=row_num, column=local % 4, sticky="nsew", padx=(10 if local % 4 == 0 else 4, 10 if local % 4 == 3 else 4), pady=(2, 10))
            tile.grid_columnconfigure(0, weight=1)
            tile.grid_rowconfigure(0, weight=1)
            button = ctk.CTkButton(tile, text="", command=lambda n=local: self.select_pad(self.bank * 16 + n, play=True),
                                   corner_radius=8, border_width=1, font=ctk.CTkFont(family="Segoe UI", size=12),
                                   text_color=TEXT, fg_color="#303A2E", hover_color="#475340", width=80)
            button.grid(row=0, column=0, sticky="nsew")
            keep = tk.BooleanVar(value=False)
            check = ctk.CTkCheckBox(tile, text="Keep on rebuild", variable=keep, command=lambda n=local: self.toggle_pad_lock(n),
                                    font=ctk.CTkFont(size=10), checkbox_width=15, checkbox_height=15,
                                    height=23, fg_color=GREEN, hover_color="#B5D576")
            check.grid(row=1, column=0, sticky="w", padx=8, pady=(3, 5))
            self.pad_buttons[local] = button
            self.pad_lock_vars[local] = keep
            self.pad_lock_checks[local] = check
        footer = ctk.CTkFrame(center, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", pady=(10, 4))
        self.label(footer, "Hardware view: pad 01 at bottom left\nClick a pad to hear it · Space to replay · Esc to stop", 11, MUTED, justify="left").pack(side="left")
        self.button(footer, "Stop ■", self.stop, width=76).pack(side="right")

        right = ctk.CTkScrollableFrame(body, width=245, fg_color=PANEL, corner_radius=12)
        right.grid(row=0, column=2, sticky="nsew", padx=(16, 0))
        self.pad_title = self.label(right, "A05  /  MAIN KICK", 14, GREEN, True)
        self.pad_title.pack(anchor="w", padx=12, pady=(12, 10))
        self.sample_title = self.label(right, "Your sound goes here", 19, TEXT, True, wraplength=224, justify="left")
        self.sample_title.pack(anchor="w", padx=12, pady=(0, 4))
        self.source_label = self.label(right, "", 10, MUTED, wraplength=222, justify="left")
        self.source_label.pack(anchor="w", padx=12, pady=(0, 12))
        self.waveform = tk.Canvas(right, height=76, width=218, bg=INSET, highlightthickness=0)
        self.waveform.pack(fill="x", padx=12, pady=(0, 12))
        self.feature_label = self.label(right, "", 12, MUTED, justify="left", wraplength=222)
        self.feature_label.pack(anchor="w", padx=12, pady=(0, 8))
        self.reason_label = self.label(right, "Choose a folder to start.", 12, TEXT, wraplength=220, justify="left")
        self.reason_label.pack(anchor="w", padx=12, pady=(4, 8))
        self.warning_label = self.label(right, "", 11, AMBER, wraplength=220, justify="left")
        self.warning_label.pack(anchor="w", padx=12, pady=(2, 8))
        self.button(right, "▶  Audition pad", self.audition).pack(fill="x", padx=12, pady=(2, 10))
        self.label(right, "TRY ANOTHER SOUND", 11, GREEN, True).pack(anchor="w", padx=12, pady=(10, 8))
        self.candidate_menu = ctk.CTkOptionMenu(right, values=["Scan a folder first"], variable=self.candidate_var,
                                              fg_color="#303B31", button_color="#435245", font=ctk.CTkFont(size=11),
                                              dynamic_resizing=False, width=216)
        self.candidate_menu.pack(fill="x", padx=12)
        self.label(right, "Alternatives ranked for this kit", 10, MUTED).pack(anchor="w", padx=12, pady=(6, 8))
        row = ctk.CTkFrame(right, fg_color="transparent")
        row.pack(fill="x", padx=12)
        self.button(row, "Preview", self.preview_candidate, width=95).pack(side="left")
        self.button(row, "Use sound", self.use_candidate, primary=True, width=101).pack(side="right")
        self.button(right, "Clear pad", self.clear_pad).pack(fill="x", padx=12, pady=(10, 12))
        self.label(right, "Export: original lengths, 16-bit / 44.1 kHz WAV. No EQ or pitch changes. Closed and open hats choke within each kit.", 11, MUTED, wraplength=218, justify="left").pack(anchor="w", padx=12, pady=6)

        status_frame = ctk.CTkFrame(self, fg_color="transparent")
        status_frame.grid(row=2, column=0, sticky="ew", padx=24, pady=(10, 14))
        status_frame.grid_columnconfigure(0, weight=1)
        self.status_label = self.label(status_frame, "Ready. Your original samples are never changed.", 12, MUTED, anchor="w")
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.cancel_button = self.button(status_frame, "Cancel scan", lambda: self.cancel_event.set(), width=94)
        self.cancel_button.grid(row=0, column=1, padx=(12, 0))
        self.cancel_button.configure(state="disabled")
        self.progress_bar = ctk.CTkProgressBar(status_frame, progress_color=GREEN, height=3)
        self.progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.progress_bar.set(0)

    def set_status(self, message):
        self.status_label.configure(text=message[:155])

    def progress(self, message, value):
        self.events.put(("progress", (message, value)))

    def run_job(self, work, done, cancellable=False):
        if self.busy:
            return
        self.busy = True
        self.cancel_event.clear()
        for widget in (self.scan_button, self.convert_button, self.shuffle_button, self.export_button):
            widget.configure(state="disabled")
        self.cancel_button.configure(state="normal" if cancellable else "disabled")
        def worker():
            try:
                result = work()
                self.events.put(("done", (done, result)))
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
                    if self.closing:
                        self.close_app()
                        return
                    for widget in (self.scan_button, self.convert_button, self.shuffle_button, self.export_button):
                        widget.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    if event == "error":
                        self.set_status(payload)
                        if payload != "Scan cancelled":
                            messagebox.showerror("Crate Builder", payload, parent=self)
                    else:
                        payload[0](payload[1])
                elif event == "audio_error":
                    self.set_status("Audio preview unavailable: " + payload)
        except queue.Empty:
            pass
        self.after(70, self.poll)

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
            os.startfile(output)
        self.run_job(lambda: convert_folder(source, destination, self.progress, self.cancel_event), done, cancellable=True)

    def settings_changed(self):
        if self.session and not self.busy:
            self.session.style = self.style_var.get()
            self.session.allow_substitutes = self.sub_var.get()
            try:
                self.session.bpm = max(40, min(200, int(self.bpm_var.get())))
            except ValueError:
                self.session.bpm = 90
            self.bpm_var.set(str(self.session.bpm))
            self.set_status("Settings updated. Regenerate unlocked pads to apply matching changes.")

    def scan(self):
        if self.busy:
            return
        path, style, substitutes = self.path_var.get(), self.style_var.get(), self.sub_var.get()
        try:
            bpm = max(40, min(200, int(self.bpm_var.get())))
        except ValueError:
            bpm = 90
        self.stop()
        def work():
            result = scan_folder(path, self.data_dir / "cache", self.progress, self.cancel_event)
            result.style, result.bpm, result.allow_substitutes = style, bpm, substitutes
            Matcher(result).build()
            return result
        self.run_job(work, self.scan_done, cancellable=True)

    def scan_done(self, session):
        self.session = session
        self.player._cache.clear()
        self.refresh()
        self.set_status("Built 8 kits. Audition the pads and review amber substitution notes before exporting.")
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
        def done(result):
            existing = self.session.by_id
            added = [s for s in result.samples if s.id not in existing]
            self.session.samples.extend(added)
            self.session.scan_notes.extend(result.scan_notes)
            self.refresh()
            self.set_status(f"Added {len(added)} sounds. Regenerate to include them; locked pads stay in place.")
            self.autosave()
        self.run_job(lambda: scan_folder(folder, self.data_dir / "cache", self.progress, self.cancel_event), done, cancellable=True)

    def shuffle(self, kit=None):
        if self.busy or not self.session:
            return
        self.stop()
        self.settings_changed()
        self.session.seed += 1
        Matcher(self.session).build(kit)
        self.refresh()
        self.set_status(f"Rebuilt {'all eight kits' if kit is None else 'kit ' + str(kit + 1)}. Locked pads were preserved.")
        self.autosave()

    def change_bank(self, bank):
        self.bank = bank
        self.selected = bank * 16 + self.selected % 16
        self.refresh()

    def select_pad(self, index, play=False):
        self.selected = index
        self.refresh()
        if play:
            self.audition()

    def refresh(self):
        for i, b in enumerate(self.bank_buttons):
            b.configure(fg_color=GREEN if i == self.bank else "#303B31", text_color=BG if i == self.bank else TEXT)
        for group, lbl in self.kit_labels:
            lbl.configure(text=f"KIT {self.bank * 2 + group + 1:02d}   /   PADS {'09–16' if group else '01–08'}")
        samples = self.session.by_id if self.session else {}
        if self.session:
            filled, unique, substitutions = overview(self.session)
            self.summary_label.configure(text=f"{filled}/64 assigned   ·   {unique} unique sounds   ·   {substitutions} substitutions")
            counts = Counter(s.kind for s in self.session.samples)
            self.folder_label.configure(text=Path(self.session.root).name)
            self.count_label.configure(text="\n".join([
                f"{counts['kick']:>3} kicks                 {counts['snare']:>3} snares",
                f"{counts['rim'] + counts['clap']:>3} rims / claps       {counts['fill']:>3} fills",
                f"{counts['hat']:>3} closed hats         {counts['open_hat']:>3} open hats",
                f"{counts['cymbal']:>3} cymbals",
                "\nNo cymbals found. Use + Folder to add some." if not counts['cymbal'] else "\nAll main drum categories available."] ))
        for local, button in self.pad_buttons.items():
            i = self.bank * 16 + local
            pad = self.session.pads[i] if self.session else Assignment()
            sample = samples.get(pad.sample_id)
            title = Path(sample.path).stem if sample else "—"
            if len(title) > 20:
                title = title[:19] + "…"
            short_roles = ("ALT KICK", "ROLL / FILL", "ALT SNARE", "CYMBAL", "MAIN KICK", "MAIN SNARE", "HI-HAT", "OPEN HAT")
            mark = "  !" if pad.warning else ""
            mark += "  L" if pad.locked else ""
            button.configure(text=f"{pad_name(i)}{mark}\n{short_roles[i % 8]}\n\n{title}",
                fg_color=("#3C4835" if local < 8 else "#443D32") if i == self.selected else ("#2B352B" if local < 8 else "#342F28"),
                border_color=GREEN if i == self.selected else AMBER if pad.warning else "#3E473C",
                border_width=2 if i == self.selected else 1)
            self.pad_lock_vars[local].set(pad.locked)
            self.pad_lock_checks[local].configure(state="normal" if self.session else "disabled")
        self.show_selected()

    def show_selected(self):
        i = self.selected
        pad = self.session.pads[i] if self.session else Assignment()
        s = self.session.by_id.get(pad.sample_id) if self.session else None
        self.pad_title.configure(text=f"{pad_name(i)}  /  {ROLES[i % 8].upper()}", wraplength=224, justify="left")
        self.sample_title.configure(text=Path(s.path).stem if s else "Empty pad")
        self.source_label.configure(text=s.relative if s else "")
        self.reason_label.configure(text=pad.reason)
        self.warning_label.configure(text=pad.warning)
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
        if self.session:
            for n, (_, candidate, reason) in enumerate(Matcher(self.session).rank(i)[:50]):
                key = f"{n + 1:02d}  {Path(candidate.path).stem[:25]}"
                self.candidates[key] = (candidate, reason)
        values = list(self.candidates) or ["No alternatives for this role"]
        self.candidate_menu.configure(values=values)
        self.candidate_var.set(values[0])

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
            sample = self.session.by_id.get(self.session.pads[self.selected].sample_id)
            if sample:
                self.audio_job(lambda: self.player.play(sample))

    def hear_kit(self, group):
        if self.session and not self.busy:
            self.settings_changed()
            snapshot = Session.from_dict(self.session.to_dict())
            kit = self.bank * 2 + group
            self.audio_job(lambda: self.player.groove(snapshot, kit))
            self.set_status(f"Playing kit {kit + 1} at {self.session.bpm} BPM. Main drums + hats; fills audition separately. Esc stops.")

    def stop(self):
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

    def toggle_pad_lock(self, local):
        if self.session and not self.busy:
            index = self.bank * 16 + local
            self.session.pads[index].locked = self.pad_lock_vars[local].get()
            self.selected = index
            self.refresh()
            self.autosave()

    def preview_candidate(self):
        choice = self.candidates.get(self.candidate_var.get())
        if choice:
            self.audio_job(lambda: self.player.play(choice[0]))

    def use_candidate(self):
        choice = self.candidates.get(self.candidate_var.get())
        if choice and self.session and not self.busy:
            self.session.pads[self.selected] = Assignment(choice[0].id, "Chosen by you. " + choice[1], locked=True)
            self.refresh()
            self.autosave()
            self.set_status("Replacement applied and locked. Uncheck Keep on rebuild on the pad to rebuild it later.")

    def clear_pad(self):
        if self.session and not self.busy:
            self.session.pads[self.selected] = Assignment(None, "Cleared by you", locked=True)
            self.refresh()
            self.autosave()

    def autosave(self):
        if self.session:
            try:
                path = self.data_dir / "last-session.json"
                temporary = self.data_dir / "last-session.tmp"
                temporary.write_text(json.dumps(self.session.to_dict()), encoding="utf-8")
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
                Path(path).write_text(json.dumps(self.session.to_dict(), indent=2), encoding="utf-8")
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
            self.player._cache.clear()
            self.path_var.set(session.root)
            self.style_var.set(session.style)
            self.bpm_var.set(str(session.bpm))
            self.sub_var.set(session.allow_substitutes)
            self.refresh()
            missing = sum(not Path(s.path).is_file() for s in session.samples)
            self.set_status(f"Opened session. {missing} source files missing; rescan before exporting." if missing else "Session opened. Click a pad or Hear groove to listen.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror("Could not open session", str(exc), parent=self)

    def export(self):
        if not self.session or self.busy:
            self.set_status("Analyse a sample folder first.")
            return
        self.settings_changed()
        folder = filedialog.askdirectory(title="Choose where to save the MPC program folder", initialdir=str(app_dir()), parent=self)
        if not folder:
            return
        self.stop()
        snapshot = Session.from_dict(self.session.to_dict())
        name = self.name_var.get()
        def done(result):
            output, validation = result
            self.set_status(f"Export verified: {validation['assigned_pads']}/64 pads, {validation['sample_memory_mb']} MB. Copy the whole folder to your MPC.")
            self.autosave()
            messagebox.showinfo("Ready for your MPC", f"Saved to:\n{output}\n\nCopy the whole folder to your MPC card, then load the .PGM with its samples.\n\n"
                                f"{validation['assigned_pads']}/64 pads · {validation['unique_wavs']} WAVs · {validation['sample_memory_mb']} MB\n\n"
                                "Review LOAD_ME.txt for substitutions. Computer checks passed; physical MPC testing is still required.", parent=self)
            os.startfile(output)
        self.run_job(lambda: export_program(snapshot, folder, name, self.progress), done)

    def show_help(self):
        notes = "\n".join(self.session.scan_notes) if self.session else "No folder analysed yet."
        messagebox.showinfo("Crate Builder — how it works", "1. Browse to a folder, then Analyse & build. + Folder adds extra sounds.\n"
            "2. Each bank has two kits. The main kick is pad 05 / 13; main snare 06 / 14.\n"
            "3. Click pads or Hear groove. Use sound replaces a pad and turns on Keep on rebuild.\n"
            "4. Each pad has its own Keep on rebuild checkbox. Amber borders mark substitutions or reuse.\n"
            "5. Export creates a NEW folder with one .PGM, WAVs and a pad map.\n\n"
            "The matcher ranks relative brightness, decay and attack within each instrument, and prefers shared filename families. "
            "It does not guarantee musical taste, infer pitch compatibility, extract hits from loops, or time-stretch fills. "
            "Tempo only affects the preview and fill ranking. Labelled loops/non-drums, silent files and files over 15 seconds are excluded. "
            "Unlabelled sounds may be guessed from their audio and are flagged for review.\n\n" + notes[:2200], parent=self)

    def close_app(self):
        self.cancel_event.set()
        if self.busy:
            self.closing = True
            self.set_status("Finishing the current operation safely, then closing...")
            return
        self.stop()
        if not self.busy:
            self.autosave()
        self.destroy()
