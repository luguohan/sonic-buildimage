#!/usr/bin/env python3
"""Focused behavior checks for the final native Docker-root ownership release."""

from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import driver
import native_action


class DockerRootCleanupTest(unittest.TestCase):
    def setUp(self):
        if os.getuid() == 0:
            self.skipTest("the cleanup caller must be an ordinary build user")
        self.temporary = tempfile.TemporaryDirectory(prefix="sonic-docker-root-cleanup-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "fsroot.docker.trixie"
        self.directory.mkdir()
        self.child = self.directory / "child"
        self.child.mkdir()
        self.payload = self.child / "payload"
        self.payload.write_bytes(b"retained Docker data\n")
        self.payload.chmod(0o600)
        self.directory.chmod(0o710)
        self.lock_path = self.root / "target/bazel/native-actions.lock"
        self.lock_path.parent.mkdir(parents=True)
        snapshot_id = "a" * 32
        (self.root / native_action.NATIVE_SNAPSHOT_MARKER).write_text(json.dumps({
            "schema": 2, "snapshot_id": snapshot_id,
        }))
        root_info = self.root.lstat()
        self.identity = {"schema": 1, "snapshot_id": snapshot_id, "device": root_info.st_dev, "inode": root_info.st_ino}
        self.directory_identity = (self.directory.stat().st_dev, self.directory.stat().st_ino)
        self.owner = (0, 0)
        self.commands = []
        self.pinned_fd = None
        self.alias_mismatch = False
        self.proc_mismatch = False
        self.mountpoints = {self.root, Path("/var/lib/docker")}
        self.operation = "release"
        self.environment = {"PATH": os.environ["PATH"], "LC_ALL": "C"}
        self.real_lstat = Path.lstat
        self.real_stat = os.stat
        self.real_fstat = os.fstat
        self.real_run = subprocess.run

    def reported(self, info, **changes):
        values = {name: getattr(info, name) for name in (
            "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_uid", "st_gid",
            "st_atime_ns", "st_mtime_ns", "st_ctime_ns",
        )}
        if (info.st_dev, info.st_ino) == self.directory_identity:
            values["st_uid"], values["st_gid"] = self.owner
        values.update(changes)
        return SimpleNamespace(**values)

    def lstat(self, path, *args, **kwargs):
        if path == Path("/var/lib/docker"):
            info = self.real_fstat(self.pinned_fd)
            return self.reported(info, st_ino=info.st_ino + int(self.alias_mismatch))
        info = self.real_lstat(path, *args, **kwargs)
        return self.reported(info) if (info.st_dev, info.st_ino) == self.directory_identity else info

    def fstat(self, descriptor):
        info = self.real_fstat(descriptor)
        if (info.st_dev, info.st_ino) == self.directory_identity:
            self.pinned_fd = descriptor
            return self.reported(info)
        return info

    def stat(self, path, *args, **kwargs):
        info = self.real_stat(path, *args, **kwargs)
        if (info.st_dev, info.st_ino) == self.directory_identity:
            mismatch = self.proc_mismatch and "/fd/" in str(path)
            return self.reported(info, st_ino=info.st_ino + int(mismatch))
        return info

    def assert_lock_held(self):
        with self.lock_path.open("a+") as other:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def command_run(self, command, **kwargs):
        self.assert_lock_held()
        self.assertEqual(kwargs, {"cwd": self.root, "env": self.environment, "check": True})
        self.commands.append(command)
        if command[3:5] == ["python3", "-B"]:
            self.assertEqual(command, [
                "sudo", "-n", "--", "python3", "-B", "scripts/bazel/native/host_snapshot.py", "assert-clean", "fsroot-vs",
            ])
            if self.operation == "unclean":
                raise subprocess.CalledProcessError(1, command)
            if self.operation == "replace":
                self.directory.rename(self.root / "detached")
                self.directory.mkdir(mode=0o710)
            return subprocess.CompletedProcess(command, 0)
        descriptor_path = command[-1]
        info = self.real_stat(descriptor_path)
        self.assertEqual((info.st_dev, info.st_ino), self.directory_identity)
        self.assertEqual(descriptor_path, "/proc/" + str(os.getpid()) + "/fd/" + str(self.pinned_fd))
        self.assertNotIn("-R", command)
        self.assertNotIn("--recursive", command)
        if command[3] == "chown":
            self.assertEqual(command[4:-1], [
                "--dereference", "--from=0:0", str(os.getuid()) + ":" + str(os.getgid()), "--",
            ])
            if self.operation == "chown-fails":
                raise subprocess.CalledProcessError(1, command)
            # Exercise the same procfs dereference on this user-owned fixture.
            actual_owner = str(os.getuid()) + ":" + str(os.getgid())
            self.real_run([
                "chown", "--dereference", "--from=" + actual_owner, actual_owner, "--", descriptor_path,
            ], **kwargs)
            if self.operation != "owner-unchanged":
                self.owner = (os.getuid(), os.getgid())
            if self.operation == "change-mode":
                os.chmod(descriptor_path, stat.S_IMODE(info.st_mode) & ~0o200)
        else:
            self.fail("unexpected privileged command: " + repr(command))
        return subprocess.CompletedProcess(command, 0)

    def patches(self):
        stack = ExitStack()
        stack.enter_context(mock.patch.object(Path, "lstat", lambda path, *args, **kwargs: self.lstat(path, *args, **kwargs)))
        stack.enter_context(mock.patch.object(os, "fstat", self.fstat))
        stack.enter_context(mock.patch.object(os, "stat", self.stat))
        stack.enter_context(mock.patch.object(subprocess, "run", self.command_run))
        stack.enter_context(mock.patch.object(native_action, "_native_mountpoints", side_effect=lambda: set(self.mountpoints)))
        return stack

    def release(self, identity=None):
        self.pinned_fd = None
        with self.patches():
            native_action.release_docker_root_ownership(self.root, identity or self.identity, self.environment)

    def assert_no_chown(self):
        self.assertFalse(any(command[3] == "chown" for command in self.commands))

    def test_release_pins_one_inode_and_preserves_captured_modes_and_contents(self):
        payload_state = (self.payload.read_bytes(), self.payload.stat().st_mode, self.payload.stat().st_uid, self.payload.stat().st_gid)
        for mode in (0o710, 0o750):
            with self.subTest(mode=oct(mode)):
                self.directory.chmod(mode)
                self.owner = (0, 0)
                self.operation = "release"
                self.commands.clear()
                mtime = self.directory.stat().st_mtime_ns
                self.release()
                self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), mode)
                self.assertEqual(self.directory.stat().st_mtime_ns, mtime)
                self.assertEqual((self.payload.read_bytes(), self.payload.stat().st_mode, self.payload.stat().st_uid, self.payload.stat().st_gid), payload_state)
                self.assertEqual([command[3] for command in self.commands], ["python3", "chown"])
                with self.assertRaises(OSError):
                    self.real_fstat(self.pinned_fd)
                with self.lock_path.open("a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_build_user_owner_is_a_guarded_noop(self):
        self.owner = (os.getuid(), os.getgid())
        for mode in (0o710, 0o755):
            with self.subTest(mode=oct(mode)):
                self.directory.chmod(mode)
                self.commands.clear()
                self.release()
                self.assertEqual([command[3] for command in self.commands], ["python3"])
                self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), mode)

    def test_refuses_invalid_snapshot_owner_type_and_unmovable_mode(self):
        invalid = dict(self.identity, inode=self.identity["inode"] + 1)
        with self.assertRaisesRegex(RuntimeError, "runtime identity"):
            self.release(invalid)
        self.owner = (os.getuid() + 1, os.getgid())
        with self.assertRaisesRegex(RuntimeError, "root or build-user owned"):
            self.release()
        self.owner = (0, 0)
        self.directory.chmod(0o500)
        with self.assertRaisesRegex(RuntimeError, "owner write access"):
            self.release()
        self.directory.chmod(0o710)
        self.directory.rename(self.root / "outside")
        self.directory.symlink_to(self.root / "outside", target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "root or build-user owned"):
            self.release()
        self.assertEqual(self.commands, [])

    def test_refuses_unclean_mount_alias_namespace_and_procfd_state(self):
        for case, pattern in (
            ("unclean", "returned non-zero"), ("nested", "still contains mounts"),
            ("missing-alias", "bind is missing"), ("wrong-alias", "bind does not match"),
            ("wrong-procfd", "procfs descriptor changed"),
        ):
            with self.subTest(case=case):
                self.commands.clear()
                self.operation = "unclean" if case == "unclean" else "release"
                self.mountpoints = {self.root, Path("/var/lib/docker")}
                if case == "nested":
                    self.mountpoints.add(self.root / "dockers/context/mount")
                elif case == "missing-alias":
                    self.mountpoints.remove(Path("/var/lib/docker"))
                self.alias_mismatch = case == "wrong-alias"
                self.proc_mismatch = case == "wrong-procfd"
                with self.assertRaisesRegex((RuntimeError, subprocess.CalledProcessError), pattern):
                    self.release()
                self.assert_no_chown()
        self.alias_mismatch = self.proc_mismatch = False
        self.mountpoints = {self.root, Path("/var/lib/docker")}
        self.commands.clear()
        with mock.patch.object(native_action, "_native_namespace_identity", side_effect=[
            {"mnt": (1, 2), "pid": (1, 3)}, {"mnt": (1, 4), "pid": (1, 3)},
        ]):
            with self.assertRaisesRegex(RuntimeError, "namespace changed"):
                self.release()
        self.assert_no_chown()

    def test_refuses_replacement_and_failed_ownership_readback(self):
        for operation, pattern in (("chown-fails", "returned non-zero"), ("owner-unchanged", "identity, mode, or owner"), ("change-mode", "identity, mode, or owner")):
            with self.subTest(operation=operation):
                self.operation = operation
                self.owner = (0, 0)
                self.directory.chmod(0o710)
                self.commands.clear()
                with self.assertRaisesRegex((RuntimeError, subprocess.CalledProcessError), pattern):
                    self.release()
        self.operation = "replace"
        self.owner = (0, 0)
        self.directory.chmod(0o710)
        self.commands.clear()
        with self.assertRaisesRegex(RuntimeError, "path or procfs descriptor changed"):
            self.release()
        self.assert_no_chown()
        self.assertEqual((self.root / "detached/child/payload").read_bytes(), b"retained Docker data\n")


