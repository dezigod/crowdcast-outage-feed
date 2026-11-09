from datetime import datetime, timedelta, timezone
import unittest
from scripts.check_feed_health import problems, source_times

NOW = datetime(2026, 11, 8, 18, tzinfo=timezone.utc)

class RotationHealthTests(unittest.TestCase):
    def feed(self):
        return {"schemaVersion": 1, "status": "ok", "schedule": {
            "checkedAt": NOW.isoformat(), "revisionSeenAt": NOW.isoformat(),
            "extractionStatus": "reviewed", "coverageEnd": (NOW + timedelta(hours=1)).isoformat()}}

    def test_current_reviewed_schedule_is_healthy(self):
        self.assertEqual(problems("rotation", self.feed(), NOW), [])

    def test_unknown_revision_requires_review(self):
        f=self.feed(); f["schedule"]["extractionStatus"]="unavailable"
        self.assertIn("unreviewed schedule revision", " ".join(problems("rotation", f, NOW)))

    def test_fresh_download_does_not_make_expired_dates_current(self):
        f=self.feed(); f["schedule"]["coverageEnd"]=NOW.isoformat()
        self.assertIn("dates have passed", " ".join(problems("rotation", f, NOW)))

    def test_missing_invalid_and_stale_metadata(self):
        f=self.feed(); f["schedule"]["checkedAt"]=(NOW-timedelta(minutes=31)).isoformat()
        self.assertIn("freshness window", " ".join(problems("rotation", f, NOW)))
        f["schedule"]["coverageEnd"]="bad"
        self.assertIn("coverage is missing", " ".join(problems("rotation", f, NOW)))
        f["schedule"]=None
        self.assertIn("unavailable", " ".join(problems("rotation", f, NOW)))
        self.assertIsNone(source_times("rotation",f)["checkedAt"])

    def test_future_check_and_failed_collection(self):
        f=self.feed(); f["status"]="failed"
        f["schedule"]["checkedAt"]=(NOW+timedelta(minutes=1)).isoformat()
        errors=" ".join(problems("rotation",f,NOW))
        self.assertIn("future",errors); self.assertIn("check failed",errors)
