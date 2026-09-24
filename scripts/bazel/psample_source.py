#!/usr/bin/env python3
"""Reproduce libpsample's recorded clone reset and recipe checkout locally."""

import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


PUBLIC_URL = "https://github.com/Mellanox/libpsample.git"
CONFIG_KEYS = {"core.repositoryformatversion", "core.filemode", "core.bare", "remote.origin.url"}


def recorded_commit():
    versions = Path(__file__).resolve().parents[2] / "files/build/versions-public/default/versions-git"
    prefix = PUBLIC_URL + "=="
    commits = [line[len(prefix):].strip() for line in versions.read_text().splitlines() if line.startswith(prefix)]
    if len(commits) != 1 or re.fullmatch(r"[0-9a-f]{40}", commits[0]) is None:
        raise RuntimeError("expected one recorded libpsample commit in versions-git")
    return commits[0]


def git(arguments, cwd=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
    })
    command = [
        "/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
    ] + arguments
    return subprocess.run(
        command, cwd=cwd, env=environment, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def local_directory(directory, description):
    directory = Path(directory).absolute()
    if directory.resolve(strict=True) != directory or not stat.S_ISDIR(directory.lstat().st_mode):
        raise RuntimeError(description + " must be a local directory without symlink components")
    return directory


def standalone_storage(directory):
    directory = local_directory(directory, "libpsample Git storage")
    pending = [directory]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("libpsample Git storage must contain independent regular files: " + str(path))
    for relative in ("commondir", "objects/info/alternates", "objects/info/http-alternates", "shallow"):
        path = directory / relative
        if path.exists() or path.is_symlink():
            raise RuntimeError("libpsample source contains unsupported Git indirection: " + relative)


def validate_selection(revision, expected_recorded_commit, expected_recipe_commit):
    if re.fullmatch(r"[0-9a-f]{7,40}", revision) is None:
        raise RuntimeError("invalid libpsample recipe revision")
    if any(re.fullmatch(r"[0-9a-f]{40}", value) is None for value in (expected_recorded_commit, expected_recipe_commit)):
        raise RuntimeError("invalid libpsample full commit identity")
    if expected_recorded_commit != recorded_commit():
        raise RuntimeError("libpsample recorded commit differs from versions-git")
    if not expected_recipe_commit.startswith(revision):
        raise RuntimeError("libpsample recipe commit differs from the recipe revision")


def selection_identity(prefix, revision, expected_recorded_commit, expected_recipe_commit, cwd=None):
    selected = git(prefix + [
        "rev-parse", "--verify", expected_recorded_commit + "^{commit}",
    ], cwd)
    recipe = git(prefix + ["rev-parse", "--verify", revision + "^{commit}"], cwd)
    if selected != expected_recorded_commit or recipe != expected_recipe_commit:
        raise RuntimeError("libpsample repository does not contain the exact recorded and recipe commits")
    return {
        "url": PUBLIC_URL,
        "recorded_commit": selected,
        "recorded_tree": git(prefix + ["rev-parse", selected + "^{tree}"], cwd),
        "recipe_revision": revision,
        "recipe_commit": recipe,
        "recipe_tree": git(prefix + ["rev-parse", recipe + "^{tree}"], cwd),
    }


def validate_repository(repository, revision, expected_recorded_commit, expected_recipe_commit):
    repository = local_directory(repository, "libpsample source")
    validate_selection(revision, expected_recorded_commit, expected_recipe_commit)
    standalone_storage(repository)
    config = repository / "config"
    keys = git(["config", "--file", str(config), "--no-includes", "--name-only", "--list"]).lower().splitlines()
    if set(keys) != CONFIG_KEYS or len(keys) != len(CONFIG_KEYS):
        raise RuntimeError("libpsample source has unexpected Git configuration")
    origin = git(["config", "--file", str(config), "--no-includes", "--get", "remote.origin.url"])
    if origin != PUBLIC_URL:
        raise RuntimeError("libpsample source does not record the expected public origin")
    prefix = ["--git-dir=" + str(repository)]
    if git(prefix + ["rev-parse", "--is-bare-repository", "--is-shallow-repository"]).splitlines() != ["true", "false"]:
        raise RuntimeError("libpsample source must be a complete bare repository")
    identity = selection_identity(prefix, revision, expected_recorded_commit, expected_recipe_commit)
    git(prefix + ["fsck", "--full", "--strict", "--no-reflogs"])
    for commit in (expected_recorded_commit, expected_recipe_commit):
        entries = git(prefix + ["ls-tree", "-r", commit]).splitlines()
        if any(entry.startswith("160000 ") for entry in entries):
            raise RuntimeError("libpsample source has unsupported submodules")
    return {"repository": str(repository), **identity}


def validate_checkout_storage(destination):
    destination = local_directory(destination, "libpsample checkout")
    storage = destination / ".git"
    standalone_storage(storage)
    for config in (storage / "config", storage / "config.worktree"):
        if config.exists():
            keys = git(["config", "--file", str(config), "--no-includes", "--name-only", "--list"]).lower().splitlines()
            if any(key.startswith(("include.", "includeif.")) for key in keys):
                raise RuntimeError("libpsample checkout Git configuration includes external files")
    reported = git([
        "rev-parse", "--path-format=absolute", "--show-toplevel", "--absolute-git-dir", "--git-common-dir",
    ], destination).splitlines()
    if reported != [str(destination), str(storage), str(storage)]:
        raise RuntimeError("libpsample checkout Git storage leaves its local directory")
    return destination


def clone_verified(repository, destination, revision, expected_recorded_commit, expected_recipe_commit):
    identity = validate_repository(repository, revision, expected_recorded_commit, expected_recipe_commit)
    destination = Path(destination).absolute()
    local_directory(destination.parent, "libpsample checkout parent")
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("libpsample clone destination already exists")
    git(["clone", "--no-local", "--", identity["repository"], str(destination)], Path(identity["repository"]))
    destination = validate_checkout_storage(destination)
    cloned = selection_identity([], revision, expected_recorded_commit, expected_recipe_commit, destination)
    if cloned != {key: value for key, value in identity.items() if key != "repository"}:
        raise RuntimeError("libpsample clone source identity differs from the verified repository")
    git(["reset", "--hard", expected_recorded_commit], destination)
    if git(["rev-parse", "HEAD"], destination) != expected_recorded_commit or git(["status", "--porcelain", "--untracked-files=all"], destination):
        raise RuntimeError("libpsample clone differs from the recorded reset state")
    git(["fsck", "--full", "--strict", "--no-reflogs"], destination)
    validate_checkout_storage(destination)
    return identity


def checkout_verified(destination, revision, expected_recorded_commit, expected_recipe_commit):
    validate_selection(revision, expected_recorded_commit, expected_recipe_commit)
    destination = validate_checkout_storage(destination)
    identity = selection_identity([], revision, expected_recorded_commit, expected_recipe_commit, destination)
    if git(["rev-parse", "HEAD"], destination) != expected_recorded_commit:
        raise RuntimeError("libpsample checkout no longer has the recorded clone reset state")
    git(["checkout", "-b", "libpsample", "-f", revision], destination)
    selected = git(["rev-parse", "HEAD", "--abbrev-ref", "HEAD"], destination).splitlines()
    if selected != [expected_recipe_commit, "libpsample"] or git(["status", "--porcelain", "--untracked-files=no"], destination):
        raise RuntimeError("libpsample checkout differs from the recipe commit")
    validate_checkout_storage(destination)
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    clone = subparsers.add_parser("clone")
    clone.add_argument("--repository", type=Path, required=True)
    checkout = subparsers.add_parser("checkout")
    for command in (clone, checkout):
        command.add_argument("--destination", type=Path, required=True)
        command.add_argument("--recipe-revision", required=True)
        command.add_argument("--recorded-commit", required=True)
        command.add_argument("--recipe-commit", required=True)
    args = parser.parse_args()
    try:
        values = (args.destination, args.recipe_revision, args.recorded_commit, args.recipe_commit)
        identity = clone_verified(args.repository, *values) if args.operation == "clone" else checkout_verified(*values)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        print("libpsample source: " + detail, file=sys.stderr)
        return 1
    print(
        "Using verified libpsample " + args.operation + " state: recorded " + identity["recorded_commit"]
        + ", recipe " + identity["recipe_revision"] + "=" + identity["recipe_commit"],
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
