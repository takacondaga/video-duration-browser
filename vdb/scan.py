"""Escaneo de carpetas, huellas de contenido y lectura de duraciones."""

import hashlib
import math
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import (
    AMBIGUOUS_EXT, AMBIGUOUS_MIN_SIZE, APP_DIR, CREATE_NO_WINDOW, FP_CHUNK,
    MTIME_TOLERANCE, NT, PROBE_TIMEOUT, PURGE_GUARD_MIN, PURGE_GUARD_RATIO,
    SKIP_DIRS,
)
from .database import get_or_create_volume, load_extensions, migrate_note, open_db
from .paths import disk_text, display_path, ncase, rel_of
from .volumes import identify_volume


# ----------------------------------------------------------------------------
# Escaneo, huella y lectura de duracion
# ----------------------------------------------------------------------------

def find_ffprobe():
    local = os.path.join(APP_DIR, "ffprobe.exe" if NT else "ffprobe")
    if os.path.isfile(local):
        return local
    return shutil.which("ffprobe")


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


PROGRESS_SECONDS = 0.3


def walk_videos(base, mount, exts, stop, failed, progress=None):
    """Genera (ruta_relativa, tamano, mtime). Las carpetas ilegibles se anotan en `failed`.

    `progress`, si se indica, recibe ``(carpetas, archivos, videos, carpeta_actual)``
    como maximo cada PROGRESS_SECONDS y siempre al terminar cada carpeta.
    """
    stack = [base]
    dirs = files = videos = 0
    last = 0.0
    while stack:
        if stop.is_set():
            return
        d = stack.pop()
        dirs += 1
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        files += 1
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
                                videos += 1
                                yield rel, st.st_size, st.st_mtime
                    except OSError:
                        _mark_failed(failed, d, mount)
        except OSError:
            _mark_failed(failed, d, mount)
        now = time.monotonic()
        if progress is not None and (now - last >= PROGRESS_SECONDS or not stack):
            progress(dirs, files, videos, d)
            last = now


def scan_root(con, job, ffprobe, q, stop, retry_failed, exts, known, info, warnings):
    """Devuelve 'ok', 'cancel', 'missing', 'lost' o 'skipped'."""
    vol, ident, mount, root_rel, workers = (
        job.vol, job.ident, job.mount, job.root_rel, job.workers)
    base = display_path(mount, root_rel)
    q.put(("stage", 1, base))
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
    q.put(("stage", 2, base))

    def on_walk(dirs, files, videos, current):
        q.put(("walk", base, dirs, files, videos, current))

    for rel, size, mtime in walk_videos(base, mount, exts, stop, failed, on_walk):
        found[rel] = (size, mtime)
    if stop.is_set():
        return "cancel"

    if not root_alive(base):
        warnings.append(
            f"{base}: el disco dejó de responder durante el escaneo; no se modificó el índice."
        )
        return "lost"

    q.put(("stage", 3, base))
    cached = {}
    pfx = ncase(root_rel) + "/" if root_rel else ""
    for rel, s, m, d, nk in con.execute(
        "SELECT rel, size, mtime, duration, nkey FROM files WHERE vol = ?", (vol,)
    ):
        n = ncase(rel)
        if not root_rel or n.startswith(pfx):
            cached[rel] = (s, m, d, nk)

    q.put(("stage", 4, base))
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
    q.put(("stage", 5, f"{total} archivos por procesar"))
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

    q.put(("stage", 6, base))
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
    q.put(("stage", 7, base))
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
        for index, job in enumerate(jobs, 1):
            if stop.is_set():
                break
            q.put(("job", index, len(jobs), display_path(job.mount, job.root_rel)))
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
