"""One collection attempt, requiring a reviewed permission file. No scheduler."""
import argparse
import fcntl
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from .normalize import SOURCE, QualityError, date, normalize, stamp
from .store import Store

URLS = tuple(SOURCE + "data/" + name for name in ("settings.json", "outageSummary.json", "outages.json"))


class PermissionRequired(ValueError):
    pass


def permission(payload, now):
    if payload.get("allow_automated_collection") is not True or payload.get("allow_app_display") is not True:
        raise PermissionRequired("Collection and app display permission must both be recorded")
    if not isinstance(payload.get("reference"), str) or not payload["reference"].strip():
        raise PermissionRequired("A permission reference is required")
    interval = payload.get("minimum_interval_seconds")
    if type(interval) is not int or not 300 <= interval <= 86400:
        raise PermissionRequired("Record an approved polling interval of at least five minutes")
    if not set(URLS).issubset(set(payload.get("approved_urls", []))):
        raise PermissionRequired("The exact source URLs must be approved")
    if payload.get("valid_until") is not None:
        expiry = date(payload["valid_until"])
        if expiry is None or expiry <= now:
            raise PermissionRequired("Permission expiry is invalid or has passed")
    return interval


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        raise PermissionRequired("Source redirect requires access review")


def fetch(url):
    # Only these already-reviewed public viewer routes; never login or reporting.
    if url not in URLS:
        raise PermissionRequired("Unapproved URL")
    request = urllib.request.Request(url, headers={"User-Agent": "CrowdCast outage adapter; approved read-only data access"})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise QualityError("Source response exceeds size limit")
        return json.loads(body)


def retry_after(value, now):
    try:
        seconds = int(value)
        return now + timedelta(seconds=max(300, seconds))
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
            return max(now + timedelta(minutes=5), parsed.astimezone(timezone.utc))
        except (ValueError, TypeError):
            return now + timedelta(minutes=15)


def collect(store, approved, *, now=None, loader=fetch):
    now = now or datetime.now(timezone.utc)
    interval = permission(approved, now)  # Must execute before any network access.
    store.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (store.directory / "collection.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return _collect_locked(store, approved, now=now, loader=loader, interval=interval)


def _collect_locked(store, approved, *, now, loader, interval):
    health = store.read("health.json", {})
    if health.get("status") == "blocked":
        raise PermissionRequired("Previous access denial requires manual review of health.json")
    next_attempt = date(health.get("nextAttemptAt"))
    if next_attempt and now < next_attempt:
        return False
    # Persist throttle before the first request, including failed attempts.
    attempt = dict(status="failed", error="Source update incomplete", nextAttemptAt=stamp(now + timedelta(seconds=interval)))
    store.write("health.json", attempt)
    try:
        settings, summary, outages = [loader(url) for url in URLS]
        viewer_minutes = settings["mapOptions"]["refreshTime"]
        if type(viewer_minutes) is not int or viewer_minutes < 1:
            raise QualityError("Invalid viewer refresh setting")
        interval = max(interval, viewer_minutes * 60)
        attempt["nextAttemptAt"] = stamp(now + timedelta(seconds=interval))
        raw = {"settings": settings, "summary": summary, "outages": outages}
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        # Retain failed as well as accepted input for private investigation.
        store.write("raw-" + fingerprint + ".json", dict(raw, checkedAt=stamp(now), permissionReference=approved["reference"]))
        feed = normalize(summary, outages, checked_at=now, raw_sha256=fingerprint, refresh_seconds=interval)
        feed["source"]["viewerRefreshSeconds"] = viewer_minutes * 60
        prior = store.read("feed.json")
        if prior and date(feed["source"]["updatedAt"]) < date(prior["source"]["updatedAt"]):
            raise QualityError("Older source snapshot refused")
        if prior and feed["source"]["updatedAt"] == prior["source"]["updatedAt"] and feed["snapshotID"] != prior["snapshotID"]:
            raise QualityError("Source contents changed without a new publication timestamp")
        # Raw source provenance is private; it is never served by the API.
        store.write("feed.json", feed)
        store.write("health.json", dict(attempt, status="ok", error=None))
        return True
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            attempt.update(status="blocked", error="Source access needs review")
        elif error.code == 429:
            attempt.update(error="Source rate limit", nextAttemptAt=stamp(max(now + timedelta(seconds=interval), retry_after(error.headers.get("Retry-After"), now))))
        else:
            attempt.update(error="Source download failed")
        error.close()
    except PermissionRequired:
        attempt.update(status="blocked", error="Source access needs review")
    except QualityError as error:
        store.write("last-failure.json", {"checkedAt": stamp(now), "reason": str(error)})
        attempt.update(error="Source update could not be verified")
    except (ValueError, KeyError, TypeError, OSError):
        attempt.update(error="Source update could not be verified")
    store.write("health.json", attempt)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--permission", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--watch", action="store_true", help="Opt-in foreground loop; never enabled by the API")
    args = parser.parse_args()
    store = Store(args.state)
    while True:
        try:
            success = collect(store, json.loads(args.permission.read_text()))
        except (PermissionRequired, OSError, ValueError) as error:
            parser.exit(2, str(error) + "\n")
        message = "Validated source listings saved; review coverage and quality flags" if success else "No new verified snapshot; previous data retained"
        if not args.watch:
            parser.exit(0 if success else 1, message + "\n")
        print(message, flush=True)
        health = store.read("health.json", {})
        if health.get("status") == "blocked":
            parser.exit(2, "Source access needs review; collector stopped\n")
        next_attempt = date(health.get("nextAttemptAt"))
        delay = max(1, (next_attempt - datetime.now(timezone.utc)).total_seconds()) if next_attempt else 300
        time.sleep(delay)


if __name__ == "__main__":
    main()
