#!/usr/bin/env python3
"""Reuse retained SONiC OCI layers for compatible SWSS package updates."""

import argparse
from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile


SCHEMA = 1
RECEIPT_SCHEMA = 2
CHUNK = 1024 * 1024
MAX_JSON = 16 * 1024 * 1024
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar"
OCI_GZIP_LAYER = OCI_LAYER + "+gzip"
LAYER_TYPES = {OCI_LAYER, OCI_GZIP_LAYER}
SHA256 = re.compile(r"[0-9a-f]{64}")
BLOB = re.compile(r"blobs/sha256/([0-9a-f]{64})")
INFO_PREFIX = "var/lib/dpkg/info/swss."
INFO_PATHS = {INFO_PREFIX + name for name in ("md5sums", "conffiles", "list")}
STATUS_PATH = "var/lib/dpkg/status"
CONTRACT_SOURCE_EXCLUDES = {
    "bazel/README.md",
    "bazel/oci_defs.bzl",
    "scripts/bazel/driver.py",
    "scripts/bazel/oci_container.py",
}
CONTRACT_MAKE_EXCLUDES = {"BUILD_TIMESTAMP", "SONIC_IMAGE_VERSION", "SOURCE_DATE_EPOCH"}
CONTRACT_ENVIRONMENT_EXCLUDES = {"SONIC_BAZEL_SOURCE_COMMIT", "SOURCE_DATE_EPOCH"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def contract_bytes(contract):
    """Return the one canonical encoding used to key retained container inputs."""
    require(isinstance(contract, dict), "container input contract must be a JSON object")
    return canonical_json(contract) + b"\n"


def logical_input_path(name):
    require(isinstance(name, str) and name and "\x00" not in name and "\\" not in name,
            "invalid logical container input path")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts and str(path) == name and name != ".",
            "logical container input path leaves its root: " + name)
    return name


def container_input_contract(manifest, specs, static_inputs, dynamic_inputs, owned_dependencies):
    """Normalize exactly the native inputs frozen by a retained OCI baseline."""
    require(isinstance(manifest, dict) and manifest.get("schema") == SCHEMA,
            "invalid native source manifest for container inputs")
    normalized_manifest = json.loads(canonical_json(manifest))
    normalized_manifest.pop("digest", None)
    normalized_manifest.pop("environment_digest", None)
    source = normalized_manifest.get("source")
    require(isinstance(source, dict) and isinstance(source.get("entries"), dict) and
            isinstance(source.get("repositories"), list), "invalid native source inventory for container inputs")
    for name in source["entries"]:
        logical_input_path(name)
    source["entries"] = {
        name: entry for name, entry in source["entries"].items()
        if name not in CONTRACT_SOURCE_EXCLUDES and not name.startswith("scripts/bazel/tests/")
    }
    require(all(isinstance(repository, dict) and isinstance(repository.get("path"), str)
                for repository in source["repositories"]), "invalid native repository inventory for container inputs")
    roots = [repository for repository in source["repositories"] if repository["path"] == "."]
    require(len(roots) == 1 and "commit" in roots[0], "native container inputs require one root repository revision")
    roots[0].pop("commit")
    variables = normalized_manifest.get("native_make_variables")
    require(isinstance(variables, dict), "invalid native make variables for container inputs")
    for name in CONTRACT_MAKE_EXCLUDES:
        variables.pop(name, None)

    require(isinstance(specs, dict) and specs, "invalid native container specifications")
    normalized_specs = json.loads(canonical_json(specs))
    for name, spec in normalized_specs.items():
        filename(name, ".gz")
        require(isinstance(spec, dict) and spec.get("schema") == SCHEMA and spec.get("stage") == "container" and
                spec.get("target") == "target/" + name and isinstance(spec.get("make_variables"), dict) and
                isinstance(spec.get("environment"), dict), "invalid native container specification: " + name)
        for key in CONTRACT_MAKE_EXCLUDES:
            spec["make_variables"].pop(key, None)
        for key in CONTRACT_ENVIRONMENT_EXCLUDES:
            spec["environment"].pop(key, None)

    require(isinstance(static_inputs, dict), "invalid static container input map")
    normalized_static = {}
    for name, value in static_inputs.items():
        logical_input_path(name)
        require(isinstance(value, str) and SHA256.fullmatch(value) is not None,
                "invalid static container input SHA-256: " + name)
        normalized_static[name] = value
    require(isinstance(dynamic_inputs, list) and all(isinstance(name, str) for name in dynamic_inputs) and
            len(dynamic_inputs) == len(set(dynamic_inputs)), "invalid dynamic SWSS input paths")
    normalized_dynamic = sorted(logical_input_path(name) for name in dynamic_inputs)
    require(not set(normalized_static) & set(normalized_dynamic), "static and dynamic container inputs overlap")
    require(isinstance(owned_dependencies, dict) and set(owned_dependencies) == set(normalized_specs),
            "owned container dependency map differs from specifications")
    owned_paths = {"target/" + name for name in normalized_specs}
    require(not set(normalized_static) & owned_paths, "static and owned container inputs overlap")
    normalized_dependencies = {}
    for name, dependencies in owned_dependencies.items():
        require(isinstance(dependencies, list) and all(isinstance(value, str) for value in dependencies) and
                len(dependencies) == len(set(dependencies)), "invalid owned container dependencies: " + name)
        values = sorted(logical_input_path(value) for value in dependencies)
        require(set(values) <= owned_paths and "target/" + name not in values,
                "owned container dependency leaves the selected set: " + name)
        normalized_dependencies[name] = values
    return {
        "schema": SCHEMA,
        "manifest": normalized_manifest,
        "specs": normalized_specs,
        "static_inputs": normalized_static,
        "dynamic_inputs": normalized_dynamic,
        "owned_dependencies": normalized_dependencies,
    }


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), "input is not a regular file: " + str(path))
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
            "input changed while reading: " + str(path))
    return digest.hexdigest(), before.st_size


def safe_path(name):
    require(isinstance(name, str) and "\x00" not in name and "\\" not in name, "invalid archive path")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts, "archive path leaves its root: " + name)
    result = str(path)
    require(result not in ("", "/"), "empty archive path")
    return result


def filename(name, suffix):
    require(isinstance(name, str) and name == Path(name).name and name.endswith(suffix) and
            name not in (".", ".."), "invalid retained filename")
    return name


