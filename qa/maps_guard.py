"""Load and e2e runs never call real Google Maps (17 Nov, PROD round 2).

The 16 Nov pilot load on QA made about 1,400 real Routes calls: every quote asked Google for a
route. Every load script now calls require_fake_maps(<backend origin>) before it starts. The run
stops unless the target's /healthz reports "maps": "fake" (MAPS_ADAPTER=fake on that deployment)
and the run's own environment holds no GOOGLE_MAPS_API_KEY.

To load-test QA: set MAPS_ADAPTER=fake on QA's backend and celery, wait for the redeploy, run,
then remove it.
"""
import json
import os
import ssl
import sys
import urllib.request


def require_fake_maps(origin):
    origin = origin.rstrip('/')
    if origin.endswith('/api/v1'):
        origin = origin[: -len('/api/v1')]
    if (os.environ.get('GOOGLE_MAPS_API_KEY') or '').strip():
        sys.exit('refusing to run: GOOGLE_MAPS_API_KEY is set in this shell. Load runs never hold a real Maps key.')
    try:
        with urllib.request.urlopen(origin + '/healthz', timeout=20, context=ssl.create_default_context()) as r:
            maps = json.load(r).get('maps')
    except Exception as exc:  # noqa: BLE001 - any failure stops the run
        sys.exit(f'refusing to run: cannot read {origin}/healthz ({type(exc).__name__}).')
    if maps != 'fake':
        sys.exit(f'refusing to run: {origin} reports maps={maps!r}, not "fake". Set MAPS_ADAPTER=fake on the '
                 f'target first; a load run must never call real Google Maps.')
    print(f'maps guard: {origin} uses the fake maps adapter', flush=True)
