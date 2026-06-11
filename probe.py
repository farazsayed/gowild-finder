"""
Probe: validate the Frontier InternalSelect endpoint and GO WILD parsing.
Test route: DFW -> SAN on 2026-06-11 (one way).

Run: ./.venv/bin/python probe.py
"""
import html
import json
import sys
from datetime import datetime

from curl_cffi import requests as cf


def fetch(origin: str, dest: str, date: datetime):
    s = cf.Session(impersonate="chrome")
    # 1. Warm the session to pick up Akamai cookies
    s.get("https://www.flyfrontier.com", timeout=15)
    # 2. Hit the internal endpoint. Date format e.g. "Jun-11, 2026"
    dd = date.strftime("%b-%d, %Y")
    url = (
        "https://booking.flyfrontier.com/Flight/InternalSelect"
        f"?o1={origin}&d1={dest}&dd1={dd}&ADT=1&mon=true&promo="
    )
    print(f"GET {url}", file=sys.stderr)
    r = s.get(url, timeout=25)
    print(f"status={r.status_code} len={len(r.text)}", file=sys.stderr)
    return r


def extract_json(text: str):
    text = html.unescape(text)
    key = '{"journeys"'
    start = text.find(key)
    if start == -1:
        # dump a window around 'journey' to help debug
        idx = text.lower().find("journey")
        snippet = text[max(0, idx - 200): idx + 400] if idx != -1 else text[:1000]
        raise ValueError(f"'{key}' not found. Nearby:\n{snippet}")
    # find the matching close brace by scanning
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start: i + 1])
    raise ValueError("Unbalanced braces while extracting journeys JSON")


def main():
    r = fetch("DFW", "SAN", datetime(2026, 6, 11))
    if r.status_code != 200:
        print("Non-200; dumping first 800 chars:")
        print(r.text[:800])
        return
    try:
        data = extract_json(r.text)
    except ValueError as e:
        print("PARSE FAILED:", e)
        # save raw for inspection
        with open("probe_raw.html", "w") as f:
            f.write(r.text)
        print("Raw saved to probe_raw.html")
        return

    journeys = data.get("journeys", [])
    print(f"\n=== {len(journeys)} journeys returned ===\n")
    for j in journeys:
        flights = j.get("flights", [])
        for fl in flights:
            keys = list(fl.keys())
            gw = fl.get("isGoWildFareEnabled")
            seats = fl.get("goWildFareSeatsRemaining")
            fare = fl.get("goWildFare", fl.get("discountDenFare"))
            stops = fl.get("stopsText", "?")
            dur = fl.get("duration", "?")
            legs = fl.get("legs", [])
            dep = legs[0].get("departureDateFormatted") if legs else "?"
            arr = legs[-1].get("arrivalDateFormatted") if legs else "?"
            print(
                f"GoWild={gw} seats={seats} fare={fare} | {dep} -> {arr} "
                f"| {stops} | {dur}"
            )
    # Dump the field names of the first flight so we know the real schema
    if journeys and journeys[0].get("flights"):
        print("\n=== first flight raw keys ===")
        print(sorted(journeys[0]["flights"][0].keys()))
        print("\n=== first flight raw JSON ===")
        print(json.dumps(journeys[0]["flights"][0], indent=2)[:2500])


if __name__ == "__main__":
    main()
