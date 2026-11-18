"""Rutas relativas al disco."""

import os

from .config import NT


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
