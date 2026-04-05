# Bypassing Akamai Bot Manager with Two HTTP Requests — A Python Case Study

*How endpoint reconnaissance beats sensor data reverse-engineering every time.*

---

There is a class of engineering problem where the obvious approach is also the hardest one — and the correct approach is to stop looking at the wall in front of you and find the door around it.

This is a case study of that exact problem: performing a live retail flight search on Virgin Atlantic's website using only pure HTTP requests, no browser automation, and no JavaScript execution, against a site protected by Akamai Bot Manager Premier — one of the most sophisticated commercial bot-mitigation platforms in production today.

The solution is two HTTP requests and 260 lines of Python.

---

## The Brief

The task was straightforward on paper:

- Write a Python script that searches for cash (retail) flights on Virgin Atlantic
- No Selenium, no Playwright, no headless Chrome, no anti-detect browsers
- Allowed: any Python HTTP library, proxies, custom headers, manual DevTools inspection
- Proof of success: an HTTP 200 response whose body contains real itinerary and fare data

The task came with a specific warning: receiving a `429 {"cpr_chlge":"true"}` response means Akamai bot mitigation is active, and that does not count as a solved problem.

---

## First Contact: What Happens When You Just Try It

The most direct path is to open Chrome DevTools, perform a flight search, copy the search request as cURL, and replay it in Python.

That request goes to:

```
POST https://www.virginatlantic.com/flights/search/api/graphql
```

Running the copied cURL naively from Python — using `requests` or `httpx` with the same headers, cookies, and body — returns an immediate `429`:

```json
{"cpr_chlge": "true", "chlge_typ": "xhr_chlge"}
```

That is Akamai's challenge envelope. The response says: I know you are not a browser. The search never ran.

This is the wall.

---

## Understanding the Wall: Akamai Bot Manager Premier

To solve a protection system you need to understand what it actually checks. Akamai Bot Manager Premier operates across three distinct, independent layers:

### Layer 1 — TLS Fingerprinting (JA3/JA4)

Every TLS connection has a fingerprint derived from the ClientHello message: the cipher suites offered, the TLS extensions included, their order, the elliptic curves advertised, and the signature algorithms listed. These are combined into a hash called a JA3 fingerprint (and the newer JA4).

Chrome 142 produces a specific, known JA3/JA4 fingerprint. Python's `requests` library — which uses the system's OpenSSL — produces a completely different one. Akamai sees the mismatch: the headers say Chrome, the TLS fingerprint says Python.

The fix for Layer 1 is a library called `curl_cffi`. It provides Python bindings to `libcurl` with the ability to impersonate specific browser TLS profiles, including Chrome 142. When you use `impersonate="chrome142"`, the library reproduces Chrome's exact ClientHello, including the correct HTTP/2 SETTINGS frames and ALPN negotiation. Layer 1 is solved.

### Layer 2 — `_abck` Cookie Validation and sensor_data HMAC

This is the hard layer. When you GET any Virgin Atlantic page, Akamai sets a cookie called `_abck`. Initially it contains `~-1~` — the unvalidated state. To validate it, the browser must POST to Akamai's sensor collection endpoint with a `sensor_data` payload.

The `sensor_data` field looks like:

```
3;0;1;{flags};{nonce};{hmac};{browser_info};{telemetry}
```

The HMAC in field 6 is a SHA-256 signature computed by `bmak.js` — Akamai's obfuscated, self-modifying JavaScript that runs in the browser. The inputs to the HMAC include canvas fingerprint data, audio context fingerprints, screen metrics, a session nonce extracted from the `bm_sz` cookie, and a device key. The nonce itself is encoded in the `bm_sz` cookie as `{hex}~{base64}~{nonce}~{key}` — the last two tilde-delimited segments.

Without a valid HMAC, `_abck` never transitions to its validated state. Any XHR request to the search endpoint while `_abck` is in the `~-1~` state returns `429`.

Correctly reimplementing this in Python is a multi-day reverse-engineering effort involving de-obfuscating `bmak.js`, implementing the key-mixing function, and reproducing the browser telemetry collection. This is not a weekend task.

### Layer 3 — IP Reputation Scoring

Akamai scores every IP address independently, drawing on aggregated traffic intelligence. Datacenter IP ranges, cloud provider CIDR blocks, and known VPN exit nodes all receive lower trust scores by default. A request with a perfect TLS fingerprint from a GCP or AWS IP is still suspicious.

---

## The Recon Phase: Finding the Door

The correct response to a three-layer defence is not to attack all three layers simultaneously. It is to ask: **is there another surface that exposes the same data with fewer defences?**