class DriverCleanupInvocationTest(unittest.TestCase):
    def invoke(self, phase, phase_error=None, cleanup_error=None, invalid_identity=False):
        identity = {"schema": 1, "snapshot_id": "a" * 32, "device": 1, "inode": 2}
        request = {"schema": 1, "target": "vs", "phase": phase, "native_snapshot_identity": identity}
        events = []

        def run_phase(value):
            events.append(("phase", value["phase"]))
            os.environ["LATE_PHASE_VALUE"] = "changed"
            if phase_error is not None:
                raise phase_error

        def cleanup(root, actual_identity, environment):
            events.append(("cleanup", root, actual_identity, environment))
            if cleanup_error is not None:
                raise cleanup_error

        with tempfile.TemporaryDirectory(prefix="sonic-driver-cleanup-test-") as temporary:
            request_path = Path(temporary) / "request.json"
            request_path.write_text(json.dumps(request))
            loaded_identity = dict(identity, inode=3) if invalid_identity else identity
            with mock.patch.object(driver, "ROOT", Path("/sonic")), \
                    mock.patch.object(sys, "argv", ["driver.py", "--request", str(request_path)]), \
                    mock.patch.object(native_action, "load_native_snapshot_identity", return_value=loaded_identity), \
                    mock.patch.object(driver, "run_driver_phase", side_effect=run_phase), \
                    mock.patch.object(native_action, "release_docker_root_ownership", side_effect=cleanup), \
                    mock.patch.dict(os.environ, {"PATH": "/usr/bin", "EARLY_VALUE": "available"}, clear=True):
                try:
                    driver.main()
                except BaseException as error:
                    return events, error
        return events, None

    def test_both_phases_cleanup_on_success_cache_hit_and_failure(self):
        for phase in ("prepare", "build"):
            for failure in (False, True):
                with self.subTest(phase=phase, failure=failure):
                    original = RuntimeError("phase failed before or during Bazel") if failure else None
                    events, error = self.invoke(phase, phase_error=original)
                    self.assertIs(error, original)
                    self.assertEqual([event[0] for event in events], ["phase", "cleanup"])
                    self.assertEqual(events[-1][1], Path("/sonic"))
                    self.assertEqual(events[-1][3], {"PATH": "/usr/bin", "EARLY_VALUE": "available"})

    def test_cleanup_failure_keeps_original_failure_and_invalid_identity_does_not_cleanup(self):
        cleanup_error = RuntimeError("cleanup guard failed")
        events, error = self.invoke("build", cleanup_error=cleanup_error)
        self.assertIs(error, cleanup_error)
        self.assertEqual([event[0] for event in events], ["phase", "cleanup"])
        events, error = self.invoke("prepare", phase_error=RuntimeError("prepare failed"), cleanup_error=cleanup_error)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("prepare failed", str(error))
        self.assertIn("cleanup guard failed", str(error))
        events, error = self.invoke("build", invalid_identity=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
