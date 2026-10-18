"""Exercise the real desktop workflow without opening devices or changing user data."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cratebuilder.model import Assignment, Sample, Session


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


with tempfile.TemporaryDirectory(prefix="cratebuilder-ux-") as temporary:
    os.environ["CRATEBUILDER_DATA_DIR"] = temporary
    from cratebuilder.ui import App, snapshot_session
    app = App()
    try:
        app.geometry("1320x860+10000+10000")
        app.update()
        for name in ("Kit", "More", "Library", "Kit"):
            app.workspace_tabs.set(name)
        for _ in range(12):
            app.update()
            time.sleep(.015)
        assert app.scope_menu.winfo_ismapped(), "Rapid tab changes hid the current workspace"
        kinds = ("kick", "snare", "fill", "rim", "clap", "hat", "open_hat", "cymbal", "perc", "foley")
        samples = [Sample(str(i), f"C:/samples/{kinds[i % 10]}/{i}.wav", f"{i}.wav", kinds[i % 10], 1,
            "test", .2, 48000, 1, str(i), 100, 1,
            {"centroid": 400 + i * 30, "decay": .1, "crest": 8, "flatness": .3, "waveform": [.1, .2], "available": True})
            for i in range(100)]
        app.session = Session("C:/samples", samples, kit_size=16, target="sp404")
        app.session.pads = [Assignment(str(i), f"unchanged {i}", locked=i == 3) for i in range(64)]
        app.refresh()
        app.update()
        assert app.scope_var.get() == "Current bank"
        app.toggle_bank_lock()
        assert app.session.locked_banks == [0]
        assert app.bank_lock_button.cget("text") == "Unlock bank"
        assert app.pad_lock_checks[3].cget("state") == "disabled"
        app.toggle_bank_lock()
        assert app.session.pads[3].locked
        assert app.session.locked_banks == []
        snapshot = snapshot_session(app.session)
        snapshot.locked_banks.append(2)
        assert not app.session.locked_banks
        before = copy.deepcopy(app.session.pads)
        for local in (4, 8):
            app.pad_select_vars[local].set(True)
            app.toggle_pad_selection(local)
        assert app.selected_pads == {4, 8}
        assert app.shuffle_button.cget("text") == "Generate 2 selected"
        app.generate_scope()
        deadline = time.monotonic() + 15
        while app.busy and time.monotonic() < deadline:
            app.update()
            time.sleep(.01)
        assert not app.busy
        for index in set(range(64)) - {4, 8}:
            assert app.session.pads[index].sample_id == before[index].sample_id
            assert app.session.pads[index].reason == before[index].reason
        assert all(app.session.pads[i].reason != before[i].reason for i in (4, 8))
        assert app.status_label.cget("text").startswith("Changed 2 pads")
        previous_choices = [app.session.pads[i].sample_id for i in (4, 8)]
        app.generate_scope()
        deadline = time.monotonic() + 15
        while app.busy and time.monotonic() < deadline:
            app.update()
            time.sleep(.01)
        assert not app.busy
        assert all(app.session.pads[i].sample_id != previous for i, previous in zip((4, 8), previous_choices))
        app.change_bank(1)
        assert not app.selected_pads
        assert app.shuffle_button.cget("state") == "disabled"
        assert app.live_button.cget("text") == "MIDI preview: OFF"
        outputs = [dict(index=1, name="Speakers (SP-404MKII-G)", hostapi_name="MME"),
                   dict(index=20, name="Speakers (SP-404MKII-G)", hostapi_name="Windows WASAPI"),
                   dict(index=3, name="Headphones", hostapi_name="Windows WASAPI")]
        with patch("cratebuilder.ui.list_audio_outputs", return_value=outputs), patch("cratebuilder.ui.list_input_ports", return_value=["SP-404MKII-G 0"]):
            app.audio_midi_settings()
        app.connection_window.geometry("690x610+10000+10000")
        app.update()
        assert len(app.audio_output_menu.cget("values")) == 3  # default + two distinct outputs
        advanced = next(w for w in descendants(app.connection_window) if hasattr(w, "cget")
            and w.__class__.__name__ == "CTkCheckBox" and w.cget("text").startswith("Advanced"))
        advanced.toggle()
        app.update()
        assert len(app.audio_output_menu.cget("values")) == 4
        assert any("WASAPI" in name for name in app.audio_output_menu.cget("values"))
        advanced.toggle()
        app.update()
        assert len(app.audio_output_menu.cget("values")) == 3
        app.connection_window.destroy()
        for width, height in ((1200, 760), (1320, 860), (1600, 1000)):
            app.geometry(f"{width}x{height}+10000+10000")
            app.update()
            assert app.pad_area.winfo_width() >= 520
            for button in app.pad_buttons.values():
                assert button.winfo_width() >= 100
                assert button.winfo_height() >= 50, (width, height, button.winfo_width(), button.winfo_height(), app.pad_area.winfo_height())
            assert app.live_button.winfo_rootx() + app.live_button.winfo_width() <= app.winfo_rootx() + app.winfo_width()
        print("UX smoke passed: selected generation, independent bank locks, saved-state isolation, concise/advanced outputs and three window sizes.")
    finally:
        app.close_app()
