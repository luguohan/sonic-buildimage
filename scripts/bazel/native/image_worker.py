#!/usr/bin/env python3
"""Own the processes and mounts used by one Bazel image composition action."""

import json
import os
from pathlib import Path
import re
import resource
import signal
import stat
import subprocess
import sys
import time


cancelled = None


def request_cancel(signum, _frame):
    global cancelled
    if cancelled is None:
        cancelled = signum


def command_returncode(status):
    value = os.waitstatus_to_exitcode(status)
    return value if value >= 0 else 128 - value


def reap_children(command_pid, command_status):
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return False, command_status
        if pid == 0:
            return True, command_status
        if pid == command_pid:
            command_status = command_returncode(status)


def signal_descendants(signum):
    if os.getpid() != 1:
        raise RuntimeError("image process teardown requires PID 1")
    try:
        os.kill(-1, signum)
    except ProcessLookupError:
        pass


def finish_descendants(command_pid, command_status):
    remaining, command_status = reap_children(command_pid, command_status)
    if remaining:
        signal_descendants(signal.SIGTERM)
        deadline = time.monotonic() + 10
        while remaining and time.monotonic() < deadline:
            time.sleep(0.05)
            remaining, command_status = reap_children(command_pid, command_status)
    if remaining:
        signal_descendants(signal.SIGKILL)
        while remaining:
            time.sleep(0.05)
            remaining, command_status = reap_children(command_pid, command_status)
    return command_status


def verify_private_mounts(outer_namespace):
    if os.stat("/proc/self/ns/mnt").st_ino == outer_namespace:
        raise RuntimeError("image worker did not enter a private mount namespace")
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    subprocess.run(["mount", "-t", "proc", "proc", "/proc"], check=True)
    if os.getpid() != 1 or os.readlink("/proc/self") != "1":
        raise RuntimeError("image worker procfs does not describe its PID namespace")
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        for line in stream:
            fields = line.split()
            separator = fields.index("-")
            if any(field.startswith(("shared:", "master:", "propagate_from:")) for field in fields[6:separator]):
                raise RuntimeError("image worker inherited a propagating mount")


def execute_command(request):
    os.umask(request["umask"])
    for identifier, soft, hard in request["rlimits"]:
        resource.setrlimit(identifier, (soft, hard))
    os.setgroups(request["groups"])
    os.setresgid(request["gid"], request["gid"], request["gid"])
    os.setresuid(request["uid"], request["uid"], request["uid"])
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    os.execvpe(request["command"][0], request["command"], request["environment"])


def run_pid1(request, outer_namespace):
    command_pid = None
    command_status = None
    error = None
    try:
        if os.getpid() != 1:
            raise RuntimeError("image worker is not PID 1")
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, request_cancel)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        if cancelled is None:
            os.unshare(os.CLONE_NEWNS)
            verify_private_mounts(outer_namespace)
            print("SONiC Bazel image worker: private namespaces ready", flush=True)
            os.chdir(request["cwd"])
            if cancelled is None:
                command_pid = os.fork()
                if command_pid == 0:
                    try:
                        execute_command(request)
                    except BaseException as failure:
                        print("SONiC Bazel image command failed to start: " + type(failure).__name__, file=sys.stderr, flush=True)
                        os._exit(127)
                while command_status is None and cancelled is None:
                    _, command_status = reap_children(command_pid, command_status)
                    if command_status is None:
                        time.sleep(0.05)
    except BaseException as failure:
        error = type(failure).__name__ + ": " + str(failure)
    finally:
        try:
            command_status = finish_descendants(command_pid, command_status)
        except BaseException as failure:
            error = "process teardown failed: " + type(failure).__name__
    if error is not None:
        print("SONiC Bazel image worker: " + error, file=sys.stderr, flush=True)
        return 1
    if cancelled is not None:
        return 128 + cancelled
    return command_status if command_status is not None else 1


