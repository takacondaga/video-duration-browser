#!/usr/bin/env python3
"""
Indexador de videos con orden por duracion, tamano y nombre, notas de texto
plano y catalogo de discos (incluidos los extraibles).

Requisitos:
  - Python 3.8+ (tkinter incluido en el instalador oficial de Windows).
  - ffprobe.exe en el PATH o en la misma carpeta que este script.

Uso:
  python video_duration_browser.py

El indice y las notas se guardan en videos_index.db junto al script.

Discos:
  - Cada disco se identifica por su numero de serie de volumen (Linux: UUID),
    no por la letra de unidad. Un disco que cambia de letra sigue siendo el
    mismo disco y no se vuelve a leer.
  - Los archivos de un disco desconectado siguen apareciendo (en gris) en las
    busquedas, con su nombre de disco, para saber cual conectar.
  - Una duracion ya leida se reutiliza por huella de contenido, sin ffprobe.

Notas:
  - Cada nota queda asociada al CONTENIDO del archivo (huella: tamano + hash de
    fragmentos), no a su ruta. Si el video se mueve, se renombra, cambia la letra
    de unidad o se monta en otro sistema, la nota lo sigue.
  - Las notas nunca se borran automaticamente.
  - Dos copias identicas de un mismo video comparten la nota.
  - Exportar/Importar notas genera/lee un .txt (TSV) legible fuera del programa.
"""

import hashlib
import math
import os
import queue
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import unicodedata
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

NT = os.name == "nt"

DEFAULT_VIDEO_EXT = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".mts", ".flv", ".f4v", ".3gp", ".ogv", ".vob", ".divx",
    ".rmvb", ".asf", ".3g2", ".3gp2", ".3gpp", ".amv", ".dv", ".m1v", ".m2t",
    ".m2v", ".mp2v", ".mp4v", ".mpe", ".mxf", ".nsv", ".ogm", ".rm", ".tod",
    ".vro", ".wm", ".wtv",
}
# Extensiones que tambien usan archivos de codigo (TypeScript): solo cuentan si pesan >= 1 MB.
AMBIGUOUS_EXT = {".ts", ".mts"}
AMBIGUOUS_MIN_SIZE = 1024 * 1024
SKIP_DIRS = {"$recycle.bin", "system volume information", "node_modules", ".git"}

MAX_ROWS_SHOWN = 50000
PROBE_TIMEOUT = 60
FP_CHUNK = 64 * 1024
MTIME_TOLERANCE = 2.0          # FAT/exFAT guardan la fecha con 2 s de resolucion
PURGE_GUARD_MIN = 20           # con al menos este indice conocido...
PURGE_GUARD_RATIO = 0.5        # ...no se purga si se encuentra menos de esta fraccion
CREATE_NO_WINDOW = 0x08000000 if NT else 0

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "videos_index.db")
SCHEMA_VERSION = 2


# ----------------------------------------------------------------------------
# Utilidades de texto
# ----------------------------------------------------------------------------

def find_ffprobe():
    local = os.path.join(APP_DIR, "ffprobe.exe" if NT else "ffprobe")
    if os.path.isfile(local):
        return local
    return shutil.which("ffprobe")


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def fmt_dur(seconds):
    if seconds < 0:
        return "?"
    s = int(round(seconds))
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}"


def fmt_ts(ts):
    if not ts:
        return "nunca"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def parse_minutes(text):
    text = text.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text) * 60.0
    except ValueError:
        return None


def fold(s):
    """Minusculas y sin acentos, para comparar y buscar."""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def clean_note(s):
    """Una sola linea, espacios colapsados."""
    return " ".join(s.split())


def merge_tags(existing, new):
    cur = existing.split()
    seen = {fold(t) for t in cur}
    for t in new.split():
        k = fold(t)
        if k not in seen:
            cur.append(t)
            seen.add(k)
    return " ".join(cur)


def remove_tags(existing, remove):
    drop = {fold(t) for t in remove.split()}
    return " ".join(t for t in existing.split() if fold(t) not in drop)


def tag_index(rows):
    """Lista de (etiqueta, cantidad de videos), ordenada por cantidad."""
    idx = {}
    for r in rows:
        if not r.note:
            continue
        seen = set()
        for t in r.note.split():
            k = fold(t)
            if k in seen:
                continue
            seen.add(k)
            entry = idx.get(k)
            if entry is None:
                idx[k] = [t.lower(), 1]
            else:
                entry[1] += 1
    items = [(v[0], v[1]) for v in idx.values()]
    items.sort(key=lambda x: (-x[1], fold(x[0])))
    return items


def parse_ext_list(text):
    """Devuelve (extensiones validas, tokens invalidos)."""
    good, bad = set(), []
    for tok in re.split(r"[,\s;]+", text.strip().lower()):
        if not tok:
            continue
        t = tok.lstrip("*")
        if not t.startswith("."):
            t = "." + t
        if re.fullmatch(r"\.[a-z0-9_+\-]{1,12}", t):
            good.add(t)
        else:
            bad.append(tok)
    return good, bad


# ----------------------------------------------------------------------------
# Rutas relativas al disco
# ----------------------------------------------------------------------------

def ncase(s):
    return s.lower() if NT else s


def to_db_rel(rel):
    """En la base las rutas relativas siempre usan '/'."""
    return rel.replace(os.sep, "/") if os.sep != "/" else rel


