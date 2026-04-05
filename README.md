# Virgin Atlantic Flight Search — Pure HTTP

A Python script that fetches real flight offers from Virgin Atlantic using only HTTP requests — no browser automation, no headless Chrome, no JavaScript execution.

## Python version & setup

```
python3.12 -V   # Python 3.12.x
```

Install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

Or with pip:

```bash
pip install "curl-cffi>=0.14.0"
```

## Run

```bash
uv run python main.py
```

Optional flags:

```bash
uv run python main.py --origin MAN --destination YYZ --date 2026-05-15
uv run python main.py --locale en-US
uv run python main.py --proxy http://user:pass@host:port
uv run python main.py --retries 5 --output my_result.json
```

All flags and their defaults:

| Flag | Default | Description |
|------|---------|-------------|
| `--origin` | `MAN` | Origin airport IATA code |
| `--destination` | `YYZ` | Destination airport IATA code |
| `--date` | `2026-05-15` | Departure date (`YYYY-MM-DD`) |
| `--locale` | `en-GB` | Homepage locale / Point-of-Sale context |
| `--proxy` | _(none)_ | HTTP(S) proxy URL |
| `--retries` | `3` | Max retry attempts for the search POST |
| `--output` | `result.json` | Path to write the full JSON response |

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Search failed (network, timeout, unexpected HTTP status) |
| `2` | Akamai 429 bot-detection triggered |
| `3` | Unexpected response schema |
| `130` | Interrupted (`Ctrl+C`) |

## Hardcoded search parameters (defaults)

| Field | Value |
|-------|-------|
| Origin | MAN (Manchester Airport) |
| Destination | YYZ (Toronto Pearson International) |
| Date | 2026-05-15 |
| Passengers | 1 adult |
| Trip type | One-way |
| Fare type | Cash / retail (`awardSearch: false`) |
| Cabin | All cabins returned (ECONOMY-LIGHT is the lowest) |

## How it works

### The approach

`www.virginatlantic.com/flights/search/api/graphql` is gated by **Akamai Bot Manager Premier**. Any XHR/CORS request from an unvalidated session returns `429 {"cpr_chlge":"true"}`.

During reverse-engineering of the live session HAR, a separate BFF (Backend-for-Frontend) GraphQL endpoint was discovered at `resources.virginatlantic.com/search/public/v1/GraphQL`. It serves **identical flight offer data** without Akamai bot-protection gating and is accessible with session cookies from a homepage GET alone.

### Request flow (2 requests)

| # | Method | URL | Purpose |
|---|--------|-----|---------|
| 1 | GET | `www.virginatlantic.com/en-GB` | Seeds session cookies (`bm_sz`, `AKA_A2`, `_abck`) via Akamai edge |
| 2 | POST | `resources.virginatlantic.com/search/public/v1/GraphQL` | `SearchOffers` GraphQL — returns real itinerary + fare data |

A 500 ms pause between the two requests is intentional: Akamai's edge needs time to propagate the `bm_sz` session state before it accepts same-session XHR-origin requests. This was determined empirically and is documented in `SearchConfig.seed_delay`.

### GraphQL endpoint & variables

**URL:** `https://resources.virginatlantic.com/search/public/v1/GraphQL`

**Operation:** `SearchOffers`

Key variables sent:

```json
{
  "request": {
    "flightSearchRequest": {
      "searchOriginDestinations": [
        {"origin": "MAN", "destination": "YYZ", "departureDate": "2026-05-15"}
      ],
      "bundleOffer": false,
      "awardSearch": false,
      "calendarSearch": false,
      "flexiDateSearch": false,
      "nonStopOnly": false,
      "currentTripIndexId": "0",
      "checkInBaggageAllowance": false,
      "carryOnBaggageAllowance": false,
      "refundableOnly": false
    },
    "customerDetails": [{"custId": "ADT_0", "ptc": "ADT"}]
  }
}
```

### TLS & headers

**Library:** `curl_cffi 0.14` with `impersonate="chrome142"` — reproduces Chrome 142's TLS ClientHello (JA3/JA4 fingerprint), ALPN (`h2`), and HTTP/2 SETTINGS frames. Required to pass Akamai's TLS fingerprint check on the homepage GET.

**User-Agent:** `Chrome/142.0.0.0` on Windows 10. Kept in sync with the impersonation profile — Akamai can correlate the JA3/JA4 fingerprint against the declared browser version; a mismatch (e.g., `chrome131` TLS with a `Chrome/146` UA) is a detectable signal.

**`sec-fetch-site: same-site`** on the GraphQL POST — `www.virginatlantic.com` and `resources.virginatlantic.com` share the `.virginatlantic.com` registrable domain, so a browser legitimately emits `same-site` here. This is not spoofed.

