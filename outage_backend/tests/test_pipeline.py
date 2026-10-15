from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import fcntl
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError

from fastapi.testclient import TestClient
from outage_backend.app import create_app
from outage_backend.collect import collect, permission, PermissionRequired, URLS
from outage_backend.normalize import normalize, QualityError, location, lookup_table
from outage_backend.store import Store

NOW = datetime(2026, 10, 15, 16, 0, tzinfo=timezone.utc)


def event(key="one", streets=None, customers=5, **changes):
    return dict(outageRecID=key, streetsAffected=streets if streets is not None else
        ["AGNES FANCY", "AGNES FANCY 5-A", "AGNES FANCY 5-A UP", "AGNES FANCY 5B"],
        customersOutNow=customers, outageStartTime="2026-10-15T10:00:00-04:00", verified=False,
        estimatedTimeOfRestoral=None, outageEndTime=None, outageModifiedTime="2026-10-15T11:59:00-04:00", **changes)


def build(events=None, **changes):
    events = events if events is not None else [event()]
    summary = {"updateTime": "2026-10-15T11:59:00-04:00", "customersOutNow": sum(e["customersOutNow"] for e in events)}
    summary.update(changes)
    return normalize(summary, events, checked_at=NOW, raw_sha256="a" * 64)


def allowed():
    return dict(allow_automated_collection=True, allow_app_display=True, reference="SYNTHETIC TEST PERMISSION ONLY",
        minimum_interval_seconds=300, approved_urls=list(URLS))


