"""Meter-based provisional layout on a mean-radius sphere; no telemetry claims."""
import math

EARTH_RADIUS_M = 6371008.8


def destination(lat: float, lon: float, distance_m: float, bearing_deg: float) -> dict[str, float]:
    """Geodesic destination; bearing clockwise from true north."""
    phi, lam, bearing = map(math.radians, (lat, lon, bearing_deg))
    angle = distance_m / EARTH_RADIUS_M
    dest_phi = math.asin(math.sin(phi) * math.cos(angle)
                         + math.cos(phi) * math.sin(angle) * math.cos(bearing))
    dest_lam = lam + math.atan2(math.sin(bearing) * math.sin(angle) * math.cos(phi),
                               math.cos(angle) - math.sin(phi) * math.sin(dest_phi))
    return {"lat": math.degrees(dest_phi), "lon": (math.degrees(dest_lam) + 180) % 360 - 180}


def offset_position(lat: float, lon: float, east_m: float, north_m: float) -> dict[str, float]:
    return destination(lat, lon, math.hypot(east_m, north_m),
                       math.degrees(math.atan2(east_m, north_m)))


def grid_positions(lat: float, lon: float) -> list[dict[str, float]]:
    """Nine cell centres, north-to-south then west-to-east, in a 100 m square."""
    return [offset_position(lat, lon, (col - 1) * 100 / 3, (1 - row) * 100 / 3)
            for row in range(3) for col in range(3)]


def grid_boundary(lat: float, lon: float) -> list[list[float]]:
    return [list(offset_position(lat, lon, e, n).values())
            for e, n in [(-50, 50), (50, 50), (50, -50), (-50, -50)]]
