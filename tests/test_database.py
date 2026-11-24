# -*- coding: utf-8 -*-
"""Tests del esquema, migraciones y operaciones de la base de datos."""

import glob
import io
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from vdb import database, presence
from vdb.paths import display_path
from vdb.volumes import VolInfo


class DatabaseTestCase(unittest.TestCase):
    """Cada test usa su propia base dentro de un directorio temporal."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.db_path = os.path.join(self.tmp, "videos_index.db")
        self._old_db_path = database.DB_PATH
        database.DB_PATH = self.db_path
        self.addCleanup(self._restore_db_path)

    def _restore_db_path(self):
        database.DB_PATH = self._old_db_path

    def open(self):
        con = database.open_db()
        self.addCleanup(con.close)
        return con

    def add_volume(self, con, ident="VOL1", label="Disco", mount=None):
        info = VolInfo(ident, label, mount if mount is not None else self.tmp, 0, "fixed")
        return database.get_or_create_volume(con, info)

    def add_file(self, con, vid, rel, size=10, mtime=100.0, duration=12.5,
                 fp="fp-1", nkey="key-1", created=None):
        con.execute(
            "INSERT OR REPLACE INTO files "
            "(vol, rel, size, mtime, duration, fp, nkey, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, rel, size, mtime, duration, fp, nkey, created),
        )
        con.commit()

    # -- esquema -------------------------------------------------------------
    def test_base_nueva_crea_esquema_y_version(self):
        con = self.open()
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"meta", "notes", "volumes", "roots", "files"} <= tables)
        indexes = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("idx_files_nkey", indexes)
        version = con.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, database.SCHEMA_VERSION)

    def test_abrir_dos_veces_no_duplica_ni_rompe(self):
        con1 = self.open()
        con1.execute("INSERT INTO meta (k, v) VALUES ('x', '1')")
        con1.commit()
        con1.close()
        con = self.open()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM meta").fetchone()[0], 1)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0],
                         database.SCHEMA_VERSION)

    def test_migracion_v2_a_v3_agrega_created(self):
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "CREATE TABLE files (vol INTEGER NOT NULL, rel TEXT NOT NULL, "
                "size INTEGER NOT NULL, mtime REAL NOT NULL, duration REAL NOT NULL, "
                "fp TEXT, nkey TEXT, PRIMARY KEY (vol, rel))")
            con.execute("PRAGMA user_version = 2")
            con.commit()
        finally:
            con.close()
        con = self.open()
        cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
        self.assertIn("created", cols)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0],
                         database.SCHEMA_VERSION)

    def test_fetch_rows_expone_creacion_y_modificacion(self):
        con = self.open()
        vid = self.add_volume(con)
        self.add_file(con, vid, "a.mp4", mtime=200.0, created=100.0)
        row = database.fetch_rows({})[0]
        self.assertEqual(row.created, 100.0)
        self.assertEqual(row.modified, 200.0)

    # -- volumenes y trabajos -------------------------------------------------
    def test_get_or_create_volume_actualiza_sin_duplicar(self):
        con = self.open()
        info = VolInfo("VOL1", "Viejo", "D:\\", 0, "removable")
        vid = database.get_or_create_volume(con, info)
        info2 = VolInfo("VOL1", "Nuevo", "E:\\", 0, "removable")
        self.assertEqual(database.get_or_create_volume(con, info2), vid)
        rows = con.execute("SELECT ident, label, mount, workers FROM volumes").fetchall()
        self.assertEqual(rows, [("VOL1", "Nuevo", "E:\\", 3)])

    def test_volume_states_y_build_jobs(self):
        con = self.open()
        vid = self.add_volume(con, mount="D:\\")
        database.add_root(con, vid, "sub")
        database.add_root(con, vid, "")
        database.add_root(con, vid, "")  # duplicado ignorado

        states = database.volume_states(con, {})
        self.assertFalse(states[vid].online)
        jobs, offline = database.build_jobs(con, states)
        self.assertEqual(jobs, [])
        self.assertEqual(offline, ["Disco"])

        info = VolInfo("VOL1", "Disco", "D:\\", 0, "fixed")
        states = database.volume_states(con, {"VOL1": info})
        self.assertTrue(states[vid].online)
        jobs, offline = database.build_jobs(con, states)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].root_rel, "")  # "sub" queda contenida en la raiz
        self.assertEqual(offline, [])

        jobs, _ = database.build_jobs(con, database.volume_states(con, {}),
                                      vol_ids={vid})
        self.assertEqual(jobs, [])

    def test_build_jobs_escanea_la_raiz_si_no_hay_roots(self):
        con = self.open()
        vid = self.add_volume(con, mount="D:\\")
        info = VolInfo("VOL1", "Disco", "D:\\", 0, "fixed")
        states = database.volume_states(con, {"VOL1": info})
        jobs, _ = database.build_jobs(con, states, vol_ids={vid})
        self.assertEqual([j.root_rel for j in jobs], [""])

    # -- notas ----------------------------------------------------------------
    def test_save_notes_y_fetch_rows(self):
        con = self.open()
        vid = self.add_volume(con)
        database.add_root(con, vid, "")
        rel = "sub/video.mp4"
        self.add_file(con, vid, rel, nkey="key-1")
        path = display_path(self.tmp, rel)
        database.save_notes({"key-1": ("nota uno", path)})

        rows = database.fetch_rows({})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.path, path)
        self.assertEqual(row.name, "video.mp4")
        self.assertEqual(row.folder, os.path.join(self.tmp, "sub"))
        self.assertEqual(row.key, "key-1")
        self.assertEqual(row.note, "nota uno")
        self.assertFalse(row.online)
        self.assertEqual(row.presence, presence.OFFLINE)
        self.assertEqual(row.kind, "fixed")
        self.assertEqual(row.mount, self.tmp)
        self.assertEqual(row.vol, vid)
        self.assertEqual(row.rel, rel)
        self.assertIn("nota uno", row.fall)

        database.save_notes({"key-1": ("", path)})
        self.assertEqual(con.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)

    def test_presence_segun_disco_y_cache(self):
        con = self.open()
        vid = self.add_volume(con, mount="D:\\\\")
        self.add_file(con, vid, "a.mp4")

        rows = database.fetch_rows({})
        self.assertEqual(rows[0].presence, presence.OFFLINE)

        info = VolInfo("VOL1", "Disco", "D:\\\\", 0, "fixed")
        rows = database.fetch_rows({"VOL1": info})
        self.assertEqual(rows[0].presence, presence.UNKNOWN)
        self.assertEqual(rows[0].kind, "fixed")

        cache = {(vid, "a.mp4"): presence.PRESENT}
        rows = database.fetch_rows({"VOL1": info}, cache)
        self.assertEqual(rows[0].presence, presence.PRESENT)

        # La cache no puede afirmar que un disco desconectado esta presente.
        rows = database.fetch_rows({}, cache)
        self.assertEqual(rows[0].presence, presence.OFFLINE)

    def test_migrate_note_sigue_al_contenido(self):
        con = self.open()
        vid = self.add_volume(con)
        self.add_file(con, vid, "a.mp4", nkey="old")
        con.execute("INSERT INTO notes (key, note) VALUES ('old', 'nota')")
        con.commit()
        database.migrate_note(con, "old", "new", vid, "a.mp4", "a.mp4")
        self.assertEqual(con.execute("SELECT key FROM notes").fetchone()[0], "new")

    def test_migrate_note_no_pisa_otra_copia_ni_una_nota_nueva(self):
        con = self.open()
        vid = self.add_volume(con)
        self.add_file(con, vid, "a.mp4", nkey="old")
        self.add_file(con, vid, "b.mp4", nkey="old")
        con.execute("INSERT INTO notes (key, note) VALUES ('old', 'nota')")
        con.commit()
        database.migrate_note(con, "old", "new", vid, "a.mp4", "a.mp4")
        self.assertEqual(con.execute("SELECT key FROM notes").fetchone()[0], "old")

        con.execute("DELETE FROM files WHERE rel = 'b.mp4'")
        con.execute("INSERT INTO notes (key, note) VALUES ('new', 'otra')")
        con.commit()
        database.migrate_note(con, "old", "new", vid, "a.mp4", "a.mp4")
        self.assertEqual(
            dict(con.execute("SELECT key, note FROM notes").fetchall()),
            {"old": "nota", "new": "otra"},
        )

    # -- poda de ubicaciones superadas ----------------------------------------
    def _add_two_copies_with_note(self, con):
        con.execute(
            "INSERT INTO volumes (id, ident, label, mount, kind, workers) "
            "VALUES (1, 'C', 'C', 'C:\\', 'fixed', 4)")
        con.execute(
            "INSERT INTO volumes (id, ident, label, mount, kind, workers) "
            "VALUES (2, 'F', 'F', 'F:\\', 'fixed', 4)")
        con.executemany(
            "INSERT INTO files (vol, rel, size, mtime, duration, fp, nkey) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(1, "a.mp4", 10, 1.0, 12.5, "fp-1", "key-1"),
             (2, "a.mp4", 10, 1.0, 12.5, "fp-1", "key-1")],
        )
        con.execute(
            "INSERT INTO notes (key, note, last_path, updated) "
            "VALUES ('key-1', 'nota', 'C:\\a.mp4', 1.0)")
        con.commit()

    def test_drop_superseded_quita_fila_y_actualiza_last_path(self):
        con = self.open()
        self._add_two_copies_with_note(con)
        gone = SimpleNamespace(vol=1, rel="a.mp4")
        removed, updated = database.drop_superseded([gone], {"key-1": "F:\\a.mp4"})
        self.assertEqual((removed, updated), (1, 1))
        self.assertEqual(con.execute("SELECT vol, rel FROM files").fetchall(),
                         [(2, "a.mp4")])
        self.assertEqual(
            con.execute("SELECT note, last_path FROM notes WHERE key = 'key-1'").fetchone(),
            ("nota", "F:\\a.mp4"),
        )

    def test_drop_superseded_sin_argumentos_no_hace_nada(self):
        self.assertEqual(database.drop_superseded([], {}), (0, 0))

    def test_drop_superseded_hace_rollback_si_falla_la_nota(self):
        con = self.open()
        self._add_two_copies_with_note(con)
        gone = SimpleNamespace(vol=1, rel="a.mp4")
        with self.assertRaises(sqlite3.Error):
            database.drop_superseded([gone], {"key-1": []})
        self.assertEqual(con.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
        self.assertEqual(
            con.execute("SELECT last_path FROM notes WHERE key = 'key-1'").fetchone()[0],
            "C:\\a.mp4",
        )

    # -- extensiones ----------------------------------------------------------
    def test_extensiones_roundtrip_y_fallback(self):
        con = self.open()
        self.assertIn(".mp4", database.load_extensions(con))
        database.save_extensions(con, {".mkv", ".avi"})
        self.assertEqual(database.load_extensions(con), {".mkv", ".avi"})
        database.save_extensions(con, set())
        self.assertIn(".mp4", database.load_extensions(con))

    def test_extensiones_invalidas_no_reemplazan_las_validas(self):
        con = self.open()
        database.save_extensions(con, {"...", "//"})
        self.assertIn(".mp4", database.load_extensions(con))

    # -- exportar / importar notas -------------------------------------------
    def test_exportar_e_importar_notas(self):
        con = self.open()
        vid = self.add_volume(con)
        rel = "sub/video.mp4"
        self.add_file(con, vid, rel, nkey="key-1")
        expected_path = display_path(self.tmp, rel)
        con.execute(
            "INSERT INTO notes (key, note, last_path, updated) VALUES (?, ?, ?, ?)",
            ("key-1", "nota uno", expected_path, 1.0),
        )
        con.commit()

        export_path = os.path.join(self.tmp, "notas.txt")
        self.assertEqual(database.export_notes_to(con, export_path), 1)
        with io.open(export_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(lines[0], "clave\tnota\truta")
        key, note, path = lines[1].split("\t")
        self.assertEqual((key, note, os.path.normpath(path)),
                         ("key-1", "nota uno", os.path.normpath(expected_path)))

        con.execute("DELETE FROM notes")
        con.commit()
        stats = database.import_notes_from(con, export_path)
        self.assertEqual(stats, {"nuevas": 1, "combinadas": 0,
                                 "sin_cambios": 0, "invalidas": 0})
        self.assertEqual(
            con.execute("SELECT note FROM notes WHERE key = 'key-1'").fetchone()[0],
            "nota uno",
        )

        stats = database.import_notes_from(con, export_path)
        self.assertEqual(stats["sin_cambios"], 1)

    def test_importar_combina_etiquetas_sin_pisar_la_nota(self):
        con = self.open()
        con.execute("INSERT INTO notes (key, note) VALUES ('key-1', 'uno')")
        con.commit()
        path = os.path.join(self.tmp, "notas.txt")
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("clave\tnota\truta\nkey-1\tuno dos\t\n")
        stats = database.import_notes_from(con, path)
        self.assertEqual(stats["combinadas"], 1)
        self.assertEqual(
            con.execute("SELECT note FROM notes WHERE key = 'key-1'").fetchone()[0],
            "uno dos",
        )

    def test_importar_lineas_invalidas(self):
        con = self.open()
        path = os.path.join(self.tmp, "notas.txt")
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "clave\tnota\truta\n"
                "\tnota sin clave\t\n"
                "key-2\t\t\n"
                "key-3\tnota tres\t\n"
            )
        stats = database.import_notes_from(con, path)
        self.assertEqual(stats, {"nuevas": 1, "combinadas": 0,
                                 "sin_cambios": 0, "invalidas": 2})
        self.assertEqual(
            con.execute("SELECT note FROM notes WHERE key = 'key-3'").fetchone()[0],
            "nota tres",
        )

    def test_importar_no_borra_notas_con_linea_vacia(self):
        con = self.open()
        con.execute("INSERT INTO notes (key, note) VALUES ('key-1', 'conservar')")
        con.commit()
        path = os.path.join(self.tmp, "notas.txt")
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("clave\tnota\truta\nkey-1\t\t\n")
        stats = database.import_notes_from(con, path)
        self.assertEqual(stats["invalidas"], 1)
        self.assertEqual(
            con.execute("SELECT note FROM notes WHERE key = 'key-1'").fetchone()[0],
            "conservar",
        )

    # -- migracion v1 -> v2 ---------------------------------------------------
    def _create_v1(self, root, file_path, with_fingerprint=False):
        con = sqlite3.connect(self.db_path)
        try:
            con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
            con.execute("INSERT INTO meta (k, v) VALUES ('roots', ?)", (root,))
            if with_fingerprint:
                con.execute(
                    "CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, "
                    "mtime REAL NOT NULL, duration REAL NOT NULL, fp TEXT, nkey TEXT)")
                con.execute(
                    "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
                    (file_path, 10, 100.0, 12.5, "fp-x", "key-x"))
            else:
                con.execute(
                    "CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, "
                    "mtime REAL NOT NULL, duration REAL NOT NULL)")
                con.execute(
                    "INSERT INTO files VALUES (?, ?, ?, ?)",
                    (file_path, 10, 100.0, 12.5))
            con.execute("PRAGMA user_version = 1")
            con.commit()
        finally:
            con.close()

    def _make_legacy_file(self):
        root = os.path.join(self.tmp, "legacy")
        folder = os.path.join(root, "sub")
        os.makedirs(folder)
        file_path = os.path.join(folder, "a.mp4")
        with io.open(file_path, "wb") as fh:
            fh.write(b"0123456789")
        return root, file_path

    def test_migracion_v1_a_v2(self):
        root, file_path = self._make_legacy_file()
        self._create_v1(root, file_path)
        info = VolInfo("TESTVOL", "Test", self.tmp, 0, "fixed")
        with mock.patch.object(database, "identify_volume", return_value=info):
            con = self.open()

        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0],
                         database.SCHEMA_VERSION)
        volumes = con.execute("SELECT ident, label, mount FROM volumes").fetchall()
        self.assertEqual(volumes, [("TESTVOL", "Test", self.tmp)])
        roots = con.execute("SELECT rel FROM roots").fetchall()
        self.assertEqual(roots, [("legacy",)])
        files = con.execute(
            "SELECT rel, size, duration, fp, nkey FROM files").fetchall()
        self.assertEqual(files, [("legacy/sub/a.mp4", 10, 12.5, None, None)])
        backups = glob.glob(self.db_path + ".bak_v1_*")
        self.assertEqual(len(backups), 1)

    def test_migracion_conserva_huella_y_clave(self):
        root, file_path = self._make_legacy_file()
        self._create_v1(root, file_path, with_fingerprint=True)
        info = VolInfo("TESTVOL", "Test", self.tmp, 0, "fixed")
        with mock.patch.object(database, "identify_volume", return_value=info):
            con = self.open()
        files = con.execute(
            "SELECT rel, fp, nkey FROM files").fetchall()
        self.assertEqual(files, [("legacy/sub/a.mp4", "fp-x", "key-x")])

    def test_migracion_de_raiz_inexistente_usa_legado(self):
        root = os.path.join(self.tmp, "disco_que_no_esta")
        self._create_v1(root, os.path.join(root, "b.mp4"))
        con = self.open()
        ident, mount = con.execute(
            "SELECT ident, mount FROM volumes").fetchone()
        self.assertTrue(ident.startswith("legacy:"))
        self.assertEqual(mount, root)
        self.assertEqual(
            con.execute("SELECT rel FROM files").fetchone()[0], "b.mp4")


class SettingsAndRelocateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self._old_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "videos_index.db")
        self.addCleanup(setattr, database, "DB_PATH", self._old_db_path)

    def open(self):
        con = database.open_db()
        self.addCleanup(con.close)
        return con

    def add_volume(self, con, ident="VOL1"):
        info = VolInfo(ident, "Disco", self.tmp, 0, "fixed")
        return database.get_or_create_volume(con, info)

    def add_file(self, con, vid, rel, nkey="key-1"):
        con.execute(
            "INSERT INTO files (vol, rel, size, mtime, duration, fp, nkey) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (vid, rel, 10, 1.0, 12.5, "fp", nkey))
        con.commit()

    def test_setting_json_hace_roundtrip_y_tolerda_valor_invalido(self):
        con = self.open()
        self.assertIsNone(database.get_setting(con, "missing"))
        self.assertEqual(database.get_setting(con, "missing", 42), 42)
        database.set_setting(con, "filtros", {"a": [1, "ñ"], "b": True})
        self.assertEqual(database.get_setting(con, "filtros"),
                         {"a": [1, "ñ"], "b": True})
        con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('roto', '{no json')")
        con.commit()
        self.assertEqual(database.get_setting(con, "roto", "por defecto"), "por defecto")

    def test_relocate_files_actualiza_rutas_y_conserva_notas(self):
        con = self.open()
        vid = self.add_volume(con)
        self.add_file(con, vid, "videos/a.mp4", nkey="huella")
        con.execute(
            "INSERT INTO notes (key, note, last_path, updated) VALUES (?, ?, ?, ?)",
            ("huella", "nota", os.path.join(self.tmp, "videos", "a.mp4"), 1.0))
        con.commit()
        new_path = os.path.join(self.tmp, "dest", "a.mp4")
        changed = database.relocate_files([(vid, "videos/a.mp4", "dest/a.mp4",
                                           "huella", new_path)])
        self.assertEqual(changed, 1)
        self.assertEqual(con.execute("SELECT rel FROM files").fetchall(),
                         [("dest/a.mp4",)])
        self.assertEqual(con.execute("SELECT note, last_path FROM notes").fetchone(),
                         ("nota", new_path))

    def test_relocate_files_no_deja_cambios_parciales(self):
        con = self.open()
        vid = self.add_volume(con)
        self.add_file(con, vid, "a.mp4")
        self.add_file(con, vid, "b.mp4")
        with self.assertRaises(ValueError):
            database.relocate_files([
                (vid, "a.mp4", "c.mp4", None, "c.mp4"),
                (vid, "b.mp4", "c.mp4", None, "c.mp4"),
            ])
        self.assertEqual({r[0] for r in con.execute("SELECT rel FROM files")},
                         {"a.mp4", "b.mp4"})


if __name__ == "__main__":
    unittest.main()
