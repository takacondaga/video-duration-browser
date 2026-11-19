"""Interfaz Tkinter de la aplicacion."""

import os
import queue
import sqlite3
import subprocess
import threading
import time
from collections import Counter

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .config import DEFAULT_VIDEO_EXT, MAX_ROWS_SHOWN, NT
from .database import (
    Job, add_root, build_jobs, drop_superseded, export_notes_to, fetch_rows,
    get_or_create_volume, import_notes_from, load_extensions, open_db,
    remove_file_records, save_extensions, save_notes, volume_states,
)
from .fileops import delete_permanently, send_to_trash
from .paths import disk_text, display_path, ncase, rel_of
from .presence import (
    MISSING, OFFLINE, PRESENCE_ORDER, PRESENT, consolidate_rows, presence_blocked,
    presence_label, presence_worker, superseded_locations,
)
from .scan import definitely_missing, find_ffprobe, root_alive, scan_worker
from .text import (
    clean_note, fold, fmt_dur, fmt_size, fmt_ts, merge_tags, parse_ext_list,
    parse_minutes, remove_tags, tag_index,
)
from .volumes import DEFAULT_WORKERS, identify_volume, list_connected_volumes


# ----------------------------------------------------------------------------
# Interfaz
# ----------------------------------------------------------------------------

COLS = ("name", "duration", "size", "notes", "disk", "presence", "folder")
HEADINGS = {
    "name": "Nombre",
    "duration": "Duración",
    "size": "Tamaño",
    "notes": "Notas",
    "disk": "Disco",
    "presence": "Estado",
    "folder": "Carpeta",
}
SORT_KEYS = {
    "name": lambda r: r.name.lower(),
    "duration": lambda r: r.duration,
    "size": lambda r: r.size,
    "notes": lambda r: r.fnote,
    "disk": lambda r: r.disk.lower(),
    "presence": lambda r: PRESENCE_ORDER.get(r.presence, len(PRESENCE_ORDER)),
    "folder": lambda r: r.folder.lower(),
}
NUMERIC_COLS = {"duration", "size"}
SCOPES = ("Ruta y notas", "Solo notas", "Solo ruta")
NOTE_FILTERS = ("Todos", "Con nota", "Sin nota")
SCAN_STAGES = (
    "Preparando disco",
    "Recorriendo carpetas",
    "Leyendo índice anterior",
    "Comparando con el índice",
    "Leyendo duraciones y huellas",
    "Guardando índice y notas",
    "Consolidando ubicaciones",
)
PRESENCE_STAGES = ("Verificando archivos", "Consolidando ubicaciones")


def stage_text(stages, stage, detail=""):
    """Texto de progreso con etapa siempre visible."""
    text = f"Etapa {stage} de {len(stages)}: {stages[stage - 1]}"
    return f"{text} — {detail}" if detail else text


def location_text(row):
    """Ruta completa de una fila, para el tooltip y el dialogo."""
    return f"Archivo: {row.path}\nCarpeta: {row.folder}\nDisco: {row.disk}"


WIDGET_TIPS = {
    "scan": "Primer uso: elegí la carpeta o el disco a indexar.",
    "disks": "Discos conocidos: marcá cuáles escanear o quitá del índice.",
    "update": "Usos posteriores: volvé a recorrer los discos ya configurados.",
    "verify": "Comprueba qué archivos siguen presentes sin borrar nada.",
    "cancel": "Detiene el escaneo o la verificación en curso.",
    "retry": "Incluye carpetas ilegibles en el próximo escaneo.",
    "annotate": "Agrega o edita la nota de la fila seleccionada; se guarda por contenido.",
    "help": "Muestra el orden de botones para el primer uso y para los siguientes.",
    "search": "Filtra por texto; el ámbito elige si busca en ruta, notas o ambos.",
    "scope": "Elige dónde buscar el texto: ruta y notas, solo notas o solo ruta.",
    "notes_filter": "Muestra todas las filas, solo las que tienen nota o solo las que no.",
    "min_duration": "Duración mínima en minutos (dejar vacío para no filtrar).",
    "max_duration": "Duración máxima en minutos (dejar vacío para no filtrar).",
    "hide_unavailable": "Esconde Falta y Disco desconectado y lanza la verificación.",
}

HELP_TEXT = """PRIMER USO

1. Escanear carpeta/disco…: elegí la carpeta o el disco que querés catalogar.
   Necesitás ffprobe.exe junto al script o en el PATH para leer duraciones.
2. Cuando termine el escaneo, seleccioná videos y usá Anotar… (F2) para
   agregar notas o etiquetas. La nota va con el contenido, no con la ruta.
3. Usá Verificar ahora para comprobar qué archivos siguen presentes.

USOS POSTERIORES

1. Conectá los discos que quieras consultar.
2. Actualizar índice: vuelve a recorrer los discos ya configurados y quita
   del índice los archivos que ya no están. Si cambiaste de carpetas, usá
   Discos… para elegir las raíces.
3. Verificar ahora: comprobá qué archivos siguen presentes después de mover
   o borrar videos. Se puede cancelar y no modifica las notas.
4. Usá los filtros (Buscar, Notas, Duración y Ocultar no disponibles) y
   hacé clic en un encabezado para ordenar.
5. Doble clic abre el video; clic derecho → Ver ruta completa… muestra la
   ruta sin cortar.

CAMBIOS DE UBICACIÓN

Si el mismo video aparece en otra ubicación, se conserva la copia presente:
la ruta vieja se quita del índice, se actualiza la ruta guardada en la nota
y el texto de la nota se mantiene. Si ambas copias están presentes, se
conservan las dos. Al terminar una verificación cancelada no se quita nada.
"""


class TipManager:
    """Tooltips programados, compartidos por la tabla y los controles."""

    DELAY = 300

    def __init__(self, master):
        self.master = master
        self.tip = None
        self.after = None
        self.key = None

    def bind(self, widget, text):
        """Muestra `text` al pasar sobre `widget`; lo oculta al salir o hacer clic."""
        def on_enter(event):
            self.schedule(widget, text, event.x_root, event.y_root)
        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", lambda _e: self.hide())
        widget.bind("<ButtonPress>", lambda _e: self.hide())

    def schedule(self, key, text, x, y):
        if self.key is key:
            return
        self.hide()
        self.key = key
        self.after = self.master.after(self.DELAY, lambda: self.show(text, x, y))

    def show(self, text, x, y):
        self.after = None
        if self.tip is not None:
            self.tip.destroy()
        self.tip = tk.Toplevel(self.master)
        self.tip.wm_overrideredirect(True)
        label = tk.Label(
            self.tip, text=text, justify="left", wraplength=520,
            background="#ffffe0", relief="solid", bd=1, padx=6, pady=4,
        )
        label.pack()
        self.tip.update_idletasks()
        w, h = self.tip.winfo_width(), self.tip.winfo_height()
        sx, sy = x + 14, y + 18
        sw, sh = self.tip.winfo_screenwidth(), self.tip.winfo_screenheight()
        if sx + w > sw:
            sx = max(0, sw - w - 4)
        if sy + h > sh:
            sy = max(0, sh - h - 4)
        self.tip.wm_geometry(f"+{sx}+{sy}")

    def hide(self):
        if self.after is not None:
            try:
                self.master.after_cancel(self.after)
            except tk.TclError:
                pass
            self.after = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None
        self.key = None


