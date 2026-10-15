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
