# -*- coding: utf-8 -*-
"""Tests del plan de renombrado y organizacion masiva."""

import os
import tempfile
import unittest
from types import SimpleNamespace

from vdb import bulkops
from vdb.config import NT
from vdb.paths import to_db_rel


def make_row(name, folder, mount, vol=1):
    return SimpleNamespace(
        name=name, path=os.path.join(folder, name),
        rel=to_db_rel(os.path.relpath(os.path.join(folder, name), mount)),
        mount=mount, vol=vol, key="k-" + name, size=10, duration=60.0, created=None,
    )


class BulkOpsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.mount = self.tmp
        self.videos = os.path.join(self.tmp, "videos")
        os.makedirs(self.videos)
        self.rows = [make_row("a.mp4", self.videos, self.mount),
                     make_row("b.mp4", self.videos, self.mount)]

    def test_safe_filename_limpia_caracteres_y_nombres_reservados(self):
        self.assertEqual(bulkops.safe_filename('a:b/c?d'), "a_b_c_d")
        self.assertEqual(bulkops.safe_filename("CON"), "_CON")
        self.assertEqual(bulkops.safe_filename("   "), "video")
        self.assertLessEqual(len(bulkops.safe_filename("x" * 500)), bulkops.MAX_NAME)

    def test_render_placeholders(self):
        row = self.rows[0]
        self.assertEqual(bulkops.render("{title} - {index}", row, 3), "a - 3")
        self.assertEqual(bulkops.render("{name} {duration}", row), "a.mp4 0-01-00")
        self.assertEqual(bulkops.render("{created} {folder}", row),
                         "sin-fecha videos")
        with self.assertRaises(ValueError):
            bulkops.render("{desconocido}", row)

    def test_plan_rename_numera_y_detecta_colisiones(self):
        plans = bulkops.plan_rename(self.rows, "{title} - {index}")
        self.assertEqual([p.new_rel for p in plans],
                         ["videos/a - 1.mp4", "videos/b - 2.mp4"])
        self.assertTrue(all(p.error is None for p in plans))

        plans = bulkops.plan_rename(
            self.rows, "{title} - {index}", occupied={1: {"videos/b - 2.mp4"}})
        self.assertIsNone(plans[0].error)
        self.assertIn("ya existe", plans[1].error)

    def test_plan_rename_repite_nombre_en_el_lote(self):
        plans = bulkops.plan_rename(self.rows, "igual")
        self.assertIsNone(plans[0].error)
        self.assertEqual(plans[1].error, "nombre repetido en el lote")

    def test_plan_move_respeta_el_mismo_disco(self):
        dest = os.path.join(self.tmp, "dest")
        plans = bulkops.plan_move(self.rows[:1], dest, "temporada 1")
        self.assertEqual(plans[0].new_rel, "dest/temporada 1/a.mp4")
        self.assertEqual(plans[0].new_path,
                         os.path.join(dest, "temporada 1", "a.mp4"))

        if NT:  # en Windows se puede simular otra unidad
            other = SimpleNamespace(**dict(self.rows[0].__dict__))
            other.vol, other.mount = 2, "Z:\\"
            other.path = "Z:\\videos\\a.mp4"
            plans = bulkops.plan_move([other], dest)
            self.assertIn("otro disco", plans[0].error)

    def test_execute_changes_mueve_y_actualiza_la_base(self):
        dest = os.path.join(self.tmp, "dest")
        for row in self.rows[:1]:
            with open(row.path, "wb"):
                pass
        plans = bulkops.plan_move(self.rows[:1], dest)
        records = []
        done, failed = bulkops.execute_changes(plans, lambda items: records.extend(items))
        self.assertEqual(len(done), 1)
        self.assertEqual(failed, [])
        self.assertTrue(os.path.isfile(plans[0].new_path))
        self.assertFalse(os.path.exists(plans[0].old_path))
        self.assertEqual(records[0][:3], (1, "videos/a.mp4", "dest/a.mp4"))

    def test_execute_changes_revierte_si_la_base_falla(self):
        for row in self.rows[:1]:
            with open(row.path, "wb"):
                pass
        plans = bulkops.plan_rename(self.rows[:1], "otro")

        def explode(_records):
            raise ValueError("conflicto en la base")

        with self.assertRaises(ValueError):
            bulkops.execute_changes(plans, explode)
        self.assertTrue(os.path.isfile(self.rows[0].path))
        self.assertFalse(os.path.exists(plans[0].new_path))