The methodology here is systematic HAR analysis. While performing a real flight search in Chrome with the Network tab open, you capture every single request the frontend makes — not just the obvious search call. You look at the full domain map.

A flight search on Virgin Atlantic's site generates traffic to multiple subdomains. Most calls go to `www.virginatlantic.com`. But one POST stands out:

```
POST https://resources.virginatlantic.com/search/public/v1/GraphQL
```

This request:
- Uses the same `SearchOffers` GraphQL operation
- Sends nearly identical variables to the `www` endpoint
- Returns a structurally identical response with real flight itineraries and fares
- Is called from the same browser session

Critically: when you replay this request with only session cookies from a homepage GET and no validated `_abck` — it returns `200`.

No sensor data. No HMAC. No Layer 2. No Layer 3.

This is the BFF (Backend-for-Frontend) endpoint. It is an internal service endpoint that the frontend uses for auxiliary queries — things like sales banner content and availability checks — and it was deployed without Akamai Bot Manager gating. It serves the same flight data as the protected endpoint.

The door was already open.

---

## Why the CORS Headers Work

One detail here matters and is often misunderstood.

When a browser makes a cross-origin request, it includes a `Sec-Fetch-Site` header with one of four values: `same-origin`, `same-site`, `cross-site`, or `none`. Bots spoofing this header with `same-site` on a request to a different domain look obviously forged.

But `www.virginatlantic.com` and `resources.virginatlantic.com` share the same **registrable domain**: `.virginatlantic.com`. Under the browser's "same-site" definition (not to be confused with "same-origin"), two URLs are same-site if they share a registrable domain. This means a real browser legitimately emits:

```
Sec-Fetch-Site: same-site
```

on a request from `www.virginatlantic.com` to `resources.virginatlantic.com`. We are not spoofing this header. It is genuinely correct. The request looks exactly like what a real frontend session would produce.

---

## The Solution: Two Requests

The complete flow is:

```
Step 1: GET https://www.virginatlantic.com/en-GB
        Headers: document-navigation Sec-Fetch-* set
        Purpose: Akamai edge issues bm_sz, AKA_A2, _abck cookies
        TLS:     curl_cffi chrome142 impersonation (satisfies Layer 1)

        ↓ wait ~500ms for Akamai edge to propagate session state

Step 2: POST https://resources.virginatlantic.com/search/public/v1/GraphQL
        Body:    SearchOffers GraphQL query with hardcoded parameters
        Headers: XHR/CORS Sec-Fetch-* set, same-site origin/referer
        Cookies: automatically forwarded from Step 1 session
        Result:  HTTP 200 with real itinerary and fare data
```

That is the entire bypass. Two requests, one `curl_cffi` session, zero sensor data, zero JavaScript.

The 500ms pause between the two requests is empirical: Akamai's edge CDN needs time to propagate the `bm_sz` session state internally before it accepts same-session XHR requests on adjacent subdomains without issuing a new challenge.

---

## The GraphQL Query

The `SearchOffers` operation against the BFF endpoint takes a `FlightOfferRequestInput` that mirrors the public schema:

```graphql
query SearchOffers($request: FlightOfferRequestInput!) {
  searchOffers(request: $request) {
    result {
      slices { current total }
      criteria {
        origin { code cityName airportName }
        destination { code cityName airportName }
        departing
      }
      slice {
        flightsAndFares {
          flight {
            origin { code }
            destination { code }
            departure
            arrival
            duration
          }
          fares {
            fareFamilyType
            available
            content { cabinName }
            price { amountIncludingTax currency }
          }
        }
      }
    }
  }
}
```

Key variables:

```json
{
  "flightSearchRequest": {
    "searchOriginDestinations": [
      {"origin": "MAN", "destination": "YYZ", "departureDate": "2026-05-15"}
    ],
    "awardSearch": false,
    "bundleOffer": false,
    "calendarSearch": false
  },
  "customerDetails": [{"custId": "ADT_0", "ptc": "ADT"}]
}
```

The `awardSearch: false` flag is what keeps this in retail/cash territory. The `ptc: "ADT"` is the Passenger Type Code for one adult.

One undocumented quirk: the `available` field on fares always returns `null` on this endpoint. The field is typed as `Int` (not `Boolean`) and is never populated by this BFF. Fare presence in the response is the correct availability signal — if the fare is there, it can be booked.

---

## Code Architecture: Making It Production-Ready

The initial proof-of-concept worked, but had structural problems that would be unacceptable in production or a professional context. Here is what was wrong and how each was fixed:

### 1. No Exception Hierarchy — `sys.exit()` in Library Functions

