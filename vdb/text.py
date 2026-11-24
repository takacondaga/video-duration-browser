"""Utilidades de texto, formato y extensiones."""

import re
import time
import unicodedata


# ----------------------------------------------------------------------------
# Utilidades de texto
# ----------------------------------------------------------------------------

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


def fmt_date(ts):
    """Fecha de archivo; `—` cuando el sistema no informa la creacion."""
    return "—" if not ts else fmt_ts(ts)


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
