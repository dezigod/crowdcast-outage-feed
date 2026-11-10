"""Publish reviewed JSON only; raw source and permission never leave private temp files."""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
import urllib.error
import urllib.request

from .collect import collect, fetch, permission, PermissionRequired
from .normalize import date, SOURCE
from .map_details import DETAIL_KEYS, validate_map_details
from .store import Store
from .rotation import ROTATION, check_rotation, public_rotation

HEALTH = "collection-status.json"
FEED = "outages.json"
MAX_PUBLIC_BYTES = 2_000_000
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
    if not isinstance(value, dict) or set(value) not in (FEED_KEYS, FEED_KEYS | DETAIL_KEYS) or value.get("schemaVersion") != 1:
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
    if DETAIL_KEYS.issubset(value):
        validate_map_details(value)
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

    def reserve(self, attempt):
        # Commit the throttle before any source access. The previous public
        # feed keeps its actual result while the new attempt is in flight.
        super().write("health.json", attempt)
        self.publish({HEALTH: public_health(attempt)})

    def write(self, name, payload):
        super().write(name, payload)
        if name == "health.json":
            # Commit the actual outcome and public snapshot together.
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


class DeliveryError(OSError):
    """Safe diagnostic: operation and HTTP code, never response bodies or tokens."""
    pass


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
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github.object+json" if path.startswith("/contents/") else "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "CrowdCast-outage-feed"})
        for attempt in range(2):
            try:
                with urllib.request.build_opener(NoGitHubRedirect).open(request, timeout=15) as response:
                    body = response.read(4_000_001)
                    if len(body) > 4_000_000:
                        raise ValueError("GitHub response too large")
                    return json.loads(body)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    raise
                retry = error.code in {500, 502, 503, 504} and not error.headers.get("Retry-After")
                error.close()
                if attempt or not retry:
                    raise DeliveryError(f"GitHub {method} operation failed (HTTP {error.code})") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt:
                    raise DeliveryError(f"GitHub {method} operation failed (network/timeout)") from None
            except OSError:
                raise DeliveryError(f"GitHub {method} operation could not complete") from None
            # Only GitHub operations use this retry. Tree/commit creation does
            # not move a branch; replaying the exact non-forcing ref update is safe.
            time.sleep(2)

    def load(self, *, include_rotation=False):
        self.tip = self.request("/git/ref/heads/" + self.branch)["object"]["sha"]
        self.tree = self.request("/git/commits/" + self.tip)["tree"]["sha"]
        result = {}
        for name in ((HEALTH, FEED, ROTATION) if include_rotation else (HEALTH, FEED)):
            try:
                value = self.request("/contents/" + name + "?ref=" + self.tip)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    error.close()
                    continue
                raise
            size = value.get("size")
            if type(size) is not int or not 0 <= size <= MAX_PUBLIC_BYTES:
                raise ValueError("Invalid stored feed size")
            if value.get("encoding") == "none":
                # GitHub Contents omits bytes above 1 MB. Read the exact blob
                # identified at this pinned commit; never follow download URLs.
                blob_sha = value.get("sha")
                if not isinstance(blob_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", blob_sha):
                    raise ValueError("Invalid stored feed blob")
                value = self.request("/git/blobs/" + blob_sha)
                if value.get("size") != size:
                    raise ValueError("Stored feed blob size mismatch")
            if value.get("encoding") != "base64":
                raise ValueError("Invalid stored feed encoding")
            body = base64.b64decode(value["content"], validate=False)
            if len(body) != size:
                raise ValueError("Stored feed size mismatch")
            result[name] = json.loads(body)
        self.previous = result
        return result

    def publish(self, files):
        if not files or not set(files).issubset({HEALTH, FEED, ROTATION}):
            raise ValueError("Only reviewed feed and collection status may be published")
        for name, value in files.items():
            {FEED: public_feed, HEALTH: public_health, ROTATION: public_rotation}[name](value)
        if all(self.previous.get(name) == value for name, value in files.items()):
            return
        serialized = {name: json.dumps(value, sort_keys=True, indent=2) + "\n"
                      for name, value in files.items()}
        if any(len(body.encode("utf-8")) > MAX_PUBLIC_BYTES for body in serialized.values()):
            raise ValueError("Public feed exceeds the reload size limit")
        tree = self.request("/git/trees", method="POST", payload={"base_tree": self.tree,
            "tree": [{"path": name, "mode": "100644", "type": "blob", "content": content}
                     for name, content in sorted(serialized.items())]})
        commit = self.request("/git/commits", method="POST", payload={
            "message": "Update reviewed outage feed and collection status",
            "tree": tree["sha"], "parents": [self.tip]})
        # Non-forcing update rejects a concurrent data-branch change. Never overwrite it.
        self.request("/git/refs/heads/" + self.branch, method="PATCH",
                     payload={"sha": commit["sha"], "force": False})
        self.tip, self.tree = commit["sha"], tree["sha"]
        self.previous.update(files)


def load_scheduled_state(remote, *, include_rotation=False, clock=None, sleep=None):
    """Absorb small runner-start jitter without relaxing the persisted throttle.

    Only the workflow entry point waits. Direct run_once callers stay immediate,
    and a backoff more than one minute away is left for a later invocation.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    sleep = sleep or time.sleep
    previous = remote.load(include_rotation=include_rotation)
    if HEALTH in previous:
        health = public_health(previous[HEALTH])
        if health["status"] != "blocked":
            delay = (date(health["nextAttemptAt"]) - clock()).total_seconds()
            if 0 < delay <= 60:
                sleep(delay)
                # Another run may have reserved an attempt or become blocked.
                # Refresh both the state and branch tip before run_once checks
                # permission and the due time again; never loop or force a run.
                previous = remote.load(include_rotation=include_rotation)
    return previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--permission", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        approved = json.loads(args.permission.read_text())
        permission(approved, datetime.now(timezone.utc))
        remote = GitHubDataBranch(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", ""))
        rotation_enabled = os.environ.get("ROTATION_CHECK_ENABLED") == "true"
        previous = load_scheduled_state(remote, include_rotation=rotation_enabled)
        outcome = run_once(previous, approved, remote.publish)
        if rotation_enabled:
            rotation_status = check_rotation(previous.get(ROTATION), remote.publish)
            print("Schedule check: " + rotation_status)
            if rotation_status in {"failed", "blocked"}:
                parser.exit(1, "Previous schedule link retained; schedule check needs review.\n")
    except DeliveryError as error:
        parser.exit(1, str(error) + "; prior feed retained.\n")
    except PermissionRequired:
        parser.exit(1, "Collection permission is missing, invalid or expired; no source request made.\n")
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(1, "Outage delivery could not complete; inspect configuration and prior feed. No raw source logged.\n")
    print("Collection result: " + outcome)
    if outcome in {"failed", "blocked", "unavailable"}:
        parser.exit(1, "Previous verified listings retained; collection needs review.\n")


if __name__ == "__main__":
    main()
