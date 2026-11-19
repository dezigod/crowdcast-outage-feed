"""Offline incident reproductions; these tests never contact a provider."""
import io
import json
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from scripts import check_feed_health as monitor
from outage_backend.collect import URLS, collect
from outage_backend.github_delivery import FEED, run_once
from outage_backend.tests.test_pipeline import allowed, event
from outage_backend.rotation import ALERTS, ROTATION, check_rotation, schedule_url
from outage_backend.collect import PermissionRequired

NOW = datetime(2026, 11, 19, 20, tzinfo=timezone.utc)
PDF = 'https://www.viwapa.vi/docs/default-source/outage-schedule/sttstj-rotational-outage-schedule-doc.pdf?sfvrsn=abc_1'
HTML = ('<a href="' + PDF + '">Schedule</a>').encode()
PNG_HTML = HTML.replace(b'.pdf?', b'.png?')


class PublishedReadRecoveryTests(unittest.TestCase):
    def run_read(self, outcomes, limit=1000):
        opener = Mock()
        def respond(request, timeout):
            value = outcomes.pop(0)
            if isinstance(value, Exception):
                raise value
            return io.BytesIO(value)
        opener.open.side_effect = respond
        with patch.object(monitor.urllib.request, 'build_opener', return_value=opener), \
                patch.object(monitor.time, 'sleep') as sleep:
            result = monitor.published_json(monitor.CONFIG['outage'][0], limit)
        return result, opener, sleep

    def test_transient_failure_recovers_with_same_cache_bucket(self):
        result, opener, sleep = self.run_read([URLError('secret-token-do-not-log'), b'{', b'{"source":{}}'])
        self.assertEqual(result, {'source': {}})
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(len({c.args[0].full_url for c in opener.open.call_args_list}), 1)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 3])

    def test_retry_exhaustion_preserves_safe_diagnostics(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(monitor.PublishedReadError) as caught:
            self.run_read([URLError('secret-token-do-not-log')] * 3)
        self.assertEqual(caught.exception.attempts, 3)
        self.assertNotIn('secret-token', output.getvalue())
        self.assertIn('network_or_incomplete_response', output.getvalue())

    def test_http_503_without_retry_after_can_recover(self):
        result, opener, _ = self.run_read([HTTPError('https://github.invalid', 503, 'temporary', {}, io.BytesIO()), b'{}'])
        self.assertEqual(result, {})
        self.assertEqual(opener.open.call_count, 2)

    def test_denial_rate_limit_and_retry_after_are_not_retried(self):
        for status, headers in [(401, {}), (403, {}), (429, {}), (503, {'Retry-After': '600'})]:
            with self.subTest(status=status), self.assertRaises(monitor.PublishedReadError) as caught:
                self.run_read([HTTPError('https://github.invalid', status, 'secret', headers, io.BytesIO())])
            self.assertEqual(caught.exception.attempts, 1)
            self.assertEqual(caught.exception.http_status, status)

    def test_redirect_size_and_wrong_envelope_are_not_retried(self):
        for value, limit, category in [(monitor.PublishedReadError('unexpected_redirect'), 1000, 'unexpected_redirect'),
                                       (b'xxx', 2, 'response_too_large'), (b'[]', 1000, 'invalid_json_envelope')]:
            with self.subTest(category=category), self.assertRaises(monitor.PublishedReadError) as caught:
                self.run_read([value], limit)
            self.assertEqual(caught.exception.category, category)

    def test_provider_urls_cannot_be_retried_by_delivery_helper(self):
        with patch.object(monitor.urllib.request, 'build_opener') as opener:
            with self.assertRaises(monitor.PublishedReadError):
                monitor.published_json(ALERTS, 1000)
        opener.assert_not_called()


class IncidentLifecycleTests(unittest.TestCase):
    def test_read_failure_does_not_erase_or_reannounce_rotation_incident(self):
        # Stateful issue store exercises actual alert create/edit/comment/close.
        issues, mutations = {}, []
        def gh(*args):
            if args[0] == 'api':
                return json.dumps([list(issues.values())])
            operation = args[1]
            mutations.append(operation)
            if operation == 'create':
                title = args[args.index('--title') + 1]
                body = args[args.index('--body') + 1]
                issues[title] = dict(title=title, number=len(issues)+1, body=body)
            elif operation == 'close':
                title = next(k for k,v in issues.items() if str(v['number']) == str(args[2]))
                del issues[title]
            elif operation == 'edit':
                row = next(v for v in issues.values() if str(v['number']) == str(args[2]))
                row['body'] = args[args.index('--body') + 1]
            return ''
        with patch.object(monitor, 'gh', side_effect=gh), \
                patch.object(monitor, 'problems', return_value=['Access review required', 'Unreviewed revision']), \
                patch.object(monitor, 'source_times', return_value={}):
            with patch.object(monitor, 'published_json', return_value={}):
                self.assertEqual(monitor.run_check('rotation', 'example/test', deduplicate=True), 1)
            old = deepcopy(issues['WAPA rotation schedule needs review'])
            with patch.object(monitor, 'published_json', side_effect=monitor.PublishedReadError('network_error', 3)):
                self.assertEqual(monitor.run_check('rotation', 'example/test', deduplicate=True), 1)
            self.assertEqual(issues['WAPA rotation schedule needs review'], old)
            with patch.object(monitor, 'published_json', return_value={}):
                self.assertEqual(monitor.run_check('rotation', 'example/test', deduplicate=True), 0)
            self.assertEqual(issues['WAPA rotation schedule needs review'], old)
            self.assertEqual(mutations, ['create', 'create', 'close'])

    def test_missing_cruise_state_cannot_close_existing_source_incident(self):
        with patch.object(monitor, 'published_json', side_effect=[{}, OSError('unavailable')]), \
                patch.object(monitor, 'problems', return_value=[]), \
                patch.object(monitor, 'source_times', return_value={}), patch.object(monitor, 'alert', return_value='new_incident') as alert:
            self.assertEqual(monitor.run_check('cruise', 'example/test'), 1)
        self.assertEqual(alert.call_count, 1)
        self.assertEqual(alert.call_args.kwargs['title'], 'Cruise feed delivery unavailable')

    def test_known_delivery_failure_stays_unhealthy_without_repeat_failure_notification(self):
        output = io.StringIO()
        with patch.object(monitor, 'published_json', side_effect=OSError('temporary')), \
                patch.object(monitor, 'alert', return_value='known_incident'), redirect_stdout(output):
            self.assertEqual(monitor.run_check('outage', 'example/test'), 0)
        self.assertIn('"healthy": false', output.getvalue())
        self.assertIn('"incidentScope": "delivery"', output.getvalue())
        self.assertIn('"repeatNotificationSuppressed": true', output.getvalue())

    def test_blocked_empty_midnight_rollover_preserves_real_failures(self):
        feed = {'schemaVersion': 1, 'status': 'blocked', 'schedule': {
            'day': '2026-11-18', 'dayStatus': 'unreviewed', 'extractionStatus': 'unavailable',
            'windows': [], 'coverageStart': None, 'coverageEnd': None, 'checkedAt': '2026-11-16T18:00:00Z'}}
        midnight = NOW.replace(hour=4, minute=0, second=20)
        errors = ' '.join(monitor.problems('rotation', feed, midnight))
        self.assertNotIn('not scoped to today', errors)
        self.assertIn('access review', errors)
        self.assertIn('freshness window', errors)
        self.assertIn('unreviewed schedule revision', errors)
        self.assertIn('not scoped to today', ' '.join(monitor.problems('rotation', feed, midnight + timedelta(minutes=5))))


class SourceTimingTests(unittest.TestCase):
    def test_service_backoff_from_response_time_survives_the_next_run(self):
        writes = []
        def unavailable(url):
            raise HTTPError(url, 503, 'temporary', {'Retry-After': '3600'}, io.BytesIO())
        self.assertEqual(run_once({}, allowed(), writes.append, now=NOW, loader=unavailable,
                                  clock=lambda: NOW+timedelta(seconds=3)), 'failed')
        from outage_backend.github_delivery import HEALTH
        self.assertEqual(writes[-1][HEALTH]['nextAttemptAt'], '2026-11-19T21:00:03Z')
        loader = Mock()
        self.assertEqual(run_once(writes[-1], allowed(), writes.append, now=NOW+timedelta(minutes=30), loader=loader), 'deferred')
        loader.assert_not_called()

    def test_publication_during_fetch_is_not_misclassified_as_future(self):
        calls, writes = [], []
        finished = NOW
        def clock(): return finished
        def loader(url):
            nonlocal finished
            calls.append(url)
            finished += timedelta(seconds=1)
            if url == URLS[0]: return {'mapOptions': {'refreshTime': 5}}
            if url == URLS[2]: return [dict(event(), outageModifiedTime=finished.isoformat())]
            return {'updateTime': finished.isoformat(), 'customersOutNow': 5}
        result = run_once({}, allowed(), writes.append, loader=loader, clock=clock)
        self.assertEqual(result, 'verified')
        self.assertEqual(calls, [URLS[0], URLS[2], URLS[1]])
        self.assertEqual(writes[-1][FEED]['source']['checkedAt'], '2026-11-19T20:00:03Z')
        self.assertEqual(writes[-1][FEED]['source']['updatedAt'], '2026-11-19T20:00:03Z')

    def test_newer_details_future_publication_and_regressed_clock_still_fail(self):
        for summary_time, event_time, end in [
                (NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)),
                (NOW + timedelta(seconds=3), NOW, NOW + timedelta(seconds=2)),
                (NOW - timedelta(seconds=2), NOW - timedelta(seconds=2), NOW - timedelta(seconds=1))]:
            calls, writes = [], []
            def loader(url):
                calls.append(url)
                return {URLS[0]: {'mapOptions': {'refreshTime': 5}},
                        URLS[1]: {'updateTime': summary_time.isoformat(), 'customersOutNow': 5},
                        URLS[2]: [dict(event(), outageModifiedTime=event_time.isoformat())]}[url]
            with self.subTest(summary_time=summary_time, event_time=event_time, end=end):
                self.assertEqual(run_once({}, allowed(), writes.append, now=NOW, loader=loader, clock=lambda: end), 'failed')
                self.assertEqual(len(calls), 3)
                self.assertNotIn(FEED, writes[-1])


