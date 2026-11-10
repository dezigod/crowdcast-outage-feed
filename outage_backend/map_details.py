"""Validate the explicitly reviewed public map-detail extension (not raw source)."""
import math
import re

from .normalize import ISLANDS, date, optional_count

DETAIL_KEYS = {"sourceSummary", "events"}
SUMMARY_KEYS = {"totalOutages", "customersServed", "customersOut", "customersAffected", "customersRestored",
                "eventCustomersOut", "customerCountDifference", "hourlyCustomersOut", "locations"}
EVENT_KEYS = {"id", "name", "island", "neighborhoodIDs", "locations", "sourceStatus", "verified", "crewAssigned",
              "cause", "workStatus", "point", "startedAt", "estimatedRestoration", "endedAt", "modifiedAt",
              "reportedCustomersOut", "reportedCustomersInitially", "reportedCustomersRestored"}


def strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_map_details(feed):
    summary, events = feed["sourceSummary"], feed["events"]
    if not isinstance(summary, dict) or set(summary) != SUMMARY_KEYS or not isinstance(events, list):
        raise ValueError("Invalid public map details")
    for field in ("totalOutages", "customersOut", "eventCustomersOut"):
        if optional_count(summary[field]) is None:
            raise ValueError("Invalid map summary count")
    for field in ("customersServed", "customersAffected", "customersRestored"):
        if summary[field] is not None and optional_count(summary[field]) is None:
            raise ValueError("Invalid optional map summary count")
    if type(summary["customerCountDifference"]) is not int:
        raise ValueError("Invalid summary count difference")
    if summary["locations"] is not None and not strings(summary["locations"]):
        raise ValueError("Invalid summary locations")
    history = summary["hourlyCustomersOut"]
    if not isinstance(history, list):
        raise ValueError("Invalid customer history")
    for row in history:
        if (not isinstance(row, dict) or set(row) != {"at", "customersOut"}
                or date(row["at"]) is None or optional_count(row["customersOut"]) is None):
            raise ValueError("Invalid customer history entry")
    ids = set()
    for event in events:
        if not isinstance(event, dict) or set(event) != EVENT_KEYS:
            raise ValueError("Unexpected public event fields")
        if not isinstance(event["id"], str) or not re.fullmatch(r"[0-9a-f]{64}", event["id"]) or event["id"] in ids:
            raise ValueError("Invalid event identity")
        ids.add(event["id"])
        if event["island"] is not None and event["island"] not in ISLANDS:
            raise ValueError("Invalid event island")
        if not strings(event["locations"]) or not strings(event["neighborhoodIDs"]):
            raise ValueError("Invalid event locations")
        if event["sourceStatus"] not in {"Verified", "Predicted", "Unconfirmed", "Restored"}:
            raise ValueError("Invalid source event status")
        for field in ("name", "cause", "workStatus"):
            if event[field] is not None and not isinstance(event[field], str):
                raise ValueError("Invalid event label")
        for field in ("verified", "crewAssigned"):
            if event[field] is not None and type(event[field]) is not bool:
                raise ValueError("Invalid event flag")
        for field in ("startedAt", "estimatedRestoration", "endedAt", "modifiedAt"):
            if event[field] is not None and date(event[field]) is None:
                raise ValueError("Invalid event timestamp")
        if optional_count(event["reportedCustomersOut"]) is None:
            raise ValueError("Invalid source event customer count")
        for field in ("reportedCustomersInitially", "reportedCustomersRestored"):
            if event[field] is not None and optional_count(event[field]) is None:
                raise ValueError("Invalid optional source event customer count")
        point = event["point"]
        if point is not None:
            if not isinstance(point, dict) or set(point) != {"latitude", "longitude"}:
                raise ValueError("Invalid source event point")
            for field, limit in (("latitude", 90), ("longitude", 180)):
                value = point[field]
                if type(value) not in (int, float) or not math.isfinite(value) or not -limit <= value <= limit:
                    raise ValueError("Invalid source event coordinate")
    total = sum(e["reportedCustomersOut"] for e in events)
    if (summary["eventCustomersOut"] != total
            or summary["totalOutages"] != sum(e["reportedCustomersOut"] > 0 for e in events)
            or summary["customerCountDifference"] != summary["customersOut"] - total
            or feed["hasConflictingTotals"] != (summary["customerCountDifference"] != 0)):
        raise ValueError("Inconsistent map summary and event detail")
