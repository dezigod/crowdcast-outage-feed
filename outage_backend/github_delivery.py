"""Publish reviewed JSON only; raw source and permission never leave private temp files."""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import urllib.error
import urllib.request

from .collect import collect, fetch, permission, PermissionRequired
from .normalize import date, SOURCE
from .store import Store

HEALTH = "collection-status.json"
FEED = "outages.json"
FEED_KEYS = {"schemaVersion", "snapshotID", "collectionStatus", "lastCollectionError", "source",
             "generatedAt", "refreshAfterSeconds", "staleAfterSeconds", "islands",
             "unassignedEventCount", "hasConflictingTotals", "qualityNotes"}
ERRORS = {None, "Source update incomplete", "Source access needs review", "Source rate limit",
          "Source download failed", "Source update could not be verified"}


def public_health(value):
    if not isinstance(value, dict) or value.get("status") not in {"ok", "failed", "blocked"}:
        raise ValueError("Invalid collection status")
    if value.get("error") not in ERRORS or date(value.get("nextAttemptAt")) is None:
        raise ValueError("Invalid collection status fields")
    return {key: value.get(key) for key in ("status", "error", "nextAttemptAt")}


def public_feed(value):
    """Explicit field allowlists prevent accidental publication of private source fields."""
    if not isinstance(value, dict) or set(value) != FEED_KEYS or value.get("schemaVersion") != 1:
        raise ValueError("Invalid public feed envelope")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {"name", "url", "checkedAt", "updatedAt", "viewerRefreshSeconds"}:
        raise ValueError("Invalid public source metadata")
    if source["url"] != SOURCE or date(source["updatedAt"]) is None or date(source["checkedAt"]) is None:
        raise ValueError("Unreviewed public source")
    if not isinstance(value.get("snapshotID"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["snapshotID"]):
        raise ValueError("Invalid source fingerprint")
    if value.get("collectionStatus") not in {"ok", "failed", "blocked"} or value.get("lastCollectionError") not in ERRORS:
        raise ValueError("Invalid feed collection status")
    if not isinstance(value.get("islands"), list) or len(value["islands"]) != 4:
        raise ValueError("Missing island coverage")
    ids = set()
    for island in value["islands"]:
        if not isinstance(island, dict) or set(island) != {"id", "coverage", "customersOut", "neighborhoods"}:
            raise ValueError("Unexpected island fields")
        ids.add(island["id"])
        if not isinstance(island["neighborhoods"], list):
            raise ValueError("Invalid neighborhoods")
        for area in island["neighborhoods"]:
            if not isinstance(area, dict) or set(area) != {"id", "island", "name", "locations", "sourceStatus", "startedAt", "estimatedRestoration", "affectedCustomers", "sourceEventIDs"}:
                raise ValueError("Unexpected neighborhood fields")
            if area["island"] != island["id"]:
                raise ValueError("Island mismatch")
    if ids != {"stThomas", "stCroix", "stJohn", "waterIsland"}:
        raise ValueError("Invalid island separation")
    return value


class PublishingStore(Store):
    def __init__(self, directory, previous, publish):
        super().__init__(directory)
        self.publish = publish
        if FEED in previous and HEALTH not in previous:
            raise ValueError("Stored feed is missing its collection state; review before resuming")
        if HEALTH in previous:
            super().write("health.json", public_health(previous[HEALTH]))
        if FEED in previous:
            super().write("feed.json", public_feed(previous[FEED]))

    def write(self, name, payload):
        super().write(name, payload)
        if name == "health.json":
            # The pre-request throttle and access-denial state must survive runner loss.
            # This callback finishes before the collector can make its next source request.
            outgoing = {HEALTH: public_health(payload)}
            feed = self.delivered()
            if feed is not None:
                outgoing[FEED] = public_feed(feed)
            self.publish(outgoing)


def run_once(previous, approved, publish, *, now=None, loader=fetch):
    now = now or datetime.now(timezone.utc)
    permission(approved, now)  # Invalid permission cannot publish or contact the source.
    with TemporaryDirectory(prefix="crowdcast-private-outages-") as directory:
        store = PublishingStore(directory, previous, publish)
        health = store.read("health.json", {})
        if health.get("status") == "blocked":
            return "blocked"
        next_attempt = date(health.get("nextAttemptAt"))
        if next_attempt and now < next_attempt:
            return "deferred"
        try:
            success = collect(store, approved, now=now, loader=loader)
        except PermissionRequired:
            if store.read("health.json", {}).get("status") == "blocked":
                return "blocked"
            raise
        health = store.read("health.json", {})
        if success:
            return "verified"
        return health.get("status", "unavailable")


class NoGitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        raise OSError("GitHub redirect refused")


class GitHubDataBranch:
    def __init__(self, repository, token, branch="data"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
            raise ValueError("GitHub repository and token required")
        self.root = "https://api.github.com/repos/" + repository
        self.token, self.branch = token, branch

    def request(self, path, *, payload=None, method="GET"):
        request = urllib.request.Request(self.root + path, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "CrowdCast-outage-feed"})
        with urllib.request.build_opener(NoGitHubRedirect).open(request, timeout=30) as response:
            body = response.read(4_000_001)
            if len(body) > 4_000_000:
                raise ValueError("GitHub response too large")
            return json.loads(body)

    def load(self):
        self.tip = self.request("/git/ref/heads/" + self.branch)["object"]["sha"]
        self.tree = self.request("/git/commits/" + self.tip)["tree"]["sha"]
        result = {}
        for name in (HEALTH, FEED):
            try:
                value = self.request("/contents/" + name + "?ref=" + self.branch)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    error.close()
                    continue
                raise
            if value.get("encoding") != "base64" or value.get("size", 4_000_001) > 2_000_000:
                raise ValueError("Invalid stored feed")
            result[name] = json.loads(base64.b64decode(value["content"], validate=False))
        self.previous = result
        return result

    def publish(self, files):
        if not files or not set(files).issubset({HEALTH, FEED}):
            raise ValueError("Only reviewed feed and collection status may be published")
        for name, value in files.items():
            (public_feed if name == FEED else public_health)(value)
        if all(self.previous.get(name) == value for name, value in files.items()):
            return
        tree = self.request("/git/trees", method="POST", payload={"base_tree": self.tree,
            "tree": [{"path": name, "mode": "100644", "type": "blob",
                      "content": json.dumps(value, sort_keys=True, indent=2) + "\n"}
                     for name, value in sorted(files.items())]})
        commit = self.request("/git/commits", method="POST", payload={
            "message": "Update reviewed outage feed and collection status",
            "tree": tree["sha"], "parents": [self.tip]})
        # Non-forcing update rejects a concurrent data-branch change. Never overwrite it.
        self.request("/git/refs/heads/" + self.branch, method="PATCH",
                     payload={"sha": commit["sha"], "force": False})
        self.tip, self.tree = commit["sha"], tree["sha"]
        self.previous.update(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--permission", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        approved = json.loads(args.permission.read_text())
        permission(approved, datetime.now(timezone.utc))
        remote = GitHubDataBranch(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", ""))
        outcome = run_once(remote.load(), approved, remote.publish)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, "Outage delivery could not complete; inspect configuration and prior feed. No raw source logged.\n")
    print("Collection result: " + outcome)
    if outcome in {"failed", "blocked", "unavailable"}:
        parser.exit(1, "Previous verified listings retained; collection needs review.\n")


if __name__ == "__main__":
    main()
