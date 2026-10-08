"""Export pristine Git sources and prepared host dependencies."""

import ast
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import shutil
import subprocess
import tarfile
import time
import warnings

from common import ZSTD, canonical, file_record, log
from common import safe_relative, sha256, validate_version, write_json

EXCLUDED_PARTS = {
    ".git",
    ".cipd",
    ".cipd_bin",
    ".cipd_client",
    ".cipd_client_cache",
    ".versions",
    "__pycache__",
}
POLICY_NAMES = {
    "nonessential_dirs",
    "TEST_DIRS",
    "ESSENTIAL_FILES",
    "prune_directories",
    "purge_directories",
}
RETAINED_TOOL_DIRECTORIES = {
    "third_party/dawn/tools/golang",
    "third_party/llvm-build",
    "third_party/node/linux",
    "third_party/rust-toolchain",
}


@dataclass(frozen=True)
class BaseReference:
    manifest: dict
    records: dict


@contextmanager
def compressed_tar(path):
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as destination:
            process = subprocess.Popen(ZSTD, stdin=subprocess.PIPE, stdout=destination)
            try:
                with tarfile.open(
                    fileobj=process.stdin, mode="w|", format=tarfile.PAX_FORMAT
                ) as archive:
                    yield archive
                    log(f"finishing {path.name}")
                process.stdin.close()
                while process.poll() is None:
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        log(
                            f"compressing {path.name}: {temporary.stat().st_size / 1024**3:.2f} gib"
                        )
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, ZSTD)
            except BrokenPipeError:
                raise subprocess.CalledProcessError(process.wait() or 1, ZSTD) from None
            finally:
                if process.poll() is None:
                    process.terminate()
                with suppress(OSError):
                    process.stdin.close()
                process.wait()
        temporary.replace(path)
        log(f"packed {path.name}: {path.stat().st_size / 1024**3:.2f} gib")
    finally:
        temporary.unlink(missing_ok=True)


class ArchiveWriter:
    """Write tar members and their JSONL records together."""

    def __init__(self, stack, archive_files, timestamp):
        self.timestamp = timestamp
        self.contents = {}
        self.streams = {}
        self.archives = {}
        for role, archive in archive_files.items():
            log(f"packing {role}")
            self.archives[role] = stack.enter_context(compressed_tar(archive))
            path = archive.with_name(
                archive.name.removesuffix(".tar.zst") + ".contents.jsonl"
            )
            self.streams[role] = stack.enter_context(path.open("wb"))
            self.contents[role] = {"filename": path.name, "files": 0, "bytes": 0}

    def add(self, role, record, path=None, data=None):
        write_member(self.archives[role], record, self.timestamp, path, data)
        self.archives[role].members.clear()
        self.streams[role].write(canonical(record))
        self.contents[role]["files"] += 1
        self.contents[role]["bytes"] += record.get("size", 0)


