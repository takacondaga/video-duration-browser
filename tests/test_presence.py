# -*- coding: utf-8 -*-
"""Tests de la verificacion de disponibilidad de archivos."""

import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from vdb import presence


def row(path, online=True, kind="fixed", vol=1, rel="a.mp4", mount=""):
    return SimpleNamespace(path=path, online=online, kind=kind, vol=vol, rel=rel,
                           mount=mount)


class CheckPathTest(unittest.TestCase):
    def test_archivo_presente(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mp4")
            with open(path, "wb") as fh:
                fh.write(b"x")
            self.assertEqual(presence.check_path(path), presence.PRESENT)

    def test_archivo_faltante(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(presence.check_path(os.path.join(tmp, "no.mp4")),
                             presence.MISSING)

    def test_ruta_dentro_de_archivo_cuenta_como_faltante(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mp4")
            with open(path, "wb") as fh:
                fh.write(b"x")
            self.assertEqual(presence.check_path(os.path.join(path, "b.mp4")),
                             presence.MISSING)

    def test_error_de_permisos_es_desconocido(self):
        with mock.patch.object(presence.os, "stat", side_effect=PermissionError):
            self.assertEqual(presence.check_path("cualquiera"), presence.UNKNOWN)

    def test_error_de_red_es_desconocido(self):
        with mock.patch.object(presence.os, "stat", side_effect=OSError("timeout")):
            self.assertEqual(presence.check_path("cualquiera"), presence.UNKNOWN)


class LabelsTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(presence.presence_label(presence.PRESENT), "Presente")
        self.assertEqual(presence.presence_label(presence.MISSING), "Falta")
        self.assertEqual(presence.presence_label(presence.OFFLINE), "Disco desconectado")
        self.assertEqual(presence.presence_label(presence.UNKNOWN), "Sin verificar")
        self.assertEqual(presence.presence_label("otro"), "Sin verificar")

    def test_blocked(self):
        self.assertTrue(presence.presence_blocked(presence.MISSING))
        self.assertTrue(presence.presence_blocked(presence.OFFLINE))
        self.assertFalse(presence.presence_blocked(presence.PRESENT))
        self.assertFalse(presence.presence_blocked(presence.UNKNOWN))


class SupersededTest(unittest.TestCase):
    def make(self, key, state, path, vol=1, rel="a.mp4"):
        return SimpleNamespace(key=key, presence=state, path=path, vol=vol, rel=rel)

    def test_ubicacion_vieja_y_ruta_ganadora(self):
        gone = self.make("huella-1", presence.MISSING, "C:\\videos\\a.mp4", 1)
        good = self.make("huella-1", presence.PRESENT, "F:\\videos\\a.mp4", 2)
        hidden, repl = presence.superseded_locations([gone, good])
        self.assertEqual(hidden, [gone])
        self.assertEqual(repl, {"huella-1": "F:\\videos\\a.mp4"})

    def test_grupo_con_una_sola_fila_no_genera_reemplazo(self):
        only = self.make("huella-1", presence.PRESENT, "F:\\a.mp4")
        hidden, repl = presence.superseded_locations([only])
        self.assertEqual((hidden, repl), ([], {}))

    def test_dos_presentes_no_descartan_nada(self):
        one = self.make("huella-1", presence.PRESENT, "C:\\a.mp4", 1)
        two = self.make("huella-1", presence.PRESENT, "F:\\a.mp4", 2)
        hidden, repl = presence.superseded_locations([one, two])
        self.assertEqual((hidden, repl), ([], {}))

    def test_sin_presente_no_descarta_nada(self):
        one = self.make("huella-1", presence.MISSING, "C:\\a.mp4", 1)
        two = self.make("huella-1", presence.OFFLINE, "E:\\a.mp4", 2)
        hidden, repl = presence.superseded_locations([one, two])
        self.assertEqual((hidden, repl), ([], {}))

    def test_clave_de_ruta_no_se_agrupa(self):
        one = self.make("path:vol|uno", presence.PRESENT, "C:\\a.mp4", 1)
        two = self.make("path:vol|dos", presence.MISSING, "F:\\a.mp4", 2)
        hidden, repl = presence.superseded_locations([one, two])
        self.assertEqual((hidden, repl), ([], {}))

    def test_consolidate_rows_sigue_usando_la_misma_regla(self):
        gone = self.make("huella-1", presence.MISSING, "C:\\a.mp4", 1)
        good = self.make("huella-1", presence.PRESENT, "F:\\a.mp4", 2)
        hidden, repl = presence.superseded_locations([gone, good])
        kept, view_hidden = presence.consolidate_rows([gone, good])
        self.assertEqual(kept, [good])
        self.assertEqual(view_hidden, hidden)
        self.assertEqual(repl["huella-1"], good.path)


class WorkerTest(unittest.TestCase):
    def run_worker(self, rows, stop=None):
        q = []
        results, cancelled = presence.presence_worker(
            rows, stop or threading.Event(), SimpleNamespace(put=q.append), batch=1)
        done = [m for m in q if m[0] == "presence_done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][1], results)
        self.assertEqual(done[0][2], cancelled)
        return results, cancelled, q

    def test_offline_no_toca_el_disco(self):
        with mock.patch.object(presence, "check_path",
                               side_effect=AssertionError("no debe verificar")):
            results, cancelled, q = self.run_worker([row("x", online=False)])
        self.assertEqual(results, {(1, "a.mp4"): presence.OFFLINE})
        self.assertFalse(cancelled)
        progress = [m for m in q if m[0] == "presence"]
        self.assertEqual(progress, [("presence", 1, 1)])

    def test_remoto_queda_sin_verificar(self):
        with mock.patch.object(presence, "check_path",
                               side_effect=AssertionError("no debe verificar")):
            results, _cancelled, _q = self.run_worker([row("x", kind="remote")])
        self.assertEqual(results, {(1, "a.mp4"): presence.UNKNOWN})

    def test_presente_y_faltante(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mp4")
            with open(path, "wb") as fh:
                fh.write(b"x")
            results, _cancelled, _q = self.run_worker([
                row(path, mount=tmp),
                row(os.path.join(tmp, "no.mp4"), vol=2, rel="b.mp4", mount=tmp),
            ])
        self.assertEqual(results, {
            (1, "a.mp4"): presence.PRESENT,
            (2, "b.mp4"): presence.MISSING,
        })

    def test_disco_desconectado_a_mitad_es_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = os.path.join(tmp, "disco")
            os.makedirs(mount)
            first = os.path.join(mount, "a.mp4")
            second = os.path.join(mount, "b.mp4")
            os.rmdir(mount)  # el disco se retira antes de verificar
            results, _cancelled, _q = self.run_worker([
                row(first, rel="a.mp4", mount=mount),
                row(second, rel="b.mp4", mount=mount),
            ])
        self.assertEqual(results, {
            (1, "a.mp4"): presence.OFFLINE,
            (1, "b.mp4"): presence.OFFLINE,
        })

    def test_cancelacion_inmediata(self):
        stop = threading.Event()
        stop.set()
        results, cancelled, _q = self.run_worker([row("x")], stop)
        self.assertEqual(results, {})
        self.assertTrue(cancelled)

    def test_error_publica_aviso_y_termina(self):
        q = []
        with mock.patch.object(presence, "check_path", side_effect=RuntimeError("boom")):
            presence.presence_worker([row("x")], threading.Event(),
                                     SimpleNamespace(put=q.append), batch=1)
        kinds = [m[0] for m in q]
        self.assertIn("presence_error", kinds)
        self.assertEqual(kinds[-1], "presence_done")
        self.assertIn("RuntimeError: boom", q[kinds.index("presence_error")][1])


class ConsolidateTest(unittest.TestCase):
    def make(self, key, state, disk):
        return SimpleNamespace(key=key, presence=state, disk=disk)

    def test_conserva_la_ubicacion_presente(self):
        good = self.make("huella-1", presence.PRESENT, "Disco (F:)")
        gone = self.make("huella-1", presence.MISSING, "Disco (C:)")
        kept, hidden = presence.consolidate_rows([gone, good])
        self.assertEqual(kept, [good])
        self.assertEqual(hidden, [gone])

    def test_oculta_tambien_disco_desconectado_y_sin_verificar(self):
        good = self.make("huella-1", presence.PRESENT, "Disco (F:)")
        offline = self.make("huella-1", presence.OFFLINE, "Disco (E:)")
        unknown = self.make("huella-1", presence.UNKNOWN, "Disco (G:)")
        kept, hidden = presence.consolidate_rows([good, offline, unknown])
        self.assertEqual(kept, [good])
        self.assertEqual(hidden, [offline, unknown])

    def test_dos_presentes_son_copias_y_se_conservan(self):
        one = self.make("huella-1", presence.PRESENT, "Disco (C:)")
        two = self.make("huella-1", presence.PRESENT, "Disco (F:)")
        kept, hidden = presence.consolidate_rows([one, two])
        self.assertEqual(kept, [one, two])
        self.assertEqual(hidden, [])

    def test_sin_presente_no_descarta_nada(self):
        one = self.make("huella-1", presence.MISSING, "Disco (C:)")
        two = self.make("huella-1", presence.OFFLINE, "Disco (E:)")
        kept, hidden = presence.consolidate_rows([one, two])
        self.assertEqual(kept, [one, two])
        self.assertEqual(hidden, [])

    def test_claves_de_ruta_no_se_agrupan(self):
        one = self.make("path:vol|un", presence.PRESENT, "Disco (C:)")
        two = self.make("path:vol|dos", presence.MISSING, "Disco (F:)")
        kept, hidden = presence.consolidate_rows([one, two])
        self.assertEqual(kept, [one, two])
        self.assertEqual(hidden, [])


if __name__ == "__main__":
    unittest.main()
