"""Check delivered data, not workflow badges. No direct provider requests."""
import argparse
import base64
import calendar
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import urllib.request
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

TITLE = "Feed freshness alert"
CRUISE_STATE = "https://raw.githubusercontent.com/dezigod/cruise-schedule-app/collection-state/state.json"
CONFIG = {
    "cruise": ("https://raw.githubusercontent.com/dezigod/cruise-schedule-app/main/official-cruise-calendar-v1.json", 12 * 3600),
    "rotation": ("https://raw.githubusercontent.com/dezigod/crowdcast-outage-feed/data/rotation-schedule.json", 30 * 60),
    "outage": ("https://raw.githubusercontent.com/dezigod/crowdcast-outage-feed/data/outages.json", 30 * 60),
}


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Missing timezone")
    return result


def document_roles(documents, local_date):
    fiscal_year = local_date.year + (local_date.month >= 10)
    month = calendar.month_name[local_date.month]
    roles = set()
    for doc in documents:
        provider = doc.get("id", "").split("-")[0]
        name = unquote(urlsplit(doc.get("url", "")).path).split("/")[-1]
        if provider == "wico":
            if re.search(rf"FY[-_ ]*{fiscal_year}\b", name, re.I):
                roles.add("WICO fiscal schedule")
            elif re.search(rf"{month}[-_ ]*{local_date.year}\b", name, re.I):
                roles.add("WICO current monthly schedule")
        elif provider == "vipa":
            revision = doc.get("sourceRevision") or ""
            # The collector already scopes this source to St. Thomas. Some
            # official PDFs omit a fiscal-year heading from their revision.
            year = re.search(r"fiscal year\s+(\d{4})", revision, re.I)
            if year is None or int(year.group(1)) == fiscal_year:
                roles.add("VIPA fiscal schedule")
    return roles


def source_times(kind, feed):
    if kind == "cruise":
        return {d["id"]: d.get("checkedAt") for d in feed.get("documents", [])}
    if kind == "rotation":
        return {key: (feed.get("schedule") or {}).get(key) for key in ("checkedAt", "revisionSeenAt", "coverageEnd")}
    source = feed.get("source", {})
    return {key: source.get(key) for key in ("checkedAt", "updatedAt")}


