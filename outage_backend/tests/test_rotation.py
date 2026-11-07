from datetime import datetime, timedelta, timezone
import unittest
from urllib.error import HTTPError

from outage_backend.rotation import ALERTS, ROTATION, check_rotation, schedule_url

NOW = datetime(2026, 11, 6, 20, tzinfo=timezone.utc)
PATH = "/docs/default-source/outage-schedule/sttstj-rotational-outage-schedule-doc.pdf"
URL = "https://www.viwapa.vi" + PATH + "?sfvrsn=abc_401"
HTML = f'<a href="{PATH}?sfvrsn=abc_401">Download</a>'.encode()
MODIFIED = "Wed, 04 Nov 2026 16:19:51 GMT"


class RotationTests(unittest.TestCase):
    def test_schedule_revision_and_conditional_check(self):
        published = []

        def original(url, **kwargs):
            if url == ALERTS:
                return HTML, None
            self.assertEqual(url, URL)
            self.assertEqual(len(published), 1, "Reserve before fetching")
            return b"%PDF-original", MODIFIED

        self.assertEqual(check_rotation(None, published.append, now=NOW, loader=original), "ok")
        first = published[-1][ROTATION]
        self.assertIs(first.pop("subjectToChange"), True)  # Simulate a legacy stored record.

        def unchanged(url, **kwargs):
            if url == ALERTS:
                return HTML, None
            self.assertEqual(kwargs["previous_modified"], MODIFIED)
            return None, MODIFIED

        self.assertEqual(check_rotation(first, published.append, now=NOW, loader=unchanged), "deferred")
        self.assertEqual(check_rotation(first, published.append, now=NOW + timedelta(minutes=5),
                                        loader=unchanged), "ok")
        second = published[-1][ROTATION]
        self.assertEqual(second["schedule"]["revisionSeenAt"], first["schedule"]["revisionSeenAt"])
        self.assertGreater(second["schedule"]["checkedAt"], first["schedule"]["checkedAt"])

        def changed(url, **kwargs):
            return (HTML, None) if url == ALERTS else (b"%PDF-revision", None)

        check_rotation(second, published.append, now=NOW + timedelta(minutes=10), loader=changed)
        self.assertIs(published[-1][ROTATION]["subjectToChange"], True)
        third = published[-1][ROTATION]["schedule"]
        self.assertEqual(third["url"], first["schedule"]["url"])
        self.assertNotEqual(third["fingerprint"], first["schedule"]["fingerprint"])
        self.assertEqual(third["checkedAt"], third["revisionSeenAt"])

    def test_official_link_only_and_ambiguity(self):
        self.assertEqual(schedule_url(HTML), URL)
        with self.assertRaises(ValueError):
            schedule_url(HTML.replace(PATH.encode(), ("https://example.com" + PATH).encode()))
        with self.assertRaises(ValueError):
            schedule_url(HTML + HTML.replace(b"abc_401", b"abc_402"))

    def test_rate_limit_and_access_denial_preserve_prior_schedule(self):
        published = []
        check_rotation(None, published.append, now=NOW,
                       loader=lambda url, **_: (HTML, None) if url == ALERTS else (b"%PDF-first", None))
        first = published[-1][ROTATION]

        def limit(url, **kwargs):
            raise HTTPError(url, 429, "rate limit", {"Retry-After": "1200"}, None)

        self.assertEqual(check_rotation(first, published.append, now=NOW + timedelta(minutes=5),
                                        loader=limit), "failed")
        failed = published[-1][ROTATION]
        self.assertEqual(failed["schedule"], first["schedule"])
        self.assertEqual(failed["nextAttemptAt"], "2026-11-06T20:25:00Z")

        def deny(url, **kwargs):
            raise HTTPError(url, 403, "denied", {}, None)

        self.assertEqual(check_rotation(failed, published.append, now=NOW + timedelta(minutes=25),
                                        loader=deny), "blocked")
        blocked = published[-1][ROTATION]
        self.assertEqual(blocked["schedule"], first["schedule"])
        self.assertEqual(check_rotation(blocked, published.append, now=NOW + timedelta(days=1),
                                        loader=deny), "deferred")
