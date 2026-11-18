# -*- coding: utf-8 -*-
"""Tests de las utilidades de texto, formato y extensiones."""

import unittest
from types import SimpleNamespace

from vdb import text


class ParseExtListTest(unittest.TestCase):
    def test_normaliza_y_separa_validas_de_invalidas(self):
        good, bad = text.parse_ext_list("mp4, .MKV; *.avi, ts, .tar.gz, bad/path")
        self.assertEqual(good, {".mp4", ".mkv", ".avi", ".ts"})
        self.assertEqual(bad, [".tar.gz", "bad/path"])

    def test_lista_vacia(self):
        self.assertEqual(text.parse_ext_list("  , ;  "), (set(), []))

    def test_extension_larga_es_invalida(self):
        good, bad = text.parse_ext_list(".extensiondemasiadolarga")
        self.assertEqual(good, set())
        self.assertEqual(bad, [".extensiondemasiadolarga"])


class TagsTest(unittest.TestCase):
    def test_fold_quita_acentos_y_mayusculas(self):
        self.assertEqual(text.fold("PelículA Ñandú"), "pelicula nandu")

    def test_clean_note_colapsa_espacios_y_lineas(self):
        self.assertEqual(text.clean_note("  uno\n dos\t tres  "), "uno dos tres")

    def test_merge_tags_no_duplica_por_mayuscula_ni_acento(self):
        self.assertEqual(text.merge_tags("Uno dos", "uno DOS Tres"), "Uno dos Tres")

    def test_remove_tags_ignora_mayusculas_y_acentos(self):
        self.assertEqual(text.remove_tags("Uno dos TRES", "DOS três"), "Uno")

    def test_tag_index_cuenta_una_vez_por_video_y_ordena(self):
        rows = [
            SimpleNamespace(note="Uno dos"),
            SimpleNamespace(note="dos TRES dos"),
            SimpleNamespace(note=""),
        ]
        self.assertEqual(text.tag_index(rows), [("dos", 2), ("tres", 1), ("uno", 1)])


class FormatTest(unittest.TestCase):
    def test_parse_minutes(self):
        self.assertEqual(text.parse_minutes("1,5"), 90.0)
        self.assertEqual(text.parse_minutes(" 2 "), 120.0)
        self.assertIsNone(text.parse_minutes(""))
        self.assertIsNone(text.parse_minutes("abc"))

    def test_fmt_dur(self):
        self.assertEqual(text.fmt_dur(-1), "?")
        self.assertEqual(text.fmt_dur(0), "0:00:00")
        self.assertEqual(text.fmt_dur(59.6), "0:01:00")
        self.assertEqual(text.fmt_dur(3661), "1:01:01")

    def test_fmt_size(self):
        self.assertEqual(text.fmt_size(0), "0 B")
        self.assertEqual(text.fmt_size(1024), "1.00 KB")
        self.assertEqual(text.fmt_size(1536), "1.50 KB")
        self.assertEqual(text.fmt_size(1024 ** 4), "1.00 TB")


if __name__ == "__main__":
    unittest.main()
