"""
frontier.py — pull and parse Frontier one-way flight availability, with a focus
on GO WILD! pass bookability.

Data source: the undocumented SkySales endpoint that powers flyfrontier.com:

    https://booking.flyfrontier.com/Flight/InternalSelect
        ?o1=<ORIGIN>&d1=<DEST>&dd1=<Mon-DD, YYYY>&ADT=1&mon=true&promo=

It returns an HTML page with an embedded JSON `journeys` array. We slice that
array out and normalise each itinerary into an `Itinerary` dataclass.

Frontier sits behind Akamai bot protection, so we use curl_cffi with Chrome TLS
impersonation and warm the session against www.flyfrontier.com first.
"""
from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional

from curl_cffi import requests as cf

BOOKING_HOST = "https://booking.flyfrontier.com"
WARM_URL = "https://www.flyfrontier.com"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    flight_number: int
    carrier: str
    origin: str
    dest: str
    dep: str  # ISO, e.g. "2026-06-11T17:39:00"
    arr: str
    dep_label: str  # e.g. "5:39 PM"
    arr_label: str
    duration_min: int


@dataclass
class Itinerary:
    origin: str
    dest: str
    date: str  # YYYY-MM-DD (departure date)
    dep: str  # ISO datetime of first leg departure
    arr: str  # ISO datetime of last leg arrival
    dep_label: str
    arr_label: str
    dep_minutes: int  # minutes past midnight, for time-of-day filtering
    arr_minutes: int
    stops: int
    stops_text: str
    duration_min: int  # total trip time, gate-to-gate
    go_wild: bool
    go_wild_fare: Optional[float]
    go_wild_seats: Optional[int]
    go_wild_seats_label: Optional[str]
    discount_den_fare: Optional[float]
    standard_fare: Optional[float]
    overnight: bool  # arrives on a later calendar day than departure
    max_layover_min: int  # longest layover between legs (0 for nonstop)
    has_six_plus_layover: bool
    legs: list[Leg] = field(default_factory=list)
    book_url: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
class FrontierError(RuntimeError):
    pass


def _new_session() -> cf.Session:
    s = cf.Session(impersonate="chrome")
    # Warm up: pick up Akamai / dotrez cookies before hitting the booking host.
    s.get(WARM_URL, timeout=15)
    return s


def fetch_raw(origin: str, dest: str, date: datetime,
              session: Optional[cf.Session] = None) -> str:
    s = session or _new_session()
    dd = date.strftime("%b-%d, %Y")  # "Jun-11, 2026"
    url = (
        f"{BOOKING_HOST}/Flight/InternalSelect"
        f"?o1={origin}&d1={dest}&dd1={dd}&ADT=1&mon=true&promo="
    )
    r = s.get(url, timeout=30)
    if r.status_code != 200:
        raise FrontierError(f"{origin}->{dest} {dd}: HTTP {r.status_code}")
    return r.text


def booking_url(origin: str, dest: str, date: datetime) -> str:
    dd = date.strftime("%b-%d, %Y")
    return (
        f"{BOOKING_HOST}/Flight/InternalSelect"
        f"?o1={origin}&d1={dest}&dd1={dd}&ADT=1&mon=true&promo="
    )


def has_service(origin: str, dest: str, date: datetime,
                session: Optional[cf.Session] = None) -> Optional[bool]:
    """Cheap pre-filter: does this route operate on this date?

    Hits /Flight/RetrieveSchedule (tiny JSON, no booking session needed) and
    checks lastAvailableDate + disabledDates. Returns True/False, or None if the
    check itself fails (caller should then not prune — treat as "maybe").
    """
    s = session or _new_session()
    url = (f"{BOOKING_HOST}/Flight/RetrieveSchedule"
           f"?calendarSelectableDays.Origin={origin}"
           f"&calendarSelectableDays.Destination={dest}")
    try:
        r = s.get(url, timeout=15)
        if r.status_code != 200:
            return None
        cal = (r.json() or {}).get("calendarSelectableDays") or {}
    except Exception:
        return None
    last = cal.get("lastAvailableDate")
    if last:
        try:
            if date.date() > datetime.fromisoformat(last.split(" ")[0]).date():
                return False
        except ValueError:
            pass
    disabled = cal.get("disabledDates") or []
    # disabledDates look like "6/12/2026" (no zero-padding)
    key = f"{date.month}/{date.day}/{date.year}"
    return key not in disabled


# --------------------------------------------------------------------------- #
# Parse
# --------------------------------------------------------------------------- #
def _extract_journeys(page_html: str) -> list[dict]:
    """Slice the fare-bearing `journeys` array out of the page HTML."""
    t = html.unescape(page_html)
    # The fare-bearing array starts with the per-itinerary objects that carry
    # isReturnTrip/flights. (A second, metadata-only journeys array also exists.)
    i = t.find('"journeys":[{"isReturnTrip"')
    if i == -1:
        if "Access Denied" in t or "Reference #" in t:
            raise FrontierError("Blocked by Akamai (Access Denied).")
        # Valid page but no flights on this date/route -> empty result.
        return []
    arr_start = t.find("[", i)
    depth = 0
    for k in range(arr_start, len(t)):
        c = t[k]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return json.loads(t[arr_start:k + 1])
    raise FrontierError("Unbalanced brackets parsing journeys array.")


