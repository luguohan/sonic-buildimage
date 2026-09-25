# Incremental SONiC VS build with Bazel

This opt-in build uses Bazel for SWSS compilation and for the artifact chain
from the SWSS Debian packages through the containers and VS image. It keeps the
existing SONiC Docker and installer recipes as local Bazel actions, so the image
uses the same public build logic as the native build.

## Build

Use this checkout with a `sonic-swss` checkout containing its `bazel/` package.
The launcher defaults to the sibling `../sonic-swss` checkout. From the
`sonic-buildimage` root, run:

```sh
./scripts/bazel/run swss --jobs 8
./scripts/bazel/run container --jobs 8
./scripts/bazel/run vs --jobs 8
./scripts/bazel/run vs-kvm --jobs 8
```

Each target includes its prerequisites:

| Target | Result |
| --- | --- |
| `swss` | `swss_1.0.0_amd64.deb`, `swss-dbg_1.0.0_amd64.deb`, and package manifest |
| `container` | SWSS packages, shared SWSS layer, and `docker-orchagent.gz` |
| `vs` | SWSS packages, every selected container that depends on the SWSS layer, filesystem composition, and `sonic-vs.bin` |
| `vs-kvm` | The VS chain for the KVM configuration, plus `sonic-vs.img.gz` and `sonic-vs-uefi.img.gz` |

Use `--swss-source /path/to/sonic-swss` to select another local SWSS checkout.
Use repeated `--bazel-arg=--option=value` arguments for Bazel build options.
`--reprepare` asks native Make to recheck its prerequisite targets before
running Bazel.

Use `--native-dpkg-cache-method rwcache` to let native preparation read and
populate SONiC's existing package cache. The default is `none`, and the existing
SONiC check for matching slave tags applies to `rwcache`. Native stages owned
by Bazel always disable the native package cache. For matched benchmarks, use
`--native-dpkg-cache-method none` and set `SONIC_DPKG_CACHE_METHOD=none` and
`SONIC_DPKG_CACHE_METHOD_OVERRIDE=none` for the native comparison run.

The host needs the normal public SONiC build prerequisites, including Git,
Make, Docker, and `j2` from `jinjanator`. The launcher builds or reuses the
public Trixie `sonic-slave` image. The Docker environment must support the
privileged mounts used by the native SONiC build. The KVM target also needs the
native KVM build prerequisites, including `/dev/kvm`.

The first supported configuration is public VS on amd64 and Trixie with the
normal release package split. Cross builds, ASAN, debug images, multi-ASIC KVM,
SBOM output, installer post-build hooks, image signing, and remote SONiC package
manager inputs are not represented in this graph. The launcher reports these
configurations before building cached image stages.

## Artifact graph

```text
SWSS C++ source ──> per-file compile ──> program link ─┐
SWSS Rust source ──> locked offline Cargo build ──────┼─> swss + swss-dbg .deb
SWSS runtime data and Debian metadata ────────────────┘             │
                                                                   v
static native prerequisites ─────────────────────────────> SWSS container layer
              │                                                    │
              │                                                    v
              │                                         selected container descendants
              v                                                    │
host filesystem before container loading ─────────────────────────┤
                                                                   v
                                         fs.squashfs + dockerfs.tar.gz + fs.zip
                                                                   │
                                                                   v
                                                             sonic-vs.bin
                                                                   │
                                                   KVM configuration only
                                                                   v
                                              BIOS and UEFI sonic-vs images
```

The SWSS generator reads configured Automake values and creates native Bazel
C++ targets. A C++ edit recompiles the affected source files and relinks their
programs. The locked Cargo build is a separate action, so a C++ edit reuses its
output. Debian packaging runs the existing debhelper sequence over the Bazel
binaries and runtime files.

The buildimage inventory comes from the evaluated Make graph. Bazel owns each
selected container that directly uses SWSS or depends on another SWSS container
layer. The host filesystem action stops immediately before container loading.
Its inputs exclude the SWSS packages and those containers, so a SWSS edit can
reuse the host snapshot. The image action restores that snapshot, loads the
current containers, and runs the normal filesystem finalization and compression.
The installer and KVM conversions are separate downstream actions.

Native container and image actions run locally in the prepared slave because
they use its mounted checkout, Docker daemon, and filesystem mounts. They are
serialized around those shared native paths. Bazel can reuse their cached file
outputs. SWSS C++ actions can run in parallel.

## Preparation and cache inputs

The launcher copies the current tracked and untracked source files into an
independent checkout under `target/bazel/native-source`. This protects the
caller's branches and files from native recipes that change submodule revisions
or apply patches. Missing submodules are initialized only when their directories
contain no local files. Existing submodule checkouts retain their current state.

Preparation has two disposable slave invocations. The first builds or reuses
native prerequisites and records their content hashes. The second verifies that
receipt, installs the compile dependencies, configures SWSS, vendors the locked
Cargo dependencies, and generates the Bazel workspace. It captures the slave
image, installed tools and packages, evaluated Make configuration, and prepared
artifact hashes in the environment identity used by SWSS actions.

The generated workspace is owned by the launcher. Always use
`scripts/bazel/run` so the source manifest and configured SWSS inputs are
refreshed before Bazel runs. Native actions verify the manifest when they
execute. The native source manifest covers the public buildimage sources outside
SWSS because the reused Make recipes read many source paths. This can rebuild
native stages after an unrelated buildimage change; the main incremental path
is SWSS development.

The default workspace uses local Bazel disk and repository caches under
`target/bazel/native-source/target/bazel`. It does not require a cache server.
The native package files are content-hashed. Downloads performed by native
Docker and filesystem recipes still follow the configured public SONiC mirrors;
their repository metadata is not a Bazel input. Refresh those native inputs
through the normal SONiC rebuild process when changing mirror contents or
dependency selection.

## Outputs and logs

The launcher copies the requested packages, `docker-orchagent.gz`, and final
image outputs to:

```text
target/bazel/native-source/target/bazel/artifacts/<target>/
```

`build-manifest.json` records source identities, output sizes and hashes, the
environment identity, and the profile and execution-log paths. Intermediate
container and filesystem outputs remain under the generated Bazel workspace's
output tree. The build does not push branches or create pull requests.

## Validation

Run the focused buildimage tests with:

```sh
python3 -m unittest discover -s scripts/bazel/tests -v
```

The graph test requires Bazel 8.5.1 in `PATH` or `SONIC_BAZEL_TEST_BINARY`. It checks
generated artifact dependencies and verifies that a SWSS input edit reruns the
container and image actions while reusing the host action. These tests use stub
native actions; an end-to-end validation must also build the public slave,
packages, containers, and images and inspect the installed SWSS version.

For an incremental run, build the same target twice, edit one SWSS C++ source,
and build again. Compare the Bazel execution logs. The unchanged run should
reuse the Bazel outputs; the edit should recompile its affected C++ targets,
repackage SWSS, rebuild the SWSS container chain, and rebuild image composition
while reusing the host filesystem action.
