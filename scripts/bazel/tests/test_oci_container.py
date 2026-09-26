#!/usr/bin/env python3
"""Behavioral tests for retained OCI extraction and compatible SWSS updates."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest


BAZEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BAZEL_DIR))
import oci_container


SCRIPT = BAZEL_DIR / "oci_container.py"
BINARY = "usr/bin/orchagent"
CONFIG = "etc/swss/config.d/example.json"
DOCUMENT = "usr/share/doc/swss/changelog.gz"
MD5SUMS = "var/lib/dpkg/info/swss.md5sums"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data, metadata in entries:
            member = tarfile.TarInfo(name)
            member.type = metadata.get("type", tarfile.REGTYPE)
            member.mode = metadata.get("mode", 0o644)
            member.uid = metadata.get("uid", 0)
            member.gid = metadata.get("gid", 0)
            member.mtime = metadata.get("mtime", 100)
            member.linkname = metadata.get("linkname", "")
            member.pax_headers = metadata.get("pax_headers", {})
            member.size = len(data) if data is not None else 0
            archive.addfile(member, io.BytesIO(data) if data is not None else None)
    return output.getvalue()


def ar_bytes(members):
    result = bytearray(b"!<arch>\n")
    for name, data in members:
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{0o100644:<8o}{len(data):<10}`\n"
        if len(header) != 60:
            raise ValueError("fixture ar member name is too long")
        result.extend(header.encode("ascii"))
        result.extend(data)
        if len(data) % 2:
            result.extend(b"\n")
    return bytes(result)


def write_deb(path, binary=b"baseline executable\n", document=b"baseline changelog\n",
              config=b'{"enabled":true}\n', mtime=100, depends="libc6", extra_control=None,
              binary_mode=0o755):
    files = {BINARY: binary, CONFIG: config, DOCUMENT: document}
    directories = {"."}
    for name in files:
        directories.update(str(parent) for parent in Path(name).parents)
    entries = [(name, None, {"type": tarfile.DIRTYPE, "mode": 0o755, "mtime": mtime})
               for name in sorted(directories)]
    for name, data in sorted(files.items()):
        metadata = {"mode": binary_mode if name == BINARY else 0o644, "mtime": mtime}
        if name == BINARY:
            metadata.update({"uid": 12, "gid": 34, "pax_headers": {"SCHILY.xattr.user.sonic": "fixture"}})
        entries.append((name, data, metadata))
    entries.append(("usr/bin/swss-link", None, {"type": tarfile.SYMTYPE, "mode": 0o777,
                                                "linkname": "orchagent", "mtime": mtime}))
    control = ("Package: swss\nVersion: 1.0.0\nArchitecture: amd64\n"
               "Maintainer: SONiC fixture <fixture@example.invalid>\nInstalled-Size: 1\n"
               "Depends: " + depends + "\nDescription: OCI fixture\n").encode()
    conffiles = ("/" + CONFIG + "\n").encode()
    md5sums = "".join(hashlib.md5(data).hexdigest() + "  " + name + "\n"
                      for name, data in sorted(files.items()) if name != CONFIG).encode()
    control_files = {"control": control, "conffiles": conffiles, "md5sums": md5sums}
    control_files.update(extra_control or {})
    controls = [(".", None, {"type": tarfile.DIRTYPE, "mode": 0o755, "mtime": mtime})]
    controls.extend((name, data, {"mtime": mtime}) for name, data in sorted(control_files.items()))
    data = ar_bytes([
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", gzip.compress(tar_bytes(controls), mtime=0)),
        ("data.tar.gz", gzip.compress(tar_bytes(entries), mtime=0)),
    ])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": path, "sha256": digest(data), "entries": entries, "files": files,
            "control_files": control_files}


def write_image(path, image_name, package, binary_override=None, tag=None,
                architecture="amd64", invalid_descriptor=False, extra_info=None, list_mtime=201):
    layer_entries = []
    for name, data, metadata in package["entries"]:
        if name == DOCUMENT:
            continue
        metadata = {**metadata, "mtime": 200}
        if name == BINARY and binary_override is not None:
            data = binary_override
        layer_entries.append((name, data, metadata))
    info_metadata = {"mode": 0o644, "mtime": 201}
    listed = "".join(("/." if name == "." else "/" + name) + "\n"
                     for name, _data, _metadata in package["entries"]).encode()
    status = (package["control_files"]["control"] + b"Status: install ok installed\nConffiles:\n " +
              ("/" + CONFIG + " " + hashlib.md5(package["files"][CONFIG]).hexdigest() + "\n\n").encode())
    layer_entries.extend([
        (MD5SUMS, package["control_files"]["md5sums"], info_metadata),
        ("var/lib/dpkg/info/swss.conffiles", package["control_files"]["conffiles"], info_metadata),
        ("var/lib/dpkg/info/swss.list", listed, {**info_metadata, "mtime": list_mtime}),
        ("var/lib/dpkg/status", status, info_metadata),
    ])
    for name, data in (extra_info or {}).items():
        layer_entries.append(("var/lib/dpkg/info/swss." + name, data, info_metadata))
    raw_layers = [tar_bytes(layer_entries), tar_bytes([
        ("usr/local/share/image-name", image_name.encode(), {"mtime": 202}),
    ])]
    layer_blobs = [raw_layers[0], gzip.compress(raw_layers[1], mtime=0)]
    layer_types = [OCI_LAYER, OCI_LAYER + "+gzip"]
    layers = [{"mediaType": media_type, "digest": "sha256:" + digest(data), "size": len(data)}
              for media_type, data in zip(layer_types, layer_blobs)]
    diff_ids = ["sha256:" + digest(data) for data in raw_layers]
    config = json_bytes({
        "architecture": architecture, "os": "linux",
        "config": {"Entrypoint": ["/usr/bin/supervisord"], "Cmd": ["-n"], "Env": ["SONIC=fixture"],
                   "Labels": {"com.azure.sonic.manifest": "{}", "fixture": image_name}},
        "rootfs": {"type": "layers", "diff_ids": diff_ids},
        "history": [{"created_by": "fixture layer"}, {"created_by": "fixture leaf"}],
    })
    config_descriptor = {"mediaType": "application/vnd.oci.image.config.v1+json",
                         "digest": "sha256:" + digest(config), "size": len(config)}
    if invalid_descriptor:
        config_descriptor["size"] += 1
    manifest = json_bytes({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                           "config": config_descriptor, "layers": layers})
    manifest_descriptor = {
        "mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + digest(manifest),
        "size": len(manifest), "platform": {"architecture": "amd64", "os": "linux"},
        "annotations": {"org.opencontainers.image.ref.name": "latest",
                        "io.containerd.image.name": "docker.io/library/" + image_name + ":latest"},
    }
    index = json_bytes({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                        "manifests": [manifest_descriptor]})
    saved = json_bytes([{
        "Config": "blobs/sha256/" + digest(config), "RepoTags": [tag or image_name + ":latest"],
        "Layers": ["blobs/sha256/" + digest(data) for data in layer_blobs],
        "LayerSources": dict(zip(diff_ids, layers)),
    }])
    blobs = {digest(data): data for data in [*layer_blobs, config, manifest, b'{"unreachable":true}']}
    outer = [(name, None, {"type": tarfile.DIRTYPE, "mode": 0o755}) for name in ("blobs", "blobs/sha256")]
    outer.extend(("blobs/sha256/" + name, data, {}) for name, data in sorted(blobs.items()))
    outer.extend([
        ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}', {}),
        ("index.json", index, {}), ("manifest.json", saved, {}),
    ])
    data = gzip.compress(tar_bytes(outer), mtime=0)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": path, "sha256": digest(data), "config": config, "manifest": manifest,
            "index": index, "layers": layer_blobs, "layer_types": layer_types}


class OciContainerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sonic-oci-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def command(self, *arguments, success=True):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), *map(str, arguments)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def extract(self, package, image_name, **image_options):
        image = write_image(self.root / (image_name + ".gz"), image_name, package, **image_options)
        layout = self.root / (image_name + "-layout")
        metadata = self.root / (image_name + "-metadata.json")
        self.command("extract", "--archive", image["path"], "--baseline-deb", package["path"],
                     "--archive-sha256", image["sha256"], "--baseline-sha256", package["sha256"],
                     "--image-name", image_name, "--layout", layout, "--metadata", metadata)
        return image, layout, metadata

    def overlay(self, baseline, package, seeds, suffix="", success=True):
        layer = self.root / ("overlay" + suffix + ".tar")
        metadata = self.root / ("overlay" + suffix + ".json")
        arguments = ["overlay", "--baseline-deb", baseline["path"], "--deb", package["path"],
                     "--baseline-sha256", baseline["sha256"], "--layer", layer, "--metadata", metadata]
        for seed in seeds:
            arguments.extend(["--seed-metadata", seed])
        result = self.command(*arguments, success=success)
        return result, layer, metadata

    def test_payload_update_preserves_retained_metadata_and_omitted_files(self):
        baseline = write_deb(self.root / "baseline.deb")
        updated = write_deb(self.root / "updated.deb", binary=b"updated executable with a longer body\n",
                            document=b"updated changelog\n", mtime=300)
        seeds = []
        list_mtimes = []
        for number, name in enumerate(("docker-shared", "docker-leaf")):
            image, layout, metadata = self.extract(baseline, name, list_mtime=201 + number)
            seeds.append(metadata)
            self.assertFalse((layout / "manifest.json").exists())
            self.assertEqual((layout / "index.json").read_bytes(), image["index"])
            expected_blobs = {digest(data) for data in [image["config"], image["manifest"], *image["layers"]]}
            self.assertEqual({path.name for path in (layout / "blobs/sha256").iterdir()}, expected_blobs)
            seed = json.loads(metadata.read_text())
            self.assertEqual(seed["omitted_paths"], [DOCUMENT])
            self.assertEqual(seed["payload"][BINARY]["mtime"], 200)
            list_mtimes.append(seed["package_files"]["var/lib/dpkg/info/swss.list"]["mtime"])
        self.assertEqual(list_mtimes, [201, 202])

        _result, layer, metadata = self.overlay(baseline, updated, seeds)

        with tarfile.open(layer, "r:") as archive:
            members = {member.name: member for member in archive}
            self.assertEqual(set(members), {BINARY, MD5SUMS})
            self.assertEqual(archive.extractfile(members[BINARY]).read(), updated["files"][BINARY])
            self.assertEqual(archive.extractfile(members[MD5SUMS]).read(), updated["control_files"]["md5sums"])
            member = members[BINARY]
            self.assertEqual((member.mode, member.uid, member.gid, member.mtime), (0o755, 12, 34, 200))
            self.assertEqual(member.pax_headers, {"SCHILY.xattr.user.sonic": "fixture"})
        report = json.loads(metadata.read_text())
        self.assertEqual(report["changed_paths"], [BINARY])
        self.assertEqual(report["omitted_paths"], [DOCUMENT])
        self.assertEqual(report["changed_omitted_paths"], [DOCUMENT])
        self.assertEqual(report["updated_package_paths"], [MD5SUMS])
        self.assertEqual(report["layer_sha256"], digest(layer.read_bytes()))
        self.assertTrue(report["payload_changes"][DOCUMENT]["omitted"])
        _result, repeated_layer, repeated_metadata = self.overlay(baseline, updated, seeds, "-repeat")
        self.assertEqual(repeated_layer.read_bytes(), layer.read_bytes())
        self.assertEqual(repeated_metadata.read_bytes(), metadata.read_bytes())

    def test_gzip_is_deterministic_and_has_zero_timestamp(self):
        source = self.root / "input.tar"
        source.write_bytes(tar_bytes([("file", b"repeatable data\n" * 1000, {})]))
        outputs = [self.root / "first.gz", self.root / "second.gz"]
        for output in outputs:
            self.command("gzip", "--input", source, "--output", output)
        self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
        self.assertEqual(outputs[0].read_bytes()[4:8], b"\0\0\0\0")
        self.assertEqual(gzip.decompress(outputs[0].read_bytes()), source.read_bytes())

    def test_rejects_incompatible_package_metadata_and_effects(self):
        baseline = write_deb(self.root / "baseline.deb")
        _image, _layout, seed = self.extract(baseline, "docker-test")
        cases = [
            ({"depends": "libc6, libnew"}, "control or conffiles changed"),
            ({"config": b'{"enabled":false}\n'}, "conffile bytes changed"),
            ({"binary_mode": 0o700}, "payload metadata changed"),
            ({"extra_control": {"postinst": b"#!/bin/sh\nexit 0\n"}}, "maintainer scripts"),
            ({"extra_control": {"triggers": b"interest /usr/lib\n"}}, "maintainer scripts"),
        ]
        for number, (options, message) in enumerate(cases):
            with self.subTest(options=options):
                package = write_deb(self.root / ("incompatible-" + str(number) + ".deb"), **options)
                result, layer, _metadata = self.overlay(baseline, package, [seed], str(number), success=False)
                self.assertIn(message, result.stderr)
                self.assertFalse(layer.exists())

    def test_rejects_effective_seed_drift_and_installed_package_effects(self):
        baseline = write_deb(self.root / "baseline.deb")
        for number, options in enumerate(({"binary_override": b"unrelated executable\n"},
                                          {"extra_info": {"postinst": b"#!/bin/sh\n"}})):
            with self.subTest(options=options):
                _image, _layout, seed = self.extract(baseline, "docker-drift-" + str(number), **options)
                result, layer, _metadata = self.overlay(baseline, baseline, [seed], str(number), success=False)
                self.assertIn("OCI seed", result.stderr)
                self.assertFalse(layer.exists())

    def test_rejects_archive_hash_descriptor_platform_and_tag_mismatches(self):
        baseline = write_deb(self.root / "baseline.deb")
        cases = [({}, True, "SHA-256 mismatch"), ({"invalid_descriptor": True}, False, "descriptor"),
                 ({"architecture": "arm64"}, False, "linux/amd64"),
                 ({"tag": "docker-test:wrong"}, False, "tag differs")]
        for number, (options, wrong_sha, message) in enumerate(cases):
            with self.subTest(options=options, wrong_sha=wrong_sha):
                image = write_image(self.root / ("invalid-" + str(number) + ".gz"), "docker-test", baseline, **options)
                layout = self.root / ("invalid-layout-" + str(number))
                result = self.command(
                    "extract", "--archive", image["path"], "--baseline-deb", baseline["path"],
                    "--archive-sha256", "0" * 64 if wrong_sha else image["sha256"],
                    "--baseline-sha256", baseline["sha256"], "--image-name", "docker-test",
                    "--layout", layout, "--metadata", self.root / ("invalid-" + str(number) + ".json"), success=False)
                self.assertIn(message, result.stderr)
                self.assertFalse(layout.exists())

    def test_capture_snapshots_private_read_only_files_and_reuses_receipt(self):
        root = self.root / "source"
        state = self.root / "state"
        inv = {"swss": "swss_1.0.0_amd64.deb"}
        owned = ["docker-test.gz"]
        identity = {"source": "fixture revision"}
        self.assertIsNone(oci_container.capture_retained_inputs(root, state, inv, owned, identity))
        package = write_deb(root / "target/debs/trixie" / inv["swss"])
        image = write_image(root / "target" / owned[0], "docker-test", package)

        receipt = oci_container.capture_retained_inputs(root, state, inv, owned, identity)

        self.assertEqual(receipt["identity"], identity)
        self.assertEqual(receipt["package"]["identity"], {"name": "swss", "version": "1.0.0", "architecture": "amd64"})
        self.assertEqual(receipt["archives"][owned[0]]["layer_media_types"], image["layer_types"])
        for original, record in ((package["path"], receipt["package"]),
                                 (image["path"], receipt["archives"][owned[0]])):
            retained = state / record["path"]
            self.assertEqual(retained.read_bytes(), original.read_bytes())
            self.assertNotEqual(retained.stat().st_ino, original.stat().st_ino)
            self.assertFalse(stat.S_IMODE(retained.stat().st_mode) & 0o222)
            self.assertEqual(record["sha256"], digest(retained.read_bytes()))
        shutil.rmtree(root / "target")
        repeated = oci_container.capture_retained_inputs(root, state, inv, owned, {"source": "later revision"})
        self.assertEqual(repeated, receipt)
        retained = state / receipt["archives"][owned[0]]["path"]
        retained.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "file identity changed"):
            oci_container.capture_retained_inputs(root, state, inv, owned, identity)


if __name__ == "__main__":
    unittest.main()
