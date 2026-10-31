"""Demand and supply per ~1 km cell.

NOT PREDICTIVE
--------------
The Django screen this replaces was called "Predictive Heatmaps" and showed a
"predicted surge" computed from a three-line ratio. Nothing is predicted here
and nothing sets a price: the page says what was asked for and what was
available in the window just gone. (No fare or surge value is read or written.)

THE GRID
--------
Cell = floor(lat / 0.01), floor(lng / 0.01): about 1.1 km tall, narrower than
that wide at Hyderabad's latitude. Plain integer arithmetic in SQL; no H3, no
new dependency.

DEMAND is the trip requests whose pickup fell in the cell in the window
(`ride.Trip.requested_at`, whatever became of the trip: a request nobody
accepted is exactly the demand that matters).

SUPPLY is the average number of IDLE ONLINE drivers in the cell across the
window's samples (`SupplySample`, one per minute, counts only). A cell with
demand and no sample at all has supply 0.

SHADING: `demand_per_baseline` is requests per `HEATMAP_DEMAND_BASELINE_MINUTES`;
a cell is `short` when that exceeds the supply (the request rate beats the idle
drivers), `balanced` otherwise.

K-ANONYMITY
-----------
A caller who holds `liveops.aggregate` and not `liveops.read` (Analyst,
Marketing) never receives a cell with fewer than `HEATMAP_K_ANONYMITY` events,
where events = requests + the most idle drivers seen in the cell at once. A cell
that names one rider's pickup or one driver's parking spot is an individual
position wearing a grid reference. They also receive no coordinate except the
cell's own south-west corner (0.01 degrees, so no finer than the grid).
Callers who may read individuals see every cell in their reading scope.
"""

import math
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, F, Max, Sum
from django.db.models.functions import Floor
from django.utils import timezone

from .config import (
    HEATMAP_CELL_CAP, HEATMAP_CELL_DEGREES, HEATMAP_DEMAND_BASELINE_MINUTES,
    HEATMAP_K_ANONYMITY, HEATMAP_WINDOWS,
)
from .geo import Geography
from .snapshot import CityNotVisible, _zone_node_ids, visible_cities

#: 1 / cell size, an integer so the SQL stays integer arithmetic.
_PER_DEGREE = round(1 / HEATMAP_CELL_DEGREES)

SHORT, BALANCED = 'short', 'balanced'


class BadWindow(ValueError):
    pass


def parse_window(raw):
    try:
        minutes = int(raw) if raw not in (None, '') else HEATMAP_WINDOWS[0]
    except (TypeError, ValueError):
        raise BadWindow(raw)
    if minutes not in HEATMAP_WINDOWS:
        raise BadWindow(raw)
    return minutes


def cell_of(lat, lng):
    return math.floor(float(lat) * _PER_DEGREE), math.floor(float(lng) * _PER_DEGREE)


def corner_of(row, col):
    """South-west corner of a cell, to the grid's own precision."""
    return round(row * HEATMAP_CELL_DEGREES, 2), round(col * HEATMAP_CELL_DEGREES, 2)


def shade(demand, supply, window_minutes):
    """`short` when requests per baseline period outnumber idle drivers."""
    per_baseline = demand * HEATMAP_DEMAND_BASELINE_MINUTES / window_minutes
    return (SHORT if per_baseline > supply else BALANCED), per_baseline


def merge_cells(demand_rows, supply_rows, samples, window_minutes):
    """Combine the two sides into cells. Pure: the tests drive it directly.

    `demand_rows`: {(row, col): requests}; `supply_rows`: {(row, col): (sum, peak)};
    `samples`: how many sampling instants the window held.
    """
    cells = []
    for key in sorted(set(demand_rows) | set(supply_rows)):
        demand = demand_rows.get(key, 0)
        total, peak = supply_rows.get(key, (0, 0))
        supply = (total / samples) if samples else 0.0
        state, per_baseline = shade(demand, supply, window_minutes)
        south, west = corner_of(*key)
        cells.append({
            'row': key[0], 'col': key[1], 'cell_south': south, 'cell_west': west,
            'demand': demand, 'supply': round(supply, 2), 'peak_supply': peak,
            'demand_per_baseline': round(per_baseline, 2), 'shade': state,
            'events': demand + peak,
        })
    return cells


def suppress_small(cells, k=HEATMAP_K_ANONYMITY):
    """`(kept, suppressed_count)`: cells with fewer than `k` events dropped."""
    kept = [c for c in cells if c['events'] >= k]
    return kept, len(cells) - len(kept)


def build_heatmap(scope, *, city_code='', window=None, geography=None, now=None):
    from servers.ride.models import Trip

    from .models import SupplySample

    window = parse_window(window)
    now = now or timezone.now()
    since = now - timedelta(minutes=window)
    geography = geography or Geography.get()
    cities = visible_cities(scope, geography, _zone_node_ids())
    selected = None
    if city_code:
        selected = next((c for c in cities if c.code == city_code), None)
        if selected is None:
            raise CityNotVisible(city_code)
    shown = [selected] if selected is not None else cities

    # A city's cells are suppressed unless the caller may read individuals
    # THERE: holding `liveops.read` in Hyderabad does not unmask Austin.
    open_ids = [c.id for c in shown if scope.reads(c)]
    guarded_ids = [c.id for c in shown if not scope.reads(c)]

    def side(city_ids):
        demand = defaultdict(int)
        rows = (Trip.objects
                .filter(requested_at__gte=since, zone__geo_node_id__in=city_ids)
                .annotate(r=Floor(F('pickup_lat') * _PER_DEGREE), c=Floor(F('pickup_long') * _PER_DEGREE))
                .values('r', 'c').annotate(n=Count('id')))
        for row in rows:
            demand[(int(row['r']), int(row['c']))] += row['n']
        supply = {}
        for row in (SupplySample.objects
                    .filter(sampled_at__gte=since, city_id__in=city_ids)
                    .values('cell_row', 'cell_col').annotate(total=Sum('drivers'), peak=Max('drivers'))):
            supply[(row['cell_row'], row['cell_col'])] = (row['total'], row['peak'])
        samples = (SupplySample.objects.filter(sampled_at__gte=since, city_id__in=city_ids)
                   .values('sampled_at').distinct().count())
        return merge_cells(demand, supply, samples, window)

    cells, suppressed = [], 0
    if open_ids:
        cells += side(open_ids)
    if guarded_ids:
        kept, dropped = suppress_small(side(guarded_ids))
        cells += kept
        suppressed += dropped

    truncated = len(cells) > HEATMAP_CELL_CAP
    cells = sorted(cells, key=lambda c: -c['events'])[:HEATMAP_CELL_CAP]
    return {
        'generated_at': now.isoformat().replace('+00:00', 'Z'),
        'title': 'Demand and supply',
        'mode': scope.mode,
        'city': selected.code if selected else None,
        'window_minutes': window,
        'windows': list(HEATMAP_WINDOWS),
        'cell_degrees': HEATMAP_CELL_DEGREES,
        'baseline_minutes': HEATMAP_DEMAND_BASELINE_MINUTES,
        'k_anonymity': HEATMAP_K_ANONYMITY if guarded_ids else None,
        'suppressed_cells': suppressed,
        'truncated': truncated,
        'cells': cells,
    }