def problems(kind, feed, now):
    limit = CONFIG[kind][1]
    failures = []
    if feed.get("schemaVersion") != 1:
        failures.append("Published feed has an unsupported format.")
    if kind == "cruise":
        try:
            days = json.loads(base64.b64decode(feed["payload"], validate=True))["days"]
            if (not isinstance(days, list) or not days
                    or not all(isinstance(d, dict) and isinstance(d.get("date"), str)
                               and isinstance(d.get("ships"), list) for d in days)):
                raise ValueError("Invalid calendar days")
        except (ValueError, KeyError, TypeError):
            failures.append("The published cruise calendar cannot be read by the app.")
        documents = feed.get("documents")
        if not isinstance(documents, list) or not documents:
            return ["Cruise feed has no source documents."]
        required = {"WICO fiscal schedule", "WICO current monthly schedule", "VIPA fiscal schedule"}
        local_date = now.astimezone(ZoneInfo("America/St_Thomas")).date()
        for role in sorted(required - document_roles(documents, local_date)):
            failures.append("Cruise feed is missing the " + role + ".")
        ids = [d.get("id") for d in documents]
        if len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids):
            failures.append("Cruise source document identifiers are missing or duplicated.")
        if any(isinstance(i, str) and i.split("-")[0] not in {"wico", "vipa"} for i in ids):
            failures.append("Cruise feed contains an unexpected provider.")
        if feed.get("collectionStatus") in {"failed", "blocked"}:
            failures.append("Cruise collector has not verified its latest attempt.")
        checks = [(str(d.get("id") or "Cruise document") + " source check", d.get("checkedAt")) for d in documents]
    elif kind == "rotation":
        schedule = feed.get("schedule")
        if not isinstance(schedule, dict):
            return ["WAPA rotation schedule is unavailable. Check the official source; do not infer restoration times."]
        checks = [("WAPA rotation PDF check", schedule.get("checkedAt"))]
        if feed.get("status") != "ok":
            failures.append("WAPA rotation PDF check failed or requires access review.")
        if schedule.get("extractionStatus") != "reviewed":
            fingerprint = schedule.get("fingerprint", "")
            revision = " (" + fingerprint[:12] + ")" if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint) else ""
            failures.append("WAPA has an unreviewed schedule revision" + revision + ". Visually verify the PDF and match its fingerprint before publishing times.")
        else:
            try:
                if timestamp(schedule.get("coverageEnd")) <= now:
                    failures.append("WAPA's reviewed schedule dates have passed. Check for a new official schedule; no current rotation times are confirmed.")
            except (TypeError, ValueError, AttributeError):
                failures.append("WAPA reviewed schedule coverage is missing or invalid.")
    else:
        islands = feed.get("islands")
        if (not isinstance(islands, list) or len(islands) != 4
                or {i.get("id") for i in islands if isinstance(i, dict)} != {"stThomas", "stCroix", "stJohn", "waterIsland"}):
            failures.append("The outage feed is missing expected island coverage.")
        checks = [("Outage collection", feed.get("source", {}).get("checkedAt")),
                  ("WAPA source publication", feed.get("source", {}).get("updatedAt"))]
        if feed.get("collectionStatus") != "ok":
            failures.append("Outage collector has not verified its latest attempt.")
    for label, value in checks:
        try:
            age = (now - timestamp(value)).total_seconds()
            if age < 0:
                failures.append(label + " timestamp is in the future.")
            elif age > limit:
                failures.append(label + " is older than the allowed freshness window.")
        except (TypeError, ValueError, AttributeError):
            failures.append(label + " timestamp is missing or invalid.")
    return sorted(set(failures))


def gh(*args):
    # GitHub CLI keeps its token in the environment, never command arguments.
    result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return result.stdout


def cruise_state_problems(state, now):
    providers = state["providers"]
    if not isinstance(providers, dict) or set(providers) != {"wico", "vipa"}:
        raise ValueError("Missing cruise collector state")
    failures = []
    for provider, status in providers.items():
        if status.get("accessRefused"):
            failures.append(provider.upper() + " refused access. Review access with the provider before resuming.")
        elif status.get("failures", 0) > 0:
            failures.append(provider.upper() + " download failed. Waiting for the next permitted successful check.")
    if state.get("lastFailure") == "monthly_missing":
        failures.append("WICO's known monthly listing disappeared. The previous calendar was retained.")
    elif state.get("lastFailure") and not failures:
        failures.append("The last cruise collection failed. Inspect the collector run before changing the feed.")
    if state.get("pendingDelivery"):
        age = (now - timestamp(state["lastSuccess"])).total_seconds()
        if age < 0 or age > 1800:
            failures.append("A validated cruise calendar is waiting for delivery. Check the publication job.")
    return failures


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise OSError("Unexpected redirect while checking GitHub delivery")


