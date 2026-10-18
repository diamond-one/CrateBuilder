"""A pad-first workspace. Library maintenance stays out of the playing surface."""
import tkinter as tk
import customtkinter as ctk
from . import desk_theme as T
from .matching import STYLES


def build(app, tabs_class):
    app.grid_columnconfigure(0, weight=1)
    app.grid_rowconfigure(1, weight=1)
    head = ctk.CTkFrame(app, fg_color=T.INSET, corner_radius=0)
    head.grid(row=0, column=0, sticky='ew')
    head.grid_columnconfigure(1, weight=1)
    brand = ctk.CTkLabel(head, text='CrateBuilder', font=T.display(30), text_color=T.TEXT)
    brand.grid(row=0, column=0, sticky='w', padx=22, pady=(8, 0))
    app.header_detail = app.label(head, 'SP-404MKII · 16-pad kits', 12, T.MUTED)
    app.header_detail.grid(row=1, column=0, sticky='w', padx=22, pady=(0, 8))
    files = ctk.CTkFrame(head, fg_color='transparent')
    files.grid(row=0, column=1, rowspan=2, sticky='w', padx=22)
    app.button(files, 'Open crate', lambda: app.open_session(), width=100).pack(side='left', padx=(0, 8))
    app.button(files, 'Save crate', app.save_session, width=100).pack(side='left')
    audio = ctk.CTkFrame(head, fg_color='transparent')
    audio.grid(row=0, column=2, rowspan=2, sticky='e', padx=18)
    app.connection_button = app.button(audio, 'Audio & MIDI', app.audio_midi_settings, width=118)
    app.connection_button.pack(side='left', padx=(0, 8))
    app.live_button = app.button(audio, 'MIDI preview: OFF', app.toggle_live_preview, width=168)
    app.live_button.pack(side='left', padx=(0, 8))
    app.button(audio, 'Stop audio', app.stop, width=98).pack(side='left')
    T.WornRule(head, background=T.INSET).grid(row=2, column=0, columnspan=3, sticky='ew')

    body = ctk.CTkFrame(app, fg_color='transparent')
    body.grid(row=1, column=0, sticky='nsew', padx=18, pady=(10, 0))
    body.grid_columnconfigure(1, weight=1)
    body.grid_rowconfigure(0, weight=1)
    app.workspace_tabs = tabs_class(body, width=228, fg_color=T.PANEL, corner_radius=1,
        segmented_button_fg_color=T.INSET, segmented_button_selected_color=T.SELECTED,
        segmented_button_selected_hover_color=T.SELECTED, text_color=T.TEXT)
    app.workspace_tabs.grid(row=0, column=0, sticky='nsew', padx=(0, 16))
    app.workspace_tabs._segmented_button.configure(font=ctk.CTkFont(size=13), height=32,
        width=210, dynamic_resizing=False)
    make, library, setup = (app.workspace_tabs.add(name) for name in ('Kit', 'Library', 'More'))
    app.workspace_tabs.set('Kit')
    left = ctk.CTkScrollableFrame(make, fg_color='transparent', width=202)
    left.pack(fill='both', expand=True)
    section(app, left, 'Shape the kit')
    app.sources_button = app.button(left, 'Packs & working crates…', app.active_packs_and_crates, height=34)
    app.sources_button.pack(fill='x', padx=4, pady=(0, 6))
    app.active_sources_label = app.label(left, 'All indexed sources', 12, T.MUTED, wraplength=190, justify='left')
    app.active_sources_label.pack(anchor='w', padx=4, pady=(0, 16))
    caption(app, left, 'Drum character')
    app.style_menu = ctk.CTkOptionMenu(left, variable=app.style_var, values=list(STYLES), height=34,
        command=lambda _: app.settings_changed())
    app.style_menu.pack(fill='x', padx=4)
    caption(app, left, 'Surprise', (18, 6))
    app.surprise_slider = ctk.CTkSlider(left, from_=0, to=1, variable=app.surprise_var,
        progress_color=T.GREEN, button_color=T.GREEN, command=lambda _: app.settings_changed())
    app.surprise_slider.pack(fill='x', padx=4)
    ends = ctk.CTkFrame(left, fg_color='transparent')
    ends.pack(fill='x', padx=4, pady=(4, 0))
    app.label(ends, 'Familiar', 12, T.MUTED).pack(side='left')
    app.label(ends, 'Adventurous', 12, T.MUTED).pack(side='right')
    tempo = ctk.CTkFrame(left, fg_color='transparent')
    tempo.pack(fill='x', padx=4, pady=(22, 16))
    app.label(tempo, 'Tempo / BPM', 13, T.MUTED).pack(side='left')
    app.bpm_entry = ctk.CTkEntry(tempo, textvariable=app.bpm_var, width=58, height=32)
    app.bpm_entry.pack(side='right')
    app.bpm_entry.bind('<FocusOut>', lambda _: app.settings_changed())
    T.WornRule(left, background=T.PANEL).pack(fill='x', padx=4, pady=8)
    section(app, left, 'Your setup')
    for title, attr, variable, values, command in (
        ('Sampler', 'target_menu', app.target_var, ['SP-404MKII', 'MPC1000'], app.target_changed),
        ('Kit size', 'size_menu', app.kit_size_var, ['8 pads', '16 pads'], app.kit_size_changed)):
        caption(app, left, title, (6, 5))
        menu = ctk.CTkOptionMenu(left, variable=variable, values=values, command=command, height=32)
        menu.pack(fill='x', padx=4, pady=(0, 8))
        setattr(app, attr, menu)
    app.button(left, 'Browse all sounds…', app.browse_library, height=34).pack(fill='x', padx=4, pady=(16, 8))
    app.label(left, 'Space: hear pad\nLeft / Right: try alternatives\nEsc: stop audio', 12,
              T.MUTED, justify='left').pack(anchor='w', padx=4, pady=8)

    build_library(app, library)
    build_more(app, setup)

    center = ctk.CTkFrame(body, fg_color='transparent')
    center.grid(row=0, column=1, sticky='nsew')
    center.grid_columnconfigure(0, weight=1)
    center.grid_rowconfigure(3, weight=1)
    banks = ctk.CTkFrame(center, fg_color='transparent')
    banks.grid(row=0, column=0, sticky='ew', pady=(0, 10))
    app.bank_buttons = []
    for i, name in enumerate('ABCD'):
        button = app.button(banks, name, lambda n=i: app.change_bank(n), width=66, height=38)
        button.pack(side='left', padx=(0, 6))
        app.bank_buttons.append(button)
    app.bank_lock_button = app.button(banks, 'Lock bank', app.toggle_bank_lock, width=116, height=38)
    app.bank_lock_button.pack(side='right')
    app.summary_label = app.label(center, 'Add your sounds in Library to start a crate.', 13, T.MUTED, anchor='w')
    app.summary_label.grid(row=1, column=0, sticky='ew', pady=(0, 12))

    generate = ctk.CTkFrame(center, fg_color=T.PANEL, corner_radius=1)
    generate.grid(row=2, column=0, sticky='ew', pady=(0, 12))
    generate.grid_columnconfigure(1, weight=1)
    app.label(generate, 'Change', 12, T.MUTED).grid(row=0, column=0, sticky='w', padx=(12, 8), pady=10)
    app.scope_menu = ctk.CTkOptionMenu(generate, variable=app.scope_var,
        values=['Current bank', 'Selected pads', 'Upper 8', 'Whole crate'], command=app.scope_changed,
        dynamic_resizing=False, width=158, height=34)
    app.scope_menu.grid(row=0, column=1, sticky='w', pady=10)
    app.shuffle_button = app.button(generate, 'Generate bank A', app.generate_scope, primary=True, width=188, height=38)
    app.shuffle_button.grid(row=0, column=2, padx=12, pady=10)
    app.selection_label = app.label(left, '0 pads selected', 12, T.MUTED, anchor='w')
    app.selection_label.pack(fill='x', padx=4, pady=(0, 6), before=app.sources_button)
    selectors = ctk.CTkFrame(left, fg_color='transparent')
    selectors.pack(fill='x', padx=4, pady=(0, 14), before=app.sources_button)
    app.button(selectors, 'Select all', lambda: app.select_all_pads(True), width=80, height=26).pack(side='left', padx=(0, 6))
    app.button(selectors, 'Clear selection', lambda: app.select_all_pads(False), width=100, height=26).pack(side='left')

    app.pad_area = ctk.CTkFrame(center, fg_color='transparent')
    app.pad_area.grid(row=3, column=0, sticky='nsew')
    for col in range(4):
        app.pad_area.grid_columnconfigure(col, weight=1, uniform='pads')
    for row in (1, 2, 4, 5):
        app.pad_area.grid_rowconfigure(row, weight=1, uniform='pads')
    app.kit_labels, app.pad_buttons, app.group_bars = [], {}, {}
    app.pad_lock_vars, app.pad_lock_checks, app.pad_select_vars, app.pad_select_checks = {}, {}, {}, {}
    for group, row in ((1, 0), (0, 3)):
        bar = ctk.CTkFrame(app.pad_area, fg_color='transparent', height=22)
        bar.grid(row=row, column=0, columnspan=4, sticky='ew', pady=(0 if group else 4, 4))
        app.group_bars[group] = bar
        label = app.label(bar, '', 12, T.MUTED, height=18)
        label.pack(side='left')
        app.kit_labels.append((group, label))
    for local in range(16):
        row = {3: 1, 2: 2, 1: 4, 0: 5}[local // 4]
        tile = ctk.CTkFrame(app.pad_area, fg_color=T.INSET, corner_radius=1)
        tile.grid(row=row, column=local % 4, sticky='nsew', padx=(0, 8 if local % 4 < 3 else 0), pady=(0, 5))
        tile.grid_columnconfigure(0, weight=1)
        tile.grid_rowconfigure(0, weight=1)
        button = ctk.CTkButton(tile, text='', command=lambda n=local: app.select_pad(app.bank * 16 + n, play=True),
            corner_radius=1, border_width=1, border_color=T.BORDER, font=ctk.CTkFont(family='Segoe UI', size=12),
            text_color=T.TEXT, fg_color=T.CONTROL, hover_color=T.CONTROL_HOVER, width=100, height=64)
        button.grid(row=0, column=0, sticky='nsew')
        button.bind('<Control-Button-1>', lambda event, n=local: app.ctrl_select_pad(n))
        T.focus_ring(button)
        controls = ctk.CTkFrame(tile, fg_color='transparent')
        controls.grid(row=1, column=0, sticky='ew', padx=6, pady=3)
        selected, kept = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        for label, variable, command, attr in (
            ('Select', selected, lambda n=local: app.toggle_pad_selection(n), app.pad_select_checks),
            ('Lock', kept, lambda n=local: app.toggle_pad_lock(n), app.pad_lock_checks)):
            check = ctk.CTkCheckBox(controls, text=label, variable=variable, width=60 if label == 'Select' else 48,
                command=command, font=ctk.CTkFont(size=11), checkbox_width=14, checkbox_height=14, height=22)
            check.pack(side='left' if label == 'Select' else 'right')
            attr[local] = check
        app.pad_buttons[local], app.pad_lock_vars[local], app.pad_select_vars[local] = button, kept, selected
    transport = ctk.CTkFrame(center, fg_color='transparent')
    transport.grid(row=4, column=0, sticky='ew', pady=(2, 4))
    app.button(transport, 'Play groove', lambda: app.hear_kit(active_group(app)), width=112).pack(side='left', padx=(0, 8))
    app.button(transport, 'Drums only', lambda: app.hear_kit(active_group(app), drums_only=True), width=100).pack(side='left')
    app.label(transport, 'Click to hear. Select to change.', 12, T.MUTED).pack(side='right')

    build_inspector(app, body)
    delivery = ctk.CTkFrame(app, fg_color=T.INSET, corner_radius=0)
    delivery.grid(row=2, column=0, sticky='ew', padx=18, pady=(12, 0))
    delivery.grid_columnconfigure(0, weight=1)
    app.export_hint = app.label(delivery, '', 12, T.MUTED, anchor='w', wraplength=650, height=18)
    app.export_hint.grid(row=0, column=0, sticky='w', padx=12, pady=(10, 0))
    app.preview_route_label = app.label(delivery, 'MIDI preview is off', 12, T.MUTED, anchor='w', wraplength=650, height=18)
    app.preview_route_label.grid(row=1, column=0, sticky='w', padx=12, pady=(0, 10))
    app.export_button = app.button(delivery, 'Export WAVs…', app.export, width=142, height=38)
    app.export_button.grid(row=0, column=1, rowspan=2, padx=10, pady=10)
    app.send_button = app.button(delivery, 'Send to SP…', app.send_to_sp, primary=True, width=152, height=38)
    app.send_button.grid(row=0, column=2, rowspan=2, padx=(0, 12), pady=10)
    status = ctk.CTkFrame(app, fg_color='transparent')
    status.grid(row=3, column=0, sticky='ew', padx=20, pady=(6, 8))
    status.grid_columnconfigure(0, weight=1)
    app.status_label = app.label(status, 'Add a sample folder in Library to begin.', 12, T.MUTED, anchor='w', height=22)
    app.status_label.grid(row=0, column=0, sticky='ew')
    app.cancel_button = app.button(status, 'Cancel', app.cancel_event.set, width=86, height=28)
    app.cancel_button.grid(row=0, column=1, padx=(10, 0))
    app.cancel_button.configure(state='disabled')
    app.progress_bar = ctk.CTkProgressBar(status, progress_color=T.GREEN, height=3)
    app.progress_bar.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(5, 0))
    app.progress_bar.set(0)


