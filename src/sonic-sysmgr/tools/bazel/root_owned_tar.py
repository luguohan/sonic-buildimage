"""Normalize tar ownership without changing file contents or other metadata."""

import argparse
import tarfile


def root_owned_tar(source: str, destination: str) -> None:
    with tarfile.open(source, "r|*") as archive:
        with tarfile.open(destination, "w|", format=tarfile.PAX_FORMAT) as output:
            for member in archive:
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                # PAX fields otherwise override the numeric and name headers.
                member.pax_headers = {
                    key: value
                    for key, value in member.pax_headers.items()
                    if key not in {"uid", "gid", "uname", "gname"}
                }
                if member.isfile():
                    with archive.extractfile(member) as contents:
                        output.addfile(member, contents)
                else:
                    output.addfile(member)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    root_owned_tar(args.source, args.destination)
