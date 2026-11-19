"""Check or refresh site/events.json: public events outside this repository.

Usage:
  python scripts/site_events.py            # validate the file, print the events
  python scripts/site_events.py --refresh  # fill title, url, created_at, state from GitHub

The refresh makes read-only GET requests to the public GitHub REST API (no
token). Timestamps on the page come from the server (`created_at`), not from
anyone's clock. Kinds:
  pull_request   repo, number
  issue_comment  repo, number (the issue), comment_id
The refresh also records who wrote each item (`user`, and for a comment the
issue's author, `issue_user`), so the page can label a reply by the issue
author without anyone typing it.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "site" / "events.json"
API = "https://api.github.com"
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/142.0.0.0 Safari/537.36")


def get(url: str) -> dict:
    """GET JSON; on a refusal, one retry with a browser User-Agent."""
    headers = {"Accept": "application/vnd.github+json"}
    for ua in (None, BROWSER_UA):
        h = dict(headers, **({"User-Agent": ua} if ua else {}))
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429) or ua:
                raise
    raise RuntimeError("unreachable")


def refresh(ev: dict) -> dict:
    repo, n = ev["repo"], ev["number"]
    if ev["kind"] == "pull_request":
        p = get(f"{API}/repos/{repo}/pulls/{n}")
        ev.update(title=p["title"], url=p["html_url"], created_at=p["created_at"],
                  state=p["state"], merged_at=p.get("merged_at"), user=p["user"]["login"])
    elif ev["kind"] == "issue_comment":
        c = get(f"{API}/repos/{repo}/issues/comments/{ev['comment_id']}")
        i = get(f"{API}/repos/{repo}/issues/{n}")
        ev.update(title=i["title"], url=c["html_url"], created_at=c["created_at"],
                  state=i["state"], user=c["user"]["login"], issue_user=i["user"]["login"])
    else:
        raise SystemExit(f"unknown kind {ev['kind']!r} in {ev.get('id')}")
    ev["checked_at"] = datetime.now(timezone.utc).date().isoformat()
    return ev


def validate(data: dict) -> None:
    ids = set()
    for ev in data["events"]:
        for k in ("id", "kind", "repo", "number", "url", "created_at"):
            if not ev.get(k):
                raise SystemExit(f"event {ev.get('id')!r}: missing {k} (run --refresh)")
        if ev["id"] in ids:
            raise SystemExit(f"duplicate event id {ev['id']}")
        ids.add(ev["id"])
        datetime.fromisoformat(ev["created_at"].replace("Z", "+00:00"))
        if not ev["url"].startswith("https://github.com/"):
            raise SystemExit(f"event {ev['id']}: not a public GitHub URL")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    data = json.loads(PATH.read_text())
    if a.refresh:
        data["events"] = [refresh(e) for e in data["events"]]
        PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    validate(data)
    for e in data["events"]:
        print(f"{e['id']}: {e['kind']} {e['repo']}#{e['number']} {e['created_at']} {e.get('state')}")


if __name__ == "__main__":
    main()
