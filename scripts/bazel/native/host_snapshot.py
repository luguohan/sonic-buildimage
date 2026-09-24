#!/usr/bin/env python3
"""State and cleanup helpers for the opt-in Bazel VS host snapshot.

The snapshot contains a small JSON record instead of a saved shell environment.
Only the version values produced before the container boundary are retained.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


FORMAT_VERSION = 1
STATE_FIELDS = (
    "build_version",
    "debian_version",
    "kernel_version",
    "asic_type",
    "asic_subtype",
    "commit_id",
    "branch",
    "release",
    "build_date",
    "build_number",
    "built_by",
    "sonic_os_version",
)
IDENTITY_FIELDS = (
    "arch",
    "platform",
    "machine",
    "image_type",
    "distro",
    "image_version",
    "source_commit",
    "source_branch",
    "source_date_epoch",
)


def identity(args):
    return {name: getattr(args, name) for name in IDENTITY_FIELDS}


def state_record(args):
    return {
        "format_version": FORMAT_VERSION,
        "boundary": "before-container-loading",
        "identity": identity(args),
        "state": {name: os.environ.get(name, "") for name in STATE_FIELDS},
    }


def read_record(path, expected_identity):
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("host snapshot has an invalid state record file")
    with path.open(encoding="utf-8") as stream:
        record = json.load(stream)
    if not isinstance(record, dict) or set(record) != {"format_version", "boundary", "identity", "state"}:
        raise ValueError("host snapshot has an invalid state record")
    if record["format_version"] != FORMAT_VERSION:
        raise ValueError("host snapshot has an unsupported format version")
    if record["boundary"] != "before-container-loading":
        raise ValueError("host snapshot has an unsupported resume boundary")
    if record["identity"] != expected_identity:
        raise ValueError("host snapshot identity does not match this image build")
    state = record["state"]
    if not isinstance(state, dict) or set(state) != set(STATE_FIELDS):
        raise ValueError("host snapshot has an invalid state field set")
    if any(not isinstance(value, str) or "\0" in value for value in state.values()):
        raise ValueError("host snapshot has an invalid state value")
    return state


def scratch_root(value, must_exist):
    supplied = Path(value)
    if supplied.is_symlink():
        raise ValueError("Bazel host root must not be a symbolic link")
    root = supplied.resolve()
    # onie-image.conf chooses this dedicated directory for TARGET_MACHINE=vs.
    # Constrain cleanup to it so a bad argument cannot affect another tree.
    if root.parent != Path.cwd().resolve() or root.name != "fsroot-vs":
        raise ValueError("Bazel host root must be the working directory's fsroot-vs")
    if must_exist and not root.is_dir():
        raise ValueError("Bazel host root does not exist")
    return root


def unescape_mount_path(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def mounts_below(root):
    prefix = str(root) + "/"
    mounts = []
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        for line in stream:
            mountpoint = unescape_mount_path(line.split(" ", 5)[4])
            if mountpoint == str(root) or mountpoint.startswith(prefix):
                mounts.append(mountpoint)
    return sorted(mounts, key=lambda value: (value.count("/"), len(value)), reverse=True)


def process_is_below(root, pid):
    prefix = str(root) + "/"
    try:
        process_root = os.readlink(Path("/proc") / str(pid) / "root")
    except (FileNotFoundError, ProcessLookupError):
        return False
    return process_root == str(root) or process_root.startswith(prefix)


def processes_below(root):
    processes = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        if process_is_below(root, entry.name):
            processes.append(int(entry.name))
    return processes


def wait_for_processes(root, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        processes = processes_below(root)
        if not processes:
            return []
        time.sleep(0.1)
    return processes_below(root)


def signal_processes(root, processes, signum):
    for pid in processes:
        try:
            # A pidfd prevents a recycled PID from selecting another process.
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            if process_is_below(root, pid):
                signal.pidfd_send_signal(descriptor, signum)
        except ProcessLookupError:
            pass
        finally:
            os.close(descriptor)


def assert_clean(root):
    processes = processes_below(root)
    mounts = mounts_below(root)
    if processes:
        raise ValueError("Bazel host root still contains running processes: " + ", ".join(map(str, processes)))
    if mounts:
        raise ValueError("Bazel host root still contains mounts: " + ", ".join(mounts))


def quiesce(root):
    # Package post-install scripts can leave services behind. Only processes
    # whose chroot is this dedicated build root are eligible for termination.
    processes = processes_below(root)
    if processes and (not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal")):
        raise ValueError("stopping host-root processes requires Linux pidfd support")
    signal_processes(root, processes, signal.SIGTERM)
    processes = wait_for_processes(root, 10)
    if processes:
        signal_processes(root, processes, signal.SIGKILL)
        processes = wait_for_processes(root, 5)
    if processes:
        raise ValueError("could not stop processes in the Bazel host root")
    for mountpoint in mounts_below(root):
        subprocess.run(["umount", mountpoint], check=True)
    assert_clean(root)


def add_identity_arguments(parser):
    for name in IDENTITY_FIELDS:
        parser.add_argument("--" + name.replace("_", "-"), required=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_parser = subparsers.add_parser("write-state")
    write_parser.add_argument("path", type=Path)
    add_identity_arguments(write_parser)
    read_parser = subparsers.add_parser("read-state")
    read_parser.add_argument("path", type=Path)
    add_identity_arguments(read_parser)
    for command in ("assert-clean", "quiesce", "remove"):
        root_parser = subparsers.add_parser(command)
        root_parser.add_argument("root")
    args = parser.parse_args()

    if args.command == "write-state":
        args.path.write_text(json.dumps(state_record(args), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    elif args.command == "read-state":
        state = read_record(args.path, identity(args))
        for name in STATE_FIELDS:
            sys.stdout.buffer.write(state[name].encode("utf-8") + b"\0")
    else:
        if os.geteuid() != 0:
            raise ValueError("host root inspection and cleanup must run as root")
        root = scratch_root(args.root, must_exist=args.command == "quiesce")
        if args.command == "quiesce":
            quiesce(root)
        elif args.command == "remove":
            if root.exists():
                quiesce(root)
                shutil.rmtree(root)
            else:
                assert_clean(root)
        else:
            assert_clean(root)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Bazel host snapshot: " + str(error), file=sys.stderr)
        sys.exit(1)
