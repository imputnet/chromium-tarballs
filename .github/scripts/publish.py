"""Publish all verified host bundles together, using a draft until uploads finish."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import quote
import zipfile

sys.path.insert(0, "scripts")
from common import BUNDLES, sha256, validate_version
from consolidate import consolidate_bundles


def add_bundle_assets(bundle, assets):
    archive = bundle.archive(bundle.role)
    os.link(archive, assets / archive.name)
    version = bundle.manifest["version"]
    with zipfile.ZipFile(
        assets / f"chromium-{version}-{bundle.role}.metadata.zip",
        "w",
        zipfile.ZIP_DEFLATED,
    ) as metadata:
        for path in sorted(bundle.directory.iterdir()):
            if path.is_file() and not path.name.endswith(".tar.zst"):
                metadata.write(path, path.name)


def collect_assets(version):
    bundles = consolidate_bundles(
        {role: Path("bundles") / f"{version}-{role}" for role in BUNDLES}
    )
    base = bundles[BUNDLES[0]]
    if base.manifest["version"] != version:
        raise ValueError(f"unexpected version: {base.directory}")
    assets = Path("release-assets")
    assets.mkdir()
    archive = base.archive("base")
    os.link(archive, assets / archive.name)
    for bundle in bundles.values():
        add_bundle_assets(bundle, assets)

    return sorted(assets.iterdir())


def ensure_draft(command, tag, repository):
    existing = subprocess.run(
        command + ["view", tag, "--repo", repository, "--json", "isDraft"],
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        if not json.loads(existing.stdout)["isDraft"]:
            raise ValueError("release already published")
        return

    subprocess.run(
        command
        + [
            "create",
            tag,
            "--repo",
            repository,
            "--draft",
            "--target",
            os.environ["GITHUB_SHA"],
            "--title",
            f"chromium {tag.removeprefix('chromium-')}",
        ],
        check=True,
    )


def main():
    version = validate_version(os.environ["VERSION"])
    tag = f"chromium-{version}"
    bucket = os.environ["R2_BUCKET"]
    endpoint = os.environ["R2_ENDPOINT_URL"]
    public_url = os.environ["R2_PUBLIC_URL"].rstrip("/")
    if not all((bucket, endpoint, public_url)):
        raise ValueError("missing r2 configuration")
    files = collect_assets(version)
    checksums = Path("release-assets/SHA256SUMS")
    hashes = {path: sha256(path, progress=True) for path in files}
    checksums.write_text(
        "".join(f"{digest}  {path.name}\n" for path, digest in hashes.items()),
        encoding="ascii",
    )
    checksum_files = [checksums]
    for path, digest in hashes.items():
        if path.name.endswith(".tar.zst"):
            sidecar = path.with_name(path.name + ".hashes")
            sidecar.write_text(f"sha256  {digest}  {path.name}\n", encoding="ascii")
            checksum_files.append(sidecar)
    repository = os.environ["GITHUB_REPOSITORY"]
    command = ["gh", "release"]

    ensure_draft(command, tag, repository)
    for path in [*files, *checksum_files]:
        subprocess.run(
            [
                "aws",
                "s3",
                "cp",
                str(path),
                f"s3://{bucket}/{tag}/{path.name}",
                "--endpoint-url",
                endpoint,
                "--no-progress",
            ],
            check=True,
        )
    subprocess.run(
        command
        + ["upload", tag, "--repo", repository, "--clobber", *map(str, checksum_files)],
        check=True,
    )
    with tempfile.TemporaryDirectory() as temporary:
        notes = Path(temporary) / "downloads.md"
        notes.write_text(
            "downloads:\n\n"
            + "".join(
                f"- [{path.name}]({public_url}/{tag}/{quote(path.name)})\n"
                for path in [*files, checksums]
            ),
            encoding="ascii",
        )
        subprocess.run(
            command
            + [
                "edit",
                tag,
                "--repo",
                repository,
                "--notes-file",
                str(notes),
                "--draft=false",
                "--latest=false",
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
