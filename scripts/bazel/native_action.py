#!/usr/bin/env python3
"""Run a checked, serialized native SONiC stage on behalf of Bazel.

The native build needs mounts and a Docker daemon, so it runs locally in the
configured sonic-slave. A content manifest makes the mounted source tree an
explicit input; the launcher refreshes it before each Bazel invocation.
"""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


SCHEMA = 1
PRIVATE_KEYS = {
    "PASSWORD", "BMC_ROOT_ACCOUNT_DEFAULT_PASSWORD", "HTTP_PROXY",
    "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
}
PROCESS_KEYS = set("HOME USER LOGNAME SHELL PATH LANG LC_ALL TZ RUSTUP_HOME DOCKER_HOST DOCKER_BUILDKIT".split())
GENERATED_ROOT_PATHS = {
    "target", "fsroot-vs", "fsroot.docker.trixie",
    "fs.squashfs", "dockerfs.tar.gz", "fs.zip",
}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest_file(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def command_output(command, cwd=None, env=None):
    return subprocess.check_output(command, cwd=cwd, env=env, text=True, stderr=subprocess.STDOUT).strip()


def generated_root_path(name):
    """Recognize native output paths that are not source inputs."""
    return bool(Path(name).parts) and Path(name).parts[0] in GENERATED_ROOT_PATHS


def git_source_listing(repository, root_repository=False):
    """List source entries without traversing generated paths in the root repo."""
    command = ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    if root_repository:
        command.extend("--exclude=/" + name for name in sorted(GENERATED_ROOT_PATHS))
    return subprocess.check_output(command, cwd=repository)


def git_sources(root, exclude_swss=True):
    """Return source paths and repository identities, excluding SWSS inputs."""
    paths = set()
    repositories = []

    def visit(directory, prefix):
        identity = {
            "path": prefix or ".",
            "commit": command_output(["git", "rev-parse", "HEAD"], directory),
            "branch": command_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], directory),
        }
        repositories.append(identity)
        staged = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=directory)
        submodules = set()
        for record in staged.split(b"\0"):
            if not record:
                continue
            metadata, encoded = record.split(b"\t", 1)
            if metadata.startswith(b"160000 "):
                submodules.add(os.fsdecode(encoded))
        listed = git_source_listing(directory, root_repository=not prefix)
        for encoded in listed.split(b"\0"):
            if not encoded:
                continue
            name = os.fsdecode(encoded)
            relative = str(Path(prefix) / name) if prefix else name
            if not prefix and generated_root_path(name):
                continue
            if exclude_swss and (relative == "src/sonic-swss" or relative.startswith("src/sonic-swss/")):
                continue
            if name in submodules:
                child = directory / name
                if not (child / ".git").exists():
                    raise RuntimeError("uninitialized public submodule: " + relative)
                visit(child, relative)
            else:
                paths.add(relative)

    visit(root, "")
    return sorted(paths), sorted(repositories, key=lambda item: item["path"])


def source_entry(root, relative):
    path = root / relative
    try:
        path.parent.resolve(strict=False).relative_to(root.resolve())
    except ValueError as error:
        raise RuntimeError("source path traverses a symlink outside the checkout: " + relative) from error
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        destination = os.readlink(path)
        return {"kind": "symlink", "mode": mode, "target": destination}
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("unsupported source file type: " + relative)
    return {"kind": "file", "mode": mode, "size": info.st_size, "sha256": digest_file(path)}


def source_state(root):
    paths, repositories = git_sources(root)
    return {
        "repositories": repositories,
        "entries": {relative: source_entry(root, relative) for relative in paths},
    }


def tool_environment(slave_image_id, environment=None):
    commands = {
        "packages": ["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Abbrev}\n"],
        "gcc": ["gcc", "-dumpfullversion", "-dumpversion"],
        "gxx": ["g++", "-dumpfullversion", "-dumpversion"],
        "rustc": ["rustc", "--version", "--verbose"],
        "cargo": ["cargo", "--version"],
        "python": ["python3", "--version"],
        "python_packages": ["python3", "-m", "pip", "list", "--format=json", "--disable-pip-version-check"],
        "linker": ["ld", "--version"],
        "docker": ["docker", "version", "--format", "{{.Server.Version}}"],
    }
    result = {"slave_image_id": slave_image_id, "kernel": os.uname().release}
    for name, command in commands.items():
        result[name] = command_output(command, env=environment)
    result["packages"] = "\n".join(sorted(result["packages"].splitlines()))
    result["python_packages"] = sorted(json.loads(result["python_packages"]), key=lambda item: item["name"].lower())
    return result


