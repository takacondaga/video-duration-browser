# -*- coding: utf-8 -*-
"""Tests de los helpers puros de identificacion de discos."""

import unittest

from vdb import volumes


class MountInfoTest(unittest.TestCase):
    def test_parse_mountinfo_con_escapes(self):
        text = (
            "36 35 98:0 /mnt1 /mnt/with\\040space rw,noatime master:1 "
            "- ext4 /dev/sda1 rw,errors=continue\n"
            "linea invalida sin separador\n"
        )
        mounts = volumes.parse_mountinfo(text)
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]["mount"], "/mnt/with space")
        self.assertEqual(mounts[0]["fstype"], "ext4")
        self.assertEqual(mounts[0]["source"], "/dev/sda1")

    def test_unescape_mnt(self):
        self.assertEqual(volumes._unescape_mnt("/a\\040b\\134c"), "/a b\\c")

    def test_decode_label(self):
        self.assertEqual(volumes._decode_label("Mi\\x20disco"), "Mi disco")


class IdentFromUuidTest(unittest.TestCase):
    def test_uuid_ntfs_usa_los_32_bits_bajos(self):
        self.assertEqual(volumes._ident_from_uuid("1234567890ABCDEF", "/mnt/x"),
                         "90ABCDEF")

    def test_uuid_fat_usa_los_32_bits(self):
        self.assertEqual(volumes._ident_from_uuid("1234abcd", "/mnt/x"), "1234ABCD")

    def test_uuid_largo_se_conserva(self):
        uuid = "12345678-1234-1234-1234-123456789abc"
        self.assertEqual(volumes._ident_from_uuid(uuid, "/mnt/x"), "uuid:" + uuid)

    def test_sin_uuid_usa_el_mount(self):
        self.assertEqual(volumes._ident_from_uuid("", "/mnt/x"), "path:/mnt/x")


if __name__ == "__main__":
    unittest.main()
