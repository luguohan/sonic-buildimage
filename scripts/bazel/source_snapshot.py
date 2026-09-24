#!/usr/bin/env python3
"""Create an independent local checkout for native SONiC build recipes.

Native recipes may change submodule branches while applying their public patch
series.  This snapshot keeps those operations away from the caller's checkout.
Only uninitialized submodules are initialized in the caller's source tree.
"""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit

import native_action


MARKER = ".sonic-bazel-native-source"


def output(command, cwd):
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def run(command, cwd):
    subprocess.run(command, cwd=cwd, check=True)


def gitlinks(repository):
    records = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=repository)
    result = []
    for record in records.split(b"\0"):
        if not record:
            continue
        metadata, encoded = record.split(b"\t", 1)
        mode, _object, stage = metadata.split()
        if stage != b"0":
            raise RuntimeError("resolve the source checkout's Git conflicts before building")
        if mode == b"160000":
            name = os.fsdecode(encoded)
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("invalid submodule path in the source checkout")
            result.append(name)
    return sorted(result)


def ensure_submodules(repository):
    """Initialize missing submodules without moving any existing checkout."""
    repository = repository.resolve()
    for name in gitlinks(repository):
        child = repository / name
        if child.is_symlink():
            raise RuntimeError("a submodule path is a symlink: " + str(child))
        if not (child / ".git").exists():
            if child.exists() and (not child.is_dir() or any(child.iterdir())):
                raise RuntimeError("an uninitialized submodule contains local files: " + str(child))
            run(["git", "submodule", "update", "--init", "--", name], repository)
        top = Path(output(["git", "rev-parse", "--show-toplevel"], child)).resolve()
        if top != child.resolve():
            raise RuntimeError("invalid initialized submodule checkout: " + str(child))
        ensure_submodules(child)


def public_origin(repository):
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"], cwd=repository,
        text=True, stdout=subprocess.PIPE, check=False,
    )
    if result.returncode:
        return None
    value = result.stdout.strip()
    if value.startswith("git@github.com:"):
        path = value.split(":", 1)[1]
    else:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "ssh"} or parsed.hostname != "github.com":
            raise RuntimeError("the native snapshot requires public GitHub source origins")
        path = parsed.path.lstrip("/")
    parts = Path(path).parts
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError("invalid public GitHub source origin")
    return "https://github.com/" + "/".join(parts)


def copy_source_file(source, destination, source_root, destination_root):
    try:
        source.parent.resolve(strict=False).relative_to(source_root.resolve())
    except ValueError as error:
        raise RuntimeError("a source path traverses a symlink outside its checkout") from error
    try:
        destination.parent.resolve(strict=False).relative_to(destination_root.resolve())
    except ValueError as error:
        raise RuntimeError("a source path traverses a symlink outside the native snapshot") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode):
        link = os.readlink(source)
        if Path(link).is_absolute():
            try:
                relative = Path(os.path.normpath(link)).relative_to(source_root)
            except ValueError:
                pass
            else:
                link = os.path.relpath(destination_root / relative, destination.parent)
        destination.symlink_to(link)
    elif stat.S_ISREG(info.st_mode):
        shutil.copyfile(source, destination)
        destination.chmod(stat.S_IMODE(info.st_mode))
    else:
        raise RuntimeError("unsupported source file type: " + str(source))


def expected_staged_entry(entry, relative, source_root, destination_root):
    result = dict(entry)
    if result["kind"] == "symlink" and Path(result["target"]).is_absolute():
        try:
            destination_relative = Path(os.path.normpath(result["target"])).relative_to(source_root)
        except ValueError:
            pass
        else:
            result["target"] = os.path.relpath(destination_root / destination_relative, (destination_root / relative).parent)
    return result


def clone_repository(source, destination, source_root, destination_root):
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--quiet", "--no-local", "--no-checkout", str(source), str(destination)], source_root)
    commit = output(["git", "rev-parse", "HEAD"], source)
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"], cwd=source,
        text=True, stdout=subprocess.PIPE, check=False,
    ).stdout.strip()
    if branch:
        run(["git", "update-ref", branch, commit], destination)
        run(["git", "symbolic-ref", "HEAD", branch], destination)
    else:
        run(["git", "update-ref", "--no-deref", "HEAD", commit], destination)
    run(["git", "reset", "--mixed", "--quiet", "HEAD"], destination)
    origin = public_origin(source)
    if origin:
        run(["git", "remote", "set-url", "origin", origin], destination)
    else:
        run(["git", "remote", "remove", "origin"], destination)
    excluded = gitlinks(source)
    encoded = native_action.git_source_listing(source, root_repository=source == source_root)
    for name in sorted({os.fsdecode(item) for item in encoded.split(b"\0") if item} - set(excluded)):
        if source == source_root and native_action.generated_root_path(name):
            continue
        path = source / name
        if path.exists() or path.is_symlink():
            copy_source_file(path, destination / name, source_root, destination_root)
    for name in excluded:
        clone_repository(source / name, destination / name, source_root, destination_root)