def published_json(url, limit):
    bucket = int(datetime.now(timezone.utc).timestamp()) // 300
    request = urllib.request.Request(url + "?health_check=" + str(bucket),
        headers={"User-Agent": "CrowdCast-feed-health", "Cache-Control": "no-cache"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("Feed size exceeded")
    return json.loads(body)


def alert(repository, failures, title=TITLE):
    # GitHub's search index can lag after issue creation. Read the live
    # issue list so adjacent checks recognise the same incident.
    pages = json.loads(gh("api", "--paginate", "--slurp", "repos/" + repository + "/issues?state=open&per_page=100"))
    rows = (row for page in pages for row in page if "pull_request" not in row)
    incident = next((row for row in rows if row["title"] == title), None)
    if failures:
        body = ("@dezigod Feed freshness or source coverage needs attention.\n\n" + "\n".join("- " + f for f in failures) +
                "\n\nCheck the collector's Actions logs and the published source timestamps. "
                "The previous valid feed must be retained. Never bypass provider refusals or reset source timestamps. "
                "This issue closes automatically after the delivered feed is healthy again.")
        if incident is None:
            gh("issue", "create", "--repo", repository, "--title", title, "--body", body)
            return "new_incident"
        elif incident.get("body", "") != body:
            earlier = set(incident.get("body", "").splitlines())
            added = [f for f in failures if "- " + f not in earlier]
            number = str(incident["number"])
            gh("issue", "edit", number, "--repo", repository, "--body", body)
            if added:
                gh("issue", "comment", number, "--repo", repository, "--body",
                   "@dezigod The delivery incident changed.\n\n" + "\n".join("- " + f for f in added))
                return "new_problems"
        return "known_incident"
    if incident:
        gh("issue", "close", str(incident["number"]), "--repo", repository,
           "--comment", "@dezigod Recovery verified: the delivered feed now passes its freshness, source-coverage and collection checks.")
        return "recovered"
    return "healthy"


def report_result(kind, failures, alert_state, deduplicate=False):
    # A successfully tracked, unchanged rotation incident is not a new failure.
    # Its feed remains unhealthy and the open issue is the durable alert.
    known = deduplicate and kind == "rotation" and alert_state == "known_incident"
    print(json.dumps({"healthy": not failures, "problems": failures,
                      "alertState": alert_state, "repeatNotificationSuppressed": bool(failures and known)}))
    if failures and known:
        print("::warning title=WAPA schedule still needs review::Known incident remains open; no new repeat-failure notification. See job summary and open rotation-review issue.")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write("\n### " + kind + " feed: " + ("UNHEALTHY" if failures else "healthy") + "\n\n")
            out.write("Alert state: " + alert_state + ".\n\n")
            for failure in failures:
                out.write("- " + failure + "\n")
            if failures and known:
                out.write("\nThe existing rotation-review issue remains open. This monitor run succeeded at tracking an unresolved incident; it does **not** mean schedule coverage recovered.\n")
    return 1 if failures and not known else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=CONFIG)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--alert-repository", choices=["dezigod/crowdcast-outage-feed"])
    parser.add_argument("--deduplicate-known-incident", action="store_true",
                        help="Rotation only: keep known open incidents visible without repeated failed-run notifications")
    args = parser.parse_args()
    if args.deduplicate_known_incident and args.kind != "rotation":
        parser.error("Incident notification deduplication is limited to rotation checks")
    expected = "dezigod/" + ("cruise-schedule-app" if args.kind == "cruise" else "crowdcast-outage-feed")
    if args.repository != expected:
        parser.error("Unexpected repository")
    try:
        feed = published_json(CONFIG[args.kind][0], 3_000_000)
        failures = problems(args.kind, feed, datetime.now(timezone.utc))
        print(json.dumps({"feed": args.kind, "observedAt": datetime.now(timezone.utc).isoformat(),
                          "sourceTimes": source_times(args.kind, feed)}))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        failures = ["Published feed could not be downloaded or validated."]
    if args.kind == "cruise":
        try:
            state = published_json(CRUISE_STATE, 65_536)
            failures.extend(cruise_state_problems(state, datetime.now(timezone.utc)))
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            failures.append("Cruise collection state could not be downloaded or validated.")
    destination = args.alert_repository or args.repository
    title = "Cruise feed freshness alert" if destination != args.repository else TITLE
    if args.kind == "rotation":
        title = "WAPA rotation schedule needs review"
    alert_state = alert(destination, failures, title=title)
    raise SystemExit(report_result(args.kind, failures, alert_state, args.deduplicate_known_incident))


if __name__ == "__main__":
    main()