def read_json(path):
    data = Path(path).read_bytes()
    require(len(data) <= MAX_JSON, "JSON input is too large: " + str(path))
    return json.loads(data)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(canonical_json(value) + b"\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def file_identity(path):
    value = Path(path).lstat()
    require(stat.S_ISREG(value.st_mode), "retained input must be a regular non-symlink file")
    return {"device": value.st_dev, "inode": value.st_ino, "mode": stat.S_IMODE(value.st_mode),
            "mtime_ns": value.st_mtime_ns, "size": value.st_size}


def copy_retained(source, destination):
    descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as input_stream:
        before = os.fstat(input_stream.fileno())
        require(stat.S_ISREG(before.st_mode), "retained source is not a regular file: " + str(source))
        digest = hashlib.sha256()
        with Path(destination).open("xb") as output:
            for chunk in iter(lambda: input_stream.read(CHUNK), b""):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(input_stream.fileno())
    current = Path(source).lstat()
    fields = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    require(fields(before) == fields(after) == fields(current) and stat.S_ISREG(current.st_mode),
            "retained source changed while copying: " + str(source))
    Path(destination).chmod(0o444)
    return {"sha256": digest.hexdigest(), "size": before.st_size,
            "file_identity": file_identity(destination)}


def _static_stat(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _static_directory_identity(value):
    require(stat.S_ISDIR(value.st_mode), "container static input directory is not a regular directory")
    return value.st_dev, value.st_ino, value.st_mode


@contextmanager
def _static_parent(root, logical):
    """Open non-symlink parents and keep their pathname identities stable."""
    logical_input_path(logical)
    parts = PurePosixPath(logical).parts
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    opened = []
    try:
        before = Path(root).lstat()
        expected = _static_directory_identity(before)
        descriptor = os.open(root, flags)
        opened.append((None, Path(root), descriptor, expected))
        require(_static_directory_identity(os.fstat(descriptor)) == expected,
                "container static input root changed while opening")
        for part in parts[:-1]:
            before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            expected = _static_directory_identity(before)
            child = os.open(part, flags, dir_fd=descriptor)
            opened.append((descriptor, part, child, expected))
            require(_static_directory_identity(os.fstat(child)) == expected,
                    "container static input directory changed while opening")
            descriptor = child
        yield descriptor, parts[-1]
        for parent, name, descriptor, expected in reversed(opened):
            current = Path(name).lstat() if parent is None else os.stat(name, dir_fd=parent, follow_symlinks=False)
            require(_static_directory_identity(current) == _static_directory_identity(os.fstat(descriptor)) == expected,
                    "container static input directory identity changed")
    except OSError as error:
        raise RuntimeError("container static input path cannot be accessed safely: " + str(Path(root) / logical)) from error
    finally:
        for _parent, _name, descriptor, _expected in reversed(opened):
            os.close(descriptor)


def _static_file(root, logical, destination=None, expected_mode=None, single_link=False, capture=False):
    """Read or copy one stable regular file without following pathname symlinks."""
    with _static_parent(root, logical) as (parent, name):
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        require(stat.S_ISREG(before.st_mode), "container static input must be a regular non-symlink file: " + logical)
        mode = stat.S_IMODE(before.st_mode)
        require(expected_mode is None or mode == expected_mode, "container static input mode changed: " + logical)
        require(not single_link or before.st_nlink == 1, "container static input has multiple hardlinks: " + logical)
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        digest = hashlib.sha256()
        data = bytearray() if capture else None
        count = 0
        with os.fdopen(descriptor, "rb") as source:
            require(_static_stat(os.fstat(source.fileno())) == _static_stat(before),
                    "container static input changed while opening: " + logical)
            output = Path(destination).open("xb") if destination is not None else None
            try:
                for chunk in iter(lambda: source.read(CHUNK), b""):
                    count += len(chunk)
                    digest.update(chunk)
                    if data is not None:
                        require(count <= MAX_JSON, "container static input receipt is too large")
                        data.extend(chunk)
                    if output is not None:
                        output.write(chunk)
                if output is not None:
                    output.flush()
                    os.fchmod(output.fileno(), 0o444)
                    os.fsync(output.fileno())
                    copied = os.fstat(output.fileno())
                    require(stat.S_ISREG(copied.st_mode) and copied.st_nlink == 1 and
                            (copied.st_dev, copied.st_ino) != (before.st_dev, before.st_ino),
                            "container static input copy is not a separate regular file")
            finally:
                if output is not None:
                    output.close()
            after = os.fstat(source.fileno())
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        require(count == before.st_size and _static_stat(before) == _static_stat(after) == _static_stat(current),
                "container static input changed while reading: " + logical)
    return ({"sha256": digest.hexdigest(), "size": count, "mode": mode},
            bytes(data) if data is not None else None, (before.st_dev, before.st_ino))


def _static_directory_snapshot(state, relative, read_only=False):
    with _static_parent(state, relative + "/.inventory") as (descriptor, _name):
        before = os.fstat(descriptor)
        require(not read_only or stat.S_IMODE(before.st_mode) == 0o555,
                "container static input snapshot directory is not read-only")
        entries = sorted(os.listdir(descriptor))
        require(_static_stat(os.fstat(descriptor)) == _static_stat(before),
                "container static input directory changed while listing")
    return _static_stat(before), entries


@contextmanager
def _static_publication_lock(state):
    with _static_parent(state, "container-static-inputs/.lock") as (parent, name):
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size == 0 and
                    stat.S_IMODE(before.st_mode) == 0o600 and
                    _static_stat(before) == _static_stat(os.stat(name, dir_fd=parent, follow_symlinks=False)),
                    "container static input publication lock is invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            require(_static_stat(before) == _static_stat(os.fstat(descriptor)) ==
                    _static_stat(os.stat(name, dir_fd=parent, follow_symlinks=False)),
                    "container static input publication lock changed while waiting")
            yield
            require(_static_stat(before) == _static_stat(os.fstat(descriptor)) ==
                    _static_stat(os.stat(name, dir_fd=parent, follow_symlinks=False)),
                    "container static input publication lock changed")
        finally:
            os.close(descriptor)


def _load_container_static_inputs(state, relative, receipt, source_identities):
    directory = state / relative
    try:
        current = directory.lstat()
    except FileNotFoundError as error:
        raise RuntimeError("container static input snapshot disappeared") from error
    require(stat.S_ISDIR(current.st_mode), "container static input snapshot path is not a regular directory")
    directory_before = _static_directory_snapshot(state, relative, read_only=True)
    require(directory_before[0] == _static_stat(current), "container static input snapshot changed while opening")
    require(directory_before[1] == ["files", "receipt.json"], "container static input snapshot has unexpected entries")
    files_before = _static_directory_snapshot(state, relative + "/files", read_only=True)
    expected_files = sorted(PurePosixPath(record["path"]).name for record in receipt["inputs"].values())
    require(files_before[1] == expected_files, "container static input files have unexpected entries")
    receipt_data = canonical_json(receipt) + b"\n"
    _record, actual_data, _identity = _static_file(
        state, receipt["receipt_path"], expected_mode=0o444, single_link=True, capture=True)
    require(actual_data == receipt_data, "container static input receipt is malformed or corrupt")
    for logical, expected in sorted(receipt["inputs"].items()):
        actual, _data, identity = _static_file(state, expected["path"], expected_mode=0o444, single_link=True)
        require(actual["sha256"] == expected["sha256"] and actual["size"] == expected["size"],
                "container static input snapshot bytes are corrupt: " + logical)
        require(identity != source_identities[logical], "container static input snapshot aliases its current source: " + logical)
    require(_static_directory_snapshot(state, relative, read_only=True) == directory_before and
            _static_directory_snapshot(state, relative + "/files", read_only=True) == files_before,
            "container static input snapshot directory identity changed")
    return {**receipt, "receipt_sha256": sha256(receipt_data)}


def freeze_container_static_inputs(state, root, artifacts):
    """Freeze prepared target files into one exact, immutable container input set.

    The returned receipt adds receipt_sha256 to the persisted canonical fields.
    Input modes describe the sources; input paths address read-only copies under state.
    """
    state, root = Path(os.path.abspath(state)), Path(os.path.abspath(root))
    require(isinstance(artifacts, dict) and all(isinstance(name, str) for name in artifacts),
            "invalid prepared container static input map")
    prepared = {}
    for logical, record in sorted(artifacts.items()):
        logical_input_path(logical)
        require(logical.startswith("target/"), "container static input is outside target: " + logical)
        require(isinstance(record, dict) and set(record) == {"sha256", "size"} and
                isinstance(record["sha256"], str) and SHA256.fullmatch(record["sha256"]) is not None and
                type(record["size"]) is int and record["size"] >= 0,
                "invalid prepared container static input receipt: " + logical)
        require(not (root / logical).is_relative_to(state / "container-static-inputs"),
                "container static input is inside its snapshot namespace: " + logical)
        prepared[logical] = {"sha256": record["sha256"], "size": record["size"]}
    require(not any(str(parent) in prepared for logical in prepared for parent in PurePosixPath(logical).parents),
            "container static input file and directory paths overlap")
    _static_directory_identity(root.lstat())
    state.mkdir(parents=True, exist_ok=True)
    _static_directory_identity(state.lstat())
    base = state / "container-static-inputs"
    base.mkdir(exist_ok=True)
    _static_directory_identity(base.lstat())
    with _static_publication_lock(state):
        _identity, namespace_entries = _static_directory_snapshot(state, "container-static-inputs")
        require(all(name == ".lock" or SHA256.fullmatch(name) is not None for name in namespace_entries),
                "container static input namespace has unexpected entries")
        for name in namespace_entries:
            if name != ".lock":
                require(stat.S_ISDIR((base / name).lstat().st_mode),
                        "container static input namespace contains an invalid snapshot")
        identity_inputs = {}
        for logical, expected in prepared.items():
            with _static_parent(root, logical) as (parent, name):
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                require(stat.S_ISREG(current.st_mode),
                        "container static input must be a regular non-symlink file: " + logical)
                identity_inputs[logical] = {**expected, "mode": stat.S_IMODE(current.st_mode)}
        identity_sha256 = sha256(canonical_json(identity_inputs))
        relative = "container-static-inputs/" + identity_sha256
        inputs = {logical: {**record, "path": relative + "/files/" + sha256(logical.encode())}
                  for logical, record in identity_inputs.items()}
        require(len({record["path"] for record in inputs.values()}) == len(inputs),
                "container static input storage paths collide")
        receipt = {"schema": SCHEMA, "kind": "sonic-container-static-inputs", "identity_sha256": identity_sha256,
                   "receipt_path": relative + "/receipt.json", "inputs": inputs}
        require(len(canonical_json(receipt) + b"\n") <= MAX_JSON, "container static input receipt is too large")
        directory = state / relative
        existing = directory.exists() or directory.is_symlink()
        temporary = None if existing else Path(tempfile.mkdtemp(prefix=".pending-", dir=base))
        temporary_identity = None if temporary is None else _static_directory_identity(temporary.lstat())[:2]
        try:
            if temporary is not None:
                (temporary / "files").mkdir()
            source_identities = {}
            for logical, expected in inputs.items():
                destination = temporary / "files" / PurePosixPath(expected["path"]).name if temporary is not None else None
                actual, _data, source_identities[logical] = _static_file(
                    root, logical, destination=destination, expected_mode=expected["mode"])
                require(actual == {key: expected[key] for key in ("sha256", "size", "mode")},
                        "container static input differs from prepared receipt: " + logical)
            if existing:
                return _load_container_static_inputs(state, relative, receipt, source_identities)
            receipt_data = canonical_json(receipt) + b"\n"
            with (temporary / "receipt.json").open("xb") as stream:
                stream.write(receipt_data)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
            (temporary / "files").chmod(0o555)
            temporary.chmod(0o555)
            require(not directory.exists() and not directory.is_symlink(),
                    "container static input snapshot appeared during capture")
            temporary.rename(directory)
            return _load_container_static_inputs(state, relative, receipt, source_identities)
        finally:
            if temporary is not None:
                try:
                    current = temporary.lstat()
                except FileNotFoundError:
                    pass
                else:
                    require(_static_directory_identity(current)[:2] == temporary_identity,
                            "container static input temporary directory identity changed")
                    temporary.chmod(0o755)
                    files = temporary / "files"
                    if files.is_dir() and not files.is_symlink():
                        files.chmod(0o755)
                    shutil.rmtree(temporary)


class DigestReader:
    def __init__(self, source):
        self.source = source
        self.digest = hashlib.sha256()
        self.count = 0

    def read(self, size=-1):
        data = self.source.read(size)
        self.digest.update(data)
        self.count += len(data)
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def readable(self):
        return True

    def seekable(self):
        return False

    def tell(self):
        return self.count


def consume(stream, capture=False, output=None):
    digest = hashlib.sha256()
    data = bytearray() if capture else None
    size = 0
    for chunk in iter(lambda: stream.read(CHUNK), b""):
        size += len(chunk)
        digest.update(chunk)
        if data is not None:
            data.extend(chunk)
        if output is not None:
            output.write(chunk)
    return {"sha256": digest.hexdigest(), "size": size}, bytes(data) if data is not None else None


def archive_catalog(path, layout=None, expected_sha256=None):
    """Read one retained dual-format archive and optionally copy its OCI files."""
    members, json_data, seen = {}, {}, set()
    with Path(path).open("rb") as raw:
        compressed = DigestReader(raw)
        with gzip.GzipFile(fileobj=compressed, mode="rb") as decoded:
            with tarfile.open(fileobj=decoded, mode="r|") as archive:
                for member in archive:
                    name = safe_path(member.name)
                    require(name not in seen, "duplicate outer archive path: " + name)
                    seen.add(name)
                    if member.isdir():
                        require(name in (".", "blobs", "blobs/sha256"), "unexpected outer archive directory: " + name)
                        continue
                    require(member.isfile() and member.sparse is None, "unsupported outer archive member: " + name)
                    match = BLOB.fullmatch(name)
                    require(match is not None or name in ("oci-layout", "index.json", "manifest.json", "repositories"),
                            "unexpected outer archive file: " + name)
                    capture = member.size <= MAX_JSON
                    destination = None
                    if layout is not None and (match is not None or name in ("oci-layout", "index.json")):
                        destination = Path(layout) / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                    extracted = archive.extractfile(member)
                    require(extracted is not None, "missing outer archive file data")
                    if destination is None:
                        info, data = consume(extracted, capture)
                    else:
                        with destination.open("xb") as output:
                            info, data = consume(extracted, capture, output)
                    require(info["size"] == member.size, "truncated outer archive member: " + name)
                    if match is not None:
                        require(info["sha256"] == match.group(1), "OCI blob digest mismatch: " + name)
                    members[name] = info
                    if data is not None and data[:1] in (b"{", b"["):
                        json_data[name] = data
            consume(decoded)
        consume(compressed)
    actual = compressed.digest.hexdigest()
    if expected_sha256 is not None:
        require(SHA256.fullmatch(expected_sha256) is not None and actual == expected_sha256,
                "retained archive SHA-256 mismatch")
    return {"members": members, "json": json_data, "sha256": actual}


def catalog_json(catalog, name):
    require(name in catalog["json"], "missing or oversized JSON member: " + name)
    return json.loads(catalog["json"][name])


def descriptor_path(catalog, descriptor, media_types):
    require(isinstance(descriptor, dict) and descriptor.get("mediaType") in media_types,
            "unsupported OCI descriptor media type")
    digest = descriptor.get("digest", "")
    require(isinstance(digest, str) and digest.startswith("sha256:") and SHA256.fullmatch(digest[7:]) is not None,
            "invalid OCI descriptor digest")
    require(type(descriptor.get("size")) is int and descriptor["size"] >= 0, "invalid OCI descriptor size")
    path = "blobs/sha256/" + digest[7:]
    require(catalog["members"].get(path) == {"sha256": digest[7:], "size": descriptor["size"]},
            "OCI descriptor does not match its blob")
    return path


def validate_catalog(catalog, image_name):
    filename(image_name + ".gz", ".gz")
    require(":" not in image_name, "image name must not contain a tag")
    require(catalog_json(catalog, "oci-layout") == {"imageLayoutVersion": "1.0.0"}, "invalid OCI layout marker")
    index = catalog_json(catalog, "index.json")
    require(isinstance(index, dict) and index.get("schemaVersion") == 2 and index.get("mediaType") == OCI_INDEX and
            isinstance(index.get("manifests"), list) and len(index["manifests"]) == 1, "expected one OCI image manifest")
    root_descriptor = index["manifests"][0]
    manifest_path = descriptor_path(catalog, root_descriptor, {OCI_MANIFEST})
    manifest = catalog_json(catalog, manifest_path)
    require(isinstance(manifest, dict) and manifest.get("schemaVersion") == 2 and manifest.get("mediaType") == OCI_MANIFEST,
            "invalid OCI image manifest")
    config_path = descriptor_path(catalog, manifest.get("config"), {OCI_CONFIG})
    config = catalog_json(catalog, config_path)
    require(isinstance(config, dict) and config.get("architecture") == "amd64" and config.get("os") == "linux" and
            not config.get("variant"), "retained image must be linux/amd64")
    platform = root_descriptor.get("platform")
    if platform is not None:
        require(isinstance(platform, dict) and platform.get("architecture") == "amd64" and platform.get("os") == "linux" and
                not platform.get("variant"), "OCI index platform differs from image config")
    layers = manifest.get("layers")
    require(isinstance(layers, list) and layers, "OCI image has no layers")
    layer_paths = [descriptor_path(catalog, value, LAYER_TYPES) for value in layers]
    rootfs = config.get("rootfs")
    require(isinstance(rootfs, dict) and rootfs.get("type") == "layers" and isinstance(rootfs.get("diff_ids"), list) and
            len(rootfs["diff_ids"]) == len(layers), "OCI rootfs layer count mismatch")
    diff_ids = rootfs["diff_ids"]
    require(all(isinstance(value, str) and value.startswith("sha256:") and SHA256.fullmatch(value[7:]) is not None
                for value in diff_ids), "invalid OCI rootfs DiffID")
    history = config.get("history", [])
    require(isinstance(history, list) and all(isinstance(value, dict) for value in history) and
            sum(not value.get("empty_layer", False) for value in history) == len(layers), "OCI history layer count mismatch")
    saved = catalog_json(catalog, "manifest.json")
    require(isinstance(saved, list) and len(saved) == 1 and isinstance(saved[0], dict), "expected one Docker saved image")
    saved = saved[0]
    require(safe_path(saved.get("Config", "")) == config_path and
            isinstance(saved.get("Layers"), list) and [safe_path(value) for value in saved["Layers"]] == layer_paths,
            "Docker saved image differs from OCI descriptors")
    expected_tag = image_name + ":latest"
    require(saved.get("RepoTags") == [expected_tag], "Docker saved image tag differs from requested image")
    annotations = root_descriptor.get("annotations", {})
    require(isinstance(annotations, dict), "invalid OCI image annotations")
    if "org.opencontainers.image.ref.name" in annotations:
        require(annotations["org.opencontainers.image.ref.name"] == "latest", "OCI reference name differs from Docker tag")
    if "io.containerd.image.name" in annotations:
        require(annotations["io.containerd.image.name"] in (expected_tag, "docker.io/library/" + expected_tag),
                "OCI image name differs from Docker tag")
    sources = saved.get("LayerSources")
    if sources is not None:
        require(isinstance(sources, dict) and set(sources) == set(diff_ids), "Docker layer sources differ from OCI layers")
        for diff_id, descriptor in zip(diff_ids, layers):
            source = sources[diff_id]
            require(isinstance(source, dict) and all(source.get(key) == descriptor.get(key) for key in ("digest", "mediaType", "size")),
                    "Docker layer source descriptor differs from OCI layer")
    runtime = config.get("config")
    require(isinstance(runtime, dict) and isinstance(runtime.get("Labels"), dict), "missing retained runtime config or labels")
    sonic_manifest = runtime["Labels"].get("com.azure.sonic.manifest")
    require(isinstance(sonic_manifest, str) and isinstance(json.loads(sonic_manifest), dict), "invalid SONiC manifest label")
    return {"layers": layers, "layer_paths": layer_paths, "diff_ids": diff_ids,
            "layer_media_types": [value["mediaType"] for value in layers], "layer_count": len(layers),
            "manifest_path": manifest_path, "config_path": config_path, "config": config,
            "runtime_config_sha256": sha256(canonical_json(runtime))}


def tar_metadata(member):
    require(member.sparse is None, "sparse package or tracked layer entry is unsupported")
    unsupported = [key for key in member.pax_headers if
                   (key.startswith("SCHILY.") and not key.startswith("SCHILY.xattr.")) or
                   (key.startswith("LIBARCHIVE.") and not key.startswith("LIBARCHIVE.xattr."))]
    require(not unsupported, "unsupported extended filesystem metadata")
    kind = "file" if member.isfile() else "directory" if member.isdir() else "symlink" if member.issym() else "hardlink" if member.islnk() else "other"
    require(isinstance(member.mtime, (int, float)) and math.isfinite(member.mtime), "invalid archive timestamp")
    return {"type": kind, "mode": member.mode, "uid": member.uid, "gid": member.gid,
            "linkname": member.linkname, "xattrs": {key: value for key, value in member.pax_headers.items()
                                                     if key.startswith(("SCHILY.xattr.", "LIBARCHIVE.xattr."))},
            "mtime": member.mtime, "size": member.size}


def control_stanzas(data):
    text = data.decode("utf-8")
    result, current, key = [], {}, None
    for line in text.splitlines():
        if not line:
            if current:
                result.append(current)
                current, key = {}, None
            continue
        if line[:1].isspace():
            require(key is not None, "invalid Debian control continuation")
            current[key] += "\n" + line
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9-]*):[ \t]*(.*)", line)
        require(match is not None, "invalid Debian control field")
        key = match.group(1).lower()
        require(key not in current, "duplicate Debian control field")
        current[key] = match.group(2)
    if current:
        result.append(current)
    return result


