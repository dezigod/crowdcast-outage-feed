from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import json
from pathlib import Path
import unittest
from urllib.error import HTTPError

from outage_backend.collect import PermissionRequired, URLS
from outage_backend.github_delivery import FEED, HEALTH, GitHubDataBranch, public_feed, public_health, run_once
from outage_backend.tests.test_pipeline import NOW, allowed, build, event


class GitHubDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.previous = {}
        self.published = []
        self.calls = []

    def publish(self, value):
        self.published.append(deepcopy(value))
        self.previous.update(deepcopy(value))

    def loader(self, url):
        self.calls.append(url)
        self.assertTrue(self.published, 'Throttle must be persisted remotely before source access')
        return [{"mapOptions": {"refreshTime": 5}},
                {"updateTime": "2026-10-15T15:59:00Z", "customersOutNow": 5}, [event()]][URLS.index(url)]

    def run_collection(self, **changes):
        return run_once(self.previous, allowed(), self.publish, now=NOW, loader=self.loader, **changes)

    def test_initial_empty_delivery_does_not_fabricate_zero(self):
        def unavailable(url):
            self.calls.append(url)
            raise OSError('SYNTHETIC PRIVATE DOWNLOAD ERROR')
        self.assertEqual(run_once({}, allowed(), self.publish, now=NOW, loader=unavailable), 'failed')
        self.assertNotIn(FEED, self.previous)
        self.assertEqual(self.previous[HEALTH]['status'], 'failed')
        self.assertNotIn('PRIVATE', json.dumps(self.previous))

    def test_throttle_is_published_before_requests_and_final_feed_is_validated(self):
        self.assertEqual(self.run_collection(), 'verified')
        self.assertEqual(len(self.published), 2)
        self.assertNotIn(FEED, self.published[0])
        expected = build()
        expected['snapshotID'] = self.previous[FEED]['snapshotID']
        self.assertEqual(self.previous[FEED], expected)
        self.assertEqual(len(expected['snapshotID']), 64)
        self.assertEqual(self.previous[HEALTH]['status'], 'ok')

    def test_permission_record_and_raw_source_are_never_published(self):
        self.run_collection()
        text = json.dumps(self.published)
        for forbidden in ['permissionReference', allowed()['reference'], 'streetsAffected', 'outageRecID', 'raw-', 'approved_urls']:
            self.assertNotIn(forbidden, text)
        self.assertTrue(all(set(p).issubset({HEALTH, FEED}) for p in self.published))

    def test_missing_permission_cannot_publish_or_download(self):
        with self.assertRaises(PermissionRequired):
            run_once({}, {}, self.publish, now=NOW, loader=self.loader)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.published, [])

    def test_restart_preserves_throttle(self):
        self.run_collection()
        self.calls.clear(); self.published.clear()
        self.assertEqual(run_once(self.previous, allowed(), self.publish, now=NOW+timedelta(seconds=299), loader=self.loader), 'deferred')
        self.assertEqual(self.calls, []); self.assertEqual(self.published, [])

    def test_remote_reservation_failure_prevents_all_source_requests(self):
        def failed(value): raise OSError('Synthetic GitHub outage')
        with self.assertRaises(OSError):
            run_once({}, allowed(), failed, now=NOW, loader=self.loader)
        self.assertEqual(self.calls, [])

    def test_source_failure_preserves_last_feed_and_marks_it_failed(self):
        self.run_collection(); original = deepcopy(self.previous[FEED])
        def broken(url): raise OSError('Unavailable')
        run_once(self.previous, allowed(), self.publish, now=NOW+timedelta(minutes=5), loader=broken)
        delivered = self.previous[FEED]
        self.assertEqual(delivered['snapshotID'], original['snapshotID'])
        self.assertEqual(delivered['source'], original['source'])
        self.assertEqual(delivered['collectionStatus'], 'failed')

    def test_access_denial_survives_fresh_runners_and_stops_requests(self):
        def denied(url): raise HTTPError(url, 403, 'Denied', {}, BytesIO())
        self.assertEqual(run_once({}, allowed(), self.publish, now=NOW, loader=denied), 'blocked')
        self.assertEqual(run_once(self.previous, allowed(), self.publish, now=NOW+timedelta(days=1), loader=self.loader), 'blocked')
        self.assertEqual(self.calls, [])

    def test_rate_limit_survives_fresh_runners(self):
        def limited(url): raise HTTPError(url, 429, 'Limited', {'Retry-After': '3600'}, BytesIO())
        run_once({}, allowed(), self.publish, now=NOW, loader=limited)
        run_once(self.previous, allowed(), self.publish, now=NOW+timedelta(minutes=30), loader=self.loader)
        self.assertEqual(self.calls, [])

    def test_conflicting_totals_keep_listings_and_withhold_counts(self):
        def conflict(url):
            result = self.loader(url)
            if url == URLS[1]: result['customersOutNow'] = 7
            return result
        run_once({}, allowed(), self.publish, now=NOW, loader=conflict)
        feed = self.previous[FEED]
        self.assertTrue(feed['hasConflictingTotals'])
        self.assertTrue(all(i['customersOut'] is None and i['coverage'] == 'partial' for i in feed['islands']))

    def test_publication_rejects_extra_private_fields_at_every_level(self):
        for layer in ['envelope', 'source', 'island', 'area']:
            feed = build()
            target = {'envelope': feed, 'source': feed['source'], 'island': feed['islands'][0],
                      'area': feed['islands'][0]['neighborhoods'][0]}[layer]
            target['raw_private'] = 'must not be published'
            with self.subTest(layer=layer), self.assertRaises(ValueError): public_feed(feed)
        with self.assertRaises(ValueError): public_health({'status': 'failed', 'error': 'private source details', 'nextAttemptAt': '2026-10-15T16:05:00Z'})

    def test_corrupt_restored_state_stops_before_source_requests(self):
        with self.assertRaises(ValueError):
            run_once({FEED: {'islands': []}}, allowed(), self.publish, now=NOW, loader=self.loader)
        self.assertEqual(self.calls, [])

    def test_missing_collection_state_cannot_bypass_a_saved_throttle(self):
        with self.assertRaises(ValueError):
            run_once({FEED: build()}, allowed(), self.publish, now=NOW, loader=self.loader)
        self.assertEqual(self.calls, [])

    def test_concurrent_branch_change_fails_without_force_or_retry(self):
        remote = GitHubDataBranch('example/test', 'synthetic-token')
        remote.tip = 'parent'; remote.tree = 'base-tree'; remote.previous = {}
        requests = []
        def request(path, **kwargs):
            requests.append((path, kwargs))
            if path.endswith('/refs/heads/data'):
                raise HTTPError(path, 422, 'Concurrent change', {}, BytesIO())
            return {'sha': 'new-tree' if path.endswith('/trees') else 'new-commit'}
        remote.request = request
        with self.assertRaises(HTTPError): remote.publish({FEED: build()})
        self.assertEqual(len(requests), 3)
        self.assertFalse(requests[-1][1]['payload']['force'])
        self.assertEqual(remote.tip, 'parent')
        self.assertEqual(remote.previous, {})

    def test_remote_publication_is_atomic_nonforcing_and_only_allowlisted_files(self):
        remote = GitHubDataBranch('example/test', 'synthetic-token')
        remote.tip = 'parent'; remote.tree = 'base-tree'; remote.previous = {}
        requests = []
        def request(path, **kwargs):
            requests.append((path, kwargs))
            return {'sha': 'new-tree' if path.endswith('/trees') else 'new-commit'}
        remote.request = request
        remote.publish({FEED: build()})
        self.assertEqual(requests[-1][1]['payload'], {'sha': 'new-commit', 'force': False})
        self.assertEqual(requests[1][1]['payload']['parents'], ['parent'])
        with self.assertRaises(ValueError): remote.publish({'permission.json': allowed()})

    def test_scheduled_workflow_has_gates_no_cache_no_raw_artifacts_no_phone_secrets(self):
        root = Path(__file__).resolve().parents[2]
        text = (root/'.github/workflows/update-outages.yml').read_text()
        for expected in ["COLLECTION_ENABLED == 'true'", "persist-credentials: false", 'cancel-in-progress: false', 'contents: write', 'ubuntu-latest']:
            self.assertIn(expected, text)
        for forbidden in ['pull_request_target', 'actions/cache', 'upload-artifact', 'macos-', 'self-hosted']:
            self.assertNotIn(forbidden, text)

    def test_explicit_schedule_requests_every_five_minutes_on_github(self):
        root = Path(__file__).resolve().parents[2]
        text = (root/'.github/workflows/update-outages.yml').read_text()
        expression = text.split("cron: '", 1)[1].split("'", 1)[0]
        minute, hour, day, month, weekday = expression.split()
        slots = [int(value) for value in minute.split(',')]
        self.assertEqual(slots, list(range(2, 60, 5)))
        self.assertTrue(all(value == '*' for value in [hour, day, month, weekday]))
        self.assertIn('run-name: WAPA outage check (${{ github.event_name }})', text)


if __name__ == '__main__': unittest.main()