def from_db_rel(rel):
    return rel.replace("/", os.sep) if os.sep != "/" else rel


def fix_mount(m):
    if NT and len(m) == 2 and m[1] == ":":
        return m + "\\"
    return m


def rel_of(path, mount):
    """Ruta de `path` relativa a `mount` (formato BD; '' si es el propio mount).
    None si `path` no esta dentro de `mount`."""
    p = os.path.normpath(path)
    m = os.path.normpath(mount)
    ms = m.rstrip("\\/")
    if ncase(p) == ncase(m) or ncase(p) == ncase(ms):
        return ""
    prefix = ms + os.sep
    if ncase(p).startswith(ncase(prefix)):
        return to_db_rel(p[len(prefix):])
    return None


def display_path(mount, rel):
    nat = from_db_rel(rel)
    if not mount:
        return nat
    mount = fix_mount(mount)
    return os.path.join(mount, nat) if nat else mount


def disk_text(label, mount, online):
    m = mount.rstrip("\\/") if mount else ""
    if label and online and m and NT:
        return f"{label} ({m})"
    return label or m or "?"


# ----------------------------------------------------------------------------
# Identificacion de discos
# ----------------------------------------------------------------------------

VolInfo = namedtuple("VolInfo", "ident label mount total kind")
DEFAULT_WORKERS = {"removable": 3, "fixed": 8, "remote": 3}


def _identify_windows(path):
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    buf = ctypes.create_unicode_buffer(1024)
    mount = ""
    if k32.GetVolumePathNameW(path, buf, 1024):
        mount = buf.value
    if not mount:
        mount = os.path.splitdrive(path)[0] + "\\"
    if not mount.endswith("\\"):
        mount += "\\"

    name_buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD(0)
    max_comp = wintypes.DWORD(0)
    flags = wintypes.DWORD(0)
    ok = k32.GetVolumeInformationW(
        mount, name_buf, 261, ctypes.byref(serial), ctypes.byref(max_comp),
        ctypes.byref(flags), fs_buf, 261,
    )
    if not ok:
        raise OSError(f"No se pudo leer el volumen {mount}")
    try:
        total = shutil.disk_usage(mount).total
    except OSError:
        total = 0
    dtype = k32.GetDriveTypeW(mount)
    kind = {2: "removable", 3: "fixed", 4: "remote"}.get(dtype, "unknown")
    ident = f"{serial.value:08X}" if serial.value else "path:" + mount.lower()
    return VolInfo(ident, name_buf.value or "", mount, total, kind)


def _unescape_mnt(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _decode_label(s):
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


def parse_mountinfo(text):
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        try:
            dash = parts.index("-")
        except ValueError:
            continue
        if dash < 6 or len(parts) < dash + 3:
            continue
        mounts.append({
            "mount": _unescape_mnt(parts[4]),
            "fstype": parts[dash + 1],
            "source": _unescape_mnt(parts[dash + 2]),
        })
    return mounts


def _read_mountinfo():
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _by_link_map(directory):
    """{ruta real del dispositivo: nombre del enlace} (por ejemplo /dev/disk/by-uuid)."""
    out = {}
    try:
        for name in os.listdir(directory):
            try:
                out[os.path.realpath(os.path.join(directory, name))] = name
            except OSError:
                continue
    except OSError:
        pass
    return out


def _ident_from_uuid(uuid, mount):
    if not uuid:
        return "path:" + mount
    hexonly = uuid.replace("-", "")
    # NTFS: 64 bits; Windows muestra los 32 bits bajos. FAT/exFAT: 32 bits.
    if re.fullmatch(r"[0-9A-Fa-f]{16}", hexonly):
        return hexonly[-8:].upper()
    if re.fullmatch(r"[0-9A-Fa-f]{8}", hexonly):
        return hexonly.upper()
    return "uuid:" + uuid.lower()


def _linux_kind(dev):
    name = os.path.basename(dev)
    base = os.path.join("/sys/class/block", name)
    for cand in (os.path.join(base, "removable"), os.path.join(base, "..", "removable")):
        try:
            with open(cand) as fh:
                return "removable" if fh.read().strip() == "1" else "fixed"
        except OSError:
            continue
    return "unknown"


def _identify_linux(path, mounts=None, uuids=None, labels=None):
    real = os.path.realpath(path)
    if mounts is None:
        mounts = parse_mountinfo(_read_mountinfo())
    best = None
    for m in mounts:
        mp = m["mount"]
        if real == mp or real.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) >= len(best["mount"]):
                best = m
    if best is None:
        raise OSError(f"No se pudo determinar el volumen de {path}")
    mount = best["mount"]
    source = best["source"]
    if uuids is None:
        uuids = _by_link_map("/dev/disk/by-uuid")
    if labels is None:
        labels = _by_link_map("/dev/disk/by-label")
    dev = os.path.realpath(source) if source.startswith("/") else source
    uuid = uuids.get(dev, "")
    label = _decode_label(labels.get(dev, ""))
    try:
        total = shutil.disk_usage(mount).total
    except OSError:
        total = 0
    return VolInfo(_ident_from_uuid(uuid, mount), label, mount, total, _linux_kind(dev))


