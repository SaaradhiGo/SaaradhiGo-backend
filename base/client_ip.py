"""The caller's IP address, taken from the part of the proxy chain we trust (AUDIT 27.78, M1).

WHY THIS EXISTS
---------------
Seven places read `X-Forwarded-For` and took its FIRST entry. That entry is
whatever the client wrote: anyone can send `X-Forwarded-For: 1.2.3.4` and every
proxy on the way APPENDS to it, so the left end is the one part of the header
an attacker controls. The console login lockout keyed its per-IP counter on it
(a fresh fake address per attempt = no per-IP lockout), and the audit log
recorded it as the operator's address (a forged address in the evidence).

HOW THE CHAIN IS READ
---------------------
Each proxy appends the address it received the request from. Reading from the
RIGHT, therefore, every entry was written by infrastructure until we pass the
last proxy we trust; the entry just left of it is the client as our edge saw
it, and anything further left is unverified client input and is ignored.

  * `TRUSTED_PROXY_COUNT` (env, default 1) is the number of proxies in front of
    the app that append to the header. Railway's edge is one: it appends the
    address that connected to it, so the client is the right-most entry. If a
    CDN is ever put in front of Railway, this becomes 2.
  * Right-most entries in a private, CGNAT (`100.64/10`), loopback or
    link-local network are infrastructure hops inside the
    platform's network and are skipped first. A client on the internet cannot
    put anything to the RIGHT of what our edge appends, so skipping them is
    safe, and it keeps the answer stable when the platform adds an internal
    hop of its own (the guest maps throttle once saw one that changed per
    request, commit 7b7f45d).
  * No header, or nothing usable left: `REMOTE_ADDR`.
"""

import ipaddress

from django.conf import settings


def trusted_proxy_count():
    try:
        return max(0, int(getattr(settings, 'TRUSTED_PROXY_COUNT', 1)))
    except (TypeError, ValueError):
        return 1


def _parse(value):
    value = (value or '').strip().strip('"')
    if value.startswith('[') and ']' in value:          # [v6]:port
        value = value[1:value.index(']')]
    elif value.count(':') == 1:                          # v4:port
        value = value.split(':', 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


#: Networks a platform hop has: private, CGNAT, loopback, link-local, ULA.
_INTERNAL = tuple(ipaddress.ip_network(n) for n in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10',
    '127.0.0.0/8', '169.254.0.0/16', '::1/128', 'fc00::/7', 'fe80::/10',
))


def _is_internal(ip):
    return any(ip.version == net.version and ip in net for net in _INTERNAL)


def client_ip(request_or_meta, default=None):
    """The client address as a string, or `default` when there is none.

    Accepts a request (Django or DRF) or a META-like dict, so the ASGI
    callers that build a META by hand use the same rule.
    """
    meta = request_or_meta if isinstance(request_or_meta, dict) else getattr(
        request_or_meta, 'META', None) or {}
    remote = _parse(meta.get('REMOTE_ADDR'))
    forwarded = meta.get('HTTP_X_FORWARDED_FOR') or ''
    hops = [_parse(part) for part in forwarded.split(',')] if forwarded.strip() else []
    count = trusted_proxy_count()
    if hops and count:
        # Unparseable entries are dropped; they cannot be an address anyway.
        chain = [ip for ip in hops if ip is not None]
        while chain and _is_internal(chain[-1]):
            chain.pop()
        # The trusted proxies wrote the last `count` entries; the client is
        # the first of those (the edge appends what connected to it).
        if len(chain) >= count:
            return str(chain[len(chain) - count])
    if remote is not None:
        return str(remote)
    return default
