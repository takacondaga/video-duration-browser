#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Empaqueta Nuevo Archivo Total con PyInstaller (modo onedir).

Uso:
    python build_exe.py

Requiere PyInstaller (``pip install pyinstaller``). El resultado queda en
``dist/Nuevo Archivo Total/``. Si ``ffprobe`` está en el PATH o junto a este
script, se copia a esa carpeta: la aplicación lo busca ahí antes que en el
PATH, así el .exe indexa sin instalar FFmpeg. La base ``videos_index.db`` se
crea junto al ejecutable.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "video_duration_browser 1.0.py"
NAME = "Nuevo Archivo Total"
DIST = HERE / "dist" / NAME
BUILD = HERE / "build"
BINARIES = ("ffprobe",)


def find_binary(name):
    """Ruta al binario local (junto al script) o al que esté en el PATH."""
    local = HERE / (name + (".exe" if os.name == "nt" else ""))
    if local.is_file():
        return local
    found = shutil.which(name)
    return Path(found) if found else None


def main():
    if not LAUNCHER.is_file():
        raise SystemExit(f"No se encontró el lanzador: {LAUNCHER}")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            "Falta PyInstaller. Instalarlo con: python -m pip install pyinstaller"
        )

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", NAME,
        "--distpath", str(DIST.parent),
        "--workpath", str(BUILD),
        "--specpath", str(HERE),
        "--add-data", f"{HERE / 'vdb'}{os.pathsep}vdb",
    ]
    for name in BINARIES:
        binary = find_binary(name)
        if binary:
            cmd += ["--add-binary", f"{binary}{os.pathsep}."]
        else:
            print(f"Aviso: no se encontró {name}; el .exe lo buscará en el PATH.")
    cmd.append(str(LAUNCHER))

    print("Ejecutando:", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, cwd=str(HERE), check=True)

    # En onedir los binarios ya deberían estar en dist, pero copiarlos de
    # nuevo garantiza que estén junto al ejecutable aunque cambie PyInstaller.
    DIST.mkdir(parents=True, exist_ok=True)
    for name in BINARIES:
        binary = find_binary(name)
        if binary:
            target = DIST / binary.name
            if target.resolve() != binary.resolve():
                shutil.copy2(binary, target)
            print(f"Incluido: {target}")

    print(f"\nListo: {DIST}")
    print("Copiar videos_index.db a esa carpeta si se quiere conservar el índice.")


if __name__ == "__main__":
    main()