def identify_volume(path):
    """Identidad estable del disco que contiene `path`. OSError si no se puede leer."""
    path = os.path.abspath(path)
    return _identify_windows(path) if NT else _identify_linux(path)


def list_connected_volumes():
    """{ident: VolInfo} de los discos fijos y extraibles conectados ahora."""
    out = {}
    if NT:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            if k32.GetDriveTypeW(root) not in (2, 3):
                continue
            try:
                info = _identify_windows(root)
            except OSError:
                continue
            out.setdefault(info.ident, info)
    else:
        mounts = parse_mountinfo(_read_mountinfo())
        uuids = _by_link_map("/dev/disk/by-uuid")
        labels = _by_link_map("/dev/disk/by-label")
        seen = set()
        for m in mounts:
            src = m["source"]
            if not src.startswith("/dev/") or src.startswith("/dev/loop"):
                continue
            if m["mount"] in seen:
                continue
            seen.add(m["mount"])
            try:
                info = _identify_linux(m["mount"], mounts, uuids, labels)
            except OSError:
                continue
            out.setdefault(info.ident, info)
    return out


# ----------------------------------------------------------------------------
# Base de datos
# ----------------------------------------------------------------------------

Row = namedtuple(
    "Row", "path name folder size duration key note fpath fnote fall vol rel disk online"
)
VolState = namedtuple("VolState", "id ident label mount online kind workers last_scan")
Job = namedtuple("Job", "vol ident mount root_rel workers label")