class HelpDialog(tk.Toplevel):
    """Muestra el orden de botones para el primer uso y los posteriores."""

    def __init__(self, master):
        super().__init__(master)
        self.title("Ayuda / Primer uso")
        self.transient(master)
        self.resizable(True, True)
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        text = tk.Text(frm, wrap="word", height=20, width=80)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)
        bf = ttk.Frame(frm)
        bf.pack(fill="x", pady=(8, 0))
        ttk.Button(bf, text="Cerrar", command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda _e: self.destroy())
        self.geometry(f"+{master.winfo_rootx() + 80}+{master.winfo_rooty() + 80}")
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window(self)


class NoteDialog(tk.Toplevel):
    """Dialogo de edicion de notas. result = (modo, texto) o None."""

    def __init__(self, master, count, initial, tags):
        super().__init__(master)
        self.title("Anotar")
        self.transient(master)
        self.resizable(False, False)
        self.result = None
        self.multi = count > 1
        self.tag_names = [t for t, _ in tags]

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        label = "1 video seleccionado" if count == 1 else f"{count} videos seleccionados"
        ttk.Label(frm, text=label).pack(anchor="w")

        self.var = tk.StringVar(value=initial)
        self.entry = ttk.Entry(frm, textvariable=self.var, width=70)
        self.entry.pack(fill="x", pady=(6, 6))

        self.mode = tk.StringVar(value="add" if self.multi else "replace")
        if self.multi:
            mf = ttk.Frame(frm)
            mf.pack(anchor="w", pady=(0, 6))
            for value, text in (
                ("add", "Agregar etiquetas"),
                ("replace", "Reemplazar nota"),
                ("remove", "Quitar etiquetas"),
            ):
                ttk.Radiobutton(mf, text=text, value=value, variable=self.mode).pack(
                    side="left", padx=(0, 12)
                )

        if tags:
            ttk.Label(frm, text="Etiquetas existentes (doble clic para agregar):").pack(anchor="w")
            lf = ttk.Frame(frm)
            lf.pack(fill="both", expand=True, pady=(2, 6))
            self.listbox = tk.Listbox(lf, height=8, exportselection=False)
            sb = ttk.Scrollbar(lf, orient="vertical", command=self.listbox.yview)
            self.listbox.configure(yscrollcommand=sb.set)
            self.listbox.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            for t, c in tags:
                self.listbox.insert("end", f"{t}  ({c})")
            self.listbox.bind("<Double-1>", self.on_tag_dbl)

        bf = ttk.Frame(frm)
        bf.pack(fill="x", pady=(6, 0))
        ttk.Button(bf, text="Cancelar", command=self.on_cancel).pack(side="right")
        ttk.Button(bf, text="Aceptar", command=self.on_ok).pack(side="right", padx=(0, 6))

        self.bind("<Return>", lambda e: self.on_ok())
        self.bind("<Escape>", lambda e: self.on_cancel())
        self.protocol("WM_DELETE_WINDOW", self.on_cancel)

        self.update_idletasks()
        self.geometry(f"+{master.winfo_rootx() + 80}+{master.winfo_rooty() + 80}")
        self.entry.focus_set()
        self.entry.icursor("end")
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window(self)

    def on_tag_dbl(self, _event):
        sel = self.listbox.curselection()
        if not sel:
            return
        tag = self.tag_names[sel[0]]
        cur = self.var.get()
        if fold(tag) in {fold(t) for t in cur.split()}:
            return
        self.var.set((cur.rstrip() + " " + tag).strip())
        self.entry.icursor("end")

    def on_ok(self):
        self.result = (self.mode.get(), clean_note(self.var.get()))
        self.destroy()

    def on_cancel(self):
        self.result = None
        self.destroy()


class DisksDialog(tk.Toplevel):
    """Lista de discos conocidos y conectados, con acciones por disco."""

    def __init__(self, app):
        super().__init__(app.master)
        self.app = app
        self.items = {}
        self.title("Discos")
        self.transient(app.master)
        self.geometry("900x420")

        top = ttk.Frame(self, padding=(8, 8, 8, 0))
        top.pack(fill="both", expand=True)
        cols = ("disk", "mount", "state", "files", "last", "workers")
        self.tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="browse")
        spec = (
            ("disk", "Disco", 220, "w"), ("mount", "Ubicación", 170, "w"),
            ("state", "Estado", 150, "w"), ("files", "Archivos", 70, "e"),
            ("last", "Último escaneo", 130, "w"), ("workers", "Hilos", 50, "e"),
        )
        for col, title, width, anchor in spec:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "disk"))
        sb = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        ttk.Label(
            self,
            text=("Los archivos de discos desconectados siguen apareciendo en las búsquedas "
                  "(en gris). Quitar un disco del índice no toca sus archivos ni las notas."),
            padding=(8, 6), wraplength=860,
        ).pack(fill="x")

        bf = ttk.Frame(self, padding=(8, 0, 8, 8))
        bf.pack(fill="x")
        ttk.Button(bf, text="Escanear / actualizar", command=self.action_scan).pack(side="left")
        ttk.Button(bf, text="Escanear carpeta…", command=self.action_folder).pack(
            side="left", padx=(6, 0))
        ttk.Button(bf, text="Quitar del índice", command=self.action_remove).pack(
            side="left", padx=(6, 0))
        ttk.Button(bf, text="Actualizar lista", command=self.reload).pack(
            side="left", padx=(6, 0))
        ttk.Button(bf, text="Cerrar", command=self.destroy).pack(side="right")
        self.spin_var = tk.StringVar(value="4")
        ttk.Button(bf, text="Aplicar", command=self.action_workers).pack(side="right", padx=(0, 12))
        ttk.Spinbox(bf, from_=1, to=32, width=4, textvariable=self.spin_var).pack(side="right")
        ttk.Label(bf, text="Hilos de lectura del disco:").pack(side="right", padx=(0, 4))

        self.reload()

    def reload(self):
        self.app.refresh_connected()
        con = open_db()
        try:
            states = volume_states(con, self.app.connected)
            counts = dict(con.execute("SELECT vol, COUNT(*) FROM files GROUP BY vol").fetchall())
        finally:
            con.close()
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        known = set()
        for st in sorted(states.values(),
                         key=lambda s: (not s.online, (s.label or s.mount or "").lower())):
            known.add(st.ident)
            state = "Conectado" if st.online else "Desconectado"
            if st.ident.startswith("legacy:"):
                state += " (legado)"
            iid = f"v{st.id}"
            self.items[iid] = ("vol", st)
            self.tree.insert("", "end", iid=iid, values=(
                disk_text(st.label, st.mount, st.online), st.mount or "", state,
                counts.get(st.id, 0), fmt_ts(st.last_scan), st.workers))
        n = 0
        for ident, info in self.app.connected.items():
            if ident in known:
                continue
            iid = f"n{n}"
            n += 1
            self.items[iid] = ("new", info)
            self.tree.insert("", "end", iid=iid, values=(
                disk_text(info.label, info.mount, True), info.mount, "Nuevo (sin escanear)",
                0, "nunca", DEFAULT_WORKERS.get(info.kind, 4)))

    def selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Sin selección", "Seleccionar un disco de la lista.")
            return None
        return self.items.get(sel[0])

    def on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        item = self.items.get(sel[0])
        if item is None:
            return
        kind, obj = item
        self.spin_var.set(str(obj.workers if kind == "vol" else DEFAULT_WORKERS.get(obj.kind, 4)))

    def action_scan(self):
        if self.app.is_scanning():
            messagebox.showinfo("Escaneo en curso", "Esperar a que termine el escaneo actual.")
            return
        item = self.selected()
        if item is None:
            return
        kind, obj = item
        if kind == "new":
            self.app.scan_new_volume(obj)
        else:
            if not obj.online:
                messagebox.showinfo(
                    "Disco desconectado",
                    f"Conectar el disco '{disk_text(obj.label, obj.mount, False)}' para escanearlo.")
                return
            self.app.scan_volume(obj.id)
        self.destroy()

    def action_folder(self):
        if self.app.is_scanning():
            messagebox.showinfo("Escaneo en curso", "Esperar a que termine el escaneo actual.")
            return
        self.destroy()
        self.app.on_scan_new()

    def action_remove(self):
        if self.app.is_scanning():
            messagebox.showinfo("Escaneo en curso", "Esperar a que termine el escaneo actual.")
            return
        item = self.selected()
        if item is None:
            return
        kind, st = item
        if kind == "new":
            messagebox.showinfo("Sin escanear", "Ese disco todavía no está en el índice.")
            return
        con = open_db()
        try:
            n = con.execute("SELECT COUNT(*) FROM files WHERE vol = ?", (st.id,)).fetchone()[0]
        finally:
            con.close()
        name = disk_text(st.label, st.mount, st.online)
        if not messagebox.askyesno(
            "Quitar del índice",
            f"Se quitarán {n} archivos de '{name}' del índice.\n"
            "Los archivos del disco no se tocan y las notas se conservan.\n\n¿Continuar?",
            icon="warning", default="no",
        ):
            return
        con = open_db()
        try:
            con.execute("DELETE FROM files WHERE vol = ?", (st.id,))
            con.execute("DELETE FROM roots WHERE vol = ?", (st.id,))
            con.execute("DELETE FROM volumes WHERE id = ?", (st.id,))
            con.commit()
        finally:
            con.close()
        self.app.load_rows()
        self.app.refresh()
        self.reload()

    def action_workers(self):
        item = self.selected()
        if item is None:
            return
        kind, st = item
        if kind != "vol":
            messagebox.showinfo("Sin escanear",
                                "Los hilos se pueden cambiar una vez que el disco está en el índice.")
            return
        try:
            n = int(self.spin_var.get())
        except ValueError:
            messagebox.showerror("Valor inválido", "Ingresar un número entero entre 1 y 32.")
            return
        n = max(1, min(32, n))
        con = open_db()
        try:
            con.execute("UPDATE volumes SET workers = ? WHERE id = ?", (n, st.id))
            con.commit()
        finally:
            con.close()
        self.reload()


