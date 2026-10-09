"""Move files shared by every host overlay into the base archive."""

from contextlib import ExitStack
from dataclasses import dataclass
import heapq
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import time

from common import (
    HOSTS,
    canonical,
    compressed_tar,
    log,
    sha256,
    update_checksums,
    write_json,
    zstd_stream,
)
from verify import check_archive, load_release


@dataclass
class Bundle:
    directory: Path
    host: str
    manifest: dict
    base: dict
    overlay: dict

    def archive(self, role):
        filename = self.manifest["formats"]["zstd"][role]["filename"]
        return self.directory / filename


def record_map(records):
    return {record["path"]: record for record in records}


def same_content(left, right):
    if right is None:
        return False
    left = left.copy()
    right = right.copy()
    left.pop("mode")
    right.pop("mode")
    return left == right


def load_bundle(directory, host, base_info, base_path):
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    base = manifest["formats"]["zstd"]["base"]
    if base != base_info:
        raise ValueError(f"base mismatch: {host}")

    path = directory / base["filename"]
    if not path.exists():
        os.link(base_path, path)

    _, manifest, records, _ = load_release(manifest_path)
    if manifest["host"] != host:
        raise ValueError(f"unexpected bundle: {directory}")
    return Bundle(
        directory,
        host,
        manifest,
        record_map(records["base"]),
        record_map(records[host]),
    )


def load_bundles(directories):
    first_directory = directories[HOSTS[0]]
    first_manifest = json.loads((first_directory / "manifest.json").read_text())
    base = first_manifest["formats"]["zstd"]["base"]
    base_path = first_directory / base["filename"]

    bundles = [load_bundle(directories[host], host, base, base_path) for host in HOSTS]
    version = bundles[0].manifest["version"]
    timestamp = bundles[0].manifest["timestamp"]
    if any(
        bundle.manifest["version"] != version
        or bundle.manifest["timestamp"] != timestamp
        for bundle in bundles[1:]
    ):
        raise ValueError("bundle versions do not match")
    return bundles


def shared_records(bundles):
    shared = {name: record.copy() for name, record in bundles[0].overlay.items()}
    for bundle in bundles[1:]:
        for name, record in list(shared.items()):
            candidate = bundle.overlay.get(name)
            if not same_content(record, candidate):
                del shared[name]
                continue
            record["mode"] |= candidate["mode"]
    return shared


def archive_members(path, selected):
    with zstd_stream(path) as stream:
        with tarfile.open(fileobj=stream, mode="r|", bufsize=1024 * 1024) as archive:
            for member in archive:
                if member.name in selected:
                    yield member.name, member, archive
                archive.members.clear()


def write_archive(bundle, role, records, sources):
    records = dict(sorted(records.items()))
    archive_info = bundle.manifest["formats"]["zstd"][role]
    log(f"repacking {role}: {len(records):,} files")

    with tempfile.TemporaryDirectory(dir=bundle.directory) as temporary:
        output = Path(temporary) / archive_info["filename"]
        with ExitStack() as stack:
            streams = [archive_members(path, names) for path, names in sources]
            for stream in streams:
                stack.callback(stream.close)
            members = heapq.merge(*streams, key=lambda item: item[0])

            with compressed_tar(output) as archive:
                next_report = time.monotonic() + 30
                for count, (_, member, source) in enumerate(members, 1):
                    if member.isfile():
                        with source.extractfile(member) as data:
                            archive.addfile(member, data)
                    else:
                        archive.addfile(member)
                    archive.members.clear()
                    if count % 50000 == 0 or time.monotonic() >= next_report:
                        log(f"repacked {role}: {count:,}/{len(records):,} files")
                        next_report = time.monotonic() + 30

        check_archive(output, list(records.values()), bundle.manifest["timestamp"])
        archive_info.update(
            sha256=sha256(output, progress=True), bytes=output.stat().st_size
        )
        output.replace(bundle.archive(role))

    contents = bundle.manifest["contents"][role]
    content_path = bundle.directory / contents["filename"]
    with content_path.open("wb") as stream:
        for record in records.values():
            stream.write(canonical(record))
    contents.update(
        sha256=sha256(content_path),
        files=len(records),
        bytes=sum(record.get("size", 0) for record in records.values()),
    )


def install_base(bundle, source):
    if bundle is not source:
        bundle.archive("base").unlink()
        os.link(source.archive("base"), bundle.archive("base"))
        source_contents = source.manifest["contents"]["base"]
        shutil.copyfile(
            source.directory / source_contents["filename"],
            bundle.directory / source_contents["filename"],
        )
        bundle.manifest["contents"]["base"] = dict(source_contents)
        bundle.manifest["formats"]["zstd"]["base"] = dict(
            source.manifest["formats"]["zstd"]["base"]
        )


def save_bundle(bundle):
    write_json(bundle.directory / "manifest.json", bundle.manifest)
    update_checksums(bundle.directory, bundle.manifest)


def consolidate_bundles(directories):
    bundles = load_bundles(directories)
    shared = shared_records(bundles)
    log(f"shared prepared files: {len(shared):,}")
    if not shared:
        return

    base = bundles[0]
    original_base = base.archive("base")
    original_overlay = base.archive(base.host)
    write_archive(
        base,
        "base",
        base.base | shared,
        [
            (original_base, base.base.keys() - shared.keys()),
            (original_overlay, shared),
        ],
    )

    for bundle in bundles:
        remaining = bundle.overlay.keys() - shared.keys()
        write_archive(
            bundle,
            bundle.host,
            {name: bundle.overlay[name] for name in remaining},
            [(bundle.archive(bundle.host), remaining)],
        )
        install_base(bundle, base)
        save_bundle(bundle)