def _create_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS notes ("
        "key TEXT PRIMARY KEY, note TEXT NOT NULL, last_path TEXT, updated REAL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS volumes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ident TEXT UNIQUE NOT NULL, "
        "label TEXT, mount TEXT, kind TEXT, last_scan REAL, "
        "workers INTEGER NOT NULL DEFAULT 4)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS roots ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, vol INTEGER NOT NULL, "
        "rel TEXT NOT NULL, UNIQUE(vol, rel))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS files ("
        "vol INTEGER NOT NULL, rel TEXT NOT NULL, size INTEGER NOT NULL, "
        "mtime REAL NOT NULL, duration REAL NOT NULL, fp TEXT, nkey TEXT, "
        "PRIMARY KEY (vol, rel))"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_files_nkey ON files(nkey)")


def get_or_create_volume(con, info, commit=True):
    row = con.execute("SELECT id FROM volumes WHERE ident = ?", (info.ident,)).fetchone()
    if row is None:
        workers = DEFAULT_WORKERS.get(info.kind, 4)
        cur = con.execute(
            "INSERT INTO volumes (ident, label, mount, kind, last_scan, workers) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (info.ident, info.label, info.mount, info.kind, workers),
        )
        vid = cur.lastrowid
    else:
        vid = row[0]
        con.execute(
            "UPDATE volumes SET label = ?, mount = ?, kind = ? WHERE id = ?",
            (info.label, info.mount, info.kind, vid),
        )
    if commit:
        con.commit()
    return vid


def add_root(con, vol_id, rel, commit=True):
    con.execute("INSERT OR IGNORE INTO roots (vol, rel) VALUES (?, ?)", (vol_id, rel))
    if commit:
        con.commit()


def volume_states(con, connected):
    """{id_volumen: VolState}. `connected` es el resultado de list_connected_volumes()."""
    states = {}
    q = "SELECT id, ident, label, mount, kind, last_scan, workers FROM volumes"
    for vid, ident, label, mount, kind, last_scan, workers in con.execute(q).fetchall():
        mount = mount or ""
        info = connected.get(ident)
        if info is not None:
            online, cur_mount, cur_label = True, info.mount, info.label
        elif ident.startswith("legacy:"):
            online = (mount == "") or os.path.isdir(mount)
            cur_mount, cur_label = mount, label or ""
        else:
            online, cur_mount, cur_label = False, mount, label or ""
        states[vid] = VolState(vid, ident, cur_label, cur_mount, online,
                               kind or "unknown", workers or 4, last_scan)
    return states


def build_jobs(con, states, vol_ids=None):
    """Trabajos de escaneo para los discos conectados. Devuelve (jobs, desconectados)."""
    jobs, offline = [], []
    for st in states.values():
        if vol_ids is not None and st.id not in vol_ids:
            continue
        if not st.online:
            offline.append(disk_text(st.label, st.mount, False))
            continue
        if not st.mount:
            continue
        roots = sorted(
            {rel for (rel,) in con.execute("SELECT rel FROM roots WHERE vol = ?", (st.id,))},
            key=lambda r: (len(r), r),
        )
        if not roots:
            if vol_ids is None:
                continue
            roots = [""]
        kept = []
        for r in roots:  # se omiten las raices contenidas en otra ya incluida
            if any(k == "" or ncase(r) == ncase(k) or ncase(r).startswith(ncase(k) + "/")
                   for k in kept):
                continue
            kept.append(r)
        label = disk_text(st.label, st.mount, True)
        for r in kept:
            jobs.append(Job(st.id, st.ident, st.mount, r, st.workers, label))
    return jobs, offline


def _legacy_roots(con):
    row = con.execute("SELECT v FROM meta WHERE k = 'roots'").fetchone()
    if not row or not row[0]:
        return []
    return [r for r in row[0].split("\n") if r]


def _sample_matches(rows, limit=12):
    """Verifica que al menos la mitad de una muestra de archivos exista con el mismo tamano."""
    if not rows:
        return True
    step = max(1, len(rows) // limit)
    sample = rows[::step][:limit]
    ok = 0
    for r in sample:
        try:
            if os.stat(r[0]).st_size == r[1]:
                ok += 1
        except OSError:
            pass
    return ok * 2 >= len(sample)


def _migrate_v1(con, cols):
    """Pasa del esquema por ruta absoluta (v1) al esquema por disco (v2)."""
    backup = f"{DB_PATH}.bak_v1_{time.strftime('%Y%m%d_%H%M%S')}"
    con.commit()
    shutil.copy2(DB_PATH, backup)
    fp_expr = "fp" if "fp" in cols else "NULL"
    nkey_expr = "nkey" if "nkey" in cols else "NULL"
    ins = ("INSERT OR REPLACE INTO files (vol, rel, size, mtime, duration, fp, nkey) "
           "VALUES (?, ?, ?, ?, ?, ?, ?)")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DROP INDEX IF EXISTS idx_files_nkey")
        con.execute("ALTER TABLE files RENAME TO files_v1")
        _create_schema(con)
        rows = con.execute(
            f"SELECT path, size, mtime, duration, {fp_expr}, {nkey_expr} FROM files_v1"
        ).fetchall()
        remaining = list(rows)
        for root in sorted(_legacy_roots(con), key=len, reverse=True):
            under = [r for r in remaining if rel_of(r[0], root) is not None]
            remaining = [r for r in remaining if rel_of(r[0], root) is None]
            info = None
            if os.path.isdir(root):
                try:
                    info = identify_volume(root)
                except OSError:
                    info = None
                if info is not None and not _sample_matches(under):
                    info = None
            if info is None:
                info = VolInfo("legacy:" + ncase(root), root, root, 0, "unknown")
            vid = get_or_create_volume(con, info, commit=False)
            root_rel = rel_of(root, info.mount) or ""
            add_root(con, vid, root_rel, commit=False)
            batch = []
            for path, size, mtime, duration, fp, nkey in under:
                rel = rel_of(path, info.mount)
                if rel is None:
                    rel = to_db_rel(path)
                batch.append((vid, rel, size, mtime, duration, fp, nkey))
            con.executemany(ins, batch)
        if remaining:
            vid = get_or_create_volume(
                con, VolInfo("legacy:otros", "(sin disco identificado)", "", 0, "unknown"),
                commit=False,
            )
            con.executemany(ins, [
                (vid, to_db_rel(p), s, m, d, fp, nk) for p, s, m, d, fp, nk in remaining
            ])
        con.execute("DROP TABLE files_v1")
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
    except Exception:
        con.rollback()
        raise


def open_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        if ver < SCHEMA_VERSION:
            has_files = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()
            cols = ({r[1] for r in con.execute("PRAGMA table_info(files)")}
                    if has_files else set())
            if "path" in cols:
                _migrate_v1(con, cols)
            else:
                con.execute("BEGIN IMMEDIATE")
                try:
                    _create_schema(con)
                    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    con.commit()
                except Exception:
                    con.rollback()
                    raise
    except Exception:
        con.close()
        raise
    return con


def load_extensions(con):
    row = con.execute("SELECT v FROM meta WHERE k = 'extensions'").fetchone()
    if row and row[0]:
        good, _bad = parse_ext_list(row[0])
        if good:
            return good
    return set(DEFAULT_VIDEO_EXT)


def save_extensions(con, exts):
    con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('extensions', ?)",
                (",".join(sorted(exts)),))
    con.commit()


def fetch_rows(connected):
    con = open_db()
    try:
        states = volume_states(con, connected)
        data = con.execute(
            "SELECT f.vol, f.rel, f.size, f.duration, f.nkey, n.note "
            "FROM files f LEFT JOIN notes n ON n.key = f.nkey"
        ).fetchall()
    finally:
        con.close()
    rows = []
    for vol, rel, size, duration, key, note in data:
        st = states.get(vol)
        if st is None:
            continue
        path = display_path(st.mount, rel)
        folder, name = os.path.split(path)
        note = note or ""
        disk = disk_text(st.label, st.mount, st.online)
        fpath = fold(path + " " + disk)
        fnote = fold(note)
        rows.append(Row(path, name, folder, size, duration, key, note, fpath, fnote,
                        fpath + " " + fnote, vol, rel, disk, st.online))
    return rows


def save_notes(updates):
    """updates: {clave: (nota, ruta)}. Nota vacia = borrar."""
    con = open_db()
    try:
        now = time.time()
        for key, (note, path) in updates.items():
            if note:
                con.execute(
                    "INSERT OR REPLACE INTO notes (key, note, last_path, updated) "
                    "VALUES (?, ?, ?, ?)",
                    (key, note, path, now),
                )
            else:
                con.execute("DELETE FROM notes WHERE key = ?", (key,))
        con.commit()
    finally:
        con.close()


def migrate_note(con, old, new, vol, rel, path):
    """Si un archivo cambia de clave, la nota lo sigue (salvo que otra copia aun use la anterior)."""
    if con.execute(
        "SELECT 1 FROM files WHERE nkey = ? AND NOT (vol = ? AND rel = ?) LIMIT 1",
        (old, vol, rel),
    ).fetchone():
        return
    if con.execute("SELECT 1 FROM notes WHERE key = ?", (new,)).fetchone():
        return
    con.execute(
        "UPDATE notes SET key = ?, last_path = ? WHERE key = ?", (new, path, old)
    )


def remove_file_records(pairs):
    """Quita filas del indice por (vol, rel). Las notas se conservan."""
    con = open_db()
    try:
        con.executemany("DELETE FROM files WHERE vol = ? AND rel = ?", list(pairs))
        con.commit()
    finally:
        con.close()


def export_notes_to(con, filepath):
    paths = {}
    q = ("SELECT f.nkey, f.rel, v.mount FROM files f JOIN volumes v ON v.id = f.vol "
         "WHERE f.nkey IN (SELECT key FROM notes)")
    for nkey, rel, mount in con.execute(q):
        p = display_path(mount or "", rel)
        if nkey not in paths or p.lower() < paths[nkey].lower():
            paths[nkey] = p
    data = []
    for key, note, last_path in con.execute("SELECT key, note, last_path FROM notes"):
        data.append((key, note, paths.get(key) or last_path or ""))
    data.sort(key=lambda r: r[2].lower())
    with open(filepath, "w", encoding="utf-8") as fh:
        fh.write("clave\tnota\truta\n")
        for key, note, path in data:
            path = path.replace("\t", " ").replace("\r", " ").replace("\n", " ")
            fh.write(f"{key}\t{clean_note(note)}\t{path}\n")
    return len(data)


def import_notes_from(con, filepath):
    with open(filepath, "r", encoding="utf-8-sig") as fh:
        lines = fh.read().splitlines()
    stats = {"nuevas": 0, "combinadas": 0, "sin_cambios": 0, "invalidas": 0}
    now = time.time()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        parts = line.split("\t")
        if i == 0 and parts[0].strip().lower() == "clave":
            continue
        key = parts[0].strip()
        note = clean_note(parts[1]) if len(parts) > 1 else ""
        if not key or not note:
            stats["invalidas"] += 1
            continue
        last_path = parts[2].strip() if len(parts) > 2 else ""
        row = con.execute("SELECT note FROM notes WHERE key = ?", (key,)).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO notes (key, note, last_path, updated) VALUES (?, ?, ?, ?)",
                (key, note, last_path, now),
            )
            stats["nuevas"] += 1
        else:
            merged = merge_tags(row[0], note)
            if merged != row[0]:
                con.execute(
                    "UPDATE notes SET note = ?, updated = ? WHERE key = ?",
                    (merged, now, key),
                )
                stats["combinadas"] += 1
            else:
                stats["sin_cambios"] += 1
    con.commit()
    return stats


