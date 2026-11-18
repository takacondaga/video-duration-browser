"""Constantes y rutas de la aplicacion."""

import os

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

# La base sigue viviendo junto al lanzador, no dentro del paquete.
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(APP_DIR, "videos_index.db")
SCHEMA_VERSION = 2
