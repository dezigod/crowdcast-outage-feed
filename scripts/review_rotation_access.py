#!/usr/bin/env python3
"""Explicit owner-reviewed recovery; normal collection never clears a refusal."""
import argparse
import os

from outage_backend.github_delivery import GitHubDataBranch
from outage_backend.rotation import ROTATION, check_rotation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-fingerprint", required=True)
    parser.add_argument("--advisory-fingerprint", required=True)
    parser.add_argument("--valid-until", required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY") != "dezigod/crowdcast-outage-feed":
        parser.error("Unexpected repository")
    review = {"expectedFingerprint": args.expected_fingerprint,
              "advisoryFingerprint": args.advisory_fingerprint, "validUntil": args.valid_until}
    remote = GitHubDataBranch(os.environ["GITHUB_REPOSITORY"], os.environ.get("GH_TOKEN", ""))
    previous = remote.load(include_rotation=True)
    result = check_rotation(previous.get(ROTATION), remote.publish, access_review=review)
    print("Rotation access review result: " + result)
    if result != "ok":
        parser.exit(1, "Schedule coverage has not recovered. Inspect the review diagnostic and monitor issue.\n")


if __name__ == "__main__":
    main()
