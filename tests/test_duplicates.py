# -*- coding: utf-8 -*-
"""Tests de la vista de duplicados por huella de contenido."""

import unittest
from types import SimpleNamespace

from vdb.duplicates import duplicate_groups, duplicate_stats


def row(name, key, size=100, duration=60.0, path=None):
    return SimpleNamespace(name=name, key=key, size=size, duration=duration,
                           path=path or "C:\\videos\\" + name)


class DuplicatesTest(unittest.TestCase):
    def test_agrupa_por_huella_no_por_nombre(self):
        rows = [
            row("uno.mp4", "huella-1"),
            row("copia.mkv", "huella-1", size=100),
            row("otro.mp4", "huella-2"),
        ]
        groups = duplicate_groups(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].key, "huella-1")
        self.assertEqual([r.name for r in groups[0].rows], ["copia.mkv", "uno.mp4"])

    def test_ignora_claves_de_ruta(self):
        rows = [row("a.mp4", "path:C|/a.mp4"), row("b.mp4", "path:D|/b.mp4"),
                row("c.mp4", None), row("d.mp4", "")]
        self.assertEqual(duplicate_groups(rows), [])

    def test_solo_devuelve_grupos_con_dos_o_mas_copias(self):
        rows = [row("a.mp4", "k1"), row("b.mp4", "k1"), row("c.mp4", "k2")]
        groups = duplicate_groups(rows)
        self.assertEqual([g.key for g in groups], ["k1"])

    def test_orden_por_copias_y_tamano(self):
        rows = [row("a.mp4", "k1", size=10), row("b.mp4", "k1", size=10),
                row("c.mp4", "k2", size=900), row("d.mp4", "k2", size=900),
                row("e.mp4", "k2", size=900)]
        groups = duplicate_groups(rows)
        self.assertEqual([g.key for g in groups], ["k2", "k1"])
        self.assertEqual(duplicate_stats(groups), (2, 3, 1810))

    def test_min_copies(self):
        rows = [row("a.mp4", "k1"), row("b.mp4", "k1"), row("c.mp4", "k1")]
        self.assertEqual(len(duplicate_groups(rows, min_copies=3)), 1)
        self.assertEqual(duplicate_groups(rows, min_copies=4), [])
