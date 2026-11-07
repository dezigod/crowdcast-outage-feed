"""Track revisions of WAPA's public STT/STJ schedule without republishing it."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
import re
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
    if set(schedule) != {"url", "fingerprint", "checkedAt", "revisionSeenAt", "lastModified"}:
        raise ValueError("Unexpected schedule fields")
    checked, seen = date(schedule["checkedAt"]), date(schedule["revisionSeenAt"])
    if (not PDF.fullmatch(schedule["url"]) or not re.fullmatch(r"[0-9a-f]{64}", schedule["fingerprint"])
            or checked is None or seen is None or seen > checked
            or (schedule["lastModified"] is not None and not re.fullmatch(
                r"[A-Za-z0-9 ,:]{1,40}", schedule["lastModified"]))):
        raise ValueError("Invalid schedule metadata")


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
                        lastModified=modified)
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
