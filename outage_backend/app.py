"""Read-only FastAPI endpoint. Starting it does not start a collector."""
import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from .store import Store


def create_app(store=None):
    store = store or Store(Path(os.environ.get("CROWDCAST_OUTAGE_STATE_DIR", Path(__file__).parent / ".local-state")))
    api = FastAPI(title="CrowdCast outage adapter", docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/v1/outages")
    def outages():
        data = store.delivered()
        return JSONResponse(data if data is not None else {"message": "Updates unavailable"},
                            status_code=200 if data is not None else 503, headers={"Cache-Control": "no-store"})

    @api.get("/health")
    def health():
        # A separate operator-started collector may exist; this API cannot know
        # whether that external process is running. Report only our own behavior.
        return {"snapshotAvailable": store.read("feed.json") is not None, "apiStartsCollector": False}

    return api


app = create_app()
