#!/usr/bin/env python3

"""prepare, package and verify chromium build inputs."""

import argparse
from pathlib import Path
import subprocess
import sys

from common import HOSTS, PLATFORMS, ROOT, validate_version
from prepare import prepare
from package import package
from verify import verify


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--version", type=validate_version, required=True)
    command.add_argument(
        "--host", choices=HOSTS, help="producer host (default: current machine)"
    )
    command.add_argument("--platform", choices=PLATFORMS, default="desktop")
    command.add_argument("--workspace", type=Path, default=ROOT / ".work")
    command.add_argument("--cache", type=Path, help="shared git/cipd cache")
    command.set_defaults(function=prepare)

    command = commands.add_parser("package")
    command.add_argument("--workspace", type=Path, default=ROOT / ".work")
    command.add_argument("--output", type=Path, default=ROOT / "dist")
    command.add_argument(
        "--base-manifest",
        type=Path,
        help="linux base manifest, or desktop host manifest for a mobile overlay",
    )
    command.set_defaults(function=package)

    command = commands.add_parser("verify")
    command.add_argument("--manifest", type=Path, required=True)
    command.add_argument("--tree", type=Path)
    command.add_argument("--compare", type=Path)
    command.set_defaults(function=verify)
    return result


def main():
    args = parser().parse_args()
    try:
        args.function(args)
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