class ExtensionsDialog(tk.Toplevel):
    """Extensiones consideradas video y conteo por extension del indice."""

    def __init__(self, app):
        super().__init__(app.master)
        self.app = app
        self.title("Extensiones de video")
        self.transient(app.master)
        self.geometry("640x500")

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Extensiones a indexar (separadas por coma o espacio):").pack(anchor="w")
        con = open_db()
        try:
            current = sorted(load_extensions(con))
        finally:
            con.close()
        self.var = tk.StringVar(value=", ".join(current))
        ttk.Entry(frm, textvariable=self.var).pack(fill="x", pady=(4, 4))
        ttk.Label(
            frm,
            text=("Los cambios se aplican en el próximo escaneo. Las extensiones quitadas se "
                  "eliminan del índice (no del disco). '.ts' y '.mts' solo cuentan si el "
                  "archivo pesa 1 MB o más, para excluir archivos de código TypeScript."),
            wraplength=600, justify="left",
        ).pack(anchor="w", pady=(0, 6))

        bf = ttk.Frame(frm)
        bf.pack(fill="x", pady=(0, 8))
        ttk.Button(bf, text="Guardar", command=self.on_save).pack(side="left")
        ttk.Button(bf, text="Restaurar predeterminadas", command=self.on_reset).pack(
            side="left", padx=(6, 0))
        ttk.Button(bf, text="Cerrar", command=self.destroy).pack(side="right")

        ttk.Label(frm, text="Archivos en el índice por extensión:").pack(anchor="w")
        lf = ttk.Frame(frm)
        lf.pack(fill="both", expand=True)
        tv = ttk.Treeview(lf, columns=("ext", "count"), show="headings", selectmode="browse")
        tv.heading("ext", text="Extensión")
        tv.heading("count", text="Archivos")
        tv.column("ext", width=200)
        tv.column("count", width=100, anchor="e")
        sb = ttk.Scrollbar(lf, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        counts = Counter(os.path.splitext(r.name)[1].lower() or "(sin extensión)"
                         for r in app.rows)
        for ext, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            tv.insert("", "end", values=(ext, n))

    def on_save(self):
        good, bad = parse_ext_list(self.var.get())
        if bad:
            messagebox.showerror("Extensiones inválidas", "No válidas: " + ", ".join(bad))
            return
        if not good:
            messagebox.showerror("Lista vacía", "Indicar al menos una extensión.")
            return
        con = open_db()
        try:
            save_extensions(con, good)
        finally:
            con.close()
        self.var.set(", ".join(sorted(good)))
        messagebox.showinfo("Guardado", "Se aplicará en el próximo escaneo.")

    def on_reset(self):
        self.var.set(", ".join(sorted(DEFAULT_VIDEO_EXT)))


class PathDialog(tk.Toplevel):
    """Muestra la ruta completa de una fila en un texto seleccionable."""

    def __init__(self, master, row):
        super().__init__(master)
        self.title("Ruta completa")
        self.transient(master)
        self.resizable(True, True)
        self.row = row

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        text = tk.Text(frm, wrap="word", height=5, width=70)
        text.insert("1.0", location_text(row))
        text.configure(state="disabled")
        text.pack(fill="both", expand=True)

        bf = ttk.Frame(frm)
        bf.pack(fill="x", pady=(8, 0))
        ttk.Button(bf, text="Cerrar", command=self.destroy).pack(side="right")
        ttk.Button(bf, text="Copiar ruta", command=self.copy_path).pack(
            side="right", padx=(0, 6))

        self.bind("<Escape>", lambda e: self.destroy())
        self.geometry(f"+{master.winfo_rootx() + 80}+{master.winfo_rooty() + 80}")
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window(self)

    def copy_path(self):
        self.master.clipboard_clear()
        self.master.clipboard_append(self.row.path)


class App:
    def __init__(self, master):
        self.master = master
        master.title("Videos por duración")
        master.geometry("1350x700")

        self.ffprobe = find_ffprobe()
        self.rows = []
        self.view = []
        self.connected = {}
        self.sort_key = "size"
        self.sort_desc = True
        self.thread = None
        self.stop = threading.Event()
        self.presence_thread = None
        self.presence_stop = threading.Event()
        self.presence_cache = {}
        self.q = queue.Queue()
        self.summary = ""
        self.scan_note = ""
        self.last_focus_check = 0.0
        self._after_id = None

        self.search_var = tk.StringVar()
        self.scope_var = tk.StringVar(value=SCOPES[0])
        self.notes_filter_var = tk.StringVar(value=NOTE_FILTERS[0])
        self.min_var = tk.StringVar()
        self.max_var = tk.StringVar()
        self.retry_var = tk.BooleanVar(value=False)
        self.hide_unavailable_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar()
        self.scan_stage = 0
        self.scan_job_index = 0
        self.scan_job_total = 0
        self.presence_stage = 0
        self.duplicate_hidden = []
        self.pruned_notice = ""
        self.scan_jobs = []
        self.tips = TipManager(master)

        self._build_ui()
        self.refresh_connected()
        self.load_rows()
        self.refresh()
        self.master.after(100, self.poll)
        self.master.protocol("WM_DELETE_WINDOW", self.on_close)
        self.master.bind("<FocusIn>", self.on_focus_in)

        if not self.ffprobe:
            self.status_var.set(
                "ffprobe no encontrado: colocar ffprobe.exe junto al script o en el PATH."
            )

    # Compatibilidad: los nombres anteriores siguen apuntando al TipManager.
    @property
    def tip(self):
        return self.tips.tip

    @tip.setter
    def tip(self, value):
        self.tips.tip = value

    @property
    def tip_after(self):
        return self.tips.after

    @tip_after.setter
    def tip_after(self, value):
        self.tips.after = value

    @property
    def tip_row(self):
        return self.tips.key

    @tip_row.setter
    def tip_row(self, value):
        self.tips.key = value

    # -- construccion ---------------------------------------------------------
    def _build_ui(self):
        menubar = tk.Menu(self.master)
        notes_menu = tk.Menu(menubar, tearoff=0)
        notes_menu.add_command(label="Anotar selección…", accelerator="F2",
                               command=self.annotate_selected)
        notes_menu.add_command(label="Etiquetas…", command=self.show_tags)
        notes_menu.add_separator()
        notes_menu.add_command(label="Exportar notas…", command=self.export_notes)
        notes_menu.add_command(label="Importar notas…", command=self.import_notes)
        menubar.add_cascade(label="Notas", menu=notes_menu)
        index_menu = tk.Menu(menubar, tearoff=0)
        index_menu.add_command(label="Discos…", command=self.show_disks)
        index_menu.add_command(label="Extensiones de video…", command=self.show_extensions)
        index_menu.add_separator()
        index_menu.add_command(label="Limpiar inexistentes…", command=self.clean_missing)
        menubar.add_cascade(label="Índice", menu=index_menu)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Primer uso…", command=self.show_help)
        menubar.add_cascade(label="Ayuda", menu=help_menu)
        self.master.config(menu=menubar)

        top = ttk.Frame(self.master, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        self.btn_scan = ttk.Button(top, text="Escanear carpeta/disco…", command=self.on_scan_new)
        self.btn_scan.pack(side="left")
        self.btn_disks = ttk.Button(top, text="Discos…", command=self.show_disks)
        self.btn_disks.pack(side="left", padx=(6, 0))
        self.btn_update = ttk.Button(top, text="Actualizar índice", command=self.on_update_all)
        self.btn_update.pack(side="left", padx=(6, 0))
        self.btn_verify = ttk.Button(top, text="Verificar ahora", command=self.verify_now)
        self.btn_verify.pack(side="left", padx=(6, 0))
        self.btn_cancel = ttk.Button(top, text="Cancelar", command=self.on_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(6, 0))
        self.chk_retry = ttk.Checkbutton(top, text="Reintentar fallidos",
                                         variable=self.retry_var)
        self.chk_retry.pack(side="left", padx=(12, 0))
        self.btn_help = ttk.Button(top, text="Ayuda / Primer uso…", command=self.show_help)
        self.btn_help.pack(side="right")
        self.btn_annotate = ttk.Button(top, text="Anotar… (F2)",
                                       command=self.annotate_selected)
        self.btn_annotate.pack(side="right", padx=(0, 6))

        flt = ttk.Frame(self.master, padding=(8, 2, 8, 6))
        flt.pack(fill="x")
        ttk.Label(flt, text="Buscar:").pack(side="left")
        self.ent_search = ttk.Entry(flt, textvariable=self.search_var, width=34)
        self.ent_search.pack(side="left", padx=(4, 6))
        self.cb_scope = ttk.Combobox(flt, textvariable=self.scope_var, values=SCOPES,
                                     state="readonly", width=12)
        self.cb_scope.pack(side="left", padx=(0, 10))
        ttk.Label(flt, text="Notas:").pack(side="left")
        self.cb_notes = ttk.Combobox(flt, textvariable=self.notes_filter_var,
                                     values=NOTE_FILTERS, state="readonly", width=9)
        self.cb_notes.pack(side="left", padx=(4, 10))
        ttk.Label(flt, text="Duración mín (min):").pack(side="left")
        self.ent_min = ttk.Entry(flt, textvariable=self.min_var, width=6)
        self.ent_min.pack(side="left", padx=(4, 8))
        ttk.Label(flt, text="máx (min):").pack(side="left")
        self.ent_max = ttk.Entry(flt, textvariable=self.max_var, width=6)
        self.ent_max.pack(side="left", padx=(4, 0))
        self.chk_hide = ttk.Checkbutton(flt, text="Ocultar no disponibles",
                                        variable=self.hide_unavailable_var,
                                        command=self.on_hide_unavailable)
        self.chk_hide.pack(side="left", padx=(14, 0))
        for v in (self.search_var, self.scope_var, self.notes_filter_var,
                  self.min_var, self.max_var):
            v.trace_add("write", lambda *_: self.schedule_refresh())
        for widget, tip in (
            (self.btn_scan, WIDGET_TIPS["scan"]),
            (self.btn_disks, WIDGET_TIPS["disks"]),
            (self.btn_update, WIDGET_TIPS["update"]),
            (self.btn_verify, WIDGET_TIPS["verify"]),
            (self.btn_cancel, WIDGET_TIPS["cancel"]),
            (self.chk_retry, WIDGET_TIPS["retry"]),
            (self.btn_annotate, WIDGET_TIPS["annotate"]),
            (self.btn_help, WIDGET_TIPS["help"]),
            (self.ent_search, WIDGET_TIPS["search"]),
            (self.cb_scope, WIDGET_TIPS["scope"]),
            (self.cb_notes, WIDGET_TIPS["notes_filter"]),
            (self.ent_min, WIDGET_TIPS["min_duration"]),
            (self.ent_max, WIDGET_TIPS["max_duration"]),
            (self.chk_hide, WIDGET_TIPS["hide_unavailable"]),
        ):
            self.tips.bind(widget, tip)

        mid = ttk.Frame(self.master, padding=(8, 0, 8, 0))
        mid.pack(fill="both", expand=True)

        # Correccion conocida de Tk 8.6.9: ignora los colores de fila en Treeview.
        style = ttk.Style()

        def fixed_map(option):
            return [e for e in style.map("Treeview", query_opt=option)
                    if e[:2] != ("!disabled", "!selected")]

        style.map("Treeview", foreground=fixed_map("foreground"),
                  background=fixed_map("background"))

        self.tree = ttk.Treeview(mid, columns=COLS, show="headings", selectmode="extended")
        widths = {"name": 320, "duration": 90, "size": 100, "notes": 260,
                  "disk": 150, "presence": 140, "folder": 340}
        for col in COLS:
            self.tree.heading(col, text=HEADINGS[col], command=lambda c=col: self.sort_by(c))
            # Anchos fijos: si las columnas se estiran, al maximizar la ventana
            # todo cabe y la barra horizontal queda como una franja sin pulgar.
            self.tree.column(
                col, width=widths[col], stretch=False,
                anchor="e" if col in NUMERIC_COLS else "w",
            )
        self.tree.tag_configure("offline", foreground="#8a8a8a")
        self.tree.tag_configure("missing", foreground="#b03a2e")
        self.vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set)
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")

        self.progress_frame = ttk.Frame(self.master, padding=(8, 0, 8, 2))
        self.progress = ttk.Progressbar(self.progress_frame, mode="determinate",
                                        maximum=100, value=0)
        self.progress.pack(fill="x")
        ttk.Label(self.master, textvariable=self.status_var, anchor="w", padding=(8, 4)).pack(
            fill="x"
        )

        self.menu = tk.Menu(self.master, tearoff=0)
        self.menu.add_command(label="Anotar…", command=self.annotate_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Abrir", command=self.open_selected)
        self.menu.add_command(label="Mostrar en carpeta", command=self.show_selected)
        self.menu.add_command(label="Copiar ruta", command=self.copy_selected)
        self.menu.add_command(label="Ver ruta completa…", command=self.show_path_dialog)
        self.menu.add_separator()
        self.menu.add_command(label="Enviar a la Papelera (Supr)",
                              command=lambda: self.delete_selected(False))
        self.menu.add_command(label="Eliminar definitivamente (Mayús+Supr)",
                              command=lambda: self.delete_selected(True))

        self.tree.bind("<Delete>", lambda e: self.delete_selected(False))
        self.tree.bind("<Shift-Delete>", lambda e: self.delete_selected(True))
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<Motion>", self.on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self.hide_tooltip())
        self.tree.bind("<ButtonPress>", lambda e: self.hide_tooltip())
        self.tree.bind("<MouseWheel>", lambda e: self.hide_tooltip())
        self.tree.bind("<Button-4>", lambda e: self.hide_tooltip())
        self.tree.bind("<Button-5>", lambda e: self.hide_tooltip())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.tree.bind("<Control-a>", self.select_all)
        self.tree.bind("<Control-A>", self.select_all)
        self.master.bind("<F2>", lambda e: self.annotate_selected())
        self.update_headings()

    # -- datos ----------------------------------------------------------------
    def refresh_connected(self):
        try:
            self.connected = list_connected_volumes()
        except Exception:  # noqa: BLE001  (nunca bloquear la interfaz por esto)
            self.connected = {}

    def load_rows(self):
        self.rows = fetch_rows(self.connected, self.presence_cache)

    def on_focus_in(self, event):
        """Al volver a la ventana se detectan discos conectados o retirados."""
        if event.widget is not self.master or self.is_scanning():
            return
        now = time.time()
        if now - self.last_focus_check < 10:
            return
        self.last_focus_check = now
        before = {k: v.mount for k, v in self.connected.items()}
        self.refresh_connected()
        after = {k: v.mount for k, v in self.connected.items()}
        if before != after:
            self.presence_cache.clear()
            self.load_rows()
            self.refresh()
            self._verify_if_hidden()

    def schedule_refresh(self):
        if self._after_id is not None:
            self.master.after_cancel(self._after_id)
        self._after_id = self.master.after(250, self.refresh)

    def refresh(self):
        self._after_id = None
        self.hide_tooltip()
        terms = fold(self.search_var.get()).split()
        scope = self.scope_var.get()
        nf = self.notes_filter_var.get()
        dmin = parse_minutes(self.min_var.get())
        dmax = parse_minutes(self.max_var.get())

        out = []
        for r in self.rows:
            if nf == "Con nota" and not r.note:
                continue
            if nf == "Sin nota" and r.note:
                continue
            if terms:
                if scope == "Solo notas":
                    blob = r.fnote
                elif scope == "Solo ruta":
                    blob = r.fpath
                else:
                    blob = r.fall
                if not all(t in blob for t in terms):
                    continue
            dur = r.duration
            if dmin is not None or dmax is not None:
                if dur < 0:
                    continue
                if dmin is not None and dur < dmin:
                    continue
                if dmax is not None and dur > dmax:
                    continue
            out.append(r)

        hidden = 0
        if self.hide_unavailable_var.get():
            kept = []
            for r in out:
                if presence_blocked(r.presence):
                    hidden += 1
                else:
                    kept.append(r)
            out = kept

        # Si el mismo contenido esta en dos lugares, se conserva la ubicacion
        # comprobada (Presente). La poda en base ocurre al terminar el escaneo
        # o la verificacion; aqui solo se decide qué se muestra.
        out, self.duplicate_hidden = consolidate_rows(out)

        out.sort(key=SORT_KEYS[self.sort_key], reverse=self.sort_desc)

        total_size = sum(r.size for r in out)
        total_dur = sum(r.duration for r in out if r.duration >= 0)
        annotated = sum(1 for r in out if r.note)
        missing = sum(1 for r in out if r.presence == MISSING)
        offline = sum(1 for r in out if r.presence == OFFLINE)
        shown = out[:MAX_ROWS_SHOWN]
        self.view = shown

        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(shown):
            if not r.online:
                tags = ("offline",)
            elif r.presence == MISSING:
                tags = ("missing",)
            else:
                tags = ()
            self.tree.insert(
                "", "end", iid=str(i),
                values=(r.name, fmt_dur(r.duration), fmt_size(r.size), r.note, r.disk,
                        presence_label(r.presence), r.folder),
                tags=tags,
            )

        summary = (
            f"{len(out)} videos | {annotated} con nota | {fmt_size(total_size)} | "
            f"{fmt_dur(total_dur)} de duración total"
        )
        if missing:
            summary += f" | {missing} faltantes"
        if offline:
            summary += f" | {offline} en discos desconectados"
        if hidden:
            summary += f" | {hidden} no disponibles ocultos"
        if self.duplicate_hidden:
            count = len(self.duplicate_hidden)
            noun = ("copia no disponible ocultada" if count == 1
                    else "copias no disponibles ocultadas")
            summary += f" | {count} {noun}"
        if len(out) > MAX_ROWS_SHOWN:
            summary += f" | mostrando los primeros {MAX_ROWS_SHOWN}"
        missing_fp = sum(1 for r in self.rows if r.key is None)
        if missing_fp:
            summary += (
                f" | {missing_fp} archivos sin huella: ejecutar 'Actualizar índice' "
                "para poder anotarlos"
            )
        self.summary = summary
        if not self.is_scanning() and not self.is_verifying():
            self.status_var.set(summary)

    # -- orden ----------------------------------------------------------------
    def sort_by(self, col):
        if col == self.sort_key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key = col
            self.sort_desc = col in NUMERIC_COLS
        self.update_headings()
        self.refresh()

    def update_headings(self):
        for col, label in HEADINGS.items():
            if col == self.sort_key:
                label += " ▼" if self.sort_desc else " ▲"
            self.tree.heading(col, text=label)

    # -- escaneo --------------------------------------------------------------
    def is_scanning(self):
        return self.thread is not None and self.thread.is_alive()

    def is_verifying(self):
        return self.presence_thread is not None and self.presence_thread.is_alive()

    def set_busy(self, busy):
        self.btn_scan.config(state="disabled" if busy else "normal")
        self.btn_update.config(state="disabled" if busy else "normal")
        self.btn_disks.config(state="disabled" if busy else "normal")
        self.btn_verify.config(state="disabled" if busy else "normal")
        self.btn_cancel.config(state="normal" if busy else "disabled")

    def set_verifying(self, active):
        state = "disabled" if active else "normal"
        self.btn_scan.config(state=state)
        self.btn_update.config(state=state)
        self.btn_disks.config(state=state)
        self.btn_verify.config(state=state)
        self.btn_cancel.config(state="normal" if active else "disabled")

    def scan_status(self, stage, detail=""):
        self.scan_stage = stage
        text = stage_text(SCAN_STAGES, stage, detail)
        if self.scan_job_total > 1:
            text = f"Disco {self.scan_job_index} de {self.scan_job_total} — {text}"
        self.status_var.set(text)

    def presence_status(self, stage, detail=""):
        self.presence_stage = stage
        self.status_var.set(stage_text(PRESENCE_STAGES, stage, detail))

    def show_progress(self, mode="indeterminate", value=0, maximum=100):
        if not self.progress_frame.winfo_ismapped():
            self.progress_frame.pack(fill="x")
        self.progress.stop()
        self.progress.configure(mode=mode)
        if mode == "determinate":
            self.progress.configure(maximum=max(1, maximum), value=value)
        else:
            self.progress.configure(value=0)
            self.progress.start(12)

    def hide_progress(self):
        self.progress.stop()
        self.progress_frame.pack_forget()

    def duplicate_detail(self):
        if not self.duplicate_hidden:
            return ""
        count = len(self.duplicate_hidden)
        noun = "copia no disponible ocultada" if count == 1 else "copias no disponibles ocultadas"
        names = ", ".join(r.disk for r in self.duplicate_hidden[:2])
        more = "" if count <= 2 else f" y {count - 2} más"
        return f"{count} {noun} en {names}{more}"

    def start_scan(self, jobs, offline=()):
        """Lanza el escaneo de `jobs`. `offline`: discos desconectados que se omitieron."""
        if self.is_scanning():
            return
        if not jobs:
            if offline:
                messagebox.showinfo(
                    "Discos desconectados",
                    "No hay discos conectados para escanear.\nDesconectados: "
                    + ", ".join(offline))
            else:
                messagebox.showinfo("Índice vacío", "Primero escanear una carpeta o disco.")
            return
        if not self.ffprobe:
            messagebox.showerror(
                "ffprobe no encontrado",
                "Colocar ffprobe.exe (incluido en cualquier build de ffmpeg) en la misma "
                "carpeta que este script o agregarlo al PATH.",
            )
            return
        self.scan_note = ("Desconectados (índice conservado): " + ", ".join(offline)
                          if offline else "")
        self.scan_jobs = list(jobs)
        self.pruned_notice = ""
        if self.is_verifying():
            self.presence_stop.set()
        self.stop.clear()
        self.set_busy(True)
        self.scan_stage = 1
        self.scan_job_index = 0
        self.scan_job_total = len(jobs)
        self.show_progress()
        self.scan_status(1, "iniciando")
        self.thread = threading.Thread(
            target=scan_worker,
            args=(jobs, self.ffprobe, self.q, self.stop, self.retry_var.get()),
            daemon=True,
        )
        self.thread.start()

    def on_scan_new(self):
        if self.is_scanning():
            return
        folder = filedialog.askdirectory(mustexist=True, title="Carpeta o disco a indexar")
        if not folder:
            return
        folder = os.path.normpath(folder)
        try:
            info = identify_volume(folder)
        except OSError as e:
            messagebox.showerror("No se pudo leer el disco", str(e))
            return
        rel = rel_of(folder, info.mount)
        if rel is None:
            messagebox.showerror("Ruta inválida", f"{folder} no está dentro de {info.mount}")
            return
        con = open_db()
        try:
            vid = get_or_create_volume(con, info)
            add_root(con, vid, rel)
            workers = con.execute("SELECT workers FROM volumes WHERE id = ?", (vid,)).fetchone()[0]
        finally:
            con.close()
        self.refresh_connected()
        job = Job(vid, info.ident, info.mount, rel, workers,
                  disk_text(info.label, info.mount, True))
        self.start_scan([job])

    def scan_new_volume(self, info):
        """Escanea un disco conectado que todavia no esta en el indice (disco completo)."""
        con = open_db()
        try:
            vid = get_or_create_volume(con, info)
            add_root(con, vid, "")
            workers = con.execute("SELECT workers FROM volumes WHERE id = ?", (vid,)).fetchone()[0]
        finally:
            con.close()
        job = Job(vid, info.ident, info.mount, "", workers,
                  disk_text(info.label, info.mount, True))
        self.start_scan([job])

    def scan_volume(self, vol_id):
        """Actualiza solo las carpetas indexadas de un disco."""
        con = open_db()
        try:
            states = volume_states(con, self.connected)
            jobs, offline = build_jobs(con, states, {vol_id})
        finally:
            con.close()
        self.start_scan(jobs, offline)

    def on_update_all(self):
        if self.is_scanning():
            return
        self.refresh_connected()
        con = open_db()
        try:
            states = volume_states(con, self.connected)
            jobs, offline = build_jobs(con, states)
        finally:
            con.close()
        self.start_scan(jobs, offline)

    def on_cancel(self):
        if self.is_scanning():
            self.stop.set()
            self.scan_status(self.scan_stage or 1, "cancelando…")
        if self.is_verifying():
            self.presence_stop.set()
            self.presence_status(self.presence_stage or 1, "cancelando…")

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "job":
                    self.scan_job_index, self.scan_job_total = msg[1], msg[2]
                    self.scan_status(self.scan_stage or 1, msg[3])
                elif kind == "stage":
                    self.scan_status(msg[1], msg[2])
                elif kind == "walk":
                    self.scan_status(2, f"{msg[2]} carpetas, {msg[3]} archivos, "
                                        f"{msg[4]} videos — {msg[5]}")
                elif kind == "scan":
                    self.scan_status(2, f"{msg[2]} videos encontrados en {msg[1]}")
                elif kind == "probe":
                    self.scan_status(5, f"{msg[1]} / {msg[2]}")
                    self.show_progress("determinate", msg[1], msg[2])
                elif kind == "done":
                    self.finish_scan(completed=not msg[1].startswith("Cancelado"))
                    self.hide_progress()
                    text = msg[1]
                    if self.scan_note:
                        text += " " + self.scan_note
                    dup = self.duplicate_detail()
                    if dup:
                        text += " " + dup
                    if self.pruned_notice:
                        text += " " + self.pruned_notice
                    self.scan_status(7, f"{text}  {self.summary}")
                    if msg[2]:
                        messagebox.showwarning("Avisos del escaneo", "\n\n".join(msg[2]))
                    self._verify_if_hidden()
                elif kind == "error":
                    self.finish_scan(completed=False)
                    self.hide_progress()
                    self.scan_status(7, f"Error: {msg[1]}")
                    messagebox.showerror("Error durante el escaneo", msg[1])
                elif kind == "presence":
                    if not self.is_scanning():
                        self.presence_status(1, f"{msg[1]} / {msg[2]}")
                        self.show_progress("determinate", msg[1], msg[2])
                elif kind == "presence_error":
                    self.hide_progress()
                    messagebox.showerror("Error al verificar", msg[1])
                elif kind == "presence_done":
                    self.finish_presence(msg[1], msg[2])
        except queue.Empty:
            pass
        self.master.after(100, self.poll)

    def finish_scan(self, completed=True):
        self.set_busy(False)
        self.refresh_connected()
        if completed:
            # Los archivos de la raiz recien escaneada se vieron en disco, así
            # que cuentan como Presente para decidir qué ruta vieja se descarta.
            # El resto conserva el estado verificado que hubiera antes.
            current = fetch_rows(self.connected, dict(self.presence_cache))
            scanned = set()
            for job in self.scan_jobs:
                pfx = ncase(job.root_rel) + "/" if job.root_rel else ""
                for r in current:
                    if r.vol != job.vol or not r.online:
                        continue
                    rel = ncase(r.rel)
                    if not job.root_rel or rel.startswith(pfx):
                        scanned.add((r.vol, r.rel))
            rows = [r._replace(presence=PRESENT) if (r.vol, r.rel) in scanned else r
                    for r in current]
            self._prune_superseded(rows)
        self.presence_cache.clear()
        self.load_rows()
        self.refresh()

    def _prune_superseded(self, rows):
        """Quita del índice las rutas viejas sin borrar ni cambiar las notas."""
        self.pruned_notice = ""
        hidden, replacements = superseded_locations(rows)
        if not hidden:
            return 0
        try:
            removed, _updated = drop_superseded(hidden, replacements)
        except sqlite3.Error as exc:
            messagebox.showerror("No se pudo quitar la ruta vieja", str(exc))
            return 0
        if removed:
            noun = ("ubicación anterior quitada" if removed == 1
                    else "ubicaciones anteriores quitadas")
            self.pruned_notice = f"{removed} {noun}; notas conservadas"
        return removed

    # -- verificacion de disponibilidad --------------------------------------
    def _verify_if_hidden(self):
        if self.hide_unavailable_var.get() and not self.is_scanning():
            self.start_presence()

    def on_hide_unavailable(self):
        self.refresh()
        self._verify_if_hidden()

    def verify_now(self):
        if self.is_scanning():
            messagebox.showinfo("Escaneo en curso", "Esperar a que termine el escaneo actual.")
            return
        self.start_presence(force=True)

    def start_presence(self, force=False):
        if self.is_scanning() or self.is_verifying():
            return
        self.pruned_notice = ""
        if force:
            self.presence_cache.clear()
            self.load_rows()
            self.refresh()
        rows = [r for r in self.rows if r.online]
        if not rows:
            self.refresh()
            return
        self.presence_stop.clear()
        self.set_verifying(True)
        self.presence_stage = 1
        self.presence_status(1, f"0 / {len(rows)}")
        self.show_progress("determinate", 0, len(rows))
        self.presence_thread = threading.Thread(
            target=presence_worker, args=(rows, self.presence_stop, self.q), daemon=True
        )
        self.presence_thread.start()

    def finish_presence(self, results, cancelled):
        self.presence_thread = None
        self.presence_cache.update(results)
        self.load_rows()
        self.refresh()
        if not cancelled:
            # Con el estado completo sabemos cuál copia está comprobada: esa
            # gana y la ruta vieja sale del índice. Si se canceló, no se toca.
            self._prune_superseded(self.rows)
            if self.pruned_notice:
                self.load_rows()
                self.refresh()
        scanning = self.is_scanning()
        if not scanning:
            self.hide_progress()
            self.set_verifying(False)
        missing = sum(1 for r in self.rows if r.presence == MISSING)
        offline = sum(1 for r in self.rows if r.presence == OFFLINE)
        text = ("Verificación cancelada (parcial)" if cancelled
                else "Verificación completa")
        text += f": {missing} faltantes, {offline} en discos desconectados"
        dup = self.duplicate_detail()
        if dup:
            text += " " + dup
        if self.pruned_notice:
            text += " " + self.pruned_notice
        if not scanning:
            self.presence_status(2, f"{text}.  {self.summary}")

    # -- ventanas auxiliares y mantenimiento ----------------------------------
    def show_disks(self):
        DisksDialog(self)

    def show_extensions(self):
        ExtensionsDialog(self)

    def clean_missing(self):
        """Quita del indice los archivos que ya no existen en discos conectados."""
        if self.is_scanning():
            messagebox.showinfo("Escaneo en curso", "Esperar a que termine el escaneo.")
            return
        self.refresh_connected()
        con = open_db()
        try:
            states = volume_states(con, self.connected)
            rows = con.execute("SELECT vol, rel FROM files").fetchall()
        finally:
            con.close()
        self.master.config(cursor="watch")
        self.master.update_idletasks()
        try:
            alive = {}
            gone = []
            for vol, rel in rows:
                st = states.get(vol)
                if st is None or not st.online or not st.mount:
                    continue
                if vol not in alive:
                    alive[vol] = root_alive(st.mount)
                if not alive[vol]:
                    continue
                if definitely_missing(display_path(st.mount, rel)):
                    gone.append((vol, rel))
        finally:
            self.master.config(cursor="")
        if not gone:
            messagebox.showinfo("Limpiar inexistentes",
                                "No hay entradas inexistentes en los discos conectados.")
            return
        if not messagebox.askyesno(
            "Limpiar inexistentes",
            f"{len(gone)} archivos del índice ya no existen en discos conectados.\n"
            "Se quitarán del índice (las notas se conservan).\n\n¿Continuar?",
            icon="warning", default="no",
        ):
            return
        remove_file_records(gone)
        self.load_rows()
        self.refresh()
        self.status_var.set(f"{len(gone)} entradas inexistentes quitadas.  {self.summary}")

    # -- seleccion y acciones sobre filas -------------------------------------
    def selected_rows(self):
        out = []
        for iid in sorted(self.tree.selection(), key=int):
            try:
                out.append(self.view[int(iid)])
            except (ValueError, IndexError):
                continue
        return out

    def select_all(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def on_double_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        try:
            col = COLS[int(self.tree.identify_column(event.x)[1:]) - 1]
        except (ValueError, IndexError):
            return
        if col == "notes":
            self.annotate_selected()
        else:
            self.open_selected()

    def on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _offline_notice(self, r):
        messagebox.showinfo("Disco desconectado",
                            f"Conectar el disco '{r.disk}' para usar este archivo.")

    def open_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        r = rows[0]
        if not r.online:
            self._offline_notice(r)
            return
        try:
            if NT:
                os.startfile(r.path)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", r.path])
        except OSError as e:
            messagebox.showerror("No se pudo abrir", str(e))

    def show_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        r = rows[0]
        if not r.online:
            self._offline_notice(r)
            return
        try:
            if NT:
                subprocess.Popen(f'explorer /select,"{r.path}"')
            else:
                subprocess.Popen(["xdg-open", r.folder])
        except OSError as e:
            messagebox.showerror("No se pudo abrir la carpeta", str(e))

    def copy_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        self.master.clipboard_clear()
        self.master.clipboard_append("\n".join(r.path for r in rows))

    # -- ruta completa y ayuda ------------------------------------------------
    def on_tree_motion(self, event):
        """Muestra la ruta completa al pasar sobre la columna Carpeta."""
        if self.tree.identify_region(event.x, event.y) != "cell":
            self.hide_tooltip()
            return
        try:
            col = COLS[int(self.tree.identify_column(event.x)[1:]) - 1]
        except (ValueError, IndexError):
            self.hide_tooltip()
            return
        if col != "folder":
            self.hide_tooltip()
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            self.hide_tooltip()
            return
        try:
            row = self.view[int(iid)]
        except (ValueError, IndexError):
            self.hide_tooltip()
            return
        self.tips.schedule(row, location_text(row), event.x_root, event.y_root)

    def show_tooltip(self, row, x, y):
        self.tips.show(location_text(row), x, y)

    def hide_tooltip(self):
        self.tips.hide()

    def show_path_dialog(self):
        rows = self.selected_rows()
        if not rows:
            return
        PathDialog(self.master, rows[0])

    def show_help(self):
        """Abre la guía de primer uso y de usos posteriores."""
        self.hide_tooltip()
        HelpDialog(self.master)

    # -- eliminar -------------------------------------------------------------
    def delete_selected(self, permanent):
        """Supr: a la Papelera. Mayus+Supr: definitivo. Siempre pide confirmacion."""
        if self.is_scanning():
            messagebox.showinfo("Escaneo en curso", "Esperar a que termine el escaneo para eliminar.")
            return "break"
        rows = self.selected_rows()
        if not rows:
            return "break"
        offline = [r for r in rows if not r.online]
        if offline:
            messagebox.showinfo(
                "Disco desconectado",
                f"{len(offline)} de los videos seleccionados están en discos desconectados "
                f"(por ejemplo '{offline[0].disk}'). Conectarlos o quitarlos de la selección.")
            return "break"

        n = len(rows)
        what = "1 video" if n == 1 else f"{n} videos"
        total = sum(r.size for r in rows)
        names = "\n".join(r.name for r in rows[:8])
        if n > 8:
            names += f"\n… y {n - 8} más"
        if permanent:
            title = "Eliminar definitivamente"
            msg = (f"Se eliminarán DEFINITIVAMENTE {what} ({fmt_size(total)}).\n"
                   f"No se podrán recuperar.\n\n{names}")
        else:
            title = "Enviar a la Papelera"
            msg = f"¿Enviar {what} ({fmt_size(total)}) a la Papelera?\n\n{names}"
        if not messagebox.askyesno(title, msg, icon="warning", default="no"):
            return "break"

        first_idx = int(sorted(self.tree.selection(), key=int)[0])
        done, errors = [], []
        for r in rows:
            err = delete_permanently(r.path) if permanent else send_to_trash(r.path)
            if err:
                errors.append(f"{r.name}: {err}")
            else:
                done.append((r.vol, r.rel))

        if done:
            try:
                remove_file_records(done)
            except sqlite3.Error as e:
                messagebox.showerror("No se pudo actualizar el índice", str(e))
        self.load_rows()
        self.refresh()
        if self.view:
            iid = str(min(first_idx, len(self.view) - 1))
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            self.tree.see(iid)

        verbo = "eliminados definitivamente" if permanent else "enviados a la Papelera"
        self.status_var.set(f"{len(done)} videos {verbo}.  {self.summary}")
        if errors:
            extra = f"\n… y {len(errors) - 10} más" if len(errors) > 10 else ""
            messagebox.showerror(
                "No se pudieron eliminar algunos archivos",
                f"{len(errors)} de {n} fallaron:\n\n" + "\n".join(errors[:10]) + extra,
            )
        return "break"

    # -- notas ----------------------------------------------------------------
    def annotate_selected(self):
        if self.is_scanning():
            messagebox.showinfo("Escaneo en curso", "Esperar a que termine el escaneo para anotar.")
            return
        rows = self.selected_rows()
        if not rows:
            messagebox.showinfo("Sin selección", "Seleccionar uno o más videos.")
            return
        if any(r.key is None for r in rows):
            messagebox.showinfo(
                "Falta la huella",
                "Algunos archivos provienen de un índice anterior y no tienen huella. "
                "Ejecutar 'Actualizar índice' una vez para poder anotarlos.",
            )
            return

        initial = rows[0].note if len(rows) == 1 else ""
        dlg = NoteDialog(self.master, len(rows), initial, tag_index(self.rows))
        if dlg.result is None:
            return
        mode, text = dlg.result
        if len(rows) > 1:
            if mode in ("add", "remove") and not text:
                return
            if mode == "replace" and not text:
                if not messagebox.askyesno(
                    "Confirmar",
                    f"Se borrarán las notas de {len(rows)} videos. ¿Continuar?",
                ):
                    return

        by_key = {}
        for r in rows:
            by_key.setdefault(r.key, r)
        updates = {}
        for key, r in by_key.items():
            cur = r.note
            if mode == "replace":
                new = text
            elif mode == "add":
                new = merge_tags(cur, text)
            else:
                new = remove_tags(cur, text)
            if new != cur:
                updates[key] = (new, r.path)
        if not updates:
            return

        affected = sum(1 for r in rows if r.key in updates)
        sel_paths = {r.path for r in rows}
        yfrac = self.tree.yview()[0]
        try:
            save_notes(updates)
        except sqlite3.Error as e:
            messagebox.showerror("No se pudo guardar", str(e))
            return
        self.load_rows()
        self.refresh()
        iids = [str(i) for i, r in enumerate(self.view) if r.path in sel_paths]
        if iids:
            self.tree.selection_set(iids)
        self.tree.yview_moveto(yfrac)
        self.status_var.set(f"Notas actualizadas en {affected} videos.  {self.summary}")

    def show_tags(self):
        tags = tag_index(self.rows)
        if not tags:
            messagebox.showinfo("Etiquetas", "Aún no hay notas.")
            return
        win = tk.Toplevel(self.master)
        win.title("Etiquetas")
        win.geometry("330x440")
        win.transient(self.master)
        ttk.Label(win, text="Doble clic: filtrar por la etiqueta", padding=6).pack(anchor="w")
        frm = ttk.Frame(win)
        frm.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        tv = ttk.Treeview(frm, columns=("tag", "count"), show="headings", selectmode="browse")
        tv.heading("tag", text="Etiqueta")
        tv.heading("count", text="Videos")
        tv.column("tag", width=210)
        tv.column("count", width=70, anchor="e")
        sb = ttk.Scrollbar(frm, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for i, (t, c) in enumerate(tags):
            tv.insert("", "end", iid=str(i), values=(t, c))

        def pick(event):
            if tv.identify_region(event.x, event.y) != "cell":
                return
            sel = tv.selection()
            if not sel:
                return
            self.scope_var.set("Solo notas")
            self.notes_filter_var.set("Todos")
            self.search_var.set(tags[int(sel[0])][0])

        tv.bind("<Double-1>", pick)

    def export_notes(self):
        path = filedialog.asksaveasfilename(
            title="Exportar notas",
            defaultextension=".txt",
            initialfile="notas_videos.txt",
            filetypes=[("Texto", "*.txt"), ("Todos", "*.*")],
        )
        if not path:
            return
        con = open_db()
        try:
            n = export_notes_to(con, path)
        except OSError as e:
            messagebox.showerror("No se pudo exportar", str(e))
            return
        finally:
            con.close()
        self.status_var.set(f"{n} notas exportadas a {path}")

    def import_notes(self):
        if self.is_scanning():
            messagebox.showinfo("Escaneo en curso", "Esperar a que termine el escaneo para importar.")
            return
        path = filedialog.askopenfilename(
            title="Importar notas",
            filetypes=[("Texto", "*.txt"), ("Todos", "*.*")],
        )
        if not path:
            return
        con = open_db()
        try:
            stats = import_notes_from(con, path)
        except (OSError, UnicodeDecodeError, sqlite3.Error) as e:
            messagebox.showerror("No se pudo importar", str(e))
            return
        finally:
            con.close()
        self.load_rows()
        self.refresh()
        messagebox.showinfo(
            "Importación terminada",
            f"Nuevas: {stats['nuevas']}\n"
            f"Combinadas con una nota existente: {stats['combinadas']}\n"
            f"Sin cambios: {stats['sin_cambios']}\n"
            f"Líneas inválidas: {stats['invalidas']}",
        )

    def on_close(self):
        self.stop.set()
        self.hide_tooltip()
        self.master.destroy()


def main():
    if NT:
        try:
            import ctypes
            # Evita ventanas de error del sistema ("no hay disco en la unidad").
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x8000)
        except Exception:  # noqa: BLE001
            pass
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()
