"""
routegraph.py — Frontier's nonstop route network.

The booking page embeds a `stations` array where each airport lists the
destination codes it serves nonstop (`markets`). We extract that once and cache
it to routes.json so the "Anywhere" search can fan out only over real nonstop
destinations from a given origin.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta

from frontier import fetch_raw

CACHE = os.path.join(os.path.dirname(__file__), "routes.json")


def _extract_stations(page_html: str) -> dict:
    t = html.unescape(page_html)
    i = t.find('"stations":[{"code"')
    if i == -1:
        raise RuntimeError("stations array not found in page")
    arr_start = t.find("[", i)
    depth = 0
    for k in range(arr_start, len(t)):
        c = t[k]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                stations = json.loads(t[arr_start:k + 1])
                break
    graph = {}
    names = {}
    for s in stations:
        code = s.get("code")
        if not code:
            continue
        graph[code] = sorted(set(s.get("markets") or []))
        city = s.get("value")  # e.g. "Atlanta"
        state = s.get("provinceStateCode")  # e.g. "GA" (None for intl)
        if city:
            names[code] = f"{city}, {state}" if state else city
    return {"graph": graph, "names": names,
            "generated": page_html and "ok"}


def build_cache(force: bool = False) -> dict:
    if os.path.exists(CACHE) and not force:
        return load()
    # Any real route page contains the full station list; use a common pair
    # a few days out so the page renders fully.
    date = datetime.now() + timedelta(days=3)
    raw = fetch_raw("DFW", "DEN", date)
    data = _extract_stations(raw)
    data["generated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(CACHE, "w") as f:
        json.dump(data, f, indent=0)
    return data


def load() -> dict:
    if not os.path.exists(CACHE):
        return build_cache()
    with open(CACHE) as f:
        return json.load(f)


def destinations_from(origin: str) -> list[str]:
    g = load()["graph"]
    return g.get(origin.upper(), [])


def all_airports() -> list[str]:
    return sorted(load()["graph"].keys())


def airport_name(code: str) -> str:
    return load().get("names", {}).get(code.upper(), code.upper())


if __name__ == "__main__":
    data = build_cache(force=True)
    g = data["graph"]
    print(f"airports: {len(g)}")
    print(f"DFW nonstop destinations ({len(g.get('DFW', []))}): {g.get('DFW')}")
    print(f"SAN nonstop destinations ({len(g.get('SAN', []))}): {g.get('SAN')}")
