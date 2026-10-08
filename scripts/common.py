"""Shared paths, checksums, process handling, and file metadata."""

from contextlib import contextmanager, suppress
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import re
import signal
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
CHROMIUM_URL = "https://chromium.googlesource.com/chromium/src"
TOOL_URL = "https://chromium.googlesource.com/chromium/tools/{}.git"
HOSTS = ("linux-x64", "mac-x64", "mac-arm64", "win-x64")
ZSTD = ("zstd", "-q", "-9", "-T4", "-c")


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
                        size = temporary.stat().st_size / 1024**3
                        log(f"compressing {path.name}: {size:.2f} gib")
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


@contextmanager
def zstd_stream(path):
    process = subprocess.Popen(
        ["zstd", "-q", "-d", "-c", str(path)], stdout=subprocess.PIPE
    )
    try:
        yield process.stdout
        if process.wait():
            raise ValueError(f"could not read archive: {path}")
    finally:
        if process.poll() is None:
            process.terminate()
        process.stdout.close()
        process.wait()


def native_host():
    osname = {"linux": "linux", "darwin": "mac", "win32": "win"}.get(sys.platform)
    cpu = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower()
    )
    host = f"{osname}-{cpu}"
    if host not in HOSTS:
        raise ValueError(f"unsupported host: {sys.platform}/{platform.machine()}")
    return host


def log(message):
    print(message, flush=True)


def run(argv, cwd=None, env=None, capture=False):
    argv = list(map(str, argv))
    log("+ " + " ".join(argv))
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        start_new_session=os.name != "nt",
        stdout=subprocess.PIPE if capture else None,
    )
    try:
        output, _ = process.communicate()
    except BaseException:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
        process.wait()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, argv, output)
    return output


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical(value))
    temporary.replace(path)


def update_checksums(output, manifest):
    files = {manifest["inputs"]["filename"]: manifest["inputs"]["sha256"]}
    for group in (manifest["contents"], manifest["formats"]["zstd"]):
        for record in group.values():
            files[record["filename"]] = record["sha256"]
    files["manifest.json"] = sha256(output / "manifest.json")
    (output / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(files.items()))
    )


def sha256(path, progress=False):
    if progress:
        log(f"hashing {path.name}: {path.stat().st_size / 1024**3:.2f} gib")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if progress:
        log(f"hashed {path.name}")
    return digest


def safe_relative(name):
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\x00" in name
        or path.as_posix() != name
    ):
        raise ValueError(f"unsafe path: {name!r}")
    return path


def validate_version(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        raise ValueError("expected a four-part numeric version")
    return version


def file_record(path, name, mode=None, data=None, link=None):
    if link is not None or (data is None and path.is_symlink()):
        target = link if link is not None else os.readlink(path)
        if os.name == "nt":
            if PureWindowsPath(target).drive or PureWindowsPath(target).root:
                raise ValueError(f"absolute win symlink in export: {name} -> {target}")
            target = target.replace("\\", "/")
        # Absolute links cannot be relocated or independently reconstructed.
        if PurePosixPath(target).is_absolute():
            raise ValueError(f"absolute symlink in export: {name} -> {target}")
        return {"path": name, "type": "symlink", "mode": 0o777, "target": target}
    permissions = (
        0o755 if (mode if mode is not None else path.stat().st_mode) & 0o111 else 0o644
    )
    return {
        "path": name,
        "type": "file",
        "mode": permissions,
        "size": len(data) if data is not None else path.stat().st_size,
        "sha256": (
            hashlib.sha256(data).hexdigest() if data is not None else sha256(path)
        ),
    }