def ar_members(data):
    require(data.startswith(b"!<arch>\n"), "invalid Debian ar archive")
    position, result = 8, {}
    while position < len(data):
        require(position + 60 <= len(data), "truncated Debian ar header")
        header = data[position:position + 60]
        require(header[58:60] == b"`\n", "invalid Debian ar header")
        name = header[:16].decode("ascii").strip().removesuffix("/")
        require(name and "/" not in name and name not in result, "unsupported or duplicate Debian ar member")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as error:
            raise RuntimeError("invalid Debian ar member size") from error
        require(size >= 0 and position + 60 + size <= len(data), "truncated Debian ar member")
        result[name] = data[position + 60:position + 60 + size]
        position += 60 + size + size % 2
    require(position == len(data), "invalid Debian ar padding")
    return result


def parse_tar_bytes(data, control=False):
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive:
            name = safe_path(member.name)
            require(name not in entries, "duplicate Debian tar path: " + name)
            metadata = tar_metadata(member)
            require(metadata["type"] in ("file", "directory", "symlink", "hardlink"), "unsupported Debian payload type")
            if control:
                require(name == "." or ("/" not in name and metadata["type"] == "file"), "invalid Debian control archive path")
            if metadata["type"] == "hardlink":
                metadata["linkname"] = safe_path(metadata["linkname"])
            if member.isfile():
                extracted = archive.extractfile(member)
                require(extracted is not None, "missing Debian file contents")
                metadata["data"] = extracted.read()
                require(len(metadata["data"]) == member.size, "truncated Debian file")
                metadata["sha256"] = sha256(metadata["data"])
            entries[name] = metadata
    return entries


