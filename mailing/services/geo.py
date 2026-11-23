"""Client IP extraction and offline GeoIP lookup for tracking events.

Production proxy chain (verified 2026-11-23 on relay.datatalks.club):
  client -> CloudFront (E3L9KUTE15WFJF, Managed-AllViewer) -> Coolify Traefik -> gunicorn
CloudFront appends the viewer IP to X-Forwarded-For and Traefik appends the
CloudFront edge IP, so the real client IP is the 2nd-from-right entry
(RELAY_TRUSTED_PROXY_HOPS=2). Never trust the leftmost X-Forwarded-For entry.

Offline GeoIP uses a standard MaxMind MMDB file (DB-IP City Lite CC BY 4.0,
https://db-ip.com, baked into the container image, or MaxMind GeoLite2 via
RELAY_GEOIP_DB_PATH). All lookup failures are non-fatal and return (None, None).
"""

import ipaddress
import logging
import os
import threading

import maxminddb
from django.conf import settings

logger = logging.getLogger(__name__)

_reader_lock = threading.Lock()
_cached_db_path = None
_cached_reader = None
_warned_missing_path = None

_CF_SPECIAL_COUNTRIES = frozenset({"XX", "T1"})


def _parse_ip(raw):
    if not raw or not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    # Handle [ipv6]:port or ipv4:port if a proxy ever includes a port.
    if candidate.startswith("[") and "]" in candidate:
        host = candidate[1 : candidate.index("]")]
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            return None
    if candidate.count(":") == 1:
        host, _port = candidate.rsplit(":", 1)
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            return None
    return None


def _is_non_public_ip(ip_str):
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _came_via_trusted_proxy(request):
    hops = getattr(settings, "RELAY_TRUSTED_PROXY_HOPS", 2)
    if hops <= 0:
        return False
    remote = _parse_ip(request.META.get("REMOTE_ADDR", ""))
    if not remote or not _is_non_public_ip(remote):
        return False
    forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
    return len(forwarded) >= hops


def client_ip(request):
    """Extract the real client IP behind trusted proxies.

    - If RELAY_TRUST_CLOUDFLARE_HEADERS is enabled and the request arrived via
      trusted proxies, prefer CF-Connecting-IP.
    - Otherwise take the Nth-from-right entry of X-Forwarded-For where
      N = RELAY_TRUSTED_PROXY_HOPS (must have at least N entries).
    - Never trust the leftmost X-Forwarded-For entry when fewer than N hops are
      present; fall back to REMOTE_ADDR.
    """
    if getattr(settings, "RELAY_TRUST_CLOUDFLARE_HEADERS", False) and _came_via_trusted_proxy(request):
        cf_ip = _parse_ip(request.META.get("HTTP_CF_CONNECTING_IP", ""))
        if cf_ip:
            return cf_ip

    hops = getattr(settings, "RELAY_TRUSTED_PROXY_HOPS", 2)
    if hops > 0:
        forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
        if len(forwarded) >= hops:
            candidate = _parse_ip(forwarded[-hops])
            if candidate:
                return candidate

    return _parse_ip(request.META.get("REMOTE_ADDR", ""))


def _get_geoip_reader():
    global _cached_db_path, _cached_reader, _warned_missing_path

    db_path = (getattr(settings, "RELAY_GEOIP_DB_PATH", "") or "").strip()
    if not db_path:
        return None

    with _reader_lock:
        if _cached_db_path == db_path and _cached_reader is not None:
            return _cached_reader
        if _cached_reader is not None and _cached_db_path != db_path:
            try:
                _cached_reader.close()
            except Exception:
                pass
            _cached_reader = None
            _cached_db_path = None
        if not os.path.isfile(db_path):
            if _warned_missing_path != db_path:
                logger.warning("geoip_database_missing path=%s", db_path)
                _warned_missing_path = db_path
            return None
        try:
            reader = maxminddb.open_database(db_path, mode=maxminddb.MODE_MMAP)
        except Exception:
            logger.exception("geoip_database_open_failed path=%s", db_path)
            return None
        _cached_db_path = db_path
        _cached_reader = reader
        return _cached_reader


def reset_geoip_reader_cache():
    """Close and clear the cached MMDB reader (used in tests)."""
    global _cached_db_path, _cached_reader, _warned_missing_path
    with _reader_lock:
        if _cached_reader is not None:
            try:
                _cached_reader.close()
            except Exception:
                pass
        _cached_reader = None
        _cached_db_path = None
        _warned_missing_path = None


def _normalize_country(value):
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if len(code) == 2 and code.isascii() and code.isalpha() and code not in _CF_SPECIAL_COUNTRIES:
        return code
    return None


def _normalize_city(value):
    if not isinstance(value, str):
        return None
    city = value.strip()[:128]
    return city or None


def lookup_geo(ip):
    """Resolve (country, city) for a public IP from the local MMDB database.

    Never raises; returns (None, None) for private/invalid IPs, missing DB, or
    any lookup error.
    """
    parsed = _parse_ip(ip)
    if not parsed or _is_non_public_ip(parsed):
        return None, None
    reader = _get_geoip_reader()
    if reader is None:
        return None, None
    try:
        record = reader.get(parsed)
    except Exception:
        logger.debug("geoip_lookup_failed ip=%s", parsed, exc_info=True)
        return None, None
    if not isinstance(record, dict):
        return None, None

    country = _normalize_country(
        (record.get("country") or {}).get("iso_code")
        or (record.get("registered_country") or {}).get("iso_code")
    )
    city_names = (record.get("city") or {}).get("names") or {}
    city = _normalize_city(city_names.get("en"))
    return country, city


def geo_for_request(request, ip=None):
    """Resolve (country, city) for an incoming request. Never raises."""
    try:
        cf_country = None
        if getattr(settings, "RELAY_TRUST_CLOUDFLARE_HEADERS", False) and _came_via_trusted_proxy(request):
            cf_country = _normalize_country(request.META.get("HTTP_CF_IPCOUNTRY", ""))

        resolved_ip = ip if ip is not None else client_ip(request)
        db_country, db_city = lookup_geo(resolved_ip)
        return cf_country or db_country, db_city
    except Exception:
        logger.debug("geo_for_request_failed", exc_info=True)
        return None, None
