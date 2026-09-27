"""Check deployed symbols and ownership of sysmgr's generated image layers."""

import sys
import tarfile
import unittest


class DebugSymbolsTest(unittest.TestCase):
    def assert_root_owned(self, members):
        for member in members:
            self.assertEqual((member.uid, member.gid), (0, 0), member.name)
            self.assertIn(member.uname, ("", "root"), member.name)
            self.assertIn(member.gname, ("", "root"), member.name)

    def test_image_layer_contains_both_sysmgr_debug_elfs(self):
        with tarfile.open(DEBUG_ARCHIVE, "r:*") as archive:
            members = archive.getmembers()
            self.assert_root_owned(members)
            files = [member for member in members if member.isfile()]
            # sysmgr_pkg deploys rebootbackend and librebootgnoi.so.0.0.0.
            # A lost DebugSymbolsInfo provider used to produce an empty layer.
            self.assertEqual(len(files), 2, [member.name for member in files])
            paths = set()
            for member in files:
                path = member.name.removeprefix("./")
                self.assertRegex(
                    path,
                    r"^usr/lib/debug/\.build-id/[0-9a-f]{2}/[0-9a-f]+\.debug$",
                )
                self.assertNotIn(path, paths)
                paths.add(path)
                with archive.extractfile(member) as contents:
                    self.assertEqual(contents.read(4), b"\x7fELF", path)

    def test_configuration_layer_has_root_ownership_and_usable_modes(self):
        expected_modes = {
            "etc/rsyslog.conf": 0o644,
            "etc/supervisor/conf.d/supervisord.conf": 0o644,
            "etc/supervisor/critical_processes": 0o644,
            "var/sonic": 0o755,
            "var/sonic/config_status": 0o644,
        }
        with tarfile.open(CONFIG_ARCHIVE, "r:*") as archive:
            members = archive.getmembers()
            self.assert_root_owned(members)
            actual_modes = {
                member.name.removeprefix("./").rstrip("/"): member.mode
                for member in members
            }
            self.assertEqual(actual_modes, expected_modes)


if __name__ == "__main__":
    DEBUG_ARCHIVE = sys.argv.pop(1)
    CONFIG_ARCHIVE = sys.argv.pop(1)
    unittest.main()
