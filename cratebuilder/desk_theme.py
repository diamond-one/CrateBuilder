"""The gallery's Grunge palette, shared by the workspace and secondary windows."""
from pathlib import Path
import tkinter as tk

import customtkinter as ctk

BG, PANEL, INSET = '#302e2b', '#393631', '#252522'
TEXT, MUTED, GREEN, AMBER = '#e7e2d9', '#c0b9ab', '#e1b1a4', '#e5bd79'
CONTROL, CONTROL_HOVER, BORDER = '#4c4842', '#605a51', '#767268'
CORE_PAD, CREATIVE_PAD, SELECTED = '#403d38', '#4c4842', '#a9433e'
ON_SELECTED, ON_ACTION = '#fff3df', '#392a27'
ASSETS = Path(__file__).parent / 'assets'


def initialise():
    ctk.set_appearance_mode('dark')
    ctk.set_default_color_theme(str(ASSETS / 'grunge.json'))
    ctk.FontManager.load_font(str(ASSETS / 'fonts' / 'Oswald.ttf'))


def display(size=22):
    return ctk.CTkFont(family='Oswald', size=size, weight='bold')


class WornRule(tk.Canvas):
    """Quiet broken rules from the gallery; no texture behind working text."""
    def __init__(self, parent, background=BG, **kwargs):
        super().__init__(parent, height=5, background=background,
                         highlightthickness=0, takefocus=0, **kwargs)
        self.bind('<Configure>', self.redraw)

    def redraw(self, _event=None):
        self.delete('all')
        self.create_line(0, 2, self.winfo_width(), 2, fill=BORDER, dash=(13, 5, 2, 7))


def focus_ring(widget):
    """Give keyboard users the same strong cue as the inspected pad."""
    if isinstance(widget, ctk.CTkButton):
        width, color = widget.cget('border_width'), widget.cget('border_color')
        widget.bind('<FocusIn>', lambda _: widget.configure(border_width=2, border_color=GREEN), add='+')
        widget.bind('<FocusOut>', lambda _: widget.configure(border_width=width, border_color=color), add='+')
        widget.bind('<Return>', lambda _: widget.invoke(), add='+')
        widget._canvas.configure(takefocus=True)
