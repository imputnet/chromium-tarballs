"""Fetch a pinned Chromium checkout and record its upstream dependencies."""

import json
import os
import subprocess
import sys
import time

from common import (
    ROOT,
    CHROMIUM_URL,
    TOOL_URL,
    PLATFORMS,
    log,
    native_host,
    run,
    write_json,
)
from package import load_upstream_policy


def configuration_for(host, platform="desktop"):
    if host not in PLATFORMS.get(platform, ()):
        raise ValueError(f"unsupported build platform: {platform} on {host}")
    target_os = [host.split("-")[0]]
    if platform != "desktop":
        target_os.append(platform)
    targets = ["x64"] if host == "win-x64" else ["x64", "arm64"]
    if platform == "android":
        targets = ["arm", "arm64", "x86", "x64"]
    elif platform == "ios":
        targets = ["arm64"]
    return {
        "target_os": target_os,
        "targets": targets,
        "depot_tools_commit": "dca727ba1f8fa8f1e3b12a815d065818bb61d395",
        "build_tools_commit": "f4615aeb0f8a2517390702416a17f3b10ab1e301",
        "custom_vars": {
            "checkout_configuration": "small",
            "checkout_pgo_profiles": True,
            "checkout_android": platform == "android",
            "checkout_ios": platform == "ios",
            "checkout_chromeos": False,
            "checkout_fuchsia": False,
        },
    }


def pinned_checkout(url, revision, destination, env):
    if not (destination / ".git").exists():
        destination.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q", destination], env=env)
        run(["git", "-C", destination, "remote", "add", "origin", url], env=env)
    for attempt in range(4):
        try:
            run(
                ["git", "-C", destination, "fetch", "--depth=2", "origin", revision],
                env=env,
            )
            break
        except subprocess.CalledProcessError:
            if attempt == 3:
                raise
            delay = 5 * 2**attempt
            log(f"fetch failed; retrying in {delay}s")
            time.sleep(delay)
    run(
        ["git", "-C", destination, "checkout", "--detach", "--force", "FETCH_HEAD"],
        env=env,
    )


def gclient_environment(depot, cache):
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": str(depot) + os.pathsep + environment["PATH"],
            "DEPOT_TOOLS_UPDATE": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CIPD_CACHE_DIR": str(cache / "cipd"),
            "GIT_CACHE_PATH": str(cache / "git"),
        }
    )
    if sys.platform == "win32":
        environment["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
    count = int(environment.get("GIT_CONFIG_COUNT", "0"))
    for key, value in (
        ("core.autocrlf", "false"),
        ("core.symlinks", "true"),
        ("core.longpaths", "true"),
    ):
        environment[f"GIT_CONFIG_KEY_{count}"] = key
        environment[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    environment["GIT_CONFIG_COUNT"] = str(count)
    return environment


def write_gclient_configuration(checkout, configuration, cache):
    config = {
        "solutions": [
            {
                "name": "src",
                "url": CHROMIUM_URL,
                "managed": True,
                "custom_vars": configuration["custom_vars"],
            }
        ],
        "target_os": configuration["target_os"],
        "target_os_only": True,
        "target_cpu": configuration["targets"],
        "target_cpu_only": True,
        "cache_dir": (
            None if os.environ.get("GITHUB_ACTIONS") == "true" else str(cache / "git")
        ),
    }
    (checkout / ".gclient").write_text(
        "\n".join(f"{key} = {value!r}" for key, value in config.items()) + "\n"
    )


def sync_checkout(depot, checkout, environment, revision):
    if sys.platform == "win32":
        command = ["cmd.exe", "/d", "/c", str(depot / "gclient.bat")]
    else:
        command = [sys.executable, depot / "gclient.py"]
    run(
        command
        + [
            "sync",
            "--no-history",
            "--nohooks",
            "--force",
            "--reset",
            "--delete_unversioned_trees",
            "--revision",
            f"src@{revision}",
        ],
        cwd=checkout,
        env=environment,
    )
    run(command + ["runhooks"], cwd=checkout, env=environment)


def prepare(args):
    host = native_host()
    if args.host and args.host != host:
        raise ValueError(f"host mismatch: expected {args.host}, got {host}")
    platform = args.platform
    configuration = configuration_for(host, platform)
    workspace = args.workspace.resolve()
    cache = args.cache.resolve() if args.cache else workspace / "cache"
    checkout = workspace / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    (workspace / "preparation.json").unlink(missing_ok=True)
    depot = workspace / "tools/depot_tools"
    build_tools = workspace / "tools/build"
    environment = gclient_environment(depot, cache)
    pinned_checkout(
        TOOL_URL.format("depot_tools"),
        configuration["depot_tools_commit"],
        depot,
        environment,
    )
    pinned_checkout(
        TOOL_URL.format("build"),
        configuration["build_tools_commit"],
        build_tools,
        environment,
    )
    pinned_checkout(
        CHROMIUM_URL, "refs/tags/" + args.version, checkout / "src", environment
    )
    chromium_commit = (
        run(["git", "-C", checkout / "src", "rev-parse", "HEAD"], capture=True)
        .decode()
        .strip()
    )
    write_gclient_configuration(checkout, configuration, cache)
    sync_checkout(depot, checkout, environment, chromium_commit)
    run(
        [
            sys.executable,
            ROOT / "scripts/inspect_deps.py",
            depot,
            checkout / "dependency-lock.json",
        ],
        cwd=checkout,
        env=environment,
    )
    policy = load_upstream_policy(build_tools, platform)
    inputs = json.loads((checkout / "dependency-lock.json").read_text())
    chromium_commit = inputs["revisions"]["src"]["rev"]
    timestamp = int(
        run(
            [
                "git",
                "-C",
                checkout / "src",
                "show",
                "-s",
                "--format=%ct",
                chromium_commit,
            ],
            capture=True,
        ).decode()
    )
    inputs["chromium"] = {
        "url": CHROMIUM_URL,
        "commit": chromium_commit,
    }
    state = {
        "version": args.version,
        "host": host,
        "platform": platform,
        "targets": configuration["targets"],
        "timestamp": timestamp,
        "configuration": configuration,
        "inputs": inputs,
        "policy": policy,
    }
    write_json(workspace / "preparation.json", state)
    log(f"prepared {args.version}: {checkout}")