**Locale / POS:** `en-GB` by default. The locale used in the homepage GET URL sets the Point-of-Sale context, which affects which fare sets are returned and the currency of displayed prices. Override with `--locale` if testing from a different market.

### Why not `www.virginatlantic.com/flights/search/api/graphql`?

This endpoint sits behind Akamai Bot Manager Premier with three distinct layers:

1. **TLS fingerprint (JA3/JA4)** — checked on every request. `curl_cffi` with Chrome impersonation passes this.
2. **`_abck` cookie validation** — requires a cryptographically valid `sensor_data` POST. The HMAC in field 6 of `sensor_data` is computed by Akamai's obfuscated `bmak.js` using browser fingerprint values (canvas, audio, screen) and a session nonce. Without reimplementing this derivation, `_abck` stays at `~-1~` (unvalidated) and all XHR calls return 429.
3. **IP reputation** — Akamai scores IPs independently; datacenter/cloud IPs receive lower trust scores and are more likely to trigger challenges.

The `resources.virginatlantic.com` endpoint sidesteps all three layers.

### Retry & error handling

The search POST is wrapped in exponential backoff (`base=2s`, doubling per attempt, configurable via `--retries`). Akamai 429 responses are **not retried** — they indicate bot detection that requires a new session or IP, not a transient failure. On any non-200 response the full body is written to `error_<label>.txt` for inspection.

## Expected output

```
[INFO] Step 1: GET https://www.virginatlantic.com/en-GB (seed cookies)
[INFO]   status=200  bm_sz=set  AKA_A2=set
[INFO] Step 2: POST SearchOffers — MAN→YYZ on 2026-05-15 (retries=3)
[INFO]   status=200  bytes=54321

================================================================
SUCCESS — Virgin Atlantic SearchOffers
================================================================
Route   : MAN (Manchester) → YYZ (Toronto)
Date    : 2026-05-15
Slices  : 0 / 1
Note    : 'available' field is always null on this endpoint — fare presence confirms bookability.

  MAN→YYZ  dep=2026-05-15T14:55:00  arr=2026-05-15T23:16:00  dur=PT13H21M  ECONOMY-LIGHT  GBP 764.73
  MAN→YYZ  dep=2026-05-15T13:45:00  arr=2026-05-15T19:55:00  dur=PT11H10M  ECONOMY-LIGHT  GBP 743.93
  ...
================================================================
[INFO] Full response saved to result.json
```

## Limitations & reproducibility notes

| Issue | Detail |
|-------|--------|
| **Endpoint stability** | `resources.virginatlantic.com/search/public/v1/GraphQL` is an internal BFF endpoint not documented in any public API. Virgin Atlantic could add authentication, rate-limiting, or Akamai gating without notice. If that happens, the fallback path requires reimplementing the Akamai `bmak.js` HMAC derivation — a multi-day reverse-engineering effort. |
| **Schema changes** | `FlightOfferRequestInput`, `Result`, and `SliceFare` GraphQL types are undocumented and subject to change. |
| **Cookie TTL** | `bm_sz` and `_abck` from the homepage GET have ~2 hour TTL. Each run performs a fresh GET so this is handled automatically. |
| **IP reputation** | Datacenter and cloud IPs receive lower Akamai trust scores and are more likely to trigger 429 challenges even on the BFF endpoint. Residential IPs are more reliable. |
| **`available` field** | `SliceFare.available` always returns `null` on this endpoint (the field is typed `Int`, not `Boolean`, and is never populated by this BFF). Presence of a fare in the response is the correct availability signal. |
| **Locale / POS** | Fare pricing and available cabins vary by locale. Default is `en-GB`; use `--locale` to test other markets. |

## Akamai analysis (reference)

The original target endpoint flow when attempting the protected path:

```
GET  /en-GB                        → 200  (_abck set to ~-1~, unvalidated)
POST /YLG1O1/.../6YScB             → 201  (sensor accepted, but HMAC invalid → _abck stays ~-1~)
GET  /flights/search/slice         → 200  (navigation, not gated)
POST /flights/search/api/graphql   → 429  (_abck never validated)
```

The `sensor_data` format is `3;0;1;{flags};{nonce};{hmac};{browser_info};{telemetry}`. The HMAC (field 6) is SHA-256 of browser fingerprint data processed through a custom key-mixing function inside `bmak.js`. The nonce comes from the `bm_sz` cookie (`{hex}~{base64}~{nonce}~{key}`, last two `~`-delimited segments). Without a valid HMAC, `_abck` never transitions from `~-1~` to `~0~` and all XHR endpoints return 429.