def parse_policy_file(path, function=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(path.read_text())

    scope = tree.body
    if function:
        scope = next(
            node.body
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == function
        )
    scope = next((node.body for node in scope if isinstance(node, ast.With)), scope)

    values = {}
    for node in scope:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in POLICY_NAMES:
            values[target.id] = list(ast.literal_eval(node.value))
    return values


def load_upstream_policy(build_dir):
    """Read literal packaging rules from pinned upstream code, without executing it."""

    resources = build_dir / "recipes/recipe_modules/chromium/resources"
    exporter = resources / "export_tarball.py"
    publisher = build_dir / "recipes/recipes/publish_tarball.py"
    values = parse_policy_file(exporter)
    values.update(parse_policy_file(publisher, "export_lite_tarball"))
    if values.keys() != POLICY_NAMES:
        raise ValueError("unsupported upstream packaging policy")
    # Keep installed tools that upstream lite archives normally purge.
    values["purge_directories"] = [
        path
        for path in values["purge_directories"]
        if path not in RETAINED_TOOL_DIRECTORIES and not path.startswith("build/linux/")
    ]
    return values


def include_path(name, policy, directory=False):
    parts = PurePosixPath(name).parts
    basename = parts[-1]
    if any(part in EXCLUDED_PARTS for part in parts):
        return False
    if "out" in parts and "node_modules" not in parts:
        return False
    # Desktop macOS builds use Xcode's Swift toolchain.
    if name == "third_party/swift-toolchain" or name.startswith(
        "third_party/swift-toolchain/"
    ):
        return False
    # SDKs are installed locally; do not bundle host paths or private SDK downloads.
    if name == "build/win_toolchain.json" or name.startswith(
        ("build/mac_files/", "third_party/depot_tools/win_toolchain/")
    ):
        return False
    if "ChangeLog" in name or any(
        name == prefix or name.startswith(prefix + "/")
        for prefix in policy["purge_directories"]
    ):
        return False
    if directory:
        return True  # Traverse pruned directories to retain their metadata.
    if basename.endswith(".pyc") or basename == ".disable_auto_update":
        return False
    if basename.startswith(".") and basename.endswith((".tar.xz", ".tar.gz", ".zip")):
        return False
    metadata = (
        bool(re.search(r"\.(gn|gni|grd|grdp|isolate|pydeps)(\.[^ /]+)?$", basename))
        or name in policy["ESSENTIAL_FILES"]
    )
    licence = bool(re.search(r"copying|copyright|license", basename, re.I))
    for prefix in policy["nonessential_dirs"] + policy["TEST_DIRS"]:
        if name == prefix or name.startswith(prefix + "/"):
            return metadata
    for prefix in policy["prune_directories"]:
        if name == prefix or name.startswith(prefix + "/"):
            return metadata or licence
    return True


def tracked_files(checkout, state):
    """Map source paths to original Git modes and blob ids across all source repos."""
    result = {}
    modified = set()
    for name, info in state["inputs"]["revisions"].items():
        if not name or ":" in name or not (checkout / name / ".git").exists():
            continue
        repo = checkout / name
        relative = repo.relative_to(checkout / "src").as_posix()
        prefix = "" if relative == "." else relative + "/"
        revision = info["rev"]
        if not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
            raise ValueError(f"unpinned git input: {name}")
        if (
            subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            != revision
        ):
            raise ValueError(f"git revision changed: {name}")
        entries = subprocess.check_output(
            ["git", "-C", str(repo), "ls-tree", "-rz", revision]
        )
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            mode, kind, blob = header.decode().split()
            if kind != "blob":  # Parent gitlinks are handled by their own repository.
                continue
            path = os.fsdecode(path)
            result[prefix + path] = {
                "mode": int(mode, 8),
                "blob": blob,
                "repo": repo,
            }
        changes = subprocess.check_output(
            ["git", "-C", str(repo), "diff", revision, "--name-only", "-z"]
        )
        for path in changes.split(b"\0"):
            if path:
                modified.add(prefix + os.fsdecode(path))
    if not result:
        raise ValueError("no tracked source files")
    return result, modified


def write_member(archive, record, timestamp, path=None, data=None):
    info = tarfile.TarInfo(record["path"])
    info.mtime = timestamp
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mode = record["mode"]
    if record["type"] == "symlink":
        info.type = tarfile.SYMTYPE
        info.linkname = record["target"]
        archive.addfile(info)
    else:
        info.size = record["size"]
        with io.BytesIO(data) if data is not None else path.open("rb") as stream:
            archive.addfile(info, stream)


def package(args):
    export(args.workspace.resolve(), args.output.resolve(), args.base_manifest)


def load_preparation(workspace, output):
    checkout = workspace / "checkout"
    if output == checkout or checkout in output.parents:
        raise ValueError("output must be outside checkout")
    state = json.loads((workspace / "preparation.json").read_text())
    validate_version(state["version"])
    policy = load_upstream_policy(workspace / "tools/build")
    if any(state["policy"].get(name) != value for name, value in policy.items()):
        raise ValueError("packaging policy changed since prepare")
    state["policy"] = policy

    return checkout, state


def load_base_reference(base_manifest, output, state):
    host = state["host"]
    if host != "linux-x64" and base_manifest is None:
        raise ValueError(f"missing --base-manifest for {host}")
    if base_manifest is None:
        return None

    from verify import load_release

    directory, manifest, records, locked = load_release(base_manifest, base_only=True)
    if directory == output:
        raise ValueError("base and overlay need separate output directories")

    chromium_commit = state["inputs"]["chromium"]["commit"]
    if (
        manifest["host"] != "linux-x64"
        or manifest["version"] != state["version"]
        or manifest["timestamp"] != state["timestamp"]
        or any(
            locked["policy"].get(name) != value
            for name, value in state["policy"].items()
        )
        or locked["inputs"]["chromium"]["commit"] != chromium_commit
    ):
        raise ValueError("base does not match preparation")

    for record in (manifest["contents"]["base"], manifest["formats"]["zstd"]["base"]):
        source = directory / record["filename"]
        destination = output / record["filename"]
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)

    return BaseReference(
        manifest=manifest,
        records={record["path"]: record for record in records["base"]},
    )


