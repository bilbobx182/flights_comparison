import json
import os
from collections import OrderedDict
from typing import Optional, List, Dict, Any
from models import FlightOption

class FlightCache:
    """
    LRU in-memory cache backed by optional persistent JSON storage.
    Stores query results, including empty results ('no flights') to prevent re-querying.
    """
    def __init__(self, cache_file: Optional[str] = "flights_cache.json", max_lru_size: int = 1000):
        self.cache_file = cache_file
        self.max_lru_size = max_lru_size
        self._cache: OrderedDict[str, List[Dict[str, Any]]] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self._load_from_disk()

    def _make_key(self, from_airport: str, to_airport: str, date: str) -> str:
        return f"{from_airport.upper()}->{to_airport.upper()}@{date}"

    def _load_from_disk(self):
        if self.cache_file and os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        self._cache[k] = v
            except Exception as e:
                print(f"[Cache] Warning: Could not load cache file {self.cache_file}: {e}")

    def save_to_disk(self):
        if not self.cache_file:
            return
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(dict(self._cache), f, indent=2)
        except Exception as e:
            print(f"[Cache] Warning: Could not save cache file {self.cache_file}: {e}")

    def get(self, from_airport: str, to_airport: str, date: str) -> Optional[List[FlightOption]]:
        key = self._make_key(from_airport, to_airport, date)
        if key in self._cache:
            self.hits += 1
            # Move to end for LRU
            self._cache.move_to_end(key)
            raw_list = self._cache[key]
            return [FlightOption.from_dict(d) for d in raw_list]
        self.misses += 1
        return None

    def set(self, from_airport: str, to_airport: str, date: str, options: List[FlightOption]):
        key = self._make_key(from_airport, to_airport, date)
        raw_list = [opt.to_dict() for opt in options]
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = raw_list
        if len(self._cache) > self.max_lru_size:
            self._cache.popitem(last=False)
        self.save_to_disk()

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        hit_ratio = (self.hits / total * 100.0) if total > 0 else 0.0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "total_lookups": total,
            "hit_ratio_percent": round(hit_ratio, 1),
            "cached_routes_count": len(self._cache),
        }
