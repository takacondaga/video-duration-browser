"""Esquema, migraciones y operaciones de la base de datos."""

import json
import os
import shutil
import sqlite3
import time
from collections import namedtuple

from .config import DB_PATH, DEFAULT_VIDEO_EXT, SCHEMA_VERSION
from .paths import disk_text, display_path, ncase, rel_of, to_db_rel
from .presence import OFFLINE, UNKNOWN
from .text import clean_note, fold, merge_tags, parse_ext_list
from .volumes import DEFAULT_WORKERS, VolInfo, identify_volume


# ----------------------------------------------------------------------------
# Base de datos
# ----------------------------------------------------------------------------

Row = namedtuple(
    "Row",
    "path name folder size duration key note fpath fnote fall vol rel disk online "
    "kind presence mount created modified",
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
        "created REAL, PRIMARY KEY (vol, rel))"
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
                    # v2 -> v3: la fecha de creacion no existia en la base vieja.
                    if has_files and "created" not in cols:
                        con.execute("ALTER TABLE files ADD COLUMN created REAL")
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


def fetch_rows(connected, presence=None):
    """Arma las filas del indice.

    `presence` es una cache opcional {(vol, rel): estado}. Los discos
    desconectados siempre quedan como OFFLINE; los conectados sin dato en la
    cache quedan como UNKNOWN hasta que se verifiquen.
    """
    con = open_db()
    try:
        states = volume_states(con, connected)
        data = con.execute(
            "SELECT f.vol, f.rel, f.size, f.duration, f.nkey, n.note, "
            "f.mtime, f.created "
            "FROM files f LEFT JOIN notes n ON n.key = f.nkey"
        ).fetchall()
    finally:
        con.close()
    presence = presence or {}
    rows = []
    for vol, rel, size, duration, key, note, mtime, created in data:
        st = states.get(vol)
        if st is None:
            continue
        path = display_path(st.mount, rel)
        folder, name = os.path.split(path)
        note = note or ""
        disk = disk_text(st.label, st.mount, st.online)
        fpath = fold(path + " " + disk)
        fnote = fold(note)
        state = OFFLINE if not st.online else presence.get((vol, rel), UNKNOWN)
        rows.append(Row(path, name, folder, size, duration, key, note, fpath, fnote,
                        fpath + " " + fnote, vol, rel, disk, st.online, st.kind, state,
                        st.mount, created, mtime))
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


def drop_superseded(hidden, replacements):
    """Quita del indice las ubicaciones superadas por otra comprobada.

    ``hidden`` son filas con ``(vol, rel)``; ``replacements`` es
    ``{nkey: ruta_ganadora}``. Las notas nunca se borran ni cambian de clave:
    solo se actualiza ``last_path`` para que no quede la ruta vieja.

    Devuelve ``(filas_borradas, notas_actualizadas)``. Si algo falla, hace
    rollback de toda la operacion.
    """
    pairs = [(r.vol, r.rel) for r in hidden]
    if not pairs and not replacements:
        return 0, 0
    con = open_db()
    removed = updated = 0
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            if pairs:
                cur = con.executemany(
                    "DELETE FROM files WHERE vol = ? AND rel = ?", pairs)
                removed = max(0, cur.rowcount)
            now = time.time()
            for key, path in replacements.items():
                cur = con.execute(
                    "UPDATE notes SET last_path = ?, updated = ? WHERE key = ?",
                    (path, now, key),
                )
                updated += max(0, cur.rowcount)
            con.commit()
        except Exception:
            con.rollback()
            raise
    finally:
        con.close()
    return removed, updated


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


def get_setting(con, key, default=None):
    """Lee un valor JSON de `meta`. Devuelve `default` si no existe o es invalido."""
    row = con.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def set_setting(con, key, value):
    """Guarda un valor JSON en `meta`."""
    con.execute(
        "INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    con.commit()


def relocate_files(changes):
    """Actualiza la base despues de renombrar o mover archivos.

    `changes` es una secuencia de ``(vol, old_rel, new_rel, key, new_path)``.
    Todo se aplica en una sola transaccion: si alguna ruta destino ya existe o
    alguna fila desaparecio, no se cambia nada. Las notas conservan su clave y
    solo se actualiza la ultima ruta conocida.
    """
    records = [tuple(c) for c in changes]
    if not records:
        return 0
    con = open_db()
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            for vol, old, new, key, path in records:
                if con.execute(
                    "SELECT 1 FROM files WHERE vol = ? AND rel = ?", (vol, new)
                ).fetchone():
                    raise ValueError(f"Ya existe la ruta {new} en el índice")
                cur = con.execute(
                    "UPDATE files SET rel = ? WHERE vol = ? AND rel = ?", (new, vol, old)
                )
                if cur.rowcount != 1:
                    raise ValueError(f"No se encontró la ruta {old} en el índice")
                if key:
                    con.execute(
                        "UPDATE notes SET last_path = ?, updated = ? WHERE key = ?",
                        (path, time.time(), key),
                    )
            con.commit()
        except Exception:
            con.rollback()
            raise
    finally:
        con.close()
    return len(records)


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
