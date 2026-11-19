"""Track revisions of WAPA's public STT/STJ schedule without republishing it."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
import re
import json
from pathlib import Path
from copy import deepcopy
from zoneinfo import ZoneInfo
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, build_opener

from .collect import NoRedirect, PermissionRequired, retry_after, deadline_stamp
from .normalize import date, stamp

AST = ZoneInfo("America/St_Thomas")
DAY_FIELDS = {"day", "timeZone", "dayStatus", "extractionMethod", "sourceURLs", "websiteCheckedAt"}
FACEBOOK_PHOTO = re.compile(r"https://www\.facebook\.com/photo/\?fbid=[0-9]+&set=pb\.100069365522656\.-2207520000")

ROTATION = "rotation-schedule.json"
ALERTS = "https://www.viwapa.vi/news-information/advisory-details"
PDF = re.compile(
    r"https://www\.viwapa\.vi/docs/default-source/outage-schedule/"
    r"sttstj-rotational-outage-schedule-doc\.pdf(?:\?sfvrsn=[a-zA-Z0-9_]+)?"
)
IMAGE = re.compile(
    r"https://www\.viwapa\.vi/docs/default-source/outage-schedule/"
    r"sttstj-rotational-outage-schedule-doc\.png(?:\?sfvrsn=[a-zA-Z0-9_]+)?"
)


class ScheduleUnavailable(ValueError):
    """An official page changed; no replacement coverage can be inferred."""


def diagnostic(category, **details):
    print(json.dumps(dict(diagnostic="rotation_check", category=category, **details)))


class ScheduleLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = set()
        self.images = set()

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href")
        if tag == "a" and href:
            url = urljoin(ALERTS, href)
            if PDF.fullmatch(url):
                self.urls.add(url)
            elif IMAGE.fullmatch(url):
                self.images.add(url)


def schedule_url(html):
    links = ScheduleLinks()
    links.feed(html.decode("utf-8"))
    if links.images:
        # The November 19 access review found a PNG for another district and
        # date at the former STT/STJ link. Never follow it as an STT/STJ PDF.
        raise ScheduleUnavailable("schedule_format_changed")
    if len(links.urls) != 1:
        raise ScheduleUnavailable("schedule_link_missing_or_ambiguous")
    return links.urls.pop()


def advisory_fingerprint(html):
    """Bind access review to the exact published link, not per-request HTML.

    Sitefinity view-state/nonces can differ between otherwise identical page
    responses. Only the allowlisted PDF/image links authorize this review.
    The linked document still needs its own format, date and district checks.
    """
    links = ScheduleLinks()
    links.feed(html.decode("utf-8"))
    if len(links.urls | links.images) != 1:
        raise PermissionRequired("Reviewed advisory link missing or ambiguous")
    value = dict(schemaVersion=1, pdf=sorted(links.urls), images=sorted(links.images))
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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
    if set(schedule) - ({"windows", "coverageStart", "coverageEnd", "extractionStatus"} | DAY_FIELDS) != {"url", "fingerprint", "checkedAt", "revisionSeenAt", "lastModified"}:
        raise ValueError("Unexpected schedule fields")
    checked, seen = date(schedule["checkedAt"]), date(schedule["revisionSeenAt"])
    if (not (PDF.fullmatch(schedule["url"]) or FACEBOOK_PHOTO.fullmatch(schedule["url"])) or not re.fullmatch(r"[0-9a-f]{64}", schedule["fingerprint"])
            or checked is None or seen is None or seen > checked
            or (schedule["lastModified"] is not None and not re.fullmatch(
                r"[A-Za-z0-9 ,:]{1,40}", schedule["lastModified"]))):
        raise ValueError("Invalid schedule metadata")

    if any(key in schedule for key in ("windows", "coverageStart", "coverageEnd", "extractionStatus")):
        validate_windows(schedule)
    if any(key in schedule for key in DAY_FIELDS):
        if (not {"day", "timeZone", "dayStatus", "extractionMethod"}.issubset(schedule)
                or schedule["timeZone"] != "America/St_Thomas"
                or not isinstance(schedule["day"], str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", schedule["day"])
                or schedule["dayStatus"] not in {"current", "not_current", "unreviewed"}
                or schedule["extractionMethod"] not in {"manual-review", "validated-layout", "unavailable"}):
            raise ValueError("Invalid schedule day metadata")
        # Validate the calendar date, not merely its string shape.
        datetime.strptime(schedule["day"], "%Y-%m-%d")
        if schedule["dayStatus"] == "current":
            if (schedule.get("extractionStatus") != "reviewed" or not schedule.get("windows")
                    or schedule["extractionMethod"] == "unavailable"
                    or any(date(row["startsAt"]).astimezone(AST).date().isoformat() != schedule["day"]
                           for row in schedule["windows"])):
                raise ValueError("Current schedule contains another day's windows")
        elif schedule.get("windows"):
            raise ValueError("Non-current schedule must not expose windows")
        if FACEBOOK_PHOTO.fullmatch(schedule["url"]) and (
                "sourceURLs" not in schedule or schedule["extractionMethod"] != "manual-review"):
            raise ValueError("Social schedules require manual source review")
        if "sourceURLs" in schedule:
            urls = schedule["sourceURLs"]
            if (not isinstance(urls, list) or not 1 <= len(urls) <= 4 or schedule["url"] != urls[0]
                    or any(not isinstance(url, str) or not FACEBOOK_PHOTO.fullmatch(url) for url in urls)):
                raise ValueError("Invalid reviewed social sources")
        if "websiteCheckedAt" in schedule and date(schedule["websiteCheckedAt"]) is None:
            raise ValueError("Invalid website check time")


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


def current_day(schedule, now):
    """Remove other dates without refreshing any source observation timestamp."""
    if schedule is None:
        return None
    result = deepcopy(schedule)
    today = now.astimezone(AST).date().isoformat()
    rows = [row for row in result.get("windows", [])
            if date(row["startsAt"]).astimezone(AST).date().isoformat() == today]
    was_reviewed = result.get("extractionStatus") == "reviewed" or result.get("dayStatus") == "not_current"
    result.update(day=today, timeZone="America/St_Thomas",
                  dayStatus="current" if rows else "not_current" if was_reviewed else "unreviewed",
                  extractionMethod=result.get("extractionMethod", "manual-review" if was_reviewed else "unavailable"),
                  windows=rows, extractionStatus="reviewed" if rows else "unavailable",
                  coverageStart=min((row["startsAt"] for row in rows), key=lambda s: date(s), default=None),
                  coverageEnd=max((row["endsAt"] for row in rows), key=lambda s: date(s), default=None))
    return result


def reviewed_social_schedule(now):
    """Dated browser-verified fallback when WAPA's website PDF trails its posts.

    checkedAt remains the actual review time. A website check is NOT a new check
    of the Facebook images. Entries expire at local midnight, without a rollout.
    """
    path = Path(__file__).with_name("reviewed-rotation-posts.json")
    records = json.loads(path.read_text())
    record = records.get(now.astimezone(AST).date().isoformat())
    if record is None:
        return None
    if set(record) != {"checkedAt", "sources", "windows"}:
        raise ValueError("Unexpected social review fields")
    sources = record["sources"]
    if (not isinstance(sources, list) or not 1 <= len(sources) <= 4
            or any(set(source) != {"url", "fingerprint"}
                   or not FACEBOOK_PHOTO.fullmatch(source["url"])
                   or not re.fullmatch(r"[0-9a-f]{64}", source["fingerprint"]) for source in sources)
            or date(record["checkedAt"]) is None or date(record["checkedAt"]) > now):
        raise ValueError("Invalid social review evidence")
    rows = record["windows"]
    result = dict(url=sources[0]["url"], sourceURLs=[s["url"] for s in sources],
                  fingerprint=sha256("\n".join(s["fingerprint"] for s in sources).encode()).hexdigest(),
                  checkedAt=record["checkedAt"], revisionSeenAt=record["checkedAt"], lastModified=None,
                  extractionStatus="reviewed", extractionMethod="manual-review", windows=rows,
                  coverageStart=min((r["startsAt"] for r in rows), key=lambda s: date(s)),
                  coverageEnd=max((r["endsAt"] for r in rows), key=lambda s: date(s)))
    validate_windows(result)
    result = current_day(result, now)
    if len(result["windows"]) != len(rows):
        raise ValueError("Social review dates differ from keyed day")
    return result


def extract_windows(fingerprint, body):
    reviewed = reviewed_windows(fingerprint)
    if reviewed["extractionStatus"] == "reviewed":
        return dict(reviewed, extractionMethod="manual-review")
    if body is not None:
        try:
            from .rotation_pdf import extracted_windows
            return dict(extracted_windows(body), extractionMethod="validated-layout")
        except (ImportError, ValueError, OSError) as error:
            diagnostic("schedule_layout_unverified", errorType=type(error).__name__, fingerprint=fingerprint)
    return dict(reviewed, extractionMethod="unavailable")


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


def validate_access_review(previous, review, now):
    """One explicit, short-lived review; no automatic reset after a refusal."""
    if (not previous or previous.get("status") != "blocked"
            or set(review) != {"expectedFingerprint", "advisoryFingerprint", "validUntil"}
            or (previous.get("schedule") or {}).get("fingerprint") != review["expectedFingerprint"]
            or any(not isinstance(review[key], str) or not re.fullmatch(r"[0-9a-f]{64}", review[key])
                   for key in ("expectedFingerprint", "advisoryFingerprint"))
            or date(review["validUntil"]) is None
            or not now < date(review["validUntil"]) <= now + timedelta(minutes=15)):
        raise PermissionRequired("A current, matching access review is required")


def check_rotation(previous, publish, *, now=None, loader=download, clock=None, access_review=None):
    clock = clock or ((lambda: now) if now is not None else (lambda: datetime.now(timezone.utc)))
    now = now or clock()
    if access_review is not None:
        validate_access_review(previous, access_review, now)
    if previous:
        public_rotation(previous)
        cleaned_schedule = current_day(previous.get("schedule"), now)
        social = reviewed_social_schedule(now)
        # Applying already-reviewed local evidence makes no source request and
        # must not wait for an unrelated website retry/throttle deadline.
        if social is not None and (cleaned_schedule is None
                or cleaned_schedule.get("dayStatus") != "current"
                or (cleaned_schedule.get("sourceURLs")
                    and date(social["checkedAt"]) > date(cleaned_schedule["checkedAt"]))):
            if cleaned_schedule is not None:
                website_checked = (cleaned_schedule.get("websiteCheckedAt")
                                   if cleaned_schedule.get("sourceURLs") else cleaned_schedule["checkedAt"])
                if website_checked is not None:
                    social = dict(social, websiteCheckedAt=website_checked)
            cleaned_schedule = social
        cleaned = dict(previous, schedule=cleaned_schedule)
        if cleaned != previous:
            public_rotation(cleaned)
            publish({ROTATION: cleaned})
            previous = cleaned
        if previous["status"] == "blocked" and access_review is None:
            diagnostic("access_review_required", sourceRequested=False)
            return "review_required"
        if date(previous["nextAttemptAt"]) > now:
            return "deferred"

    old = previous.get("schedule") if previous else None
    state = dict(schemaVersion=1, subjectToChange=True, status=previous["status"] if previous else "failed",
                 nextAttemptAt=deadline_stamp(now + timedelta(minutes=5)), schedule=old)
    publish({ROTATION: state.copy()})
    reviewed_page = False
    try:
        html, _ = loader(ALERTS, limit=1_000_000)
        if access_review is not None:
            observed = advisory_fingerprint(html)
            if observed != access_review["advisoryFingerprint"]:
                diagnostic("advisory_link_mismatch", observedFingerprint=observed)
                raise PermissionRequired("Advisory link changed after access review")
            if clock() >= date(access_review["validUntil"]):
                raise PermissionRequired("Access review expired during request")
            reviewed_page = True
        url = schedule_url(html)
        same_url = old and old["url"] == url
        # Do not trust yesterday's conditional response or an unparsed revision
        # for today's schedule. A current, already validated document may use 304.
        conditional = same_url and old.get("dayStatus") == "current"
        body, modified = loader(url, previous_modified=old["lastModified"] if conditional else None,
                                limit=8_000_000)
        checked_at = clock()
        if checked_at < now:
            raise ValueError("Collection clock regressed")
        if body is not None and not body.startswith(b"%PDF-"):
            raise ValueError("Invalid schedule response")
        fingerprint = old["fingerprint"] if body is None else sha256(body).hexdigest()
        unchanged = same_url and old["fingerprint"] == fingerprint
        extracted = ({key: old[key] for key in ("windows", "coverageStart", "coverageEnd", "extractionStatus", "extractionMethod")}
                     if body is None else extract_windows(fingerprint, body))
        schedule = current_day(dict(url=url, fingerprint=fingerprint, checkedAt=stamp(checked_at),
                        revisionSeenAt=old["revisionSeenAt"] if unchanged else stamp(checked_at),
                        lastModified=modified, **extracted), checked_at)
        if schedule["dayStatus"] != "current":
            social = reviewed_social_schedule(checked_at)
            if social is not None:
                schedule = dict(social, websiteCheckedAt=stamp(checked_at))
        candidate = dict(state, status="ok", schedule=schedule)
        public_rotation(candidate)
        state = candidate
    except HTTPError as error:
        state["status"] = "blocked" if error.code in (401, 403) or access_review is not None else "failed"
        if error.code in (429, 503) and (error.code == 429 or error.headers.get("Retry-After")):
            state["nextAttemptAt"] = deadline_stamp(retry_after(error.headers.get("Retry-After"), clock()))
        diagnostic("http_error", httpStatus=error.code, status=state["status"])
        error.close()
    except PermissionRequired as error:
        state["status"] = "blocked"
        categories = {"Source redirect requires access review": "source_redirect_requires_review",
                      "Advisory link changed after access review": "advisory_link_changed_since_review",
                      "Reviewed advisory link missing or ambiguous": "reviewed_advisory_link_missing_or_ambiguous",
                      "Access review expired during request": "access_review_expired"}
        diagnostic(categories.get(str(error), "access_review_required"), status="blocked")
    except ScheduleUnavailable as error:
        # A matching, owner-reviewed HTTP 200 advisory proves access recovered,
        # even when it proves the required schedule is missing. Resume ordinary
        # page checks, retaining the old source clocks and empty coverage.
        state["status"] = "failed" if access_review is None or reviewed_page else "blocked"
        diagnostic(str(error), status=state["status"])
    except (OSError, ValueError) as error:
        state["status"] = "blocked" if access_review is not None else "failed"
        diagnostic("schedule_response_unverified", errorType=type(error).__name__, status=state["status"])
    publish({ROTATION: state})
    return state["status"]