class NormalizeTests(unittest.TestCase):
    def test_plots_are_not_customer_counts(self):
        feed = build(); island = feed["islands"][0]; area = island["neighborhoods"][0]
        self.assertEqual(island["customersOut"], 5)
        self.assertEqual(area["locations"], ["5-A", "5B"])
        self.assertEqual(area["affectedCustomers"], 5)
        self.assertNotEqual(len(area["locations"]), island["customersOut"])

    def test_known_islands_stay_separate(self):
        feed = build([event("a", ["AGNES FANCY 5-A"], 5), event("b", ["SION HILL 7P (DNSTR)"], 7),
                      event("c", ["WATER ISLAND LOT# 6"], 2)])
        self.assertEqual([i["customersOut"] for i in feed["islands"]], [5, 7, 0, 2])
        self.assertEqual(feed["islands"][3]["neighborhoods"][0]["locations"], ["Lot 6"])

    def test_unknown_estates_do_not_become_zero_on_other_islands(self):
        feed = build([event(streets=["UNREVIEWED ESTATE 123"])])
        self.assertEqual(feed["unassignedEventCount"], 1)
        self.assertTrue(all(i["customersOut"] is None and i["coverage"] == "partial" for i in feed["islands"]))

    def test_partially_mapped_event_does_not_assign_its_customer_total(self):
        feed = build([event(streets=["AGNES FANCY 5-A", "UNKNOWN ESTATE 7"])])
        self.assertIsNone(feed["islands"][0]["customersOut"])
        self.assertIsNone(feed["islands"][0]["neighborhoods"][0]["affectedCustomers"])

    def test_event_with_two_islands_is_held_for_review(self):
        feed = build([event(streets=["AGNES FANCY 5-A", "SION HILL 7P"])])
        self.assertEqual(feed["unassignedEventCount"], 1)
        self.assertTrue(all(not i["neighborhoods"] for i in feed["islands"]))

    def test_multi_neighborhood_event_counts_once(self):
        feed = build([event(streets=["AGNES FANCY 5-A", "SUBBASE 69"])])
        self.assertEqual(feed["islands"][0]["customersOut"], 5)
        self.assertEqual(len(feed["islands"][0]["neighborhoods"]), 2)
        self.assertTrue(all(a["affectedCustomers"] is None for a in feed["islands"][0]["neighborhoods"]))

    def test_duplicate_events_collapse_without_double_counting(self):
        e = event()
        feed = normalize({"updateTime":"2026-10-15T11:59:00-04:00","customersOutNow":5}, [e, deepcopy(e)], checked_at=NOW, raw_sha256="a"*64)
        self.assertEqual(feed["islands"][0]["customersOut"], 5)

    def test_conflicting_duplicate_events_fail(self):
        e = event(); other = deepcopy(e); other["verified"] = True
        with self.assertRaises(QualityError): build([e, other])

    def test_summary_disagreement_keeps_listings_but_withholds_all_customer_totals(self):
        feed=build(customersOutNow=7)
        self.assertTrue(feed["hasConflictingTotals"])
        self.assertTrue(all(i["customersOut"] is None and i["coverage"] == "partial" for i in feed["islands"]))
        self.assertEqual(feed["islands"][0]["neighborhoods"][0]["locations"], ["5-A","5B"])
        self.assertIsNone(feed["islands"][0]["neighborhoods"][0]["affectedCustomers"])

    def test_missing_or_boolean_customers_are_not_zero(self):
        for value in [None, True, -1, "5"]:
            with self.subTest(value=value), self.assertRaises(QualityError): build(customersOutNow=value)

    def test_unknown_restoration_and_timezone_free_times_stay_hidden(self):
        e = event(); e.update(estimatedTimeOfRestoral="Unknown", outageStartTime="2026-10-15T10:00:00")
        area = build([e])["islands"][0]["neighborhoods"][0]
        self.assertIsNone(area["estimatedRestoration"]); self.assertIsNone(area["startedAt"])

    def test_offset_times_preserved_as_utc(self):
        self.assertEqual(build()["islands"][0]["neighborhoods"][0]["startedAt"], "2026-10-15T14:00:00Z")

    def test_source_publication_clock_never_replaced_by_download_clock(self):
        feed = build(updateTime="2026-10-15T11:59:00-04:00")
        self.assertEqual(feed["source"]["updatedAt"], "2026-10-15T15:59:00Z")
        self.assertEqual(feed["source"]["checkedAt"], "2026-10-15T16:00:00Z")
        self.assertEqual(feed["staleAfterSeconds"], 900)

    def test_missing_future_or_ambiguous_source_publication_is_rejected(self):
        for timestamp in [None, "2026-10-15T16:00:01Z", "2026-10-15T11:59:00"]:
            with self.subTest(timestamp=timestamp), self.assertRaises(QualityError): build(updateTime=timestamp)

    def test_newer_event_than_summary_is_rejected(self):
        e=event(); e["outageModifiedTime"]="2026-10-15T16:00:00Z"
        with self.assertRaises(QualityError): build([e])

    def test_verified_does_not_assert_crew_awareness(self):
        e=event(); e.update(verified=True, crewAssigned=False)
        self.assertEqual(build([e])["islands"][0]["neighborhoods"][0]["sourceStatus"], "Verified")

    def test_suffixes_do_not_destroy_estate_names(self):
        table = lookup_table()
        self.assertEqual(location("SUBBASE 101 (EL)", table)[1], "101")
        self.assertEqual(location("VICORP LAND 175", table)[0]["name"], "Vicorp Land")
        self.assertEqual(location("PMP GARDEN BLDG-7 (HSE)", table)[1], "Building 7")
        self.assertEqual(location("AGNES FANCY 5-A UP (HSE)", table)[1], "5-A")

    def test_ambiguous_reviewed_alias_is_not_guessed(self):
        table = lookup_table()
        table.append(dict(table[0], id="conflicting-area", island="stCroix"))
        self.assertIsNone(location("AGNES FANCY 5-A", table))

    def test_restored_events_do_not_inflate_active_records(self):
        feed = build([event(customers=0)])
        self.assertTrue(all(i["customersOut"] == 0 and not i["neighborhoods"] for i in feed["islands"]))

    def test_new_estates_use_official_island_references(self):
        table = lookup_table()
        self.assertEqual(location("LA GRANGE 18-B", table)[0]["island"], "stCroix")
        self.assertEqual(location("SOLITUDE 8-B", table)[0]["island"], "stCroix")
        area, plot = location("C-5-B-24,LOVENLUND", table)
        self.assertEqual(area["island"], "stThomas"); self.assertEqual(plot, "C-5-B-24")
        self.assertTrue(all(a.get("evidence", "").startswith("https://") for a in table if a["id"] in ["la-grange","solitude","lovenlund"]))


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.store=Store(self.temporary.name); self.calls=[]

    def loader(self, url):
        self.calls.append(url)
        return [{"mapOptions":{"refreshTime":5}}, {"updateTime":"2026-10-15T15:59:00Z","customersOutNow":5}, [event()]][URLS.index(url)]

    def test_missing_permission_cannot_make_requests(self):
        for values in [{}, dict(allowed(), allow_app_display=False), dict(allowed(), reference=""),
                       dict(allowed(), approved_urls=[]), dict(allowed(), minimum_interval_seconds=30),
                       dict(allowed(), valid_until="2026-10-14T00:00:00Z")]:
            with self.subTest(values=values), self.assertRaises(PermissionRequired):
                collect(self.store, values, now=NOW, loader=self.loader)
        self.assertEqual(self.calls, [])

    def test_verified_snapshot_and_private_raw_provenance_are_saved(self):
        self.assertTrue(collect(self.store, allowed(), now=NOW, loader=self.loader))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.store.delivered()["collectionStatus"], "ok")
        self.assertEqual(len(list(Path(self.temporary.name).glob("raw-*.json"))), 1)

    def test_refresh_is_throttled_including_restarts(self):
        self.assertTrue(collect(self.store, allowed(), now=NOW, loader=self.loader))
        self.assertFalse(collect(Store(self.temporary.name), allowed(), now=NOW+timedelta(seconds=299), loader=self.loader))
        self.assertEqual(len(self.calls), 3)

    def test_failed_snapshot_preserves_last_good_feed_and_marks_delivery(self):
        collect(self.store, allowed(), now=NOW, loader=self.loader)
        prior=self.store.read("feed.json")
        def broken(url):
            value=self.loader(url)
            if url==URLS[1]: value["updateTime"]="invalid timestamp"
            return value
        self.assertFalse(collect(self.store, allowed(), now=NOW+timedelta(minutes=5), loader=broken))
        self.assertEqual(self.store.read("feed.json"), prior)
        self.assertEqual(self.store.delivered()["collectionStatus"], "failed")
        self.assertIn("publication time", self.store.read("last-failure.json")["reason"])

    def test_access_denial_stops_further_requests(self):
        def denied(url): raise HTTPError(url, 403, "Denied", {}, BytesIO())
        self.assertFalse(collect(self.store, allowed(), now=NOW, loader=denied))
        with self.assertRaises(PermissionRequired): collect(self.store, allowed(), now=NOW+timedelta(hours=1), loader=self.loader)
        self.assertEqual(self.calls, [])

    def test_rate_limit_honors_retry_after(self):
        def limited(url): raise HTTPError(url, 429, "Slow down", {"Retry-After":"3600"}, BytesIO())
        self.assertFalse(collect(self.store, allowed(), now=NOW, loader=limited))
        self.assertFalse(collect(self.store, allowed(), now=NOW+timedelta(minutes=30), loader=self.loader))
        self.assertEqual(self.calls, [])

    def test_older_snapshot_cannot_replace_latest(self):
        collect(self.store, allowed(), now=NOW, loader=self.loader)
        def older(url):
            if url==URLS[1]: return {"updateTime":"2026-10-15T15:58:00Z","customersOutNow":0}
            if url==URLS[2]: return []
            return self.loader(url)
        self.assertFalse(collect(self.store, allowed(), now=NOW+timedelta(minutes=5), loader=older))
        self.assertEqual(self.store.read("feed.json")["source"]["updatedAt"], "2026-10-15T15:59:00Z")

    def test_simultaneous_collector_cannot_bypass_throttle(self):
        with (self.store.directory / "collection.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(collect(self.store, allowed(), now=NOW, loader=self.loader))
            self.assertEqual(self.calls, [])

    def test_source_change_without_new_publication_is_refused(self):
        collect(self.store, allowed(), now=NOW, loader=self.loader)
        def changed(url):
            value=self.loader(url)
            if url==URLS[2]: value[0]["verified"]=True
            return value
        self.assertFalse(collect(self.store, allowed(), now=NOW+timedelta(minutes=5), loader=changed))
        self.assertEqual(self.store.read("feed.json")["islands"][0]["neighborhoods"][0]["sourceStatus"], "Predicted")

    def test_redirect_requires_access_review(self):
        def redirect(url): raise PermissionRequired("Unapproved source redirect")
        self.assertFalse(collect(self.store, allowed(), now=NOW, loader=redirect))
        self.assertEqual(self.store.read("health.json")["status"], "blocked")


class APITests(unittest.TestCase):
    def test_checked_in_native_contract_fixture_matches_adapter_output(self):
        fixture=Path(__file__).parent / "fixtures" / "normalized-example.json"
        self.assertEqual(json.loads(fixture.read_text()), build())

    def test_unavailable_is_503_never_empty_success(self):
        with TemporaryDirectory() as directory, TestClient(create_app(Store(directory))) as client:
            response=client.get("/v1/outages")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("islands", response.json())

    def test_serves_validated_feed_and_failure_state_but_never_private_raw(self):
        with TemporaryDirectory() as directory:
            store=Store(directory); store.write("feed.json", build())
            store.write("health.json", {"status":"failed","error":"Source update could not be verified"})
            with TestClient(create_app(store)) as client:
                response=client.get("/v1/outages")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["collectionStatus"], "failed")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(client.get("/raw-a.json").status_code, 404)
                self.assertEqual(client.post("/v1/outages").status_code, 405)
                self.assertFalse(client.get("/health").json()["apiStartsCollector"])


if __name__ == "__main__": unittest.main()
