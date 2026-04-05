"""
Virgin Atlantic flight search — pure-HTTP via curl_cffi (Chrome TLS impersonation).
Default route: MAN → YYZ, 2026-05-15, 1 adult, Economy, one-way, cash fare.

Two-request flow
----------------
1. GET  www.virginatlantic.com/<locale>
        Seeds Akamai session cookies (bm_sz, AKA_A2, _abck). Must use
        document-navigation Sec-Fetch headers; TLS fingerprint is checked here.
2. POST resources.virginatlantic.com/search/public/v1/GraphQL
        Unprotected BFF endpoint serving identical data to the Akamai-gated
        www endpoint. Cookies from step 1 satisfy surface-level validation.

Akamai layers and how each is addressed
----------------------------------------
Layer 1 — TLS fingerprint (JA3/JA4): satisfied by curl_cffi chrome142 impersonation.
Layer 2 — _abck sensor_data HMAC: sidestepped — resources.* has no Akamai gating.
Layer 3 — IP reputation: sidestepped — same endpoint sidestep.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from curl_cffi.requests import AsyncSession

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SearchError(RuntimeError):
    """Base error for all flight-search failures."""


class AkamaiBlockedError(SearchError):
    """
    Raised when Akamai returns a 429 challenge response.

    Not retried — a new session and/or IP is required to recover.
    """


class SchemaError(SearchError):
    """Raised when the GraphQL response shape is unexpected."""


# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------


class AirportInfo(TypedDict, total=False):
    code: str
    cityName: str
    airportName: str


class Criteria(TypedDict, total=False):
    origin: AirportInfo
    destination: AirportInfo
    departing: str


class PriceInfo(TypedDict, total=False):
    amountIncludingTax: float | None
    amount: float | None
    currency: str


class FareInfo(TypedDict, total=False):
    id: str
    fareFamilyType: str
    # NOTE: available is always null on resources.virginatlantic.com (the field
    # is typed Int, not Boolean, and this BFF endpoint never populates it).
    # Presence of the fare in the response is the authoritative availability signal.
    available: int | None
    content: dict[str, Any] | None
    price: PriceInfo | None


class FlightInfo(TypedDict, total=False):
    origin: AirportInfo
    destination: AirportInfo
    departure: str
    arrival: str
    duration: str  # ISO 8601 e.g. "PT13H21M"


class FlightAndFare(TypedDict, total=False):
    flight: FlightInfo
    fares: list[FareInfo]


class SliceItem(TypedDict, total=False):
    id: str
    fareId: str | None
    flightsAndFares: list[FlightAndFare]


class SearchResult(TypedDict, total=False):
    slices: dict[str, Any]
    criteria: Criteria
    slice: list[SliceItem] | SliceItem


# ---------------------------------------------------------------------------
# TLS / browser identity
# ---------------------------------------------------------------------------

# Impersonation target and User-Agent MUST be kept in sync.
# Akamai correlates the JA3/JA4 fingerprint (from the TLS ClientHello) with the
# browser version declared in the User-Agent string. A mismatch is a detectable signal.
_IMPERSONATE = "chrome142"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
_SEC_CH_UA = '"Chromium";v="142", "Not-A.Brand";v="24", "Google Chrome";v="142"'

_BASE_URL = "https://www.virginatlantic.com"
_RESOURCES_GQL = "https://resources.virginatlantic.com/search/public/v1/GraphQL"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SearchConfig:
    origin: str = "MAN"
    destination: str = "YYZ"
    departure_date: str = "2026-05-15"
    # Locale used for the homepage GET and as the GraphQL referer.
    # This is the Point-of-Sale (POS) context: it affects fare currency and
    # which fare sets are returned. en-GB is correct for a UK-origin route.
    locale: str = "en-GB"
    proxy_url: str | None = None
    max_retries: int = 3
    output_file: Path = field(default_factory=lambda: Path("result.json"))
    # Akamai's edge needs ~500 ms to propagate the bm_sz session state before
    # it will accept same-session XHR requests without issuing a new challenge.
    seed_delay: float = 0.5
    # Exponential backoff base in seconds; delay doubles on each subsequent attempt.
    retry_base_delay: float = 2.0


# ---------------------------------------------------------------------------
# GraphQL query
# ---------------------------------------------------------------------------

_SEARCH_QUERY = """
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
        id
        fareId
        flightsAndFares {
          flight {
            origin { code }
            destination { code }
            departure
            arrival
            duration
          }
          fares {
            id
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
"""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _build_logger(name: str = __name__) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    return logger


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _common_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Base headers present on every request."""
    h: dict[str, str] = {
        "sec-ch-ua": _SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "user-agent": _USER_AGENT,
        "sec-gpc": "1",
    }
    if extra:
        h.update(extra)
    return h


def _build_payload(cfg: SearchConfig) -> dict[str, Any]:
    return {
        "operationName": "SearchOffers",
        "query": _SEARCH_QUERY,
        "variables": {
            "request": {
                "flightSearchRequest": {
                    "searchOriginDestinations": [
                        {
                            "origin": cfg.origin,
                            "destination": cfg.destination,
                            "departureDate": cfg.departure_date,
                        }
                    ],
                    "bundleOffer": False,
                    "awardSearch": False,
                    "calendarSearch": False,
                    "flexiDateSearch": False,
                    "nonStopOnly": False,
                    "currentTripIndexId": "0",
                    "checkInBaggageAllowance": False,
                    "carryOnBaggageAllowance": False,
                    "refundableOnly": False,
                },
                "customerDetails": [{"custId": "ADT_0", "ptc": "ADT"}],
            }
        },
    }


def _dump_error_body(body: str, label: str, logger: logging.Logger) -> Path:
    """Write the full response body to a debug file and return its path."""
    path = Path(f"error_{label}.txt")
    path.write_text(body, encoding="utf-8")
    logger.debug("Full error body written to %s", path)
    return path


def _validate_schema(data: dict[str, Any]) -> None:
    """
    Verify the response contains expected top-level GraphQL structure.

    Raises SchemaError with a truncated body excerpt to keep the message readable.
    """
    if errors := data.get("errors"):
        raise SchemaError(f"GraphQL errors: {json.dumps(errors)[:400]}")
    result = data.get("data", {}).get("searchOffers", {}).get("result")
    if not result:
        raise SchemaError(
            f"Missing data.searchOffers.result in response. "
            f"Got top-level keys: {list(data.keys())}"
        )


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------


async def _with_retry(
    coro_fn: Any,
    *,
    max_attempts: int,
    base_delay: float,
    logger: logging.Logger,
) -> Any:
    """
    Call an async coroutine function with exponential backoff.

    429 responses raise AkamaiBlockedError immediately — retrying on the same
    session will not resolve a bot-detection block.
    """
    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_fn()
        except AkamaiBlockedError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "attempt %d/%d failed (%s: %s) — retrying in %.1fs",
                    attempt,
                    max_attempts,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("All %d attempts failed.", max_attempts)

    raise SearchError(f"All {max_attempts} attempts exhausted") from last_exc


# ---------------------------------------------------------------------------
# Search steps
# ---------------------------------------------------------------------------


async def _seed_cookies(
    session: AsyncSession, cfg: SearchConfig, logger: logging.Logger
) -> None:
    """
    GET the homepage to obtain Akamai session cookies.

    Sec-Fetch headers must declare dest=document and mode=navigate so that
    the Akamai edge treats the request as a legitimate browser page load and
    issues bm_sz / AKA_A2 cookies rather than an edge challenge.
    """
    url = f"{_BASE_URL}/{cfg.locale}"
    logger.info("Step 1: GET %s (seed cookies)", url)

    resp = await session.get(
        url,
        headers=_common_headers({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-GB,en;q=0.9",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "upgrade-insecure-requests": "1",
        }),
        timeout=30,
    )

    if resp.status_code != 200:
        path = _dump_error_body(resp.text, "seed", logger)
        raise SearchError(
            f"Homepage GET returned HTTP {resp.status_code}. "
            f"Full body written to {path}."
        )

    bm_sz = session.cookies.get("bm_sz", "")
    aka_a2 = session.cookies.get("AKA_A2", "")
    logger.info(
        "  status=%d  bm_sz=%s  AKA_A2=%s",
        resp.status_code,
        "set" if bm_sz else "MISSING",
        "set" if aka_a2 else "MISSING",
    )

    if not bm_sz:
        logger.warning(
            "bm_sz was not set — Akamai may have returned a challenge page rather "
            "than the homepage. Consider a residential IP or a different locale."
        )


async def _search_flights(
    session: AsyncSession, cfg: SearchConfig, logger: logging.Logger
) -> dict[str, Any]:
    """
    POST SearchOffers to the unprotected BFF GraphQL endpoint.

    resources.virginatlantic.com and www.virginatlantic.com share the
    .virginatlantic.com registrable domain, so Sec-Fetch-Site is legitimately
    'same-site' from the browser's perspective — this is not spoofed.

    Raises
    ------
    AkamaiBlockedError
        If the BFF endpoint starts returning Akamai 429 challenges.
    SchemaError
        If the 200 response body is missing expected GraphQL structure.
    SearchError
        On any non-200, non-429 failure after all retries are exhausted.
    """
    payload = _build_payload(cfg)
    logger.info(
        "Step 2: POST SearchOffers — %s→%s on %s (retries=%d)",
        cfg.origin,
        cfg.destination,
        cfg.departure_date,
        cfg.max_retries,
    )

    async def _attempt() -> Any:
        resp = await session.post(
            _RESOURCES_GQL,
            json=payload,
            headers=_common_headers({
                "accept": "application/json",
                "accept-language": "en-GB,en;q=0.9",
                "content-type": "application/json",
                "origin": _BASE_URL,
                "referer": f"{_BASE_URL}/{cfg.locale}/",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
            }),
            timeout=60,
        )
        if resp.status_code == 429:
            path = _dump_error_body(resp.text, "429", logger)
            raise AkamaiBlockedError(
                f"Akamai 429 — bot detection triggered on the BFF endpoint. "
                f"A new session and/or residential IP is required. "
                f"Full body written to {path}."
            )
        if resp.status_code != 200:
            path = _dump_error_body(resp.text, f"http{resp.status_code}", logger)
            raise SearchError(
                f"Unexpected HTTP {resp.status_code}. Full body written to {path}."
            )
        return resp

    resp = await _with_retry(
        _attempt,
        max_attempts=cfg.max_retries,
        base_delay=cfg.retry_base_delay,
        logger=logger,
    )

    logger.info("  status=%d  bytes=%d", resp.status_code, len(resp.content))

    data: dict[str, Any] = resp.json()
    _validate_schema(data)
    return data


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------


def _render_result(
    data: dict[str, Any], cfg: SearchConfig, logger: logging.Logger
) -> None:
    result: SearchResult = data["data"]["searchOffers"]["result"]
    criteria: Criteria = result.get("criteria", {})
    slices_meta = result.get("slices", {})
    raw_slices = result.get("slice", [])

    # Normalize: the API returns a dict for a single-slice result, list otherwise.
    flight_list: list[SliceItem] = (
        [raw_slices] if isinstance(raw_slices, dict) else raw_slices
    )

    origin_info: AirportInfo = criteria.get("origin", {})
    dest_info: AirportInfo = criteria.get("destination", {})

    print()
    print("=" * 64)
    print("SUCCESS — Virgin Atlantic SearchOffers")
    print("=" * 64)
    print(
        f"Route   : {origin_info.get('code', '?')} ({origin_info.get('cityName', '?')}) "
        f"→ {dest_info.get('code', '?')} ({dest_info.get('cityName', '?')})"
    )
    print(f"Date    : {criteria.get('departing', cfg.departure_date)}")
    print(f"Slices  : {slices_meta.get('current')} / {slices_meta.get('total')}")
    print(
        "Note    : 'available' field is always null on this endpoint — "
        "fare presence in the response confirms bookability."
    )
    print()

    displayed = 0
    for slice_item in flight_list:
        for ff in slice_item.get("flightsAndFares", []):
            flight: FlightInfo = ff.get("flight", {})
            fares: list[FareInfo] = ff.get("fares", [])
            if not fares:
                continue

            # Use the first fare that has a valid price; skip unpriceable fares
            fare = next(
                (f for f in fares if (f.get("price") or {}).get("amountIncludingTax") is not None
                 or (f.get("price") or {}).get("amount") is not None),
                fares[0],
            )
            price: PriceInfo = fare.get("price") or {}
            amt = price.get("amountIncludingTax") or price.get("amount")
            cur = price.get("currency", "?")
            cabin = (fare.get("content") or {}).get("cabinName") or fare.get(
                "fareFamilyType", "?"
            )
            if amt is None:
                continue  # No priceable fare for this flight; skip
            amt_str = (
                f"{cur} {amt:.2f}" if isinstance(amt, (int, float)) else f"{cur} {amt}"
            )

            orig_code = flight.get("origin", {}).get("code", "?")
            dest_code = flight.get("destination", {}).get("code", "?")
            dep = flight.get("departure", "?")
            arr = flight.get("arrival", "?")
            dur = flight.get("duration", "?")

            print(
                f"  {orig_code}→{dest_code}  dep={dep}  arr={arr}  "
                f"dur={dur}  {cabin}  {amt_str}"
            )
            displayed += 1
            if displayed >= 10:
                break
        if displayed >= 10:
            break

    print("=" * 64)

    cfg.output_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Full response saved to %s", cfg.output_file)


# ---------------------------------------------------------------------------
# Async entry point
# ---------------------------------------------------------------------------


async def run(cfg: SearchConfig) -> None:
    logger = _build_logger()

    session_kwargs: dict[str, Any] = {"impersonate": _IMPERSONATE}
    if cfg.proxy_url:
        # Redact credentials from log output (format: scheme://user:pass@host:port)
        redacted = cfg.proxy_url.split("@")[-1] if "@" in cfg.proxy_url else cfg.proxy_url
        logger.info("Using proxy: ...@%s", redacted)
        session_kwargs["proxies"] = {"http": cfg.proxy_url, "https": cfg.proxy_url}

    async with AsyncSession(**session_kwargs) as session:
        await _seed_cookies(session, cfg, logger)
        await asyncio.sleep(cfg.seed_delay)
        data = await _search_flights(session, cfg, logger)

    _render_result(data, cfg, logger)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> SearchConfig:
    parser = argparse.ArgumentParser(
        description="Virgin Atlantic flight search — pure HTTP",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--origin", default="MAN", metavar="IATA", help="Origin airport IATA code"
    )
    parser.add_argument(
        "--destination", default="YYZ", metavar="IATA", help="Destination airport IATA code"
    )
    parser.add_argument(
        "--date", default="2026-05-15", metavar="YYYY-MM-DD", help="Departure date"
    )
    parser.add_argument(
        "--locale",
        default="en-GB",
        help="Homepage locale / Point-of-Sale context (affects fare currency)",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help="HTTP(S) proxy URL e.g. http://user:pass@host:port",
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="Max retry attempts for the search POST"
    )
    parser.add_argument(
        "--output", default="result.json", help="Path to write the full JSON response"
    )
    args = parser.parse_args()

    return SearchConfig(
        origin=args.origin,
        destination=args.destination,
        departure_date=args.date,
        locale=args.locale,
        proxy_url=args.proxy,
        max_retries=args.retries,
        output_file=Path(args.output),
    )


def main() -> None:
    cfg = _parse_args()
    try:
        asyncio.run(run(cfg))
    except AkamaiBlockedError as exc:
        logging.getLogger(__name__).error("Bot detection triggered: %s", exc)
        sys.exit(2)
    except SchemaError as exc:
        logging.getLogger(__name__).error("Unexpected response schema: %s", exc)
        sys.exit(3)
    except SearchError as exc:
        logging.getLogger(__name__).error("Search failed: %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
