from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from outage_backend.github_delivery import FEED, HEALTH, public_feed, run_once
from outage_backend.map_details import DETAIL_KEYS
from outage_backend.normalize import normalize
from outage_backend.tests.test_pipeline import NOW, allowed, build, event
from outage_backend.collect import URLS


class MapDetailTests(unittest.TestCase):
    def test_every_event_survives_unreviewed_geography_and_group_conflicts(self):
        first = event("first", ["AGNES FANCY 5-A", "UNREVIEWED ESTATE 7"], 30)
        first.update(outageName="Feeder 07A", outagePoint={"lat":18.35,"lng":-64.93},
                     customersOutInitially=35, customersRestored=5, crewAssigned=False,
                     cause="Equipment", outageWorkStatus="Investigating")
        second = event("second", ["AGNES FANCY 5B"], 2)
        second.update(verified=True, outageStartTime="2026-10-14T09:00:00-04:00")
        third = event("third", ["UNKNOWN ISLAND ESTATE 1"], 1)
        feed = build([first, second, third], customersOutNow=35, customersAffected=40,
                     customersRestored=5, customersServed=54723)
        public_feed(feed)
        self.assertEqual(len(feed["events"]), 3)
        details = {e["name"]:e for e in feed["events"] if e["name"]}
        detail = details["Feeder 07A"]
        self.assertIsNone(detail["island"])
        self.assertEqual(detail["neighborhoodIDs"], ["stThomas-agnes-fancy"])
        self.assertEqual(detail["locations"], first["streetsAffected"])
        self.assertEqual(detail["reportedCustomersOut"], 30)
        self.assertEqual(detail["reportedCustomersInitially"], 35)
        self.assertEqual(detail["reportedCustomersRestored"], 5)
        self.assertEqual(detail["point"], {"latitude":18.35,"longitude":-64.93})
        self.assertEqual(detail["startedAt"], "2026-10-15T14:00:00Z")
        self.assertEqual(detail["cause"], "Equipment")
        self.assertEqual(detail["workStatus"], "Investigating")
        self.assertFalse(detail["crewAssigned"])
        self.assertEqual(detail["sourceStatus"], "Predicted")
        area = feed["islands"][0]["neighborhoods"][0]
        self.assertIsNone(area["startedAt"])
        self.assertEqual(area["sourceStatus"], "Unconfirmed")
        self.assertEqual(feed["sourceSummary"]["totalOutages"], 3)
        self.assertEqual(feed["sourceSummary"]["customersOut"], 35)
        self.assertEqual(feed["sourceSummary"]["eventCustomersOut"], 33)
        self.assertEqual(feed["sourceSummary"]["customerCountDifference"], 2)
        self.assertTrue(feed["hasConflictingTotals"])
        self.assertTrue(all(i["customersOut"] is None for i in feed["islands"]))

    def test_territory_summary_and_chart_keep_source_values_and_clock(self):
        feed = build(customersServed=54723, customersAffected=8, customersRestored=3,
            streetsAffected=["AGNES FANCY", "AGNES FANCY 5-A"], hourlyCustomersOut=[
                {"customers":1,"eventTime":"2026-10-14T12:00:00-04:00"},
                {"customers":5,"eventTime":"2026-10-15T11:00:00-04:00"}])
        summary = feed["sourceSummary"]
        self.assertEqual(summary["customersServed"],54723)
        self.assertEqual(summary["customersAffected"],8)
        self.assertEqual(summary["customersRestored"],3)
        self.assertEqual(summary["locations"],["AGNES FANCY", "AGNES FANCY 5-A"])
        self.assertEqual(summary["hourlyCustomersOut"],[
            {"at":"2026-10-14T16:00:00Z","customersOut":1},
            {"at":"2026-10-15T15:00:00Z","customersOut":5}])
        self.assertEqual(feed["source"]["updatedAt"],"2026-10-15T15:59:00Z")

    def test_restored_and_duplicate_records_do_not_inflate_outage_total(self):
        active = event()
        restored = event("restored", customers=0)
        restored.update(outageEndTime="2026-10-15T11:00:00-04:00")
        feed = normalize({"updateTime":"2026-10-15T15:59:00Z","customersOutNow":5},
                         [active,deepcopy(active),restored],checked_at=NOW,raw_sha256="a"*64)
        self.assertEqual(len(feed["events"]),2)
        self.assertEqual(feed["sourceSummary"]["totalOutages"],1)
        self.assertEqual(feed["sourceSummary"]["eventCustomersOut"],5)
        self.assertEqual(sum(e["sourceStatus"] == "Restored" for e in feed["events"]),1)
        self.assertEqual(feed["islands"][0]["customersOut"],5)

    def test_optional_missing_or_invalid_values_never_become_guesses(self):
        for point in (None, {"lat":91,"lng":-64}, {"lat":True,"lng":-64}, {"lat":float("nan"),"lng":-64}):
            raw = event()
            raw.update(outagePoint=point, customersRestored=True, customersOutInitially="5", crewAssigned="yes",
                       cause={}, estimatedTimeOfRestoral="Unknown", outageStartTime="2026-10-15T10:00:00")
            feed = build([raw],customersServed="54723")
            detail = feed["events"][0]
            for key in ("point","reportedCustomersRestored","reportedCustomersInitially","crewAssigned","cause","estimatedRestoration","startedAt"):
                self.assertIsNone(detail[key])
            self.assertIsNone(feed["sourceSummary"]["customersServed"])
            public_feed(feed)

    def test_invalid_history_is_not_published_as_zero_or_future_observations(self):
        feed = build(hourlyCustomersOut=[None, {"eventTime":"2026-10-15T11:00:00","customers":1},
            {"eventTime":"2026-10-15T17:00:00Z","customers":5},
            {"eventTime":"2026-10-15T15:00:00Z","customers":True},
            {"eventTime":"2026-10-15T15:00:00Z","customers":0}])
        self.assertEqual(feed["sourceSummary"]["hourlyCustomersOut"],[{"at":"2026-10-15T15:00:00Z","customersOut":0}])

    def test_extension_preserves_hash_identity_and_excludes_unreviewed_raw_fields(self):
        raw = event()
        raw.update(privateContact="DO NOT PUBLISH", permissionReference="DO NOT PUBLISH")
        feed = build([raw])
        self.assertEqual(feed["events"][0]["id"],feed["islands"][0]["neighborhoods"][0]["sourceEventIDs"][0])
        self.assertNotIn("DO NOT PUBLISH",json.dumps(feed))
        self.assertNotIn("outageRecID",json.dumps(feed))
        for layer in ("summary","event","point","history"):
            feed = build(hourlyCustomersOut=[{"customers":1,"eventTime":"2026-10-15T15:00:00Z"}])
            feed["events"][0]["point"]={"latitude":18.35,"longitude":-64.93}
            target={"summary":feed["sourceSummary"],"event":feed["events"][0],
                    "point":feed["events"][0]["point"],"history":feed["sourceSummary"]["hourlyCustomersOut"][0]}[layer]
            target["rawPrivate"]="must not publish"
            with self.subTest(layer=layer), self.assertRaises(ValueError):public_feed(feed)

    def test_legacy_feed_can_load_and_next_collection_upgrades_atomically(self):
        legacy = {k:v for k,v in build().items() if k not in DETAIL_KEYS}
        public_feed(legacy)
        previous={FEED:legacy, HEALTH:{"status":"ok","error":None,"nextAttemptAt":"2026-10-15T16:00:00Z"}}
        # Same timestamp and source fingerprint are allowed during format migration.
        source=[{"mapOptions":{"refreshTime":5}},
                {"updateTime":"2026-10-15T15:59:00Z","customersOutNow":5},[event()]]
        import hashlib
        raw=dict(zip(("settings","summary","outages"),source))
        legacy["snapshotID"]=hashlib.sha256(json.dumps(raw,sort_keys=True).encode()).hexdigest()
        writes=[]
        result=run_once(previous,allowed(),lambda files:writes.append(deepcopy(files)),
                        now=NOW,loader=lambda url:source[URLS.index(url)])
        self.assertEqual(result,"verified")
        self.assertEqual(len(writes),2)
        self.assertTrue(DETAIL_KEYS.issubset(writes[-1][FEED]))
        public_feed(writes[-1][FEED])

    def test_partial_extension_or_inconsistent_counts_are_rejected(self):
        feed=build(); del feed["events"]
        with self.assertRaises(ValueError):public_feed(feed)
        for field in ("totalOutages","eventCustomersOut","customerCountDifference"):
            feed=build();feed["sourceSummary"][field]+=1
            with self.subTest(field=field),self.assertRaises(ValueError):public_feed(feed)


if __name__ == "__main__":
    unittest.main()
