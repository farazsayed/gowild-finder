"""
app.py — FastAPI backend for the GO WILD one-way flight finder.

Endpoints
  GET  /api/airports                 -> [{code, name}]
  GET  /api/destinations?origin=DFW  -> [codes] nonstop from origin
  POST /api/search                   -> one-way search (destination may be "ANY")
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from curl_cffi import requests as cf

import frontier
import gopass
import pathfinder
import routegraph

app = FastAPI(title="GO WILD Flight Finder")
# Local tool: allow any origin so the UI works when opened as a file:// page too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# One warmed curl_cffi session per worker thread (sessions aren't thread-safe).
_local = threading.local()


def _session() -> cf.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = cf.Session(impersonate="chrome")
        s.get(frontier.WARM_URL, timeout=15)
        _local.session = s
    return s


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
class Filters(BaseModel):
    gowild_only: bool = True
    nonstop_only: bool = False
    no_overnight: bool = False
    max_layover_min: Optional[int] = None     # exclude itineraries above this
    dep_after: Optional[int] = None           # minutes past midnight
    dep_before: Optional[int] = None
    arr_after: Optional[int] = None
    arr_before: Optional[int] = None
    max_duration_min: Optional[int] = None


class SearchRequest(BaseModel):
    origin: str
    destination: str = "ANY"     # IATA code or "ANY"
    date: str                    # YYYY-MM-DD
    filters: Filters = Filters()
    sort: str = "dep"            # dep | duration | seats | layover
    thorough: bool = False       # ANY: force the full live-scrape fan-out


def passes(it: frontier.Itinerary, f: Filters) -> bool:
    if f.gowild_only and not it.go_wild:
        return False
    if f.nonstop_only and it.stops > 0:
        return False
    if f.no_overnight and it.overnight:
        return False
    if f.max_layover_min is not None and it.max_layover_min > f.max_layover_min:
        return False
    if f.dep_after is not None and it.dep_minutes < f.dep_after:
        return False
    if f.dep_before is not None and it.dep_minutes > f.dep_before:
        return False
    if f.arr_after is not None and it.arr_minutes < f.arr_after:
        return False
    if f.arr_before is not None and it.arr_minutes > f.arr_before:
        return False
    if f.max_duration_min is not None and it.duration_min > f.max_duration_min:
        return False
    return True


def _sort_key(sort: str):
    return {
        "dep": lambda it: it.dep,
        "duration": lambda it: it.duration_min,
        "seats": lambda it: -(it.go_wild_seats or 0),
        "layover": lambda it: it.max_layover_min,
    }.get(sort, lambda it: it.dep)


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def _has_service(origin: str, dest: str, date: datetime):
    return frontier.has_service(origin, dest, date, session=_session())


def _search_one(origin: str, dest: str, date: datetime) -> list[frontier.Itinerary]:
    """Fetch one route, retrying once on transient transport errors.

    Returns [] for "no flights that day"; raises only if both attempts fail
    with a transport error (so the caller can count it).
    """
    last = None
    for attempt in range(2):
        try:
            raw = frontier.fetch_raw(origin, dest, date, session=_session())
            return frontier.parse(raw, origin, dest, date)
        except frontier.FrontierError:
            return []  # valid page, just no service / blocked-and-not-worth-retry
        except Exception as e:  # transport hiccup (timeout, reset) — retry fresh
            last = e
            _local.session = None  # force a new warmed session next call
    raise last


@app.get("/api/airports")
def airports():
    data = routegraph.load()
    names = data.get("names", {})
    return [{"code": c, "name": names.get(c, c)} for c in sorted(data["graph"])]


@app.get("/api/destinations")
def destinations(origin: str):
    return routegraph.destinations_from(origin)


def _fanout(origin: str, dests: list[str], date: datetime):
    """Full live-scrape fan-out across destinations (authoritative, slower)."""
    results: list[frontier.Itinerary] = []
    errors = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_search_one, origin, d, date): d for d in dests}
        for fut in as_completed(futs):
            try:
                results.extend(fut.result())
            except Exception:
                errors += 1
    return results, errors


@app.post("/api/search")
def search(req: SearchRequest):
    origin = req.origin.upper().strip()
    date = datetime.fromisoformat(req.date)
    is_any = req.destination.upper().strip() == "ANY"

    results: list[frontier.Itinerary] = []
    errors = 0
    source = ""
    dests_searched = 1

    if is_any:
        dests = routegraph.destinations_from(origin)
        dests_searched = len(dests)
        if not req.thorough:
            # Fast path: one gopassflights call covers every destination.
            try:
                results = gopass.search_oneway(origin, "ANY", date)
                source = "gopassflights (fast)"
            except gopass.GoPassError:
                results = []
        if not results:  # thorough mode, or gopass failed/empty -> live fan-out
            results, errors = _fanout(origin, dests, date)
            source = "live scrape (fan-out)"
    else:
        dest = req.destination.upper().strip()
        # Specific route: live scrape is complete + fresh; gopass is the fallback.
        try:
            results = _search_one(origin, dest, date)
            source = "live scrape"
        except Exception:
            try:
                results = gopass.search_oneway(origin, dest, date)
                source = "gopassflights (fallback)"
            except gopass.GoPassError:
                results, errors = [], 1
                source = "unavailable"

    filtered = [it for it in results if passes(it, req.filters)]
    filtered.sort(key=_sort_key(req.sort))

    return {
        "origin": origin,
        "destination": req.destination.upper(),
        "date": req.date,
        "source": source,
        "destinations_searched": dests_searched,
        "total_found": len(results),
        "gowild_found": sum(1 for it in results if it.go_wild),
        "shown": len(filtered),
        "errors": errors,
        "results": [it.to_dict() for it in filtered],
    }


# --------------------------------------------------------------------------- #
# Route builder — discover ALL GO WILD paths O -> D
# --------------------------------------------------------------------------- #
class PathRequest(BaseModel):
    origin: str
    dest: str
    date: str
    max_stops: int = 1            # 0 = nonstop only, 1 = one hub, 2 = two hubs
    min_conn: int = 90            # minutes; min layover for a self-transfer
    max_conn: int = 1440          # minutes; 1440 = allow one overnight at a hub
    no_overnight: bool = False
    sort: str = "fastest"         # fastest | cheapest | fewest_stops | most_seats


_PATH_SORTS = {
    "fastest": lambda p: (p["total_duration_min"], p["total_fees"]),
    "cheapest": lambda p: (p["total_fees"], p["total_duration_min"]),
    "fewest_stops": lambda p: (p["flights"], p["total_duration_min"]),
    "most_seats": lambda p: (-(p["min_seats"] or 0), p["total_duration_min"]),
}


@app.post("/api/paths")
def paths(req: PathRequest):
    date = datetime.fromisoformat(req.date)
    res = pathfinder.discover(
        req.origin, req.dest, date,
        max_stops=max(0, min(2, req.max_stops)),
        min_conn=req.min_conn,
        max_conn=req.max_conn,
        no_overnight=req.no_overnight,
        search_one=_search_one,
        has_service=_has_service,
    )
    res["paths"].sort(key=_PATH_SORTS.get(req.sort, _PATH_SORTS["fastest"]))
    return res


# --------------------------------------------------------------------------- #
# Static frontend
# --------------------------------------------------------------------------- #
import os
_static = os.path.join(os.path.dirname(__file__), "static")


@app.get("/")
def index():
    return FileResponse(os.path.join(_static, "index.html"))


app.mount("/static", StaticFiles(directory=_static), name="static")
