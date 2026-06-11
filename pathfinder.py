"""
pathfinder.py — discover ALL GO WILD routings from an origin to a destination,
including self-transfers stitched from separate one-way GO WILD tickets through
intermediate Frontier cities.

Strategy (cheap-to-expensive, so we never blind-fetch):
  1. Enumerate candidate city-paths O->...->D from the nonstop route graph only
     (routes.json) — a topological filter, no network calls.
  2. Pre-filter each unique segment with /Flight/RetrieveSchedule (tiny JSON) to
     drop routes that don't operate on the date — before any heavy scrape.
  3. Live-scrape the surviving segments (completeness is the point here) and keep
     the nonstop GO WILD flights as atomic "hops".
  4. Combine hops into concrete paths where the connection timing is feasible.

Each hop is a separate GO WILD booking (Frontier won't sell a multi-city GO WILD
ticket), so fees add up and the binding seat count is the minimum across hops.
Frontier's own through-itineraries (nonstop or single-ticket connections) come
free from the O->D scrape and are reported as "direct" / "frontier-connection".
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Callable, Optional

import routegraph
from frontier import Itinerary

MAX_SEGMENTS = 220       # hard cap on segment fetches per request
MAX_PATHS = 400          # cap assembled paths returned
MAX_DATE_WINDOW = 3      # never look more than this many days past a hub arrival


# --------------------------------------------------------------------------- #
# Reachability (route-graph topology, no network)
# --------------------------------------------------------------------------- #
def _can_reach(graph: dict, b: str, dest: str, flights_left: int) -> bool:
    """Can you get from b to dest in at most `flights_left` nonstop hops?"""
    if b == dest:
        return True
    if flights_left <= 0:
        return False
    if dest in graph.get(b, []):
        return True
    if flights_left >= 2:
        return any(dest in graph.get(c, []) for c in graph.get(b, []))
    return False


# --------------------------------------------------------------------------- #
# Path model
# --------------------------------------------------------------------------- #
@dataclass
class Path:
    kind: str                 # direct | frontier-connection | self-transfer
    origin: str
    dest: str
    date: str
    hubs: list[str]           # transfer cities (where you change tickets/planes)
    flights: int              # total flight segments flown
    transfers: int            # separate GO WILD tickets - 1
    total_duration_min: int   # first departure -> last arrival
    total_fees: float         # summed GO WILD fares across tickets
    min_seats: Optional[int]  # binding GO WILD seat count (min across hops)
    overnight: bool
    layovers: list[dict] = field(default_factory=list)   # {airport, minutes, overnight}
    hops: list[dict] = field(default_factory=list)       # each hop = Itinerary.to_dict()

    def to_dict(self) -> dict:
        return asdict(self)


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _layovers(hops: list[Itinerary]) -> list[dict]:
    """Layovers between consecutive hops (separate tickets)."""
    lays = []
    for prev, nxt in zip(hops, hops[1:]):
        gap = int((_dt(nxt.dep) - _dt(prev.arr)).total_seconds() // 60)
        lays.append({"airport": prev.dest, "minutes": gap,
                     "overnight": _dt(nxt.dep).date() != _dt(prev.arr).date()})
    return lays


def _leg_layovers(it: Itinerary) -> list[dict]:
    """Layovers between legs inside one itinerary (a single Frontier ticket)."""
    lays = []
    for a, b in zip(it.legs, it.legs[1:]):
        gap = int((_dt(b.dep) - _dt(a.arr)).total_seconds() // 60)
        lays.append({"airport": a.dest, "minutes": gap,
                     "overnight": _dt(b.dep).date() != _dt(a.arr).date()})
    return lays


def _build_path(hops: list[Itinerary], kind: str, origin: str, dest: str,
                date: str, layovers: list[dict]) -> Path:
    flights = sum(len(h.legs) for h in hops)
    fees = [h.go_wild_fare for h in hops if h.go_wild_fare is not None]
    seats = [h.go_wild_seats for h in hops if h.go_wild_seats is not None]
    overnight = (any(h.overnight for h in hops)
                 or _dt(hops[-1].arr).date() != _dt(hops[0].dep).date())
    return Path(
        kind=kind,
        origin=origin,
        dest=dest,
        date=date,
        hubs=[h.dest for h in hops[:-1]],
        flights=flights,
        transfers=len(hops) - 1,
        total_duration_min=int((_dt(hops[-1].arr) - _dt(hops[0].dep)).total_seconds() // 60),
        total_fees=round(sum(fees), 2) if fees else 0.0,
        min_seats=min(seats) if seats else None,
        overnight=overnight,
        layovers=layovers,
        hops=[h.to_dict() for h in hops],
    )


# --------------------------------------------------------------------------- #
# Discovery (demand-driven, multi-day BFS)
# --------------------------------------------------------------------------- #
class _SegStore:
    """Thread-safe cache of nonstop GO WILD flights for (origin, dest, date),
    with a cheap RetrieveSchedule pre-filter and a hard fetch budget."""

    def __init__(self, search_one, has_service, budget):
        self._search = search_one
        self._service = has_service
        self._budget = budget
        self._cache: dict[tuple, list[Itinerary]] = {}
        self._full: dict[tuple, list[Itinerary]] = {}
        self._lock = threading.Lock()
        self.fetched = 0
        self.pruned = 0
        self.errors = 0
        self.truncated = False

    def _do_fetch(self, o, d, dt):
        try:
            if self._service(o, d, dt) is False:
                with self._lock:
                    self.pruned += 1
                return None  # no service -> definitively empty
        except Exception:
            pass  # service check is best-effort; fall through to the real fetch
        try:
            its = self._search(o, d, dt)
            with self._lock:
                self.fetched += 1
            return its
        except Exception:
            with self._lock:
                self.errors += 1
            return []

    def nonstop_gw(self, o, d, dt) -> list[Itinerary]:
        key = (o, d, dt.strftime("%Y-%m-%d"))
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if self.fetched >= self._budget:
                self.truncated = True
                return []
        its = self._do_fetch(o, d, dt)
        res = [] if not its else [x for x in its if x.stops == 0 and x.go_wild]
        with self._lock:
            self._cache[key] = res
            if its is not None:
                self._full[key] = its
        return res

    def full_gw(self, o, d, dt) -> list[Itinerary]:
        """All GO WILD itineraries (incl. Frontier connections) for o->d/date."""
        self.nonstop_gw(o, d, dt)  # ensures fetch
        key = (o, d, dt.strftime("%Y-%m-%d"))
        with self._lock:
            return [x for x in self._full.get(key, []) if x.go_wild]


def _dates_in_window(arr: datetime, max_conn: int) -> list[datetime]:
    latest = arr + timedelta(minutes=max_conn)
    span = min((latest.date() - arr.date()).days, MAX_DATE_WINDOW)
    base = datetime(arr.year, arr.month, arr.day)
    return [base + timedelta(days=i) for i in range(span + 1)]


def discover(origin: str, dest: str, date: datetime, *,
             max_stops: int = 1,
             min_conn: int = 90,
             max_conn: int = 1440,           # 24h: allows one overnight at a hub
             no_overnight: bool = False,
             search_one: Callable[[str, str, datetime], list[Itinerary]],
             has_service: Callable[[str, str, datetime], Optional[bool]],
             max_workers: int = 6) -> dict:
    origin, dest = origin.upper(), dest.upper()
    graph = routegraph.load()["graph"]
    max_flights = max_stops + 1
    store = _SegStore(search_one, has_service, MAX_SEGMENTS)
    if no_overnight:
        max_conn = min(max_conn, 1439)

    paths: list[Path] = []
    seen: set = set()
    date_str = date.strftime("%Y-%m-%d")

    # 1. Single-ticket options (direct + Frontier connections) from the O->D scrape.
    for it in store.full_gw(origin, dest, date):
        if no_overnight and it.overnight:
            continue
        key = tuple((l.flight_number, l.dep) for l in it.legs)
        if key in seen:
            continue
        seen.add(key)
        kind = "direct" if it.stops == 0 else "frontier-connection"
        paths.append(_build_path([it], kind, origin, dest, date_str, _leg_layovers(it)))

    # 2. Self-transfer BFS over nonstop GO WILD hops, allowing multi-day connections.
    # A state is (airport, arrival_dt, hops_so_far, visited_cities).
    frontier: list[tuple] = [(origin, None, [], {origin})]

    def feasible_dates(arr) -> list[datetime]:
        return [date] if arr is None else _dates_in_window(arr, max_conn)

    def neighbours_of(state) -> list[str]:
        a, arr, hops, visited = state
        flights_left = max_flights - len(hops)
        out = []
        for b in graph.get(a, []):
            if b in visited:
                continue
            if b == dest and len(hops) == 0:        # self-transfer needs >=1 hub
                continue
            if _can_reach(graph, b, dest, flights_left - 1):
                out.append(b)
        return out

    for _level in range(max_flights):
        if not frontier:
            break
        # PLAN: gather every (origin, dest, date) segment this level needs.
        plans = [(st, neighbours_of(st), feasible_dates(st[1])) for st in frontier]
        keys = {(a, b, d) for (st, nbrs, dates) in plans
                for a in [st[0]] for b in nbrs for d in dates}
        # FETCH all of them concurrently (this is the real parallelism).
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(lambda k: store.nonstop_gw(*k), keys))
        # EXPAND in-memory from the now-warm cache (no network here).
        new_frontier = []
        for state, nbrs, dates in plans:
            a, arr, hops, visited = state
            for b in nbrs:
                for d in dates:
                    for fl in store.nonstop_gw(a, b, d):
                        dep, far = _dt(fl.dep), _dt(fl.arr)
                        if arr is not None:
                            gap = int((dep - arr).total_seconds() // 60)
                            if gap < min_conn or gap > max_conn:
                                continue
                        elif dep.date() != date.date():
                            continue
                        if no_overnight and (fl.overnight or far.date() != date.date()):
                            continue
                        new_hops = hops + [fl]
                        if b == dest:
                            key = tuple((h.legs[0].flight_number, h.legs[0].dep)
                                        for h in new_hops)
                            if key in seen:
                                continue
                            seen.add(key)
                            paths.append(_build_path(new_hops, "self-transfer",
                                                     origin, dest, date_str,
                                                     _layovers(new_hops)))
                        elif len(new_hops) < max_flights:
                            new_frontier.append((b, far, new_hops, visited | {b}))
        frontier = new_frontier

    paths.sort(key=lambda p: (p.total_duration_min, p.total_fees))
    paths = paths[:MAX_PATHS]

    kinds = {}
    for p in paths:
        kinds[p.kind] = kinds.get(p.kind, 0) + 1

    return {
        "origin": origin,
        "dest": dest,
        "date": date_str,
        "max_stops": max_stops,
        "min_conn": min_conn,
        "max_conn": max_conn,
        "segments_fetched": store.fetched,
        "segments_pruned_no_service": store.pruned,
        "segment_errors": store.errors,
        "truncated": store.truncated,
        "paths_found": len(paths),
        "by_kind": kinds,
        "paths": [p.to_dict() for p in paths],
    }


if __name__ == "__main__":
    import sys
    import threading
    from frontier import search_oneway, has_service as fro_has_service, _new_session

    _tl = threading.local()

    def _sess():
        s = getattr(_tl, "s", None)
        if s is None:
            s = _new_session()
            _tl.s = s
        return s

    def _search(o, d, dt):
        return search_oneway(o, d, dt, session=_sess())

    def _service(o, d, dt):
        return fro_has_service(o, d, dt, session=_sess())

    o, d, ds = (sys.argv[1:4] + ["DFW", "SAN", "2026-06-11"])[:3]
    stops = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    res = discover(o, d, datetime.fromisoformat(ds), max_stops=stops,
                   search_one=_search, has_service=_service)
    print(f"\norigin={res['origin']} dest={res['dest']} date={res['date']} "
          f"max_stops={res['max_stops']} (min_conn={res['min_conn']} max_conn={res['max_conn']}m)")
    print(f"segments: {res['segments_pruned_no_service']} pruned, "
          f"{res['segments_fetched']} fetched, {res['segment_errors']} errored"
          f"{' [TRUNCATED]' if res['truncated'] else ''}")
    print(f"paths found: {res['paths_found']}  {res['by_kind']}\n")
    for p in res["paths"][:30]:
        def hopstr(h):
            dep = datetime.fromisoformat(h["dep"]); arr = datetime.fromisoformat(h["arr"])
            return (f"{h['origin']} {dep.strftime('%a %-I:%M%p')} -> {h['dest']} "
                    f"{arr.strftime('%a %-I:%M%p')} [F9{h['legs'][0]['flight_number']}]")
        chain = "  ::  ".join(hopstr(h) for h in p["hops"])
        lay = " / ".join(f"{l['airport']} {l['minutes']//60}h{l['minutes']%60:02d}m"
                         f"{'(o/n)' if l['overnight'] else ''}" for l in p["layovers"]) or "-"
        print(f"[{p['kind']:19}] {chain}")
        print(f"      total {p['total_duration_min']//60}h{p['total_duration_min']%60:02d}m | "
              f"${p['total_fees']} | seats={p['min_seats']} | overnight={int(p['overnight'])} | layover: {lay}")
