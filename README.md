# chromium-tarballs

This repository builds prepared Chromium source archives used by Helium builds.
Each archive contains a pinned Chromium checkout with its dependencies and
toolchains already fetched for a specific host, so builds don't have to run
`gclient sync` or reach Google's infrastructure on their own.

New Chromium Stable releases are picked up automatically every few hours.
Each version is prepared, packaged, and verified on every supported host,
then published together as a single release.

## Release contents

Archives are split into layers that are extracted on top of each other into
a shared `chromium-<version>/` root:

- `chromium-<version>-base.tar.zst`: files shared by every host.
- `chromium-<version>-<host>.tar.zst`: host-specific files and toolchains for
  `linux-x64`, `mac-x64`, `mac-arm64`, and `win-x64`.
- `chromium-<version>-<platform>.tar.zst`: mobile overlays, applied on top of
  a host layer. `android` builds on `linux-x64`, `ios` builds on `mac-arm64`.
- `chromium-<version>-<role>.metadata.zip`: the manifest, recorded inputs,
  and full file lists for each layer.
- `SHA256SUMS` and `*.tar.zst.hashes`: sha256 digests of all release files.

Archives are hosted outside of GitHub due to their size. Download links are
listed in each release's notes, and checksums are attached to the release.

## Local building

Requires Python 3.11+ and `zstd`.

```sh
python3 scripts/chromium_tarballs.py prepare --version <version>
python3 scripts/chromium_tarballs.py package
python3 scripts/chromium_tarballs.py verify --manifest dist/manifest.json
```

Hosts other than `linux-x64` are packaged as overlays, so they need the
Linux base manifest and archive passed with `--base-manifest`.

## License

The code in this repository is licensed under GPL-3.0. See [LICENSE](LICENSE).

Chromium and its dependencies retain their original licenses.
