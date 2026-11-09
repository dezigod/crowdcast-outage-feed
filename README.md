# CrowdCast outage feed

Reviewed power-outage listings for St. Thomas, St. Croix, St. John and Water Island,
derived from the official WAPA outage viewer. This repository contains the
deterministic updater and its tests. It contains no CrowdCast iOS code or user data.

## App feed

The stable HTTPS address, after the first successful collection, is:

`https://raw.githubusercontent.com/dezigod/crowdcast-outage-feed/refs/heads/data/outages.json`

The `data` branch holds only the reviewed feed, generic collection status and
its README. Before any verified snapshot exists, the feed is absent; absence
means **updates unavailable**, not zero outages. Source publication, source check
and app download times must remain distinct in clients. Creating this repository
does not reconfigure or reinstall an already-installed iPhone build.

## Update timing and limits

- The workflow requests a check every five minutes, offset from the top of the hour.
  WAPA's viewer was configured to check every five minutes when inspected on
  October 15, 2026. Neither interval guarantees when WAPA records a change.
- GitHub can delay or drop scheduled runs. This is periodic delivery, not an
  instant or guaranteed-availability outage service. Public scheduled workflows
  may be disabled after 60 days without repository activity.
- GitHub's raw-file delivery also caches responses (observed `max-age=300`).
  Source checks, delivery caching and the app's refresh interval can add delay;
  do not promise that a source change appears on the phone within five minutes.
- Source snapshots over 15 minutes old must be labeled as earlier updates.
  A new check or download never changes the source publication timestamp.
- Standard GitHub-hosted Ubuntu runners are used, without caches or artifacts.
  No paid runners, server subscription or billing-setting change is required.