def package_content(package, path, active=()):
    require(path not in active and path in package["entries"], "invalid Debian hardlink target")
    entry = package["entries"][path]
    if entry["type"] == "file":
        return entry["data"]
    require(entry["type"] == "hardlink", "Debian checksum path is not a file")
    return package_content(package, entry["linkname"], active + (path,))


def parse_package(path, expected_sha256=None):
    data = Path(path).read_bytes()
    digest = sha256(data)
    if expected_sha256 is not None:
        require(SHA256.fullmatch(expected_sha256) is not None and digest == expected_sha256, "baseline DEB SHA-256 mismatch")
    members = ar_members(data)
    require(members.pop("debian-binary", None) == b"2.0\n", "unsupported Debian package version")
    controls = [name for name in members if name in ("control.tar", "control.tar.gz", "control.tar.xz", "control.tar.bz2")]
    payloads = [name for name in members if name in ("data.tar", "data.tar.gz", "data.tar.xz", "data.tar.bz2")]
    require(len(controls) == len(payloads) == 1 and len(members) == 2, "unsupported Debian package members")
    control_entries = parse_tar_bytes(members[controls[0]], control=True)
    control_files = {name: value["data"] for name, value in control_entries.items() if value["type"] == "file"}
    require({"control", "md5sums"} <= set(control_files) <= {"control", "conffiles", "md5sums"},
            "SWSS package has unsupported control files, maintainer scripts, or triggers")
    stanzas = control_stanzas(control_files["control"])
    require(len(stanzas) == 1, "expected one Debian package control stanza")
    fields = stanzas[0]
    require(fields.get("package") == "swss" and fields.get("architecture") == "amd64" and fields.get("version"),
            "expected an amd64 SWSS package")
    entries = parse_tar_bytes(members[payloads[0]])
    require(entries.get(".", {}).get("type") == "directory", "Debian payload root directory is missing")
    conffiles = set()
    for line in control_files.get("conffiles", b"").decode().splitlines():
        require(line.startswith("/") and len(line.split()) == 1, "unsupported Debian conffile declaration")
        name = safe_path(line[1:])
        require(name not in conffiles and entries.get(name, {}).get("type") == "file", "invalid Debian conffile")
        conffiles.add(name)
    package = {"sha256": digest, "size": len(data), "control_files": control_files, "control": fields,
               "identity": {"name": fields["package"], "version": fields["version"], "architecture": fields["architecture"]},
               "entries": entries, "conffiles": conffiles}
    checksums = {}
    for line in control_files["md5sums"].decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{32})[ \t]+(.+)", line)
        require(match is not None, "invalid Debian md5sums entry")
        name = safe_path(match.group(2))
        require(name not in checksums and name not in conffiles, "duplicate or conffile Debian checksum")
        require(hashlib.md5(package_content(package, name)).hexdigest() == match.group(1), "Debian payload MD5 mismatch: " + name)
        checksums[name] = match.group(1)
    expected = {name for name, entry in entries.items() if entry["type"] in ("file", "hardlink")} - conffiles
    require(set(checksums) == expected, "Debian md5sums do not cover its regular payload")
    package["md5sums"] = checksums
    return package


