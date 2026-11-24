"""Tema claro / oscuro para la interfaz Tkinter."""

import tkinter as tk
from tkinter import ttk

LIGHT = {
    "bg": "#f2f2f2", "fg": "#1c1c1c", "field": "#ffffff",
    "select": "#cfe2f3", "select_fg": "#10233a",
    "offline": "#8a8a8a", "missing": "#b03a2e",
    "tooltip": "#ffffe0", "tooltip_fg": "#000000",
}
DARK = {
    "bg": "#22252a", "fg": "#e6e6e6", "field": "#30343b",
    "select": "#3f6ea8", "select_fg": "#ffffff",
    "offline": "#8d939b", "missing": "#ff8a7a",
    "tooltip": "#3a3f47", "tooltip_fg": "#f2f2f2",
}

_ORIGINAL_THEME = None


def palette(dark=False):
    return dict(DARK if dark else LIGHT)


def _safe_configure(widget, **options):
    try:
        widget.configure(**options)
    except (tk.TclError, AttributeError):
        pass


def _paint_tk_widget(widget, p):
    """Colorea los widgets tk puros (Text, Listbox, Menu, Toplevel, ...)."""
    cls = widget.winfo_class()
    opts = {"background": p["bg"], "foreground": p["fg"]}
    if cls in ("Text", "Listbox"):
        opts.update(background=p["field"], selectbackground=p["select"],
                    selectforeground=p["select_fg"],
                    insertbackground=p["fg"], highlightbackground=p["bg"])
    _safe_configure(widget, **opts)


def apply_theme(root, dark=False):
    """Aplica la paleta a `root` y a todos sus widgets actuales."""
    global _ORIGINAL_THEME
    p = palette(dark)
    style = ttk.Style()
    if _ORIGINAL_THEME is None:
        try:
            _ORIGINAL_THEME = style.theme_use()
        except tk.TclError:
            _ORIGINAL_THEME = "clam"
    # El tema nativo de Windows ignora algunos colores; 'clam' es el unico
    # que permite un tema oscuro real. El claro vuelve al tema original.
    try:
        style.theme_use("clam" if dark else _ORIGINAL_THEME)
    except tk.TclError:
        pass

    for name in ("Treeview", "TEntry", "TButton", "TFrame", "TLabel",
                 "TCheckbutton", "TCombobox", "TLabelframe", "TLabelframe.Label",
                 "TNotebook", "TNotebook.Tab", "TProgressbar", "TSpinbox",
                 "TScrollbar", "Horizontal.TScrollbar", "Vertical.TScrollbar",
                 "TMenubutton"):
        opts = {"background": p["bg"], "foreground": p["fg"]}
        if name in ("TEntry", "TCombobox", "TSpinbox", "Treeview"):
            opts["fieldbackground"] = p["field"]
        if name == "Treeview":
            opts.update(selectbackground=p["select"], selectforeground=p["select_fg"])
        try:
            style.configure(name, **opts)
        except tk.TclError:
            pass

    # Correccion conocida de Tk 8.6.9: ignora los colores de fila en Treeview.
    def fixed_map(option):
        return [e for e in style.map("Treeview", query_opt=option)
                if e[:2] != ("!disabled", "!selected")]

    try:
        style.map("Treeview", foreground=fixed_map("foreground"),
                  background=fixed_map("background"))
    except tk.TclError:
        pass

    stack = [root]
    while stack:
        widget = stack.pop()
        _paint_tk_widget(widget, p)
        stack.extend(widget.winfo_children())
    return p
