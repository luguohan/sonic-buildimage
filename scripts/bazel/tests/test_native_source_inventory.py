#!/usr/bin/env python3
"""Behavioral tests for the owned native source inventory."""

import hashlib
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


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_action


def git(repository, *arguments):
    return subprocess.run(
        ["git", *arguments], cwd=repository, text=True, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def commit(repository, message):
    git(repository, "add", "--all")
    git(repository, "commit", "--quiet", "-m", message)
    return git(repository, "rev-parse", "HEAD")


class NativeSourceInventoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sonic-native-inventory-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.count = 0

    def repository(self, path):
        path.mkdir(parents=True)
        git(path, "init", "--quiet", "--initial-branch=main")
        git(path, "config", "user.name", "SONiC inventory fixture")
        git(path, "config", "user.email", "inventory-fixture@example.invalid")
        (path / "tracked.txt").write_text("initial source\n")
        commit(path, "initial fixture")
        return path

    def native_root(self):
        self.count += 1
        root = self.repository(self.directory / ("snapshot-" + str(self.count)))
        snapshot_id = format(self.count, "032x")
        (root / native_action.NATIVE_SNAPSHOT_MARKER).write_text(json.dumps({
            "schema": 2, "snapshot_id": snapshot_id,
        }))
        info = root.lstat()
        identity = {
            "schema": 1, "snapshot_id": snapshot_id,
            "device": info.st_dev, "inode": info.st_ino,
        }
        runtime = root / native_action.NATIVE_SNAPSHOT_RUNTIME
        runtime.parent.mkdir(parents=True)
        runtime.write_text(json.dumps(identity))
        return root, identity

    def add_submodule(self, root):
        child = self.repository(root / "deps/child")
        revision = git(child, "rev-parse", "HEAD")
        git(root, "update-index", "--add", "--cacheinfo", "160000," + revision + ",deps/child")
        git(root, "commit", "--quiet", "-m", "add independent submodule fixture")
        return child

    def state(self, root, identity):
        return native_action.source_state(root, context="native", snapshot_identity=identity)

    def assert_rejected_before_source_read(self, root, identity, pattern):
        with mock.patch.object(native_action, "digest_file", side_effect=AssertionError("source read before rejection")):
            with self.assertRaisesRegex(RuntimeError, pattern):
                self.state(root, identity)

    def test_caller_marker_does_not_enable_generated_repository_traversal(self):
        root, identity = self.native_root()
        self.repository(root / "generated")

        paths, repositories = native_action.git_sources(root)

        self.assertIn("generated/", paths)
        self.assertEqual([item["path"] for item in repositories], ["."])
        self.assertNotIn("role", repositories[0])
        with self.assertRaisesRegex(RuntimeError, "unsupported source file type"):
            native_action.source_state(root)
        with self.assertRaisesRegex(RuntimeError, "only valid for native"):
            native_action.source_state(root, context="caller", snapshot_identity=identity)

    def test_native_context_requires_valid_identity_and_rejects_copies(self):
        root, identity = self.native_root()
        self.assertEqual(native_action.load_native_snapshot_identity(root), identity)
        with self.assertRaisesRegex(RuntimeError, "invalid native snapshot identity"):
            native_action.source_state(root, context="native")
        for field, value in (("schema", True), ("snapshot_id", "A" * 32), ("device", True), ("inode", 0)):
            invalid = dict(identity)
            invalid[field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeError, "invalid native snapshot identity"):
                    native_action.validate_native_snapshot(root, invalid)
        copied = self.directory / "copied"
        shutil.copytree(root, copied)
        with self.assertRaisesRegex(RuntimeError, "does not match its runtime identity"):
            native_action.load_native_snapshot_identity(copied)
        linked = self.directory / "linked"
        linked.symlink_to(root, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "must be a directory"):
            native_action.validate_native_snapshot(linked, identity)

    def test_runtime_identity_and_marker_must_be_regular_files(self):
        root, identity = self.native_root()
        marker = root / native_action.NATIVE_SNAPSHOT_MARKER
        marker.write_text(json.dumps({"schema": 2.0, "snapshot_id": identity["snapshot_id"]}))
        with self.assertRaisesRegex(RuntimeError, "marker does not match"):
            native_action.validate_native_snapshot(root, identity)
        marker.write_text(json.dumps({"schema": 2, "snapshot_id": identity["snapshot_id"]}))
        runtime = root / native_action.NATIVE_SNAPSHOT_RUNTIME
        outside_runtime = self.directory / "runtime.json"
        runtime.rename(outside_runtime)
        runtime.symlink_to(outside_runtime)
        with self.assertRaisesRegex(RuntimeError, "regular non-symlink"):
            native_action.load_native_snapshot_identity(root)
        runtime.unlink()
        outside_runtime.rename(runtime)
        outside_marker = self.directory / "marker.json"
        marker.rename(outside_marker)
        marker.symlink_to(outside_marker)
        with self.assertRaisesRegex(RuntimeError, "regular non-symlink"):
            native_action.validate_native_snapshot(root, identity)

    def test_generated_source_revision_branch_and_set_invalidate_native_state(self):
        root, identity = self.native_root()
        child = self.add_submodule(root)
        generated = self.repository(child / "generated")
        initial = self.state(root, identity)

        self.assertEqual(
            [(item["path"], item["role"]) for item in initial["repositories"]],
            [(".", "root"), ("deps/child", "submodule"), ("deps/child/generated", "generated")],
        )
        self.assertIn("deps/child/generated/tracked.txt", initial["entries"])
        self.assertNotIn(native_action.NATIVE_SNAPSHOT_MARKER, initial["entries"])
        self.assertFalse(any(name.startswith("target/") for name in initial["entries"]))

        (generated / "tracked.txt").write_text("changed generated source\n")
        edited = self.state(root, identity)
        self.assertNotEqual(edited, initial)
        commit(generated, "generated source revision")
        committed = self.state(root, identity)
        self.assertNotEqual(committed, edited)
        git(generated, "switch", "--quiet", "--create", "native-branch")
        branched = self.state(root, identity)
        self.assertNotEqual(branched, committed)
        shutil.rmtree(generated)
        self.assertNotEqual(self.state(root, identity), branched)

    def test_runtime_identity_is_excluded_from_native_source_state(self):
        root, identity = self.native_root()
        original = self.state(root, identity)
        replacement = dict(identity, snapshot_id="f" * 32)
        (root / native_action.NATIVE_SNAPSHOT_MARKER).write_text(json.dumps({
            "schema": 2, "snapshot_id": replacement["snapshot_id"],
        }))
        (root / native_action.NATIVE_SNAPSHOT_RUNTIME).write_text(json.dumps(replacement))

        self.assertEqual(self.state(root, replacement), original)

    def test_generated_repository_rejects_gitlinks_and_listed_embedded_repositories(self):
        root, identity = self.native_root()
        generated = self.repository(root / "generated")
        leaf = self.repository(generated / "leaf")
        revision = git(leaf, "rev-parse", "HEAD")
        git(generated, "update-index", "--add", "--cacheinfo", "160000," + revision + ",leaf")
        shutil.rmtree(leaf)
        self.assert_rejected_before_source_read(root, identity, "must not contain gitlinks")

        root, identity = self.native_root()
        generated = self.repository(root / "generated")
        self.repository(generated / "leaf")
        self.assert_rejected_before_source_read(root, identity, "must not contain embedded repositories")

    def test_completed_p4_source_cleanup_preserves_outputs_and_restores_inventory(self):
        root, identity = self.native_root()
        output = root / "target/debs/trixie/p4lang-p4c_1.2.4.2-3_amd64.deb"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"completed package")
        paths = [root / "src/dash-sai/DASH", root / "src/p4lang/p4lang-p4c-1.2.4.2"]
        for path in paths:
            generated = self.repository(path)
            revision = git(generated, "rev-parse", "HEAD")
            git(generated, "update-index", "--add", "--cacheinfo", "160000," + revision + ",nested")
        self.assert_rejected_before_source_read(root, identity, "must not contain gitlinks")

        native_action.cleanup_p4_source_repositories(root, identity, "1.2.4.2")

        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(output.read_bytes(), b"completed package")
        self.state(root, identity)
        native_action.cleanup_p4_source_repositories(root, identity, "1.2.4.2")

    def test_completed_p4_source_cleanup_refuses_unclassified_and_mounted_paths(self):
        root, identity = self.native_root()
        ordinary = root / "src/dash-sai/DASH"
        ordinary.mkdir(parents=True)
        (ordinary / "source.txt").write_text("caller source\n")
        with self.assertRaisesRegex(RuntimeError, "not a generated repository"):
            native_action.cleanup_p4_source_repositories(root, identity, "1.2.4.2")
        self.assertEqual((ordinary / "source.txt").read_text(), "caller source\n")

        root, identity = self.native_root()
        generated = self.repository(root / "src/dash-sai/DASH")
        with mock.patch.object(native_action, "_native_mountpoints", return_value={generated / "mounted"}):
            with self.assertRaisesRegex(RuntimeError, "refuses a nested mount"):
                native_action.cleanup_p4_source_repositories(root, identity, "1.2.4.2")
        self.assertTrue(generated.is_dir())

    def test_every_reached_repository_requires_a_local_git_directory(self):
        root, identity = self.native_root()
        child = self.add_submodule(root)
        outside = self.directory / "external-child"
        child.rename(outside)
        child.symlink_to(outside, target_is_directory=True)
        self.assert_rejected_before_source_read(root, identity, "must not traverse a symlink")

        root, identity = self.native_root()
        generated = self.repository(root / "generated")
        outside_git = self.directory / "external-git"
        (generated / ".git").rename(outside_git)
        (generated / ".git").write_text("gitdir: " + str(outside_git) + "\n")
        self.assert_rejected_before_source_read(root, identity, "requires a local .git directory")

    def test_native_git_storage_rejects_indirection_links_and_special_files(self):
        cases = ("commondir", "alternates", "http-alternates", "symlink", "hardlink", "fifo")
        for case in cases:
            with self.subTest(case=case):
                root, identity = self.native_root()
                generated = self.repository(root / "generated")
                storage = generated / ".git"
                outside = self.directory / ("outside-" + case)
                if case == "commondir":
                    (storage / "commondir").write_text(str(outside) + "\n")
                    pattern = "unsupported (indirection|embedded repository)"
                elif case in {"alternates", "http-alternates"}:
                    (storage / "objects/info" / case).write_text(str(outside) + "\n")
                    pattern = "unsupported indirection"
                elif case == "symlink":
                    (storage / "objects").rename(outside)
                    (storage / "objects").symlink_to(outside, target_is_directory=True)
                    pattern = "contains a symlink"
                elif case == "hardlink":
                    fixture = storage / "hardlink-fixture"
                    fixture.write_text("linked Git storage\n")
                    os.link(fixture, outside)
                    pattern = "file with hard links"
                else:
                    os.mkfifo(storage / "fifo-fixture")
                    pattern = "contains a special file"
                self.assert_rejected_before_source_read(root, identity, pattern)

    def test_native_git_configuration_rejects_includes_and_worktree_redirection(self):
        root, identity = self.native_root()
        with (root / ".git/config").open("a") as stream:
            stream.write("\n[include]\n\tpath = " + str(self.directory / "outside-config") + "\n")
        self.assert_rejected_before_source_read(root, identity, "must not include external files")

        root, identity = self.native_root()
        git(root, "config", "extensions.worktreeConfig", "true")
        (root / ".git/config.worktree").write_text("[include]\n\tpath = " + str(self.directory / "outside-config") + "\n")
        self.assert_rejected_before_source_read(root, identity, "must not include external files")

        root, identity = self.native_root()
        outside = self.directory / "outside-worktree"
        outside.mkdir()
        git(root, "config", "core.worktree", str(outside))
        self.assert_rejected_before_source_read(root, identity, "worktree or storage leaves")

    def test_native_git_queries_ignore_inherited_git_redirection(self):
        root, identity = self.native_root()
        (root / "untracked.txt").write_text("native source\n")
        original = self.state(root, identity)
        outside = self.repository(self.directory / "outside-repository")
        configuration = self.directory / "outside-config"
        configuration.write_text("[core]\n\texcludesFile = " + str(self.directory / "ignore-all") + "\n")
        (self.directory / "ignore-all").write_text("*\n")
        inherited = {
            "GIT_DIR": str(outside / ".git"),
            "GIT_WORK_TREE": str(outside),
            "GIT_INDEX_FILE": str(outside / ".git/index"),
            "GIT_OBJECT_DIRECTORY": str(outside / ".git/objects"),
            "GIT_CONFIG_GLOBAL": str(configuration),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": str(outside),
        }

        with mock.patch.dict(os.environ, inherited):
            self.assertEqual(self.state(root, identity), original)

    def test_native_inventory_rejects_nested_git_mounts_and_repeated_repositories(self):
        root, identity = self.native_root()
        generated = self.repository(root / "generated")
        with mock.patch.object(native_action, "_native_mountpoints", return_value={root, generated / ".git"}):
            self.assert_rejected_before_source_read(root, identity, "Git storage contains a nested mount")

        listing = native_action.git_source_listing

        def repeated_listing(repository, root_repository=False, env=None):
            result = listing(repository, root_repository=root_repository, env=env)
            return result + b"generated\0" if repository == root else result

        with mock.patch.object(native_action, "git_source_listing", side_effect=repeated_listing):
            self.assert_rejected_before_source_read(root, identity, "repeats a repository")


class NativeContainerStaticInputTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sonic-container-static-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = b"frozen package bytes\n"
        self.source = self.root / "frozen"
        self.source.write_bytes(self.data)
        self.source.chmod(0o444)
        self.link = self.root / "bazel-input"
        self.link.symlink_to(self.source)
        self.destination = self.root / "target/package.deb"
        self.destination.parent.mkdir()

    def record(self, mode=0o644):
        return {"sha256": hashlib.sha256(self.data).hexdigest(), "size": len(self.data), "mode": mode}

    def test_staging_restores_frozen_bytes_and_original_modes(self):
        for mode in (0o644, 0o755, 0o555):
            with self.subTest(mode=oct(mode)):
                if self.destination.exists():
                    self.destination.chmod(0o600)
                self.destination.write_bytes(b"mutated native target\n")
                native_action.stage_container_static_input(self.link, self.destination, self.record(mode))
                self.assertEqual(self.destination.read_bytes(), self.data)
                self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), mode)
                self.assertFalse(self.source.samefile(self.destination))
                self.assertEqual(self.source.read_bytes(), self.data)
                self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o444)

    def test_staging_refuses_writable_alias_and_mismatched_inputs(self):
        self.destination.write_bytes(b"keep native target\n")
        for field, value in (("sha256", "0" * 64), ("size", len(self.data) + 1)):
            expected = self.record()
            expected[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "SHA-256 or size"):
                native_action.stage_container_static_input(self.link, self.destination, expected)
            self.assertEqual(self.destination.read_bytes(), b"keep native target\n")
        self.source.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "read-only regular"):
            native_action.stage_container_static_input(self.link, self.destination, self.record())
        self.source.chmod(0o444)
        alias = self.root / "hardlink"
        os.link(self.source, alias)
        with self.assertRaisesRegex(RuntimeError, "read-only regular"):
            native_action.stage_container_static_input(self.link, self.destination, self.record())
        alias.unlink()
        with self.assertRaisesRegex(RuntimeError, "separate"):
            native_action.stage_container_static_input(self.link, self.source, self.record())

    def test_container_spec_binds_static_hashes_sizes_and_modes(self):
        logical = "target/package.deb"
        manifest = {"dependency_artifacts": {logical: self.record()["sha256"]}}
        spec = {"stage": "container", "container_static_inputs": {logical: self.record()}}
        inputs = {logical: self.link}
        self.assertEqual(native_action.container_static_input_records(manifest, spec, inputs), spec["container_static_inputs"])
        invalid = json.loads(json.dumps(spec))
        invalid["container_static_inputs"][logical]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "invalid native container"):
            native_action.container_static_input_records(manifest, invalid, inputs)
        with self.assertRaisesRegex(RuntimeError, "do not match"):
            native_action.container_static_input_records(manifest, {"stage": "container"}, inputs)
        with self.assertRaisesRegex(RuntimeError, "only native container"):
            native_action.container_static_input_records(manifest, {**spec, "stage": "host"}, inputs)


if __name__ == "__main__":
    unittest.main()
