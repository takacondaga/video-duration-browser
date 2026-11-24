# -*- coding: utf-8 -*-
"""Tests de normalizacion y persistencia de filtros guardados."""

import unittest

from vdb import filters


class FiltersTest(unittest.TestCase):
    def test_normalize_rellena_y_descarta_claves(self):
        got = filters.normalize({"search": "  Drama  ", "otra": 1,
                                 "hide_unavailable": "si"})
        self.assertEqual(got["search"], "  Drama  ")
        self.assertEqual(got["scope"], "Ruta y notas")
        self.assertTrue(got["hide_unavailable"])
        self.assertNotIn("otra", got)

    def test_normalize_con_entrada_invalida(self):
        self.assertEqual(filters.normalize(None), dict(filters.DEFAULT_FILTER))

    def test_save_reemplaza_y_no_modifica_el_original(self):
        first = filters.save({}, "Accion", {"search": "accion"})
        second = filters.save(first, "Accion", {"search": "otra"})
        self.assertEqual(first["Accion"]["search"], "accion")
        self.assertEqual(second["Accion"]["search"], "otra")

    def test_save_rechaza_nombre_vacio(self):
        with self.assertRaises(ValueError):
            filters.save({}, "   ", {})

    def test_remove_y_names_ordenados(self):
        saved = filters.save({}, "zeta", {})
        saved = filters.save(saved, "Alfa", {})
        self.assertEqual(filters.names(saved), ["Alfa", "zeta"])
        self.assertEqual(filters.names(filters.remove(saved, "Alfa")), ["zeta"])

    def test_clean_saved_normaliza_cada_filtro(self):
        raw = {"a": {"search": "x"}, "b": None, "c": 42}
        clean = filters.clean_saved(raw)
        self.assertEqual(clean["a"]["search"], "x")
        self.assertEqual(clean["b"], dict(filters.DEFAULT_FILTER))
        self.assertEqual(clean["c"], dict(filters.DEFAULT_FILTER))
        self.assertEqual(filters.clean_saved(None), {})
