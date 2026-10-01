#!/usr/bin/env python3
"""Build and verify native sysmgr packages without a Make-built image base."""

import argparse
import hashlib
import json
import platform
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
TESTS = [
    "//tools/bazel/registry:registry_lib_test",
    "//tools/bazel/equivalence_checker:rules_engine_test",
    "//tools/bazel/equivalence_checker:reporter_test",
    "//tools/bazel/oci:docker_archive_to_oci_layout_test",
    "//tools/bazel/dpkg:test_dpkg_patterns_up_to_date",
]
PACKAGES = {
    "sysmgr.deb": "@sonic_sysmgr//:sysmgr_deb",
    "sysmgr-dbg.deb": "@sonic_sysmgr//:sysmgr-dbg_deb",
    "runtime-layer.tar": "//dockers/docker-sysmgr:rdeps",
    "config-layer.tar": "//dockers/docker-sysmgr:source_files",
    "debug-layer.tar": "//tools/bazel/ci/sysmgr:symbols",
}
OPTIONS = ["--jobs=4", "--local_resources=cpu=4", "--local_resources=memory=10000",
           "--lockfile_mode=off", "--noshow_progress", "--color=no", "--curses=no"]
BINARIES = ["usr/bin/rebootbackend", "usr/lib/x86_64-linux-gnu/librebootgnoi.so.0.0.0"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def output(*command):
    return subprocess.check_output(command, cwd=ROOT, text=True)


def unpack(archive, destination):
    with tarfile.open(archive) as stream:
        for member in stream.getmembers():
            require((member.uid, member.gid) == (0, 0), "Non-root archive owner: " + member.name)
        stream.extractall(destination, filter="data")


def verify_packages(paths):
    with tempfile.TemporaryDirectory(prefix="sysmgr-ci-") as temporary:
        directory = Path(temporary)
        runtime, debug, layers, symbols = [directory / name for name in ("runtime", "debug", "layers", "symbols")]
        for name, package in (("sysmgr.deb", "sysmgr"), ("sysmgr-dbg.deb", "sysmgr-dbg")):
            metadata = output("dpkg-deb", "-f", str(paths[name]), "Package", "Architecture", "Version")
            require(f"Package: {package}\n" in metadata and "Architecture: amd64\n" in metadata
                    and "Version: 1.0.0\n" in metadata, "Unexpected Debian metadata: " + metadata)
            data = directory / (package + ".tar")
            with data.open("wb") as stream:
                subprocess.run(["dpkg-deb", "--fsys-tarfile", str(paths[name])], stdout=stream, check=True)
            unpack(data, runtime if package == "sysmgr" else debug)
        unpack(paths["runtime-layer.tar"], layers)
        unpack(paths["debug-layer.tar"], symbols)
        pairs = []
        for relative in BINARIES:
            binary = runtime / relative
            require(binary.is_file() and binary.read_bytes()[:4] == b"\x7fELF", "Missing ELF: " + relative)
            require(binary.read_bytes()[4:6] == bytes((2, 1)) and struct.unpack_from("<H", binary.read_bytes(), 18)[0] == 62,
                    "Expected AMD64 ELF: " + relative)
            require(sha(binary) == sha(layers / relative), "Container layer changed: " + relative)
            notes = output("readelf", "-n", str(binary))
            build_id = re.search(r"Build ID: ([0-9a-f]+)", notes).group(1)
            debug_path = Path("usr/lib/debug/.build-id") / build_id[:2] / (build_id[2:] + ".debug")
            detached = debug / debug_path
            require(detached.is_file() and sha(detached) == sha(symbols / debug_path), "Missing matching image symbols: " + relative)
            require("Build ID: " + build_id in output("readelf", "-n", str(detached)), "Debug build ID mismatch")
            require(".debug_info" in output("readelf", "-S", "-W", str(detached)), "No detached DWARF")
            require(".debug_info" not in output("readelf", "-S", "-W", str(binary)), "Runtime ELF is unstripped")
            section = directory / "debuglink"
            subprocess.run(["objcopy", "--dump-section", f".gnu_debuglink={section}", str(binary)], check=True)
            link = section.read_bytes()
            filename = link.split(b"\0", 1)[0]
            require(filename.decode() == detached.name, "Wrong debug-link filename")
            offset = (len(filename) + 1 + 3) & ~3
            require(struct.unpack_from("<I", link, offset)[0] == zlib.crc32(detached.read_bytes()), "Debug-link checksum mismatch")
            source = "rebootbe.cpp" if relative == BINARIES[0] else "system.pb.cc"
            decoded = output("readelf", "--debug-dump=decodedline", str(detached))
            source_line = re.search(r"^\s*" + re.escape(source) + r"\s+(\d+)\s+0x", decoded, re.MULTILINE)
            require(source_line is not None, "No source line table for " + source)
            lookup = output("gdb", "-q", "-nx", "-batch",
                            "-ex", "set debug-file-directory " + str(debug / "usr/lib/debug"),
                            "-ex", "file " + str(binary),
                            "-ex", "info line " + source + ":" + source_line.group(1))
            require(re.search(r'Line \d+ of \".*' + re.escape(source) + r'\" starts at address', lookup),
                    "GDB cannot resolve deployed symbols: " + lookup)
            pairs.append({"path": relative, "gdb_source_line": lookup.strip(), "build_id": build_id, "debug_path": str(debug_path),
                          "runtime_sha256": sha(binary), "debug_sha256": sha(detached)})
        library = runtime / BINARIES[1]
        require("[librebootgnoi.so.0]" in output("readelf", "-d", str(library)), "Wrong gNOI SONAME")
        for suffix in ("", ".0"):
            link = library.parent / ("librebootgnoi.so" + suffix)
            require(link.is_symlink() and link.readlink() == Path(library.name), "Broken library symlink")
        require((runtime / BINARIES[0]).stat().st_mode & 0o777 == 0o755, "Wrong executable mode")
        require(library.stat().st_mode & 0o777 == 0o644, "Wrong library mode")
        require(len(list(debug.rglob("*.debug"))) == len(list(symbols.rglob("*.debug"))) == 2,
                "Unexpected debug symbol inventory")
        subprocess.run(["python3", str(ROOT / "dockers/docker-sysmgr/debug_symbols_test.py"),
                        str(paths["debug-layer.tar"]), str(paths["config-layer.tar"])], check=True)
        return {"runtime_debug_pairs": pairs, "architecture": "amd64", "image_layer_tests": 2}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("test", "build"))
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    directory = args.artifacts.resolve()
    require(not directory.exists() or not any(directory.iterdir()), "Artifact directory must be empty")
    directory.mkdir(parents=True, exist_ok=True)
    receipt = {"status": "running", "mode": args.mode, "commands": [],
               "scope": "AMD64/Trixie source packages and container contribution layers; no base image or installer build."}

    def run(command, name):
        started = time.monotonic()
        record = {"argv": command, "log": name + ".log"}
        receipt["commands"].append(record)
        result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        (directory / record["log"]).write_text(result.stdout)
        print(result.stdout, end="", flush=True)
        record.update(returncode=result.returncode, elapsed_seconds=time.monotonic() - started)
        require(result.returncode == 0, f"{name} failed: {result.returncode}")

    try:
        require(platform.machine() == "x86_64" and platform.freedesktop_os_release().get("VERSION_CODENAME") == "trixie",
                "Requires native AMD64 Debian Trixie")
        receipt["revision"] = output("git", "rev-parse", "HEAD").strip()
        receipt["gitlinks"] = output("git", "submodule", "status").splitlines()
        receipt["bazel_version"] = output("bazel", "--version").strip()
        require(receipt["bazel_version"] == "bazel " + (ROOT / ".bazelversion").read_text().strip(), "Wrong Bazel version")
        receipt["platform"] = platform.platform()
        receipt["os_release"] = platform.freedesktop_os_release()
        if args.mode == "test":
            for name in ("root_config_test", "submodule_config_test"):
                run(["python3", "-E", str(ROOT / "tools/bazel/registry" / (name + ".py"))], name)
            run(["bazel", "test", *OPTIONS, "--nocache_test_results", "--test_output=errors",
                 "--build_event_json_file=" + str(directory / "bep.json"), *TESTS], "test")
            summaries = {}
            for line in (directory / "bep.json").read_text().splitlines():
                event = json.loads(line)
                if "testSummary" in event.get("id", {}):
                    summaries[event["id"]["testSummary"]["label"]] = event["testSummary"]["overallStatus"]
            require(set(summaries) == set(TESTS) and set(summaries.values()) == {"PASSED"}, "Missing passing required test: " + repr(summaries))
            receipt["tests"] = summaries
        else:
            run(["bazel", "build", *OPTIONS, "--build_event_json_file=" + str(directory / "bep.json"),
                 "--profile=" + str(directory / "profile.json.gz"), *PACKAGES.values()], "build")
            paths = {}
            receipt["artifacts"] = {}
            for name, target in PACKAGES.items():
                files = output("bazel", "cquery", *OPTIONS, "--output=files", target).splitlines()
                require(len(files) == 1 and (ROOT / files[0]).is_file() and (ROOT / files[0]).stat().st_size > 0,
                        "Missing required output: " + target)
                paths[name] = directory / name
                shutil.copyfile(ROOT / files[0], paths[name])
                receipt["artifacts"][name] = {"target": target, "sha256": sha(paths[name]), "bytes": paths[name].stat().st_size}
            receipt["validation"] = verify_packages(paths)
        receipt["status"] = "passed"
    except Exception as error:
        receipt.update(status="failed", error=str(error))
        raise
    finally:
        (directory / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