**Before:**
```python
def step2_search_flights(s: Session) -> dict:
    ...
    if r.status_code != 200:
        log(f"  Error body: {r.text[:300]}")
        sys.exit(1)
```

`sys.exit()` inside a library function makes code untestable and un-importable for any caller that wants to handle errors gracefully. The fix is a proper exception hierarchy:

```python
class SearchError(RuntimeError):
    """Base error for all flight-search failures."""

class AkamaiBlockedError(SearchError):
    """429 challenge — not retried, requires new session/IP."""

class SchemaError(SearchError):
    """GraphQL response shape is unexpected."""
```

`sys.exit()` now only appears in `main()`, with specific exit codes per exception type.

### 2. No Retry Logic

A single timeout or transient network error killed the entire run. The fix is an async retry wrapper with exponential backoff:

```python
async def _with_retry(coro_fn, *, max_attempts, base_delay, logger):
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_fn()
        except AkamaiBlockedError:
            raise  # Never retry a bot block — same session won't recover
        except Exception as exc:
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning("attempt %d/%d failed — retrying in %.1fs", ...)
                await asyncio.sleep(delay)
    raise SearchError("All attempts exhausted") from last_exc
```

The important design decision here: `AkamaiBlockedError` is re-raised immediately and never retried. Retrying a bot block on the same session is pointless — Akamai has already scored the session as suspicious. A new IP or session is required.

### 3. TLS/UA Version Mismatch

The original code used `impersonate="chrome142"` (TLS fingerprint = Chrome 142) but set `User-Agent: Chrome/146.0.0.0`. Akamai can correlate the JA3/JA4 fingerprint against the declared browser version. A TLS handshake that looks like Chrome 142 but a UA string claiming Chrome 146 is a detectable inconsistency. Aligned both to `142`.

### 4. Locale Hardcoded to `en-IN`

The route is Manchester → Toronto. The locale used for the homepage GET sets the Point-of-Sale context, which affects which fare sets are returned and what currency prices are quoted in. Using `en-IN` (India) returns GBP prices for a UK-origin route — technically functional but semantically wrong. Changed to `en-GB` as default, parameterized via `--locale`.

### 5. No `logging` Module

`print(f"[*] {msg}")` was used throughout. The fix is the standard `logging` module with structured formatting:

```python
def _build_logger(name: str = __name__) -> logging.Logger:
    logger = logging.getLogger(name)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return logger
```

### 6. Error Body Truncated to 300 Characters

When debugging bot-detection blocks or schema changes, you need the full response body. The fix writes the complete body to a debug file on any error:

```python
def _dump_error_body(body: str, label: str, logger) -> Path:
    path = Path(f"error_{label}.txt")
    path.write_text(body, encoding="utf-8")
    logger.debug("Full error body written to %s", path)
    return path
```

### 7. Module-Level Constants Used as Logic State

The original code had `ORIGIN = "MAN"` at module level, and argparse defaults that could override them — but if someone imported the module, the constants governed the behavior, not the args. The fix is a `SearchConfig` dataclass that owns all parameters, constructed from args in `_parse_args()`, and passed explicitly through every function:

```python
@dataclass
class SearchConfig:
    origin: str = "MAN"
    destination: str = "YYZ"
    departure_date: str = "2026-05-15"
    locale: str = "en-GB"
    proxy_url: str | None = None
    max_retries: int = 3
    output_file: Path = field(default_factory=lambda: Path("result.json"))
    seed_delay: float = 0.5
    retry_base_delay: float = 2.0
```

### 8. Synchronous Requests in 2025

For an HTTP scraping tool in 2025, the idiomatic pattern is `asyncio` + `AsyncSession`. The refactored code uses `curl_cffi.requests.AsyncSession` as an async context manager:

```python
async def run(cfg: SearchConfig) -> None:
    async with AsyncSession(impersonate=_IMPERSONATE) as session:
        await _seed_cookies(session, cfg, logger)
        await asyncio.sleep(cfg.seed_delay)
        data = await _search_flights(session, cfg, logger)
    _render_result(data, cfg, logger)
```

### 9. Proxy Credentials Logged in Plaintext

The original code logged the full proxy URL including any embedded credentials. Fixed with a simple redact:

```python
redacted = cfg.proxy_url.split("@")[-1] if "@" in cfg.proxy_url else cfg.proxy_url
logger.info("Using proxy: ...@%s", redacted)
```

### 10. TypedDicts for the Response Shape

Raw `dict.get()` chains are fine for a PoC but make code opaque to readers and tools. The refactored code defines the response shape explicitly:

