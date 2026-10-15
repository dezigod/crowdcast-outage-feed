"""Separate private outage storage; never touches CrowdCast preferences."""
import json
import os
from pathlib import Path
import tempfile


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)

    def read(self, name, default=None):
        try:
            return json.loads((self.directory / name).read_text())
        except (FileNotFoundError, ValueError):
            return default

    def write(self, name, payload):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def delivered(self):
        feed = self.read("feed.json")
        if feed is None:
            return None
        health = self.read("health.json", {})
        return dict(feed, collectionStatus=health.get("status", "ok"), lastCollectionError=health.get("error"))
