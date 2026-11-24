"""Renombrado y organizacion masiva de los videos seleccionados.

Todas las decisiones se toman antes de tocar el disco: `plan_rename` y
`plan_move` devuelven cambios con un `.error` cuando el nombre no es valido o
cuando el destino ya esta ocupado. `execute_changes` solo aplica los cambios
sin error, nunca sobrescribe archivos y avisa a la base al final, en una sola
transaccion, a traves de la funcion `relocate` que recibe.
"""

import os
import re
import time
from collections import namedtuple

from .config import NT
from .paths import ncase, rel_of, to_db_rel
from .text import fmt_dur

Change = namedtuple("Change", "row old_path new_path old_rel new_rel error")

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)
}
MAX_NAME = 180


def _clean_part(text):
    """Componente de nombre de archivo segura en Windows y Linux."""
    text = INVALID_CHARS.sub("_", str(text))
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    return text


def safe_filename(text, fallback="video"):
    """Nombre de archivo valido, con longitud acotada."""
    stem = _clean_part(text) or fallback
    if stem.split(".")[0].upper() in RESERVED:
        stem = "_" + stem
    if len(stem) > MAX_NAME:
        stem = stem[:MAX_NAME].rstrip(". ")
    return stem or fallback


def _fields(row, index):
    title, ext = os.path.splitext(row.name)
    created = getattr(row, "created", None)
    date = time.strftime("%Y-%m-%d", time.localtime(created)) if created else "sin-fecha"
    return {
        "name": row.name,
        "title": title,
        "ext": ext,
        "index": index,
        "duration": fmt_dur(row.duration).replace(":", "-"),
        "created": date,
        "folder": os.path.basename(os.path.dirname(row.path)),
    }


def render(template, row, index=1):
    """Aplica la plantilla al nombre `row.name` (sin contar la extension)."""
    try:
        text = str(template).format(**_fields(row, index))
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"Plantilla invalida: {exc}") from exc
    return safe_filename(text)


def _check_target(row, new_path, new_rel, occupied, seen):
    key = ncase(new_rel)
    if key in seen:
        return "nombre repetido en el lote"
    blocked = (occupied.get(row.vol, occupied.get("*", ()))
               if isinstance(occupied, dict) else occupied)
    if key in blocked:
        return "ya existe otro archivo con ese nombre"
    if os.path.exists(new_path) and ncase(os.path.abspath(new_path)) != ncase(
            os.path.abspath(row.path)):
        return "el archivo de destino ya existe"
    return None


def _normalized(occupied):
    """Acepta un conjunto global de rutas o un mapa ``{vol: rutas}``."""
    if isinstance(occupied, dict):
        return {vol: {ncase(x) for x in rels} for vol, rels in occupied.items()}
    return {"*": {ncase(x) for x in (occupied or ())}}


def _change(row, new_path, new_rel, occupied, seen):
    error = _check_target(row, new_path, new_rel, occupied, seen)
    if new_rel == row.rel and not error:
        error = "sin cambios"
    if not error:
        seen.add(ncase(new_rel))
    return Change(row, row.path, new_path, row.rel, new_rel, error)


def plan_rename(rows, template, occupied=(), start=1):
    """Planea renombrar cada fila dentro de su carpeta actual."""
    seen = set()
    occupied = _normalized(occupied)
    plans = []
    for i, row in enumerate(rows, start):
        try:
            name = render(template, row, i) + os.path.splitext(row.name)[1]
        except ValueError as exc:
            plans.append(Change(row, row.path, row.path, row.rel, row.rel, str(exc)))
            continue
        parent = row.rel.rsplit("/", 1)[0] if "/" in row.rel else ""
        new_rel = f"{parent}/{name}" if parent else name
        new_path = os.path.join(os.path.dirname(row.path), name)
        plans.append(_change(row, new_path, new_rel, occupied, seen))
    return plans


def plan_move(rows, destination, subdir_template="", occupied=(), start=1):
    """Planea mover filas a `destination`, con una subcarpeta opcional.

    Solo se pueden mover videos dentro del mismo disco: la base identifica los
    archivos por volumen y ruta relativa, y cambiar de volumen no es una simple
    actualizacion de la ruta.
    """
    seen = set()
    occupied = _normalized(occupied)
    plans = []
    for i, row in enumerate(rows, start):
        try:
            sub = render(subdir_template, row, i) if subdir_template else ""
        except ValueError as exc:
            plans.append(Change(row, row.path, row.path, row.rel, row.rel, str(exc)))
            continue
        target_dir = os.path.join(destination, sub) if sub else destination
        new_path = os.path.join(target_dir, row.name)
        rel = rel_of(new_path, row.mount)
        if rel is None:
            plans.append(Change(row, row.path, new_path, row.rel, row.rel,
                                "el destino esta en otro disco"))
            continue
        new_rel = to_db_rel(rel)
        plans.append(_change(row, new_path, new_rel, occupied, seen))
    return plans


def execute_changes(changes, relocate=None):
    """Aplica los cambios sin errores. Devuelve (aplicados, fallidos).

    `relocate`, si se indica, recibe la lista de
    ``(vol, old_rel, new_rel, key, new_path)`` de los cambios aplicados para que
    la base actualice las rutas en una sola transaccion.
    """
    done, failed = [], []
    records = []
    for ch in changes:
        if ch.error:
            failed.append(ch)
            continue
        try:
            parent = os.path.dirname(ch.new_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            os.rename(ch.old_path, ch.new_path)
        except OSError as exc:
            failed.append(ch._replace(error=str(exc)))
            continue
        done.append(ch)
        records.append((ch.row.vol, ch.old_rel, ch.new_rel, ch.row.key, ch.new_path))
    if records and relocate is not None:
        try:
            relocate(records)
        except Exception:
            # La base no acepto el cambio: devuelve los archivos a su sitio para
            # no dejar el indice describiendo ubicaciones que ya no existen.
            for ch in reversed(done):
                try:
                    os.rename(ch.new_path, ch.old_path)
                except OSError:
                    pass
            raise
    return done, failed
