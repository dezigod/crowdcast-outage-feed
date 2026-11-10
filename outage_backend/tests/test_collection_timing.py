from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import BytesIO
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from outage_backend.collect import PermissionRequired, URLS, deadline_stamp
from outage_backend.github_delivery import HEALTH, load_scheduled_state, run_once
from outage_backend.normalize import date, stamp
from outage_backend.rotation import ROTATION


NOW = datetime(2026, 11, 10, 15, 40, 28, tzinfo=timezone.utc)


def health(due, *, status="ok", error=None):
    return {HEALTH: {"status": status, "error": error, "nextAttemptAt": due.isoformat()}}


def allowed():
    return dict(allow_automated_collection=True, allow_app_display=True,
                reference="SYNTHETIC TEST PERMISSION ONLY", minimum_interval_seconds=300,
                approved_urls=list(URLS))


class Clock:
    def __init__(self):
        self.now = NOW
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class Remote:
    def __init__(self, *states):
        self.states = list(states)
        self.loads = []
        self.published = []

    def load(self, *, include_rotation=False):
        self.loads.append(include_rotation)
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(state, Exception):
            raise state
        self.previous = deepcopy(state)
        return self.previous

    def publish(self, value):
        self.published.append(deepcopy(value))
        self.previous.update(deepcopy(value))


class CollectionTimingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.requests = []

    def loader(self, url):
        self.requests.append((url, self.clock()))
        return [
            {"mapOptions": {"refreshTime": 5}},
            {"updateTime": "2026-11-10T15:39:00Z", "customersOutNow": 5},
            [{"outageRecID": "synthetic", "streetsAffected": ["AGNES FANCY 5-A"],
              "customersOutNow": 5, "outageStartTime": "2026-11-10T14:00:00Z",
              "verified": False, "estimatedTimeOfRestoral": None, "outageEndTime": None,
              "outageModifiedTime": "2026-11-10T15:39:00Z"}],
        ][URLS.index(url)]

    def load(self, remote, **kwargs):
        return load_scheduled_state(remote, clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def collect(self, remote, approved=None):
        previous = self.load(remote)
        return run_once(previous, allowed() if approved is None else approved,
                        remote.publish, now=self.clock(), loader=self.loader)

    def test_two_second_jitter_waits_until_full_five_minute_interval(self):
        due = NOW + timedelta(seconds=2)
        prior_attempt = due - timedelta(seconds=300)
        remote = Remote(health(due))
        self.assertEqual(self.collect(remote), "verified")
        self.assertEqual(self.clock.sleeps, [2])
        self.assertEqual(remote.loads, [False, False])
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all((at - prior_attempt).total_seconds() >= 300 for _, at in self.requests))
        self.assertEqual(date(remote.previous[HEALTH]["nextAttemptAt"]), due + timedelta(seconds=300))

    def test_exactly_one_minute_wait_is_allowed(self):
        remote = Remote(health(NOW + timedelta(seconds=60)))
        self.assertEqual(self.collect(remote), "verified")
        self.assertEqual(self.clock.sleeps, [60])

    def test_more_than_one_minute_defers_without_waiting(self):
        remote = Remote(health(NOW + timedelta(seconds=60, microseconds=1)))
        self.assertEqual(self.collect(remote), "deferred")
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(remote.loads, [False])
        self.assertEqual(self.requests, [])
        self.assertEqual(remote.published, [])

    def test_due_or_first_collection_never_waits(self):
        for state in [{}, health(NOW), health(NOW - timedelta(seconds=1))]:
            with self.subTest(state=state):
                remote = Remote(state)
                self.assertEqual(self.collect(remote), "verified")
                self.assertEqual(remote.loads, [False])
        self.assertEqual(self.clock.sleeps, [])

    def test_blocked_state_never_waits_or_contacts_source(self):
        remote = Remote(health(NOW + timedelta(seconds=2), status="blocked",
                               error="Source access needs review"))
        self.assertEqual(self.collect(remote), "blocked")
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(remote.loads, [False])
        self.assertEqual(self.requests, [])
        self.assertEqual(remote.published, [])

    def test_long_rate_limit_backoff_is_not_shortened(self):
        remote = Remote(health(NOW + timedelta(hours=1), status="failed", error="Source rate limit"))
        self.assertEqual(self.collect(remote), "deferred")
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.requests, [])
        self.assertEqual(remote.published, [])

    def test_near_backoff_expiry_waits_for_saved_deadline(self):
        due = NOW + timedelta(seconds=30)
        remote = Remote(health(due, status="failed", error="Source rate limit"))
        self.assertEqual(self.collect(remote), "verified")
        self.assertEqual(self.clock.sleeps, [30])
        self.assertTrue(all(at >= due for _, at in self.requests))

    def test_new_remote_reservation_is_rechecked_without_another_wait(self):
        remote = Remote(health(NOW + timedelta(seconds=2)), health(NOW + timedelta(seconds=4)))
        self.assertEqual(self.collect(remote), "deferred")
        self.assertEqual(self.clock.sleeps, [2])
        self.assertEqual(remote.loads, [False, False])
        self.assertEqual(self.requests, [])
        self.assertEqual(remote.published, [])

    def test_new_remote_access_refusal_stops_collection(self):
        remote = Remote(health(NOW + timedelta(seconds=2)),
                        health(NOW + timedelta(seconds=302), status="blocked",
                               error="Source access needs review"))
        self.assertEqual(self.collect(remote), "blocked")
        self.assertEqual(self.requests, [])
        self.assertEqual(remote.published, [])

    def test_early_wake_still_defers(self):
        remote = Remote(health(NOW + timedelta(seconds=2)))
        def early_sleep(seconds):
            self.clock.sleep(seconds / 2)
        previous = load_scheduled_state(remote, clock=self.clock, sleep=early_sleep)
        self.assertEqual(run_once(previous, allowed(), remote.publish, now=self.clock(),
                                  loader=self.loader), "deferred")
        self.assertEqual(self.requests, [])
        self.assertEqual(remote.published, [])

    def test_permission_expiring_during_wait_cannot_collect(self):
        approved = dict(allowed(), valid_until=stamp(NOW + timedelta(seconds=1)))
        remote = Remote(health(NOW + timedelta(seconds=2)))
        with self.assertRaises(PermissionRequired):
            self.collect(remote, approved)
        self.assertEqual(self.requests, [])
        self.assertEqual(remote.published, [])

    def test_wait_refreshes_rotation_state_too(self):
        initial = dict(health(NOW + timedelta(seconds=2)), **{ROTATION: {"testRevision": 1}})
        refreshed = dict(health(NOW + timedelta(seconds=2)), **{ROTATION: {"testRevision": 2}})
        remote = Remote(initial, refreshed)
        previous = self.load(remote, include_rotation=True)
        self.assertEqual(remote.loads, [True, True])
        self.assertEqual(previous[ROTATION], refreshed[ROTATION])

    def test_failed_reload_does_not_fall_back_to_old_state(self):
        remote = Remote(health(NOW + timedelta(seconds=2)), OSError("synthetic remote failure"))
        with self.assertRaises(OSError):
            self.collect(remote)
        self.assertEqual(self.requests, [])
        self.assertEqual(remote.published, [])

    def test_direct_run_once_call_stays_nonblocking(self):
        remote = Remote(health(NOW + timedelta(seconds=2)))
        with patch("outage_backend.github_delivery.time.sleep") as sleep:
            self.assertEqual(run_once(remote.load(), allowed(), remote.publish, now=NOW,
                                      loader=self.loader), "deferred")
        sleep.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_whole_second_deadline_keeps_existing_value(self):
        self.assertEqual(deadline_stamp(NOW), stamp(NOW))

    def test_fractional_deadline_rounds_up_instead_of_down(self):
        for microseconds in [1, 900000, 999999]:
            with self.subTest(microseconds=microseconds):
                value = NOW.replace(microsecond=microseconds)
                saved = date(deadline_stamp(value))
                self.assertEqual(saved, NOW + timedelta(seconds=1))
                self.assertGreaterEqual(saved, value)

    def test_fractional_start_reserves_at_least_full_approved_interval(self):
        self.clock.now = NOW.replace(microsecond=900000)
        started = self.clock()
        remote = Remote({})
        self.assertEqual(self.collect(remote), "verified")
        due = date(remote.previous[HEALTH]["nextAttemptAt"])
        self.assertEqual(due, NOW + timedelta(seconds=301))
        self.assertTrue(all(date(update[HEALTH]["nextAttemptAt"]) >= started + timedelta(seconds=300)
                            for update in remote.published))
        self.clock.now = started + timedelta(seconds=300)
        self.requests.clear()
        self.assertEqual(run_once(remote.previous, allowed(), remote.publish, now=self.clock(),
                                  loader=self.loader), "deferred")
        self.assertEqual(self.requests, [])
        self.clock.now = due
        self.assertEqual(run_once(remote.previous, allowed(), remote.publish, now=self.clock(),
                                  loader=self.loader), "verified")

    def test_viewer_refresh_enlargement_is_rounded_up(self):
        self.clock.now = NOW.replace(microsecond=900000)
        remote = Remote({})
        def longer_refresh(url):
            return {"mapOptions": {"refreshTime": 10}} if url == URLS[0] else self.loader(url)
        self.assertEqual(run_once(remote.load(), allowed(), remote.publish, now=self.clock(),
                                  loader=longer_refresh), "verified")
        self.assertGreaterEqual(date(remote.published[0][HEALTH]["nextAttemptAt"]),
                                self.clock() + timedelta(seconds=300))
        self.assertGreaterEqual(date(remote.previous[HEALTH]["nextAttemptAt"]),
                                self.clock() + timedelta(seconds=600))
        self.assertEqual(date(remote.previous[HEALTH]["nextAttemptAt"]), NOW + timedelta(seconds=601))

    def rate_limited(self, header, approved=None):
        remote = Remote({})
        def limited(url):
            raise HTTPError(url, 429, "Synthetic rate limit", {"Retry-After": header}, BytesIO())
        self.assertEqual(run_once(remote.load(), approved or allowed(), remote.publish,
                                  now=self.clock(), loader=limited), "failed")
        return date(remote.previous[HEALTH]["nextAttemptAt"])

    def test_numeric_retry_after_never_rounds_below_its_required_wait(self):
        self.clock.now = NOW.replace(microsecond=900000)
        for seconds in [1, 300, 301, 3600]:
            with self.subTest(seconds=seconds):
                due = self.rate_limited(str(seconds))
                self.assertGreaterEqual(due, self.clock() + timedelta(seconds=max(300, seconds)))
                self.assertEqual(due.microsecond, 0)

    def test_retry_after_future_http_date_is_not_shortened(self):
        self.clock.now = NOW.replace(microsecond=900000)
        requested = NOW + timedelta(hours=1)
        due = self.rate_limited(format_datetime(requested, usegmt=True))
        self.assertEqual(due, requested)

    def test_retry_after_does_not_shorten_longer_approved_interval(self):
        self.clock.now = NOW.replace(microsecond=900000)
        approved = dict(allowed(), minimum_interval_seconds=600)
        for header in ["1", format_datetime(NOW + timedelta(minutes=5), usegmt=True)]:
            with self.subTest(header=header):
                due = self.rate_limited(header, approved)
                self.assertGreaterEqual(due, self.clock() + timedelta(seconds=600))
                self.assertEqual(due, NOW + timedelta(seconds=601))


if __name__ == "__main__":
    unittest.main()
