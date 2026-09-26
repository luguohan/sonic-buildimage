#!/usr/bin/env python3
"""Exercise the local SONiC Bazel cache graph without native build actions."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


BAZEL_DIR = Path(__file__).resolve().parents[1]
REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BAZEL_DIR))
import driver


def write(path, contents, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    path.chmod(mode)


STUB_RUNNER = r'''#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("command", choices=("build",))
parser.add_argument("--manifest", required=True)
parser.add_argument("--spec")
parser.add_argument("--input", action="append", default=[])
parser.add_argument("--output", action="append", default=[])
args = parser.parse_args()
stage = json.loads(Path(args.spec).read_text())["stage"]
with Path(os.environ["SONIC_BAZEL_TEST_LOG"]).open("a") as stream:
    stream.write(stage + "\n")
digest = hashlib.sha256(stage.encode())
digest.update(Path(args.manifest).read_bytes())
for mapping in sorted(args.input):
    logical, path = mapping.split("=", 1)
    digest.update(logical.encode())
    digest.update(Path(path).read_bytes())
for mapping in args.output:
    logical, path = mapping.split("=", 1)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(logical + ":" + digest.hexdigest() + "\n")
'''


class BazelGraphTest(unittest.TestCase):
    def setUp(self):
        binary = os.environ.get("SONIC_BAZEL_TEST_BINARY", "bazel")
        self.binary = shutil.which(binary)
        if not self.binary:
            self.skipTest("Bazel is required; set SONIC_BAZEL_TEST_BINARY to its executable")
        self.temporary = tempfile.TemporaryDirectory(prefix="sonic-bazel-graph-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def bazel(self, workspace, command, target, options=()):
        truststore = os.environ.get("SONIC_BAZEL_TEST_TRUSTSTORE")
        startup_options = ["--host_jvm_args=-Djavax.net.ssl.trustStore=" + truststore] if truststore else []
        invocation = [
            self.binary, *startup_options, "--batch", "--nohome_rc", "--nosystem_rc", "--noworkspace_rc",
            "--output_user_root=" + str(self.root / "bazel-user-root"),
            command,
            "--repository_cache=" + str(self.root / "repository-cache"),
            "--disk_cache=" + str(self.root / "disk-cache"),
            "--remote_cache=", "--remote_executor=", "--bes_backend=",
            "--lockfile_mode=off", "--color=no", "--curses=no",
            *options, target,
        ]
        result = subprocess.run(invocation, cwd=workspace, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_swss_edit_reuses_host_and_rebuilds_container_and_image(self):
        workspace = self.root / "cache-workspace"
        workspace.mkdir()
        write(workspace / ".bazelversion", (REPOSITORY / ".bazelversion").read_text())
        write(workspace / "MODULE.bazel", 'module(name = "sonic_bazel_cache_fixture")\n')
        write(workspace / "defs.bzl", (REPOSITORY / "bazel/defs.bzl").read_text())
        write(workspace / "tools/native_action.py", STUB_RUNNER, 0o755)
        write(workspace / "tools/BUILD.bazel", 'exports_files(["native_action.py"])\n')
        write(workspace / "manifest.json", "{}\n")
        write(workspace / "host-input.txt", "host foundation\n")
        write(workspace / "swss-input.txt", "SWSS revision one\n")
        for stage in ("host", "container", "image"):
            write(workspace / (stage + ".json"), json.dumps({"stage": stage}) + "\n")
        write(workspace / "BUILD.bazel", textwrap.dedent('''\
            load(":defs.bzl", "sonic_native_stage")

            sonic_native_stage(
                name = "host", spec = "host.json", manifest = "manifest.json",
                inputs = {":host-input.txt": "host-input"},
                output_paths = ["host.bin"], mnemonic = "SonicHost",
            )
            sonic_native_stage(
                name = "container", spec = "container.json", manifest = "manifest.json",
                inputs = {":swss-input.txt": "swss-input"},
                output_paths = ["container.bin"], mnemonic = "SonicContainer",
            )
            sonic_native_stage(
                name = "image", spec = "image.json", manifest = "manifest.json",
                inputs = {":host": "host", ":container": "container"},
                output_paths = ["image.bin"], mnemonic = "SonicImage",
            )
        '''))
        log = self.root / "actions.log"
        options = ["--jobs=2", "--action_env=SONIC_BAZEL_TEST_LOG=" + str(log)]

        self.bazel(workspace, "build", "//:image", options)
        self.assertEqual(Counter(log.read_text().splitlines()), Counter(("host", "container", "image")))

        log.write_text("")
        self.bazel(workspace, "build", "//:image", options)
        self.assertEqual(Counter(log.read_text().splitlines()), Counter())

        log.write_text("")
        write(workspace / "swss-input.txt", "SWSS revision two\n")
        self.bazel(workspace, "build", "//:image", options)
        self.assertEqual(Counter(log.read_text().splitlines()), Counter(("container", "image")))

    def generated_fixture(self, target, use_oci=False):
        root = self.root / ("generated-" + target + ("-oci" if use_oci else ""))
        workspace = root / "target/bazel/workspace"
        write(root / ".bazelversion", (REPOSITORY / ".bazelversion").read_text())
        write(root / "bazel/defs.bzl", (REPOSITORY / "bazel/defs.bzl").read_text())
        write(root / "scripts/bazel/native_action.py", (REPOSITORY / "scripts/bazel/native_action.py").read_text())
        artifact = "target/debs/trixie/foundation.deb"
        write(root / artifact, "fixture foundation\n")
        request = {
            "target": target, "jobs": 2, "source_date_epoch": 1,
            "source_commit": "a" * 40, "source_branch": "bazel",
            "_native_environment": {"PATH": "/usr/bin:/bin", "RUSTUP_HOME": str(self.root / "rustup")},
            "_native_make_variables": {
                "BUILD_TIMESTAMP": "19700101.000001", "SONIC_IMAGE_VERSION": "bazel.fixture",
                "SOURCE_DATE_EPOCH": "1",
            },
        }
        containers = {}
        for name, dependencies in (
            ("docker-swss-layer-trixie.gz", []),
            ("docker-orchagent.gz", ["docker-swss-layer-trixie.gz"]),
        ):
            containers[name] = {
                "path": "dockers/" + name.removesuffix(".gz"),
                "debs_path": "target/debs/trixie", "files_path": "target/files/trixie",
                "load_dockers": dependencies, "after": [],
                "depends": [],
            }
        inventory = {
            "image": "sonic-vs.img.gz" if target == "vs-kvm" else "sonic-vs.bin",
            "image_version": "bazel.fixture",
            "swss": "swss_1.0.0_amd64.deb", "swss_dbg": "swss-dbg_1.0.0_amd64.deb",
            "containers": containers,
        }
        artifact_sha256 = hashlib.sha256(b"fixture foundation\n").hexdigest()
        artifacts = {artifact: {"sha256": artifact_sha256, "size": 19}}
        manifest = {
            "schema": 1,
            "source": {
                "repositories": [{"path": ".", "commit": "a" * 40, "branch": "bazel", "role": "root"}],
                "entries": {},
            },
            "environment": {}, "environment_digest": "c" * 64,
            "native_environment": request["_native_environment"],
            "native_make_variables": request["_native_make_variables"],
            "dependency_artifacts": {artifact: artifact_sha256},
            "private_environment_sha256": "d" * 64, "digest": "e" * 64,
        }
        contract = driver.container_input_contract(request, inventory, sorted(containers), artifacts, manifest)
        retained = None
        if use_oci:
            write(root / "bazel/oci_defs.bzl", (REPOSITORY / "bazel/oci_defs.bzl").read_text())
            write(root / "scripts/bazel/oci_container.py", (REPOSITORY / "scripts/bazel/oci_container.py").read_text())
            contract_data = driver.oci_container.contract_bytes(contract)
            contract_sha256 = hashlib.sha256(contract_data).hexdigest()
            prefix = "oci-retained-inputs/" + contract_sha256 + "/"
            contract_path = prefix + "container-inputs.json"
            write(root / "target/bazel" / contract_path, contract_data.decode())
            package_path = prefix + inventory["swss"]
            write(root / "target/bazel" / package_path, "retained package fixture\n")
            retained = {
                "contract": {"path": contract_path, "sha256": contract_sha256},
                "package": {"path": package_path, "sha256": "a" * 64}, "archives": {},
            }
            for name in containers:
                path = prefix + name
                write(root / "target/bazel" / path, "retained archive fixture\n")
                retained["archives"][name] = {
                    "path": path, "sha256": "b" * 64, "image_name": name.removesuffix(".gz"),
                    "layer_media_types": ["application/vnd.oci.image.layer.v1.tar"],
                }
        with mock.patch.multiple(driver, ROOT=root, STATE=root / "target/bazel", WORKSPACE=workspace):
            driver.generate_workspace(request, inventory, sorted(containers), artifacts, manifest, retained, contract)
        write(workspace / "swss/swss.deb", "fixture SWSS package\n")
        write(workspace / "swss/swss-dbg.deb", "fixture SWSS debug package\n")
        write(workspace / "swss/BUILD.bazel", textwrap.dedent('''\
            filegroup(name = "swss_deb", srcs = ["swss.deb"], visibility = ["//visibility:public"])
            filegroup(name = "swss_dbg_deb", srcs = ["swss-dbg.deb"], visibility = ["//visibility:public"])
        '''))
        return workspace

    def test_generated_vs_targets_analyze_with_expected_outputs(self):
        cases = (
            ("vs", "//image:sonic_vs", {"image/onie/sonic-vs.bin"},
             Counter({"SonicContainer": 2, "SonicHost": 1, "SonicImage": 1, "SonicOnie": 1})),
            ("vs-kvm", "//image:sonic_vs_kvm", {"image/kvm/sonic-vs.img.gz", "image/kvm/sonic-vs-uefi.img.gz"},
             Counter({"SonicContainer": 2, "SonicHost": 1, "SonicImage": 1, "SonicOnie": 1, "SonicKvm": 1})),
        )
        for variant, target, expected, expected_actions in cases:
            with self.subTest(target=target):
                workspace = self.generated_fixture(variant)
                output = self.bazel(
                    workspace, "cquery", target,
                    ["--output=files"],
                )
                files = output.splitlines()
                self.assertEqual(len(files), len(expected), output)
                self.assertTrue(all(path.startswith("bazel-out/") for path in files), output)
                self.assertTrue(all(any(path.endswith(suffix) for path in files) for suffix in expected), output)
                graph = json.loads(self.bazel(workspace, "aquery", "deps(" + target + ")", ["--output=jsonproto"]))
                actions = [action for action in graph.get("actions", []) if action["mnemonic"].startswith("Sonic")]
                self.assertEqual(Counter(action["mnemonic"] for action in actions), expected_actions)
                for action in actions:
                    arguments = action["arguments"]
                    spec = json.loads((workspace / arguments[arguments.index("--spec") + 1]).read_text())
                    outputs = dict(arguments[index + 1].split("=", 1) for index, value in enumerate(arguments) if value == "--output")
                    self.assertEqual(set(outputs), set(spec["outputs"]))
                    for logical, path in outputs.items():
                        self.assertTrue(path.startswith("bazel-out/") and path.endswith("/image/" + logical), path)

    def test_generated_oci_vs_graph_replaces_native_container_actions(self):
        workspace = self.generated_fixture("vs", use_oci=True)
        graph = json.loads(self.bazel(workspace, "aquery", "deps(//image:sonic_vs)", ["--output=jsonproto"]))
        actions = [action for action in graph.get("actions", []) if action["mnemonic"].startswith("Sonic")]
        self.assertEqual(Counter(action["mnemonic"] for action in actions), Counter({
            "SonicOciContract": 1, "SonicOciExtract": 2, "SonicSwssOverlay": 1, "SonicOciGzip": 2,
            "SonicHost": 1, "SonicImage": 1, "SonicOnie": 1,
        }))
        for action in actions:
            if action["mnemonic"] in {"SonicOciContract", "SonicOciExtract", "SonicSwssOverlay", "SonicOciGzip"}:
                self.assertTrue(action["arguments"][0].endswith("/oci_container.py"))
                self.assertNotIn("no-sandbox", {item["key"] for item in action.get("executionInfo", [])})
        outputs = self.bazel(workspace, "cquery", "//image:oci_overlay_metadata", ["--output=files"]).splitlines()
        self.assertEqual(len(outputs), 1)
        self.assertTrue(outputs[0].endswith("/image/swss_overlay.metadata.json"))


if __name__ == "__main__":
    unittest.main()
