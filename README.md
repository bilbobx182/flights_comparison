# Multi-City Flight Price Finder (Raspberry Pi & Python)

A high-performance, lightweight CLI tool designed to find the cheapest multi-city trip:
**Dublin (DUB) → China → Northern Japan (Hokkaido) → Tokyo → Dublin (DUB)**.

Built with Python 3 and [`fast-flights`](https://pypi.org/project/fast-flights/) using a **staged branch-and-bound search** that queries long-haul legs first, prunes non-competitive dates, and prices short-haul connections only for top-performing windows.

---

## Key Features

* **Lightweight & Pi-Optimized:** Runs on Raspberry Pi (ARM64 / ARMv7) and x86 without heavy headless browsers (no Playwright, no Chromium). Powered by `primp` (Rust-based TLS fingerprint impersonation).
* **EU Consent Intercept Bypass:** Automatically bypasses Google's European GDPR consent screens (`consent.google.com`) by injecting compliant `SOCS` headers.
* **Branch-and-Bound Pruning:** 
  1. *Stage 1:* Prices long-haul legs (L1 Dublin $\to$ China and L4 Tokyo $\to$ Dublin) across all dates.
  2. *Stage 2:* Prunes dates whose long-haul cost exceeds the best date by more than a configurable slack (default: €200) or rank cap (default: top 5).
  3. *Stage 3:* Prices short-haul legs (L2 China $\to$ North Japan and L3 North Japan $\to$ Tokyo) only for surviving dates.
  4. *Stage 4:* Evaluates all slot combinations, ranks by total price, and outputs top 10.
* **Safety Bound Verification:** Post-search check that mathematically proves whether any dropped date could have beaten the best itinerary under an optimistic €30/leg minimum fare.
* **Two-Tier LRU & Persistent Disk Cache:** In-memory LRU backed by `flights_cache.json`. Caches both found flights and empty routes to eliminate redundant HTTP requests across runs.
* **Scraper Politeness:** Randomized delays (3–8s), exponential backoff, request budget caps (`--max-requests`), and graceful exit on Google blocks/CAPTCHAs.
* **Automated Alerts:** Optional Telegram Bot and SMTP Email notifications when an itinerary drops below your price threshold.
* **Clean Data Export:** Exports full itinerary rankings with flight numbers, airlines, layovers, and leg prices to `results.csv`.

---

## Project Structure

```text
├── Dockerfile                   # Minimal Docker setup based on python:3.11-slim-bullseye
├── requirements.txt             # Python dependencies (fast-flights, pyyaml, typing-extensions)
├── config.yaml                  # Default configuration file
├── main.py                      # Main CLI entrypoint
├── flight_fetcher.py            # Fast-flights wrapper with EU consent bypass, filters & backoff
├── flight_cache.py              # LRU in-memory + persistent disk JSON cache
├── models.py                    # Dataclasses for flight options, segments & itineraries
├── notifier.py                  # Telegram & Email notification module
├── live_query_step1.py          # Standalone single-route test script (DUB -> PVG)
├── results.csv                  # Exported top itinerary rankings
├── flights_cache.json           # Persistent query cache
└── examples/                    # Sample configurations
    ├── config_september_2027.yaml   # Full 17-day production configuration
    ├── config_spring_march.yaml     # Spring test configuration (active schedules)
    └── config_direct_shanghai_loop.yaml # Optimized direct-flight loop configuration
```

---

## Quick Start

### Option A: Using Docker (Recommended for Raspberry Pi & Servers)

1. **Build the container:**
   ```bash
   docker build -t flights-finder .
   ```

2. **Run a Dry Run (Calculate planned queries without fetching):**
   ```bash
   docker run --rm -v $(pwd):/app flights-finder main.py --dry-run
   ```

3. **Run a Live Search:**
   ```bash
   docker run --rm -v $(pwd):/app flights-finder main.py --config examples/config_spring_march.yaml
   ```

4. **Run Single Live Test Query (Dublin → Shanghai):**
   ```bash
   docker run --rm -v $(pwd):/app flights-finder live_query_step1.py
   ```

---

### Option B: Running Natively with Python (3.10+)

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Run the CLI:**
   ```bash
   # Dry-run
   python3 main.py --dry-run

   # Live search with date overrides
   python3 main.py --start-date 2027-03-05 --end-date 2027-03-07
   ```

---

## CLI Usage & Options

```text
usage: main.py [-h] [--config CONFIG] [--start-date START_DATE]
               [--end-date END_DATE] [--dry-run] [--max-requests MAX_REQUESTS]
               [--stage {1,2,all}] [--output-csv OUTPUT_CSV] [--no-cache]

Multi-city flight price finder

options:
  -h, --help            Show this help message and exit
  --config CONFIG       Path to YAML configuration (default: config.yaml)
  --start-date DATE     Override start date (YYYY-MM-DD)
  --end-date DATE       Override end date (YYYY-MM-DD)
  --dry-run             Print planned query estimation without sending network requests
  --max-requests N      Stop gracefully after N network requests and show partial results
  --stage {1,2,all}     Stop after Stage 2 pruning (1 or 2), or run all stages
  --output-csv FILE     Path to export CSV results (default: results.csv)
  --no-cache            Bypass disk cache loading and saving
```

### Useful Command Examples

* **Evaluate planned request counts for a full month:**
  ```bash
  python3 main.py --config examples/config_september_2027.yaml --dry-run
  ```

* **Quick long-haul sweep only (Stage 1 & 2):**
  ```bash
  python3 main.py --start-date 2027-03-01 --end-date 2027-03-07 --stage 1
  ```

  Re-run without `--stage 1` to use the saved long-haul cache and complete Stages 3–4.

* **Enforce a strict 20-request budget cap:**
  ```bash
  python3 main.py --start-date 2027-03-05 --end-date 2027-03-10 --max-requests 20
  ```

---

## Configuration Reference (`config.yaml`)

```yaml
# Date range for the trip START date
month_range:
  start_date: "2027-09-01"
  end_date: "2027-09-30"

# Fixed route origin and final destination
origin: "DUB"
final_destination: "DUB"

# Duration spent in each stop (Total trip = 7 + 3 + 7 = 17 days)
days_per_city:
  china: 7
  north_japan: 3
  tokyo: 7

# Slot candidate airports (queried individually; cheapest per slot is selected)
slots:
  china:
    cities:
      Beijing: ["PEK", "PKX"]
      Shanghai: ["PVG", "SHA"]
  north_japan:
    cities:
      Sapporo: ["CTS"]
      # Optional additions (uncomment to include):
      # Hakodate: ["HKD"]
      # Aomori: ["AOJ"]
      # Sendai: ["SDJ"]
  tokyo:
    cities:
      Tokyo: ["HND", "NRT"]

# Exclusions and filtering rules
filters:
  blocked_carriers:
    - "Emirates"
    - "Qatar Airways"
    - "Etihad Airways"
    - "Etihad"
  block_turkish_airlines: false  # Set true to block Turkish Airlines

  blocked_layover_airports:
    - "DXB"
    - "DOH"
    - "AUH"
  block_istanbul_layover: false  # Set true to block Istanbul (IST)

  max_stops_long_haul: 1   # Long-haul legs (DUB -> China and Tokyo -> DUB)
  max_stops_short_haul: 2  # Short-haul legs (China -> Hokkaido and Hokkaido -> Tokyo)
  max_price_per_leg: 1500  # Sanity cap per leg in EUR

# Branch-and-bound pruning settings
pruning:
  keep_top_dates: 5  # Maximum surviving dates to advance to Stage 3
  slack_eur: 200     # Drop dates if long_haul_cost > min_long_haul_cost + slack_eur

# Politeness and rate-limiting
politeness:
  min_delay_seconds: 3.0
  max_delay_seconds: 8.0
  max_retries: 3
  backoff_factor: 2.0
  cache_file: "flights_cache.json"

# Automated alerts
alerts:
  enabled: false
  alert_threshold_eur: 1800  # Trigger alert if best full itinerary <= threshold
  telegram:
    enabled: false
    bot_token: "YOUR_TELEGRAM_BOT_TOKEN"
    chat_id: "YOUR_TELEGRAM_CHAT_ID"
  email:
    enabled: false
    smtp_host: "smtp.gmail.com"
    smtp_port: 587
    sender_email: "you@example.com"
    smtp_password: "APP_PASSWORD"
    recipient_email: "you@example.com"
```

---

## Sample Configurations (`examples/`)

1. **[`examples/config_september_2027.yaml`](file:///Users/ciarannolan/projects/flights/examples/config_september_2027.yaml)**:
   * Target production configuration for September 2027 across the entire month.
2. **[`examples/config_spring_march.yaml`](file:///Users/ciarannolan/projects/flights/examples/config_spring_march.yaml)**:
   * Focused test window for dates currently within the airline booking horizon.
3. **[`examples/config_direct_shanghai_loop.yaml`](file:///Users/ciarannolan/projects/flights/examples/config_direct_shanghai_loop.yaml)**:
   * Loop configuration returning directly from Shanghai (PVG) to Dublin (DUB).

---

## Important Booking & Airline Notes

1. **Direct Flight Insights:**
   * **Dublin → Beijing:** **Hainan Airlines** operates non-stop direct flights (`HU752`, ~10h, ~€350) on Tuesdays, Thursdays, Saturdays, and Sundays. Hainan distributes via China's TravelSky GDS and is best booked directly on **Trip.com** or **HainanAirlines.com**.
   * **Shanghai → Dublin:** **China Eastern Airlines** operates non-stop direct flights (`MU231`, ~13.5h, ~€380–€480) on Mondays, Wednesdays, and Fridays.
   * **Shanghai → Sapporo:** **Spring Airlines** (`9C8791`) and **China Eastern** (`MU279`) fly non-stop (~3h 15m, ~€180–€280) bookable via Trip.com.
2. **Booking Horizon:**
   * International legacy airlines publish schedules ~300–330 days in advance.
   * Chinese and Japanese domestic/regional summer schedules (e.g. Hokkaido routes) typically release ~6 to 8 months in advance.
3. **Separate Tickets:**
   * All legs are separate one-way fares. Always allow sufficient connection time and verify final pricing on airline websites before booking.
