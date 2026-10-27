from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any

@dataclass
class FlightSegment:
    from_airport: str
    from_name: str
    to_airport: str
    to_name: str
    departure_date: str
    departure_time: str
    arrival_date: str
    arrival_time: str
    duration_minutes: int
    plane_type: str = ""

@dataclass
class FlightOption:
    price: int  # in EUR
    airlines: List[str]
    stops: int
    layover_airports: List[str]
    layover_names: List[str]
    departure_date: str
    departure_time: str
    arrival_date: str
    arrival_time: str
    total_flight_minutes: int
    segments: List[FlightSegment] = field(default_factory=list)
    layover_unverified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FlightOption":
        segments = [FlightSegment(**s) for s in data.get("segments", [])]
        data_copy = {k: v for k, v in data.items() if k != "segments"}
        return cls(**data_copy, segments=segments)

@dataclass
class DateLongHaulSummary:
    start_date: str
    return_date: str
    cheapest_l1_price: Optional[int] = None
    cheapest_l1_flight: Optional[FlightOption] = None
    cheapest_l1_route: Optional[str] = None
    cheapest_l4_price: Optional[int] = None
    cheapest_l4_flight: Optional[FlightOption] = None
    cheapest_l4_route: Optional[str] = None
    long_haul_cost: Optional[int] = None
    pruned: bool = False
    prune_reason: str = ""

@dataclass
class FullItinerary:
    start_date: str
    total_price: int
    route_summary: str
    l1_from: str
    l1_to: str
    l1_date: str
    l1_flight: FlightOption
    l2_from: str
    l2_to: str
    l2_date: str
    l2_flight: FlightOption
    l3_from: str
    l3_to: str
    l3_date: str
    l3_flight: FlightOption
    l4_from: str
    l4_to: str
    l4_date: str
    l4_flight: FlightOption
