"""Select recent Chromiumdash desktop Stable versions, without tag crawling."""

from functools import cache
import json
import os
from urllib.request import Request, urlopen
import sys

sys.path.insert(0, "scripts")
from common import validate_version

RELEASES_PER_PLATFORM = 3


def version_number(version):
    return tuple(map(int, version.split(".")))


def fetch(url, token=None):
    headers = {"User-Agent": "chromium-tarballs", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers), timeout=60) as response:
        payload = response.read().decode()
        return json.loads(payload.removeprefix(")]}'\n"))


@cache
def source_identity(version):
    # Ignore chrome/VERSION so version-only releases share a source identity.
    identity = []
    for directory, excluded in (("", "chrome"), ("chrome/", "VERSION")):
        tree = fetch(
            f"https://chromium.googlesource.com/chromium/src/+/{version}/{directory}?format=JSON"
        )
        if not any(entry["name"] == excluded for entry in tree["entries"]):
            raise ValueError(f"missing {directory}{excluded} in {version}")
        identity.append(
            tuple(
                sorted(
                    (entry["name"], entry["mode"], entry["type"], entry["id"])
                    for entry in tree["entries"]
                    if entry["name"] != excluded
                )
            )
        )
    return tuple(identity)


def candidates(requested):
    requested = validate_version(requested) if requested else None
    versions, linux = set(), set()
    for platform in ("Linux",) if requested else ("Linux", "Mac", "Windows"):
        releases = fetch(
            f"https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform={platform}&num={RELEASES_PER_PLATFORM}"
        )
        if not releases:
            raise ValueError(f"no stable release for {platform.lower()}")
        selected = {
            validate_version(release["version"])
            for release in releases[:RELEASES_PER_PLATFORM]
        }
        versions.update(selected)
        if platform == "Linux":
            linux = selected
    linux = sorted(linux, key=version_number)
    canonical = set()
    for version in sorted({requested} if requested else versions, key=version_number):
        current_number = version_number(version)
        for lower in linux:
            lower_number = version_number(lower)
            if lower_number[:3] == current_number[:3] and lower_number < current_number:
                if source_identity(lower) == source_identity(version):
                    print(f"{version}: using equivalent {lower}")
                    version = lower
                    break
        canonical.add(version)
    return sorted(canonical, key=version_number, reverse=True)


def release_tags():
    """List published and draft tags; the tag endpoint omits drafts."""
    url = (
        f"{os.environ['GITHUB_API_URL']}/repos/"
        f"{os.environ['GITHUB_REPOSITORY']}/releases"
    )
    tags = set()
    page = 1
    while True:
        releases = fetch(f"{url}?per_page=100&page={page}", os.environ["GH_TOKEN"])
        tags.update(release["tag_name"] for release in releases)
        if len(releases) < 100:
            return tags
        page += 1


def main():
    tags = release_tags()
    versions = []
    for version in candidates(os.environ.get("REQUESTED_VERSION", "").strip()):
        tag = f"chromium-{version}"
        build = tag not in tags
        if build:
            versions.append(version)
        print(f"{tag}: {'build' if build else 'exists'}")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"versions={json.dumps(versions)}\n")


if __name__ == "__main__":
    main()