def retained_location(state, inv, owned, contract):
    require(isinstance(owned, list) and len(owned) == len(set(owned)) and owned, "invalid retained owned-container list")
    for name in owned:
        filename(name, ".gz")
    package_name = filename(inv["swss"], ".deb")
    require(isinstance(contract, dict) and contract.get("schema") == SCHEMA and
            set(contract) == {"schema", "manifest", "specs", "static_inputs", "dynamic_inputs", "owned_dependencies"} and
            isinstance(contract.get("specs"), dict) and set(contract["specs"]) == set(owned) and
            isinstance(contract.get("owned_dependencies"), dict) and set(contract["owned_dependencies"]) == set(owned),
            "invalid retained container input contract")
    data = contract_bytes(contract)
    require(len(data) <= MAX_JSON, "container input contract is too large")
    digest = sha256(data)
    relative = "oci-retained-inputs/" + digest
    return Path(state), package_name, data, digest, relative


def load_retained_inputs(state, inv, owned, contract):
    """Load only the immutable baseline for this exact container input contract."""
    state, package_name, contract_data, contract_digest, relative = retained_location(state, inv, owned, contract)
    base = state / "oci-retained-inputs"
    require(not base.is_symlink() and (not base.exists() or base.is_dir()), "retained OCI input namespace is invalid")
    directory = state / relative
    receipt_path = directory / "receipt.json"
    if not receipt_path.exists() and not receipt_path.is_symlink():
        require(not directory.exists() and not directory.is_symlink(), "retained OCI directory has no receipt")
        return None
    require(directory.is_dir() and not directory.is_symlink() and not stat.S_IMODE(directory.stat().st_mode) & 0o222,
            "retained OCI directory is invalid")
    receipt_stat = receipt_path.lstat()
    require(stat.S_ISREG(receipt_stat.st_mode) and not stat.S_IMODE(receipt_stat.st_mode) & 0o222,
            "retained OCI receipt must be a read-only regular file")
    receipt = read_json(receipt_path)
    require(isinstance(receipt, dict) and receipt.get("schema") == RECEIPT_SCHEMA and
            isinstance(receipt.get("provenance"), dict) and receipt["provenance"] and
            receipt.get("receipt_path") == relative + "/receipt.json" and
            receipt.get("owned") == sorted(owned) and isinstance(receipt.get("archives"), dict) and
            set(receipt["archives"]) == set(owned), "invalid retained OCI receipt")
    package = receipt.get("package")
    require(isinstance(package, dict) and package.get("filename") == package_name and
            isinstance(package.get("identity"), dict) and package["identity"].get("name") == "swss" and
            package["identity"].get("architecture") == "amd64" and package["identity"].get("version"), "invalid retained package receipt")
    contract_record = receipt.get("contract")
    require(isinstance(contract_record, dict) and contract_record.get("sha256") == contract_digest and
            contract_record.get("size") == len(contract_data), "invalid retained container input receipt")
    records = [("container-inputs.json", contract_record), (package_name, package)] + list(receipt["archives"].items())
    for name, record in records:
        require(isinstance(record, dict) and record.get("path") == relative + "/" + name and
                isinstance(record.get("sha256"), str) and SHA256.fullmatch(record["sha256"]) is not None and
                type(record.get("size")) is int and record["size"] >= 0 and isinstance(record.get("file_identity"), dict),
                "invalid retained file receipt")
        current = file_identity(state / record["path"])
        require(not current["mode"] & 0o222 and current == record["file_identity"] and current["size"] == record["size"],
                "retained OCI input file identity changed")
        if name in receipt["archives"]:
            require(record.get("image_name") == name.removesuffix(".gz") and type(record.get("layer_count")) is int and
                    isinstance(record.get("layer_media_types"), list) and record["layer_count"] == len(record["layer_media_types"]) > 0 and
                    set(record["layer_media_types"]) <= LAYER_TYPES, "invalid retained OCI layer receipt")
    require((state / contract_record["path"]).read_bytes() == contract_data, "retained container input contract changed")
    return receipt


