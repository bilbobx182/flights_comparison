"""
Live query test for Step 1: DUB -> PVG
Using fast-flights with ConsentBypassFetchIntegration.
"""
from typing import Union
from primp import Client
from fast_flights import FlightQuery, Passengers, create_filter, get_flights
from fast_flights.integrations.base import FetchIntegration
from fast_flights.querying import Query

class PrimpConsentFetchIntegration(FetchIntegration):
    """
    FetchIntegration that handles Google's EU consent requirement without a browser.
    Injects consent cookies (SOCS) and realistic headers via primp (Rust TLS impersonation).
    Lightweight and fast, ideal for Raspberry Pi.
    """
    def __init__(self, proxy: str | None = None, timeout: int = 30):
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

    def fetch_html(self, q: Union[Query, str]) -> str:
        url = "https://www.google.com/travel/flights"
        params = q.params() if isinstance(q, Query) else {"q": q}
        res = self.client.get(url, params=params)
        return res.text


def run_live_query(date: str = "2027-03-05", from_code: str = "DUB", to_code: str = "PVG"):
    print("=" * 60)
    print(f"STEP 1: LIVE QUERY TEST ({from_code} -> {to_code} on {date})")
    print("=" * 60)

    # 1. Build Query using fast-flights 3.x API
    flight_query = FlightQuery(
        date=date,
        from_airport=from_code,
        to_airport=to_code,
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

    fetch_integration = PrimpConsentFetchIntegration()
    
    print(f"Querying Google Flights for {from_code} -> {to_code} on {date} (Currency: EUR, Seat: economy)...")
    results = get_flights(query, integration=fetch_integration)

    print(f"\nSuccessfully retrieved and parsed flights! Found: {len(results)} flight options.\n")
    
    for idx, flight in enumerate(results):
        stops = len(flight.flights) - 1
        layover_airports = [f.to_airport.code for f in flight.flights[:-1]]
        layover_names = [f.to_airport.name for f in flight.flights[:-1]]
        total_duration = sum(f.duration for f in flight.flights)
        dep = flight.flights[0].departure
        arr = flight.flights[-1].arrival
        
        dep_str = f"{dep.date[0]}-{dep.date[1]:02d}-{dep.date[2]:02d} {dep.time[0]:02d}:{dep.time[1]:02d}"
        arr_str = f"{arr.date[0]}-{arr.date[1]:02d}-{arr.date[2]:02d} {arr.time[0]:02d}:{arr.time[1]:02d}"

        print(f"Option {idx + 1}:")
        print(f"  Price:           €{flight.price}")
        print(f"  Airlines:        {', '.join(flight.airlines)}")
        print(f"  Stops:           {stops} {f'(Layover at {layover_airports[0]} - {layover_names[0]})' if stops > 0 else '(Direct)'}")
        if stops > 1:
            print(f"  All Layovers:    {', '.join(f'{code} ({name})' for code, name in zip(layover_airports, layover_names))}")
        print(f"  Departure:       {dep_str} from {flight.flights[0].from_airport.code} ({flight.flights[0].from_airport.name})")
        print(f"  Arrival:         {arr_str} at {flight.flights[-1].to_airport.code} ({flight.flights[-1].to_airport.name})")
        print(f"  Total Air Time:  {total_duration // 60}h {total_duration % 60}m ({total_duration} mins)")
        
        print(f"  Flight Segments ({len(flight.flights)}):")
        for s_idx, seg in enumerate(flight.flights):
            s_dep = f"{seg.departure.date[0]}-{seg.departure.date[1]:02d}-{seg.departure.date[2]:02d} {seg.departure.time[0]:02d}:{seg.departure.time[1]:02d}"
            s_arr = f"{seg.arrival.date[0]}-{seg.arrival.date[1]:02d}-{seg.arrival.date[2]:02d} {seg.arrival.time[0]:02d}:{seg.arrival.time[1]:02d}"
            print(f"    [{s_idx+1}] {seg.from_airport.code} -> {seg.to_airport.code} | {s_dep} - {s_arr} | {seg.duration}m | {seg.plane_type or 'Aircraft unspecified'}")
        print("-" * 60)

if __name__ == "__main__":
    run_live_query()
