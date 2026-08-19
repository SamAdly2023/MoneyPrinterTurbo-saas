"""
Visitor analytics for the public marketing pages (landing/login/privacy/terms).

Modeled after the "Visitor Insights" plugin folder dropped into this repo:
session + pageview tracking, referrer/UTM traffic-source classification, and
IP geolocation via ip-api.com's free, keyless endpoint. Deliberately does NOT
include that tool's optional "skip trace" identity-lookup feature - resolving
an anonymous visitor's real name/contact details via a third-party data
broker carries real privacy/legal exposure (GDPR/CCPA and similar) that's a
business decision to make explicitly with legal review, not something to
ship by default alongside ordinary traffic analytics.
"""

import csv
import io

import requests
from loguru import logger

from app.services import firestore_db

_AD_SOURCE_HINTS = ("google", "googleads", "adwords", "gclid")
_META_SOURCE_HINTS = ("facebook", "instagram", "fb", "ig")
_SEARCH_ENGINE_DOMAINS = ("google.", "bing.com", "duckduckgo.com", "yahoo.com")
_SOCIAL_DOMAINS = (
    "facebook.com", "instagram.com", "tiktok.com", "t.co", "twitter.com",
    "x.com", "linkedin.com", "pinterest.com", "reddit.com",
)


def classify_source(referrer: str, utm_source: str, utm_medium: str) -> str:
    referrer = (referrer or "").lower()
    utm_source = (utm_source or "").lower()
    utm_medium = (utm_medium or "").lower()

    is_paid = utm_medium in ("cpc", "ppc", "paid", "paidsocial", "ad", "ads")
    if is_paid and any(s in utm_source for s in _AD_SOURCE_HINTS):
        return "Google Ads"
    if is_paid and any(s in utm_source for s in _META_SOURCE_HINTS):
        return "Facebook/Instagram Ads"
    if any(d in referrer for d in _SEARCH_ENGINE_DOMAINS):
        return "Organic search"
    if any(d in referrer for d in _SOCIAL_DOMAINS):
        return "Organic social"
    if not referrer:
        return "Direct"
    return "Referral"


def parse_device(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "ipad" in ua or "tablet" in ua:
        return "Tablet"
    if "mobile" in ua or "android" in ua or "iphone" in ua:
        return "Mobile"
    return "Desktop"


def geo_lookup(ip: str) -> dict:
    """ip-api.com's free tier - no key, ~45 req/min. Called once per new
    session only (not per pageview), well within that limit even under load."""
    if not ip or ip in ("127.0.0.1", "::1", ""):
        return {}
    try:
        resp = requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,country,regionName,city,isp,proxy,hosting"},
            timeout=4,
        )
        data = resp.json()
        if data.get("status") != "success":
            return {}
        return {
            "country": data.get("country", ""),
            "region": data.get("regionName", ""),
            "city": data.get("city", ""),
            "isp": data.get("isp", ""),
            "is_proxy": bool(data.get("proxy")),
            "is_hosting": bool(data.get("hosting")),
        }
    except Exception as e:  # noqa: BLE001 - geo is a nice-to-have, never block tracking
        logger.warning(f"visitor geo lookup failed for {ip}: {e}")
        return {}


def record_pageview(
    session_id: str, ip: str, user_agent: str, path: str, title: str,
    referrer: str, utm: dict,
) -> None:
    session = firestore_db.get_visitor_session(session_id)
    if session is None:
        geo = geo_lookup(ip)
        firestore_db.create_visitor_session(session_id, {
            "referrer": (referrer or "")[:500],
            "utm_source": utm.get("utm_source", ""),
            "utm_medium": utm.get("utm_medium", ""),
            "utm_campaign": utm.get("utm_campaign", ""),
            "utm_term": utm.get("utm_term", ""),
            "utm_content": utm.get("utm_content", ""),
            "source_type": classify_source(referrer, utm.get("utm_source", ""), utm.get("utm_medium", "")),
            "device_type": parse_device(user_agent),
            "landing_path": path,
            **geo,
        })
    firestore_db.add_visitor_pageview(session_id, path, title)


def summarize(sessions: list) -> dict:
    total_sessions = len(sessions)
    total_pageviews = sum(s.get("pageview_count", 0) for s in sessions)
    by_source, by_device, by_country = {}, {}, {}
    for s in sessions:
        src = s.get("source_type") or "Direct"
        by_source[src] = by_source.get(src, 0) + 1
        dev = s.get("device_type") or "Desktop"
        by_device[dev] = by_device.get(dev, 0) + 1
        country = s.get("country")
        if country:
            by_country[country] = by_country.get(country, 0) + 1
    top_countries = sorted(by_country.items(), key=lambda kv: -kv[1])[:6]
    return {
        "total_sessions": total_sessions,
        "total_pageviews": total_pageviews,
        "by_source": by_source,
        "by_device": by_device,
        "top_countries": [{"country": c, "count": n} for c, n in top_countries],
    }


_CSV_FIELDS = [
    "session_id", "first_seen", "last_seen", "pageview_count", "source_type",
    "referrer", "utm_source", "utm_medium", "utm_campaign", "landing_path",
    "device_type", "country", "region", "city", "isp", "is_proxy", "is_hosting",
]


def export_csv(sessions: list) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for s in sessions:
        writer.writerow(s)
    return buf.getvalue()
