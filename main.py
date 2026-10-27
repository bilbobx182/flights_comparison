#!/usr/bin/env python3
"""
Multi-city flight price finder for Dublin -> China -> North Japan -> Tokyo -> Dublin.
Performs staged branch-and-bound pruning, short-haul leg pricing, ranking, and safety bounds.
"""
import argparse
import csv
import logging
import shlex
from datetime import datetime, timedelta
from typing import Dict, List, Any, Tuple, Optional
import yaml

from flight_cache import FlightCache
from flight_fetcher import FlightFetcher, ScraperBlockedException
from models import DateLongHaulSummary, FlightOption, FullItinerary
from notifier import check_and_send_alerts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("main")


def load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def format_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def get_date_range(start_str: str, end_str: str) -> List[str]:
    start = parse_date(start_str)
    end = parse_date(end_str)
    dates = []
    curr = start
    while curr <= end:
        dates.append(format_date(curr))
        curr += timedelta(days=1)
    return dates


def get_slot_airports(config: Dict[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    slots = config.get("slots", {})
    china_airports = []
    for city, codes in slots.get("china", {}).get("cities", {}).items():
        china_airports.extend(codes)

    north_japan_airports = []
    for city, codes in slots.get("north_japan", {}).get("cities", {}).items():
        north_japan_airports.extend(codes)

    tokyo_airports = []
    for city, codes in slots.get("tokyo", {}).get("cities", {}).items():
        tokyo_airports.extend(codes)

    return china_airports, north_japan_airports, tokyo_airports


def plan_all_queries(start_dates: List[str], config: Dict[str, Any]) -> Tuple[List[Tuple[str, str, str, str]], List[Tuple[str, str, str, str]]]:
    origin = config.get("origin", "DUB")
    final_dest = config.get("final_destination", "DUB")
    days = config.get("days_per_city", {})
    days_china = days.get("china", 7)
    days_nj = days.get("north_japan", 3)
    days_tokyo = days.get("tokyo", 7)
    total_days = days_china + days_nj + days_tokyo

    china_aps, nj_aps, tokyo_aps = get_slot_airports(config)

    stage1_queries = []
    stage3_queries = []

    for s_date in start_dates:
        dt_start = parse_date(s_date)
        d_l2 = format_date(dt_start + timedelta(days=days_china))
        d_l3 = format_date(dt_start + timedelta(days=days_china + days_nj))
        d_l4 = format_date(dt_start + timedelta(days=total_days))

        for c in china_aps:
            stage1_queries.append(("L1", origin, c, s_date))
        for t in tokyo_aps:
            stage1_queries.append(("L4", t, final_dest, d_l4))

        for c in china_aps:
            for nj in nj_aps:
                stage3_queries.append(("L2", c, nj, d_l2))
        for nj in nj_aps:
            for t in tokyo_aps:
                stage3_queries.append(("L3", nj, t, d_l3))

    return stage1_queries, stage3_queries


def build_live_command(args: argparse.Namespace, stage: Optional[str] = None) -> str:
    """Build a repeatable CLI command from the current dry-run options."""
    command = ["main.py", "--config", args.config]
    if args.start_date:
        command.extend(["--start-date", args.start_date])
    if args.end_date:
        command.extend(["--end-date", args.end_date])
    if args.max_requests is not None:
        command.extend(["--max-requests", str(args.max_requests)])
    if args.output_csv != "results.csv":
        command.extend(["--output-csv", args.output_csv])
    if args.no_cache:
        command.append("--no-cache")
    if stage:
        command.extend(["--stage", stage])
    return shlex.join(command)


def print_next_steps(args: argparse.Namespace):
    """Print copy-and-pasteable commands after a dry run."""
    full_run = build_live_command(args)
    print("\nNEXT STEPS")
    print("=" * 70)
    if args.no_cache:
        print("Run all stages (1–4):")
        print(f'  docker run --rm -v "$(pwd)":/app flights-finder {full_run}')
        print("Caching is disabled, so this run cannot be resumed from cached results.")
        return

    stage_one = build_live_command(args, stage="1")
    print("1. Fetch long-haul flights and apply Stage 2 pruning:")
    print(f'   docker run --rm -v "$(pwd)":/app flights-finder {stage_one}')
    print("2. Resume from the cache and complete Stages 3–4:")
    print(f'   docker run --rm -v "$(pwd)":/app flights-finder {full_run}')
    print("The second command rechecks Stage 1 from the cache; cached routes make no network requests.")


def print_dry_run(
    start_dates: List[str], config: Dict[str, Any], cache: FlightCache, args: argparse.Namespace
):
    s1_queries, s3_queries = plan_all_queries(start_dates, config)
    uniq_s1 = list(dict.fromkeys((q[1], q[2], q[3]) for q in s1_queries))
    uniq_s3 = list(dict.fromkeys((q[1], q[2], q[3]) for q in s3_queries))

    s1_cached = sum(1 for q in uniq_s1 if cache.get(q[0], q[1], q[2]) is not None)
    s3_cached = sum(1 for q in uniq_s3 if cache.get(q[0], q[1], q[2]) is not None)

    min_delay = config.get("politeness", {}).get("min_delay_seconds", 3.0)
    max_delay = config.get("politeness", {}).get("max_delay_seconds", 8.0)
    avg_delay = (min_delay + max_delay) / 2.0

    print("=" * 70)
    print("DRY RUN: PLANNED QUERY ESTIMATION")
    print("=" * 70)
    print(f"Date range:              {start_dates[0]} to {start_dates[-1]} ({len(start_dates)} start dates)")
    days = config.get("days_per_city", {})
    trip_len = days.get("china", 7) + days.get("north_japan", 3) + days.get("tokyo", 7)
    print(f"Total trip length:       {trip_len} days (China: {days.get('china')}, N.Japan: {days.get('north_japan')}, Tokyo: {days.get('tokyo')})")
    
    china_aps, nj_aps, tokyo_aps = get_slot_airports(config)
    print(f"Airports:                China: {china_aps} | North Japan: {nj_aps} | Tokyo: {tokyo_aps}")
    print("\nSTAGE 1 (Long-Haul L1 + L4):")
    print(f"  Total evaluations:     {len(s1_queries)} queries ({len(uniq_s1)} unique)")
    print(f"  Cached queries:        {s1_cached} queries")
    print(f"  Pending net fetches:   {len(uniq_s1) - s1_cached} queries (~{((len(uniq_s1) - s1_cached) * avg_delay) / 60:.1f} mins)")

    print("\nSTAGE 3 (Short-Haul L2 + L3, Worst-Case before Stage 2 Pruning):")
    print(f"  Total evaluations:     {len(s3_queries)} queries ({len(uniq_s3)} unique)")
    print(f"  Cached queries:        {s3_cached} queries")
    print(f"  Pending net fetches:   {len(uniq_s3) - s3_cached} queries")

    p_conf = config.get("pruning", {})
    keep_top = p_conf.get("keep_top_dates", 5)
    slack = p_conf.get("slack_eur", 200)
    print(f"\nBranch & Bound Pruning Policy: Keep top {keep_top} dates within €{slack} slack of best long-haul cost.")
    print("Stage 3 will ONLY query surviving dates, significantly cutting short-haul queries.")
    print("=" * 70)
    print_next_steps(args)


def run_stage1(
    start_dates: List[str],
    config: Dict[str, Any],
    fetcher: FlightFetcher,
    cache: FlightCache
) -> Tuple[Dict[str, DateLongHaulSummary], Dict[str, Dict[str, List[FlightOption]]]]:
    china_aps, _, tokyo_aps = get_slot_airports(config)
    origin = config.get("origin", "DUB")
    final_dest = config.get("final_destination", "DUB")
    days = config.get("days_per_city", {})
    trip_days = days.get("china", 7) + days.get("north_japan", 3) + days.get("tokyo", 7)

    summaries: Dict[str, DateLongHaulSummary] = {}
    l1_results_by_date: Dict[str, Dict[str, List[FlightOption]]] = {}  # date -> {ap: flights}
    l4_results_by_date: Dict[str, Dict[str, List[FlightOption]]] = {}  # ret_date -> {ap: flights}

    print("\n" + "=" * 70)
    print(f"STAGE 1: Long-Haul Pricing across {len(start_dates)} start dates")
    print(f"Notice: All legs are separate one-way tickets priced independently.")
    print("=" * 70)

    try:
        for idx, s_date in enumerate(start_dates, 1):
            dt_start = parse_date(s_date)
            ret_date = format_date(dt_start + timedelta(days=trip_days))
            print(f"\n[{idx}/{len(start_dates)}] Start Date: {s_date} | Return Date: {ret_date}")

            summary = DateLongHaulSummary(start_date=s_date, return_date=ret_date)
            l1_results_by_date[s_date] = {}

            best_l1_price = None
            best_l1_flight = None
            best_l1_route = None

            for ap in china_aps:
                print(f"  Querying L1: {origin} -> {ap} on {s_date}...", end=" ", flush=True)
                flights = fetcher.fetch_leg(origin, ap, s_date, is_long_haul=True)
                l1_results_by_date[s_date][ap] = flights
                if flights:
                    cheapest = min(flights, key=lambda f: f.price)
                    print(f"found {len(flights)} valid flights (Cheapest: €{cheapest.price} with {', '.join(cheapest.airlines)})")
                    if best_l1_price is None or cheapest.price < best_l1_price:
                        best_l1_price = cheapest.price
                        best_l1_flight = cheapest
                        best_l1_route = f"{origin}->{ap}"
                else:
                    print("no valid flights found.")

            if ret_date not in l4_results_by_date:
                l4_results_by_date[ret_date] = {}
                for ap in tokyo_aps:
                    print(f"  Querying L4: {ap} -> {final_dest} on {ret_date}...", end=" ", flush=True)
                    flights = fetcher.fetch_leg(ap, final_dest, ret_date, is_long_haul=True)
                    l4_results_by_date[ret_date][ap] = flights
                    if flights:
                        cheapest = min(flights, key=lambda f: f.price)
                        print(f"found {len(flights)} valid flights (Cheapest: €{cheapest.price} with {', '.join(cheapest.airlines)})")
                    else:
                        print("no valid flights found.")

            # Determine best L4 for this date
            best_l4_price = None
            best_l4_flight = None
            best_l4_route = None
            for ap, flights in l4_results_by_date[ret_date].items():
                if flights:
                    cheapest = min(flights, key=lambda f: f.price)
                    if best_l4_price is None or cheapest.price < best_l4_price:
                        best_l4_price = cheapest.price
                        best_l4_flight = cheapest
                        best_l4_route = f"{ap}->{final_dest}"

            summary.cheapest_l1_price = best_l1_price
            summary.cheapest_l1_flight = best_l1_flight
            summary.cheapest_l1_route = best_l1_route

            summary.cheapest_l4_price = best_l4_price
            summary.cheapest_l4_flight = best_l4_flight
            summary.cheapest_l4_route = best_l4_route

            if best_l1_price is not None and best_l4_price is not None:
                summary.long_haul_cost = best_l1_price + best_l4_price
                print(f"  => Long-haul total: €{summary.long_haul_cost} (L1: {best_l1_route} €{best_l1_price} + L4: {best_l4_route} €{best_l4_price})")
            else:
                summary.long_haul_cost = None
                print(f"  => Incomplete long-haul options (L1: {best_l1_price or 'N/A'}, L4: {best_l4_price or 'N/A'})")

            summaries[s_date] = summary

    except ScraperBlockedException as e:
        print(f"\n[Scraper Interrupted] {e}")

    # Stage 1 summary table
    print("\n" + "=" * 75)
    print("STAGE 1 RESULTS: Long-Haul Costs per Start Date")
    print("=" * 75)
    print(f"{'Start Date':<12} | {'Return Date':<12} | {'Best L1 (DUB->China)':<20} | {'Best L4 (Tokyo->DUB)':<20} | {'Long-Haul Cost':<14}")
    print("-" * 75)
    for s_date, s in sorted(summaries.items()):
        l1_str = f"{s.cheapest_l1_route} €{s.cheapest_l1_price}" if s.cheapest_l1_price else "No valid"
        l4_str = f"{s.cheapest_l4_route} €{s.cheapest_l4_price}" if s.cheapest_l4_price else "No valid"
        cost_str = f"€{s.long_haul_cost}" if s.long_haul_cost is not None else "N/A"
        print(f"{s.start_date:<12} | {s.return_date:<12} | {l1_str:<20} | {l4_str:<20} | {cost_str:<14}")
    print("=" * 75)

    stage1_flights = {"l1": l1_results_by_date, "l4": l4_results_by_date}
    return summaries, stage1_flights


def run_stage2_prune(
    summaries: Dict[str, DateLongHaulSummary],
    config: Dict[str, Any]
) -> Tuple[List[str], List[DateLongHaulSummary]]:
    """
    Branch-and-bound pruning: keeps dates within slack_eur of minimum long-haul cost,
    capped at keep_top_dates.
    """
    p_conf = config.get("pruning", {})
    keep_top_dates = p_conf.get("keep_top_dates", 5)
    slack_eur = p_conf.get("slack_eur", 200)

    valid_dates = [s for s in summaries.values() if s.long_haul_cost is not None]
    invalid_dates = [s for s in summaries.values() if s.long_haul_cost is None]

    for inv in invalid_dates:
        inv.pruned = True
        inv.prune_reason = "No valid long-haul flights found"

    valid_dates.sort(key=lambda s: s.long_haul_cost)

    surviving_dates: List[str] = []
    pruned_dates: List[DateLongHaulSummary] = list(invalid_dates)

    if not valid_dates:
        print("\n[Stage 2 Pruning] Warning: No valid start dates found with long-haul flights.")
        return [], pruned_dates

    min_cost = valid_dates[0].long_haul_cost
    threshold = min_cost + slack_eur

    print("\n" + "=" * 70)
    print("STAGE 2: Branch-and-Bound Pruning")
    print(f"Policy: Min long-haul cost = €{min_cost}. Slack = €{slack_eur}. Threshold = €{threshold}. Keep top {keep_top_dates}.")
    print("=" * 70)

    for rank, s in enumerate(valid_dates, 1):
        if s.long_haul_cost > threshold:
            s.pruned = True
            s.prune_reason = f"Long-haul cost €{s.long_haul_cost} > threshold €{threshold} (€{min_cost} + €{slack_eur} slack)"
            pruned_dates.append(s)
            print(f"❌ Pruned {s.start_date}: €{s.long_haul_cost} (Exceeds threshold €{threshold})")
        elif rank > keep_top_dates:
            s.pruned = True
            s.prune_reason = f"Rank #{rank} exceeds keep_top_dates ({keep_top_dates})"
            pruned_dates.append(s)
            print(f"❌ Pruned {s.start_date}: €{s.long_haul_cost} (Rank #{rank} > {keep_top_dates})")
        else:
            s.pruned = False
            surviving_dates.append(s.start_date)
            print(f"✅ Kept   {s.start_date}: €{s.long_haul_cost} (Rank #{rank}, within €{s.long_haul_cost - min_cost} of best)")

    print("-" * 70)
    print(f"Surviving Start Dates ({len(surviving_dates)}): {', '.join(surviving_dates)}")
    print(f"Pruned Start Dates ({len(pruned_dates)}): {len(pruned_dates)} dates dropped")
    print("=" * 70)

    return surviving_dates, pruned_dates


def run_stage3(
    surviving_dates: List[str],
    config: Dict[str, Any],
    fetcher: FlightFetcher,
    cache: FlightCache
) -> Dict[str, Dict[str, Any]]:
    """
    Price short-haul legs L2 (China -> North Japan) and L3 (North Japan -> Tokyo)
    for surviving dates ONLY.
    """
    china_aps, nj_aps, tokyo_aps = get_slot_airports(config)
    days = config.get("days_per_city", {})
    days_china = days.get("china", 7)
    days_nj = days.get("north_japan", 3)

    stage3_data: Dict[str, Dict[str, Any]] = {}

    print("\n" + "=" * 70)
    print(f"STAGE 3: Short-Haul Leg Pricing for {len(surviving_dates)} surviving dates")
    print("=" * 70)

    try:
        for idx, s_date in enumerate(surviving_dates, 1):
            dt_start = parse_date(s_date)
            d_l2 = format_date(dt_start + timedelta(days=days_china))
            d_l3 = format_date(dt_start + timedelta(days=days_china + days_nj))

            print(f"\n[{idx}/{len(surviving_dates)}] Surviving Date {s_date}:")
            print(f"  L2 Date (China -> N.Japan): {d_l2}")
            print(f"  L3 Date (N.Japan -> Tokyo): {d_l3}")

            stage3_data[s_date] = {"l2": {}, "l3": {}}

            # L2: China candidates -> North Japan candidates
            for c in china_aps:
                for nj in nj_aps:
                    route_key = f"{c}->{nj}"
                    print(f"  Querying L2: {route_key} on {d_l2}...", end=" ", flush=True)
                    flights = fetcher.fetch_leg(c, nj, d_l2, is_long_haul=False)
                    stage3_data[s_date]["l2"][route_key] = flights
                    if flights:
                        cheapest = min(flights, key=lambda f: f.price)
                        print(f"found {len(flights)} valid flights (Cheapest: €{cheapest.price} with {', '.join(cheapest.airlines)})")
                    else:
                        print("no valid flights found.")

            # L3: North Japan candidates -> Tokyo candidates
            for nj in nj_aps:
                for t in tokyo_aps:
                    route_key = f"{nj}->{t}"
                    print(f"  Querying L3: {route_key} on {d_l3}...", end=" ", flush=True)
                    flights = fetcher.fetch_leg(nj, t, d_l3, is_long_haul=False)
                    stage3_data[s_date]["l3"][route_key] = flights
                    if flights:
                        cheapest = min(flights, key=lambda f: f.price)
                        print(f"found {len(flights)} valid flights (Cheapest: €{cheapest.price} with {', '.join(cheapest.airlines)})")
                    else:
                        print("no valid flights found.")

    except ScraperBlockedException as e:
        print(f"\n[Scraper Interrupted] {e}")

    return stage3_data


def run_stage4(
    surviving_dates: List[str],
    stage1_flights: Dict[str, Any],
    stage3_data: Dict[str, Any],
    config: Dict[str, Any]
) -> List[FullItinerary]:
    """
    Build full itineraries for all candidate combinations, compute totals, and rank.
    """
    origin = config.get("origin", "DUB")
    final_dest = config.get("final_destination", "DUB")
    china_aps, nj_aps, tokyo_aps = get_slot_airports(config)
    days = config.get("days_per_city", {})
    days_china = days.get("china", 7)
    days_nj = days.get("north_japan", 3)
    days_tokyo = days.get("tokyo", 7)
    total_days = days_china + days_nj + days_tokyo

    itineraries: List[FullItinerary] = []

    l1_by_date = stage1_flights.get("l1", {})
    l4_by_date = stage1_flights.get("l4", {})

    for s_date in surviving_dates:
        dt_start = parse_date(s_date)
        d_l2 = format_date(dt_start + timedelta(days=days_china))
        d_l3 = format_date(dt_start + timedelta(days=days_china + days_nj))
        d_l4 = format_date(dt_start + timedelta(days=total_days))

        s3 = stage3_data.get(s_date, {})
        l2_routes = s3.get("l2", {})
        l3_routes = s3.get("l3", {})

        for c in china_aps:
            l1_flights = l1_by_date.get(s_date, {}).get(c, [])
            if not l1_flights:
                continue
            best_l1 = min(l1_flights, key=lambda f: f.price)

            for nj in nj_aps:
                l2_key = f"{c}->{nj}"
                l2_flights = l2_routes.get(l2_key, [])
                if not l2_flights:
                    continue
                best_l2 = min(l2_flights, key=lambda f: f.price)

                for t in tokyo_aps:
                    l3_key = f"{nj}->{t}"
                    l3_flights = l3_routes.get(l3_key, [])
                    if not l3_flights:
                        continue
                    best_l3 = min(l3_flights, key=lambda f: f.price)

                    l4_flights = l4_by_date.get(d_l4, {}).get(t, [])
                    if not l4_flights:
                        continue
                    best_l4 = min(l4_flights, key=lambda f: f.price)

                    total_price = best_l1.price + best_l2.price + best_l3.price + best_l4.price
                    route_summary = f"{origin} -> {c} -> {nj} -> {t} -> {final_dest}"

                    itin = FullItinerary(
                        start_date=s_date,
                        total_price=total_price,
                        route_summary=route_summary,
                        l1_from=origin,
                        l1_to=c,
                        l1_date=s_date,
                        l1_flight=best_l1,
                        l2_from=c,
                        l2_to=nj,
                        l2_date=d_l2,
                        l2_flight=best_l2,
                        l3_from=nj,
                        l3_to=t,
                        l3_date=d_l3,
                        l3_flight=best_l3,
                        l4_from=t,
                        l4_to=final_dest,
                        l4_date=d_l4,
                        l4_flight=best_l4,
                    )
                    itineraries.append(itin)

    # Rank ascending by total price
    itineraries.sort(key=lambda x: x.total_price)
    return itineraries


def output_results(
    itineraries: List[FullItinerary],
    pruned_dates: List[DateLongHaulSummary],
    cache: FlightCache,
    csv_file: str = "results.csv"
):
    print("\n" + "=" * 95)
    print("STAGE 4: TOP 10 ITINERARY RANKINGS")
    print("=" * 95)
    print("NOTE: All legs are separate one-way tickets priced independently. Verify on Google Flights before booking.\n")

    if not itineraries:
        print("No complete full itineraries found matching all criteria.")
    else:
        top_10 = itineraries[:10]
        for rank, itin in enumerate(top_10, 1):
            print(f"Rank #{rank:02d} | Total: €{itin.total_price} | Start Date: {itin.start_date} | Route: {itin.route_summary}")
            
            def leg_desc(lid: str, ffrom: str, fto: str, fdate: str, fl: FlightOption) -> str:
                stops_info = f"{fl.stops} stop" if fl.stops == 1 else f"{fl.stops} stops" if fl.stops > 1 else "Direct"
                layovers = f" (via {', '.join(fl.layover_airports)})" if fl.layover_airports else ""
                unv = " [Layover unverified]" if fl.layover_unverified else ""
                return f"  {lid}: {ffrom}->{fto} on {fdate} | €{fl.price:<4} | {', '.join(fl.airlines)} | {stops_info}{layovers}{unv}"

            print(leg_desc("L1", itin.l1_from, itin.l1_to, itin.l1_date, itin.l1_flight))
            print(leg_desc("L2", itin.l2_from, itin.l2_to, itin.l2_date, itin.l2_flight))
            print(leg_desc("L3", itin.l3_from, itin.l3_to, itin.l3_date, itin.l3_flight))
            print(leg_desc("L4", itin.l4_from, itin.l4_to, itin.l4_date, itin.l4_flight))
            print("-" * 95)

    # Safety Bound Check
    print("\n" + "=" * 70)
    print("SAFETY BOUND VERIFICATION")
    print("=" * 70)
    if itineraries:
        best_total = itineraries[0].total_price
        print(f"Best full itinerary total found: €{best_total}")
        violations = []
        for p in pruned_dates:
            if p.long_haul_cost is not None:
                # Hypothetical min bound: long-haul cost + 30 EUR per short leg (2 short legs = 60 EUR)
                hypothetical_bound = p.long_haul_cost + 60
                if hypothetical_bound < best_total:
                    violations.append((p.start_date, p.long_haul_cost, hypothetical_bound))

        if violations:
            for s_date, lh_cost, hyp in violations:
                print(f"⚠️  WARNING: Pruned date {s_date} had long-haul cost €{lh_cost}.")
                print(f"    Hypothetical bound: €{lh_cost} + 2×€30 = €{hyp} < best total €{best_total}!")
                print(f"    Consider increasing 'slack_eur' or 'keep_top_dates' to evaluate this date.")
        else:
            print("✅ Safety bound satisfied: No pruned date could beat the best total even")
            print("   assuming an optimistic minimum fare of €30 per short-haul leg (€60 total).")
    else:
        print("No completed itineraries to evaluate safety bounds against.")
    print("=" * 70)

    # Pruned Dates Summary
    print("\n" + "=" * 70)
    print("PRUNED DATES SUMMARY")
    print("=" * 70)
    if not pruned_dates:
        print("No dates were pruned.")
    else:
        print(f"{'Start Date':<12} | {'Long-Haul Cost':<15} | {'Prune Reason'}")
        print("-" * 70)
        for p in pruned_dates:
            cost_str = f"€{p.long_haul_cost}" if p.long_haul_cost is not None else "N/A"
            print(f"{p.start_date:<12} | {cost_str:<15} | {p.prune_reason}")
    print("=" * 70)

    # Cache Stats
    c_stats = cache.stats()
    print("\n" + "=" * 70)
    print(f"CACHE PERFORMANCE: {c_stats['hits']} hits, {c_stats['misses']} misses ({c_stats['hit_ratio_percent']}% hit ratio)")
    print(f"Cached routes in store: {c_stats['cached_routes_count']}")
    print("=" * 70)

    # Write CSV
    write_results_csv(itineraries, csv_file)
    print(f"\nFull results exported to {csv_file}")


def write_results_csv(itineraries: List[FullItinerary], csv_file: str):
    fieldnames = [
        "Rank", "Start Date", "Total Price (EUR)", "Route Summary",
        "L1 Route", "L1 Date", "L1 Airlines", "L1 Stops", "L1 Layovers", "L1 Price (EUR)",
        "L2 Route", "L2 Date", "L2 Airlines", "L2 Stops", "L2 Layovers", "L2 Price (EUR)",
        "L3 Route", "L3 Date", "L3 Airlines", "L3 Stops", "L3 Layovers", "L3 Price (EUR)",
        "L4 Route", "L4 Date", "L4 Airlines", "L4 Stops", "L4 Layovers", "L4 Price (EUR)",
    ]

    try:
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for rank, itin in enumerate(itineraries, 1):
                writer.writerow({
                    "Rank": rank,
                    "Start Date": itin.start_date,
                    "Total Price (EUR)": itin.total_price,
                    "Route Summary": itin.route_summary,
                    "L1 Route": f"{itin.l1_from}->{itin.l1_to}",
                    "L1 Date": itin.l1_date,
                    "L1 Airlines": ", ".join(itin.l1_flight.airlines),
                    "L1 Stops": itin.l1_flight.stops,
                    "L1 Layovers": ", ".join(itin.l1_flight.layover_airports),
                    "L1 Price (EUR)": itin.l1_flight.price,
                    "L2 Route": f"{itin.l2_from}->{itin.l2_to}",
                    "L2 Date": itin.l2_date,
                    "L2 Airlines": ", ".join(itin.l2_flight.airlines),
                    "L2 Stops": itin.l2_flight.stops,
                    "L2 Layovers": ", ".join(itin.l2_flight.layover_airports),
                    "L2 Price (EUR)": itin.l2_flight.price,
                    "L3 Route": f"{itin.l3_from}->{itin.l3_to}",
                    "L3 Date": itin.l3_date,
                    "L3 Airlines": ", ".join(itin.l3_flight.airlines),
                    "L3 Stops": itin.l3_flight.stops,
                    "L3 Layovers": ", ".join(itin.l3_flight.layover_airports),
                    "L3 Price (EUR)": itin.l3_flight.price,
                    "L4 Route": f"{itin.l4_from}->{itin.l4_to}",
                    "L4 Date": itin.l4_date,
                    "L4 Airlines": ", ".join(itin.l4_flight.airlines),
                    "L4 Stops": itin.l4_flight.stops,
                    "L4 Layovers": ", ".join(itin.l4_flight.layover_airports),
                    "L4 Price (EUR)": itin.l4_flight.price,
                })
    except Exception as e:
        logger.error(f"Failed to write results CSV: {e}")


def main():
    parser = argparse.ArgumentParser(description="Multi-city flight price finder")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--start-date", help="Override start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Override end date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Print planned query count without fetching")
    parser.add_argument("--max-requests", type=int, default=None, help="Stop after N network requests")
    parser.add_argument(
        "--stage",
        choices=["1", "2", "all"],
        default="all",
        help="Stop after Stage 2 pruning (1 or 2), or run all stages (default: all)",
    )
    parser.add_argument("--output-csv", default="results.csv", help="CSV output file")
    parser.add_argument("--no-cache", action="store_true", help="Do not load/save disk cache")

    args = parser.parse_args()

    config = load_config(args.config)

    # Date range determination
    month_cfg = config.get("month_range", {})
    start_date = args.start_date or month_cfg.get("start_date", "2027-09-01")
    end_date = args.end_date or month_cfg.get("end_date", "2027-09-30")

    start_dates = get_date_range(start_date, end_date)

    cache_file = None if args.no_cache else config.get("politeness", {}).get("cache_file", "flights_cache.json")
    cache = FlightCache(cache_file=cache_file)

    if args.dry_run:
        print_dry_run(start_dates, config, cache, args)
        return

    fetcher = FlightFetcher(config, cache=cache, max_requests=args.max_requests)

    # Stage 1: Long-Haul
    summaries, stage1_flights = run_stage1(start_dates, config, fetcher, cache)

    # Stage 2: Pruning
    surviving_dates, pruned_dates = run_stage2_prune(summaries, config)

    # If only Stage 1 or 2 requested
    if args.stage in ["1", "2"]:
        return

    # Stage 3: Short-Haul for surviving dates
    stage3_data = run_stage3(surviving_dates, config, fetcher, cache)

    # Stage 4: Build, Rank, Safety Bound, CSV
    itineraries = run_stage4(surviving_dates, stage1_flights, stage3_data, config)
    output_results(itineraries, pruned_dates, cache, csv_file=args.output_csv)
    check_and_send_alerts(config, itineraries)


if __name__ == "__main__":
    main()
