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
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile


SCHEMA = 1
PRIVATE_KEYS = {
    "PASSWORD", "BMC_ROOT_ACCOUNT_DEFAULT_PASSWORD", "HTTP_PROXY",
    "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
}
PROCESS_KEYS = set("HOME USER LOGNAME SHELL PATH LANG LC_ALL TZ RUSTUP_HOME DOCKER_HOST DOCKER_BUILDKIT IMAGENAME".split())
GENERATED_ROOT_PATHS = {
    "target", "fsroot-vs", "fsroot.docker.trixie",
    "fs.squashfs", "dockerfs.tar.gz", "fs.zip",
}
NATIVE_SNAPSHOT_MARKER = ".sonic-bazel-native-source"
NATIVE_SNAPSHOT_RUNTIME = "target/bazel/native-snapshot.json"


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


def _regular_json(path, description):
    """Read a regular JSON file without following its final path component."""
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(description + " must be a regular non-symlink file")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise RuntimeError(description + " changed while opening it")
            return json.load(stream)
    except (OSError, ValueError) as error:
        raise RuntimeError("invalid " + description + ": " + str(path)) from error


def validate_native_snapshot(root, identity):
    """Bind an internal native inventory request to its owned snapshot root."""
    root = Path(root)
    valid_identity = (
        isinstance(identity, dict)
        and set(identity) == {"schema", "snapshot_id", "device", "inode"}
        and type(identity.get("schema")) is int and identity["schema"] == 1
        and isinstance(identity.get("snapshot_id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", identity["snapshot_id"]) is not None
        and type(identity.get("device")) is int and identity["device"] >= 0
        and type(identity.get("inode")) is int and identity["inode"] > 0
    )
    if not valid_identity:
        raise RuntimeError("invalid native snapshot identity")
    try:
        info = root.lstat()
    except OSError as error:
        raise RuntimeError("native snapshot root is unavailable: " + str(root)) from error
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("native snapshot root must be a directory, not a symlink")
    if (info.st_dev, info.st_ino) != (identity["device"], identity["inode"]):
        raise RuntimeError("native snapshot root does not match its runtime identity")
    marker = _regular_json(root / NATIVE_SNAPSHOT_MARKER, "native snapshot marker")
    if not isinstance(marker, dict) or type(marker.get("schema")) is not int or marker != {"schema": 2, "snapshot_id": identity["snapshot_id"]}:
        raise RuntimeError("native snapshot marker does not match its runtime identity")
    return dict(identity)


def load_native_snapshot_identity(root):
    """Load and validate the launcher-issued native snapshot runtime identity."""
    root = Path(root)
    runtime = root / NATIVE_SNAPSHOT_RUNTIME
    current = root
    for component in Path(NATIVE_SNAPSHOT_RUNTIME).parts[:-1]:
        current = current / component
        try:
            info = current.lstat()
        except OSError as error:
            raise RuntimeError("native snapshot runtime directory is unavailable: " + str(current)) from error
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("native snapshot runtime path must not traverse a symlink")
    identity = _regular_json(runtime, "native snapshot runtime identity")
    return validate_native_snapshot(root, identity)


def _native_git_environment():
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return environment


def _git_command(arguments, native=False):
    command = ["git"]
    if native:
        command.extend([
            "-c", "core.fsmonitor=false",
            "-c", "core.excludesFile=/dev/null",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.untrackedCache=false",
        ])
    return command + arguments


def _native_mountpoints():
    try:
        with open("/proc/self/mountinfo") as stream:
            return {
                Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), line.split(" ", 5)[4]))
                for line in stream
            }
    except (OSError, IndexError) as error:
        raise RuntimeError("cannot inspect native snapshot mount containment") from error


