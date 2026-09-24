#!/usr/bin/env python3
"""Clone the recorded host-sflow source from a local bare repository."""

import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


PUBLIC_URL = "https://github.com/sflow/host-sflow"
# The task-local input is a plain bare clone; reject configuration that can redirect Git.
CONFIG_KEYS = {"core.repositoryformatversion", "core.filemode", "core.bare", "remote.origin.url"}


def recorded_commit():
    versions = Path(__file__).resolve().parents[2] / "files/build/versions-public/default/versions-git"
    prefix = PUBLIC_URL + "=="
    commits = [line[len(prefix):].strip() for line in versions.read_text().splitlines() if line.startswith(prefix)]
    if len(commits) != 1 or re.fullmatch(r"[0-9a-f]{40}", commits[0]) is None:
        raise RuntimeError("expected one recorded host-sflow commit in versions-git")
    return commits[0]


def git(arguments, cwd=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0",
    })
    command = [
        "/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
    ] + arguments
    return subprocess.run(
        command, cwd=cwd, env=environment, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def standalone_storage(directory):
    pending = [directory]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("host-sflow Git storage must contain independent regular files: " + str(path))


def validate_repository(repository, tag, commit):
    repository = Path(repository).absolute()
    if repository.resolve(strict=True) != repository or not stat.S_ISDIR(repository.lstat().st_mode):
        raise RuntimeError("host-sflow source must be a local directory without symlink components")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None or re.fullmatch(r"v[0-9A-Za-z._-]+", tag) is None:
        raise RuntimeError("invalid host-sflow tag or recorded commit")
    if commit != recorded_commit():
        raise RuntimeError("host-sflow source commit differs from versions-git")
    standalone_storage(repository)
    for relative in ("commondir", "objects/info/alternates", "objects/info/http-alternates", "shallow"):
        path = repository / relative
        if path.exists() or path.is_symlink():
            raise RuntimeError("host-sflow source contains unsupported Git indirection: " + relative)
    config = repository / "config"
    keys = git(["config", "--file", str(config), "--no-includes", "--name-only", "--list"]).lower().splitlines()
    if set(keys) != CONFIG_KEYS or len(keys) != len(CONFIG_KEYS):
        raise RuntimeError("host-sflow source has unexpected Git configuration")
    origin = git(["config", "--file", str(config), "--no-includes", "--get", "remote.origin.url"])
    if origin != PUBLIC_URL:
        raise RuntimeError("host-sflow source does not record the expected public origin")
    prefix = ["--git-dir=" + str(repository)]
    if git(prefix + ["rev-parse", "--is-bare-repository", "--is-shallow-repository"]).splitlines() != ["true", "false"]:
        raise RuntimeError("host-sflow source must be a complete bare repository")
    selected = git(prefix + ["rev-parse", "--verify", "refs/tags/" + tag + "^{commit}"])
    if selected != commit:
        raise RuntimeError("host-sflow source tag differs from the recorded commit")
    git(prefix + ["fsck", "--full", "--strict", "--no-reflogs"])
    entries = git(prefix + ["ls-tree", "-r", commit]).splitlines()
    if any(entry.startswith("160000 ") for entry in entries):
        raise RuntimeError("host-sflow source has unsupported submodules")
    return {
        "repository": str(repository), "url": PUBLIC_URL, "tag": tag, "commit": commit,
        "tree": git(prefix + ["rev-parse", commit + "^{tree}"]),
    }


def clone_verified(repository, destination, tag, commit):
    identity = validate_repository(repository, tag, commit)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("host-sflow clone destination already exists")
    git([
        "clone", "--no-local", "--branch", tag, "--single-branch", "--no-tags", "--",
        identity["repository"], str(destination),
    ], Path(identity["repository"]))
    selected = git(["rev-parse", "HEAD", "refs/tags/" + tag + "^{commit}"], destination).splitlines()
    refs = git(["for-each-ref", "--format=%(refname)"], destination).splitlines()
    if selected != [commit, commit] or refs != ["refs/tags/" + tag]:
        raise RuntimeError("host-sflow clone did not select only the recorded source tag")
    git(["fsck", "--full", "--strict", "--no-reflogs"], destination)
    git(["checkout", "-b", "sflow", commit], destination)
    if git(["rev-parse", "HEAD"], destination) != commit or git(["status", "--porcelain", "--untracked-files=all"], destination):
        raise RuntimeError("host-sflow checkout differs from the recorded commit")
    standalone_storage(destination / ".git")
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    try:
        identity = clone_verified(args.repository, args.destination, args.tag, args.commit)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
        print("host-sflow source: " + detail, file=sys.stderr)
        return 1
    print("Using verified host-sflow source at " + identity["commit"], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
