"""Track revisions of WAPA's public STT/STJ schedule without republishing it."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
import re
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, build_opener

from .collect import NoRedirect, PermissionRequired, retry_after
from .normalize import date, stamp

ROTATION = "rotation-schedule.json"
ALERTS = "https://www.viwapa.vi/news-information/advisory-details"
PDF = re.compile(
    r"https://www\.viwapa\.vi/docs/default-source/outage-schedule/"
    r"sttstj-rotational-outage-schedule-doc\.pdf(?:\?sfvrsn=[a-zA-Z0-9_]+)?"
)


class ScheduleLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = set()

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href")
        if tag == "a" and href:
            url = urljoin(ALERTS, href)
            if PDF.fullmatch(url):
                self.urls.add(url)


def schedule_url(html):
    links = ScheduleLinks()
    links.feed(html.decode("utf-8"))
    if len(links.urls) != 1:
        raise ValueError("WAPA schedule link is missing or ambiguous")
    return links.urls.pop()


def public_rotation(value):
    # Accept older records written before the tentative flag was added.
    if (set(value) - {"subjectToChange"} != {"schemaVersion", "status", "nextAttemptAt", "schedule"}
            or value.get("subjectToChange", True) is not True):
        raise ValueError("Unexpected rotation metadata")
    if value["schemaVersion"] != 1 or value["status"] not in {"ok", "failed", "blocked"}:
        raise ValueError("Invalid rotation status")
    if date(value["nextAttemptAt"]) is None:
        raise ValueError("Invalid rotation throttle")
    schedule = value["schedule"]
    if schedule is None and value["status"] != "ok":
        return
    if set(schedule) - {"windows", "coverageStart", "coverageEnd", "extractionStatus"} != {"url", "fingerprint", "checkedAt", "revisionSeenAt", "lastModified"}:
        raise ValueError("Unexpected schedule fields")
    checked, seen = date(schedule["checkedAt"]), date(schedule["revisionSeenAt"])
    if (not PDF.fullmatch(schedule["url"]) or not re.fullmatch(r"[0-9a-f]{64}", schedule["fingerprint"])
            or checked is None or seen is None or seen > checked
            or (schedule["lastModified"] is not None and not re.fullmatch(
                r"[A-Za-z0-9 ,:]{1,40}", schedule["lastModified"]))):
        raise ValueError("Invalid schedule metadata")

    if any(key in schedule for key in ("windows", "coverageStart", "coverageEnd", "extractionStatus")):
        validate_windows(schedule)


def validate_windows(schedule):
    windows = schedule.get("windows")
    status = schedule.get("extractionStatus")
    if status not in {"reviewed", "unavailable"} or not isinstance(windows, list) or len(windows) > 300:
        raise ValueError("Invalid schedule windows")
    if status == "unavailable":
        if windows or schedule.get("coverageStart") is not None or schedule.get("coverageEnd") is not None:
            raise ValueError("Unreviewed schedule must not contain inferred times")
        return
    if not windows:
        raise ValueError("Reviewed schedule requires windows")
    ids = set()
    starts, ends = [], []
    for row in windows:
        if set(row) != {"id", "startsAt", "endsAt", "feeders", "areas"}:
            raise ValueError("Unexpected window fields")
        start, end = date(row["startsAt"]), date(row["endsAt"])
        if (not isinstance(row["id"], str) or not 1 <= len(row["id"]) <= 100 or row["id"] in ids
                or start is None or end is None or not timedelta(0) < end - start <= timedelta(days=1)):
            raise ValueError("Invalid window timing")
        ids.add(row["id"])
        for key in ("feeders", "areas"):
            if (not isinstance(row[key], list) or len(row[key]) > 30
                    or any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 200 for s in row[key])):
                raise ValueError("Invalid window locations")
        if not row["feeders"] and not row["areas"]:
            raise ValueError("Missing window location")
        starts.append(start); ends.append(end)
    if date(schedule.get("coverageStart")) != min(starts) or date(schedule.get("coverageEnd")) != max(ends):
        raise ValueError("Coverage differs from reviewed dates")


def reviewed_windows(fingerprint):
    # A changing PDF layout must never silently remap feeder columns. Only exact
    # visually reviewed bytes receive rows; new revisions immediately fall back
    # to the official PDF until reviewed. The five-minute monitor still updates.
    records = json.loads(Path(__file__).with_name("reviewed-rotation-windows.json").read_text())
    result = records.get(fingerprint, dict(extractionStatus="unavailable", coverageStart=None,
                                           coverageEnd=None, windows=[]))
    validate_windows(result)
    return result


def download(url, *, previous_modified=None, limit):
    headers = {"User-Agent": "CrowdCast public WAPA schedule monitor"}
    if previous_modified:
        headers["If-Modified-Since"] = previous_modified
    try:
        with build_opener(NoRedirect).open(Request(url, headers=headers), timeout=15) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError("Oversized schedule response")
            return body, response.headers.get("Last-Modified")
    except HTTPError as error:
        if error.code == 304 and previous_modified:
            error.close()
            return None, previous_modified
        raise


def check_rotation(previous, publish, *, now=None, loader=download):
    now = now or datetime.now(timezone.utc)
    if previous:
        public_rotation(previous)
        if previous["status"] == "blocked" or date(previous["nextAttemptAt"]) > now:
            return "deferred"

    old = previous.get("schedule") if previous else None
    state = dict(schemaVersion=1, subjectToChange=True, status=previous["status"] if previous else "failed",
                 nextAttemptAt=stamp(now + timedelta(minutes=5)), schedule=old)
    publish({ROTATION: state.copy()})
    try:
        html, _ = loader(ALERTS, limit=1_000_000)
        url = schedule_url(html)
        same_url = old and old["url"] == url
        body, modified = loader(url, previous_modified=old["lastModified"] if same_url else None,
                                limit=8_000_000)
        if body is not None and not body.startswith(b"%PDF-"):
            raise ValueError("Invalid schedule response")
        fingerprint = old["fingerprint"] if body is None else sha256(body).hexdigest()
        unchanged = same_url and old["fingerprint"] == fingerprint
        schedule = dict(url=url, fingerprint=fingerprint, checkedAt=stamp(now),
                        revisionSeenAt=old["revisionSeenAt"] if unchanged else stamp(now),
                        lastModified=modified, **reviewed_windows(fingerprint))
        candidate = dict(state, status="ok", schedule=schedule)
        public_rotation(candidate)
        state = candidate
    except HTTPError as error:
        state["status"] = "blocked" if error.code in (401, 403) else "failed"
        if error.code in (429, 503) and (error.code == 429 or error.headers.get("Retry-After")):
            state["nextAttemptAt"] = stamp(retry_after(error.headers.get("Retry-After"), now))
        error.close()
    except PermissionRequired:
        state["status"] = "blocked"
    except (OSError, ValueError):
        state["status"] = "failed"
    publish({ROTATION: state})
    return state["status"]