def active_group(app):
    return 1 if app.session and app.session.kit_size == 8 and app.selected % 16 >= 8 else 0


def section(app, parent, text):
    ctk.CTkLabel(parent, text=text, text_color=T.TEXT, font=T.display(20), anchor='w').pack(
        fill='x', padx=4, pady=(12, 10))


def caption(app, parent, text, pady=(0, 5)):
    app.label(parent, text, 12, T.MUTED).pack(anchor='w', padx=4, pady=pady)


def build_library(app, page):
    source = ctk.CTkScrollableFrame(page, fg_color='transparent', width=202)
    source.pack(fill='both', expand=True)
    section(app, source, 'Your sounds')
    caption(app, source, 'Sample folder')
    app.path_entry = ctk.CTkEntry(source, textvariable=app.path_var, height=34)
    app.path_entry.pack(fill='x', padx=4)
    app.folder_label = app.label(source, 'Choose a folder to index.', 12, T.MUTED, wraplength=190, justify='left')
    app.folder_label.pack(anchor='w', padx=4, pady=8)
    row = ctk.CTkFrame(source, fg_color='transparent')
    row.pack(fill='x', padx=4)
    app.button(row, 'Browse', app.browse, width=88).pack(side='left')
    app.button(row, '+ Folder', app.add_folder, width=88).pack(side='right')
    app.scan_button = app.button(source, 'Update library', app.scan, primary=True)
    app.scan_button.pack(fill='x', padx=4, pady=(12, 8))
    app.tag_button = app.button(source, 'Tag folders…', app.manage_folder_tags)
    app.tag_button.pack(fill='x', padx=4, pady=(0, 16))
    section(app, source, 'NI expansions')
    app.ni_import_button = app.button(source, 'Import expansions…', app.import_ni_expansions)
    app.ni_import_button.pack(fill='x', padx=4, pady=(0, 8))
    app.library_browser_button = app.button(source, 'Browse collection…', app.browse_library)
    app.library_browser_button.pack(fill='x', padx=4, pady=(0, 16))
    app.count_label = app.label(source, 'WAV, AIFF and FLAC\nSubfolders included.\nOriginals stay in place.', 12, T.MUTED, justify='left', wraplength=190)
    app.count_label.pack(anchor='w', padx=4)
    app.button(source, 'Scan notes & help', app.show_help).pack(fill='x', padx=4, pady=16)


