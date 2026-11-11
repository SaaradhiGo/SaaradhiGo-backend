"""Every number the Live Ops map depends on, named once.

Nothing here is a fare, a commission or a driver payment. These are display
and sampling choices; a change is a code review, not a maker-checker request.
"""

# --- Payload minimisation -------------------------------------------------
#: A driver dot: lat/lng to 5 decimals (about 1 m).
DOT_COORD_DECIMALS = 5
#: A trip's pickup and drop: 3 decimals (about 100 m), and no address.
TRIP_COORD_DECIMALS = 3

# --- Performance ----------------------------------------------------------
#: The snapshot never sends more dots (or trips) than this; past it the answer
#: says so and asks the operator to pick a city.
SNAPSHOT_DOT_CAP = 2000
SNAPSHOT_TRIP_CAP = 2000
#: A driver's position reaches an admin socket at most once per interval
#: (coalesced to the latest position inside the interval).
STREAM_FRAME_INTERVAL_SECONDS = 1.0
#: Socket scope and capabilities are re-read at most this often.
SCOPE_RECHECK_SECONDS = 60
#: City counts are pushed on the socket this often.
STREAM_STATUS_INTERVAL_SECONDS = 30
#: The console falls back to polling the snapshot at this interval.
POLL_FALLBACK_SECONDS = 10

# --- Single-use socket ticket ----------------------------------------------
TICKET_TTL_SECONDS = 30
TICKET_KEY_PREFIX = 'liveops:ticket:'

# --- WebSocket close codes -------------------------------------------------
CLOSE_NO_TICKET = 4001      # no ticket, an unknown one, an expired one or a used one
CLOSE_NO_CAPABILITY = 4003  # signed in, but holds neither liveops capability (or lost it)
CLOSE_UNAVAILABLE = 1013    # try again later: the ticket store could not answer

# --- Where the last position of every live driver lives ---------------------
#: Written beside the presence heartbeat. The geo index drops a driver who is
#: on a trip, so on-trip drivers' positions need a home of their own.
LIVEPOS_PREFIX = 'driver:livepos:'
LIVEPOS_TTL_SECONDS = 120

# --- Network status, ported from the Django `predictive_heatmaps` screen ----
#: No live drivers at all.
STATUS_OFFLINE = 'OFFLINE'
#: Unassigned requests outnumber idle drivers by more than this ratio.
STATUS_CRITICAL = 'CRITICAL'
CRITICAL_REQUEST_TO_DRIVER_RATIO = 2
#: ... or simply outnumber them.
STATUS_HIGH_DEMAND = 'HIGH DEMAND'
HIGH_DEMAND_REQUEST_TO_DRIVER_RATIO = 1
#: The Django screen called this OPTIMAL.
STATUS_NORMAL = 'NORMAL'

# --- Demand and supply heatmap --------------------------------------------
HEATMAP_WINDOWS = (15, 60, 1440)  # minutes
#: Lat/lng bucket size in degrees: about 1 km.
HEATMAP_CELL_DEGREES = 0.01
#: Aggregate-only callers never see a cell with fewer events than this
#: (events = trip requests + the most idle drivers seen at once).
HEATMAP_K_ANONYMITY = 3
#: Requests are compared with idle drivers per this many minutes: one request
#: per driver per quarter hour is balanced.
HEATMAP_DEMAND_BASELINE_MINUTES = 15
HEATMAP_CELL_CAP = 5000
#: Supply is sampled once a minute as counts per cell, never as driver ids.
SUPPLY_SAMPLE_RETENTION_HOURS = 26

# --- Trip states shown on the map ------------------------------------------
REQUESTED = 'requested'
ACTIVE_TRIP_STATES = ('requested', 'accepted', 'reached', 'in_progress')

# --- Audit ------------------------------------------------------------------
AUDIT_SESSION_OPENED = 'live_ops_session_opened'
AUDIT_SESSION_CLOSED = 'live_ops_session_closed'
AUDIT_TICKET_ISSUED = 'live_ops_ticket_issued'
AUDIT_SNAPSHOT_VIEWED = 'live_ops_snapshot_viewed'
AUDIT_HEATMAP_VIEWED = 'live_ops_heatmap_viewed'
