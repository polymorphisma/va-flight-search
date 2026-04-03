# Virgin Atlantic Flight Search — Pure HTTP

A minimal Python script that fetches real flight offers from Virgin Atlantic using only HTTP requests — no browser automation, no headless Chrome, no JavaScript execution.

## Python version & setup

```
python3.12 -V   # Python 3.12.3
```

Install dependencies (using [uv](https://docs.astral.sh/uv/)):

```bash
uv sync
```

Or with pip:

```bash
pip install curl-cffi>=0.14.0
```

## Run

```bash
uv run python main.py
```

Optional flags:

```bash
uv run python main.py --origin MAN --destination YYZ --date 2026-05-15
uv run python main.py --proxy http://user:pass@host:port
```

## Hardcoded search parameters

| Field       | Value |
|-------------|-------|
| Origin      | MAN (Manchester Airport) |
| Destination | YYZ (Toronto Pearson International) |
| Date        | 2026-05-15 |
| Passengers  | 1 adult |
| Trip type   | One-way |
| Fare type   | Cash / retail (`awardSearch: false`) |
| Cabin       | All cabins returned (ECONOMY-LIGHT is the default lowest) |

## How it works

### The approach

The standard `www.virginatlantic.com/flights/search/api/graphql` endpoint is gated by **Akamai Bot Manager Premier**. Any XHR/CORS request from an unvalidated session gets a `429 {"cpr_chlge":"true"}` response.

During reverse-engineering of the live session HAR, it was discovered that `resources.virginatlantic.com/search/public/v1/GraphQL` is a separate backend GraphQL endpoint that serves the **same flight offer data** without Akamai bot-protection gating. This is the same endpoint the frontend app uses for auxiliary queries (sales banners, etc.) and is accessible with just a valid session cookie from a homepage GET.

### Request flow (2 requests total)

| # | Method | URL | Purpose |
|---|--------|-----|---------|
| 1 | GET | `www.virginatlantic.com/en-IN` | Seeds session cookies (`bm_sz`, `AKA_A2`, `_abck`) via Akamai edge |
| 2 | POST | `resources.virginatlantic.com/search/public/v1/GraphQL` | `SearchOffers` GraphQL — returns real itinerary + fare data |

### GraphQL endpoint & variables

**URL:** `https://resources.virginatlantic.com/search/public/v1/GraphQL`

**Operation:** `SearchOffers`

**Input type:** `FlightOfferRequestInput` → `flightSearchRequest: FlightSearchRequestInput`

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

- **Library:** `curl_cffi 0.14` with `impersonate="chrome142"` — reproduces Chrome 142's TLS ClientHello (JA3/JA4 fingerprint), ALPN (`h2`), and HTTP/2 SETTINGS frames exactly. This is required to pass Akamai's TLS fingerprint check on the homepage GET.
- **User-Agent:** `Chrome/146.0.0.0` on Windows 10, matching the observed browser session.
- **`sec-fetch-site: same-site`** on the GraphQL request — `www.virginatlantic.com` and `resources.virginatlantic.com` share a registrable domain, so the browser sends this header automatically.

### Why not `www.virginatlantic.com/flights/search/api/graphql`?

This is the frontend-facing search endpoint. It sits behind Akamai Bot Manager Premier with these layers:

1. **TLS fingerprint (JA3/JA4)** — checked on every request. `curl_cffi` with Chrome impersonation passes this.
2. **`_abck` cookie validation** — requires a cryptographically valid `sensor_data` POST. The HMAC in field 6 of `sensor_data` is computed by Akamai's obfuscated `bmak.js` using browser fingerprint values (canvas, audio, screen) and a session nonce. Without reimplementing this derivation in Python, `_abck` stays at `~-1~` (unvalidated) and all XHR calls return 429.
3. **IP reputation** — Akamai scores IPs independently; datacenter/WSL2 IPs receive lower trust scores.

The `resources.virginatlantic.com` endpoint sidesteps all three layers entirely.

## Expected output

```
[*] Step 1: GET https://www.virginatlantic.com/en-IN (seed cookies)
[*]   status=200  bm_sz=set
[*] Step 2: POST SearchOffers — MAN→YYZ on 2026-05-15
[*]   status=200

============================================================
SUCCESS — Virgin Atlantic SearchOffers
============================================================
Route   : MAN (Manchester) → YYZ (Toronto)
Date    : 2026-05-15
Slices  : 0 of 1

  MAN→YYZ  dep=2026-05-15T14:55:00  arr=2026-05-15T23:16:00  dur=PT13H21Mmin  ECONOMY-LIGHT  GBP 764.73  avail=None
  MAN→YYZ  dep=2026-05-15T13:45:00  arr=2026-05-15T19:55:00  dur=PT11H10Mmin  ECONOMY-LIGHT  GBP 743.93  avail=None
  ...
============================================================
[*] Full response saved to result.json
```

## Limitations & reproducibility notes

| Issue | Detail |
|-------|--------|
| **Endpoint stability** | `resources.virginatlantic.com/search/public/v1/GraphQL` is an internal BFF endpoint. Virgin Atlantic could add authentication or rate-limiting to it without notice. |
| **Schema changes** | The `FlightOfferRequestInput` / `Result` / `SliceFare` GraphQL types are undocumented and subject to change. |
| **Cookie TTL** | `bm_sz` and `_abck` from the homepage GET have ~2 hour TTL. Each run performs a fresh GET so this is handled automatically. |
| **Geo-blocking** | The `en-IN` locale is used. Some locales may return different fare sets or require different currency/POS parameters. |
| **`available` field** | The `SliceFare.available` field returns `null` on this endpoint (returns an `Int`, not a `Boolean`). Availability is confirmed by the presence of the fare in the response. |
| **`www` endpoint path** | Achieving HTTP 200 on `www.virginatlantic.com/flights/search/api/graphql` requires reimplementing the Akamai `bmak.js` HMAC derivation — a multi-day reverse-engineering effort documented in the analysis below. |

## Akamai analysis (reference)

This was the original target endpoint. The sensor submission flow:

```
GET  /en-IN                    → 200  (_abck set to ~-1~)
POST /YLG1O1/.../6YScB         → 201  (sensor accepted, but HMAC invalid → _abck stays ~-1~)
GET  /flights/search/slice     → 200  (navigation, not gated)
POST /flights/search/api/graphql → 429  (_abck unvalidated)
```

The `sensor_data` format is `3;0;1;{flags};{nonce};{hmac};{browser_info};{telemetry}`. The HMAC (field 6) is SHA-256 of browser fingerprint data processed through a custom key-mixing function inside `bmak.js`. The nonce comes from the `bm_sz` cookie (`{hex}~{base64}~{nonce}~{key}`, last two `~`-segments). Without the correct HMAC, `_abck` never transitions from `~-1~` to `~0~` and all XHR endpoints return 429.
