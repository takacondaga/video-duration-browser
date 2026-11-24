# -*- coding: utf-8 -*-
"""Tests de las paletas y la aplicacion del tema."""

import unittest

try:
    import tkinter as tk

    from vdb import theme
except ImportError:
    tk = None
    theme = None


class PaletteTest(unittest.TestCase):
    @unittest.skipIf(theme is None, "tkinter no esta instalado")
    def test_paletas_claro_y_oscuro(self):
        light = theme.palette(False)
        dark = theme.palette(True)
        for key in ("bg", "fg", "field", "select", "offline", "missing"):
            self.assertIn(key, light)
            self.assertIn(key, dark)
        self.assertNotEqual(light["bg"], dark["bg"])
        self.assertEqual(light["bg"], theme.LIGHT["bg"])
        self.assertEqual(dark["bg"], theme.DARK["bg"])

    @unittest.skipIf(tk is None, "tkinter no esta instalado")
    def test_aplicar_tema_no_rompe_la_ventana(self):
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"Tk no disponible: {e}")
        self.addCleanup(root.destroy)
        root.withdraw()
        tk.Label(root, text="hola").pack()
        self.assertEqual(theme.apply_theme(root, True), theme.DARK)
        self.assertEqual(theme.apply_theme(root, False), theme.LIGHT)
