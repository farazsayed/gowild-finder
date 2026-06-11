# GO WILD Finder

A one-way flight finder for Frontier Airlines' **GO WILD!** all-you-can-fly pass,
inspired by gopassflights.com. Focused on **one-way** trips for now.

## ▶ Launch

```bash
./.venv/bin/python -m uvicorn app:app --port 8011    # then open http://127.0.0.1:8011
```

First-time setup (creates the venv, installs deps, builds the route graph):

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt && ./.venv/bin/python routegraph.py
```

## Features
- **One-way search** — origin → destination on a date, GO WILD availability surfaced.
- **"Anywhere"** — fan out across every nonstop destination from an origin to find
  any GO WILD-bookable flight that day.
- **Route Builder (path discovery)** — given start + end + date, find **every** GO WILD
  way to get there: nonstop, Frontier's own through-connections, and **self-transfers**
  stitched through hubs as separate one-way GO WILD tickets — including
  **overnight-at-hub / multi-day** routings.
- **Filters** — GO WILD only, nonstop only, no overnight, max layover (hrs),
  max trip duration, and **departure / arrival time-of-day ranges**.
- **Airport names on hover** — hover any 3-letter code (results, route cards,
  layovers) to reveal the full city/state, e.g. `DFW → "Dallas/Ft. Worth, TX (DFW)"`.

## Route Builder — how path discovery works (`pathfinder.py`)
A demand-driven, multi-day graph search, cheap-to-expensive so it never blind-fetches:
1. **Topological prune** — enumerate only hub paths that physically exist in the
   nonstop route graph (`O→X→D` where both hops are real Frontier routes).
2. **Service pre-filter** — `/Flight/RetrieveSchedule` (tiny JSON, no Akamai) drops
   route+date combos with no service before any heavy scrape.
3. **Live-scrape** the surviving segments (completeness matters here), keeping the
   nonstop GO WILD flights as atomic "hops".
4. **Assemble** concrete paths via BFS, allowing a connection to spill to a **later
   day** (overnight-at-hub) within the max-connection window.

Each path is tagged: `direct` (nonstop), `frontier-connection` (one GO WILD ticket
through a hub), or `self-transfer` (separate GO WILD tickets per hop — fees add up,
binding seats = min across hops). A self-transfer that exactly matches a Frontier
through-ticket is de-duplicated in favor of the single ticket.

> **Reality check:** GO WILD seats cluster on off-peak (late/early) flights, so
> same-day self-transfers rarely connect; the useful ones are overnight-at-hub.
> And because Frontier already sells most viable routings as single through-tickets,
> standalone self-transfer paths are uncommon — the engine surfaces them only when
> they're genuinely distinct from (or cheaper than) a bundled connection.

## How it works — hybrid data source
There are two ways to get GO WILD data; the app picks per search type.

**A) Live scrape (source of truth) — `frontier.py`.** Frontier's site is powered by
an undocumented Navitaire SkySales endpoint:

```
GET https://booking.flyfrontier.com/Flight/InternalSelect
    ?o1=<ORIGIN>&d1=<DEST>&dd1=<Mon-DD, YYYY>&ADT=1&mon=true&promo=
```

It returns HTML with an embedded JSON `journeys` array. Each flight exposes
`isGoWildFareEnabled`, `goWildFare`, `goWildFareSeatsRemaining`, per-leg times,
`stopsText`, `hasSixPlusLayover`, and `isNextDayArrival` — everything the filters
need. Frontier sits behind Akamai, so we use **curl_cffi** (Chrome TLS
impersonation) and warm the session against `www.flyfrontier.com` first.

**B) Fast path (gopassflights) — `gopass.py`.** gopassflights.com's own backend
already scrapes the same InternalSelect server-side and caches it, exposed via a
public Socket.IO endpoint (`wss://api.gopassflights.com:2443`). One `emit("get")`
returns the entire "anywhere" map in ~4s. It's faster but **~half-complete** (its
cache drops some bookable GO WILD flights), so we don't trust it for booking.

**Routing rule (in `app.py`):**
| Search | Primary | Fallback |
|---|---|---|
| Specific route (DFW→SAN) | live scrape (complete + fresh) | gopassflights |
| Anywhere | gopassflights (fast, one call) | live fan-out |
| Anywhere + **Thorough** toggle | live fan-out (all destinations) | — |

Each response includes a `source` field, shown in the UI status line.

The full nonstop route network is embedded in the booking page (a `stations`
array); `routegraph.py` extracts and caches it to `routes.json` to power "Anywhere".

## Files
| File | Purpose |
|---|---|
| `frontier.py` | Live scrape — fetch + parse InternalSelect; `has_service` pre-filter |
| `gopass.py` | Fast path — gopassflights Socket.IO client, same `Itinerary` shape |
| `pathfinder.py` | Route-builder path discovery (multi-day BFS over the route graph) |
| `routegraph.py` | Build/cache Frontier's nonstop route graph (`routes.json`) |
| `app.py` | FastAPI backend — `/api/search`, `/api/paths`, `/api/airports`, `/api/destinations` |
| `static/index.html` | Single-page UI (one-way search + route builder, filters) |
| `probe.py` | Standalone validation script for the endpoint |

CLI test for the path engine:
```bash
./.venv/bin/python pathfinder.py DFW SAN 2026-06-11 1   # origin dest date max_stops
```

## Run
```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python routegraph.py          # build routes.json (first run)
./.venv/bin/python -m uvicorn app:app --port 8011
# open http://127.0.0.1:8011
```

CLI smoke test:
```bash
./.venv/bin/python frontier.py DFW SAN 2026-06-11
```

## GO WILD booking-window notes
- Frontier's docs say domestic GO WILD "unlocks the day before departure," but in
  practice the **GO WILD fare shows several days out** (verified: 6/12 and 6/13 both
  returned GO WILD-enabled flights). The "Early Booking" option lets you book further
  ahead for a fee on select dates.
- Per-segment fare is $0.01 + taxes/fees (≈ $15 domestic nonstop). Frontier's bundled
  through-connections can cost much more (DFW→SAN via a hub ≈ $114), which is exactly
  why **self-transfers can be cheaper** — see below.
- A polling/alert layer ("notify me when a GO WILD seat opens on this route") is the
  most natural next addition.

## Why the Route Builder finds deals a normal search misses
Frontier prices two cheap nonstop GO WILD legs far below its own through-connection,
so stitching them yourself (a "self-transfer") can be dramatically cheaper. Real
example the engine surfaced for **DFW → MCO on 2026-06-11**:

| Option | Routing | Price |
|---|---|---|
| Frontier through-connection | DFW → MCO via a hub (one ticket) | **$114.40** |
| **Self-transfer (2 GO WILD tickets)** | DFW→ATL 6:25 AM ($15.41) + ATL→MCO 8:21 PM ($15.41) | **$30.82** |

Same day, ~$84 cheaper. Sort the Route Builder by **Cheapest** to put these first.
(The scan found self-transfers on 7 of 20 sampled DFW routes.)

## Caveats
- Scrapes an undocumented endpoint; the JSON shape can change without notice
  (the parser is defensive). Rate-limit the "Anywhere" fan-out responsibly.
- "Anywhere" fans out one request per nonstop destination (~107 for DFW); the
  backend uses bounded concurrency (~13s for DFW) with one warmed session per worker.