def discover_paths(source, tracked, policy):
    paths = set(tracked)
    for directory, dirs, files in os.walk(source, followlinks=False):
        relative = Path(directory).relative_to(source)
        dirs[:] = [
            name
            for name in dirs
            if include_path((relative / name).as_posix(), policy, directory=True)
        ]
        symlinked_dirs = [
            name for name in dirs if (Path(directory) / name).is_symlink()
        ]
        paths.update((relative / name).as_posix() for name in files + symlinked_dirs)
    return paths


def prepared_record(path, name, git=None, mode=None):
    if not os.path.lexists(path):
        if git:
            raise ValueError(f"missing source file: {name}")
        return None
    if not path.is_file() and not path.is_symlink():
        return None
    return file_record(path, "src/" + name, mode)


def original_record(path, name, git, changed):
    data = link = None
    if changed or not os.path.lexists(path):
        data = subprocess.check_output(
            ["git", "-C", str(git["repo"]), "cat-file", "blob", git["blob"]]
        )
        if stat.S_ISLNK(git["mode"]):
            link, data = os.fsdecode(data), None
    record = file_record(path, "src/" + name, git["mode"], data, link)
    return record, data


def export_path(writer, source, name, git, changed, host, reference):
    path = source / name
    if reference:
        record = prepared_record(path, name, git, mode=git["mode"] if git else None)
        if record and record != reference.records.get(record["path"]):
            writer.add(host, record, path)
        return

    if git:
        record, data = original_record(path, name, git, changed)
        writer.add("base", record, path, data)

    if not git or changed:
        record = prepared_record(path, name, git)
        if record:
            writer.add(host, record, path)


def write_inputs(output, state):
    path = output / "inputs.json"
    inputs = {
        "version": state["version"],
        "host": state["host"],
        "targets": state["targets"],
        "configuration": state["configuration"],
        "inputs": state["inputs"],
        "policy": state["policy"],
        "timestamp": state["timestamp"],
    }
    write_json(path, inputs)
    return {"filename": path.name, "sha256": sha256(path, progress=True)}


def export(workspace, output, base_manifest=None):
    checkout, state = load_preparation(workspace, output)
    output.mkdir(parents=True, exist_ok=True)
    host = state["host"]
    version = state["version"]
    reference = load_base_reference(base_manifest, output, state)
    tracked, modified = tracked_files(checkout, state)
    (output / "manifest.json").unlink(missing_ok=True)
    source = checkout / "src"
    paths = discover_paths(source, tracked, state["policy"])

    roles = (host,) if reference else ("base", host)
    stem = f"chromium-{version}"
    archive_files = {role: output / f"{stem}-{role}.tar.zst" for role in roles}
    with ExitStack() as stack:
        writer = ArchiveWriter(stack, archive_files, state["timestamp"])
        next_report = time.monotonic() + 30
        for number, name in enumerate(sorted(paths), 1):
            if include_path(name, state["policy"]):
                safe_relative(name)
                export_path(
                    writer,
                    source,
                    name,
                    tracked.get(name),
                    name in modified,
                    host,
                    reference,
                )
            now = time.monotonic()
            if number % 50000 == 0 or now >= next_report:
                compressed = sum(
                    path.with_name(path.name + ".tmp").stat().st_size
                    for path in archive_files.values()
                )
                log(
                    f"scanned {number:,}/{len(paths):,} paths; {compressed / 1024**3:.2f} gib compressed"
                )
                next_report = now + 30
        log(f"scanned {len(paths):,} paths")

    content = writer.contents
    for record in content.values():
        record["sha256"] = sha256(output / record["filename"], progress=True)
    if reference:
        content["base"] = reference.manifest["contents"]["base"]
    manifest = {
        "schema": 1,
        "version": version,
        "host": host,
        "targets": state["targets"],
        "inputs": write_inputs(output, state),
        "timestamp": state["timestamp"],
        "contents": content,
        "formats": {
            "zstd": {
                role: {
                    "filename": path.name,
                    "sha256": sha256(path, progress=True),
                    "bytes": path.stat().st_size,
                }
                for role, path in archive_files.items()
            }
        },
    }
    if reference:
        manifest["formats"]["zstd"]["base"] = reference.manifest["formats"]["zstd"][
            "base"
        ]
    write_json(output / "manifest.json", manifest)
    update_checksums(output, manifest)
    log(f"packaged: {output / 'manifest.json'}")


def update_checksums(output, manifest):
    files = {manifest["inputs"]["filename"]: manifest["inputs"]["sha256"]}
    for record in manifest["contents"].values():
        files[record["filename"]] = record["sha256"]
    for record in manifest["formats"]["zstd"].values():
        files[record["filename"]] = record["sha256"]
    files["manifest.json"] = sha256(output / "manifest.json")
    (output / "SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in sorted(files.items()))
    )