class CollectorOutcomeTests(unittest.TestCase):
    def test_rotation_problem_does_not_mask_live_outage_outcome(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from outage_backend import github_delivery as delivery
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'permission.json'
            path.write_text(json.dumps(allowed()))
            for outcome in ['verified', 'failed']:
                output = io.StringIO()
                with self.subTest(outcome=outcome), \
                        patch('sys.argv', ['delivery', '--permission', str(path)]), \
                        patch.dict('os.environ', {'ROTATION_CHECK_ENABLED': 'true'}), \
                        patch.object(delivery, 'GitHubDataBranch'), \
                        patch.object(delivery, 'load_scheduled_state', return_value={}), \
                        patch.object(delivery, 'run_once', return_value=outcome), \
                        patch.object(delivery, 'check_rotation', return_value='failed'), redirect_stdout(output):
                    if outcome == 'failed':
                        with self.assertRaises(SystemExit) as caught: delivery.main()
                        self.assertEqual(caught.exception.code, 1)
                    else:
                        delivery.main()
                self.assertIn('Schedule check: failed', output.getvalue())
                self.assertIn('Collection result: ' + outcome, output.getvalue())
                self.assertIn('::warning', output.getvalue())


class AccessReviewTests(unittest.TestCase):
    def setUp(self):
        writes = []
        check_rotation(None, writes.append, now=NOW - timedelta(minutes=6),
                       loader=lambda url, **_: (HTML, None) if url == ALERTS else (b'%PDF-original', None))
        self.previous = dict(writes[-1][ROTATION], status='blocked')
        from hashlib import sha256
        self.review = dict(expectedFingerprint=self.previous['schedule']['fingerprint'],
                           advisoryFingerprint=sha256(HTML).hexdigest(), validUntil=(NOW+timedelta(minutes=10)).isoformat())

    def test_blocked_ordinary_collection_makes_no_source_request(self):
        loader = Mock()
        self.assertEqual(check_rotation(self.previous, Mock(), now=NOW, loader=loader), 'review_required')
        loader.assert_not_called()

    def test_invalid_expired_or_wrong_revision_review_cannot_publish_or_fetch(self):
        for changes in [dict(validUntil=NOW.isoformat()), dict(validUntil=(NOW+timedelta(hours=1)).isoformat()),
                        dict(expectedFingerprint='0'*64), dict(advisoryFingerprint='bad')]:
            publish, loader = Mock(), Mock()
            with self.subTest(changes=changes), self.assertRaises(PermissionRequired):
                check_rotation(self.previous, publish, now=NOW, loader=loader, access_review=dict(self.review, **changes))
            publish.assert_not_called(); loader.assert_not_called()

    def test_review_does_not_shorten_backoff(self):
        previous = dict(self.previous, nextAttemptAt=(NOW+timedelta(minutes=5)).isoformat())
        publish, loader = Mock(), Mock()
        self.assertEqual(check_rotation(previous, publish, now=NOW, loader=loader, access_review=self.review), 'deferred')
        publish.assert_not_called(); loader.assert_not_called()

    def test_denial_redirect_mismatched_page_and_network_failure_leave_block_in_place(self):
        for failure in [HTTPError(ALERTS, 403, 'denied', {}, io.BytesIO()),
                        PermissionRequired('redirect'), URLError('temporary'), (b'<changed>', None)]:
            writes = []
            loader = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
            self.assertEqual(check_rotation(self.previous, writes.append, now=NOW, loader=loader, access_review=self.review), 'blocked')
            self.assertEqual(writes[-1][ROTATION]['schedule'], self.previous['schedule'])
            self.assertEqual(loader.call_count, 1)
            self.assertEqual(writes[0][ROTATION]['status'], 'blocked')

    def test_reviewed_changed_format_recovers_access_without_inventing_coverage(self):
        from hashlib import sha256
        writes = []
        loader = Mock(return_value=(PNG_HTML, None))
        review = dict(self.review, advisoryFingerprint=sha256(PNG_HTML).hexdigest())
        self.assertEqual(check_rotation(self.previous, writes.append, now=NOW, loader=loader, access_review=review), 'failed')
        self.assertEqual(writes[-1][ROTATION]['schedule'], self.previous['schedule'])
        self.assertEqual(loader.call_count, 1, 'Do not fetch another district image as a schedule')
        # Next ordinary check resumes, preserving the five minute throttle.
        self.assertEqual(check_rotation(writes[-1][ROTATION], writes.append, now=NOW+timedelta(minutes=5), loader=loader), 'failed')
        self.assertEqual(loader.call_count, 2)

    def test_reviewed_success_uses_completed_check_time(self):
        writes = []
        loader = lambda url, **_: (HTML, None) if url == ALERTS else (b'%PDF-original', None)
        self.assertEqual(check_rotation(self.previous, writes.append, now=NOW, clock=lambda: NOW+timedelta(seconds=2),
                                       loader=loader, access_review=self.review), 'ok')
        self.assertEqual(writes[-1][ROTATION]['schedule']['checkedAt'], '2026-11-19T20:00:02Z')
        self.assertEqual(writes[-1][ROTATION]['schedule']['dayStatus'], 'unreviewed')

    def test_image_or_ambiguous_link_is_never_assumed_to_be_stt_stj(self):
        for value in [PNG_HTML, HTML + PNG_HTML]:
            with self.assertRaisesRegex(ValueError, 'schedule_format_changed'):
                schedule_url(value)
