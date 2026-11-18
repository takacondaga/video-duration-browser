"""Identificacion de discos por serial de volumen o UUID."""

import os
import re
import shutil
from collections import namedtuple

from .config import NT


# ----------------------------------------------------------------------------
# Identificacion de discos
# ----------------------------------------------------------------------------

VolInfo = namedtuple("VolInfo", "ident label mount total kind")
DEFAULT_WORKERS = {"removable": 3, "fixed": 8, "remote": 3}


def _identify_windows(path):
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    buf = ctypes.create_unicode_buffer(1024)
    mount = ""
    if k32.GetVolumePathNameW(path, buf, 1024):
        mount = buf.value
    if not mount:
        mount = os.path.splitdrive(path)[0] + "\\"
    if not mount.endswith("\\"):
        mount += "\\"

    name_buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD(0)
    max_comp = wintypes.DWORD(0)
    flags = wintypes.DWORD(0)
    ok = k32.GetVolumeInformationW(
        mount, name_buf, 261, ctypes.byref(serial), ctypes.byref(max_comp),
        ctypes.byref(flags), fs_buf, 261,
    )
    if not ok:
        raise OSError(f"No se pudo leer el volumen {mount}")
    try:
        total = shutil.disk_usage(mount).total
    except OSError:
        total = 0
    dtype = k32.GetDriveTypeW(mount)
    kind = {2: "removable", 3: "fixed", 4: "remote"}.get(dtype, "unknown")
    ident = f"{serial.value:08X}" if serial.value else "path:" + mount.lower()
    return VolInfo(ident, name_buf.value or "", mount, total, kind)


def _unescape_mnt(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _decode_label(s):
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


def parse_mountinfo(text):
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        try:
            dash = parts.index("-")
        except ValueError:
            continue
        if dash < 6 or len(parts) < dash + 3:
            continue
        mounts.append({
            "mount": _unescape_mnt(parts[4]),
            "fstype": parts[dash + 1],
            "source": _unescape_mnt(parts[dash + 2]),
        })
    return mounts


def _read_mountinfo():
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _by_link_map(directory):
    """{ruta real del dispositivo: nombre del enlace} (por ejemplo /dev/disk/by-uuid)."""
    out = {}
    try:
        for name in os.listdir(directory):
            try:
                out[os.path.realpath(os.path.join(directory, name))] = name
            except OSError:
                continue
    except OSError:
        pass
    return out


def _ident_from_uuid(uuid, mount):
    if not uuid:
        return "path:" + mount
    hexonly = uuid.replace("-", "")
    # NTFS: 64 bits; Windows muestra los 32 bits bajos. FAT/exFAT: 32 bits.
    if re.fullmatch(r"[0-9A-Fa-f]{16}", hexonly):
        return hexonly[-8:].upper()
    if re.fullmatch(r"[0-9A-Fa-f]{8}", hexonly):
        return hexonly.upper()
    return "uuid:" + uuid.lower()


def _linux_kind(dev):
    name = os.path.basename(dev)
    base = os.path.join("/sys/class/block", name)
    for cand in (os.path.join(base, "removable"), os.path.join(base, "..", "removable")):
        try:
            with open(cand) as fh:
                return "removable" if fh.read().strip() == "1" else "fixed"
        except OSError:
            continue
    return "unknown"


def _identify_linux(path, mounts=None, uuids=None, labels=None):
    real = os.path.realpath(path)
    if mounts is None:
        mounts = parse_mountinfo(_read_mountinfo())
    best = None
    for m in mounts:
        mp = m["mount"]
        if real == mp or real.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) >= len(best["mount"]):
                best = m
    if best is None:
        raise OSError(f"No se pudo determinar el volumen de {path}")
    mount = best["mount"]
    source = best["source"]
    if uuids is None:
        uuids = _by_link_map("/dev/disk/by-uuid")
    if labels is None:
        labels = _by_link_map("/dev/disk/by-label")
    dev = os.path.realpath(source) if source.startswith("/") else source
    uuid = uuids.get(dev, "")
    label = _decode_label(labels.get(dev, ""))
    try:
        total = shutil.disk_usage(mount).total
    except OSError:
        total = 0
    return VolInfo(_ident_from_uuid(uuid, mount), label, mount, total, _linux_kind(dev))


def identify_volume(path):
    """Identidad estable del disco que contiene `path`. OSError si no se puede leer."""
    path = os.path.abspath(path)
    return _identify_windows(path) if NT else _identify_linux(path)


def list_connected_volumes():
    """{ident: VolInfo} de los discos fijos y extraibles conectados ahora."""
    out = {}
    if NT:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            if k32.GetDriveTypeW(root) not in (2, 3):
                continue
            try:
                info = _identify_windows(root)
            except OSError:
                continue
            out.setdefault(info.ident, info)
    else:
        mounts = parse_mountinfo(_read_mountinfo())
        uuids = _by_link_map("/dev/disk/by-uuid")
        labels = _by_link_map("/dev/disk/by-label")
        seen = set()
        for m in mounts:
            src = m["source"]
            if not src.startswith("/dev/") or src.startswith("/dev/loop"):
                continue
            if m["mount"] in seen:
                continue
            seen.add(m["mount"])
            try:
                info = _identify_linux(m["mount"], mounts, uuids, labels)
            except OSError:
                continue
            out.setdefault(info.ident, info)
    return out
