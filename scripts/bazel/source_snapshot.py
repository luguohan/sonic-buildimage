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
import uuid

import native_action


MARKER = ".sonic-bazel-native-source"


def read_regular_json(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError("expected a regular native snapshot state file: " + str(path))
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise RuntimeError("native snapshot state must be a JSON object: " + str(path))
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot_marker_version(checkout):
    marker = checkout / MARKER
    try:
        info = marker.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("refusing to replace an unowned native source directory") from error
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("native source marker must be a regular file")
    text = marker.read_text()
    if text == "schema=1\n":
        return 1
    try:
        value = json.loads(text)
    except ValueError as error:
        raise RuntimeError("invalid native source marker") from error
    snapshot_id = value.get("snapshot_id") if isinstance(value, dict) else None
    valid_schema = isinstance(value, dict) and type(value.get("schema")) is int
    valid_id = isinstance(snapshot_id, str) and len(snapshot_id) == 32
    valid_id = valid_id and all(character in "0123456789abcdef" for character in snapshot_id)
    if not valid_schema or value != {"schema": 2, "snapshot_id": snapshot_id} or not valid_id:
        raise RuntimeError("invalid native source marker")
    return 2


def write_snapshot_marker(checkout):
    snapshot_id = uuid.uuid4().hex
    write_json(checkout / MARKER, {"schema": 2, "snapshot_id": snapshot_id})
    return snapshot_id


def snapshot_identity(checkout, snapshot_id):
    info = checkout.lstat()
    identity = {
        "schema": 1, "snapshot_id": snapshot_id,
        "device": info.st_dev, "inode": info.st_ino,
    }
    native_action.validate_native_snapshot(checkout, identity)
    return identity


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


def verify_isolation(checkout, identity):
    native_action.git_sources(
        checkout, exclude_swss=False, context="native", snapshot_identity=identity,
    )


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
    if checkout.is_symlink():
        raise RuntimeError("native source directory must not be a symlink")
    marker_version = snapshot_marker_version(checkout) if checkout.exists() else None
    previous = read_regular_json(receipt) if receipt.exists() or receipt.is_symlink() else {}
    identity = previous.get("snapshot_identity")
    if marker_version == 2:
        if previous.get("schema") != 2:
            raise RuntimeError("native snapshot ownership receipt is missing or unsupported")
        native_action.validate_native_snapshot(checkout, identity)
    elif marker_version == 1 and previous.get("schema") == 2:
        raise RuntimeError("native snapshot marker and ownership receipt disagree")
    if marker_version == 2 and previous.get("digest") == digest:
        root_identity = next(item for item in snapshot["repositories"] if item["path"] == ".")
        current = native_action.source_state(checkout, context="native", snapshot_identity=identity)
        current_root = next(item for item in current["repositories"] if item["path"] == ".")
        current_digest = native_action.digest_bytes(native_action.canonical_json(current))
        approved_path = checkout / "target/bazel/native-source-approved.json"
        approved = read_regular_json(approved_path) if approved_path.exists() or approved_path.is_symlink() else {}
        expected_state = current_digest == previous.get("raw_digest", digest) or (
            approved.get("schema") == 2 and approved.get("source_input_digest") == digest
            and approved.get("snapshot_id") == identity["snapshot_id"]
            and approved.get("prepared_digest") == current_digest
        )
        if expected_state and current_root["commit"] == root_identity["commit"] and current_root["branch"] == root_identity["branch"]:
            verify_isolation(checkout, identity)
            return checkout, identity
    assert_not_in_use(checkout)
    backup = state / "native-source.previous"
    if backup.exists():
        raise RuntimeError("a previous native snapshot replacement needs cleanup: " + str(backup))
    temporary_root = Path(tempfile.mkdtemp(prefix=".native-source-", dir=state))
    temporary = temporary_root / "checkout"
    try:
        print("Creating an isolated snapshot of the public SONiC sources.", flush=True)
        clone_repository(source, temporary, source, temporary)
        snapshot_id = write_snapshot_marker(temporary)
        exclude = temporary / ".git/info/exclude"
        with exclude.open("a") as stream:
            stream.write("\n/" + MARKER + "\n")
        temporary_identity = snapshot_identity(temporary, snapshot_id)
        verify_isolation(temporary, temporary_identity)
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
        raw = native_action.source_state(temporary, context="native", snapshot_identity=temporary_identity)
        raw_digest = native_action.digest_bytes(native_action.canonical_json(raw))
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
        identity = snapshot_identity(checkout, snapshot_id)
        write_json(receipt, {
            "schema": 2, "digest": digest, "raw_digest": raw_digest,
            "snapshot_identity": identity,
        })
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
    return checkout, identity
