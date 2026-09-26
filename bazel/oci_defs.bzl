"""Sandboxed actions for assembling SONiC containers from retained OCI images."""

RetainedOciInfo = provider(
    doc = "Metadata for a retained OCI image and its baseline SWSS package.",
    fields = {
        "metadata": "The retained image metadata JSON file.",
    },
)

SwssOverlayInfo = provider(
    doc = "Metadata for a deterministic SWSS package overlay.",
    fields = {
        "metadata": "The SWSS overlay metadata JSON file.",
    },
)

def _validate_retained_contract_impl(ctx):
    stamp = ctx.actions.declare_file(ctx.label.name + ".stamp")
    args = ctx.actions.args()
    args.add("contract")
    args.add("--baseline", ctx.file.baseline.path)
    args.add("--current", ctx.file.current.path)
    args.add("--baseline-sha256", ctx.attr.baseline_sha256)
    args.add("--stamp", stamp.path)
    ctx.actions.run(
        executable = ctx.executable.tool,
        arguments = [args],
        inputs = [ctx.file.baseline, ctx.file.current],
        outputs = [stamp],
        mnemonic = "SonicOciContract",
        progress_message = "Validating retained OCI container inputs %{label}",
        execution_requirements = {"block-network": "1"},
    )
    return [DefaultInfo(files = depset([stamp]))]

validate_retained_contract = rule(
    implementation = _validate_retained_contract_impl,
    attrs = {
        "baseline": attr.label(allow_single_file = True, mandatory = True),
        "baseline_sha256": attr.string(mandatory = True),
        "current": attr.label(allow_single_file = True, mandatory = True),
        "tool": attr.label(
            allow_files = True,
            cfg = "exec",
            executable = True,
            mandatory = True,
        ),
    },
)

def _retained_oci_layout_impl(ctx):
    layout = ctx.actions.declare_directory(ctx.label.name + ".oci")
    metadata = ctx.actions.declare_file(ctx.label.name + ".metadata.json")
    args = ctx.actions.args()
    args.add("extract")
    args.add("--archive", ctx.file.src.path)
    args.add("--baseline-deb", ctx.file.baseline_deb.path)
    args.add("--archive-sha256", ctx.attr.archive_sha256)
    args.add("--baseline-sha256", ctx.attr.baseline_sha256)
    args.add("--image-name", ctx.attr.image_name)
    args.add("--layout", layout.path)
    args.add("--metadata", metadata.path)
    ctx.actions.run(
        executable = ctx.executable.tool,
        arguments = [args],
        inputs = [ctx.file.src, ctx.file.baseline_deb, ctx.file.contract],
        outputs = [layout, metadata],
        mnemonic = "SonicOciExtract",
        progress_message = "Extracting retained OCI layout %{label}",
        execution_requirements = {"block-network": "1"},
    )
    return [
        DefaultInfo(files = depset([layout])),
        RetainedOciInfo(metadata = metadata),
        OutputGroupInfo(metadata = depset([metadata])),
    ]

retained_oci_layout = rule(
    implementation = _retained_oci_layout_impl,
    attrs = {
        "archive_sha256": attr.string(mandatory = True),
        "baseline_deb": attr.label(allow_single_file = True, mandatory = True),
        "baseline_sha256": attr.string(mandatory = True),
        "contract": attr.label(allow_single_file = True, mandatory = True),
        "image_name": attr.string(mandatory = True),
        "src": attr.label(allow_single_file = True, mandatory = True),
        "tool": attr.label(
            allow_files = True,
            cfg = "exec",
            executable = True,
            mandatory = True,
        ),
    },
    provides = [RetainedOciInfo],
)

def _swss_package_overlay_impl(ctx):
    layer = ctx.actions.declare_file(ctx.label.name + ".tar")
    metadata = ctx.actions.declare_file(ctx.label.name + ".metadata.json")
    seed_metadata = [seed[RetainedOciInfo].metadata for seed in ctx.attr.seeds]
    args = ctx.actions.args()
    args.add("overlay")
    args.add("--baseline-deb", ctx.file.baseline_deb.path)
    args.add("--deb", ctx.file.deb.path)
    args.add("--baseline-sha256", ctx.attr.baseline_sha256)
    for seed in seed_metadata:
        args.add("--seed-metadata", seed.path)
    args.add("--layer", layer.path)
    args.add("--metadata", metadata.path)
    ctx.actions.run(
        executable = ctx.executable.tool,
        arguments = [args],
        inputs = [ctx.file.baseline_deb, ctx.file.deb] + seed_metadata,
        outputs = [layer, metadata],
        mnemonic = "SonicSwssOverlay",
        progress_message = "Creating SWSS OCI overlay %{label}",
        execution_requirements = {"block-network": "1"},
    )
    return [
        DefaultInfo(files = depset([layer])),
        SwssOverlayInfo(metadata = metadata),
        OutputGroupInfo(metadata = depset([metadata])),
    ]

swss_package_overlay = rule(
    implementation = _swss_package_overlay_impl,
    attrs = {
        "baseline_deb": attr.label(allow_single_file = True, mandatory = True),
        "baseline_sha256": attr.string(mandatory = True),
        "deb": attr.label(allow_single_file = True, mandatory = True),
        "seeds": attr.label_list(mandatory = True, providers = [RetainedOciInfo]),
        "tool": attr.label(
            allow_files = True,
            cfg = "exec",
            executable = True,
            mandatory = True,
        ),
    },
    provides = [SwssOverlayInfo],
)

def _deterministic_gzip_impl(ctx):
    args = ctx.actions.args()
    args.add("gzip")
    args.add("--input", ctx.file.src.path)
    args.add("--output", ctx.outputs.out.path)
    ctx.actions.run(
        executable = ctx.executable.tool,
        arguments = [args],
        inputs = [ctx.file.src],
        outputs = [ctx.outputs.out],
        mnemonic = "SonicOciGzip",
        progress_message = "Compressing OCI archive %{label}",
        execution_requirements = {"block-network": "1"},
    )
    return [DefaultInfo(files = depset([ctx.outputs.out]))]

deterministic_gzip = rule(
    implementation = _deterministic_gzip_impl,
    attrs = {
        "out": attr.output(mandatory = True),
        "src": attr.label(allow_single_file = True, mandatory = True),
        "tool": attr.label(
            allow_files = True,
            cfg = "exec",
            executable = True,
            mandatory = True,
        ),
    },
)
