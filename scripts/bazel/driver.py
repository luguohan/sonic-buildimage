#!/usr/bin/env python3
"""Prepare and invoke the incremental SONiC Bazel graph inside sonic-slave."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_action
import oci_container


ROOT = Path.cwd().resolve()
STATE = ROOT / "target/bazel"
WORKSPACE = STATE / "workspace"


def write_text(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != data:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(data)
        temporary.chmod(mode)
        temporary.replace(path)
    else:
        path.chmod(mode)


def write_json(path, value, mode=0o644):
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n", mode)


def load_json(path):
    return json.loads(path.read_text())


def run(command, cwd=ROOT, env=None):
    print("+ " + " ".join(command[:8]) + (" ..." if len(command) > 8 else ""), flush=True)
    environment = dict(os.environ if env is None else env)
    environment["PWD"] = str(Path(cwd).resolve())
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def capture_environment():
    # Replay the actual Makefile.work command variables so inventory and native
    # actions use the same evaluated configuration. Keep the process environment
    # small so Bazel's compiler discovery sees the same default toolchain as SWSS.
    command_variables = set(os.environ.get("SONIC_BAZEL_COMMAND_VARIABLES", "").split())
    command_variables.discard("SONIC_BUILD_TARGET")
    native = {key: os.environ[key] for key in native_action.PROCESS_KEYS if key in os.environ}
    required = {"HOME", "USER", "PATH", "RUSTUP_HOME"}
    missing = required - set(native)
    if missing:
        raise RuntimeError("bazel-driver did not receive native configuration: " + ", ".join(sorted(missing)))
    private = {key: os.environ[key] for key in native_action.PRIVATE_KEYS if key in os.environ}
    make_variables = {
        key: os.environ[key] for key in command_variables - native_action.PRIVATE_KEYS
        if key in os.environ
    }
    return native, private, make_variables


def build_environment(request):
    result = dict(request["_native_environment"])
    result.update(request["_private_environment"])
    result["PWD"] = str(ROOT)
    return result


def make(request, targets, variables=None, environment=None, assume_old=()):
    command = ["make", "-f", "slave.mk", "-f", "bazel/native.mk", "--no-print-directory", "-j" + str(request["jobs"])]
    values = dict(request["_native_make_variables"])
    values.update(variables or {})
    values["SONIC_BUILD_TARGET"] = targets[-1]
    command.extend(name + "=" + str(value) for name, value in values.items())
    command.extend("--assume-old=" + path for path in sorted(set(assume_old)))
    command.extend(targets)
    env = build_environment(request)
    env.update(environment or {})
    run(command, env=env)


def inventory(request, image):
    path = STATE / ("native-inventory-" + image.replace(".", "_") + ".json")
    make(request, ["bazel-inventory"], {"BAZEL_IMAGE": image, "BAZEL_INVENTORY": str(path.relative_to(ROOT))})
    result = load_json(path)
    if (result["platform"], result["arch"], result["distro"]) != ("vs", "amd64", "trixie"):
        raise RuntimeError("this Bazel graph requires the public amd64 Trixie VS configuration")
    unsupported = [name for name in (
        "enable_sbom", "enable_asan", "install_debug_tools", "build_multiasic_kvm",
        "multiarch_qemu_environ", "cross_build_environ", "image_signature",
    ) if result["features"].get(name) == "y"]
    if result["features"].get("post_build_hook"):
        unsupported.append("post_build_hook")
    if result["features"].get("secure_upgrade_mode", "").strip("\"'") not in ("", "no", "none", "no_sign"):
        unsupported.append("secure_upgrade_mode")
    if result["remote_packages"]:
        unsupported.append("remote SONiC packages")
    if {"nostrip", "noopt"} & set(result["swss_deb_build_options"].split()):
        unsupported.append("SWSS debugging or profiling package mode")
    if result["swss_deb_build_profiles"]:
        unsupported.append("SWSS Debian build profiles")
    if unsupported:
        raise RuntimeError("the local Bazel graph does not support: " + ", ".join(unsupported))
    if "docker-orchagent.gz" not in result["owned_dockers"]:
        raise RuntimeError("configured Docker graph does not connect docker-orchagent to SWSS")
    return result


def docker_closure(inv, roots):
    result = set()
    active = set()

    def visit(name):
        if name in active:
            raise RuntimeError("cycle in configured Docker graph at " + name)
        if name in result:
            return
        if name not in inv["containers"]:
            raise RuntimeError("missing configured Docker input " + name)
        active.add(name)
        item = inv["containers"][name]
        for dependency in item["load_dockers"] + item["after"]:
            visit(dependency)
        active.remove(name)
        result.add(name)

    for root in roots:
        visit(root)
    return result


def selected_owned(request, inv):
    if request["target"] == "swss":
        return []
    selected = docker_closure(inv, ["docker-orchagent.gz"]) if request["target"] == "container" else set(inv["selected_dockers"])
    return sorted(selected & set(inv["owned_dockers"]))


def prerequisite_targets(inv, owned):
    targets = {"target/debs/trixie/" + name for name in inv["swss_depends"] + inv["swss_rdepends"]}
    excluded = {inv["swss"], inv["swss_dbg"]}
    for name in owned:
        item = inv["containers"][name]
        targets.update(item["debs_path"] + "/" + value for value in item["depends"] if value not in excluded)
        targets.update(item["files_path"] + "/" + value for value in item["files"])
        targets.update("target/python-wheels/trixie/" + value for value in item["wheels"])
        targets.update("target/python-debs/trixie/" + value for value in item["python_debs"])
        targets.update("target/" + value for value in item["load_dockers"] + item["after"] if value not in owned)
        targets.update("target/debs/trixie/" + value for value in item["install_debs"])
        targets.update("target/python-wheels/trixie/" + value for value in item["install_wheels"])
    return sorted(targets)


def install_targets(inv, owned):
    targets = {"target/debs/trixie/" + name + "-install" for name in inv["swss_depends"]}
    for name in owned:
        item = inv["containers"][name]
        targets.update("target/debs/trixie/" + value + "-install" for value in item["install_debs"])
        targets.update("target/python-wheels/trixie/" + value + "-install" for value in item["install_wheels"])
    return sorted(targets)


def container_context_mounts(inv, names):
    result = {}
    for name in names:
        item = inv["containers"][name]
        if not item["path"] or not item["debs_path"] or not item["files_path"]:
            continue
        mappings = {
            item["path"] + "/debs": item["debs_path"],
            item["path"] + "/files": item["files_path"],
            item["path"] + "/python-debs": "target/python-debs/trixie",
            item["path"] + "/python-wheels": "target/python-wheels/trixie",
        }
        for path, source in mappings.items():
            if path in result and result[path] != source:
                raise RuntimeError("conflicting native Docker context mounts: " + path)
            result[path] = source
    return result


def artifact_state(inv):
    excluded_names = set(inv["owned_dockers"]) | {inv["swss"], inv["swss_dbg"], "sonic-vs.bin", "sonic-vs.img.gz", "sonic-vs-uefi.img.gz"}
    result = {}
    target = ROOT / "target"
    for directory, directories, filenames in os.walk(target):
        directory = Path(directory)
        if directory == target:
            directories[:] = [name for name in directories if name not in {"bazel", "vcache", "versions", "logs", "phony"}]
        for filename in filenames:
            path = directory / filename
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(ROOT)
            parts = relative.parts
            if path.name in excluded_names or path.name.endswith((".log", ".lock", ".dep", ".tmp")):
                continue
            selected = len(parts) > 1 and parts[1] in {"debs", "files", "python-debs", "python-wheels"}
            selected = selected or path.name.endswith((".gz", ".squashfs", ".iso"))
            if selected:
                result[str(relative)] = {"sha256": native_action.digest_file(path), "size": path.stat().st_size}
    return result


def artifacts_match(artifacts):
    for relative, expected in artifacts.items():
        path = ROOT / relative
        if not path.is_file() or path.stat().st_size != expected["size"] or native_action.digest_file(path) != expected["sha256"]:
            return False
    return True


def native_source_state(request):
    return native_action.source_state(
        ROOT, context="native", snapshot_identity=request["native_snapshot_identity"],
    )


def preparation_key(request, inv, owned):
    source = native_source_state(request)
    source_digest = native_action.digest_bytes(native_action.canonical_json(source))
    key_data = {
        "source": source_digest, "inventory": inv, "owned": owned,
        "make_variables": request["_native_make_variables"], "slave_image_id": request["slave_image_id"],
        "target": request["target"],
        "native_environment": request["_native_environment"],
        "source_input_digest": request["native_source_input_digest"],
    }
    return native_action.digest_bytes(native_action.canonical_json(key_data))


def approve_native_source(request):
    source = native_source_state(request)
    write_json(STATE / "native-source-approved.json", {
        "schema": 2, "source_input_digest": request["native_source_input_digest"],
        "snapshot_id": request["native_snapshot_identity"]["snapshot_id"],
        "prepared_digest": native_action.digest_bytes(native_action.canonical_json(source)),
    })


def prepare_native(request, inv, owned):
    key = preparation_key(request, inv, owned)
    receipt_path = STATE / ("prepared-" + request["target"] + ".json")
    receipt = load_json(receipt_path) if receipt_path.exists() else {}
    reusable = not request["reprepare"] and receipt.get("key") == key and artifacts_match(receipt.get("artifacts", {}))
    if reusable:
        print("Native prerequisites match the saved content receipt.", flush=True)
        artifacts = receipt["artifacts"]
    else:
        try:
            targets = prerequisite_targets(inv, owned)
            if targets:
                make(request, targets)
            if request["target"] in {"vs", "vs-kvm"}:
                ignored = ["target/debs/trixie/" + inv["swss"], "target/debs/trixie/" + inv["swss_dbg"]]
                ignored += ["target/" + name for name in owned]
                make(
                    request, ["target/" + inv["image"]], {"BAZEL_IMAGE": inv["image"]},
                    {"SONIC_BAZEL_REQUESTED_STAGE": "prepare"}, ignored,
                )
        finally:
            native_action.cleanup_context_mounts(ROOT, container_context_mounts(inv, inv["selected_dockers"]), build_environment(request))
            if request["target"] in {"vs", "vs-kvm"}:
                native_action.cleanup_host_root(ROOT, build_environment(request))
        artifacts = artifact_state(inv)
        missing = [path for path in prerequisite_targets(inv, owned) if not (ROOT / path).is_file()]
        if missing:
            raise RuntimeError("native preparation did not produce: " + ", ".join(missing[:8]))
        native_action.cleanup_p4_source_repositories(
            ROOT, request["native_snapshot_identity"], inv["p4c_version"],
        )
        key = preparation_key(request, inv, owned)
        write_json(receipt_path, {"schema": 1, "key": key, "artifacts": artifacts})
    approve_native_source(request)
    return artifacts


def prepared_artifacts(request, inv, owned):
    receipt_path = STATE / ("prepared-" + request["target"] + ".json")
    receipt = load_json(receipt_path) if receipt_path.exists() else {}
    artifacts = receipt.get("artifacts", {})
    required = set(prerequisite_targets(inv, owned))
    valid = receipt.get("schema") == 1 and receipt.get("key") == preparation_key(request, inv, owned)
    valid = valid and isinstance(artifacts, dict) and bool(artifacts) and required <= set(artifacts)
    if not valid or not artifacts_match(artifacts):
        raise RuntimeError("native preparation changed before the action slave; rerun scripts/bazel/run")
    return artifacts


def prepare_action_environment(request, inv, owned, artifacts):
    if request["target"] in {"vs", "vs-kvm"}:
        ignored = list(artifacts) + ["target/debs/trixie/" + inv["swss"], "target/debs/trixie/" + inv["swss_dbg"]]
        ignored += ["target/" + name for name in owned]
        try:
            make(
                request, ["target/" + inv["image"]], {"BAZEL_IMAGE": inv["image"]},
                {"SONIC_BAZEL_REQUESTED_STAGE": "prepare"}, ignored,
            )
        finally:
            native_action.cleanup_context_mounts(ROOT, container_context_mounts(inv, inv["selected_dockers"]), build_environment(request))
            native_action.cleanup_host_root(ROOT, build_environment(request))
    installs = install_targets(inv, owned)
    if installs:
        make(request, installs, assume_old=artifacts)
    make(request, ["docker-start"], assume_old=artifacts)


def prepare_swss(request, inv):
    source = STATE / "swss-source"
    configured = STATE / "swss-configured"
    if not (source / ".sonic-bazel-source").is_file():
        raise RuntimeError("the selected public SWSS source was not staged")
    if configured.exists():
        if not (configured / ".sonic-bazel-configured").is_file():
            raise RuntimeError("refusing to replace an unowned SWSS configuration directory")
        shutil.rmtree(configured)
    shutil.copytree(source, configured, symlinks=True)
    (configured / ".sonic-bazel-configured").touch()
    environment = build_environment(request)
    environment["SOURCE_DATE_EPOCH"] = str(request["source_date_epoch"])
    environment["DEB_BUILD_OPTIONS"] = inv["swss_deb_build_options"]
    environment["DEB_BUILD_PROFILES"] = inv["swss_deb_build_profiles"]
    environment["CARGO_HOME"] = str(STATE / "cargo-home")
    (STATE / "cargo-home").mkdir(parents=True, exist_ok=True)
    run(["./autogen.sh"], cwd=configured, env=environment)
    run(["./debian/rules", "override_dh_auto_configure"], cwd=configured, env=environment)
    vendor = STATE / "cargo-vendor"
    vendor_config = STATE / "cargo-vendor.toml"
    if vendor.exists():
        shutil.rmtree(vendor)
    with vendor_config.open("w") as stream:
        subprocess.run(
            ["cargo", "vendor", "--locked", "--versioned-dirs", str(vendor)],
            cwd=configured, env=environment, stdout=stream, check=True,
        )
    run([
        "python3", "bazel/generate.py", "--source", str(configured),
        "--configured-build", str(configured), "--output-package", str(WORKSPACE / "swss"),
        "--architecture", "amd64", "--source-date-epoch", str(request["source_date_epoch"]),
        "--deb-build-options", inv["swss_deb_build_options"],
        "--cargo-vendor", str(vendor), "--cargo-vendor-config", str(vendor_config),
    ], cwd=configured, env=environment)


def prepare_environment(request, inv, artifacts):
    private = request["_private_environment"]
    private_path = STATE / "private-environment.json"
    write_json(private_path, private, 0o600)
    environment = native_action.tool_environment(request["slave_image_id"], build_environment(request))
    # Native -install rules recursively install development packages. Hash the
    # prepared artifacts until Make exports that complete installed closure.
    dependency_hashes = {path: value["sha256"] for path, value in artifacts.items()}
    environment_digest = native_action.digest_bytes(native_action.canonical_json({
        "environment": environment, "dependencies": dependency_hashes,
        "native_environment": request["_native_environment"],
        "native_make_variables": request["_native_make_variables"],
    }))
    manifest = {
        "schema": 1, "source": native_source_state(request),
        "environment": environment, "environment_digest": environment_digest,
        "native_environment": request["_native_environment"],
        "native_make_variables": request["_native_make_variables"],
        "dependency_artifacts": dependency_hashes,
        "private_environment_sha256": native_action.digest_file(private_path),
    }
    manifest["digest"] = native_action.digest_bytes(native_action.canonical_json(manifest))
    approve_native_source(request)
    return manifest


def starlark(value):
    return json.dumps(value, indent=4, sort_keys=True)


def native_stage_spec(request, inv, stage, target, outputs, host_snapshot=None, assume_old=(), context_mounts=None):
    spec = {
        "schema": 1, "stage": stage, "target": target, "outputs": outputs,
        "environment": {
            "SONIC_BAZEL_SOURCE_COMMIT": request["source_commit"],
            "SONIC_BAZEL_SOURCE_BRANCH": request["source_branch"],
            "SOURCE_DATE_EPOCH": str(request["source_date_epoch"]),
        },
        "make_variables": {**request["_native_make_variables"], "BAZEL_IMAGE": inv["image"]},
        "assume_old": sorted(set(assume_old)),
        "context_mounts": context_mounts or {},
    }
    if host_snapshot:
        spec["host_snapshot"] = host_snapshot
    return spec


def container_stage_specs(request, inv, owned):
    result = {}
    for name in owned:
        item = inv["containers"][name]
        if inv["swss_dbg"] in item.get("depends", []):
            raise RuntimeError("the OCI container graph does not support an installed SWSS debug package: " + name)
        mounts = container_context_mounts(inv, [name])
        if len(mounts) != 4:
            raise RuntimeError("Bazel-owned container lacks native context paths: " + name)
        result[name] = native_stage_spec(
            request, inv, "container", "target/" + name,
            {"containers/" + name: "target/" + name}, context_mounts=mounts,
        )
    return result


def container_input_contract(request, inv, owned, artifacts, manifest):
    static_inputs = {path: item["sha256"] for path, item in artifacts.items()}
    dynamic_inputs = sorted("target/debs/trixie/" + name for name in (inv["swss"], inv["swss_dbg"]))
    dependencies = {
        name: sorted({"target/" + dependency for dependency in
                     inv["containers"][name]["load_dockers"] + inv["containers"][name]["after"]
                     if dependency in owned})
        for name in owned
    }
    return oci_container.container_input_contract(
        manifest, container_stage_specs(request, inv, owned), static_inputs, dynamic_inputs, dependencies,
    )


def oci_container_rules(inv, owned, retained, baseline_label, archive_labels, contract_label):
    def rule(kind, attributes):
        return kind + "(\n" + "\n".join(
            "    " + key + " = " + starlark(value) + "," for key, value in attributes.items()
        ) + "\n)"

    lines = [rule("validate_retained_contract", {
        "name": "oci_input_contract", "baseline": contract_label,
        "current": ":container-inputs.json", "baseline_sha256": retained["contract"]["sha256"],
        "tool": "//tools:oci_container.py",
    })]
    seeds = []
    names = {}
    for name in owned:
        suffix = name.removesuffix(".gz").replace("-", "_")
        layout = "retained_" + suffix + "_layout"
        base = "retained_" + suffix
        names[name] = (suffix, base)
        seeds.append(":" + layout)
        archive = retained["archives"][name]
        lines.append(rule("retained_oci_layout", {
            "name": layout, "src": archive_labels[name], "baseline_deb": baseline_label,
            "contract": ":oci_input_contract",
            "archive_sha256": archive["sha256"], "baseline_sha256": retained["package"]["sha256"],
            "image_name": archive["image_name"], "tool": "//tools:oci_container.py",
        }))
        lines.append(rule("image_manifest_from_oci_layout", {
            "name": base, "src": ":" + layout, "architecture": "amd64", "os": "linux",
            "layers": archive["layer_media_types"],
        }))
    lines.append(rule("swss_package_overlay", {
        "name": "swss_overlay", "deb": "//swss:swss_deb", "baseline_deb": baseline_label,
        "baseline_sha256": retained["package"]["sha256"], "seeds": seeds,
        "tool": "//tools:oci_container.py",
    }))
    lines.append('layer_from_tar(\n    name = "swss_overlay_layer",\n    src = ":swss_overlay",\n    compress = "none",\n    optimize = False,\n)')
    lines.append('filegroup(name = "oci_overlay_metadata", srcs = [":swss_overlay"], output_group = "metadata")')
    for name in owned:
        suffix, base = names[name]
        image = "oci_" + suffix
        archive = "archive_" + suffix
        tar = "tar_" + suffix
        lines.append(rule("image_manifest", {
            "name": image, "base": ":" + base, "layers": [":swss_overlay_layer"],
            "created": ":image-created.txt", "labels": {"Tag": inv["image_version"]},
        }))
        lines.append(rule("image_load", {
            "name": archive, "image": ":" + image, "tag": name.removesuffix(".gz") + ":latest",
        }))
        lines.append(rule("filegroup", {
            "name": tar, "srcs": [":" + archive], "output_group": "tarball",
        }))
        lines.append(rule("deterministic_gzip", {
            "name": "container_" + name.replace("-", "_").replace(".", "_"),
            "src": ":" + tar, "out": "containers/" + name, "tool": "//tools:oci_container.py",
        }))
    return lines


def generate_workspace(request, inv, owned, artifacts, manifest, retained=None, contract=None):
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    write_text(WORKSPACE / ".bazelversion", (ROOT / ".bazelversion").read_text())
    module = 'module(name = "sonic_vs_local")\n\nbazel_dep(name = "rules_cc", version = "0.1.1")\n'
    if retained:
        module += 'bazel_dep(name = "rules_img", version = "0.3.22")\n'
    write_text(WORKSPACE / "MODULE.bazel", module)
    rc = [
        "startup --output_user_root=/sonic/target/bazel/output-user-root",
        "common --repository_cache=/sonic/target/bazel/repository-cache",
        "build --disk_cache=/sonic/target/bazel/disk-cache",
        "build --remote_upload_local_results=false",
        "build --strip=never",
        "build --jobs=" + str(request["jobs"]),
        "build --action_env=SONIC_BAZEL_NATIVE_ROOT=/sonic",
        "build --action_env=SONIC_BAZEL_PRIVATE_ENV=/sonic/target/bazel/private-environment.json",
        "build --action_env=SONIC_BAZEL_ENVIRONMENT_DIGEST=" + manifest["environment_digest"],
        "build --action_env=SOURCE_DATE_EPOCH=" + str(request["source_date_epoch"]),
        "build --action_env=PATH=" + request["_native_environment"]["PATH"],
        "build --action_env=RUSTUP_HOME=" + request["_native_environment"]["RUSTUP_HOME"],
    ]
    write_text(WORKSPACE / ".bazelrc", "\n".join(rc) + "\n")
    write_text(WORKSPACE / "tools/native_action.py", (ROOT / "scripts/bazel/native_action.py").read_text(), 0o755)
    tools = ["native_action.py"]
    if retained:
        write_text(WORKSPACE / "tools/oci_container.py", (ROOT / "scripts/bazel/oci_container.py").read_text(), 0o755)
        write_text(WORKSPACE / "image/oci_defs.bzl", (ROOT / "bazel/oci_defs.bzl").read_text())
        tools.append("oci_container.py")
    write_text(WORKSPACE / "tools/BUILD.bazel", "exports_files(" + starlark(tools) + ")\n")
    write_text(WORKSPACE / "image/defs.bzl", (ROOT / "bazel/defs.bzl").read_text())
    write_json(WORKSPACE / "image/source-manifest.json", manifest)
    if contract is not None:
        write_text(WORKSPACE / "image/container-inputs.json", oci_container.contract_bytes(contract).decode())
    inputs_dir = WORKSPACE / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    labels = {}
    keep = {"BUILD.bazel"}

    def expose_input(logical):
        name = hashlib.sha256(logical.encode()).hexdigest()[:16] + "-" + Path(logical).name
        link = inputs_dir / name
        target = ROOT / logical
        if not link.is_symlink() or link.resolve() != target:
            link.unlink(missing_ok=True)
            link.symlink_to(target)
        keep.add(name)
        return "//inputs:" + name

    for logical in sorted(artifacts):
        labels[expose_input(logical)] = logical
    baseline_label = None
    contract_label = None
    archive_labels = {}
    if retained:
        if contract is None:
            raise RuntimeError("retained OCI inputs require the current container input contract")
        baseline_label = expose_input(str((STATE / retained["package"]["path"]).relative_to(ROOT)))
        contract_label = expose_input(str((STATE / retained["contract"]["path"]).relative_to(ROOT)))
        for name in owned:
            archive_labels[name] = expose_input(str((STATE / retained["archives"][name]["path"]).relative_to(ROOT)))
    for path in inputs_dir.iterdir():
        if path.name not in keep:
            if path.is_dir() and not path.is_symlink():
                raise RuntimeError("unexpected directory in generated input package")
            path.unlink()
    write_text(inputs_dir / "BUILD.bazel", "exports_files(" + starlark(sorted(keep - {"BUILD.bazel"})) + ")\n")
    lines = [
        'load(":defs.bzl", "sonic_native_stage")',
        'exports_files(["source-manifest.json"])',
    ]
    if retained:
        lines = [
            'load(":oci_defs.bzl", "deterministic_gzip", "retained_oci_layout", "swss_package_overlay", "validate_retained_contract")',
            'load("@rules_img//img:convert.bzl", "image_manifest_from_oci_layout")',
            'load("@rules_img//img:image.bzl", "image_manifest")',
            'load("@rules_img//img:layer.bzl", "layer_from_tar")',
            'load("@rules_img//img:load.bzl", "image_load")',
        ] + lines
        created = datetime.datetime.fromtimestamp(request["source_date_epoch"], datetime.timezone.utc)
        write_text(WORKSPACE / "image/image-created.txt", created.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    def add_stage(name, stage, target, inputs, outputs, mnemonic, host_snapshot=None, assume_old=(), context_mounts=None):
        spec = native_stage_spec(request, inv, stage, target, outputs, host_snapshot, assume_old, context_mounts)
        spec_name = name + ".json"
        write_json(WORKSPACE / "image" / spec_name, spec)
        lines.append("sonic_native_stage(\n    name = " + starlark(name) + ",\n    spec = " + starlark(":" + spec_name) +
                     ',\n    manifest = ":source-manifest.json",\n    mnemonic = ' + starlark(mnemonic) +
                     ",\n    inputs = " + starlark(inputs) + ",\n    output_paths = " + starlark(list(outputs)) + ",\n)")

    package_inputs = {
        "//swss:swss_deb": "target/debs/trixie/" + inv["swss"],
        "//swss:swss_dbg_deb": "target/debs/trixie/" + inv["swss_dbg"],
    }
    container_labels = {name: ":containers/" + name for name in owned}
    if retained:
        lines.extend(oci_container_rules(inv, owned, retained, baseline_label, archive_labels, contract_label))
    else:
        specs = container_stage_specs(request, inv, owned)
        for name in owned:
            rule_name = "container_" + name.replace("-", "_").replace(".", "_")
            output_path = "containers/" + name
            inputs = dict(labels)
            inputs.update(package_inputs)
            item = inv["containers"][name]
            for dependency in item["load_dockers"] + item["after"]:
                if dependency in owned:
                    inputs[":containers/" + dependency] = "target/" + dependency
            add_stage(rule_name, "container", "target/" + name, inputs, {output_path: "target/" + name}, "SonicContainer", context_mounts=specs[name]["context_mounts"])
    if container_labels:
        lines.append('filegroup(name = "owned_container_archives", srcs = ' + starlark(list(container_labels.values())) + ')')
    if "docker-orchagent.gz" in container_labels:
        lines.append('alias(name = "swss_container", actual = ":containers/docker-orchagent.gz")')
    if request["target"] in {"vs", "vs-kvm"}:
        variant = "kvm" if request["target"] == "vs-kvm" else "onie"
        native_target = "target/" + inv["image"]
        snapshot = "target/bazel/native/host-" + variant + ".squashfs"
        host_output = "host/" + variant + ".squashfs"
        ignored = list(package_inputs.values()) + ["target/" + name for name in owned]
        add_stage("host_" + variant, "host", native_target, labels, {host_output: snapshot}, "SonicHost", snapshot, ignored)
        image_inputs = dict(labels)
        image_inputs.update(package_inputs)
        image_inputs[":" + host_output] = snapshot
        for name, label in container_labels.items():
            image_inputs[label] = "target/" + name
        composition_outputs = {variant + "/" + name: name for name in ("fs.squashfs", "dockerfs.tar.gz", "fs.zip")}
        add_stage("image_" + variant, "image", native_target, image_inputs, composition_outputs, "SonicImage", snapshot)
        installer_inputs = dict(image_inputs)
        for output_path, native in composition_outputs.items():
            installer_inputs[":" + output_path] = native
        installer_output = variant + "/sonic-vs.bin"
        add_stage("installer_" + variant, "onie", native_target, installer_inputs, {installer_output: "target/sonic-vs.bin"}, "SonicOnie")
        if variant == "onie":
            lines.append('alias(name = "sonic_vs", actual = ":onie/sonic-vs.bin")')
        else:
            kvm_inputs = dict(installer_inputs)
            kvm_inputs[":" + installer_output] = "target/sonic-vs.bin"
            kvm_outputs = {
                "kvm/sonic-vs.img.gz": "target/sonic-vs.img.gz",
                "kvm/sonic-vs-uefi.img.gz": "target/sonic-vs-uefi.img.gz",
            }
            add_stage("sonic_vs_kvm", "kvm", native_target, kvm_inputs, kvm_outputs, "SonicKvm")
    write_text(WORKSPACE / "image/BUILD.bazel", "\n\n".join(lines) + "\n")


def build(request, inv, manifest, retained=None, contract=None):
    target = {
        "swss": "//swss:swss_deb", "container": "//image:swss_container",
        "vs": "//image:sonic_vs", "vs-kvm": "//image:sonic_vs_kvm",
    }[request["target"]]
    labels = {"//swss:swss_deb": 1, "//swss:swss_dbg_deb": 1, "//swss:swss_package_manifest": 1}
    if request["target"] != "swss":
        labels["//image:swss_container"] = 1
    if request["target"] in {"vs", "vs-kvm"}:
        labels[target] = 2 if request["target"] == "vs-kvm" else 1
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    log_dir = STATE / "logs" / stamp
    log_dir.mkdir(parents=True, exist_ok=True)
    bazel = ["bazel", "--nosystem_rc", "--nohome_rc"]
    environment = build_environment(request)
    environment["PWD"] = str(WORKSPACE)
    owned = selected_owned(request, inv)
    if owned and contract is None:
        raise RuntimeError("container builds require the current input contract")
    # cquery reports paths without materializing cached outputs. Request every
    # collected artifact as a top-level build output before querying its path.
    metadata_label = "//image:oci_overlay_metadata"
    archive_label = "//image:owned_container_archives"
    validation_labels = [archive_label] if owned else []
    if retained:
        validation_labels.append(metadata_label)
    command = bazel + [
        "build", "--profile=" + str(log_dir / "profile.json.gz"),
        "--execution_log_json_file=" + str(log_dir / "execution.json"),
    ] + request["bazel_args"] + list(labels) + validation_labels
    run(command, cwd=WORKSPACE, env=environment)
    artifact_dir = STATE / "artifacts" / request["target"]
    artifact_dir.mkdir(parents=True, exist_ok=True)

    def query_paths(label, expected_count):
        result = subprocess.check_output(
            bazel + ["cquery", "--output=files"] + request["bazel_args"] + [label],
            cwd=WORKSPACE, env=environment, text=True,
        )
        paths = [WORKSPACE / line.strip() for line in result.splitlines() if line.strip()]
        if len(paths) != expected_count or any(not path.is_file() for path in paths):
            raise RuntimeError("Bazel did not produce the expected outputs for " + label)
        return paths

    outputs = {}
    produced_paths = {}
    for label, expected_count in labels.items():
        paths = query_paths(label, expected_count)
        for path in paths:
            destination = artifact_dir / path.name
            native_action.copy_file(path, destination)
            outputs[path.name] = {"sha256": native_action.digest_file(destination), "size": destination.stat().st_size, "label": label}
            produced_paths[path.name] = path
    archive_paths = {}
    if owned:
        archive_paths = {path.name: path for path in query_paths(archive_label, len(owned))}
        if set(archive_paths) != set(owned):
            raise RuntimeError("Bazel did not produce the selected owned container archives")
    report = {
        "schema": 1, "target": request["target"], "buildimage_commit": request["source_commit"],
        "buildimage_branch": request["source_branch"], "swss_commit": request["swss_commit"],
        "swss_source_digest": request["swss_digest"], "source_date_epoch": request["source_date_epoch"],
        "environment_digest": manifest["environment_digest"], "image_version": inv["image_version"],
        "source_manifest_sha256": native_action.digest_file(WORKSPACE / "image/source-manifest.json"),
        "owned_containers": inv["owned_dockers"], "outputs": outputs,
        "profile": str(log_dir / "profile.json.gz"), "execution_log": str(log_dir / "execution.json"),
    }
    report["container_backend"] = "oci" if retained else ("native-docker" if request["target"] != "swss" else "none")
    if contract is not None:
        destination = artifact_dir / "container-inputs.json"
        native_action.copy_file(WORKSPACE / "image/container-inputs.json", destination)
        report["container_input_contract"] = {
            "path": str(destination), "sha256": native_action.digest_file(destination),
        }
    if retained:
        paths = query_paths(metadata_label, 1)
        destination = artifact_dir / paths[0].name
        native_action.copy_file(paths[0], destination)
        receipt_path = STATE / retained["receipt_path"]
        report["oci"] = {
            "rules_img_version": "0.3.22",
            "retained_receipt": str(receipt_path),
            "retained_receipt_sha256": native_action.digest_file(receipt_path),
            "overlay_metadata": {"path": str(destination), "sha256": native_action.digest_file(destination)},
        }
    elif owned:
        captured = oci_container.capture_retained_inputs(
            STATE, inv, owned, contract, {**archive_paths, inv["swss"]: produced_paths[inv["swss"]]}, {
                "kind": "successful-bazel-native-build",
                "buildimage_commit": request["source_commit"], "swss_commit": request["swss_commit"],
                "swss_source_digest": request["swss_digest"], "image_version": inv["image_version"],
                "source_manifest_sha256": report["source_manifest_sha256"],
                "profile": report["profile"], "execution_log": report["execution_log"],
            },
        )
        receipt_path = STATE / captured["receipt_path"]
        report["retained_for_oci"] = {
            "receipt": str(receipt_path), "sha256": native_action.digest_file(receipt_path),
        }
    write_json(artifact_dir / "build-manifest.json", report)
    print("Bazel artifacts: " + str(artifact_dir), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    request = load_json(args.request)
    if request.get("schema") != 1 or request.get("target") not in {"swss", "container", "vs", "vs-kvm"} or request.get("phase") not in {"prepare", "build"}:
        raise RuntimeError("invalid SONiC Bazel build request")
    if ROOT != Path("/sonic"):
        raise RuntimeError("driver.py must run in the public sonic-slave at /sonic")
    runtime_identity = native_action.load_native_snapshot_identity(ROOT)
    if runtime_identity != request.get("native_snapshot_identity"):
        raise RuntimeError("native snapshot identity does not match the launcher request")
    request["_native_environment"], request["_private_environment"], request["_native_make_variables"] = capture_environment()
    request["_native_make_variables"].update({key: str(value) for key, value in request["make_variables"].items()})
    image = "sonic-vs.img.gz" if request["target"] == "vs-kvm" else "sonic-vs.bin"
    inv = inventory(request, image)
    owned = selected_owned(request, inv)
    if request["phase"] == "prepare":
        prepare_native(request, inv, owned)
        print("Native prerequisites are ready for the fresh action slave.", flush=True)
        return
    artifacts = prepared_artifacts(request, inv, owned)
    prepare_action_environment(request, inv, owned, artifacts)
    prepare_swss(request, inv)
    manifest = prepare_environment(request, inv, artifacts)
    contract = container_input_contract(request, inv, owned, artifacts, manifest) if owned else None
    retained = oci_container.load_retained_inputs(STATE, inv, owned, contract) if owned else None
    if owned:
        print("Container construction: " + ("verified retained OCI layers" if retained else "native Docker baseline for the current inputs"), flush=True)
    generate_workspace(request, inv, owned, artifacts, manifest, retained, contract)
    build(request, inv, manifest, retained, contract)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print("SONiC Bazel: " + str(error), file=sys.stderr)
        sys.exit(1)
