#!/usr/bin/env python3

"""Record Git revisions needed to export pristine dependency sources."""

import json
from pathlib import Path
import sys


def dependency_lock(client, gclient):
    revisions = {}
    for dependency in client.subtree(False):
        if (
            not dependency.name
            or not dependency.should_process
            or dependency.GetScmName() != "git"
        ):
            continue
        dependency.PinToActualRevision()
        remote, revision = gclient.gclient_utils.SplitUrlRevision(dependency.url)
        revisions[dependency.name] = {"url": remote, "rev": revision}
    return {"revisions": revisions}


def main():
    depot, output = map(Path, sys.argv[1:])
    sys.path.insert(0, str(depot))
    import gclient

    options, _ = gclient.OptionParser().parse_args([])
    options.nohooks = True
    options.noprehooks = True
    client = gclient.GClient.LoadCurrentConfig(options)
    if client is None:
        raise ValueError("missing .gclient")
    if client.RunOnDeps("validate", []):
        raise ValueError("gclient validation failed")
    output.write_text(json.dumps(dependency_lock(client, gclient), sort_keys=True))


if __name__ == "__main__":
    main()