# ----------------------------------------------------------------------------
# Eliminacion de archivos
# ----------------------------------------------------------------------------

def send_to_trash(path):
    """Envia el archivo a la Papelera. Devuelve None si salio bien, o un texto de error."""
    if not os.path.exists(path):
        return None
    if NT:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", ctypes.c_void_p),
                ("pTo", ctypes.c_void_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 0x0003
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_ALLOWUNDO = 0x0040
        FOF_NOERRORUI = 0x0400
        FOF_WANTNUKEWARNING = 0x4000  # avisa si no se puede reciclar y se borraria de verdad

        full = os.path.abspath(path).replace("/", "\\")
        buf = ctypes.create_unicode_buffer(full + "\0")  # doble terminador nulo
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = ctypes.addressof(buf)
        op.pTo = None
        op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI
                     | FOF_SILENT | FOF_WANTNUKEWARNING)
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if res != 0 or op.fAnyOperationsAborted or os.path.exists(path):
            if res == 0:
                return "operacion cancelada o no completada"
            return f"error de Windows 0x{res:X}"
        return None
    for cmd in (["gio", "trash"], ["trash-put"]):
        if shutil.which(cmd[0]):
            try:
                r = subprocess.run(cmd + [path], capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as e:
                return str(e)
            if r.returncode == 0 and not os.path.exists(path):
                return None
            return (r.stderr or "no se pudo enviar a la papelera").strip()
    return "no hay herramienta de papelera (instalar gio o trash-cli)"


def delete_permanently(path):
    """Borra el archivo sin pasar por la Papelera. None si salio bien, o un texto de error."""
    try:
        os.remove(path)
        return None
    except FileNotFoundError:
        return None
    except PermissionError:
        try:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)
            return None
        except OSError as e:
            return str(e)
    except OSError as e:
        return str(e)


# ----------------------------------------------------------------------------
# Escaneo, huella y lectura de duracion
# ----------------------------------------------------------------------------

def probe_duration(ffprobe, path):
    """Duracion en segundos, o -1.0 si no se pudo leer."""
    try:
        r = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration:stream=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return -1.0
    best = -1.0
    for line in r.stdout.splitlines():
        try:
            v = float(line.strip())
        except ValueError:
            continue
        if math.isfinite(v) and v > best:
            best = v
    return best


