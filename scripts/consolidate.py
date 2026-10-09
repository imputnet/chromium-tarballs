"""Move files shared by every host overlay into the base archive."""

from contextlib import ExitStack
from dataclasses import dataclass
import heapq
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import time

from common import (
    BUNDLES,
    HOSTS,
    bundle_role,
    bundle_roles,
    canonical,
    compressed_tar,
    link_or_copy,
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
    role: str
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


def load_bundle(directory, role, loaded):
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if bundle_role(manifest) != role:
        raise ValueError(f"unexpected bundle: {directory}")

    dependencies = () if role == HOSTS[0] else bundle_roles(manifest)[:-1]
    for dependency in dependencies:
        source = loaded[HOSTS[0] if dependency == "base" else dependency]
        info = manifest["formats"]["zstd"][dependency]
        if info != source.manifest["formats"]["zstd"][dependency]:
            raise ValueError(f"dependency mismatch: {role} needs {dependency}")
        if any(
            manifest[key] != source.manifest[key] for key in ("version", "timestamp")
        ):
            raise ValueError(f"dependency version mismatch: {role}")
        path = directory / info["filename"]
        if not path.exists():
            link_or_copy(source.archive(dependency), path)

    _, manifest, records, _ = load_release(manifest_path)
    base = record_map(records["base"]) if role == HOSTS[0] else loaded[HOSTS[0]].base
    return Bundle(
        directory=directory,
        role=role,
        manifest=manifest,
        base=base,
        overlay=record_map(records[role]),
    )


def load_bundles(directories):
    bundles = {}
    for role in BUNDLES:
        bundles[role] = load_bundle(directories[role], role, bundles)
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
                    member.mode = records[member.name]["mode"]
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


def install_dependency(bundle, source, role):
    if bundle is source:
        return
    link_or_copy(source.archive(role), bundle.archive(role))
    source_contents = source.manifest["contents"][role]
    shutil.copyfile(
        source.directory / source_contents["filename"],
        bundle.directory / source_contents["filename"],
    )
    bundle.manifest["contents"][role] = source_contents.copy()
    source_archive = source.manifest["formats"]["zstd"][role]
    bundle.manifest["formats"]["zstd"][role] = source_archive.copy()


def save_bundle(bundle):
    write_json(bundle.directory / "manifest.json", bundle.manifest)
    update_checksums(bundle.directory, bundle.manifest)


def consolidate_bundles(directories):
    bundles = load_bundles(directories)
    shared = shared_records([bundles[host] for host in HOSTS])
    log(f"shared prepared files: {len(shared):,}")
    if not shared:
        return bundles

    base = bundles[HOSTS[0]]
    original_base = base.archive("base")
    original_overlay = base.archive(base.role)
    base_records = base.base.copy()
    base_records.update(shared)
    write_archive(
        base,
        "base",
        base_records,
        [
            (original_base, base.base.keys() - shared.keys()),
            (original_overlay, shared),
        ],
    )

    for bundle in bundles.values():
        if bundle.role in HOSTS:
            remaining = bundle.overlay.keys() - shared.keys()
            bundle.overlay = {name: bundle.overlay[name] for name in remaining}
            write_archive(
                bundle,
                bundle.role,
                bundle.overlay,
                [(bundle.archive(bundle.role), remaining)],
            )
        for dependency in bundle_roles(bundle.manifest)[:-1]:
            source = base if dependency == "base" else bundles[dependency]
            install_dependency(bundle, source, dependency)
        bundle.base = base_records
        save_bundle(bundle)
    return bundles
