#!/usr/bin/env python3

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


HOOK_ROOT = Path(__file__).resolve().parents[1]
SOURCE = (HOOK_ROOT / "scripts" / "buildinfo_base.sh").read_text()
START = SOURCE.index("download_packages()\n")
END = SOURCE.index("\nrun_pip_command()\n", START)
DOWNLOAD_FUNCTION = SOURCE[START:END]
PROXY_URL = "https://packages.example.test/public/package.deb"
CACHED_URL = "https://source.example.test/cached.deb"
CACHE_HASH = "0123456789abcdef0123456789abcdef"


class DownloadPackagesTest(unittest.TestCase):
    def run_download(self, arguments, *, cache_hit=False, command_exit=0):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        cache = root / "cache" / "web"
        cache.mkdir(parents=True)
        versions = root / "versions-web"
        versions.write_text(f"{CACHED_URL}=={CACHE_HASH}\n" if cache_hit else "")
        (root / "build-versions-web").touch()
        if cache_hit:
            (cache / f"cached.deb-{CACHE_HASH}.tgz").write_bytes(b"cached package\n")

        command = root / "real-command"
        command.write_text(
            "#!/bin/bash\n"
            'printf "%s\\n" "$@" >> "$TEST_CALLS"\n'
            'if [ "$TEST_COMMAND_EXIT" -ne 0 ]; then exit "$TEST_COMMAND_EXIT"; fi\n'
            "while [ $# -gt 0 ]; do\n"
            '    if [ "$1" = -O ] || [ "$1" = -o ]; then\n'
            '        printf "downloaded package\\n" > "$2"\n'
            "        shift\n"
            "    fi\n"
            "    shift\n"
            "done\n"
        )
        command.chmod(0o755)
        settings = {
            "PKG_CACHE_PATH": str(root / "cache"),
            "WEB_VERSION_FILE": str(versions),
            "BUILD_WEB_VERSION_FILE": str(root / "build-versions-web"),
            "URL_PREFIX": "https://packages.example.test/",
            "BUILD_PACKAGES_URL": "https://packages.example.test/packages",
            "ENABLE_VERSION_CONTROL_WEB": "y",
            "GET_RETRY_COUNT": "1",
            "REAL_COMMAND": str(command),
        }
        script = root / "run.sh"
        script.write_text(
            "\n".join(f"{name}={shlex.quote(value)}" for name, value in settings.items())
            + "\nget_version_cache_option() { printf rcache; }\n"
            + "get_url_version() { printf unexpected > unexpected-version-lookup; printf 11111111111111111111111111111111; }\n"
            + "check_if_url_exist() { printf n; }\n"
            + "log_err() { :; }\nlog_info() { :; }\nFLOCK() { :; }\nFUNLOCK() { :; }\n"
            + DOWNLOAD_FUNCTION
            + '\ndownload_packages "$@"\n'
        )
        environment = os.environ.copy()
        environment.update(TEST_CALLS=str(root / "calls"), TEST_COMMAND_EXIT=str(command_exit))
        result = subprocess.run(
            ["bash", str(script), *arguments],
            cwd=root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return root, result

    def test_proxy_download_runs_real_command_with_output_option(self):
        for option in ("-O", "-o"):
            with self.subTest(option=option):
                arguments = [option, "package.deb", PROXY_URL]
                root, result = self.run_download(arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / "calls").read_text().splitlines(), arguments)
                self.assertEqual((root / "package.deb").read_bytes(), b"downloaded package\n")
                self.assertFalse((root / "unexpected-version-lookup").exists())

    def test_proxy_download_propagates_real_command_failure(self):
        root, result = self.run_download(["-O", "package.deb", PROXY_URL], command_exit=6)
        self.assertEqual(result.returncode, 6)
        self.assertTrue((root / "calls").exists())
        self.assertFalse((root / "package.deb").exists())

    def test_cache_hit_skips_real_command(self):
        root, result = self.run_download(["-O", "package.deb", CACHED_URL], cache_hit=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((root / "package.deb").read_bytes(), b"cached package\n")
        self.assertFalse((root / "calls").exists())
        self.assertFalse((root / "unexpected-version-lookup").exists())

    def test_proxy_download_with_cache_hit_preserves_command_arguments(self):
        for arguments in ([CACHED_URL, PROXY_URL], [PROXY_URL, CACHED_URL]):
            with self.subTest(arguments=arguments):
                root, result = self.run_download(arguments, cache_hit=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((root / "calls").read_text().splitlines(), arguments)
                self.assertFalse((root / "unexpected-version-lookup").exists())


if __name__ == "__main__":
    unittest.main()
