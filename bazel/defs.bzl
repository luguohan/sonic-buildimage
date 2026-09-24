"""Local Bazel actions for the native SONiC build stages.

The native actions use the configured sonic-slave and its mounted checkout.
The launcher refreshes the source manifest before each Bazel invocation.
"""

def _native_stage_impl(ctx):
    outputs = ctx.outputs.output_paths
    args = ctx.actions.args()
    args.add("build")
    args.add("--spec", ctx.file.spec.path)
    args.add("--manifest", ctx.file.manifest.path)
    for target, logical_path in ctx.attr.inputs.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("%s must produce exactly one file" % target.label)
        args.add("--input", logical_path + "=" + files[0].path)
    prefix = ctx.label.package + "/" if ctx.label.package else ""
    for output in outputs:
        args.add("--output", output.short_path[len(prefix):] + "=" + output.path)
    input_files = [ctx.file.spec, ctx.file.manifest]
    transitive = [target[DefaultInfo].files for target in ctx.attr.inputs]
    ctx.actions.run(
        executable = ctx.executable._runner,
        arguments = [args],
        inputs = depset(input_files, transitive = transitive),
        outputs = outputs,
        mnemonic = ctx.attr.mnemonic,
        progress_message = "Building %{label}",
        execution_requirements = {
            "no-remote-exec": "1",
            "no-sandbox": "1",
        },
        use_default_shell_env = True,
    )
    return [DefaultInfo(files = depset(outputs))]

sonic_native_stage = rule(
    implementation = _native_stage_impl,
    attrs = {
        "inputs": attr.label_keyed_string_dict(allow_files = True),
        "manifest": attr.label(allow_single_file = True, mandatory = True),
        "mnemonic": attr.string(default = "SonicNativeStage"),
        "output_paths": attr.output_list(mandatory = True),
        "spec": attr.label(allow_single_file = True, mandatory = True),
        "_runner": attr.label(
            default = Label("//tools:native_action.py"),
            allow_files = True,
            executable = True,
            cfg = "exec",
        ),
    },
)