def load_json(path):
    with path.open() as stream:
        return json.load(stream)


def native_root():
    value = os.environ.get("SONIC_BAZEL_NATIVE_ROOT", "")
    root = Path(value)
    if not value or not root.is_absolute() or not (root / "slave.mk").is_file():
        raise RuntimeError("SONIC_BAZEL_NATIVE_ROOT must name the configured public buildimage checkout")
    return root


def verify_manifest(manifest, root):
    if manifest.get("schema") != SCHEMA:
        raise RuntimeError("unsupported SONiC Bazel source manifest")
    expected_digest = manifest.get("digest")
    content = {key: value for key, value in manifest.items() if key != "digest"}
    if digest_bytes(canonical_json(content)) != expected_digest:
        raise RuntimeError("SONiC Bazel source manifest checksum is invalid")
    actual = source_state(root)
    if actual != manifest["source"]:
        expected_entries = manifest["source"]["entries"]
        actual_entries = actual["entries"]
        changed = sorted(
            name for name in set(expected_entries) | set(actual_entries)
            if expected_entries.get(name) != actual_entries.get(name)
        )
        detail = ", ".join(changed[:8]) or "repository revision or branch"
        raise RuntimeError("public source changed after preparation (" + detail + "); rerun scripts/bazel/run")
    private_path = Path(os.environ.get("SONIC_BAZEL_PRIVATE_ENV", ""))
    if not private_path.is_absolute() or digest_file(private_path) != manifest["private_environment_sha256"]:
        raise RuntimeError("private build environment changed; rerun scripts/bazel/run")
    environment = action_environment(manifest)
    tools = tool_environment(manifest["environment"]["slave_image_id"], environment)
    if tools != manifest["environment"]:
        raise RuntimeError("sonic-slave tools or installed packages changed; rerun scripts/bazel/run")
    if os.environ.get("SONIC_BAZEL_ENVIRONMENT_DIGEST") != manifest["environment_digest"]:
        raise RuntimeError("SONIC_BAZEL_ENVIRONMENT_DIGEST does not match the prepared environment")
    return environment


def parse_mapping(values):
    result = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in result:
            raise RuntimeError("invalid or duplicate action file mapping")
        logical = Path(name)
        if logical.is_absolute() or ".." in logical.parts:
            raise RuntimeError("action file mapping leaves the native checkout: " + name)
        result[name] = Path(path).absolute()
    return result


def native_path(root, logical):
    path = Path(logical)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError("native path leaves the checkout: " + logical)
    result = root / path
    try:
        result.parent.resolve(strict=False).relative_to(root.resolve())
    except ValueError as error:
        raise RuntimeError("native path traverses a symlink outside the checkout: " + logical) from error
    return result


def is_mountpoint(path):
    with open("/proc/self/mountinfo") as stream:
        for line in stream:
            mountpoint = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), line.split(" ", 5)[4])
            if mountpoint == str(path):
                return True
    return False


def cleanup_context_mounts(root, mappings, environment):
    for logical, source_logical in sorted(mappings.items()):
        path = native_path(root, logical)
        source = native_path(root, source_logical)
        count = 0
        while is_mountpoint(path):
            if count >= 16 or not path.samefile(source):
                raise RuntimeError("unexpected mount in native Docker context: " + logical)
            subprocess.run(["sudo", "umount", "--", str(path)], env=environment, check=True)
            count += 1


def cleanup_host_root(root, environment):
    subprocess.run(
        ["sudo", "python3", "scripts/bazel/native/host_snapshot.py", "remove", "fsroot-vs"],
        cwd=root, env=environment, check=True,
    )