def _validate_native_repository(directory, root, environment, mountpoints):
    """Require an independent standalone Git checkout inside the native root."""
    directory = directory.absolute()
    try:
        canonical = directory.resolve(strict=True)
        relative = directory.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError("native repository leaves the snapshot: " + str(directory)) from error
    if canonical != directory:
        raise RuntimeError("native repository path must not traverse a symlink: " + str(directory))
    current = root
    for component in (None, *relative.parts):
        if component is not None:
            current = current / component
        try:
            info = current.lstat()
        except OSError as error:
            raise RuntimeError("native repository path is unavailable: " + str(current)) from error
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("native repository path must contain only directories: " + str(current))
        if current != root and current in mountpoints:
            raise RuntimeError("native repository path contains a nested mount: " + str(current))

    git_directory = directory / ".git"
    try:
        git_info = git_directory.lstat()
    except OSError as error:
        raise RuntimeError("native repository requires a local .git directory: " + str(directory)) from error
    if not stat.S_ISDIR(git_info.st_mode):
        raise RuntimeError("native repository requires a local .git directory: " + str(directory))
    forbidden = {
        git_directory / "commondir",
        git_directory / "objects/info/alternates",
        git_directory / "objects/info/http-alternates",
    }
    pending = [git_directory]
    seen_storage = set()
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
        except OSError as error:
            raise RuntimeError("native Git storage changed while inspecting it: " + str(path)) from error
        if path in forbidden:
            raise RuntimeError("native Git storage contains an unsupported indirection: " + str(path))
        if path in mountpoints:
            raise RuntimeError("native Git storage contains a nested mount: " + str(path))
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("native Git storage contains a symlink: " + str(path))
        if stat.S_ISDIR(info.st_mode):
            storage_identity = (info.st_dev, info.st_ino)
            if storage_identity in seen_storage:
                raise RuntimeError("native Git storage repeats a directory: " + str(path))
            seen_storage.add(storage_identity)
            try:
                with os.scandir(path) as entries:
                    pending.extend(path / entry.name for entry in entries)
            except OSError as error:
                raise RuntimeError("native Git storage is unavailable: " + str(path)) from error
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise RuntimeError("native Git storage contains a file with hard links: " + str(path))
        else:
            raise RuntimeError("native Git storage contains a special file: " + str(path))

    for configuration in (git_directory / "config", git_directory / "config.worktree"):
        if configuration.exists():
            try:
                keys = subprocess.check_output(
                    _git_command(["config", "--file", str(configuration), "--no-includes", "--name-only", "--null", "--list"], native=True),
                    cwd=directory, env=environment, stderr=subprocess.STDOUT,
                )
            except (OSError, subprocess.CalledProcessError) as error:
                raise RuntimeError("invalid native Git configuration: " + str(configuration)) from error
            if any(key.lower().startswith((b"include.", b"includeif.")) for key in keys.split(b"\0") if key):
                raise RuntimeError("native Git configuration must not include external files: " + str(configuration))
    try:
        reported = command_output(
            _git_command(["rev-parse", "--path-format=absolute", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"], native=True),
            directory, env=environment,
        ).splitlines()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("invalid native Git repository: " + str(directory)) from error
    if reported != [str(directory), str(git_directory), str(git_directory)]:
        raise RuntimeError("native Git repository worktree or storage leaves its local checkout: " + str(directory))
    return canonical


def _validate_native_source_ancestry(repository, name, checked):
    """Reject malformed embedded Git layouts exposed as individual source files."""
    current = repository
    for component in Path(name).parts[:-1]:
        current = current / component
        if current in checked:
            continue
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            return
        try:
            (current / ".git").lstat()
        except FileNotFoundError:
            checked.add(current)
        else:
            raise RuntimeError("native Git listed a source path through an unsupported embedded repository: " + str(current))


def git_source_listing(repository, root_repository=False, env=None):
    """List source entries without traversing generated paths in the root repo."""
    command = _git_command(["ls-files", "--cached", "--others", "--exclude-standard", "-z"], native=env is not None)
    if root_repository:
        command.extend("--exclude=/" + name for name in sorted(GENERATED_ROOT_PATHS))
    return subprocess.check_output(command, cwd=repository, env=env)


def git_sources(root, exclude_swss=True, context="caller", snapshot_identity=None):
    """Return source paths and repository identities, excluding SWSS inputs."""
    root = Path(root)
    if context not in {"caller", "native"}:
        raise RuntimeError("unsupported source inventory context: " + str(context))
    native = context == "native"
    if native:
        validate_native_snapshot(root, snapshot_identity)
        root = root.absolute()
        environment = _native_git_environment()
        mountpoints = _native_mountpoints()
    else:
        if snapshot_identity is not None:
            raise RuntimeError("snapshot identity is only valid for native inventory")
        environment = None
        mountpoints = set()
    paths = set()
    repositories = []
    visited = set()
    checked_source_directories = set()

    def visit(directory, prefix, role):
        if native:
            canonical = _validate_native_repository(directory, root, environment, mountpoints)
            if canonical in visited:
                raise RuntimeError("native source inventory repeats a repository: " + str(directory))
            visited.add(canonical)
        identity = {
            "path": prefix or ".",
            "commit": command_output(_git_command(["rev-parse", "HEAD"], native), directory, env=environment),
            "branch": command_output(_git_command(["rev-parse", "--abbrev-ref", "HEAD"], native), directory, env=environment),
        }
        if native:
            identity["role"] = role
        repositories.append(identity)
        staged = subprocess.check_output(_git_command(["ls-files", "--stage", "-z"], native), cwd=directory, env=environment)
        submodules = set()
        for record in staged.split(b"\0"):
            if not record:
                continue
            try:
                metadata, encoded = record.split(b"\t", 1)
            except ValueError as error:
                if native:
                    raise RuntimeError("invalid native Git index entry") from error
                raise
            if native:
                try:
                    mode, _object, stage = metadata.split()
                except ValueError as error:
                    raise RuntimeError("invalid native Git index entry") from error
                if stage != b"0":
                    raise RuntimeError("native source repository has unresolved Git conflicts: " + str(directory))
                name = os.fsdecode(encoded)
                logical = Path(name)
                if not name or logical.is_absolute() or ".." in logical.parts:
                    raise RuntimeError("native Git source path leaves the snapshot: " + name)
                if mode == b"160000":
                    if role == "generated":
                        raise RuntimeError("generated native repositories must not contain gitlinks: " + str(directory))
                    submodules.add(name)
            elif metadata.startswith(b"160000 "):
                submodules.add(os.fsdecode(encoded))
        listed = git_source_listing(directory, root_repository=not prefix, env=environment)
        for encoded in listed.split(b"\0"):
            if not encoded:
                continue
            name = os.fsdecode(encoded)
            if native:
                logical = Path(name)
                if not name or logical.is_absolute() or ".." in logical.parts:
                    raise RuntimeError("native Git source path leaves the snapshot: " + name)
            relative = str(Path(prefix) / name) if prefix else name
            if not prefix and generated_root_path(name):
                continue
            if native and not prefix and name == NATIVE_SNAPSHOT_MARKER:
                continue
            if exclude_swss and (relative == "src/sonic-swss" or relative.startswith("src/sonic-swss/")):
                continue
            if native:
                _validate_native_source_ancestry(directory, name, checked_source_directories)
            if name in submodules:
                child = directory / name
                if not native and not (child / ".git").exists():
                    raise RuntimeError("uninitialized public submodule: " + relative)
                visit(child, relative, "submodule")
            else:
                if native:
                    child = directory / name
                    try:
                        is_directory = stat.S_ISDIR(child.lstat().st_mode)
                    except FileNotFoundError:
                        is_directory = False
                    if is_directory:
                        if role == "generated":
                            raise RuntimeError("generated native repositories must not contain embedded repositories: " + relative)
                        visit(child, str(Path(relative)), "generated")
                        continue
                paths.add(relative)

    visit(root, "", "root")
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


def source_state(root, context="caller", snapshot_identity=None):
    paths, repositories = git_sources(root, context=context, snapshot_identity=snapshot_identity)
    return {
        "repositories": repositories,
        "entries": {relative: source_entry(root, relative) for relative in paths},
    }


def cleanup_p4_source_repositories(root, snapshot_identity, p4c_version):
    """Remove disposable Git worktrees left by completed P4C and DASH packages."""
    if not isinstance(p4c_version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+~_-]*", p4c_version):
        raise RuntimeError("invalid P4C version for native source cleanup")
    root = Path(root).absolute()
    validate_native_snapshot(root, snapshot_identity)
    environment = _native_git_environment()
    mountpoints = _native_mountpoints()
    _validate_native_repository(root, root, environment, mountpoints)
    listed = git_source_listing(root, root_repository=True, env=environment)
    generated = {
        os.fsdecode(entry).rstrip("/")
        for entry in listed.split(b"\0") if entry.endswith(b"/")
    }
    planned = []
    for logical in ("src/dash-sai/DASH", "src/p4lang/p4lang-p4c-" + p4c_version):
        path = root / logical
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        if logical not in generated:
            raise RuntimeError("native source cleanup path is not a generated repository: " + logical)
        _validate_native_repository(path, root, environment, mountpoints)
        if any(path == mount or path in mount.parents for mount in mountpoints):
            raise RuntimeError("native source cleanup refuses a nested mount: " + logical)
        planned.append((logical, path))
    # Validate both paths before removing either. Package outputs live under
    # target/ and have already been checked by the preparation driver.
    for logical, path in planned:
        shutil.rmtree(path)
        print("Removed completed native source repository " + logical, flush=True)


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
    identity = load_native_snapshot_identity(root)
    actual = source_state(root, context="native", snapshot_identity=identity)
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


def image_worker_limits():
    identifiers = sorted({
        getattr(resource, name) for name in dir(resource)
        if name.startswith("RLIMIT_") and isinstance(getattr(resource, name), int)
    })
    return [[identifier, *resource.getrlimit(identifier)] for identifier in identifiers]


def run_image_stage(command, root, environment):
    """Run image composition with owned processes and private mounts."""
    if os.getresuid() != (os.getuid(),) * 3 or os.getresgid() != (os.getgid(),) * 3:
        raise RuntimeError("image worker requires ordinary build-user credentials")
    root_info = root.stat()
    previous_umask = os.umask(0)
    os.umask(previous_umask)
    request = {
        "schema": 1, "stage": "image", "cwd": str(root),
        "cwd_device": root_info.st_dev, "cwd_inode": root_info.st_ino,
        "command": command, "environment": environment,
        "uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(),
        "umask": previous_umask, "rlimits": image_worker_limits(),
    }
    process = None
    interrupted = None
    previous_handlers = {}

    def interrupt(signum, _frame):
        nonlocal interrupted
        if interrupted is None:
            interrupted = signum

    # The helper reads this anonymous file before entering namespaces.  This
    # preserves stdin and keeps the exact build environment out of argv.
    with tempfile.TemporaryFile(mode="w+b") as request_file:
        os.fchmod(request_file.fileno(), 0o600)
        request_file.write(canonical_json(request))
        request_file.flush()
        request_file.seek(0)
        request_path = "/proc/" + str(os.getpid()) + "/fd/" + str(request_file.fileno())
        try:
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous_handlers[signum] = signal.signal(signum, interrupt)
            previous_handlers[signal.SIGCHLD] = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            process = subprocess.Popen(
                ["sudo", "-n", "--", "python3", "-B", "scripts/bazel/native/image_worker.py", request_path],
                cwd=root, env=environment, start_new_session=True,
            )
            cancellation_sent = False
            while True:
                if interrupted is not None and not cancellation_sent:
                    process.send_signal(signal.SIGTERM)
                    cancellation_sent = True
                try:
                    process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            if process is not None:
                process.send_signal(signal.SIGTERM)
            raise
        finally:
            try:
                if process is not None:
                    # Keep the caller's action lock until PID 1 has been reaped.
                    process.wait()
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
    if interrupted is not None:
        raise RuntimeError("image worker interrupted by signal " + str(interrupted))
    if process is None:
        raise RuntimeError("image worker did not start")
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command)


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
        if (
            source.stat().st_size == destination.stat().st_size
            and stat.S_IMODE(source.stat().st_mode) == stat.S_IMODE(destination.stat().st_mode)
            and digest_file(source) == digest_file(destination)
        ):
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
            if spec["stage"] == "image":
                if spec.get("context_mounts"):
                    raise RuntimeError("image worker does not support Docker context mounts")
                run_image_stage(command, root, environment)
            else:
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