def verify_isolation(checkout):
    _paths, repositories = native_action.git_sources(checkout, exclude_swss=False)
    for identity in repositories:
        repository = checkout if identity["path"] == "." else checkout / identity["path"]
        common = Path(output(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], repository)).resolve()
        try:
            common.relative_to(checkout.resolve())
        except ValueError as error:
            raise RuntimeError("native snapshot shares Git state with another checkout") from error
        if (common / "objects/info/alternates").exists():
            raise RuntimeError("native snapshot has an external Git object store")


def assert_not_in_use(checkout):
    if not checkout.exists():
        return
    result = output(["docker", "ps", "--quiet"], checkout)
    if not result:
        return
    containers = json.loads(output(["docker", "inspect"] + result.splitlines(), checkout))
    for container in containers:
        for mount in container.get("Mounts", []):
            source = Path(mount.get("Source", "/")).resolve()
            if source == checkout.resolve() or checkout.resolve() in source.parents:
                raise RuntimeError("the native snapshot is still mounted by a running container")


def stage_checkout(source, state):
    source = source.resolve()
    state.mkdir(parents=True, exist_ok=True)
    checkout = state / "native-source"
    receipt = state / "native-source-input.json"
    snapshot = native_action.source_state(source)
    digest = native_action.digest_bytes(native_action.canonical_json(snapshot))
    if checkout.exists() and not (checkout / MARKER).is_file():
        raise RuntimeError("refusing to replace an unowned native source directory")
    if checkout.exists() and receipt.exists():
        previous = json.loads(receipt.read_text())
        if previous.get("schema") == 1 and previous.get("digest") == digest:
            root_identity = next(item for item in snapshot["repositories"] if item["path"] == ".")
            current_commit = output(["git", "rev-parse", "HEAD"], checkout)
            current_branch = output(["git", "rev-parse", "--abbrev-ref", "HEAD"], checkout)
            current_digest = native_action.digest_bytes(native_action.canonical_json(native_action.source_state(checkout)))
            approved_path = checkout / "target/bazel/native-source-approved.json"
            approved = json.loads(approved_path.read_text()) if approved_path.is_file() else {}
            expected_state = current_digest == previous.get("raw_digest", digest) or (
                approved.get("schema") == 1 and approved.get("source_input_digest") == digest
                and approved.get("prepared_digest") == current_digest
            )
            if expected_state and current_commit == root_identity["commit"] and current_branch == root_identity["branch"]:
                verify_isolation(checkout)
                return checkout
    assert_not_in_use(checkout)
    backup = state / "native-source.previous"
    if backup.exists():
        raise RuntimeError("a previous native snapshot replacement needs cleanup: " + str(backup))
    temporary_root = Path(tempfile.mkdtemp(prefix=".native-source-", dir=state))
    temporary = temporary_root / "checkout"
    try:
        print("Creating an isolated snapshot of the public SONiC sources.", flush=True)
        clone_repository(source, temporary, source, temporary)
        (temporary / MARKER).write_text("schema=1\n")
        exclude = temporary / ".git/info/exclude"
        with exclude.open("a") as stream:
            stream.write("\n/" + MARKER + "\n")
        verify_isolation(temporary)
        if native_action.source_state(source) != snapshot:
            raise RuntimeError("public sources changed while staging; rerun scripts/bazel/run")
        staged = native_action.source_state(temporary)
        expected = json.loads(json.dumps(snapshot))
        expected["entries"] = {
            relative: expected_staged_entry(entry, relative, source, temporary)
            for relative, entry in expected["entries"].items()
        }
        if staged != expected:
            raise RuntimeError("the staged public source bytes do not match the selected checkout")
        if checkout.exists():
            checkout.rename(backup)
        preserved = []
        try:
            # These native work directories may contain root-owned files. Keep
            # them at the same final path; the next slave invocation cleans its
            # Docker and host scratch roots through the native build machinery.
            for name in ("target", "fsroot-vs", "fsroot.docker.trixie"):
                if (backup / name).exists():
                    (backup / name).rename(temporary / name)
                    preserved.append(name)
            temporary.rename(checkout)
        except BaseException:
            if backup.exists():
                for name in preserved:
                    if (temporary / name).exists():
                        (temporary / name).rename(backup / name)
                backup.rename(checkout)
            raise
        raw_digest = native_action.digest_bytes(native_action.canonical_json(staged))
        receipt.write_text(json.dumps({"schema": 1, "digest": digest, "raw_digest": raw_digest}, indent=2) + "\n")
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
    return checkout
