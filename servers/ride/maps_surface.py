"""Which client a request comes from, for the Maps meter (9 Nov).

The web and the apps send `X-SG-Surface: rider_web | rider_app | driver_app`; every Maps call
made while serving the request is counted against that surface. Anything else is `backend`.
"""

from servers.ride import maps_meter


class MapsSurfaceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        surface = (request.headers.get('X-SG-Surface') or '').strip().lower()
        if surface not in maps_meter.SURFACES:
            return self.get_response(request)
        with maps_meter.context(surface=surface):
            return self.get_response(request)
