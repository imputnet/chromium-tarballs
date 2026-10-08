"""Reclaim hosted-runner storage and select a workspace volume."""

import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys


def remove(path):
    if not path.exists():
        return
    print(f"removing: {path}", flush=True)
    if os.name == "nt":

        def retry(function, name, error):
            os.chmod(name, stat.S_IWRITE)
            function(name)

        shutil.rmtree(path, onerror=retry)
    else:
        subprocess.run(["sudo", "rm", "-rf", "--", str(path)], check=True)


def main():
    if os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise ValueError("requires a github-hosted runner")

    volumes = [Path(os.environ["RUNNER_TEMP"])]
    tool_cache = Path(os.environ["RUNNER_TOOL_CACHE"])
    for path in tool_cache.iterdir():
        if path.is_dir() and path.name != "Python":
            remove(path)

    if sys.platform == "linux":
        for name in (
            "/usr/local/lib/android",
            "/usr/share/dotnet",
            "/usr/local/swift",
            "/usr/share/swift",
            "/usr/local/.ghcup",
            "/opt/ghc",
            "/usr/local/share/boost",
            "/opt/az",
            "/opt/microsoft",
        ):
            remove(Path(name))
        subprocess.run(["docker", "system", "prune", "--all", "--force"], check=True)
        volumes.append(Path("/mnt"))
        subprocess.run(["df", "-h"], check=True)
    elif sys.platform == "darwin":
        xcodes = [
            path
            for path in Path("/Applications").glob("Xcode_*.app")
            if not path.is_symlink()
        ]
        selected = max(
            xcodes, key=lambda path: tuple(map(int, re.findall(r"\d+", path.stem)))
        )
        developer = selected / "Contents/Developer"
        subprocess.run(["sudo", "xcode-select", "--switch", str(developer)], check=True)
        os.environ["DEVELOPER_DIR"] = str(developer)
        with open(os.environ["GITHUB_ENV"], "a") as environment:
            environment.write(f"DEVELOPER_DIR={developer}\n")
        for path in xcodes:
            if path != selected:
                remove(path)
        remove(Path("/Users/runner/Library/Android"))
        volumes.extend(path for path in Path("/Volumes").iterdir() if path.is_dir())
        subprocess.run(["df", "-h"], check=True)
    elif os.name == "nt":
        for name in ("C:/Android", "C:/ghcup", "C:/Program Files/dotnet/sdk"):
            remove(Path(name))
        volumes.extend(Path(f"{letter}:/") for letter in "CDE")
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Volume"], check=True
        )

    volumes = [path for path in volumes if path.is_dir()]
    if os.name != "nt":
        volumes = [
            path for path in volumes if not os.statvfs(path).f_flag & os.ST_RDONLY
        ]
    for path in volumes:
        print(
            f"free: {path}: {shutil.disk_usage(path).free // 1024**3} gib", flush=True
        )
    volume = max(volumes, key=lambda path: shutil.disk_usage(path).free)
    workspace = volume / "ct"
    if os.name != "nt":
        subprocess.run(["sudo", "mkdir", "-p", str(workspace)], check=True)
        subprocess.run(
            ["sudo", "chown", f"{os.getuid()}:{os.getgid()}", str(workspace)],
            check=True,
        )
    else:
        workspace.mkdir(parents=True, exist_ok=True)
    print(f"workspace: {workspace}", flush=True)
    with open(os.environ["GITHUB_ENV"], "a") as environment:
        environment.write(f"PRODUCER_WORKSPACE={workspace.as_posix()}\n")
        environment.write(f"PRODUCER_CACHE={(workspace / 'cache').as_posix()}\n")
        environment.write(f"PRODUCER_OUTPUT={(workspace / 'dist').as_posix()}\n")


if __name__ == "__main__":
    main()