def copy_file(source, destination):
    if not source.is_file():
        raise RuntimeError("required action input is missing: " + str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not destination.is_symlink():
        try:
            if source.samefile(destination):
                return
        except OSError:
            pass
        if source.stat().st_size == destination.stat().st_size and digest_file(source) == digest_file(destination):
            destination.chmod(stat.S_IMODE(source.stat().st_mode))
            return
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        with source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, stream, 1024 * 1024)
    try:
        temporary.chmod(stat.S_IMODE(source.stat().st_mode))
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def private_environment(path):
    values = load_json(path)
    if not isinstance(values, dict) or any(not isinstance(value, str) for value in values.values()):
        raise RuntimeError("private build environment values must be strings")
    return values


def action_environment(manifest):
    values = manifest.get("native_environment")
    if not isinstance(values, dict) or any(not isinstance(value, str) for value in values.values()):
        raise RuntimeError("native build environment values must be strings")
    result = dict(values)
    result.update(private_environment(Path(os.environ["SONIC_BAZEL_PRIVATE_ENV"])))
    for key in ("SONIC_BAZEL_NATIVE_ROOT", "SONIC_BAZEL_PRIVATE_ENV", "SONIC_BAZEL_ENVIRONMENT_DIGEST"):
        result[key] = os.environ[key]
    return result


def build(args):
    root = native_root()
    manifest = load_json(args.manifest)
    spec = load_json(args.spec)
    if spec.get("schema") != SCHEMA or spec.get("stage") not in {"container", "host", "image", "onie", "kvm"}:
        raise RuntimeError("invalid native stage specification")
    inputs = parse_mapping(args.input)
    outputs = parse_mapping(args.output)
    if set(outputs) != set(spec["outputs"]):
        raise RuntimeError("native stage output declaration does not match its specification")
    if not Path(spec["target"]).parts or Path(spec["target"]).parts[0] != "target":
        raise RuntimeError("native stage target must be under target/")
    native_path(root, spec["target"])
    lock_path = root / "target/bazel/native-actions.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        environment = verify_manifest(manifest, root)
        environment["PWD"] = str(root)
        for logical, source in inputs.items():
            copy_file(source, native_path(root, logical))
        for logical in spec["outputs"].values():
            path = native_path(root, logical)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_dir():
                raise RuntimeError("native output unexpectedly names a directory: " + logical)
            path.unlink(missing_ok=True)
        environment.update({key: str(value) for key, value in spec["environment"].items()})
        environment["SONIC_BAZEL_REQUESTED_STAGE"] = spec["stage"] if spec["stage"] != "container" else ""
        if spec.get("host_snapshot"):
            environment["SONIC_BAZEL_HOST_SNAPSHOT"] = str(native_path(root, spec["host_snapshot"]))
        command = ["make", "-f", "slave.mk", "-f", "bazel/native.mk", "--no-print-directory"]
        ignored = set(inputs) | set(spec.get("assume_old", []))
        for name in ignored:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("an assumed native input leaves the checkout")
        command.extend("--assume-old=" + name for name in sorted(ignored))
        command.extend(key + "=" + str(value) for key, value in sorted(spec["make_variables"].items()))
        command.append("SONIC_DPKG_CACHE_METHOD=none")
        command.append("SONIC_DPKG_CACHE_METHOD_OVERRIDE=none")
        command.append("SONIC_BUILD_TARGET=" + spec["target"])
        if spec["stage"] == "container":
            command.append(Path(spec["target"]).name + "_CACHE_MODE=none")
        command.append(spec["target"])
        print("Running native SONiC " + spec["stage"] + " stage for " + spec["target"], flush=True)
        try:
            subprocess.run(command, cwd=root, env=environment, check=True)
        finally:
            cleanup_context_mounts(root, spec.get("context_mounts", {}), environment)
            if spec["stage"] in {"host", "image"}:
                cleanup_host_root(root, environment)
        verify_manifest(manifest, root)
        for declared, native in spec["outputs"].items():
            copy_file(native_path(root, native), outputs[declared])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("build",))
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--output", action="append", default=[])
    args = parser.parse_args()
    build(args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        print("SONiC Bazel: " + str(error), file=sys.stderr)
        sys.exit(1)
