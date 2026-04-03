"""
Virgin Atlantic flight search — pure HTTP via curl_cffi (Chrome TLS impersonation).
Route: MAN → YYZ, 2026-05-15, 1 adult, Economy, one-way, cash fare.

Uses resources.virginatlantic.com/search/public/v1/GraphQL which serves the same
flight data as the Akamai-protected www.virginatlantic.com/flights/search/api/graphql
but without bot-protection gating. A homepage GET seeds session cookies first.
"""

import argparse
import json
import sys
import time

from curl_cffi.requests import Session

# ---------------------------------------------------------------------------
# Search parameters
# ---------------------------------------------------------------------------
ORIGIN = "MAN"
DESTINATION = "YYZ"
DEPARTURE_DATE = "2026-05-15"
BASE_URL = "https://www.virginatlantic.com"
RESOURCES_GQL = "https://resources.virginatlantic.com/search/public/v1/GraphQL"
LOCALE = "en-IN"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

SEC_CH_UA = '"Chromium";v="146", "Not-A.Brand";v="24", "Brave";v="146"'

# ---------------------------------------------------------------------------
# GraphQL query — matches resources.virginatlantic.com schema
# (different from www.virginatlantic.com/flights/search/api/graphql schema)
# ---------------------------------------------------------------------------
SEARCH_QUERY = """
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


def log(msg: str) -> None:
    print(f"[*] {msg}", flush=True)


def make_session(proxy_url: str | None = None) -> Session:
    kwargs = {"impersonate": "chrome142"}
    if proxy_url:
        kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        log(f"  Using proxy: {proxy_url[:40]}...")
    return Session(**kwargs)


def common_headers(extra: dict | None = None) -> dict:
    h = {
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "user-agent": USER_AGENT,
        "sec-gpc": "1",
    }
    if extra:
        h.update(extra)
    return h


def step1_seed_cookies(s: Session) -> None:
    """GET homepage to seed session cookies (bm_sz, AKA_A2, etc.)."""
    log(f"Step 1: GET {BASE_URL}/{LOCALE} (seed cookies)")
    r = s.get(
        f"{BASE_URL}/{LOCALE}",
        headers=common_headers({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-GB,en;q=0.6",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "upgrade-insecure-requests": "1",
        }),
        timeout=30,
    )
    bm_sz = s.cookies.get("bm_sz", "")
    log(f"  status={r.status_code}  bm_sz={'set' if bm_sz else 'MISSING'}")


def step2_search_flights(s: Session) -> dict:
    """POST GraphQL SearchOffers to resources.virginatlantic.com (no Akamai)."""
    log(f"Step 2: POST SearchOffers — {ORIGIN}→{DESTINATION} on {DEPARTURE_DATE}")

    payload = {
        "operationName": "SearchOffers",
        "query": SEARCH_QUERY,
        "variables": {
            "request": {
                "flightSearchRequest": {
                    "searchOriginDestinations": [
                        {
                            "origin": ORIGIN,
                            "destination": DESTINATION,
                            "departureDate": DEPARTURE_DATE,
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

    r = s.post(
        RESOURCES_GQL,
        json=payload,
        headers=common_headers({
            "accept": "application/json",
            "accept-language": "en-GB,en;q=0.6",
            "content-type": "application/json",
            "origin": BASE_URL,
            "referer": f"{BASE_URL}/{LOCALE}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }),
        timeout=60,
    )
    log(f"  status={r.status_code}")

    if r.status_code != 200:
        log(f"  Error body: {r.text[:300]}")
        sys.exit(1)

    return r.json()


def print_result(data: dict) -> None:
    result = data.get("data", {}).get("searchOffers", {}).get("result", {})
    if not result:
        errors = data.get("errors", [])
        if errors:
            print(f"\n[!] GraphQL errors: {json.dumps(errors)[:400]}")
        else:
            print(f"\n[!] No result in response:\n{json.dumps(data, indent=2)[:600]}")
        return

    criteria = result.get("criteria", {})
    slices_meta = result.get("slices", {})
    flight_list = result.get("slice", [])

    # flight_list may be a dict (single slice) or list
    if isinstance(flight_list, dict):
        flight_list = [flight_list]

    print("\n" + "=" * 60)
    print("SUCCESS — Virgin Atlantic SearchOffers")
    print("=" * 60)
    print(f"Route   : {criteria.get('origin', {}).get('code')} "
          f"({criteria.get('origin', {}).get('cityName', '?')}) → "
          f"{criteria.get('destination', {}).get('code')} "
          f"({criteria.get('destination', {}).get('cityName', '?')})")
    print(f"Date    : {criteria.get('departing')}")
    print(f"Slices  : {slices_meta.get('current')} of {slices_meta.get('total')}")
    print()

    displayed = 0
    for item in flight_list:
        for ff in item.get("flightsAndFares", []):
            fl = ff.get("flight", {})
            orig = fl.get("origin", {}).get("code", "?")
            dest = fl.get("destination", {}).get("code", "?")
            dep = fl.get("departure", "?")
            arr = fl.get("arrival", "?")
            dur = fl.get("duration", "?")
            fares = ff.get("fares", [])
            if fares:
                p = fares[0].get("price") or {}
                amt = p.get("amountIncludingTax", p.get("amount", "?"))
                cur = p.get("currency", "?")
                cabin = (fares[0].get("content") or {}).get("cabinName", fares[0].get("fareFamilyType", "?"))
                avail = fares[0].get("available", "?")
                print(f"  {orig}→{dest}  dep={dep}  arr={arr}  "
                      f"dur={dur}min  {cabin}  {cur} {amt}  avail={avail}")
                displayed += 1
                if displayed >= 10:
                    break
        if displayed >= 10:
            break

    print("=" * 60)

    with open("result.json", "w") as f:
        json.dump(data, f, indent=2)
    log("Full response saved to result.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Virgin Atlantic flight search")
    parser.add_argument("--proxy", type=str, default=None,
                        help="HTTP(S) proxy URL e.g. http://user:pass@host:port")
    parser.add_argument("--origin", type=str, default=ORIGIN)
    parser.add_argument("--destination", type=str, default=DESTINATION)
    parser.add_argument("--date", type=str, default=DEPARTURE_DATE)
    args = parser.parse_args()

    proxy_url = args.proxy

    s = make_session(proxy_url)

    step1_seed_cookies(s)
    time.sleep(0.5)

    data = step2_search_flights(s)
    print_result(data)


if __name__ == "__main__":
    main()
