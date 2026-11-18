#!/usr/bin/env python3
"""
Indexador de videos con orden por duracion, tamano y nombre, notas de texto
plano y catalogo de discos (incluidos los extraibles).

Requisitos:
  - Python 3.8+ (tkinter incluido en el instalador oficial de Windows).
  - ffprobe.exe en el PATH o en la misma carpeta que este script.

Uso:
  python "video_duration_browser 1.0.py"
  python -m vdb

El indice y las notas se guardan en videos_index.db junto a este script.
El codigo esta separado en el paquete `vdb/`:
  config.py     constantes y rutas
  text.py       utilidades de texto y extensiones
  paths.py      rutas relativas al disco
  volumes.py    identificacion de discos
  database.py   esquema, migraciones y notas
  scan.py       escaneo, huellas y duraciones
  fileops.py    eliminacion de archivos
  ui.py         interfaz Tkinter
"""

import os
import sys

# Permite encontrar el paquete vdb/ aunque se ejecute desde otra carpeta.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vdb.ui import main  # noqa: E402

if __name__ == "__main__":
    main()