def capture_retained_inputs(state, inv, owned, contract, sources, provenance):
    """Capture explicit verified Bazel outputs under their native input contract."""
    state, package_name, contract_data, contract_digest, relative = retained_location(state, inv, owned, contract)
    require(isinstance(provenance, dict) and provenance, "retained provenance must be a nonempty JSON object")
    canonical_json(provenance)
    require(isinstance(sources, dict) and set(sources) == set(owned) | {package_name},
            "retained sources must name exactly the selected Bazel outputs")
    sources = {name: Path(path) for name, path in sources.items()}
    require(all(stat.S_ISREG(path.lstat().st_mode) for path in sources.values()), "retained sources must be regular non-symlink files")
    existing = load_retained_inputs(state, inv, owned, contract)
    if existing is not None:
        records = {package_name: existing["package"], **existing["archives"]}
        require(all(hash_file(path) == (records[name]["sha256"], records[name]["size"]) for name, path in sources.items()),
                "retained container input contract already has a different baseline")
        return existing
    base = state / "oci-retained-inputs"
    base.mkdir(parents=True, exist_ok=True)
    require(base.is_dir() and not base.is_symlink(), "retained OCI input namespace is invalid")
    directory = state / relative
    temporary = Path(tempfile.mkdtemp(prefix="." + contract_digest + "-", dir=base))
    try:
        records = {name: copy_retained(source, temporary / name) for name, source in sorted(sources.items())}
        with (temporary / "container-inputs.json").open("xb") as stream:
            stream.write(contract_data)
            stream.flush()
            os.fsync(stream.fileno())
        (temporary / "container-inputs.json").chmod(0o444)
        contract_record = {"path": relative + "/container-inputs.json", "sha256": contract_digest,
                           "size": len(contract_data), "file_identity": file_identity(temporary / "container-inputs.json")}
        package = parse_package(temporary / package_name, records[package_name]["sha256"])
        package_record = {"filename": package_name, "path": relative + "/" + package_name,
                          "identity": package["identity"], **records[package_name]}
        archives = {}
        for name in sorted(owned):
            image_name = name.removesuffix(".gz")
            inspected = validate_catalog(archive_catalog(temporary / name, expected_sha256=records[name]["sha256"]), image_name)
            archives[name] = {"path": relative + "/" + name, "image_name": image_name,
                              "layer_media_types": inspected["layer_media_types"], "layer_count": inspected["layer_count"], **records[name]}
        receipt = {"schema": RECEIPT_SCHEMA, "receipt_path": relative + "/receipt.json", "provenance": provenance,
                   "contract": contract_record, "owned": sorted(owned), "package": package_record, "archives": archives}
        write_json(temporary / "receipt.json", receipt)
        (temporary / "receipt.json").chmod(0o444)
        temporary.chmod(0o555)
        require(not directory.exists() and not directory.is_symlink(), "retained OCI directory appeared during capture")
        temporary.rename(directory)
        return load_retained_inputs(state, inv, owned, contract)
    finally:
        if temporary.exists():
            temporary.chmod(0o755)
            shutil.rmtree(temporary)


