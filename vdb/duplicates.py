"""Deteccion de videos duplicados usando la huella ya calculada en el escaneo."""

from collections import namedtuple

DuplicateGroup = namedtuple("DuplicateGroup", "key rows size duration")


def duplicate_groups(rows, min_copies=2):
    """Agrupa filas con la misma huella de contenido.

    Solo se agrupan huellas reales: las claves ``path:`` son marcadores de
    archivos a los que no se les pudo calcular la huella y no prueban que dos
    videos sean iguales. Los grupos internos se ordenan por nombre y ruta, y
    la lista de grupos por cantidad de copias, tamaño y nombre.
    """
    groups = {}
    for row in rows:
        key = getattr(row, "key", None)
        if not key or key.startswith("path:"):
            continue
        groups.setdefault(key, []).append(row)
    out = []
    for key, items in groups.items():
        if len(items) < max(2, int(min_copies)):
            continue
        items = sorted(items, key=lambda r: (r.name.lower(), r.path.lower()))
        out.append(DuplicateGroup(key, items, items[0].size, items[0].duration))
    out.sort(key=lambda g: (-len(g.rows), -g.size, g.rows[0].name.lower()))
    return out


def duplicate_stats(groups):
    """Devuelve (grupos, copias_excedentes, espacio_recuperable)."""
    extra = sum(len(g.rows) - 1 for g in groups)
    wasted = sum(g.size * (len(g.rows) - 1) for g in groups)
    return len(groups), extra, wasted
