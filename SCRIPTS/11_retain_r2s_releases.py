#!/usr/bin/env python3
"""Keep two complete R2S Release asset pairs; never delete releases or Git tags."""
import argparse
from datetime import datetime
import json
import os
import re
from urllib.request import Request, urlopen


NAME = re.compile(r"^R2S-GC404-(\d{4}-\d{2}-\d{2})-([A-Za-z0-9.-]+)-(ext4|sfs)\.zip$")


def plan(releases):
    groups = {}
    for release in releases:
        if release.get("draft"):
            continue
        for asset in release["assets"]:
            match = NAME.fullmatch(asset["name"])
            if not match:
                continue
            day, version, filesystem = match.groups()
            key = (release["id"], day, version)
            groups.setdefault(key, {}).setdefault(filesystem, []).append(asset)
    complete = []
    for key, files in groups.items():
        if set(files) != {"ext4", "sfs"} or any(len(v) != 1 for v in files.values()):
            continue
        pair = [files[fs][0] for fs in ("ext4", "sfs")]
        if any(a.get("state") != "uploaded" or a["size"] < 1024 * 1024 for a in pair):
            continue
        dates = [datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")) for a in pair]
        complete.append((max(dates), key, pair))
    complete.sort(key=lambda item: (item[0], item[1]), reverse=True)
    # A partial/failed upload must never evict the previous complete firmware.
    keep = [a for _, _, pair in complete[:2] for a in pair]
    delete = [a for _, _, pair in complete[2:] for a in pair]
    return {"keep": keep, "delete": delete, "complete_versions": len(complete)}


def api(path, method="GET"):
    repo = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid repository")
    request = Request("https://api.github.com/repos/" + repo + path, method=method, headers={
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "YAOF-R2S-retention",
    })
    with urlopen(request, timeout=30) as response:
        return json.load(response) if method == "GET" else None


def pages(path):
    result = []
    for page in range(1, 1001):
        batch = api(path + "?per_page=100&page=" + str(page))
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise RuntimeError("pagination limit exceeded; no cleanup performed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete only planned old R2S asset pairs")
    args = parser.parse_args()
    releases = pages("/releases")
    for release in releases:
        if not release.get("draft"):
            release["assets"] = pages("/releases/" + str(release["id"]) + "/assets")
    result = plan(releases)
    print(json.dumps({"apply": args.apply, **result}, indent=2))
    if args.apply:
        # Refuse to delete an asset changed after the plan was assembled.
        for asset in result["delete"]:
            path = "/releases/assets/" + str(asset["id"])
            current = api(path)
            for key in ("id", "name", "size", "created_at", "updated_at", "state"):
                if current.get(key) != asset.get(key):
                    raise RuntimeError("asset changed during cleanup; stopped")
            api(path, "DELETE")
            print("Deleted old R2S firmware asset: " + asset["name"])


if __name__ == "__main__":
    main()
