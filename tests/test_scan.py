# -*- coding: utf-8 -*-
"""Tests de huellas, recorrido de carpetas y helpers de escaneo."""

import io
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from vdb import database, scan
from vdb.config import FP_CHUNK, NT


class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with io.open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_tamano_cero_no_tiene_huella(self):
        path = self.write("vacio.mp4", b"")
        self.assertIsNone(scan.file_fingerprint(path, 0))

    def test_archivo_inexistente_no_tiene_huella(self):
        self.assertIsNone(scan.file_fingerprint(
            os.path.join(self.tmp, "no-existe.mp4"), 10))

    def test_huella_estable_y_sensible_al_contenido(self):
        path = self.write("a.mp4", b"abcdef" * 100)
        size = os.path.getsize(path)
        one = scan.file_fingerprint(path, size)
        two = scan.file_fingerprint(path, size)
        self.assertEqual(one, two)
        self.assertRegex(one, r"^\d+-[0-9a-f]{40}$")
        other = self.write("b.mp4", b"abcdef" * 99 + b"XXXXXX")
        self.assertEqual(os.path.getsize(other), size)
        self.assertNotEqual(scan.file_fingerprint(other, size), one)

    def test_archivo_grande_usa_fragmentos_muestreados(self):
        size = FP_CHUNK * 4
        data = bytearray(b"A" * size)
        path = self.write("grande.mp4", data)
        one = scan.file_fingerprint(path, size)
        data[0] = ord("B")
        with io.open(path, "wb") as fh:
            fh.write(data)
        self.assertNotEqual(scan.file_fingerprint(path, size), one)

    def test_process_file_reutiliza_duracion_conocida(self):
        path = self.write("a.mp4", b"12345")
        size = os.path.getsize(path)
        fp = scan.file_fingerprint(path, size)
        with mock.patch.object(scan, "probe_duration") as probe:
            duration, got_fp, reused = scan.process_file(
                None, path, size, True, -1.0, {fp: 42.0})
        self.assertEqual((duration, got_fp, reused), (42.0, fp, True))
        probe.assert_not_called()

    def test_process_file_consulta_ffprobe_si_no_la_conocia(self):
        path = self.write("a.mp4", b"12345")
        size = os.path.getsize(path)
        with mock.patch.object(scan, "probe_duration", return_value=7.5) as probe:
            duration, _fp, reused = scan.process_file(
                "ffprobe", path, size, True, -1.0, {})
        self.assertEqual((duration, reused), (7.5, False))
        probe.assert_called_once()

    def test_process_file_sin_probe_conserva_la_duracion_previa(self):
        path = self.write("a.mp4", b"12345")
        size = os.path.getsize(path)
        duration, _fp, reused = scan.process_file(
            None, path, size, False, 3.0, {})
        self.assertEqual((duration, reused), (3.0, False))


class WalkVideosTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def make(self, rel, size=10):
        path = os.path.join(self.tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "wb") as fh:
            fh.write(b"x" * size)
        return path

    def test_filtra_extensiones_tamano_y_carpetas_ignoradas(self):
        self.make("a.mp4")
        self.make("sub/b.mkv")
        self.make("UPPER.MP4")
        self.make("small.ts", 100)
        self.make("big.ts", 1024 * 1024 + 1)
        self.make("notas.txt")
        self.make("node_modules/c.mp4")
        self.make(".git/d.mp4")

        failed = set()
        found = {
            rel for rel, _size, _mtime, _created in scan.walk_videos(
                self.tmp, self.tmp, {".mp4", ".mkv", ".ts"},
                threading.Event(), failed)
        }
        self.assertEqual(found, {"a.mp4", "sub/b.mkv", "UPPER.MP4", "big.ts"})
        self.assertEqual(failed, set())

    def test_progreso_del_recorrido(self):
        self.make("a.mp4")
        self.make("sub/b.mkv")
        self.make("sub/c.mkv")
        seen = []
        found = list(scan.walk_videos(
            self.tmp, self.tmp, {".mp4", ".mkv"}, threading.Event(), set(),
            lambda *args: seen.append(args),
        ))
        self.assertEqual(len(found), 3)
        self.assertTrue(all(created for _rel, _size, _mtime, created in found))
        self.assertTrue(seen)
        dirs, files, videos, current = seen[-1]
        self.assertGreaterEqual(dirs, 2)
        self.assertGreaterEqual(files, 3)
        self.assertEqual(videos, 3)
        self.assertTrue(current)

    def test_under_failed(self):
        self.assertTrue(scan.under_failed("sub/a.mp4", {"sub"}))
        self.assertTrue(scan.under_failed("sub/a.mp4", {""}))
        self.assertFalse(scan.under_failed("otro/a.mp4", {"sub"}))
        self.assertFalse(scan.under_failed("subx/a.mp4", {"sub"}))
        self.assertFalse(scan.under_failed("a.mp4", set()))


