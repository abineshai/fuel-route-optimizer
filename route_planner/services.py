import csv
import math
import os
import requests
from django.conf import settings

# ── Fuel price loader ────────────────────────────────────────────────────────

def load_fuel_prices() -> dict[str, float]:
    """Return {state_name: price_per_gallon (Regular)}."""
    prices = {}
    csv_path = os.path.join(settings.BASE_DIR, "fuel_prices.csv")
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                prices[row["OPIS Truckstop"].strip()] = float(row["Regular"])
            except (KeyError, ValueError):
                pass
    return prices


# ── Geocoding (Nominatim – free, no key needed) ──────────────────────────────

def geocode(location: str) -> tuple[float, float]:
    """Return (lat, lon) for a location string using Nominatim."""
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": location + ", USA",
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
    }
    headers = {"User-Agent": "FuelRouteApp/1.0"}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Could not geocode location: '{location}'")
    return float(results[0]["lat"]), float(results[0]["lon"])


# ── Reverse geocoding: (lat, lon) → state name ───────────────────────────────

def reverse_geocode_state(lat: float, lon: float) -> str:
    """Return the US state name for a lat/lon coordinate."""
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": lat, "lon": lon, "format": "json"}
    headers = {"User-Agent": "FuelRouteApp/1.0"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        return data.get("address", {}).get("state", "Unknown")
    except Exception:
        return "Unknown"


# ── Routing (OSRM public instance – free, no key needed) ─────────────────────

def get_route(start_lat: float, start_lon: float,
              end_lat: float, end_lon: float) -> dict:
    """
    Call OSRM to get a driving route and return a dict with:
      - distance_miles
      - duration_seconds
      - waypoints: list of (lat, lon) sampled every ~50 miles along the route
      - geometry: encoded polyline string
    """
    url = (
        f"http://router.project-osrm.org/route/v1/driving/"
        f"{start_lon},{start_lat};{end_lon},{end_lat}"
        f"?overview=full&geometries=geojson&steps=false"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != "Ok":
        raise ValueError(f"OSRM error: {data.get('message', 'Unknown')}")

    route = data["routes"][0]
    distance_m = route["distance"]
    distance_miles = distance_m / 1609.344

    # Sample waypoints roughly every 50 miles
    coords = route["geometry"]["coordinates"]  # [[lon, lat], ...]
    total_points = len(coords)
    miles_per_point = distance_miles / max(total_points - 1, 1)
    sample_every = max(1, int(50 / miles_per_point))

    waypoints = [(c[1], c[0]) for i, c in enumerate(coords)
                 if i % sample_every == 0 or i == total_points - 1]

    return {
        "distance_miles": round(distance_miles, 2),
        "duration_seconds": route["duration"],
        "waypoints": waypoints,
        "geometry": route["geometry"],
    }


# ── Haversine distance between two lat/lon points ────────────────────────────

def haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Fuel stop optimiser ───────────────────────────────────────────────────────

MAX_RANGE_MILES = 500
MPG = 10
TANK_CAPACITY = MAX_RANGE_MILES / MPG  # 50 gallons


def plan_fuel_stops(waypoints: list[tuple[float, float]],
                    total_distance_miles: float,
                    fuel_prices: dict[str, float]) -> dict:
    """
    Given route waypoints, determine optimal (cheapest) fuel stops.

    Strategy (greedy look-ahead):
      - Track current fuel level (start full).
      - At each waypoint, look ahead: if the cheapest reachable station
        within range is cheaper than the current one, wait; otherwise
        fill up here.
      - Always refuel if we cannot reach the next station without running out.

    Returns a list of fuel stop dicts and total cost.
    """
    if not waypoints:
        return {"stops": [], "total_cost_usd": 0.0}

    # Annotate each waypoint with cumulative distance and state
    annotated = []
    cum_dist = 0.0
    for i, (lat, lon) in enumerate(waypoints):
        if i > 0:
            prev_lat, prev_lon = waypoints[i - 1]
            cum_dist += haversine_miles(prev_lat, prev_lon, lat, lon)
        state = reverse_geocode_state(lat, lon)
        price = fuel_prices.get(state, 3.50)  # fallback average
        annotated.append({
            "index": i,
            "lat": lat,
            "lon": lon,
            "cum_dist": cum_dist,
            "state": state,
            "price_per_gallon": price,
        })

    stops = []
    fuel_level = TANK_CAPACITY  # start full
    current_dist = 0.0
    total_cost = 0.0

    i = 0
    while i < len(annotated) - 1:
        wp = annotated[i]
        next_wp = annotated[i + 1]

        dist_to_next = next_wp["cum_dist"] - wp["cum_dist"]
        fuel_to_next = dist_to_next / MPG

        # How far can we go from here?
        max_reachable_dist = wp["cum_dist"] + fuel_level * MPG

        # Find reachable waypoints within range from here
        reachable = [a for a in annotated[i:] if a["cum_dist"] <= max_reachable_dist]
        cheapest_ahead = min(reachable, key=lambda x: x["price_per_gallon"])

        # Must we stop here? (Can't reach next cheapest without refueling)
        must_stop = fuel_level < fuel_to_next + 0.5  # 0.5gal safety margin

        # Is it cheaper to stop here than anywhere reachable ahead?
        should_stop = (
            wp["price_per_gallon"] <= cheapest_ahead["price_per_gallon"]
            or must_stop
        )

        if should_stop and i > 0:  # Don't mark start as a stop
            gallons_needed = TANK_CAPACITY - fuel_level
            if gallons_needed > 0.5:  # only meaningful fill-ups
                cost = gallons_needed * wp["price_per_gallon"]
                total_cost += cost
                fuel_level = TANK_CAPACITY
                stops.append({
                    "lat": wp["lat"],
                    "lon": wp["lon"],
                    "state": wp["state"],
                    "price_per_gallon": round(wp["price_per_gallon"], 3),
                    "gallons_added": round(gallons_needed, 2),
                    "cost_usd": round(cost, 2),
                    "miles_from_start": round(wp["cum_dist"], 1),
                })

        fuel_level -= fuel_to_next
        i += 1

    # Always account for fuel used on last segment
    total_gallons = total_distance_miles / MPG
    # Recalculate total cost from stops (already accumulated)
    return {
        "stops": stops,
        "total_cost_usd": round(total_cost, 2),
        "total_gallons_used": round(total_gallons, 2),
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def compute_fuel_route(start: str, finish: str) -> dict:
    fuel_prices = load_fuel_prices()

    start_lat, start_lon = geocode(start)
    end_lat, end_lon = geocode(finish)

    route_data = get_route(start_lat, start_lon, end_lat, end_lon)

    fuel_plan = plan_fuel_stops(
        route_data["waypoints"],
        route_data["distance_miles"],
        fuel_prices,
    )

    hours = int(route_data["duration_seconds"] // 3600)
    minutes = int((route_data["duration_seconds"] % 3600) // 60)

    return {
        "start": {"name": start, "lat": start_lat, "lon": start_lon},
        "finish": {"name": finish, "lat": end_lat, "lon": end_lon},
        "route": {
            "distance_miles": route_data["distance_miles"],
            "estimated_duration": f"{hours}h {minutes}m",
            "geometry": route_data["geometry"],
        },
        "fuel_plan": {
            "vehicle_range_miles": MAX_RANGE_MILES,
            "fuel_efficiency_mpg": MPG,
            "fuel_stops": fuel_plan["stops"],
            "total_fuel_stops": len(fuel_plan["stops"]),
            "total_gallons_used": fuel_plan["total_gallons_used"],
            "total_fuel_cost_usd": fuel_plan["total_cost_usd"],
        },
    }
