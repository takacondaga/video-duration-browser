"""Eliminacion de archivos: Papelera y borrado definitivo."""

import os
import shutil
import stat
import subprocess

from .config import NT


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
