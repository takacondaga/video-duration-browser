"""Filtros guardados: normalizacion y operaciones sobre el diccionario."""

FILTER_KEYS = ("search", "scope", "notes", "min", "max", "hide_unavailable")
DEFAULT_FILTER = {
    "search": "", "scope": "Ruta y notas", "notes": "Todos",
    "min": "", "max": "", "hide_unavailable": False,
}
SETTING_NAME = "saved_filters"


def normalize(data):
    """Copia `data` con solo las claves conocidas y valores del tipo correcto."""
    out = dict(DEFAULT_FILTER)
    if not isinstance(data, dict):
        return out
    for key in FILTER_KEYS:
        if key not in data:
            continue
        value = data[key]
        if key == "hide_unavailable":
            out[key] = bool(value)
        else:
            out[key] = "" if value is None else str(value)
    return out


def clean_saved(saved):
    """Normaliza un diccionario completo de filtros guardados al leerlo de la base."""
    if not isinstance(saved, dict):
        return {}
    return {str(k): normalize(v) for k, v in saved.items()}


def names(saved):
    """Nombres de filtros guardados, ordenados sin distinguir mayusculas."""
    if not isinstance(saved, dict):
        return []
    return sorted((str(k) for k in saved), key=lambda s: s.lower())


def save(saved, name, data):
    """Devuelve un diccionario nuevo con el filtro agregado o reemplazado."""
    name = str(name).strip()
    if not name:
        raise ValueError("El nombre del filtro no puede estar vacío")
    out = {str(k): normalize(v) for k, v in (saved or {}).items()}
    out[name] = normalize(data)
    return out


def remove(saved, name):
    """Devuelve un diccionario nuevo sin el filtro `name`."""
    out = {str(k): v for k, v in (saved or {}).items() if str(k) != str(name)}
    return out
