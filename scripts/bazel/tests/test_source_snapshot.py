#!/usr/bin/env python3
"""Behavioral tests for isolated local source staging."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


BAZEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BAZEL_DIR))
import source_snapshot

loader = importlib.machinery.SourceFileLoader("sonic_bazel_launcher", str(BAZEL_DIR / "run"))
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


def git(repository, *arguments, check=True):
    return subprocess.run(
        ["git", *arguments], cwd=repository, text=True, check=check,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def initialize(repository, name):
    repository.mkdir(parents=True)
    git(repository, "init", "--quiet", "--initial-branch=main")
    git(repository, "config", "user.name", "SONiC snapshot fixture")
    git(repository, "config", "user.email", "snapshot-fixture@example.invalid")
    git(repository, "remote", "add", "origin", "https://github.com/sonic-fixture/" + name + ".git")


def commit(repository, message):
    git(repository, "add", "--all")
    git(repository, "commit", "--quiet", "-m", message)
    return git(repository, "rev-parse", "HEAD").stdout.strip()


def identity(repository):
    return (
        git(repository, "rev-parse", "HEAD").stdout.strip(),
        git(repository, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
        git(repository, "status", "--porcelain=v1", "-z").stdout,
    )


class SourceSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sonic-source-snapshot-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        protocol = mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file:https:ssh"})
        protocol.start()
        self.addCleanup(protocol.stop)

    def repository(self, name):
        path = self.root / name
        initialize(path, name)
        (path / "tracked.txt").write_text("committed\n")
        commit(path, "initial fixture")
        return path

    def with_submodule(self):
        child_source = self.repository("child-source")
        first = git(child_source, "rev-parse", "HEAD").stdout.strip()
        (child_source / "tracked.txt").write_text("second commit\n")
        second = commit(child_source, "second child revision")
        source = self.repository("source")
        git(source, "submodule", "add", "--quiet", str(child_source), "deps/child")
        child = source / "deps/child"
        git(child, "remote", "set-url", "origin", "https://github.com/sonic-fixture/child.git")
        commit(source, "add child")
        return source, child, first, second

    def stage(self, source):
        with mock.patch.object(source_snapshot, "assert_not_in_use"):
            return source_snapshot.stage_checkout(source, self.root / "state")

    def test_initialization_preserves_initialized_checkout_and_local_changes(self):
        source, child, first, _second = self.with_submodule()
        git(child, "switch", "--quiet", "--create", "local-work", first)
        (child / "tracked.txt").write_text("local edit\n")
        (child / "untracked.txt").write_text("keep me\n")
        before_root = identity(source)
        before_child = identity(child)

        source_snapshot.ensure_submodules(source)

        self.assertEqual(identity(source), before_root)
        self.assertEqual(identity(child), before_child)
        self.assertEqual((child / "tracked.txt").read_text(), "local edit\n")
        self.assertEqual((child / "untracked.txt").read_text(), "keep me\n")

    def test_initialization_fills_missing_child_without_moving_existing_parent(self):
        leaf = self.repository("leaf-source")
        parent_source = self.repository("parent-source")
        git(parent_source, "submodule", "add", "--quiet", str(leaf), "nested/leaf")
        commit(parent_source, "add nested child")
        source = self.repository("source")
        git(source, "submodule", "add", "--quiet", str(parent_source), "deps/parent")
        commit(source, "add parent")
        parent = source / "deps/parent"
        git(parent, "switch", "--quiet", "--create", "local-parent")
        (parent / "tracked.txt").write_text("parent edit\n")
        before_root = identity(source)
        before_parent = identity(parent)

        source_snapshot.ensure_submodules(source)

        self.assertEqual(identity(source), before_root)
        self.assertEqual(identity(parent), before_parent)
        self.assertEqual((parent / "nested/leaf/tracked.txt").read_text(), "committed\n")

    def test_initialization_rejects_local_files_in_missing_submodule(self):
        source, child, _first, _second = self.with_submodule()
        git(source, "submodule", "deinit", "--force", "--", "deps/child")
        (child / "local.txt").write_text("preserve this file\n")
        before = identity(source)

        with self.assertRaisesRegex(RuntimeError, "contains local files"):
            source_snapshot.ensure_submodules(source)

        self.assertEqual(identity(source), before)
        self.assertEqual((child / "local.txt").read_text(), "preserve this file\n")

    def test_clone_copies_current_tree_and_has_independent_git_state(self):
        source, child, first, _second = self.with_submodule()
        (source / ".gitignore").write_text("ignored.txt\n")
        (source / "deleted.txt").write_text("delete after commit\n")
        (source / "relative-link").symlink_to("tracked.txt")
        commit(source, "add file cases")
        (source / "tracked.txt").write_text("current working contents\n")
        (source / "tracked.txt").chmod(0o755)
        (source / "deleted.txt").unlink()
        (source / "untracked.txt").write_text("untracked working contents\n")
        (source / "untracked.txt").chmod(0o640)
        (source / "ignored.txt").write_text("do not stage\n")
        (source / "absolute-link").symlink_to(source / "tracked.txt")
        git(child, "switch", "--quiet", "--create", "local-child", first)
        (child / "tracked.txt").write_text("child working contents\n")
        before_root = identity(source)
        before_child = identity(child)
        destination = self.root / "snapshot"

        source_snapshot.clone_repository(source, destination, source, destination)
        source_snapshot.verify_isolation(destination)

        self.assertEqual(identity(source), before_root)
        self.assertEqual(identity(child), before_child)
        self.assertEqual((destination / "tracked.txt").read_text(), "current working contents\n")
        self.assertEqual(stat.S_IMODE((destination / "tracked.txt").stat().st_mode), 0o755)
        self.assertEqual((destination / "untracked.txt").read_text(), "untracked working contents\n")
        self.assertEqual(stat.S_IMODE((destination / "untracked.txt").stat().st_mode), 0o640)
        self.assertFalse((destination / "deleted.txt").exists())
        self.assertFalse((destination / "ignored.txt").exists())
        self.assertEqual(os.readlink(destination / "relative-link"), "tracked.txt")
        self.assertEqual((destination / "absolute-link").resolve(), destination / "tracked.txt")
        self.assertEqual(identity(destination)[:2], before_root[:2])
        self.assertEqual(identity(destination / "deps/child")[:2], before_child[:2])
        self.assertEqual((destination / "deps/child/tracked.txt").read_text(), "child working contents\n")
        git(destination, "branch", "snapshot-only")
        self.assertNotEqual(git(source, "show-ref", "--verify", "refs/heads/snapshot-only", check=False).returncode, 0)

    def test_clone_preserves_detached_revisions(self):
        source, child, first, _second = self.with_submodule()
        git(source, "switch", "--quiet", "--detach")
        git(child, "switch", "--quiet", "--detach", first)
        before_root = identity(source)
        before_child = identity(child)
        destination = self.root / "snapshot"

        source_snapshot.clone_repository(source, destination, source, destination)

        self.assertEqual(identity(destination)[:2], before_root[:2])
        self.assertEqual(identity(destination / "deps/child")[:2], before_child[:2])
        self.assertEqual(identity(source), before_root)
        self.assertEqual(identity(child), before_child)

    def test_restage_discards_native_worktree_changes(self):
        source = self.repository("source")
        destination = self.stage(source)
        (destination / "tracked.txt").write_text("native recipe left a change\n")

        destination = self.stage(source)

        self.assertEqual((destination / "tracked.txt").read_text(), "committed\n")

    def test_restage_restores_native_submodule_revision(self):
        source, child, first, _second = self.with_submodule()
        destination = self.stage(source)
        git(destination / "deps/child", "switch", "--quiet", "--detach", first)

        destination = self.stage(source)

        self.assertEqual(identity(destination / "deps/child")[:2], identity(child)[:2])

    def test_reuse_accepts_only_the_exact_approved_prepared_state(self):
        source, child, first, _second = self.with_submodule()
        destination = self.stage(source)
        git(destination / "deps/child", "switch", "--quiet", "--detach", first)
        (destination / "tracked.txt").write_text("approved preparation change\n")
        input_receipt = json.loads((self.root / "state/native-source-input.json").read_text())
        prepared = source_snapshot.native_action.source_state(destination)
        approved_path = destination / "target/bazel/native-source-approved.json"
        approved_path.parent.mkdir(parents=True)
        approved_path.write_text(json.dumps({
            "schema": 1,
            "source_input_digest": input_receipt["digest"],
            "prepared_digest": source_snapshot.native_action.digest_bytes(
                source_snapshot.native_action.canonical_json(prepared)
            ),
        }))

        destination = self.stage(source)

        self.assertEqual((destination / "tracked.txt").read_text(), "approved preparation change\n")
        self.assertEqual(identity(destination / "deps/child")[:2], (first, "HEAD"))

        (destination / "tracked.txt").write_text("later unapproved change\n")
        destination = self.stage(source)

        self.assertEqual((destination / "tracked.txt").read_text(), "committed\n")
        self.assertEqual(identity(destination / "deps/child")[:2], identity(child)[:2])

    def test_copy_rejects_destination_symlink_before_creating_external_directories(self):
        source = self.root / "source-file"
        source.write_text("contents\n")
        destination = self.root / "destination"
        destination.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (destination / "link").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(RuntimeError):
            source_snapshot.copy_source_file(
                source, destination / "link/new-directory/file", self.root, destination,
            )

        self.assertFalse((outside / "new-directory").exists())

    def test_clone_rejects_source_parent_symlink_outside_checkout(self):
        source = self.repository("source")
        (source / ".gitignore").write_text("/directory\n")
        (source / "directory").mkdir()
        (source / "directory/file").write_text("tracked contents\n")
        git(source, "add", "--force", "directory/file")
        commit(source, "add ignored directory with tracked child")
        shutil.rmtree(source / "directory")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "file").write_text("external fixture contents\n")
        (source / "directory").symlink_to(outside, target_is_directory=True)
        destination = self.root / "snapshot"

        with self.assertRaises(RuntimeError):
            source_snapshot.clone_repository(source, destination, source, destination)

        self.assertFalse((destination / "directory/file").exists())

    def test_source_inventory_does_not_read_through_external_parent_symlink(self):
        source = self.root / "source"
        source.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "file").write_text("external fixture contents\n")
        (source / "link").symlink_to(outside, target_is_directory=True)

        with mock.patch.object(source_snapshot.native_action, "digest_file", side_effect=AssertionError("external read")):
            with self.assertRaisesRegex(RuntimeError, "outside"):
                source_snapshot.native_action.source_entry(source, "link/file")

    def test_swss_staging_copies_current_files_and_replaces_removed_files(self):
        source = self.repository("swss")
        (source / "configure.ac").write_text("AC_INIT([fixture], [1])\n")
        (source / "debian").mkdir()
        (source / "debian/rules").write_text("#!/usr/bin/make -f\n")
        (source / "debian/rules").chmod(0o755)
        commit(source, "add SWSS shape")
        (source / "working-link").symlink_to("tracked.txt")
        (source / "working.txt").write_text("working file\n")
        state = self.root / "swss-state"
        first = launcher.sync_swss(source, state)
        destination = state / "swss-source"
        self.assertEqual((destination / "working.txt").read_text(), "working file\n")
        self.assertEqual(os.readlink(destination / "working-link"), "tracked.txt")
        self.assertEqual(stat.S_IMODE((destination / "debian/rules").stat().st_mode), 0o755)
        self.assertFalse((destination / ".git").exists())

        (source / "working.txt").unlink()
        (source / "tracked.txt").write_text("new contents\n")
        second = launcher.sync_swss(source, state)

        self.assertFalse((destination / "working.txt").exists())
        self.assertEqual((destination / "tracked.txt").read_text(), "new contents\n")
        self.assertNotEqual(first["digest"], second["digest"])

    def test_swss_staging_rejects_concurrent_edit_and_preserves_prior_receipt(self):
        source = self.repository("swss")
        (source / "configure.ac").write_text("AC_INIT([fixture], [1])\n")
        (source / "debian").mkdir()
        (source / "debian/rules").write_text("#!/usr/bin/make -f\n")
        commit(source, "add SWSS shape")
        copy_file = source_snapshot.copy_source_file
        state = self.root / "swss-state"
        launcher.sync_swss(source, state)
        previous_receipt = (state / "swss-source.json").read_bytes()

        def copy_then_edit(original, destination, source_root, destination_root):
            copy_file(original, destination, source_root, destination_root)
            if original == source / "tracked.txt":
                original.write_text("changed during staging\n")

        with mock.patch.object(source_snapshot, "copy_source_file", side_effect=copy_then_edit):
            with self.assertRaisesRegex(RuntimeError, "SWSS sources changed while staging"):
                launcher.sync_swss(source, state)

        staged = state / "swss-source/tracked.txt"
        self.assertEqual(staged.read_text(), "committed\n")
        self.assertEqual((state / "swss-source.json").read_bytes(), previous_receipt)

    def test_swss_staging_rejects_metadata_changes_during_copy(self):
        source = self.repository("swss")
        (source / "tracked.txt").chmod(0o644)
        (source / "configure.ac").write_text("AC_INIT([fixture], [1])\n")
        (source / "debian").mkdir()
        (source / "debian/rules").write_text("#!/usr/bin/make -f\n")
        (source / "working-link").symlink_to("tracked.txt")
        commit(source, "add SWSS shape")
        copy_file = source_snapshot.copy_source_file
        state = self.root / "swss-state"
        launcher.sync_swss(source, state)
        previous_receipt = (state / "swss-source.json").read_bytes()

        def copy_temporary_metadata(original, destination, source_root, destination_root):
            if original == source / "tracked.txt":
                original.chmod(0o755)
                copy_file(original, destination, source_root, destination_root)
                original.chmod(0o644)
            elif original == source / "working-link":
                original.unlink()
                original.symlink_to("configure.ac")
                copy_file(original, destination, source_root, destination_root)
                original.unlink()
                original.symlink_to("tracked.txt")
            else:
                copy_file(original, destination, source_root, destination_root)

        with mock.patch.object(source_snapshot, "copy_source_file", side_effect=copy_temporary_metadata):
            with self.assertRaises(RuntimeError):
                launcher.sync_swss(source, state)

        self.assertEqual(stat.S_IMODE((state / "swss-source/tracked.txt").stat().st_mode), 0o644)
        self.assertEqual(os.readlink(state / "swss-source/working-link"), "tracked.txt")
        self.assertEqual((state / "swss-source.json").read_bytes(), previous_receipt)


if __name__ == "__main__":
    unittest.main()
