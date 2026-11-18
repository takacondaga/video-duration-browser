# -*- coding: utf-8 -*-
"""Tests de las rutas relativas al disco."""

import os
import tempfile
import unittest

from vdb import paths
from vdb.config import NT


class PathsTest(unittest.TestCase):
    def test_to_db_rel_y_from_db_rel_son_inversas(self):
        native = os.path.join("sub", "carpeta", "video.mp4")
        db_rel = paths.to_db_rel(native)
        self.assertNotIn("\\", db_rel)
        self.assertEqual(paths.from_db_rel(db_rel), native)

    def test_rel_of(self):
        with tempfile.TemporaryDirectory() as mount:
            inside = os.path.join(mount, "sub", "video.mp4")
            self.assertEqual(paths.rel_of(inside, mount), "sub/video.mp4")
            self.assertEqual(paths.rel_of(mount, mount), "")
            self.assertIsNone(paths.rel_of(os.path.dirname(mount), mount))

    def test_rel_of_ignora_mayusculas_solo_en_windows(self):
        with tempfile.TemporaryDirectory() as mount:
            inside = os.path.join(mount, "Sub", "video.mp4")
            upper = inside.upper()
            if NT:
                self.assertEqual(paths.rel_of(upper, mount.upper()),
                                 "SUB/VIDEO.MP4")
            else:
                self.assertIsNone(paths.rel_of(upper, mount))

    def test_display_path(self):
        with tempfile.TemporaryDirectory() as mount:
            native = paths.display_path(mount, "sub/video.mp4")
            self.assertEqual(native, os.path.join(mount, "sub", "video.mp4"))
        self.assertEqual(paths.display_path("", "sub/video.mp4"),
                         os.path.join("sub", "video.mp4"))

    def test_fix_mount_en_windows(self):
        if NT:
            self.assertEqual(paths.fix_mount("C:"), "C:\\")
            self.assertEqual(paths.fix_mount("C:\\"), "C:\\")
        else:
            self.assertEqual(paths.fix_mount("/mnt/disk"), "/mnt/disk")

    def test_disk_text(self):
        self.assertEqual(paths.disk_text("", "", False), "?")
        self.assertEqual(paths.disk_text("Disco", "", False), "Disco")
        self.assertEqual(paths.disk_text("Disco", "C:", True), "Disco (C:)")
        self.assertEqual(paths.disk_text("", "C:", True), "C:")


if __name__ == "__main__":
    unittest.main()