def read_request(path):
    if not re.fullmatch(r"/proc/[1-9][0-9]*/fd/[0-9]+", path):
        raise RuntimeError("invalid image worker request path")
    with open(path, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("image worker request must be an anonymous private file")
        if not 0 < info.st_size <= 4 * 1024 * 1024:
            raise RuntimeError("invalid image worker request size")
        request = json.load(stream)
    expected = {
        "schema", "stage", "cwd", "cwd_device", "cwd_inode", "command", "environment",
        "uid", "gid", "groups", "umask", "rlimits",
    }
    if not isinstance(request, dict) or set(request) != expected or request["schema"] != 1 or request["stage"] != "image":
        raise RuntimeError("invalid image worker request")
    if request["uid"] != info.st_uid or request["uid"] != int(os.environ["SUDO_UID"]) or request["gid"] != int(os.environ["SUDO_GID"]):
        raise RuntimeError("image worker caller credentials do not match")
    if not isinstance(request["gid"], int) or request["gid"] < 0 or not isinstance(request["groups"], list) or any(
        not isinstance(value, int) or value < 0 for value in request["groups"]
    ):
        raise RuntimeError("invalid image worker groups")
    if not isinstance(request["command"], list) or not request["command"] or any(
        not isinstance(value, str) or "\0" in value for value in request["command"]
    ):
        raise RuntimeError("invalid image worker command")
    if not isinstance(request["environment"], dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or "\0" in key or "\0" in value or "=" in key
        for key, value in request["environment"].items()
    ):
        raise RuntimeError("invalid image worker environment")
    if not isinstance(request["umask"], int) or not 0 <= request["umask"] <= 0o777:
        raise RuntimeError("invalid image worker umask")
    if not isinstance(request["rlimits"], list) or any(
        not isinstance(value, list) or len(value) != 3 or any(not isinstance(item, int) for item in value)
        for value in request["rlimits"]
    ):
        raise RuntimeError("invalid image worker resource limits")
    root = Path(request["cwd"])
    if not root.is_absolute() or root.resolve() != root:
        raise RuntimeError("invalid image worker working directory")
    root_info = root.stat()
    if (root_info.st_dev, root_info.st_ino) != (request["cwd_device"], request["cwd_inode"]):
        raise RuntimeError("image worker working directory changed")
    return request


def supervise(request):
    outer_namespace = os.stat("/proc/self/ns/mnt").st_ino
    os.unshare(os.CLONE_NEWPID)
    pid = os.fork()
    if pid == 0:
        os._exit(run_pid1(request, outer_namespace))
    reaped = False
    cancel_started = None
    kill_sent = False
    try:
        while True:
            if cancelled is not None and cancel_started is None:
                cancel_started = time.monotonic()
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if cancel_started is not None and not kill_sent and time.monotonic() - cancel_started >= 10:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                kill_sent = True
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                reaped = True
                print("SONiC Bazel image worker: PID 1 reaped", flush=True)
                return 128 + cancelled if cancelled is not None else command_returncode(status)
            time.sleep(0.05)
    finally:
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                # Do not return to outer cleanup until the child is terminal.
                while True:
                    try:
                        os.waitpid(pid, 0)
                        break
                    except InterruptedError:
                        continue
                    except ChildProcessError:
                        break
                    except OSError:
                        time.sleep(1)


def main():
    if os.geteuid() != 0:
        raise RuntimeError("image worker supervisor requires root")
    if len(sys.argv) != 2:
        raise RuntimeError("image worker requires one request path")
    for name in ("unshare", "CLONE_NEWPID", "CLONE_NEWNS"):
        if not hasattr(os, name):
            raise RuntimeError("image worker requires Linux namespace support")
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_cancel)
    request = read_request(sys.argv[1])
    if cancelled is None:
        subprocess.run(
            ["python3", "-B", "scripts/bazel/native/host_snapshot.py", "assert-clean", "fsroot-vs"],
            cwd=request["cwd"], env=request["environment"], stdin=subprocess.DEVNULL, check=True,
        )
    return supervise(request) if cancelled is None else 128 + cancelled


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, TypeError, ValueError, KeyError) as error:
        print("SONiC Bazel image worker: " + str(error), file=sys.stderr)
        sys.exit(1)