def _dur_to_min(text: Optional[str]) -> int:
    """'3 hrs 11 min' / '03:11:00' / '3hrs 11min' -> minutes."""
    if not text:
        return 0
    if ":" in text and "hr" not in text:  # HH:MM:SS
        parts = text.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    h = re.search(r"(\d+)\s*hr", text)
    m = re.search(r"(\d+)\s*min", text)
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def _parse_seats(val) -> tuple[Optional[int], Optional[str]]:
    """goWildFareSeatsRemaining is None, an int, or a label like '2 Seats Left!'."""
    if val is None:
        return None, None
    if isinstance(val, (int, float)):
        return int(val), None
    s = str(val)
    m = re.search(r"\d+", s)
    return (int(m.group()) if m else None), s


def _minutes_past_midnight(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    return dt.hour * 60 + dt.minute


def _layovers(legs: list[Leg]) -> int:
    """Longest gap (minutes) between consecutive legs."""
    longest = 0
    for a, b in zip(legs, legs[1:]):
        gap = int((datetime.fromisoformat(b.dep) -
                   datetime.fromisoformat(a.arr)).total_seconds() // 60)
        longest = max(longest, gap)
    return longest


def parse(page_html: str, origin: str, dest: str, date: datetime) -> list[Itinerary]:
    journeys = _extract_journeys(page_html)
    out: list[Itinerary] = []
    seen = set()
    for j in journeys:
        for f in j.get("flights", []):
            raw_legs = f.get("legs", [])
            if not raw_legs:
                continue
            legs = [
                Leg(
                    flight_number=L.get("flightNumber"),
                    carrier=L.get("carrierCode", "F9"),
                    origin=L.get("departureStation"),
                    dest=L.get("arrivalStation"),
                    dep=L.get("departureDate"),
                    arr=L.get("arrivalDate"),
                    dep_label=L.get("departureDateFormatted", ""),
                    arr_label=L.get("arrivalDateFormatted", ""),
                    duration_min=_dur_to_min(L.get("duration")),
                )
                for L in raw_legs
            ]
            key = (tuple((l.flight_number, l.dep) for l in legs), f.get("stopCount"))
            if key in seen:
                continue
            seen.add(key)

            dep_iso = legs[0].dep
            arr_iso = legs[-1].arr
            seats, seats_label = _parse_seats(f.get("goWildFareSeatsRemaining"))
            gw_fare = f.get("goWildFare")
            gw_fare = None if gw_fare in (None, -1, -1.0) else round(float(gw_fare), 2)
            overnight = bool(f.get("isNextDayArrival")) or (
                datetime.fromisoformat(arr_iso).date() >
                datetime.fromisoformat(dep_iso).date()
            )
            out.append(Itinerary(
                origin=origin,
                dest=dest,
                date=date.strftime("%Y-%m-%d"),
                dep=dep_iso,
                arr=arr_iso,
                dep_label=legs[0].dep_label,
                arr_label=legs[-1].arr_label,
                dep_minutes=_minutes_past_midnight(dep_iso),
                arr_minutes=_minutes_past_midnight(arr_iso),
                stops=f.get("stopCount", len(legs) - 1),
                stops_text=f.get("stopsText", ""),
                duration_min=_dur_to_min(f.get("duration")),
                go_wild=bool(f.get("isGoWildFareEnabled")),
                go_wild_fare=gw_fare,
                go_wild_seats=seats,
                go_wild_seats_label=seats_label,
                discount_den_fare=f.get("discountDenFare"),
                standard_fare=f.get("standardFare"),
                overnight=overnight,
                max_layover_min=_layovers(legs),
                has_six_plus_layover=bool(f.get("hasSixPlusLayover")),
                legs=legs,
                book_url=booking_url(origin, dest, date),
            ))
    return out


def search_oneway(origin: str, dest: str, date: datetime,
                  session: Optional[cf.Session] = None) -> list[Itinerary]:
    """Fetch + parse a single one-way origin->dest on a date."""
    raw = fetch_raw(origin, dest, date, session=session)
    return parse(raw, origin.upper(), dest.upper(), date)


if __name__ == "__main__":
    import sys
    o, d, ds = (sys.argv[1:4] + ["DFW", "SAN", "2026-06-11"])[:3]
    res = search_oneway(o, d, datetime.fromisoformat(ds))
    gw = [r for r in res if r.go_wild]
    print(f"{len(res)} itineraries, {len(gw)} GO WILD-available\n")
    for r in sorted(res, key=lambda x: x.dep):
        flag = "GOWILD" if r.go_wild else "      "
        seats = r.go_wild_seats_label or (str(r.go_wild_seats) if r.go_wild_seats else "")
        print(f"[{flag}] {r.dep_label:>8} -> {r.arr_label:<8} | {r.stops_text:<11}"
              f" | {r.duration_min//60}h{r.duration_min%60:02d}m | maxlay={r.max_layover_min}m"
              f" | overnight={int(r.overnight)} | ${r.go_wild_fare or '-'} {seats}")
