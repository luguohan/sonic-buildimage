#!/usr/bin/env python3
"""Serialize the evaluated public Make graph for the local Bazel launcher."""

import argparse
import json
import os
from pathlib import Path


def words(name):
    return os.environ.get(name, "").split()


def value(name):
    return os.environ.get(name, "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    selected = words("BAZEL_SELECTED_DOCKERS")
    containers = {}
    for name in selected:
        prefix = "BAZEL_DOCKER_" + name.replace("-", "_").replace(".", "_") + "_"
        containers[name] = {
            "path": value(prefix + "PATH"),
            "depends": words(prefix + "DEPENDS"),
            "load_dockers": words(prefix + "LOAD_DOCKERS"),
            "after": words(prefix + "AFTER"),
            "files": words(prefix + "FILES"),
            "wheels": words(prefix + "WHEELS"),
            "python_debs": words(prefix + "PYTHON_DEBS"),
            "install_debs": words(prefix + "INSTALL_DEBS"),
            "install_wheels": words(prefix + "INSTALL_WHEELS"),
            "debs_path": value(prefix + "DEBS_PATH"),
            "files_path": value(prefix + "FILES_PATH"),
        }
    result = {
        "schema": 1,
        "image": value("BAZEL_IMAGE"),
        "platform": value("BAZEL_PLATFORM"),
        "arch": value("BAZEL_ARCH"),
        "distro": value("BAZEL_DISTRO"),
        "swss": value("BAZEL_SWSS"),
        "swss_dbg": value("BAZEL_SWSS_DBG"),
        "p4c_version": value("BAZEL_P4C_VERSION"),
        "swss_depends": words("BAZEL_SWSS_DEPENDS"),
        "swss_rdepends": words("BAZEL_SWSS_RDEPENDS"),
        "swss_deb_build_options": value("BAZEL_SWSS_DEB_BUILD_OPTIONS"),
        "swss_deb_build_profiles": value("BAZEL_SWSS_DEB_BUILD_PROFILES"),
        "selected_dockers": selected,
        "installed_dockers": words("BAZEL_INSTALLED_DOCKERS"),
        "owned_dockers": words("BAZEL_OWNED_DOCKERS"),
        "local_packages": words("BAZEL_LOCAL_PACKAGES"),
        "remote_packages": words("BAZEL_REMOTE_PACKAGES"),
        "rfs_depends": words("BAZEL_RFS_DEPENDS"),
        "image_files": words("BAZEL_IMAGE_FILES"),
        "image_installs": words("BAZEL_IMAGE_INSTALLS"),
        "image_version": value("BAZEL_IMAGE_VERSION"),
        "build_timestamp": value("BAZEL_BUILD_TIMESTAMP"),
        "build_number": value("BAZEL_BUILD_NUMBER"),
        "features": {
            key.lower(): value("BAZEL_" + key)
            for key in (
                "ENABLE_SBOM", "ENABLE_ASAN", "INSTALL_DEBUG_TOOLS",
                "BUILD_MULTIASIC_KVM", "MULTIARCH_QEMU_ENVIRON",
                "CROSS_BUILD_ENVIRON", "POST_BUILD_HOOK",
                "IMAGE_SIGNATURE", "SECURE_UPGRADE_MODE",
            )
        },
        "config_flags": dict(item.split("=", 1) for item in words("BAZEL_CONFIG_FLAGS")),
        "containers": containers,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if not args.output.exists() or args.output.read_text() != data:
        args.output.write_text(data)


if __name__ == "__main__":
    main()
