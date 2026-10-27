import json
import logging
import random
import re
import time
from typing import List, Optional, Tuple, Dict, Any

from primp import Client
from selectolax.lexbor import LexborHTMLParser

from fast_flights import FlightQuery, Passengers, create_filter
from fast_flights.integrations.base import FetchIntegration
from fast_flights.model import SingleFlight, Airport, SimpleDatetime
from fast_flights.querying import Query
from models import FlightOption, FlightSegment

logger = logging.getLogger("flight_fetcher")


class ScraperBlockedException(Exception):
    """Raised when Google blocks requests (429 or CAPTCHA detected)."""
    pass


class PrimpConsentFetchIntegration(FetchIntegration):
    """
    FetchIntegration that handles Google's EU consent requirement without a browser.
    Injects consent cookie (SOCS) and realistic headers via primp (Rust TLS impersonation).
    """
    def __init__(self, proxy: Optional[str] = None, timeout: int = 30):
        self.proxy = proxy
        self.timeout = timeout
        self.client = Client(
            impersonate="chrome_145",
            impersonate_os="macos",
            referer=True,
            cookie_store=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": "SOCS=CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg",
            }
        )

    def fetch_html(self, q: Query | str) -> str:
        url = "https://www.google.com/travel/flights"
        params = q.params() if isinstance(q, Query) else {"q": q}
        res = self.client.get(url, params=params)
        
        # Check for CAPTCHA / Block
        if res.status_code == 429 or "sorry/index" in res.url or "unusual traffic" in res.text.lower():
            raise ScraperBlockedException(f"Google rate-limit/CAPTCHA encountered (HTTP {res.status_code}, url: {res.url})")
            
        return res.text


def _parse_time(value: Optional[List[Optional[int]]]) -> Tuple[int, int]:
    padded = [*(value or []), None, None]
    return (padded[0] or 0, padded[1] or 0)


def parse_flights_html(html: str) -> List[FlightOption]:
    """
    Robust parser that handles Google Flights payload and gracefully returns
    an empty list if no flights are scheduled/found, avoiding fast-flights parser crashes.
    """
    parser = LexborHTMLParser(html)
    script = parser.css_first(r"script.ds\:1")
    if not script or not script.text():
        return []

    js_text = script.text()
    if "data:" not in js_text:
        return []

    try:
        data_str = js_text.split("data:", 1)[1].rsplit(",", 1)[0]
        if "errorHasStatus: true" in data_str:
            return []
        payload = json.loads(data_str)
    except Exception as e:
        logger.debug(f"JSON parsing error in payload: {e}")
        return []

    # Check if payload[3] and flights exist
    if not isinstance(payload, list) or len(payload) <= 3 or payload[3] is None:
        return []

    flights_data = payload[3]
    if not isinstance(flights_data, list) or len(flights_data) == 0 or flights_data[0] is None:
        return []

    flight_options: List[FlightOption] = []

    for item in flights_data[0]:
        try:
            flight_info = item[0]
            price_raw = item[1][0][1] if (len(item) > 1 and item[1] and item[1][0]) else None
            if price_raw is None:
                continue

            # Price to numeric int
            try:
                price = int(price_raw)
            except (ValueError, TypeError):
                continue

            airlines = flight_info[1] if len(flight_info) > 1 else []
            segments_raw = flight_info[2] if len(flight_info) > 2 else []

            segments: List[FlightSegment] = []
            layover_airports: List[str] = []
            layover_names: List[str] = []

            for seg in segments_raw:
                from_code = seg[3] or ""
                from_name = seg[4] or ""
                to_code = seg[6] or ""
                to_name = seg[5] or ""
                dep_time = _parse_time(seg[8])
                dep_date = seg[20] if len(seg) > 20 and seg[20] else [1970, 1, 1]
                arr_time = _parse_time(seg[10])
                arr_date = seg[21] if len(seg) > 21 and seg[21] else [1970, 1, 1]
                duration = seg[11] if len(seg) > 11 and seg[11] is not None else 0
                plane = seg[17] or ""

                dep_date_str = f"{dep_date[0]:04d}-{dep_date[1]:02d}-{dep_date[2]:02d}"
                dep_time_str = f"{dep_time[0]:02d}:{dep_time[1]:02d}"
                arr_date_str = f"{arr_date[0]:04d}-{arr_date[1]:02d}-{arr_date[2]:02d}"
                arr_time_str = f"{arr_time[0]:02d}:{arr_time[1]:02d}"

                segments.append(
                    FlightSegment(
                        from_airport=from_code,
                        from_name=from_name,
                        to_airport=to_code,
                        to_name=to_name,
                        departure_date=dep_date_str,
                        departure_time=dep_time_str,
                        arrival_date=arr_date_str,
                        arrival_time=arr_time_str,
                        duration_minutes=duration,
                        plane_type=plane,
                    )
                )

            stops = max(0, len(segments) - 1)
            for seg in segments[:-1]:
                layover_airports.append(seg.to_airport)
                layover_names.append(seg.to_name)

            first_seg = segments[0]
            last_seg = segments[-1]
            total_duration = sum(s.duration_minutes for s in segments)

            option = FlightOption(
                price=price,
                airlines=airlines,
                stops=stops,
                layover_airports=layover_airports,
                layover_names=layover_names,
                departure_date=first_seg.departure_date,
                departure_time=first_seg.departure_time,
                arrival_date=last_seg.arrival_date,
                arrival_time=last_seg.arrival_time,
                total_flight_minutes=total_duration,
                segments=segments,
                layover_unverified=False,
            )
            flight_options.append(option)
        except Exception as e:
            logger.debug(f"Error parsing single flight option: {e}")
            continue

    return flight_options


