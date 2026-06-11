# GO WILD Finder

A one-way flight finder for Frontier Airlines' **GO WILD!** all-you-can-fly pass.
Focused on **one-way** trips for now.

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

## ✈️ Getting the most out of it — the pass + the card
This tool is built around the **GO WILD! pass**: a flat monthly/annual fee for
all-you-can-fly. The hard part of GO WILD is *finding which routes and dates actually
have seats* — that's exactly what this does, including cheaper self-transfer routings a
normal search hides. If you're going to fly Frontier a lot, pairing it with the pass
(and the co-brand card for the taxes/fees you still pay) is where it pays for itself.

**GO WILD! pass — referral.** During pass-launch promo windows, a new passholder can
enter an existing member's **FRONTIER Miles number** as a referral code; the referrer
earns Frontier vouchers (recently **$25 per referral, up to $250**). If you sign up,
use mine:

> **GO WILD referral code (FRONTIER Miles #):** `<<YOUR_FRONTIER_MILES_NUMBER>>`
> Sign up → https://www.flyfrontier.com/deals/gowild-pass/

**Frontier World Mastercard.** Pairs naturally with GO WILD — a sizable signup bonus,
up to **15× miles** on Frontier purchases, and annual perks that help offset the
taxes/fees GO WILD segments still carry. Apply with my link:

> **Card referral / apply:** `<<YOUR_CARD_REFERRAL_LINK>>`
> (public page if you don't use a referral: https://www.flyfrontier.com/mastercard)

> *Referral terms, voucher amounts, and promo windows change frequently — always
> confirm the current offer on Frontier's site before signing up.*

## Route Builder — how path discovery works (`pathfinder.py`)
A demand-driven, multi-day graph search, cheap-to-expensive so it never blind-fetches:
1. **Topological prune** — enumerate only hub paths that physically exist in the
   nonstop route graph (`O→X→D` where both hops are real Frontier routes).
2. **Schedule pre-filter** — a lightweight check drops route+date combos with no
   service before any heavier lookups.
3. **Fetch** the surviving segments live (completeness matters here), keeping the
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

## How it works
GO WILD availability is read from Frontier's public booking site and normalized into
a common itinerary shape (`frontier.py`). An optional faster path can pull from an
external cached source when available (`gopass.py`); the app falls back to the live
read for completeness and freshness. Each response reports which `source` it used,
shown in the UI status line.

**Routing rule (in `app.py`):**
| Search | Primary | Fallback |
|---|---|---|
| Specific route (e.g. DFW→SAN) | live read (most complete + fresh) | fast source |
| Anywhere | fast source (one call) | live fan-out |
| Anywhere + **Thorough** toggle | full live fan-out | — |

The nonstop route network that powers "Anywhere" and the route builder is derived
once and cached to `routes.json` by `routegraph.py`.

## Files
| File | Purpose |
|---|---|
| `frontier.py` | Live data read + parse into `Itinerary` objects; schedule pre-filter |
| `gopass.py` | Optional fast external data source (same `Itinerary` shape) |
| `pathfinder.py` | Route-builder path discovery (multi-day BFS over the route graph) |
| `routegraph.py` | Build/cache the nonstop route graph (`routes.json`) |
| `app.py` | FastAPI backend — `/api/search`, `/api/paths`, `/api/airports`, `/api/destinations` |
| `static/index.html` | Single-page UI (one-way search + route builder, filters) |
| `probe.py` | Standalone data-source validation script |

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
- The data source is unofficial and its shape can change without notice (the parser
  is defensive). Be considerate with request volume — especially "Anywhere," which
  fans out one lookup per nonstop destination (~107 for DFW) using bounded concurrency.

## Disclaimer
Personal, educational project — **not affiliated with, authorized, or endorsed by
Frontier Airlines**. "GO WILD!" and "Frontier" are trademarks of their respective
owner. Read-only and for personal use; please use responsibly and in accordance with
the relevant sites' terms of service, and always confirm prices and book on Frontier's
official site. No warranty — prices and availability change constantly.
