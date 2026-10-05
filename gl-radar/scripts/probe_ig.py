"""Try several Instagram actors against two known-good handles and record
exactly what each returns.

Written because three runs billed for results and stored nothing, and the
job log is not readable from the session doing the debugging. This writes
its findings into the repository instead, where they can be read directly.

Run: python scripts/probe_ig.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent.parent / "data" / "probe.json"
TOKEN = os.environ.get("APIFY_TOKEN", "")
HANDLES = ["breakaway", "beeupsnacks"]

# Each entry is (actor id, a function building the input for one handle).
CANDIDATES = [
    ("apidojo~instagram-scraper",
     lambda h: {"startUrls": [f"https://www.instagram.com/{h}/"], "maxItems": 3}),
    ("apify~instagram-scraper",
     lambda h: {"directUrls": [f"https://www.instagram.com/{h}/"],
                "resultsType": "posts", "resultsLimit": 3,
                "addParentData": False}),
    ("apify~instagram-post-scraper",
     lambda h: {"username": [h], "resultsLimit": 3}),
    ("shu8hvra~instagram-post-scraper-pro",
     lambda h: {"usernames": [h], "maxPosts": 3}),
]


def probe(actor: str, payload: dict) -> dict:
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
    try:
        r = httpx.post(url, params={"token": TOKEN}, json=payload, timeout=180)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why": f"request failed: {exc}"}
    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "body": r.text[:300]}
    try:
        rows = r.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "why": "response was not JSON", "body": r.text[:300]}
    if not isinstance(rows, list):
        return {"ok": False, "why": "response was not a list", "body": str(rows)[:300]}
    real = [x for x in rows
            if isinstance(x, dict) and not x.get("noResults") and not x.get("error")]
    out = {"ok": bool(real), "rows": len(rows), "usable": len(real)}
    if real:
        first = real[0]
        out["fields"] = sorted(first.keys())[:30]
        # The four values that decide whether a post can go on the board.
        out["sample"] = {k: str(first.get(k))[:90] for k in first
                         if any(t in k.lower() for t in
                                ("caption", "text", "url", "date", "time",
                                 "created", "taken", "like", "comment", "id"))}
    elif rows:
        out["first_raw"] = str(rows[0])[:300]
    return out


def main() -> int:
    if not TOKEN:
        print("APIFY_TOKEN is not set"); return 1
    results: dict = {}
    for actor, build in CANDIDATES:
        results[actor] = {}
        for handle in HANDLES:
            res = probe(actor, build(handle))
            results[actor][handle] = res
            print(f"{actor:42} @{handle:14} {json.dumps(res)[:150]}")
            if res.get("ok"):
                break      # one good handle is enough to judge the actor
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=1))
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
