#!/usr/bin/env python3
"""Behavioral tests for retained OCI extraction and compatible SWSS updates."""

import copy
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


def contract_fixture():
    manifest = {
        "schema": 1, "digest": "d" * 64, "environment_digest": "e" * 64,
        "source": {
            "entries": {
                "dockers/docker-test/Dockerfile.j2": {"kind": "file", "sha256": "a" * 64},
                "scripts/bazel/driver.py": {"kind": "file", "sha256": "b" * 64},
                "scripts/bazel/tests/test_bazel_graph.py": {"kind": "file", "sha256": "c" * 64},
                "bazel/README.md": {"kind": "file", "sha256": "d" * 64},
            },
            "repositories": [
                {"path": ".", "commit": "1" * 40, "branch": "bazel", "role": "root"},
                {"path": "src/dependency", "commit": "2" * 40, "branch": "HEAD", "role": "submodule"},
            ],
        },
        "native_make_variables": {"BUILD_TIMESTAMP": "old", "SONIC_IMAGE_VERSION": "old",
                                  "SOURCE_DATE_EPOCH": "1", "ENABLE_ASAN": "n"},
        "environment": {"slave_image_id": "sha256:" + "3" * 64, "packages": "old"},
        "native_environment": {"PATH": "/usr/bin"},
        "dependency_artifacts": {"target/debs/trixie/libc6.deb": "4" * 64},
        "private_environment_sha256": "5" * 64,
    }
    specs = {"docker-test.gz": {
        "schema": 1, "stage": "container", "target": "target/docker-test.gz",
        "outputs": {"containers/docker-test.gz": "target/docker-test.gz"},
        "environment": {"SONIC_BAZEL_SOURCE_COMMIT": "1" * 40, "SONIC_BAZEL_SOURCE_BRANCH": "bazel",
                        "SOURCE_DATE_EPOCH": "1"},
        "make_variables": {**manifest["native_make_variables"], "BAZEL_IMAGE": "sonic-vs.bin"},
        "assume_old": [], "context_mounts": {"dockers/docker-test/debs": "target/debs/trixie"},
    }}
    static_inputs = {"target/debs/trixie/libc6.deb": "4" * 64}
    dynamic_inputs = ["target/debs/trixie/swss-dbg_1.0.0_amd64.deb", "target/debs/trixie/swss_1.0.0_amd64.deb"]
    return [manifest, specs, static_inputs, dynamic_inputs, {"docker-test.gz": []}]


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

    def static_fixture(self, suffix=""):
        native = self.root / ("native" + suffix)
        state = self.root / ("static-state" + suffix)
        payloads = {
            "target/sonic-vs.bin__vs__rfs.squashfs": (b"native rootfs fixture\n", 0o640),
            "target/files/tool": (b"#!/bin/sh\nexit 0\n", 0o755),
        }
        for logical, (data, mode) in payloads.items():
            source = native / logical
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(data)
            source.chmod(mode)
        artifacts = {logical: {"sha256": digest(data), "size": len(data)}
                     for logical, (data, _mode) in payloads.items()}
        return native, state, payloads, artifacts

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

    def test_extract_accepts_precreated_empty_layout_directory(self):
        baseline = write_deb(self.root / "baseline.deb")
        layout = self.root / "docker-test-layout"
        layout.mkdir()
        image, output, metadata = self.extract(baseline, "docker-test")
        self.assertEqual(output, layout)
        self.assertEqual((layout / "index.json").read_bytes(), image["index"])
        self.assertEqual(json.loads(metadata.read_text())["image_name"], "docker-test")

    def test_extract_rejects_invalid_layout_destinations(self):
        baseline = write_deb(self.root / "baseline.deb")
        image = write_image(self.root / "docker-test.gz", "docker-test", baseline)
        regular = self.root / "file-layout"
        regular.write_bytes(b"keep\n")
        nonempty = self.root / "nonempty-layout"
        nonempty.mkdir()
        (nonempty / "keep").write_bytes(b"keep\n")
        target = self.root / "symlink-target"
        target.mkdir()
        symlink = self.root / "symlink-layout"
        symlink.symlink_to(target, target_is_directory=True)
        dangling = self.root / "dangling-layout"
        dangling.symlink_to(self.root / "missing", target_is_directory=True)
        for layout in (regular, nonempty, symlink, dangling):
            with self.subTest(layout=layout.name):
                metadata = self.root / (layout.name + ".json")
                result = self.command(
                    "extract", "--archive", image["path"], "--baseline-deb", baseline["path"],
                    "--archive-sha256", image["sha256"], "--baseline-sha256", baseline["sha256"],
                    "--image-name", "docker-test", "--layout", layout, "--metadata", metadata, success=False)
                self.assertIn("OCI layout output must be absent or an empty non-symlink directory", result.stderr)
                self.assertFalse(metadata.exists())
        self.assertEqual(regular.read_bytes(), b"keep\n")
        self.assertEqual((nonempty / "keep").read_bytes(), b"keep\n")
        self.assertEqual(symlink.readlink(), target)
        self.assertEqual(dangling.readlink(), self.root / "missing")
        self.assertEqual(list(target.iterdir()), [])

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

    def test_container_contract_allows_only_declared_metadata_changes(self):
        inputs = contract_fixture()
        original = copy.deepcopy(inputs)
        baseline = oci_container.container_input_contract(*inputs)
        self.assertEqual(inputs, original)
        changed = copy.deepcopy(inputs)
        changed[0]["digest"] = "6" * 64
        changed[0]["environment_digest"] = "7" * 64
        changed[0]["source"]["entries"]["scripts/bazel/driver.py"]["sha256"] = "8" * 64
        changed[0]["source"]["entries"]["bazel/oci_defs.bzl"] = {"kind": "file", "sha256": "9" * 64}
        changed[0]["source"]["entries"]["scripts/bazel/oci_container.py"] = {"kind": "file", "sha256": "a" * 64}
        changed[0]["source"]["entries"]["scripts/bazel/tests/new_test.py"] = {"kind": "file", "sha256": "b" * 64}
        changed[0]["source"]["entries"].pop("bazel/README.md")
        changed[0]["source"]["repositories"][0]["commit"] = "3" * 40
        for variables in (changed[0]["native_make_variables"], changed[1]["docker-test.gz"]["make_variables"]):
            variables.update({"BUILD_TIMESTAMP": "new", "SONIC_IMAGE_VERSION": "new", "SOURCE_DATE_EPOCH": "2"})
        changed[1]["docker-test.gz"]["environment"].update({"SONIC_BAZEL_SOURCE_COMMIT": "3" * 40, "SOURCE_DATE_EPOCH": "2"})
        self.assertEqual(oci_container.container_input_contract(*changed), baseline)
        mutations = [
            lambda value: value[0]["source"]["entries"]["dockers/docker-test/Dockerfile.j2"].update({"sha256": "0" * 64}),
            lambda value: value[0]["source"]["repositories"][0].update({"branch": "other"}),
            lambda value: value[0]["source"]["repositories"][1].update({"commit": "4" * 40}),
            lambda value: value[0]["environment"].update({"packages": "new"}),
            lambda value: value[0].update({"private_environment_sha256": "0" * 64}),
            lambda value: value[0].update({"future_native_input": "preserved"}),
            lambda value: value[1]["docker-test.gz"]["make_variables"].update({"ENABLE_ASAN": "y"}),
            lambda value: value[2].update({"target/debs/trixie/libc6.deb": "0" * 64}),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(preserved_input=number):
                current = copy.deepcopy(changed)
                mutate(current)
                self.assertNotEqual(oci_container.container_input_contract(*current), baseline)

    def test_contract_action_requires_canonical_equal_inputs_and_baseline_hash(self):
        contract = oci_container.container_input_contract(*contract_fixture())
        data = oci_container.contract_bytes(contract)
        baseline, current = self.root / "baseline.json", self.root / "current.json"
        baseline.write_bytes(data)
        current.write_bytes(data)
        stamp = self.root / "contract.stamp"
        self.command("contract", "--baseline", baseline, "--current", current,
                     "--baseline-sha256", digest(data), "--stamp", stamp)
        self.assertEqual(json.loads(stamp.read_bytes()), {"schema": 1, "contract_sha256": digest(data)})
        current.write_bytes(oci_container.contract_bytes({**contract, "future_native_input": "changed"}))
        result = self.command("contract", "--baseline", baseline, "--current", current,
                              "--baseline-sha256", digest(data), "--stamp", self.root / "mismatch.stamp", success=False)
        self.assertIn("current container inputs differ", result.stderr)
        current.write_bytes(data)
        result = self.command("contract", "--baseline", baseline, "--current", current,
                              "--baseline-sha256", "0" * 64, "--stamp", self.root / "hash.stamp", success=False)
        self.assertIn("SHA-256 mismatch", result.stderr)
        baseline.write_text(json.dumps(contract, indent=2) + "\n")
        result = self.command("contract", "--baseline", baseline, "--current", current,
                              "--baseline-sha256", digest(baseline.read_bytes()), "--stamp", self.root / "encoding.stamp", success=False)
        self.assertIn("not canonical", result.stderr)

    def test_freeze_static_inputs_preserves_bytes_and_original_modes(self):
        native, state, payloads, artifacts = self.static_fixture()

        receipt = oci_container.freeze_container_static_inputs(state, native, artifacts)

        identity_inputs = {logical: {**artifacts[logical], "mode": mode}
                           for logical, (_data, mode) in payloads.items()}
        identity = digest(oci_container.canonical_json(identity_inputs))
        relative = "container-static-inputs/" + identity
        self.assertEqual(set(receipt), {"schema", "kind", "identity_sha256", "receipt_path", "inputs", "receipt_sha256"})
        self.assertEqual((receipt["schema"], receipt["kind"], receipt["identity_sha256"]),
                         (1, "sonic-container-static-inputs", identity))
        self.assertEqual(receipt["receipt_path"], relative + "/receipt.json")
        persisted = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        receipt_data = oci_container.canonical_json(persisted) + b"\n"
        self.assertEqual((state / receipt["receipt_path"]).read_bytes(), receipt_data)
        self.assertEqual(receipt["receipt_sha256"], digest(receipt_data))
        self.assertEqual(stat.S_IMODE((state / receipt["receipt_path"]).stat().st_mode), 0o444)
        for directory in (state / relative, state / relative / "files"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o555)
        for logical, (data, mode) in payloads.items():
            record = receipt["inputs"][logical]
            frozen = state / record["path"]
            self.assertEqual(record, {**artifacts[logical], "mode": mode,
                                      "path": relative + "/files/" + digest(logical.encode())})
            self.assertEqual(frozen.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(frozen.stat().st_mode), 0o444)
            self.assertNotEqual((frozen.stat().st_dev, frozen.stat().st_ino),
                                ((native / logical).stat().st_dev, (native / logical).stat().st_ino))
        self.assertEqual(oci_container.freeze_container_static_inputs(state, native, artifacts), receipt)

        logical = "target/sonic-vs.bin__vs__rfs.squashfs"
        (native / logical).write_bytes(b"x" * len(payloads[logical][0]))
        self.assertEqual((state / receipt["inputs"][logical]["path"]).read_bytes(), payloads[logical][0])
        with self.assertRaisesRegex(RuntimeError, "differs from prepared receipt"):
            oci_container.freeze_container_static_inputs(state, native, artifacts)

    def test_freeze_static_inputs_refuses_corrupt_existing_snapshots(self):
        cases = (("receipt", "receipt is malformed or corrupt"), ("bytes", "snapshot bytes are corrupt"),
                 ("extra", "unexpected entries"), ("mode", "mode changed"))
        for kind, message in cases:
            with self.subTest(corruption=kind):
                native, state, _payloads, artifacts = self.static_fixture("-" + kind)
                receipt = oci_container.freeze_container_static_inputs(state, native, artifacts)
                directory = (state / receipt["receipt_path"]).parent
                inode = directory.stat().st_ino
                frozen = state / next(iter(receipt["inputs"].values()))["path"]
                if kind == "receipt":
                    path = state / receipt["receipt_path"]
                    path.chmod(0o644)
                    path.write_bytes(b"{}\n")
                    path.chmod(0o444)
                elif kind == "bytes":
                    frozen.chmod(0o644)
                    frozen.write_bytes(b"corrupt frozen bytes\n")
                    frozen.chmod(0o444)
                elif kind == "extra":
                    (directory / "files").chmod(0o755)
                    (directory / "files/unexpected").write_bytes(b"unexpected\n")
                    (directory / "files").chmod(0o555)
                else:
                    frozen.chmod(0o644)
                with self.assertRaisesRegex(RuntimeError, message):
                    oci_container.freeze_container_static_inputs(state, native, artifacts)
                self.assertEqual(directory.stat().st_ino, inode)
                self.assertEqual({path.name for path in directory.parent.iterdir()}, {".lock", receipt["identity_sha256"]})

    def test_freeze_static_inputs_requires_regular_prepared_sources(self):
        native, state, payloads, artifacts = self.static_fixture("-mismatch")
        logical = "target/files/tool"
        wrong_size = copy.deepcopy(artifacts)
        wrong_size[logical]["size"] += 1
        with self.assertRaisesRegex(RuntimeError, "differs from prepared receipt"):
            oci_container.freeze_container_static_inputs(state, native, wrong_size)
        self.assertEqual({path.name for path in (state / "container-static-inputs").iterdir()}, {".lock"})

        source = native / logical
        source.unlink()
        outside = self.root / "outside-static-input"
        outside.write_bytes(payloads[logical][0])
        source.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "regular non-symlink file"):
            oci_container.freeze_container_static_inputs(state, native, artifacts)

        source.unlink()
        source.parent.rmdir()
        source.parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "not a regular directory"):
            oci_container.freeze_container_static_inputs(state, native, artifacts)

    def test_capture_uses_explicit_outputs_and_contract_keyed_read_only_receipt(self):
        root = self.root / "bazel-bin"
        state = self.root / "state"
        inv = {"swss": "swss_1.0.0_amd64.deb"}
        owned = ["docker-test.gz"]
        contract = oci_container.container_input_contract(*contract_fixture())
        provenance = {"kind": "verified-native-execution", "execution_log_sha256": "a" * 64}
        self.assertIsNone(oci_container.load_retained_inputs(state, inv, owned, contract))
        package = write_deb(root / "swss" / inv["swss"])
        image = write_image(root / "image/containers" / owned[0], "docker-test", package)
        sources = {inv["swss"]: package["path"], owned[0]: image["path"]}

        receipt = oci_container.capture_retained_inputs(state, inv, owned, contract, sources, provenance)

        contract_data = oci_container.contract_bytes(contract)
        relative = "oci-retained-inputs/" + digest(contract_data)
        self.assertEqual(receipt["schema"], 2)
        self.assertEqual(receipt["provenance"], provenance)
        self.assertEqual(receipt["receipt_path"], relative + "/receipt.json")
        self.assertEqual(receipt["contract"]["path"], relative + "/container-inputs.json")
        self.assertEqual((state / receipt["contract"]["path"]).read_bytes(), contract_data)
        self.assertEqual(receipt["package"]["identity"], {"name": "swss", "version": "1.0.0", "architecture": "amd64"})
        self.assertEqual(receipt["archives"][owned[0]]["layer_media_types"], image["layer_types"])
        for original, record in ((package["path"], receipt["package"]),
                                 (image["path"], receipt["archives"][owned[0]])):
            retained = state / record["path"]
            self.assertEqual(retained.read_bytes(), original.read_bytes())
            self.assertNotEqual(retained.stat().st_ino, original.stat().st_ino)
            self.assertFalse(stat.S_IMODE(retained.stat().st_mode) & 0o222)
            self.assertEqual(record["sha256"], digest(retained.read_bytes()))
        repeated = oci_container.capture_retained_inputs(state, inv, owned, contract, sources, provenance)
        self.assertEqual(repeated, receipt)
        image["path"].write_bytes(b"a different native baseline")
        with self.assertRaisesRegex(RuntimeError, "different baseline"):
            oci_container.capture_retained_inputs(state, inv, owned, contract, sources, provenance)
        shutil.rmtree(root)
        self.assertEqual(oci_container.load_retained_inputs(state, inv, owned, contract), receipt)
        different_inputs = contract_fixture()
        different_inputs[2]["target/debs/trixie/libc6.deb"] = "0" * 64
        different = oci_container.container_input_contract(*different_inputs)
        self.assertIsNone(oci_container.load_retained_inputs(state, inv, owned, different))
        retained = state / receipt["archives"][owned[0]]["path"]
        retained.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "file identity changed"):
            oci_container.load_retained_inputs(state, inv, owned, contract)


if __name__ == "__main__":
    unittest.main()
