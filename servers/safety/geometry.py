"""Local geometry for route deviation. No network, no dependencies.

`decode_polyline` reads Google's encoded polyline format (precision 5), the
same string `ride.maps_utils.google_directions` returns in
`routes[0].overview_polyline.points`.

`distance_to_polyline_m` is the distance from a point to the nearest point on
the line, in metres, using a local flat projection around the point. Over the
few kilometres a trip spans the error of that projection is far below the GPS
noise the thresholds already sit above; it keeps the evaluator a pure
function of numbers it already holds.

COST
----
O(segments) per point. The evaluator walks a trip's newest points first and
stops at the first one that is back on the route, so a trip that is on its
route costs one pass over its segments.
"""

import math

EARTH_RADIUS_M = 6371008.8


def decode_polyline(encoded):
    """`[(lat, lng), ...]` from an encoded polyline. Empty for nothing/garbage."""
    if not encoded:
        return []
    points, index, lat, lng = [], 0, 0, 0
    length = len(encoded)
    try:
        while index < length:
            for axis in (0, 1):
                shift = result = 0
                while True:
                    byte = ord(encoded[index]) - 63
                    index += 1
                    result |= (byte & 0x1F) << shift
                    shift += 5
                    if byte < 0x20:
                        break
                delta = ~(result >> 1) if result & 1 else result >> 1
                if axis == 0:
                    lat += delta
                else:
                    lng += delta
            points.append((lat / 1e5, lng / 1e5))
    except (IndexError, TypeError):
        return []
    return points


def _project(lat, lng, lat0, lng0, cos0):
    x = math.radians(lng - lng0) * cos0 * EARTH_RADIUS_M
    y = math.radians(lat - lat0) * EARTH_RADIUS_M
    return x, y


def distance_to_polyline_m(lat, lng, points):
    """Metres from (lat, lng) to the nearest point on the polyline; None if
    the line has no points."""
    if not points:
        return None
    lat, lng = float(lat), float(lng)
    cos0 = math.cos(math.radians(lat))
    prev = _project(points[0][0], points[0][1], lat, lng, cos0)
    if len(points) == 1:
        return math.hypot(*prev)
    best = float('inf')
    for p_lat, p_lng in points[1:]:
        cur = _project(p_lat, p_lng, lat, lng, cos0)
        ax, ay = prev
        bx, by = cur
        dx, dy = bx - ax, by - ay
        seg = dx * dx + dy * dy
        # The point is the origin of the projection: project it onto A->B.
        t = 0.0 if seg == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg))
        d = math.hypot(ax + t * dx, ay + t * dy)
        if d < best:
            best = d
        prev = cur
    return best
