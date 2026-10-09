"""Verify archive contents, extracted files, and independent reconstruction."""

from bisect import bisect_left
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import tarfile
import time

from common import bundle_roles, file_record, log, safe_relative, sha256, zstd_stream


def artifact_path(directory, filename):
    path = safe_relative(filename)
    if len(path.parts) != 1:
        raise ValueError(f"artifact must be a basename: {filename!r}")
    return directory / filename


def validate_manifest(manifest):
    if manifest.get("schema") != 1:
        raise ValueError("unsupported manifest schema")
    roles = bundle_roles(manifest)
    if set(manifest.get("formats", {})) != {"zstd"}:
        raise ValueError("expected zstd archives")
    expected_roles = set(roles)
    if set(manifest["contents"]) != expected_roles:
        raise ValueError(f"expected content lists: {', '.join(roles)}")
    if set(manifest["formats"]["zstd"]) != expected_roles:
        raise ValueError("incomplete archive pair")


def verify_artifacts(directory, manifest, roles):
    checks = [manifest["inputs"]]
    for role in roles:
        checks.extend((manifest["contents"][role], manifest["formats"]["zstd"][role]))
    for record in checks:
        artifact = artifact_path(directory, record["filename"])
        if sha256(artifact, progress=True) != record["sha256"]:
            raise ValueError(f"sha256 mismatch: {artifact.name}")


def load_content_records(directory, manifest, roles):
    records = {}
    archive_root = None
    for role in roles:
        content = manifest["contents"][role]
        path = artifact_path(directory, content["filename"])
        with path.open() as stream:
            values = [json.loads(line) for line in stream]
        total_bytes = sum(value.get("size", 0) for value in values)
        if len(values) != content["files"] or total_bytes != content["bytes"]:
            raise ValueError(f"content totals mismatch: {role}")

        previous = ""
        for value in values:
            name = value["path"]
            if name <= previous:
                raise ValueError(f"unsorted or duplicate paths: {role}")
            safe_relative(name)
            if archive_root is None:
                archive_root = name.split("/", 1)[0] + "/"
            if archive_root not in (
                "src/",
                f"chromium-{manifest['version']}/",
            ) or not name.startswith(archive_root):
                raise ValueError(f"path outside archive root: {name}")
            previous = name
        records[role] = values
    return records


def validate_links(records):
    """Reject links that escape the export or are traversed by another member."""

    all_names = sorted(
        {record["path"] for values in records.values() for record in values}
    )
    for values in records.values():
        for value in values:
            if value["type"] not in ("file", "symlink"):
                raise ValueError(f"unsupported file type: {value['type']}")
            if value["type"] != "symlink":
                continue

            target = value["target"]
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(value["path"]), target)
            )
            archive_root = value["path"].split("/", 1)[0] + "/"
            if PurePosixPath(target).is_absolute() or not resolved.startswith(
                archive_root
            ):
                raise ValueError(f"symlink escapes archive root: {value['path']}")

            prefix = value["path"] + "/"
            position = bisect_left(all_names, prefix)
            if position < len(all_names) and all_names[position].startswith(prefix):
                raise ValueError(f"path beneath symlink: {value['path']}")


def load_release(path, base_only=False):
    directory = path.resolve().parent
    manifest = json.loads(path.read_text())
    validate_manifest(manifest)
    roles = ("base",) if base_only else bundle_roles(manifest)
    verify_artifacts(directory, manifest, roles)

    inputs = json.loads(
        artifact_path(directory, manifest["inputs"]["filename"]).read_text()
    )
    if inputs["version"] != manifest["version"] or inputs["host"] != manifest["host"]:
        raise ValueError("input lock mismatch")
    if inputs.get("platform", "desktop") != manifest.get("platform", "desktop"):
        raise ValueError("input platform mismatch")

    records = load_content_records(directory, manifest, roles)
    validate_links(records)
    return directory, manifest, records, inputs


def check_archive(path, records, timestamp):
    if not path.name.endswith(".tar.zst"):
        raise ValueError(f"expected .tar.zst: {path}")
    log(f"checking {path.name}: {len(records):,} files")
    count = 0
    next_report = time.monotonic() + 30
    with zstd_stream(path) as stream:
        with tarfile.open(fileobj=stream, mode="r|", bufsize=1024 * 1024) as archive:
            for member in archive:
                if count == len(records):
                    raise ValueError(f"unexpected tar entry: {member.name}")
                expected = records[count]
                if (
                    member.name != expected["path"]
                    or member.mode != expected["mode"]
                    or member.mtime != timestamp
                    or member.uid
                    or member.gid
                    or member.uname != "root"
                    or member.gname != "root"
                ):
                    raise ValueError(f"tar metadata mismatch: {member.name}")
                if expected["type"] == "file":
                    if not member.isfile() or member.size != expected["size"]:
                        raise ValueError(f"tar file mismatch: {member.name}")
                    with archive.extractfile(member) as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if digest != expected["sha256"]:
                        raise ValueError(f"file checksum mismatch: {member.name}")
                elif (
                    expected["type"] != "symlink"
                    or not member.issym()
                    or member.linkname != expected["target"]
                ):
                    raise ValueError(f"symlink target mismatch: {member.name}")
                count += 1
                archive.members.clear()
                now = time.monotonic()
                if now >= next_report:
                    log(f"checked {path.name}: {count:,}/{len(records):,} files")
                    next_report = now + 30
        if count != len(records):
            raise ValueError(f"incomplete archive: {path}")
    log(f"checked {path.name}: {count:,} files")


def verify_tree(destination, records):
    expected = {record["path"]: record for record in records["base"]}
    for role, values in records.items():
        if role != "base":
            expected.update((record["path"], record) for record in values)
    for directory, dirs, files in os.walk(destination, followlinks=False):
        for name in files + [d for d in dirs if (Path(directory) / d).is_symlink()]:
            path = Path(directory) / name
            relative = path.relative_to(destination).as_posix()
            record = expected.pop(relative, None)
            if record is None:
                raise ValueError(f"unexpected file: {relative}")
            if (
                os.name != "nt"
                and not path.is_symlink()
                and stat.S_IMODE(path.stat().st_mode) != record["mode"]
            ):
                raise ValueError(f"mode mismatch: {relative}")
            if (
                file_record(
                    path,
                    relative,
                    record["mode"] if os.name == "nt" else None,
                )
                != record
            ):
                raise ValueError(f"file mismatch: {relative}")
    if expected:
        raise ValueError(f"missing files: {sorted(expected)[:5]}")


def verify(args):
    directory, manifest, records, _ = load_release(args.manifest)
    for role in bundle_roles(manifest):
        check_archive(
            artifact_path(directory, manifest["formats"]["zstd"][role]["filename"]),
            records[role],
            manifest["timestamp"],
        )
    if args.tree:
        verify_tree(args.tree.resolve(), records)
    if args.compare:
        _, other, other_records, _ = load_release(args.compare)
        if records != other_records:
            raise ValueError("file manifests differ")
        for role in bundle_roles(manifest):
            if (
                manifest["formats"]["zstd"][role]["sha256"]
                != other["formats"]["zstd"][role]["sha256"]
            ):
                raise ValueError("archive bytes differ")
    log(f"verified: {args.manifest}")
