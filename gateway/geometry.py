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


def _local(lat0: float, lon0: float):
    """Equirectangular metres around (lat0, lon0): ample for a few kilometres."""
    k = math.pi * EARTH_RADIUS_M / 180
    c = math.cos(math.radians(lat0))
    to_xy = lambda lat, lon: ((lon - lon0) * k * c, (lat - lat0) * k)
    to_ll = lambda x, y: [lat0 + y / k, lon0 + x / (k * c)]
    return to_xy, to_ll


def sectors(reserve: list[list[float]], gateways: list[tuple[float, float]]) -> list[list[list[float]]]:
    """Split the reserve outline [[lat, lon], ...] between gateways: each point
    goes to its nearest gateway (Voronoi cells clipped to the reserve). Returns
    one [[lat, lon], ...] ring per gateway, in the gateways' order (empty if a
    gateway's cell misses the reserve)."""
    if not reserve or not gateways:
        return [[] for _ in gateways]
    to_xy, to_ll = _local(reserve[0][0], reserve[0][1])
    ring = [to_xy(lat, lon) for lat, lon in reserve]
    gws = [to_xy(lat, lon) for lat, lon in gateways]
    out = []
    for i, g in enumerate(gws):
        poly = ring
        for j, h in enumerate(gws):
            if i == j or not poly:
                continue
            # Keep the half-plane closer to g than to h: n.p <= c.
            n = (h[0] - g[0], h[1] - g[1])
            c = (h[0] ** 2 + h[1] ** 2 - g[0] ** 2 - g[1] ** 2) / 2
            side = lambda p: n[0] * p[0] + n[1] * p[1] - c
            clipped = []
            for k, cur in enumerate(poly):
                prev = poly[k - 1]
                sc, sp = side(cur), side(prev)
                if (sc <= 0) != (sp <= 0):
                    t = sp / (sp - sc)
                    clipped.append((prev[0] + t * (cur[0] - prev[0]), prev[1] + t * (cur[1] - prev[1])))
                if sc <= 0:
                    clipped.append(cur)
            poly = clipped
        out.append([to_ll(x, y) for x, y in poly])
    return out


def polygon_area_m2(ring: list[list[float]]) -> float:
    if len(ring) < 3:
        return 0.0
    to_xy, _ = _local(ring[0][0], ring[0][1])
    p = [to_xy(lat, lon) for lat, lon in ring]
    return abs(sum(p[i - 1][0] * p[i][1] - p[i][0] * p[i - 1][1] for i in range(len(p)))) / 2
