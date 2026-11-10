from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError

from outage_backend.rotation import (ALERTS, ROTATION, check_rotation, current_day,
    public_rotation, reviewed_social_schedule, reviewed_windows)

OLD_HASH = '10172c3012aa7d0c8f7951b811eca027380e8dc076905c8410401e505f86aa9e'
URL = 'https://www.viwapa.vi/docs/default-source/outage-schedule/sttstj-rotational-outage-schedule-doc.pdf?sfvrsn=2c938249_404'
HTML = f'<a href="{URL}">Schedule</a>'.encode()
NOW = datetime(2026, 11, 10, 17, tzinfo=timezone.utc)


def old_feed():
    return dict(schemaVersion=1, status='ok', subjectToChange=True,
        nextAttemptAt='2026-11-10T16:00:00Z', schedule=dict(
        url=URL, fingerprint=OLD_HASH, checkedAt='2026-11-09T20:00:00Z',
        revisionSeenAt='2026-11-09T16:00:00Z', lastModified='Mon, 09 Nov 2026 15:27:19 GMT',
        **reviewed_windows(OLD_HASH)))


class CurrentDayTests(TestCase):
    def test_ast_midnight_not_utc_midnight(self):
        schedule = old_feed()['schedule']
        before = current_day(schedule, datetime(2026, 11, 10, 3, 59, 59, tzinfo=timezone.utc))
        after = current_day(schedule, datetime(2026, 11, 10, 4, tzinfo=timezone.utc))
        self.assertEqual(before['day'], '2026-11-09')
        self.assertEqual(len(before['windows']), 7)
        self.assertEqual(after['day'], '2026-11-10')
        self.assertEqual(after['windows'], [])
        self.assertEqual(after['dayStatus'], 'not_current')
        self.assertIsNone(after['coverageEnd'])
        self.assertEqual(after['checkedAt'], schedule['checkedAt'])

    def test_all_todays_slots_retained_not_only_upcoming(self):
        social = reviewed_social_schedule(NOW)
        self.assertEqual(len(social['windows']), 7)
        self.assertEqual(social['windows'][0]['startsAt'], '2026-11-10T08:30:00-04:00')
        self.assertEqual(social['windows'][-1]['endsAt'], '2026-11-10T22:30:00-04:00')
        self.assertEqual(social['windows'][0]['areas'][-1], 'MALL')
        self.assertEqual(social['windows'][-1]['feeders'], ['Feeder 8B', 'Feeder 9E'])

    def test_social_review_never_rolls_forward(self):
        self.assertIsNone(reviewed_social_schedule(NOW + timedelta(days=1)))
        past = current_day(reviewed_social_schedule(NOW), NOW + timedelta(days=1))
        self.assertEqual(past['windows'], [])
        self.assertEqual(past['dayStatus'], 'not_current')

    def test_deferred_and_blocked_checks_still_remove_past_dates(self):
        for blocked in (False, True):
            previous = old_feed()
            previous['status'] = 'blocked' if blocked else 'ok'
            previous['nextAttemptAt'] = (NOW + timedelta(minutes=5)).isoformat()
            output = []
            def no_access(*a, **k): self.fail('Deferred check must not access source')
            outcome = check_rotation(previous, output.append, now=NOW, loader=no_access)
            self.assertEqual(outcome, 'deferred')
            self.assertEqual(output[-1][ROTATION]['schedule']['windows'], [])
            self.assertEqual(output[-1][ROTATION]['status'], previous['status'])
            public_rotation(output[-1][ROTATION])

    def test_failed_fetch_never_restores_yesterdays_rows(self):
        output = []
        def unavailable(*a, **k): raise OSError('network')
        check_rotation(old_feed(), output.append, now=NOW, loader=unavailable)
        self.assertEqual(output[-1][ROTATION]['status'], 'failed')
        self.assertEqual(output[-1][ROTATION]['schedule']['windows'], [])
        self.assertTrue(all(not item[ROTATION]['schedule']['windows'] for item in output))

    def test_stale_pdf_falls_back_to_exact_todays_review(self):
        output = []
        def loader(url, **kwargs):
            if url == ALERTS: return HTML, None
            self.assertIsNone(kwargs['previous_modified'])
            return b'%PDF-old', 'Mon, 09 Nov 2026 15:27:19 GMT'
        with patch('outage_backend.rotation.extract_windows', return_value=dict(
                reviewed_windows(OLD_HASH), extractionMethod='manual-review')):
            check_rotation(old_feed(), output.append, now=NOW, loader=loader)
        result = output[-1][ROTATION]
        public_rotation(result)
        self.assertEqual(result['schedule']['dayStatus'], 'current')
        self.assertEqual(len(result['schedule']['windows']), 7)
        self.assertEqual(result['schedule']['checkedAt'], '2026-11-10T16:30:23Z')
        self.assertEqual(result['schedule']['websiteCheckedAt'], '2026-11-10T17:00:00Z')
        self.assertEqual(len(result['schedule']['sourceURLs']), 2)

    def test_current_validated_pdf_can_use_conditional_check(self):
        previous = old_feed()
        previous['schedule'] = current_day(previous['schedule'], datetime(2026, 11, 9, 20, tzinfo=timezone.utc))
        previous['nextAttemptAt'] = '2026-11-09T20:05:00Z'
        output = []
        def loader(url, **kwargs):
            if url == ALERTS: return HTML, None
            self.assertEqual(kwargs['previous_modified'], previous['schedule']['lastModified'])
            return None, previous['schedule']['lastModified']
        check_rotation(previous, output.append, now=datetime(2026,11,9,20,10,tzinfo=timezone.utc), loader=loader)
        self.assertEqual(output[-1][ROTATION]['schedule']['windows'], previous['schedule']['windows'])

    def test_validation_rejects_past_date_in_current_day(self):
        value = old_feed()
        value['schedule'] = current_day(value['schedule'], NOW)
        value['schedule'].update(dayStatus='current', extractionStatus='reviewed', **{
            key: reviewed_windows(OLD_HASH)[key] for key in ('windows','coverageStart','coverageEnd')})
        with self.assertRaises(ValueError): public_rotation(value)

    def test_validation_rejects_unreviewed_social_account(self):
        value = old_feed(); value['schedule'] = reviewed_social_schedule(NOW)
        value['schedule']['url'] = value['schedule']['url'].replace('100069365522656', '123456789')
        with self.assertRaises(ValueError): public_rotation(value)

    def test_current_rows_require_validated_method(self):
        value = old_feed(); value['schedule'] = reviewed_social_schedule(NOW)
        value['schedule']['extractionMethod'] = 'unavailable'
        with self.assertRaises(ValueError): public_rotation(value)

    def test_social_schedule_requires_review_provenance(self):
        value = old_feed(); value['schedule'] = reviewed_social_schedule(NOW)
        del value['schedule']['sourceURLs']
        with self.assertRaises(ValueError): public_rotation(value)