class HelpersTest(unittest.TestCase):
    def test_definitely_missing_y_root_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.mp4")
            self.assertTrue(scan.definitely_missing(path))
            self.assertTrue(scan.root_alive(tmp))
            with io.open(path, "wb") as fh:
                fh.write(b"x")
            self.assertFalse(scan.definitely_missing(path))
        self.assertFalse(scan.root_alive(os.path.join(tmp, "no-existe")))

    def test_created_time_prefiere_birthtime(self):
        st = SimpleNamespace(st_birthtime=123.0, st_ctime=999.0)
        self.assertEqual(scan.created_time(st), 123.0)

    def test_created_time_cae_a_ctime_solo_en_windows(self):
        st = SimpleNamespace(st_ctime=456.0)
        self.assertEqual(scan.created_time(st), 456.0 if NT else None)
        self.assertIsNone(scan.created_time(SimpleNamespace(st_ctime=0)))

    def test_find_ffprobe_prefiere_la_carpeta_de_la_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = "ffprobe.exe" if NT else "ffprobe"
            local = os.path.join(tmp, name)
            with io.open(local, "wb") as fh:
                fh.write(b"x")
            with mock.patch.object(scan, "APP_DIR", tmp):
                self.assertEqual(scan.find_ffprobe(), local)

    def test_find_ffprobe_cae_al_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(scan, "APP_DIR", tmp), \
                    mock.patch.object(scan.shutil, "which", return_value="/bin/ffprobe"):
                self.assertEqual(scan.find_ffprobe(), "/bin/ffprobe")


class StageEventsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def test_scan_root_publica_las_siete_etapas(self):
        q = []
        job = database.Job(1, "legacy:test", self.tmp, "", 1, "Disco (C:)")
        con = mock.MagicMock()
        con.execute.return_value.fetchall.return_value = []
        with mock.patch.object(scan, "walk_videos",
                               return_value=iter([("a.mp4", 10, 0.0, 100.0)])), \
                mock.patch.object(scan, "process_file",
                                  return_value=(60.0, "huella-1", False)), \
                mock.patch.object(scan, "identify_volume",
                                  side_effect=AssertionError("no debe llamarse")):
            result = scan.scan_root(
                con, job, None, SimpleNamespace(put=q.append), threading.Event(),
                False, {".mp4"}, {}, [], [])
        self.assertEqual(result, "ok")
        stages = [m[1] for m in q if m[0] == "stage"]
        self.assertEqual(stages, [1, 2, 3, 4, 5, 6, 7])

    def test_scan_guarda_created_y_completa_filas_viejas(self):
        old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "videos_index.db")
        self.addCleanup(setattr, database, "DB_PATH", old_path)
        con = database.open_db()
        self.addCleanup(con.close)
        info = SimpleNamespace(ident="legacy:test", label="Test", mount=self.tmp,
                               kind="fixed")
        vid = database.get_or_create_volume(con, info)
        con.execute(
            "INSERT INTO files (vol, rel, size, mtime, duration, fp, nkey, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (vid, "a.mp4", 10, 100.0, 12.5, "fp-a", "key-a"),
        )
        con.commit()

        job = database.Job(vid, "legacy:test", self.tmp, "", 1, "Disco (C:)")
        found = iter([("a.mp4", 10, 100.0, 555.0), ("b.mp4", 10, 100.0, 666.0)])
        with mock.patch.object(scan, "walk_videos", return_value=found), \
                mock.patch.object(scan, "process_file",
                                  return_value=(12.5, "fp-b", False)):
            result = scan.scan_root(
                con, job, None, SimpleNamespace(put=lambda *args: None),
                threading.Event(), False, {".mp4"}, {}, [], [])

        self.assertEqual(result, "ok")
        dates = dict(con.execute("SELECT rel, created FROM files").fetchall())
        self.assertEqual(dates["a.mp4"], 555.0)
        self.assertEqual(dates["b.mp4"], 666.0)


class FindProbeTest(unittest.TestCase):
    def test_find_ffprobe_prefiere_el_binario_junto_a_la_app(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        name = "ffprobe.exe" if NT else "ffprobe"
        local = os.path.join(tmp.name, name)
        with open(local, "wb"):
            pass
        with mock.patch.object(scan, "APP_DIR", tmp.name), \
                mock.patch.object(scan.shutil, "which", return_value="del-path"):
            self.assertEqual(scan.find_ffprobe(), local)

    def test_find_ffprobe_cae_al_path(self):
        with mock.patch.object(scan.shutil, "which", return_value="del-path"):
            self.assertEqual(scan.find_ffprobe(), "del-path")


if __name__ == "__main__":
    unittest.main()