def build_more(app, page):
    options = ctk.CTkScrollableFrame(page, fg_color='transparent', width=202)
    options.pack(fill='both', expand=True)
    section(app, options, 'Generation options')
    app.substitute_check = ctk.CTkCheckBox(options, text='Allow role substitutes', variable=app.sub_var,
        font=ctk.CTkFont(size=12), checkbox_width=18, checkbox_height=18, command=app.settings_changed)
    app.substitute_check.pack(anchor='w', padx=4, pady=(4, 8))
    app.label(options, 'Use a related sound when a role is missing. The pad shows a warning.', 12,
              T.MUTED, wraplength=190, justify='left').pack(anchor='w', padx=4, pady=(0, 16))
    app.complete_button = app.button(options, 'Complete to 16', app.complete_to_16)
    app.complete_button.pack(fill='x', padx=4, pady=(0, 8))
    app.upper_button = app.button(options, 'Refresh upper 8', app.refresh_upper)
    app.upper_button.pack(fill='x', padx=4, pady=(0, 16))
    section(app, options, 'Export naming')
    caption(app, options, 'Crate / program name')
    ctk.CTkEntry(options, textvariable=app.name_var, placeholder_text='My crate').pack(fill='x', padx=4, pady=(0, 12))
    app.label(options, 'Save and open your editable crate from the top bar. Samples stay in their original folders.',
              12, T.MUTED, wraplength=190, justify='left').pack(anchor='w', padx=4, pady=(0, 16))
    section(app, options, 'Folder conversion')
    app.convert_button = app.button(options, 'Convert folder to MPC WAV', app.convert_audio_folder)
    app.convert_button.pack(fill='x', padx=4)