def scan_layers(layout, inspected, targets):
    targets = set(targets)
    resolved = {}
    for descriptor, path, expected_diff_id in zip(inspected["layers"], inspected["layer_paths"], inspected["diff_ids"]):
        removed, entries = set(), {}
        with (Path(layout) / path).open("rb") as raw:
            decoded = gzip.GzipFile(fileobj=raw, mode="rb") if descriptor["mediaType"] == OCI_GZIP_LAYER else raw
            hashed = DigestReader(decoded)
            with tarfile.open(fileobj=hashed, mode="r|") as archive:
                for member in archive:
                    name = safe_path(member.name)
                    if name.startswith(INFO_PREFIX):
                        targets.add(name)
                    directory, _, basename = name.rpartition("/")
                    if basename == ".wh..wh..opq":
                        removed.update(target for target in targets if not directory or target.startswith(directory + "/"))
                        continue
                    if basename.startswith(".wh."):
                        hidden = (directory + "/" if directory else "") + basename[4:]
                        removed.update(target for target in targets if target == hidden or target.startswith(hidden + "/"))
                        continue
                    if not member.isdir():
                        removed.update(target for target in targets if target.startswith(name + "/"))
                        for target in list(entries):
                            if target.startswith(name + "/"):
                                entries.pop(target)
                    if name not in targets:
                        continue
                    entry = tar_metadata(member)
                    if member.isfile():
                        extracted = archive.extractfile(member)
                        require(extracted is not None, "missing tracked layer file data")
                        capture = name in INFO_PATHS or name == STATUS_PATH
                        require(not capture or member.size <= 32 * 1024 * 1024, "package metadata file is too large")
                        info, data = consume(extracted, capture)
                        require(info["size"] == member.size, "truncated tracked layer file")
                        entry.update(info)
                        if data is not None:
                            entry["data"] = data
                    entries[name] = entry
            consume(hashed)
            if decoded is not raw:
                decoded.close()
        require("sha256:" + hashed.digest.hexdigest() == expected_diff_id, "OCI layer DiffID mismatch")
        for name in removed:
            resolved.pop(name, None)
        resolved.update(entries)
    return resolved


def without_data(entry):
    return {key: value for key, value in entry.items() if key != "data"}


def extract(args):
    package = parse_package(args.baseline_deb, args.baseline_sha256)
    layout, metadata = Path(args.layout), Path(args.metadata)
    layout.parent.mkdir(parents=True, exist_ok=True)
    require(not layout.is_symlink() and (not layout.exists() or (layout.is_dir() and not any(layout.iterdir()))),
            "OCI layout output must be absent or an empty non-symlink directory")
    temporary = Path(tempfile.mkdtemp(prefix=".oci-layout-", dir=layout.parent))
    try:
        catalog = archive_catalog(args.archive, temporary, args.archive_sha256)
        inspected = validate_catalog(catalog, args.image_name)
        reachable = {inspected["manifest_path"], inspected["config_path"], *inspected["layer_paths"]}
        for path in (temporary / "blobs/sha256").iterdir():
            if "blobs/sha256/" + path.name not in reachable:
                path.unlink()
        payload_paths = {name for name, entry in package["entries"].items() if entry["type"] != "directory"}
        resolved = scan_layers(temporary, inspected, payload_paths | INFO_PATHS | {STATUS_PATH})
        payload = {name: without_data(resolved[name]) for name in sorted(payload_paths & set(resolved))}
        package_files = {name: without_data(resolved[name]) for name in sorted(resolved) if name.startswith(INFO_PREFIX)}
        list_entry = resolved.get(INFO_PREFIX + "list", {})
        list_paths = list_entry.get("data", b"").decode().splitlines() if list_entry.get("type") == "file" else None
        status_entry = resolved.get(STATUS_PATH, {})
        stanzas = control_stanzas(status_entry.get("data", b"")) if status_entry.get("type") == "file" else []
        swss_status = [value for value in stanzas if value.get("package") == "swss"]
        result = {"schema": SCHEMA, "kind": "sonic-oci-seed", "image_name": args.image_name,
                  "archive_sha256": args.archive_sha256, "baseline_sha256": package["sha256"], "package": package["identity"],
                  "layout": {"manifest": inspected["manifest_path"], "config": inspected["config_path"],
                             "layers": inspected["layers"], "diff_ids": inspected["diff_ids"],
                             "runtime_config_sha256": inspected["runtime_config_sha256"]},
                  "payload": payload, "omitted_paths": sorted(payload_paths - set(payload)), "package_files": package_files,
                  "package_list_paths": list_paths, "swss_status": swss_status}
        temporary.rename(layout)
        write_json(metadata, result)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def inventory_signature(entry):
    return {key: entry[key] for key in ("type", "mode", "uid", "gid", "linkname", "xattrs")}


def validate_seed(seed, package):
    require(isinstance(seed, dict) and seed.get("schema") == SCHEMA and seed.get("kind") == "sonic-oci-seed" and
            seed.get("baseline_sha256") == package["sha256"] and seed.get("package") == package["identity"] and
            isinstance(seed.get("image_name"), str) and isinstance(seed.get("archive_sha256"), str) and
            SHA256.fullmatch(seed["archive_sha256"]) is not None, "invalid or incompatible OCI seed metadata")
    payload = seed.get("payload")
    require(isinstance(payload, dict), "invalid OCI seed payload metadata")
    expected_paths = {name for name, entry in package["entries"].items() if entry["type"] != "directory"}
    require(set(payload) <= expected_paths and seed.get("omitted_paths") == sorted(expected_paths - set(payload)), "OCI seed payload inventory differs")
    for name, entry in payload.items():
        baseline = package["entries"][name]
        require(isinstance(entry, dict) and inventory_signature(entry) == inventory_signature(baseline), "OCI seed payload metadata differs: " + name)
        if baseline["type"] == "file":
            data = package_content(package, name)
            require(entry.get("sha256") == sha256(data) and entry.get("size") == len(data), "OCI seed payload bytes differ: " + name)
    package_files = seed.get("package_files")
    expected_info = {INFO_PREFIX + "md5sums", INFO_PREFIX + "list"}
    if "conffiles" in package["control_files"]:
        expected_info.add(INFO_PREFIX + "conffiles")
    require(isinstance(package_files, dict) and set(package_files) == expected_info, "OCI seed package metadata is incomplete or has unsupported effects")
    for name in ("md5sums", "conffiles"):
        if name not in package["control_files"]:
            continue
        entry = package_files[INFO_PREFIX + name]
        data = package["control_files"].get(name, b"")
        require(entry.get("type") == "file" and entry.get("sha256") == sha256(data) and entry.get("size") == len(data),
                "OCI seed installed package metadata differs: " + name)
    expected_list = {"/." if name == "." else "/" + name for name in package["entries"]}
    listed = seed.get("package_list_paths")
    require(isinstance(listed, list) and len(listed) == len(set(listed)) and set(listed) == expected_list and
            package_files[INFO_PREFIX + "list"].get("type") == "file", "OCI seed installed package file list differs")
    statuses = seed.get("swss_status")
    require(isinstance(statuses, list) and len(statuses) == 1 and isinstance(statuses[0], dict), "OCI seed SWSS status is missing or duplicated")
    status_fields = statuses[0]
    expected_status_fields = set(package["control"]) | {"status"}
    if package["conffiles"]:
        expected_status_fields.add("conffiles")
    require(status_fields.get("status") == "install ok installed" and
            set(status_fields) == expected_status_fields and
            all(status_fields.get(key) == value for key, value in package["control"].items()), "OCI seed SWSS status differs from baseline package")
    expected_conffiles = ["/" + name + " " + hashlib.md5(package_content(package, name)).hexdigest()
                          for name in sorted(package["conffiles"])]
    actual_conffiles = [line.strip() for line in status_fields.get("conffiles", "").splitlines() if line.strip()]
    require(actual_conffiles == expected_conffiles, "OCI seed conffile status differs")
    return seed


