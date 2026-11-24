# -*- coding: utf-8 -*-
"""Prueba de humo: la ventana principal se construye y carga sin errores."""

import os
import tempfile
import unittest
from unittest import mock

try:
    import tkinter as tk

    from vdb import __version__, database, theme, ui
    from vdb.presence import MISSING, OFFLINE, PRESENT, UNKNOWN
    from vdb.volumes import VolInfo
except ImportError:  # tkinter no instalado
    tk = None
    database = theme = ui = VolInfo = None
    __version__ = ""
    MISSING = OFFLINE = PRESENT = UNKNOWN = None


def make_row(name, state, online=None, key=None, disk=None, path=None, folder=None,
             created=None, modified=None):
    path = path or "C:\\videos\\" + name
    if online is None:
        online = state != OFFLINE
    return database.Row(
        path, name, folder or "C:\\videos", 100, 60.0, key or "key-" + name, "",
        path.lower(), "", path.lower(), 1, name, disk or "Disco (C:)",
        online, "fixed", state, "C:\\", created, modified,
    )


@unittest.skipIf(ui is None, "tkinter no esta instalado")
class UiSmokeTest(unittest.TestCase):
    def open_app(self, tmp, rows=None):
        old_path = database.DB_PATH
        database.DB_PATH = os.path.join(tmp, "videos_index.db")
        self.addCleanup(setattr, database, "DB_PATH", old_path)
        try:
            root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"Tk no disponible: {e}")
        self.addCleanup(root.destroy)
        root.withdraw()
        with mock.patch.object(ui, "list_connected_volumes", return_value={}), \
                mock.patch.object(ui, "find_ffprobe", return_value="ffprobe"), \
                mock.patch.object(ui, "fetch_rows", return_value=rows or []):
            app = ui.App(root)
        root.update_idletasks()
        self._app = app
        return app

    def seed_moved_video(self):
        """Dos copias con la misma huella y una nota en la ruta vieja."""
        con = database.open_db()
        try:
            con.executemany(
                "INSERT INTO volumes (id, ident, label, mount, kind, workers) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(1, "C", "C", "C:\\", "fixed", 4), (2, "F", "F", "F:\\", "fixed", 4)],
            )
            con.executemany(
                "INSERT INTO files (vol, rel, size, mtime, duration, fp, nkey) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(1, "video.mp4", 100, 1.0, 60.0, "fp", "huella-1"),
                 (2, "video.mp4", 100, 1.0, 60.0, "fp", "huella-1")],
            )
            con.execute(
                "INSERT INTO notes (key, note, last_path, updated) VALUES (?, ?, ?, ?)",
                ("huella-1", "nota", "C:\\videos\\video.mp4", 1.0),
            )
            con.commit()
        finally:
            con.close()

    def read_index(self):
        con = database.open_db()
        try:
            files = con.execute("SELECT vol, rel FROM files ORDER BY vol").fetchall()
            note = con.execute(
                "SELECT note, last_path FROM notes WHERE key = 'huella-1'").fetchone()
            return files, note
        finally:
            con.close()

    def moved_rows(self):
        gone = make_row("video.mp4", MISSING, key="huella-1",
                        disk="Disco (C:)", path="C:\\videos\\video.mp4")
        good = make_row("video.mp4", PRESENT, key="huella-1",
                        disk="Disco (F:)", path="F:\\videos\\video.mp4")
        return gone._replace(vol=1, rel="video.mp4"), good._replace(vol=2, rel="video.mp4")

    def live_moved_rows(self, all_rows):
        """Filas que siguen en la base, con los estados de la prueba."""
        con = database.open_db()
        try:
            live = set(con.execute("SELECT vol, rel FROM files").fetchall())
        finally:
            con.close()
        return [r for r in all_rows if (r.vol, r.rel) in live]

    def test_ventana_principal_se_construye(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp)
            self.assertEqual(app.rows, [])
            self.assertEqual(app.view, [])
            self.assertIn("0 videos", app.status_var.get())

    def test_barras_de_desplazamiento_configuradas(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp)
            self.assertTrue(app.tree.cget("yscrollcommand"))
            self.assertTrue(app.tree.cget("xscrollcommand"))
            self.assertEqual(str(app.vsb.cget("orient")), "vertical")
            self.assertEqual(str(app.hsb.cget("orient")), "horizontal")
            self.assertEqual(str(app.vsb.grid_info()["row"]), "0")
            self.assertEqual(str(app.vsb.grid_info()["column"]), "1")
            self.assertEqual(str(app.hsb.grid_info()["row"]), "1")
            self.assertEqual(str(app.hsb.grid_info()["column"]), "0")
            self.assertGreater(app.vsb.winfo_reqwidth(), 1)
            self.assertGreater(app.hsb.winfo_reqheight(), 1)

    def test_columna_estado_y_filtro_no_disponibles(self):
        rows = [
            make_row("a.mp4", PRESENT),
            make_row("b.mp4", MISSING),
            make_row("c.mp4", OFFLINE),
            make_row("d.mp4", UNKNOWN),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp, rows)
            labels = [app.tree.item(iid, "values")[5]
                      for iid in app.tree.get_children()]
            self.assertEqual(labels, ["Presente", "Falta", "Disco desconectado",
                                      "Sin verificar"])
            self.assertEqual(len(app.view), 4)

            app.hide_unavailable_var.set(True)
            app.refresh()
            self.assertEqual(len(app.view), 2)
            self.assertIn("2 no disponibles ocultos", app.summary)

            app.hide_unavailable_var.set(False)
            app.refresh()
            self.assertEqual(len(app.view), 4)

    def test_orden_por_estado_agrupa_los_no_disponibles(self):
        rows = [
            make_row("a.mp4", UNKNOWN),
            make_row("b.mp4", OFFLINE),
            make_row("c.mp4", PRESENT),
            make_row("d.mp4", MISSING),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp, rows)
            app.sort_by("presence")
            self.assertEqual([r.presence for r in app.view],
                             [PRESENT, MISSING, OFFLINE, UNKNOWN])

    def test_columnas_de_fechas_y_orden(self):
        rows = [
            make_row("sin.mp4", PRESENT),
            make_row("viejo.mp4", PRESENT, created=1000000000, modified=1100000000),
            make_row("nuevo.mp4", PRESENT, created=1500000000, modified=1600000000),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp, rows)
            self.assertEqual(len(app.tree["columns"]), 9)
            self.assertEqual(app.tree.heading("created", "text"), "Creado")
            self.assertEqual(app.tree.heading("modified", "text"), "Modificado")
            values = [app.tree.item(iid, "values") for iid in app.tree.get_children()]
            self.assertEqual(values[0][7], "—")
            self.assertEqual(values[1][7], ui.fmt_date(1000000000))

            app.sort_by("created")
            self.assertEqual([r.name for r in app.view],
                             ["nuevo.mp4", "viejo.mp4", "sin.mp4"])
            app.sort_by("created")
            self.assertEqual([r.name for r in app.view],
                             ["viejo.mp4", "nuevo.mp4", "sin.mp4"])

    def test_leyenda_con_etapas_y_barra_de_progreso(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp)
            self.assertIsNotNone(app.progress)
            app.scan_job_index = 2
            app.scan_job_total = 3
            app.scan_status(2, "10 carpetas, 4 videos")
            text = app.status_var.get()
            self.assertIn("Disco 2 de 3", text)
            self.assertIn("Etapa 2 de 7: Recorriendo carpetas", text)
            self.assertIn("10 carpetas, 4 videos", text)
            app.presence_status(1, "5 / 20")
            self.assertIn("Etapa 1 de 2: Verificando archivos", app.status_var.get())

    def test_duplicados_conservan_la_ubicacion_presente(self):
        gone = make_row("video.mp4", MISSING, key="huella-1",
                        disk="Disco (C:)", path="C:\\videos\\video.mp4")
        good = make_row("video.mp4", PRESENT, key="huella-1",
                        disk="Disco (F:)", path="F:\\videos\\video.mp4")
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp, [gone, good])
            self.assertEqual(len(app.view), 1)
            self.assertEqual(app.view[0].disk, "Disco (F:)")
            self.assertIn("1 copia no disponible ocultada", app.summary)
            self.assertIn("Disco (C:)", app.duplicate_detail())

    def test_dos_copias_presentes_se_conservan(self):
        one = make_row("video.mp4", PRESENT, key="huella-1", disk="Disco (C:)")
        two = make_row("video.mp4", PRESENT, key="huella-1", disk="Disco (F:)",
                       path="F:\\videos\\video.mp4")
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp, [one, two])
            self.assertEqual(len(app.view), 2)
            self.assertEqual(app.duplicate_hidden, [])


    def test_ruta_completa_en_menu_tooltip_y_dialogo(self):
        long_path = "F:\\Series\\Temporada 04\\Carpeta con un nombre muy largo " * 2 + "video.mkv"
        row = make_row("video.mkv", PRESENT, path=long_path,
                       folder=os.path.dirname(long_path), disk="Disco (F:)")
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp, [row])
            labels = [app.menu.entrycget(i, "label")
                      for i in range(int(app.menu.index("end")) + 1)
                      if app.menu.type(i) == "command"]
            self.assertIn("Ver ruta completa…", labels)

            text = ui.location_text(row)
            self.assertIn(long_path, text)
            self.assertIn("Carpeta: " + os.path.dirname(long_path), text)
            self.assertIn("Disco: Disco (F:)", text)

            app.show_tooltip(row, 10, 10)
            self.assertIsNotNone(app.tip)
            self.assertEqual(app.tip.winfo_children()[0].cget("text"), text)
            app.refresh()
            self.assertIsNone(app.tip)

            with mock.patch.object(ui, "PathDialog") as dialog:
                app.tree.selection_set("0")
                app.show_path_dialog()
                dialog.assert_called_once_with(app.master, row)

    def test_botones_con_tooltip_y_ayuda_de_primer_uso(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp)
            self.assertEqual(app.btn_help.cget("text"), "Ayuda / Primer uso…")
            for widget in (app.btn_scan, app.btn_disks, app.btn_update, app.btn_verify,
                           app.btn_cancel, app.chk_retry, app.btn_annotate,
                           app.btn_help, app.ent_search, app.cb_scope, app.cb_notes,
                           app.ent_min, app.ent_max, app.chk_hide):
                self.assertTrue(widget.bind("<Enter>"))
                self.assertTrue(widget.bind("<Leave>"))

            app.tips.show(ui.WIDGET_TIPS["scan"], 10, 10)
            self.assertIsNotNone(app.tip)
            tip_text = app.tip.winfo_children()[0].cget("text")
            self.assertEqual(tip_text, ui.WIDGET_TIPS["scan"])
            app.hide_tooltip()
            self.assertIsNone(app.tip)

            with mock.patch.object(ui.tk.Toplevel, "wait_visibility"), \
                    mock.patch.object(ui.tk.Toplevel, "grab_set"), \
                    mock.patch.object(ui.tk.Toplevel, "wait_window"):
                app.show_help()
            dialogs = [w for w in app.master.winfo_children()
                       if hasattr(w, "title") and w.title() == "Ayuda / Primer uso"]
            self.assertEqual(len(dialogs), 1)
            texts = []
            stack = list(dialogs[0].winfo_children())
            while stack:
                widget = stack.pop()
                stack.extend(widget.winfo_children())
                if isinstance(widget, tk.Text):
                    texts.append(widget.get("1.0", "end-1c"))
            self.assertEqual(len(texts), 1)
            self.assertIn("PRIMER USO", texts[0])
            self.assertIn("USOS POSTERIORES", texts[0])
            self.assertIn("CAMBIOS DE UBICACIÓN", texts[0])

    def test_verificacion_quita_ruta_vieja_y_conserva_nota(self):
        gone, good = self.moved_rows()
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp, [gone, good])
            self.seed_moved_video()
            with mock.patch.object(ui, "fetch_rows",
                                   side_effect=lambda c, p=None: self.live_moved_rows(
                                       [gone, good])):
                app.finish_presence({}, False)
            files, note = self.read_index()
            self.assertEqual(files, [(2, "video.mp4")])
            self.assertEqual(note, ("nota", "F:\\videos\\video.mp4"))
            self.assertIn("1 ubicación anterior quitada", app.pruned_notice)

    def test_verificacion_cancelada_no_quita_nada(self):
        gone, good = self.moved_rows()
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp, [gone, good])
            self.seed_moved_video()
            with mock.patch.object(ui, "fetch_rows",
                                   side_effect=lambda c, p=None: self.live_moved_rows(
                                       [gone, good])):
                app.finish_presence({}, True)
            files, note = self.read_index()
            self.assertEqual(files, [(1, "video.mp4"), (2, "video.mp4")])
            self.assertEqual(note, ("nota", "C:\\videos\\video.mp4"))
            self.assertEqual(app.pruned_notice, "")

    def test_escaneo_quita_ruta_vieja_de_la_raiz_escaneada(self):
        gone, good = self.moved_rows()
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp, [gone, good])
            self.seed_moved_video()
            app.scan_jobs = [database.Job(2, "F", "F:\\", "", 4, "Disco (F:)")]
            with mock.patch.object(ui, "list_connected_volumes", return_value={}), \
                    mock.patch.object(ui, "fetch_rows",
                                      side_effect=lambda c, p=None:
                                      self.live_moved_rows([gone, good])):
                app.finish_scan(completed=True)
            files, note = self.read_index()
            self.assertEqual(files, [(2, "video.mp4")])
            self.assertEqual(note, ("nota", "F:\\videos\\video.mp4"))
            self.assertIn("1 ubicación anterior quitada", app.pruned_notice)


    def dialogo(self, title):
        for win in self._app.master.winfo_children():
            if hasattr(win, "title") and win.title() == title:
                return win
        return None

    def test_version_y_herramientas_nuevas(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp)
            self.assertIn("v" + __version__, app.master.title())
            labels = [app.menu.entrycget(i, "label")
                      for i in range(int(app.menu.index("end")) + 1)
                      if app.menu.type(i) == "command"]
            for name in ("btn_duplicates", "btn_bulk", "cb_saved"):
                self.assertIsNotNone(getattr(app, name))
            cascades = [app.menubar.entrycget(i, "label")
                        for i in range(int(app.menubar.index("end")) + 1)]
            self.assertIn("Ver", cascades)
            self.assertIn("Herramientas", cascades)

    def test_vista_de_duplicados(self):
        one = make_row("a.mp4", PRESENT, key="huella", path="C:\\videos\\a.mp4")
        two = make_row("copia.mp4", PRESENT, key="huella",
                       path="F:\\videos\\copia.mp4")
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp, [one, two])
            app.show_duplicates()
            dlg = self.dialogo("Videos duplicados")
            self.assertIsNotNone(dlg)
            self.assertEqual(len(dlg.groups), 1)
            self.assertEqual(len(dlg.tree.get_children()), 1)
            self.assertEqual(len(dlg.tree.get_children(dlg.tree.get_children()[0])), 2)
            dlg.destroy()

    def test_filtros_guardados_se_guardan_aplican_y_borran(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp)
            app.search_var.set("drama")
            app.scope_var.set("Solo notas")
            app.hide_unavailable_var.set(True)
            app.save_current_filter("Drama")
            self.assertIn("Drama", ui.saved_filters.names(app.saved_filters))

            con = database.open_db()
            try:
                stored = database.get_setting(
                    con, ui.saved_filters.SETTING_NAME, {})
            finally:
                con.close()
            self.assertEqual(stored["Drama"]["search"], "drama")

            app.search_var.set("")
            app.scope_var.set("Ruta y notas")
            app.hide_unavailable_var.set(False)
            self.assertTrue(app.apply_saved_filter("Drama"))
            self.assertEqual(app.search_var.get(), "drama")
            self.assertEqual(app.scope_var.get(), "Solo notas")
            self.assertTrue(app.hide_unavailable_var.get())

            app.delete_saved_filter("Drama")
            self.assertEqual(app.saved_filters, {})
            self.assertFalse(app.apply_saved_filter("Drama"))

    def test_tema_oscuro_se_aplica_y_persiste(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.open_app(tmp)
            app.theme_var.set(True)
            app.toggle_theme()
            self.assertTrue(app.theme_dark)
            self.assertEqual(str(app.tree.tag_configure("missing", "foreground")),
                             theme.DARK["missing"])
            con = database.open_db()
            try:
                self.assertIs(database.get_setting(con, "dark_theme"), True)
            finally:
                con.close()
            app.theme_var.set(False)
            app.toggle_theme()
            self.assertFalse(app.theme_dark)

    def test_renombrado_masivo_se_previsualiza_y_aplica(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Parchear DB_PATH antes de sembrar para no tocar la base real.
            old_path = database.DB_PATH
            database.DB_PATH = os.path.join(tmp, "videos_index.db")
            self.addCleanup(setattr, database, "DB_PATH", old_path)
            name = "a.mp4"
            source = os.path.join(tmp, name)
            with open(source, "wb"):
                pass
            con = database.open_db()
            try:
                vid = database.get_or_create_volume(
                    con, VolInfo("C", "C", tmp, 0, "fixed"))
                con.execute(
                    "INSERT INTO files (vol, rel, size, mtime, duration, fp, nkey) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (vid, name, 1, 1.0, 60.0, "fp", "huella"))
                con.commit()
            finally:
                con.close()
            row = make_row(name, PRESENT, key="huella", path=source,
                           folder=tmp)._replace(vol=vid, rel=name, mount=tmp)
            app = self.open_app(tmp, [row])
            app.tree.selection_set("0")
            app.show_bulk()
            dlg = self.dialogo("Renombrado y organización masiva")
            self.assertIsNotNone(dlg)
            dlg.tpl_var.set("{title} - nuevo")
            dlg.preview_rename()
            self.assertEqual(dlg.plans[0].new_rel, "a - nuevo.mp4")
            self.assertIn("1 cambios listos", dlg.status_var.get())

            with mock.patch.object(ui.messagebox, "askyesno", return_value=True):
                dlg.apply()
            self.assertTrue(os.path.isfile(os.path.join(tmp, "a - nuevo.mp4")))
            con = database.open_db()
            try:
                self.assertEqual(con.execute("SELECT rel FROM files").fetchall(),
                                 [("a - nuevo.mp4",)])
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