def file_fingerprint(path, size):
    """Huella basada en contenido: tamano + hash de tres fragmentos. None si falla."""
    if size <= 0:
        return None
    try:
        h = hashlib.sha1()
        h.update(str(size).encode())
        with open(path, "rb") as f:
            if size <= FP_CHUNK * 3:
                h.update(f.read())
            else:
                for off in (0, size // 2 - FP_CHUNK // 2, size - FP_CHUNK):
                    f.seek(off)
                    h.update(f.read(FP_CHUNK))
        return f"{size}-{h.hexdigest()}"
    except OSError:
        return None


def process_file(ffprobe, path, size, need_probe, old_duration, known):
    """Devuelve (duracion, huella, reutilizada). Una duracion ya conocida para la
    misma huella se reutiliza sin ejecutar ffprobe."""
    fp = file_fingerprint(path, size)
    if not need_probe:
        return old_duration, fp, False
    if fp is not None:
        d = known.get(fp)
        if d is not None:
            return d, fp, True
    return probe_duration(ffprobe, path), fp, False


def definitely_missing(path):
    """True solo si el sistema confirma que el archivo no existe (no ante otros errores)."""
    try:
        os.stat(path)
        return False
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False


def root_alive(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _mark_failed(failed, d, mount):
    r = rel_of(d, mount)
    failed.add(ncase(r) if r is not None else "")


def under_failed(rel, failed):
    """True si algun directorio ancestro de `rel` no pudo leerse durante el recorrido."""
    if not failed:
        return False
    if "" in failed:
        return True
    cur = ""
    for part in ncase(rel).split("/")[:-1]:
        cur = part if not cur else cur + "/" + part
        if cur in failed:
            return True
    return False


def walk_videos(base, mount, exts, stop, failed):
    """Genera (ruta_relativa, tamano, mtime). Las carpetas ilegibles se anotan en `failed`."""
    stack = [base]
    while stack:
        if stop.is_set():
            return
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name.lower() not in SKIP_DIRS:
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            ext = os.path.splitext(e.name)[1].lower()
                            if ext in exts:
                                st = e.stat()
                                if ext in AMBIGUOUS_EXT and st.st_size < AMBIGUOUS_MIN_SIZE:
                                    continue
                                rel = rel_of(e.path, mount)
                                if rel is None:
                                    continue
                                yield rel, st.st_size, st.st_mtime
                    except OSError:
                        _mark_failed(failed, d, mount)
        except OSError:
            _mark_failed(failed, d, mount)


def scan_root(con, job, ffprobe, q, stop, retry_failed, exts, known, info, warnings):
    """Devuelve 'ok', 'cancel', 'missing', 'lost' o 'skipped'."""
    vol, ident, mount, root_rel, workers = (
        job.vol, job.ident, job.mount, job.root_rel, job.workers)
    base = display_path(mount, root_rel)
    try:
        os.listdir(base)
    except OSError:
        return "missing"

    if not ident.startswith("legacy:"):
        try:
            cur = identify_volume(base)
        except OSError:
            return "missing"
        if cur.ident != ident:
            warnings.append(
                f"{base}: ahora corresponde a otro disco "
                f"({disk_text(cur.label, cur.mount, True)}); se omitió."
            )
            return "skipped"
        get_or_create_volume(con, cur)  # actualiza etiqueta y punto de montaje vistos

    failed = set()
    found = {}
    for rel, size, mtime in walk_videos(base, mount, exts, stop, failed):
        found[rel] = (size, mtime)
        if len(found) % 200 == 0:
            q.put(("scan", base, len(found)))
    if stop.is_set():
        return "cancel"
    q.put(("scan", base, len(found)))

    if not root_alive(base):
        warnings.append(
            f"{base}: el disco dejó de responder durante el escaneo; no se modificó el índice."
        )
        return "lost"

    cached = {}
    pfx = ncase(root_rel) + "/" if root_rel else ""
    for rel, s, m, d, nk in con.execute(
        "SELECT rel, size, mtime, duration, nkey FROM files WHERE vol = ?", (vol,)
    ):
        n = ncase(rel)
        if not root_rel or n.startswith(pfx):
            cached[rel] = (s, m, d, nk)

    # Solo se borran filas de archivos; las notas se conservan siempre.
    stale_all = [r for r in cached if r not in found]
    stale = [r for r in stale_all if not under_failed(r, failed)]
    protected = len(stale_all) - len(stale)
    if len(cached) >= PURGE_GUARD_MIN and len(found) < PURGE_GUARD_RATIO * len(cached):
        warnings.append(
            f"{base}: se encontraron {len(found)} de {len(cached)} archivos conocidos; "
            f"no se quitó nada del índice (usar Índice > Limpiar inexistentes)."
        )
        stale = []
    elif protected:
        warnings.append(
            f"{base}: {protected} archivos están en carpetas que no se pudieron leer; "
            f"se conservaron en el índice."
        )
    if stale:
        con.executemany("DELETE FROM files WHERE vol = ? AND rel = ?",
                        [(vol, r) for r in stale])
        con.commit()

    # todo[rel] = (leer_duracion, duracion_previa, clave_previa)
    todo = {}
    for rel, (s, m) in found.items():
        c = cached.get(rel)
        if c is None:
            todo[rel] = (True, -1.0, None)
        elif c[0] != s or abs(c[1] - m) > MTIME_TOLERANCE:
            todo[rel] = (True, -1.0, c[3])
        elif c[3] is None:
            # Indice previo sin huella: solo se calcula la huella.
            todo[rel] = (False, c[2], None)
        elif retry_failed and (c[2] < 0 or c[3].startswith("path:")):
            todo[rel] = (c[2] < 0, c[2], c[3])

    total = len(todo)
    lost = False
    reused = 0
    if total:
        futs = {}
        ex = ThreadPoolExecutor(max_workers=max(1, int(workers)))
        try:
            for rel, (need_probe, old_d, _old_key) in todo.items():
                full = display_path(mount, rel)
                futs[ex.submit(process_file, ffprobe, full, found[rel][0],
                               need_probe, old_d, known)] = rel
            for i, f in enumerate(as_completed(futs), 1):
                if stop.is_set():
                    break
                rel = futs[f]
                try:
                    d, fp, was_reused = f.result()
                except Exception:  # noqa: BLE001
                    d, fp, was_reused = todo[rel][1], None, False
                full = display_path(mount, rel)
                if d < 0:
                    if not root_alive(base):
                        lost = True
                        break
                    if definitely_missing(full):
                        continue  # el archivo desaparecio durante el escaneo
                size, mtime = found[rel]
                nkey = fp if fp else f"path:{ident}|{rel}"
                con.execute(
                    "INSERT OR REPLACE INTO files (vol, rel, size, mtime, duration, fp, nkey) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (vol, rel, size, mtime, d, fp, nkey),
                )
                old_key = todo[rel][2]
                if old_key and old_key != nkey:
                    migrate_note(con, old_key, nkey, vol, rel, full)
                if fp and d >= 0:
                    known.setdefault(fp, d)
                if was_reused:
                    reused += 1
                if i % 50 == 0:
                    con.commit()
                if i % 10 == 0 or i == total:
                    q.put(("probe", i, total))
        finally:
            for f in futs:
                f.cancel()
            ex.shutdown(wait=True)
            con.commit()

    if reused:
        info.append(f"{reused} duraciones reutilizadas por huella.")
    if lost:
        warnings.append(
            f"{base}: el disco dejó de responder; los archivos pendientes se leerán "
            f"en el próximo escaneo."
        )
        return "lost"
    if stop.is_set():
        return "cancel"
    con.execute("UPDATE volumes SET last_scan = ? WHERE id = ?", (time.time(), vol))
    con.commit()
    return "ok"


def scan_worker(jobs, ffprobe, q, stop, retry_failed):
    con = None
    try:
        con = open_db()
        exts = load_extensions(con)
        known = {}
        for nk, d in con.execute(
            "SELECT nkey, duration FROM files WHERE duration >= 0 "
            "AND nkey IS NOT NULL AND nkey NOT LIKE 'path:%'"
        ):
            known.setdefault(nk, d)
        info, warnings, missing = [], [], []
        for job in jobs:
            if stop.is_set():
                break
            result = scan_root(con, job, ffprobe, q, stop, retry_failed, exts,
                               known, info, warnings)
            if result == "cancel":
                break
            if result == "missing":
                missing.append(display_path(job.mount, job.root_rel))
        msg = "Cancelado." if stop.is_set() else "Listo."
        if missing:
            msg += " No accesibles (índice conservado): " + ", ".join(missing) + "."
        if info:
            msg += " " + " ".join(info)
        q.put(("done", msg, warnings))
    except Exception as e:  # noqa: BLE001
        q.put(("error", f"{type(e).__name__}: {e}"))
    finally:
        if con is not None:
            con.close()


# ----------------------------------------------------------------------------
# Interfaz
# ----------------------------------------------------------------------------

COLS = ("name", "duration", "size", "notes", "disk", "folder")
HEADINGS = {
    "name": "Nombre",
    "duration": "Duración",
    "size": "Tamaño",
    "notes": "Notas",
    "disk": "Disco",
    "folder": "Carpeta",
}
SORT_KEYS = {
    "name": lambda r: r.name.lower(),
    "duration": lambda r: r.duration,
    "size": lambda r: r.size,
    "notes": lambda r: r.fnote,
    "disk": lambda r: r.disk.lower(),
    "folder": lambda r: r.folder.lower(),
}
NUMERIC_COLS = {"duration", "size"}
SCOPES = ("Ruta y notas", "Solo notas", "Solo ruta")
NOTE_FILTERS = ("Todos", "Con nota", "Sin nota")


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
        self.status_var = tk.StringVar()

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
        self.master.config(menu=menubar)

        top = ttk.Frame(self.master, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        self.btn_scan = ttk.Button(top, text="Escanear carpeta/disco…", command=self.on_scan_new)
        self.btn_scan.pack(side="left")
        self.btn_disks = ttk.Button(top, text="Discos…", command=self.show_disks)
        self.btn_disks.pack(side="left", padx=(6, 0))
        self.btn_update = ttk.Button(top, text="Actualizar índice", command=self.on_update_all)
        self.btn_update.pack(side="left", padx=(6, 0))
        self.btn_cancel = ttk.Button(top, text="Cancelar", command=self.on_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(6, 0))
        ttk.Checkbutton(top, text="Reintentar fallidos", variable=self.retry_var).pack(
            side="left", padx=(12, 0)
        )
        ttk.Button(top, text="Anotar… (F2)", command=self.annotate_selected).pack(side="right")

        flt = ttk.Frame(self.master, padding=(8, 2, 8, 6))
        flt.pack(fill="x")
        ttk.Label(flt, text="Buscar:").pack(side="left")
        ttk.Entry(flt, textvariable=self.search_var, width=34).pack(side="left", padx=(4, 6))
        ttk.Combobox(flt, textvariable=self.scope_var, values=SCOPES, state="readonly",
                     width=12).pack(side="left", padx=(0, 10))
        ttk.Label(flt, text="Notas:").pack(side="left")
        ttk.Combobox(flt, textvariable=self.notes_filter_var, values=NOTE_FILTERS,
                     state="readonly", width=9).pack(side="left", padx=(4, 10))
        ttk.Label(flt, text="Duración mín (min):").pack(side="left")
        ttk.Entry(flt, textvariable=self.min_var, width=6).pack(side="left", padx=(4, 8))
        ttk.Label(flt, text="máx (min):").pack(side="left")
        ttk.Entry(flt, textvariable=self.max_var, width=6).pack(side="left", padx=(4, 0))
        for v in (self.search_var, self.scope_var, self.notes_filter_var,
                  self.min_var, self.max_var):
            v.trace_add("write", lambda *_: self.schedule_refresh())

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
                  "disk": 150, "folder": 340}
        for col in COLS:
            self.tree.heading(col, text=HEADINGS[col], command=lambda c=col: self.sort_by(c))
            self.tree.column(
                col, width=widths[col], stretch=(col in ("name", "notes", "folder")),
                anchor="e" if col in NUMERIC_COLS else "w",
            )
        self.tree.tag_configure("offline", foreground="#8a8a8a")
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        ttk.Label(self.master, textvariable=self.status_var, anchor="w", padding=(8, 4)).pack(
            fill="x"
        )

        self.menu = tk.Menu(self.master, tearoff=0)
        self.menu.add_command(label="Anotar…", command=self.annotate_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Abrir", command=self.open_selected)
        self.menu.add_command(label="Mostrar en carpeta", command=self.show_selected)
        self.menu.add_command(label="Copiar ruta", command=self.copy_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Enviar a la Papelera (Supr)",
                              command=lambda: self.delete_selected(False))
        self.menu.add_command(label="Eliminar definitivamente (Mayús+Supr)",
                              command=lambda: self.delete_selected(True))

        self.tree.bind("<Delete>", lambda e: self.delete_selected(False))
        self.tree.bind("<Shift-Delete>", lambda e: self.delete_selected(True))
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self.on_right_click)
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
        self.rows = fetch_rows(self.connected)

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
            self.load_rows()
            self.refresh()

    def schedule_refresh(self):
        if self._after_id is not None:
            self.master.after_cancel(self._after_id)
        self._after_id = self.master.after(250, self.refresh)

    def refresh(self):
        self._after_id = None
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

        out.sort(key=SORT_KEYS[self.sort_key], reverse=self.sort_desc)

        total_size = sum(r.size for r in out)
        total_dur = sum(r.duration for r in out if r.duration >= 0)
        annotated = sum(1 for r in out if r.note)
        offline = sum(1 for r in out if not r.online)
        shown = out[:MAX_ROWS_SHOWN]
        self.view = shown

        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(shown):
            self.tree.insert(
                "", "end", iid=str(i),
                values=(r.name, fmt_dur(r.duration), fmt_size(r.size), r.note, r.disk, r.folder),
                tags=() if r.online else ("offline",),
            )

        summary = (
            f"{len(out)} videos | {annotated} con nota | {fmt_size(total_size)} | "
            f"{fmt_dur(total_dur)} de duración total"
        )
        if offline:
            summary += f" | {offline} en discos desconectados"
        if len(out) > MAX_ROWS_SHOWN:
            summary += f" | mostrando los primeros {MAX_ROWS_SHOWN}"
        missing_fp = sum(1 for r in self.rows if r.key is None)
        if missing_fp:
            summary += (
                f" | {missing_fp} archivos sin huella: ejecutar 'Actualizar índice' "
                "para poder anotarlos"
            )
        self.summary = summary
        if not self.is_scanning():
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

    def set_busy(self, busy):
        self.btn_scan.config(state="disabled" if busy else "normal")
        self.btn_update.config(state="disabled" if busy else "normal")
        self.btn_disks.config(state="disabled" if busy else "normal")
        self.btn_cancel.config(state="normal" if busy else "disabled")

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
        self.stop.clear()
        self.set_busy(True)
        self.status_var.set("Iniciando escaneo…")
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
        self.stop.set()
        self.status_var.set("Cancelando…")

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "scan":
                    self.status_var.set(f"Buscando videos en {msg[1]}: {msg[2]} encontrados")
                elif kind == "probe":
                    self.status_var.set(f"Leyendo duraciones y huellas: {msg[1]} / {msg[2]}")
                elif kind == "done":
                    self.finish_scan()
                    text = msg[1]
                    if self.scan_note:
                        text += " " + self.scan_note
                    self.status_var.set(f"{text}  {self.summary}")
                    if msg[2]:
                        messagebox.showwarning("Avisos del escaneo", "\n\n".join(msg[2]))
                elif kind == "error":
                    self.finish_scan()
                    messagebox.showerror("Error durante el escaneo", msg[1])
        except queue.Empty:
            pass
        self.master.after(100, self.poll)

    def finish_scan(self):
        self.set_busy(False)
        self.refresh_connected()
        self.load_rows()
        self.refresh()

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


if __name__ == "__main__":
    main()