def comparable_package_files(seed):
    result = {}
    for name, entry in seed["package_files"].items():
        result[name] = dict(entry)
        if name == INFO_PREFIX + "list":
            # Leaf package operations may refresh this untouched file's timestamp.
            result[name].pop("mtime", None)
    return result


def overlay(args):
    baseline = parse_package(args.baseline_deb, args.baseline_sha256)
    package = parse_package(args.deb)
    require(baseline["control_files"]["control"] == package["control_files"]["control"] and
            baseline["control_files"].get("conffiles") == package["control_files"].get("conffiles"),
            "SWSS package control or conffiles changed")
    require(set(baseline["entries"]) == set(package["entries"]), "SWSS package payload path inventory changed")
    for name, entry in baseline["entries"].items():
        require(inventory_signature(entry) == inventory_signature(package["entries"][name]), "SWSS package payload metadata changed: " + name)
    require(baseline["conffiles"] == package["conffiles"] and
            all(package_content(baseline, name) == package_content(package, name) for name in baseline["conffiles"]),
            "SWSS package conffile bytes changed")
    require(all(package_content(baseline, name) == package_content(package, name)
                for name, entry in baseline["entries"].items() if entry["type"] == "hardlink"),
            "SWSS package hardlink content changed")
    seeds = [validate_seed(read_json(path), baseline) for path in args.seed_metadata]
    require(seeds and len({seed["image_name"] for seed in seeds}) == len(seeds), "OCI seed image names must be unique")
    first = seeds[0]
    first_package_files = comparable_package_files(first)
    for seed in seeds[1:]:
        require(seed["payload"] == first["payload"] and seed["omitted_paths"] == first["omitted_paths"] and
                comparable_package_files(seed) == first_package_files and seed["package_list_paths"] == first["package_list_paths"] and
                seed["swss_status"] == first["swss_status"], "OCI seed images disagree on effective SWSS files or metadata")
    changed = [name for name, entry in baseline["entries"].items() if entry["type"] == "file" and
               entry["sha256"] != package["entries"][name]["sha256"]]
    emitted = sorted(set(changed) & set(first["payload"]))
    omitted_changes = sorted(set(changed) - set(first["payload"]))
    output_entries = [(name, package["entries"][name]["data"], first["payload"][name]) for name in emitted]
    old_md5 = baseline["control_files"]["md5sums"]
    new_md5 = package["control_files"]["md5sums"]
    if new_md5 != old_md5:
        output_entries.append((INFO_PREFIX + "md5sums", new_md5, first["package_files"][INFO_PREFIX + "md5sums"]))
    layer = Path(args.layer)
    layer.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=layer.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with tarfile.open(temporary, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for name, data, retained in sorted(output_entries):
                require(retained["type"] == "file", "OCI overlay can update only retained regular files")
                member = tarfile.TarInfo(name)
                member.mode, member.uid, member.gid = retained["mode"], retained["uid"], retained["gid"]
                member.mtime, member.size = retained["mtime"], len(data)
                member.pax_headers = retained["xattrs"]
                archive.addfile(member, io.BytesIO(data))
        layer_digest, layer_size = hash_file(temporary)
        temporary.replace(layer)
    finally:
        temporary.unlink(missing_ok=True)
    result = {"schema": SCHEMA, "kind": "sonic-swss-overlay", "baseline_sha256": baseline["sha256"], "deb_sha256": package["sha256"],
              "layer_sha256": layer_digest, "layer_size": layer_size, "changed_paths": emitted,
              "omitted_paths": first["omitted_paths"], "changed_omitted_paths": omitted_changes,
              "updated_package_paths": [INFO_PREFIX + "md5sums"] if new_md5 != old_md5 else [],
              "md5sums_sha256": sha256(new_md5),
              "payload_changes": {name: {"baseline_sha256": baseline["entries"][name]["sha256"],
                                         "sha256": package["entries"][name]["sha256"], "omitted": name in omitted_changes}
                                  for name in sorted(changed)},
              "seed_images": {seed["image_name"]: seed["archive_sha256"] for seed in seeds}}
    write_json(args.metadata, result)


def validate_contract(args):
    baseline_data = Path(args.baseline).read_bytes()
    current_data = Path(args.current).read_bytes()
    for name, data in (("baseline", baseline_data), ("current", current_data)):
        require(len(data) <= MAX_JSON, name + " container input contract is too large")
        require(data == contract_bytes(json.loads(data)), name + " container input contract is not canonical")
    require(isinstance(args.baseline_sha256, str) and SHA256.fullmatch(args.baseline_sha256) is not None and
            sha256(baseline_data) == args.baseline_sha256, "baseline container input contract SHA-256 mismatch")
    require(current_data == baseline_data, "current container inputs differ from retained baseline")
    write_json(args.stamp, {"schema": SCHEMA, "contract_sha256": args.baseline_sha256})


def deterministic_gzip(args):
    source, output = Path(args.input), Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as stream:
        temporary = Path(stream.name)
        with source.open("rb") as input_stream, gzip.GzipFile(filename="", fileobj=stream, mode="wb", compresslevel=6, mtime=0) as compressed:
            shutil.copyfileobj(input_stream, compressed, CHUNK)
    try:
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("contract")
    for name in ("baseline", "current", "baseline-sha256", "stamp"):
        command.add_argument("--" + name, required=True)
    command.set_defaults(run=validate_contract)
    command = commands.add_parser("extract")
    for name in ("archive", "baseline-deb", "archive-sha256", "baseline-sha256", "image-name", "layout", "metadata"):
        command.add_argument("--" + name, required=True)
    command.set_defaults(run=extract)
    command = commands.add_parser("overlay")
    for name in ("baseline-deb", "deb", "baseline-sha256", "layer", "metadata"):
        command.add_argument("--" + name, required=True)
    command.add_argument("--seed-metadata", action="append", required=True)
    command.set_defaults(run=overlay)
    command = commands.add_parser("gzip")
    command.add_argument("--input", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(run=deterministic_gzip)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, tarfile.TarError, EOFError) as error:
        print("SONiC OCI: " + str(error), file=sys.stderr)
        sys.exit(1)