class FlightFetcher:
    """
    Coordinates fetching, rate-limiting, error handling, caching, and filtering.
    """
    def __init__(self, config: Dict[str, Any], cache=None, max_requests: Optional[int] = None):
        self.config = config
        self.cache = cache
        self.max_requests = max_requests
        self.requests_made = 0
        self.integration = PrimpConsentFetchIntegration()

        politeness = config.get("politeness", {})
        self.min_delay = politeness.get("min_delay_seconds", 3.0)
        self.max_delay = politeness.get("max_delay_seconds", 8.0)
        self.max_retries = politeness.get("max_retries", 3)
        self.backoff_factor = politeness.get("backoff_factor", 2.0)

        # Filters
        f_conf = config.get("filters", {})
        self.blocked_carriers = [c.lower() for c in f_conf.get("blocked_carriers", [])]
        if f_conf.get("block_turkish_airlines", False):
            self.blocked_carriers.append("turkish airlines")

        self.blocked_layovers = [a.upper() for a in f_conf.get("blocked_layover_airports", [])]
        if f_conf.get("block_istanbul_layover", False):
            self.blocked_layovers.append("IST")

        self.max_stops_long_haul = f_conf.get("max_stops_long_haul", 1)
        self.max_stops_short_haul = f_conf.get("max_stops_short_haul", 2)
        self.max_price_per_leg = f_conf.get("max_price_per_leg", 1500)

    def is_flight_valid(self, flight: FlightOption, is_long_haul: bool) -> Tuple[bool, str]:
        # 1. Price cap
        if self.max_price_per_leg and flight.price > self.max_price_per_leg:
            return False, f"Price €{flight.price} exceeds max cap €{self.max_price_per_leg}"

        # 2. Max stops
        max_allowed_stops = self.max_stops_long_haul if is_long_haul else self.max_stops_short_haul
        if flight.stops > max_allowed_stops:
            return False, f"Stops ({flight.stops}) exceed max allowed ({max_allowed_stops})"

        # 3. Blocked carriers
        for airline in flight.airlines:
            airline_lower = airline.lower()
            for blocked in self.blocked_carriers:
                if blocked in airline_lower:
                    return False, f"Carrier '{airline}' matches blocked carrier '{blocked}'"

        # 4. Blocked layover airports
        for layover in flight.layover_airports:
            if layover.upper() in self.blocked_layovers:
                return False, f"Layover airport '{layover}' is blocked"

        return True, "OK"

    def fetch_leg(self, from_airport: str, to_airport: str, date_str: str, is_long_haul: bool = True) -> List[FlightOption]:
        # Check cache
        if self.cache:
            cached = self.cache.get(from_airport, to_airport, date_str)
            if cached is not None:
                # Filter cached results
                valid = [opt for opt in cached if self.is_flight_valid(opt, is_long_haul)[0]]
                return valid

        # Check request budget
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            logger.warning(f"Request budget limit reached ({self.max_requests}). Stopping queries.")
            raise ScraperBlockedException(f"Request budget limit reached ({self.max_requests})")

        # Rate limiting delay before making request
        delay = random.uniform(self.min_delay, self.max_delay)
        time.sleep(delay)

        # Build query
        flight_query = FlightQuery(
            date=date_str,
            from_airport=from_airport,
            to_airport=to_airport,
        )
        passengers = Passengers(adults=1)
        query = create_filter(
            flights=[flight_query],
            seat="economy",
            trip="one-way",
            passengers=passengers,
            currency="EUR",
            language="en-US"
        )

        attempts = 0
        backoff = 2.0
        raw_options: List[FlightOption] = []

        while attempts < self.max_retries:
            attempts += 1
            self.requests_made += 1
            try:
                html = self.integration.fetch_html(query)
                raw_options = parse_flights_html(html)
                break
            except ScraperBlockedException:
                raise
            except Exception as e:
                logger.warning(f"Attempt {attempts} failed for {from_airport}->{to_airport} on {date_str}: {e}")
                if attempts >= self.max_retries:
                    logger.error(f"Max retries reached for {from_airport}->{to_airport} on {date_str}.")
                    raw_options = []
                else:
                    time.sleep(backoff)
                    backoff *= self.backoff_factor

        # Cache raw options (even if empty to cache "no results")
        if self.cache:
            self.cache.set(from_airport, to_airport, date_str, raw_options)

        # Filter
        valid_options = [opt for opt in raw_options if self.is_flight_valid(opt, is_long_haul)[0]]
        return valid_options
