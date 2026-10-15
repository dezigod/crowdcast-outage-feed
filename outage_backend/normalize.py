"""Deterministic adapter for the public viewer schema inspected on 2026-10-15.

Only offline input is accepted here. Raw source bytes belong in private storage,
not in the app or Git. Counts retain WAPA's `customersOutNow` meaning.
"""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import re

SOURCE = "http://outageviewer.viwapa.vi:7575/"
ISLANDS = ("stThomas", "stCroix", "stJohn", "waterIsland")
UTC = timezone.utc


class QualityError(ValueError):
    pass


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def date(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except ValueError:
        return None


def count(value):
    if type(value) is not int or not 0 <= value <= 10_000_000:
        raise QualityError("Missing or invalid source customer count")
    return value


def canonical(value):
    return " ".join(value.upper().replace("’", "'").split())


def lookup_table():
    return json.loads(Path(__file__).with_name("island_lookup.json").read_text())["areas"]


def location(raw, table):
    """Only reviewed prefixes; unknown geography stays unassigned."""
    text = canonical(raw)
    # Observed plot-before-estate form; only an exact reviewed estate can match.
    if text.count(",") == 1:
        plot, estate = [part.strip() for part in text.split(",")]
        if plot and any(estate in area["aliases"] for area in table):
            text = estate + " " + plot
    candidates = [(alias, area) for area in table for alias in area["aliases"]
                  if text == alias or text.startswith(alias + " ")]
    if not candidates:
        return None
    alias, area = max(candidates, key=lambda pair: len(pair[0]))
    if len({a["id"] for prefix, a in candidates if len(prefix) == len(alias)}) != 1:
        return None
    plot = text[len(alias):].strip()
    # Remove utility suffixes only at the end, never from an estate's name.
    suffix = r"(?:\s+|^)(?:\((?:UP|DNSTR|DWN|HSE|EL|VICORP)\)|UP|DNSTR|DWN|HSE|EL|VICORP)$"
    while re.search(suffix, plot):
        plot = re.sub(suffix, "", plot).strip()
    plot = re.sub(r"^LOT\s*#?\s*", "Lot ", plot)
    plot = re.sub(r"^BLDG[- ]*", "Building ", plot)
    if plot == "(HOUSE)":
        plot = "House"
    return area, plot


def normalize(summary, outages, *, checked_at, raw_sha256, refresh_seconds=300):
    if not isinstance(summary, dict) or not isinstance(outages, list):
        raise QualityError("Unexpected source schema")
    updated = date(summary.get("updateTime"))
    if updated is None or updated > checked_at:
        raise QualityError("Source publication time missing or in the future")
    if type(refresh_seconds) is not int or not 300 <= refresh_seconds <= 86400:
        raise QualityError("Invalid refresh interval")
    if not re.fullmatch(r"[0-9a-f]{64}", raw_sha256):
        raise QualityError("Missing raw-source fingerprint")
    total = count(summary.get("customersOutNow"))
    unique = {}
    for event in outages:
        if not isinstance(event, dict) or not isinstance(event.get("outageRecID"), str) or not event["outageRecID"].strip():
            raise QualityError("Source event ID missing")
        key = event["outageRecID"]
        if key in unique and unique[key] != event:
            raise QualityError("Conflicting duplicate source event")
        unique[key] = event
    # Keep source listings useful, but never endorse disagreeing totals.
    conflicting_totals = sum(count(e.get("customersOutNow")) for e in unique.values()) != total
    table = lookup_table()
    groups = {}
    totals = dict.fromkeys(ISLANDS, 0)
    unassigned = 0
    notes = set()
    if conflicting_totals:
        notes.add("WAPA’s summary and event customer totals disagree. Customer totals are unconfirmed.")
    for event in unique.values():
        affected = count(event.get("customersOutNow"))
        if affected == 0:
            continue  # Restored records do not inflate active outage counts.
        if date(event.get("outageEndTime")) is not None:
            raise QualityError("Ended outage still has customers out")
        modified = date(event.get("outageModifiedTime"))
        if modified and modified > updated:
            raise QualityError("Outage record is newer than the summary")
        streets = event.get("streetsAffected")
        if not isinstance(streets, list) or any(not isinstance(s, str) for s in streets):
            raise QualityError("Missing source locations")
        mapped = [location(s, table) for s in streets if s.strip()]
        known = [entry for entry in mapped if entry is not None]
        event_islands = {entry[0]["island"] for entry in known}
        complete_event = bool(mapped) and len(known) == len(mapped) and len(event_islands) == 1
        if complete_event:
            totals[next(iter(event_islands))] += affected  # Count once per event, not once per lot.
        else:
            unassigned += 1
            notes.add("Some source events need island review. Island totals and empty listings may be incomplete.")
        event_groups = {}
        for area, plot in (known if len(event_islands) == 1 else []):
            event_groups.setdefault(area["id"], (area, set()))[1].add(plot)
        start = date(event.get("outageStartTime"))
        if start and start > updated:
            start = None
        estimate = date(event.get("estimatedTimeOfRestoral"))
        if estimate and start and estimate < start:
            estimate = None
        status = "Verified" if event.get("verified") is True else "Predicted" if event.get("verified") is False else "Unconfirmed"
        for area_id, (area, plots) in event_groups.items():
            group = groups.setdefault(area_id, {"area": area, "plots": set(), "statuses": set(),
                "starts": set(), "estimates": set(), "events": set(), "customers": 0, "count_complete": True})
            group["plots"].update(plots)
            group["statuses"].add(status)
            group["starts"].add(stamp(start) if start else None)
            group["estimates"].add(stamp(estimate) if estimate else None)
            group["events"].add(sha256(event["outageRecID"].encode()).hexdigest())
            if complete_event and len(event_groups) == 1:
                group["customers"] += affected
            else:
                group["count_complete"] = False
    islands = []
    for island in ISLANDS:
        neighborhoods = []
        for key, g in sorted(groups.items(), key=lambda pair: pair[1]["area"]["name"]):
            if g["area"]["island"] != island:
                continue
            plots = sorted(p for p in g["plots"] if p)  # Remove redundant bare parent labels.
            if not plots:
                notes.add("Some neighborhood listings do not identify individual lots.")
            neighborhoods.append({"id": island + "-" + key, "island": island, "name": g["area"]["name"],
                "locations": plots, "sourceStatus": next(iter(g["statuses"])) if len(g["statuses"]) == 1 else "Unconfirmed",
                "startedAt": next(iter(g["starts"])) if len(g["starts"]) == 1 else None,
                "estimatedRestoration": next(iter(g["estimates"])) if len(g["estimates"]) == 1 else None,
                "affectedCustomers": g["customers"] if g["count_complete"] and not conflicting_totals else None,
                "sourceEventIDs": sorted(g["events"])})
        islands.append({"id": island, "coverage": "partial" if unassigned or conflicting_totals else "complete",
            "customersOut": None if conflicting_totals else totals[island] if totals[island] or not unassigned else None,
            "neighborhoods": neighborhoods})
    return {"schemaVersion": 1, "snapshotID": raw_sha256, "collectionStatus": "ok", "lastCollectionError": None,
        "source": {"name": "WAPA outage viewer", "url": SOURCE, "checkedAt": stamp(checked_at),
                   "updatedAt": stamp(updated), "viewerRefreshSeconds": 300},
        "generatedAt": stamp(checked_at), "refreshAfterSeconds": refresh_seconds,
        "staleAfterSeconds": 900, "islands": islands,
        "unassignedEventCount": unassigned, "hasConflictingTotals": conflicting_totals, "qualityNotes": sorted(notes)}