def build_inspector(app, body):
    right = ctk.CTkScrollableFrame(body, width=270, fg_color=T.PANEL, corner_radius=1)
    right.grid(row=0, column=2, sticky='nsew', padx=(16, 0))
    app.pad_title = app.label(right, 'Selected pad', 13, T.GREEN, True, anchor='w')
    app.pad_title.pack(fill='x', padx=14, pady=(14, 8))
    app.sample_title = app.label(right, 'Choose a pad', 20, T.TEXT, True, wraplength=248, justify='left', anchor='w')
    app.sample_title.pack(fill='x', padx=14, pady=(0, 10))
    app.waveform = tk.Canvas(right, height=76, width=245, bg=T.INSET, highlightthickness=0)
    app.waveform.pack(fill='x', padx=14, pady=(0, 10))
    app.button(right, 'Hear original pad', app.audition).pack(fill='x', padx=14, pady=(0, 12))
    T.WornRule(right, background=T.PANEL).pack(fill='x', padx=14, pady=(0, 10))
    ctk.CTkLabel(right, text='Try another sound', font=T.display(22), text_color=T.TEXT).pack(anchor='w', padx=14, pady=(0, 8))
    app.candidate_menu = ctk.CTkOptionMenu(right, values=['Update library first'], variable=app.candidate_var,
        command=app.on_candidate_chosen, font=ctk.CTkFont(size=12), dynamic_resizing=False, width=245, height=34)
    app.candidate_menu.pack(fill='x', padx=14)
    app.candidate_count_label = app.label(right, 'Choose an alternative to hear it.', 12, T.MUTED, wraplength=248, justify='left')
    app.candidate_count_label.pack(anchor='w', padx=14, pady=(6, 8))
    row = ctk.CTkFrame(right, fg_color='transparent')
    row.pack(fill='x', padx=14)
    app.button(row, 'Previous sound', lambda: app.step_candidate(-1), width=118).pack(side='left')
    app.button(row, 'Next sound', lambda: app.step_candidate(1), width=118).pack(side='right')
    app.keep_sound_button = app.button(right, 'Use & lock sound', app.use_candidate, primary=True, height=38)
    app.keep_sound_button.pack(fill='x', padx=14, pady=(8, 6))
    app.candidate_preview_label = app.label(right, 'Trying a sound leaves your pad unchanged.', 12,
        T.MUTED, wraplength=248, justify='left')
    app.candidate_preview_label.pack(anchor='w', padx=14, pady=(0, 12))
    marks = ctk.CTkFrame(right, fg_color='transparent')
    marks.pack(fill='x', padx=14, pady=(0, 8))
    app.favourite_button = app.button(marks, 'Favourite', app.mark_selected_favourite, width=118, height=30)
    app.favourite_button.pack(side='left')
    app.hide_button = app.button(marks, 'Hide sound', app.hide_selected_sound, width=118, height=30)
    app.hide_button.pack(side='right')
    app.button(right, 'Clear pad', app.clear_pad, height=30).pack(fill='x', padx=14, pady=(0, 14))
    T.WornRule(right, background=T.PANEL).pack(fill='x', padx=14, pady=(0, 10))
    app.source_label = app.label(right, '', 12, T.MUTED, wraplength=248, justify='left')
    app.source_label.pack(anchor='w', padx=14, pady=(0, 8))
    app.feature_label = app.label(right, '', 12, T.MUTED, wraplength=248, justify='left')
    app.feature_label.pack(anchor='w', padx=14, pady=(0, 8))
    app.reason_label = app.label(right, 'Add your sounds in Library to begin.', 12, T.TEXT, wraplength=248, justify='left')
    app.reason_label.pack(anchor='w', padx=14, pady=(0, 8))
    app.warning_label = app.label(right, '', 12, T.AMBER, wraplength=248, justify='left')
    app.warning_label.pack(anchor='w', padx=14, pady=(0, 14))
