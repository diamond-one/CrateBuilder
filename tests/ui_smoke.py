"""Programmatic Windows GUI smoke check; creates only its own off-screen window."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with tempfile.TemporaryDirectory() as temporary:
    os.environ["CRATEBUILDER_DATA_DIR"] = temporary
    from cratebuilder.ui import App
    from cratebuilder.model import Session
    app = App()
    app.geometry("1280x900+10000+10000")
    app.update()
    assert app.kit_size_var.get() == "16 pads"
    assert app.target_var.get() == "SP-404MKII"
    assert "A09" in app.pad_buttons[4].cget("text")
    assert "A01" in app.pad_buttons[12].cget("text")
    assert len(app.pad_buttons) == 16
    assert not app.group_bars[1].winfo_ismapped()
    app.session = Session("C:/samples", [], kit_size=8, target="mpc")
    app.refresh()
    app.update_idletasks()
    assert app.group_bars[1].winfo_ismapped()
    assert app.kit_for_group(1) == 1
    assert "A05" in app.pad_buttons[4].cget("text")
    app.manage_folder_tags()
    app.tag_window.geometry("810x570+10000+10000")
    app.update()
    assert app.tag_window.winfo_exists()
    for widget in app.tag_window.winfo_children():
        if hasattr(widget, "grid_info") and widget.grid_info():
            assert widget.winfo_y() + widget.winfo_height() <= app.tag_window.winfo_height() + 2
    app.tag_window.destroy()
    app.close_app()
    print("GUI smoke passed: 16/SP default, legacy 8/MPC layout, physical pad labels, folder-rule dialog.")