```python
class FareInfo(TypedDict, total=False):
    id: str
    fareFamilyType: str
    available: int | None   # always null on this endpoint — see comment
    content: dict[str, Any] | None
    price: PriceInfo | None
```

---

## The Output

Running `python main.py` produces:

```
[INFO] Step 1: GET https://www.virginatlantic.com/en-GB (seed cookies)
[INFO]   status=200  bm_sz=set  AKA_A2=set
[INFO] Step 2: POST SearchOffers — MAN→YYZ on 2026-05-15 (retries=3)
[INFO]   status=200  bytes=54231

================================================================
SUCCESS — Virgin Atlantic SearchOffers
================================================================
Route   : MAN (Manchester) → YYZ (Toronto)
Date    : 2026-05-15
Slices  : 0 / 1
Note    : 'available' is always null on this endpoint — fare presence confirms bookability.

  MAN→YYZ  dep=2026-05-15T14:55:00  arr=2026-05-15T23:16:00  dur=PT13H21M  ECONOMY-LIGHT  GBP 764.73
  MAN→YYZ  dep=2026-05-15T13:45:00  arr=2026-05-15T19:55:00  dur=PT11H10M  ECONOMY-LIGHT  GBP 743.93
  MAN→YYZ  dep=2026-05-15T09:00:00  arr=2026-05-15T16:44:00  dur=PT12H44M  ECONOMY-LIGHT  GBP 781.23
  ...
================================================================
[INFO] Full response saved to result.json
```

Real prices, real departure times, real cabin names. The search ran.

---

## Limitations and Honest Notes

This solution works today. Whether it works in six months depends entirely on Virgin Atlantic's infrastructure decisions. The specific risks:

**Endpoint stability.** `resources.virginatlantic.com/search/public/v1/GraphQL` is an internal BFF endpoint with no public documentation or stability guarantee. Virgin Atlantic could add Akamai gating, an API key requirement, or IP-based allowlisting to it without any public announcement. If that happens, the fallback is the hard path: implementing the `bmak.js` HMAC derivation in Python.

**IP reputation.** The solution was developed and tested from a WSL2 environment — a Microsoft datacenter IP. Akamai scored it at a threshold that still allowed the BFF endpoint to respond. Running from AWS, GCP, or other cloud IPs may trigger more aggressive scoring. Residential IPs are consistently more reliable.

**Schema volatility.** The GraphQL types (`FlightOfferRequestInput`, `SliceFare`, etc.) are undocumented. A schema migration on Virgin Atlantic's backend could silently break response parsing.

**`available` field.** As mentioned: always null. If you need actual seat availability data rather than just fare prices, this endpoint does not provide it in its current form.

---

## Key Takeaways

**1. Recon before code.** The most valuable hour in this project was spent in the Network tab with the HAR open — not writing code. Identifying the unprotected BFF endpoint eliminated layers 2 and 3 of the Akamai stack entirely.

**2. Fight the battle you can win.** Reimplementing `bmak.js` HMAC derivation is technically possible. It is also weeks of work that becomes obsolete every time Akamai rotates the obfuscation. Routing around it via endpoint discovery costs one afternoon.

**3. Fingerprint consistency matters.** TLS impersonation with `curl_cffi` is not magic — it only works if your declared browser version (User-Agent, `sec-ch-ua`) matches the TLS fingerprint profile you are impersonating. Chrome 142 TLS + Chrome 146 UA is a detectable inconsistency. Consistency at every layer is what makes impersonation credible.

**4. BFF endpoints are a recurring attack surface.** Modern SPAs commonly talk to multiple backend services, and internal BFF endpoints frequently lack the same hardening as the primary CDN-fronted entrypoints. This is not unique to Virgin Atlantic — it is an architectural pattern worth looking for on any well-protected site.

**5. Code that only works is not production-ready.** The final version added exception hierarchy, retry logic, proper logging, async execution, TypedDicts, and structured error handling. None of those changed what the code does. All of them change how confidently you can run it at scale.

---

## Stack Summary

| Component | Choice | Why |
|-----------|--------|-----|
| HTTP client | `curl_cffi 0.14` | TLS impersonation (JA3/JA4 matching) |
| Impersonation profile | `chrome142` | Matches User-Agent and sec-ch-ua headers |
| Execution model | `asyncio` + `AsyncSession` | Idiomatic for async HTTP in Python 3.12 |
| Config management | `dataclass` | Eliminates module-level state, clean CLI integration |
| Error handling | Custom exception hierarchy | Enables caller-specific recovery logic |
| Dependencies | One (`curl_cffi`) | Minimal surface area, reproducible via `uv.lock` |

---

*The full source code, README, and example output are available in the repository linked below.*
