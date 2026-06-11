"""
gopass.py — fast data path via gopassflights.com's public Socket.IO backend.

gopassflights' own backend already scrapes Frontier's InternalSelect server-side
and caches/normalizes it (their response carries a `book_page` InternalSelect URL
and a `getter:"i"` field). We connect to their websocket, emit one "get", collect
the streamed "got" events, and map them to our `Itinerary` shape so the rest of
the app doesn't care which source produced the data.

This is an undocumented third-party API (shared hardcoded token, non-standard
port). It's the *fast* path; app.py falls back to the self-contained scrape in
frontier.py whenever this errors or returns nothing.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Optional

import socketio

from frontier import Itinerary, Leg, _dur_to_min

ENDPOINT = "https://api.gopassflights.com:2443"
USER_ID = "NGY2eWc3dnU6OGc1dDZmdjk"  # hardcoded in their public funcs.js
ORIGIN = "https://gopassflights.com"
ANY_WIRE = "All Airports"


class GoPassError(RuntimeError):
    pass


def _minutes(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    return dt.hour * 60 + dt.minute


def _seats(val) -> tuple[Optional[int], Optional[str]]:
    if val is None or val in (-1, "-1"):
        return None, None
    if isinstance(val, (int, float)):
        n = int(val)
        return (n if n >= 0 else None), (f"{n} left" if n >= 0 else None)
    s = str(val)
    m = re.search(r"\d+", s)
    return (int(m.group()) if m else None), s


def _to_itinerary(raw: dict, origin: str) -> Optional[Itinerary]:
    sectors = raw.get("sectors") or []
    if not sectors:
        return None
    legs = [
        Leg(
            flight_number=(raw.get("flightIDs") or [{}])[min(i, len(raw.get("flightIDs", [])) - 1)].get("id")
                          if raw.get("flightIDs") else None,
            carrier=s.get("airline", "F9"),
            origin=s.get("from"),
            dest=s.get("to"),
            dep=s.get("depart"),
            arr=s.get("arrive"),
            dep_label=_fmt_time(s.get("depart")),
            arr_label=_fmt_time(s.get("arrive")),
            duration_min=_dur_to_min(s.get("duration")),
        )
        for i, s in enumerate(sectors)
    ]
    dep_iso = legs[0].dep
    arr_iso = legs[-1].arr
    if not dep_iso or not arr_iso:
        return None

    gw_price = raw.get("go_wild_price")
    gw_price = None if gw_price in (None, -1, -1.0) else round(float(gw_price), 2)
    seats, seats_label = _seats(raw.get("go_wild_seats"))
    go_wild = gw_price is not None and raw.get("go_wild_seats") not in (-1, "-1")

    overnight = (datetime.fromisoformat(arr_iso).date() >
                 datetime.fromisoformat(dep_iso).date())
    max_lay = 0
    for a, b in zip(legs, legs[1:]):
        gap = int((datetime.fromisoformat(b.dep) -
                   datetime.fromisoformat(a.arr)).total_seconds() // 60)
        max_lay = max(max_lay, gap)

    dest = raw.get("codes", [None])[-1] or legs[-1].dest
    return Itinerary(
        origin=origin,
        dest=dest,
        date=datetime.fromisoformat(dep_iso).strftime("%Y-%m-%d"),
        dep=dep_iso,
        arr=arr_iso,
        dep_label=raw.get("from_hour") or legs[0].dep_label,
        arr_label=raw.get("to_hour") or legs[-1].arr_label,
        dep_minutes=_minutes(dep_iso),
        arr_minutes=_minutes(arr_iso),
        stops=len(legs) - 1,
        stops_text=raw.get("flight_breaks") or ("Nonstop" if len(legs) == 1 else f"{len(legs)-1} Stop"),
        duration_min=_dur_to_min(raw.get("flight_length")) or sum(l.duration_min for l in legs),
        go_wild=go_wild,
        go_wild_fare=gw_price,
        go_wild_seats=seats,
        go_wild_seats_label=seats_label,
        discount_den_fare=raw.get("discount_den_price"),
        standard_fare=raw.get("standard_price"),
        overnight=overnight,
        max_layover_min=max_lay,
        has_six_plus_layover=max_lay >= 360,
        legs=legs,
        book_url=raw.get("book_page", ""),
    )


def _fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%-I:%M %p")
    except ValueError:
        return ""


def search_oneway(origin: str, dest: str, date: datetime,
                  fare: str = "go-wild", timeout: float = 25.0) -> list[Itinerary]:
    """Single 'get' over the gopassflights socket. dest may be 'ANY'."""
    arrival = [ANY_WIRE] if dest.upper() == "ANY" else [dest.upper()]
    payload = {
        "departAirportCode": [origin.upper()],
        "arrivalAirportCode": arrival,
        "departDateUS": date.strftime("%m/%d/%Y"),
        "returnDateUS": None,
        "trip": "one-way",
        "fare": fare,
        "travelers": {"adults": 1, "children": 0, "infants": 0},
        "browser": "chrome",
    }

    sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
    raws: list[dict] = []
    done = threading.Event()
    err: list[str] = []

    @sio.event
    def connect():
        sio.emit("get", payload)

    @sio.on("got")
    def on_got(data):
        if isinstance(data, list):
            raws.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            raws.append(data)

    @sio.on("done")
    def on_done(_=None):
        done.set()

    @sio.event
    def connect_error(data):
        err.append(f"connect_error: {data}")
        done.set()

    try:
        sio.connect(f"{ENDPOINT}?userID={USER_ID}",
                    headers={"Origin": ORIGIN},
                    transports=["websocket"], wait_timeout=10)
    except Exception as e:
        raise GoPassError(f"connect failed: {e}")

    finished = done.wait(timeout=timeout)
    try:
        sio.disconnect()
    except Exception:
        pass

    if err:
        raise GoPassError(err[0])
    if not finished and not raws:
        raise GoPassError("timed out with no data")

    out = []
    for r in raws:
        try:
            it = _to_itinerary(r, origin.upper())
            if it:
                out.append(it)
        except Exception:
            continue
    return out


if __name__ == "__main__":
    import sys, json
    o, d, ds = (sys.argv[1:4] + ["DFW", "SAN", "2026-06-11"])[:3]
    res = search_oneway(o, d, datetime.fromisoformat(ds))
    gw = [r for r in res if r.go_wild]
    print(f"{len(res)} itineraries, {len(gw)} GO WILD\n")
    for r in sorted(res, key=lambda x: x.dep)[:40]:
        flag = "GOWILD" if r.go_wild else "      "
        seats = r.go_wild_seats_label or ""
        print(f"[{flag}] {r.dep_label:>8} -> {r.arr_label:<8} | {r.origin}->{r.dest} "
              f"| {r.stops_text:<11} | {r.duration_min//60}h{r.duration_min%60:02d}m "
              f"| lay={r.max_layover_min}m over={int(r.overnight)} | ${r.go_wild_fare or '-'} {seats}")