See GitHub's [schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
and [Actions billing documentation](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

## Data quality

The adapter reads only the three reviewed JSON routes used by
[WAPA's official viewer](http://outageviewer.viwapa.vi:7575/). These routes are
undocumented and can change; WAPA has not promised a supported API.
The upstream viewer uses HTTP. HTTPS delivery here cannot authenticate upstream
HTTP records. CrowdCast is not WAPA's official outage service.

Reviewed estate mappings keep islands separate; unknown estates stay unassigned.
Lots are grouped under neighborhoods without equating lot counts to homes,
meters or affected customers. WAPA's customer field retains its source meaning.
Conflicting summary/event totals withhold customer totals and mark coverage
partial. Missing, failed, old or partial empty listings cannot establish all-clear.
Verified/Predicted describes WAPA's record status, not crew assignment.

Source access refusals and unapproved redirects stop collection pending manual
review. Rate limits, including Retry-After, survive runner restarts. Invalid,
older or conflicting snapshots cannot replace the last verified listings.
Failures publish a generic warning alongside the prior feed when one exists.
No automatic geography, status or restoration-time guesses are used.

## Privacy and permission

Collection and app-display permission are recorded separately in the repository
secret `WAPA_COLLECTION_PERMISSION`. Public availability does not establish a
reuse license. The permission record is never committed, cached, logged or
uploaded as an artifact. Access limits in that record are enforced before requests.

Raw source files exist only in private temporary runner storage and are removed
on completion. They are **not retained as a cloud audit archive** by this design.
The published feed retains a source fingerprint and reviewed derived fields.
Private local audit snapshots remain outside this repository. Keeping a full
private source history would require separately reviewed storage.

## Operation

`COLLECTION_ENABLED=true` is required in repository variables. The updater is
restricted to this repository's `main` branch, schedule or manual dispatch.
Disable that variable to stop future collection. The API and iPhone never start
their own WAPA collectors.

Before source access, the updater atomically commits a throttle reservation to
the `data` branch. Each final result updates only allowlisted JSON files, using
a non-forcing branch update. Runs are serialized; concurrent changes fail safely.
There is no force-push or automatic overwrite of an unexpected branch change.

If collection becomes blocked, inspect WAPA's access response privately, review
permission and access limits, then explicitly review/reset the generic collection
state before resuming. Do not delete the state just to bypass a refusal or rate limit.
Review workflow failures and storage/history growth as part of operation.

## Tests

Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r outage_backend/requirements-lock.txt
.venv/bin/python -m unittest discover -s outage_backend/tests -v
```

All normal tests are offline and synthetic. CI has no source permission secret
and never calls WAPA. Only the explicitly enabled collection workflow fetches
the reviewed source. The production collector itself uses Python's standard
library, without an LLM, browser renderer, Firebase or third-party paid API.

## Feed reliability and alerts (November 2, 2026)

Native GitHub schedules are enabled. Outage collection also retains its existing
Cloudflare five-minute dispatch timer; the durable collector throttle prevents
duplicate source polls. Its dedicated dispatch token is documented to expire
January 13, 2027 and must be renewed; native GitHub scheduling is a fallback.

`Monitor delivered feed` runs four times an hour and after collector jobs. It
checks the actual public feed: cruise document checks must be within 12 hours;
outage collection and WAPA publication must be within 30 minutes. Missing,
invalid or future timestamps and failed collection status are incidents. It
opens one `Feed freshness alert` issue mentioning @dezigod and closes it after
verified recovery. Enable GitHub email/push notifications for mentions; opening
an issue does not prove delivery to an inbox or device. A separate Codex watcher
checks both feeds to catch missing GitHub jobs while the desktop scheduler is
available. Neither mechanism is an availability guarantee.

Transient source failures retry on later authorized collection attempts, honoring
Retry-After and backoff. Access refusals stay stopped for manual resolution.
Previous valid data and original source timestamps are preserved. Do not reset
state or relabel old data as fresh to clear an alert.

The existing Cloudflare timer also dispatches `Monitor delivered feed` every
five minutes. This backup monitor checks BOTH public feeds using the same
outage-repository-scoped Actions key. A cruise incident found by this independent
path is titled `Cruise feed freshness alert` in this repository; it cannot
change or collect cruise source data. Native cruise monitoring remains enabled.

Each outage check first saves its next permitted attempt time. The public feed keeps its previous result during the download. When the check finishes, the job publishes the outcome and the reviewed feed together. A temporary GitHub server or network error gets one bounded retry of the same GitHub operation. A refused source request retains the existing access restrictions. Both-feed monitor logs include the observed source timestamps so delivery gaps can be checked later.

## St. Thomas rotational schedule

With repository variable `ROTATION_CHECK_ENABLED=true`, the same five-minute
workflow checks WAPA's public Alerts page and its linked STT/STJ schedule.
This opt-in covers public schedule monitoring and link delivery. The outage
map retains its separate collection and display permission record.

`data/rotation-schedule.json` publishes the official document link, fingerprint,
last successful check and first observation of the revision. The PDF stays on
WAPA's website and opens inside CrowdCast. The feed explicitly sets `subjectToChange: true` for the tentative district
schedule. A changed PDF at the same URL counts as a new revision. A failed check preserves the last link and its original
timestamps. The collector persists backoff and stops on access refusals or
redirects. No raw PDFs, paid runners or storage services are used.

## St. Thomas parcel review

The weekly parcel review runs Mondays at 10:23 a.m. Atlantic time on a
standard runner. It rechecks the original unresolved labels and new labels
in Agnes Fancy, Lovenlund, and Subbase against official parcel records
within each Census estate boundary. The workflow summary lists candidates
for review. Duplicate matches require additional evidence. Only reviewed
matches bundled in CrowdCast are eligible for pins. The review does not
write to the outage or schedule feeds.

### Reviewed planned interruption times

`rotation-schedule.json` optionally includes `schedule.windows` with zoned
`startsAt` / `endsAt`, feeder and area labels, and `coverageStart` / `coverageEnd`.
These are **tentative planned interruptions**, never actual outage/restoration
confirmations. `extractionStatus` is `reviewed` or `unavailable`.

The 2026-11-07 review visually checked all six pages of WAPA's official PDF
(SHA256 `6df95be896a902f1a0b4cdae910da0bb4a924e4af942561c0b1cb85cdd789019`).
Its 18 windows are dated **November 4–6**, not November 7. The PDF says activation
is only if needed, some feeders may not be affected, restoration may be delayed
up to 15 minutes, and schedules may change with demand. Those dates must not be
rolled forward or presented as current after their coverage ends. The HTTP
Last-Modified date and revision-first-seen timestamp are not effective dates.

The existing five-minute monitor refreshes the PDF fingerprint. Exact reviewed
bytes use `reviewed-rotation-windows.json`; any new/unreviewed fingerprint clears
structured rows and returns `unavailable`, while continuing to link the current
official PDF. This intentionally requires review of new layouts rather than
risk assigning one column's feeder to another column's times. Checked timestamps
remain retrieval evidence, not evidence that a past schedule is still active.
WAPA viewer estimates already come from `estimatedTimeOfRestoral`; unknown,
timezone-free, pre-start or conflicting grouped estimates remain null.

### Rotation review monitoring

The existing delivered-feed monitor separately checks `rotation-schedule.json` after collection and on its 15-minute schedule. A deduplicated **WAPA rotation schedule needs review** issue flags a new unreviewed PDF fingerprint, expired reviewed schedule dates, failed checks, or checks older than 30 minutes. This does not speed up WAPA publication or invent restoration estimates. Fresh downloads never make expired schedule dates current. New PDF layouts still require visual review before their times are published. The five-minute source collector and independent dispatcher are unchanged.

### Rotation notification deduplication

Rotation checks still open a new issue and fail the Actions run for a new incident or newly added problem. An unchanged already-open rotation incident now emits an explicit warning and an **UNHEALTHY** job summary instead of failing every monitor run again. The issue stays open until verified recovery, when the existing recovery comment/close path runs. A green monitor run means the check/incident tracking succeeded, not that expired rotation coverage recovered. This opt-in applies only to rotation checks: outage and cruise failure exits, GitHub/API errors, collection schedules and user notification settings are unchanged. No new notification subscription was added.
