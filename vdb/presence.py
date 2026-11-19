"""Verificacion no destructiva de la existencia de los archivos indexados."""

import os

PRESENT = "presente"
MISSING = "falta"
OFFLINE = "offline"
UNKNOWN = "desconocido"

PRESENCE_ORDER = {PRESENT: 0, MISSING: 1, OFFLINE: 2, UNKNOWN: 3}
PRESENCE_LABELS = {
    PRESENT: "Presente",
    MISSING: "Falta",
    OFFLINE: "Disco desconectado",
    UNKNOWN: "Sin verificar",
}


def presence_label(state):
    return PRESENCE_LABELS.get(state, PRESENCE_LABELS[UNKNOWN])


def presence_blocked(state):
    """True si el estado significa que el video no esta disponible ahora."""
    return state in (MISSING, OFFLINE)


def superseded_locations(rows):
    """Ubicaciones viejas de un mismo contenido, con su reemplazo.

    Agrupa por huella de contenido (``Row.key``), ignorando las claves ``path:``
    que no son huellas reales. Si hay **una sola** fila PRESENTE, esa gana y las
    demas del grupo (Falta / Disco desconectado / Sin verificar) quedan
    superadas. Si hay varias PRESENTES son copias legitimas: no se descarta
    nada. Si no hay ninguna PRESENTE, tampoco.

    Devuelve ``(ocultas, reemplazos)``. ``reemplazos`` es ``{nkey: ruta_ganadora}``
    y solo incluye grupos con algo superado, para no escribir la base de forma
    innecesaria. Esta funcion es la unica fuente de verdad: la vista oculta
    ``ocultas`` y la base borra esas mismas filas y apunta la nota a la ruta
    ganadora. No modifica nada.
    """
    groups = {}
    for row in rows:
        key = row.key
        if key and not key.startswith("path:"):
            groups.setdefault(key, []).append(row)
    hidden = []
    replacements = {}
    for key, group in groups.items():
        present = [r for r in group if r.presence == PRESENT]
        if len(present) != 1:
            continue
        winner = present[0]
        others = [r for r in group if r is not winner and r.presence != PRESENT]
        if not others:
            continue
        hidden.extend(others)
        # Las filas reales siempre tienen `path`; el getattr mantiene la
        # funcion usable con filas minimas en pruebas.
        replacements[key] = getattr(winner, "path", "")
    return hidden, replacements


def consolidate_rows(rows):
    """Oculta las copias superadas usando ``superseded_locations``.

    Devuelve ``(visibles, ocultas)`` sin modificar la base ni las notas.
    """
    hidden, _replacements = superseded_locations(rows)
    if not hidden:
        return list(rows), []
    hidden_ids = {id(r) for r in hidden}
    return [r for r in rows if id(r) not in hidden_ids], hidden


def check_path(path):
    """Estado de una ruta: PRESENT, MISSING o UNKNOWN.

    Solo se informa MISSING cuando el sistema confirma que la ruta no existe.
    Los errores de permisos, red o rutas invalidas quedan como UNKNOWN para no
    confundirlos con un archivo borrado.
    """
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return MISSING
    except (OSError, ValueError):
        return UNKNOWN
    return PRESENT


def presence_worker(rows, stop, q, batch=200):
    """Verifica las filas recibidas y publica el resultado en la cola.

    Las filas de discos desconectados se marcan OFFLINE sin hacer E/S. Las
    unidades de red quedan como UNKNOWN para evitar bloqueos del hilo. Si el
    punto de montaje desaparece durante la verificacion, las filas restantes de
    ese volumen tambien quedan como OFFLINE.
    Publica ``('presence', hechos, total)`` durante el trabajo y
    ``('presence_done', resultados, cancelado)`` al terminar.
    """
    results = {}
    total = len(rows)
    cancelled = False
    gone_vols = set()
    try:
        for i, row in enumerate(rows, 1):
            if stop.is_set():
                cancelled = True
                break
            if not row.online:
                state = OFFLINE
            elif row.vol in gone_vols:
                state = OFFLINE
            elif row.kind == "remote":
                state = UNKNOWN
            else:
                state = check_path(row.path)
                if (state == MISSING and row.mount
                        and not os.path.isdir(row.mount)):
                    # El disco se desconecto despues de iniciar la verificacion:
                    # lo que falta no es el archivo sino el volumen completo.
                    gone_vols.add(row.vol)
                    state = OFFLINE
            results[(row.vol, row.rel)] = state
            if i % batch == 0 or i == total:
                q.put(("presence", i, total))
        if stop.is_set():
            cancelled = True
    except Exception as e:  # noqa: BLE001  (nunca dejar la interfaz bloqueada)
        q.put(("presence_error", f"{type(e).__name__}: {e}"))
    q.put(("presence_done", results, cancelled))
    return results, cancelled
