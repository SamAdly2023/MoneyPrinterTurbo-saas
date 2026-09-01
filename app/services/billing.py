"""Live-site paywall: PayPal credit purchases + the Auto Mode subscription.

Two independent PayPal products:

- **Credits** (PayPal Orders v2, one-time payment). A user buys a fixed
  package; the server creates a PayPal Order, the client approves it via the
  PayPal JS SDK, and the server captures it and grants credits - the client
  side of the flow is never trusted to grant credits on its own, only a
  verified server-to-server capture does (see capture_order below).
- **Auto Mode** (PayPal Subscriptions, $29/month, one Billing Plan shared by
  every subscriber). Unlocks the *ability* to use Auto Mode for that
  account - it does not make videos free. Every video Auto Mode generates
  still spends 1 credit from the same balance manual generation uses.
  Subscription state is only ever changed by a verified webhook event
  (see verify_webhook_signature), never by the client-side redirect-back,
  since cancellations/failed renewals happen entirely on PayPal's side with
  no client present to tell the app about it.

Nothing here runs on the local desktop app. `live_billing_enabled()` is the
one switch everything above this module checks first - it's keyed off the
data backend rather than a deployment flag, because the render-dispatch-based
signal this app used to use (Cloud Run vs. not) stopped meaning "hosted vs.
local" once the live site moved to cPanel and started rendering in-process
just like the local app does. SQLite is only ever the local desktop app's
backend; MySQL/Postgres are only ever a real hosted deployment's.
"""

import base64
import os
import time

import requests
from loguru import logger

from app.services import firestore_db

# Cached per-process; PayPal access tokens are valid for several hours, so
# re-fetching one on every single API call would be wasteful.
_token_cache = {"token": "", "expires_at": 0.0}


def live_billing_enabled() -> bool:
    return firestore_db.backend_name() in ("mysql", "postgres")


def _global() -> dict:
    return firestore_db.get_global_settings()


def paypal_client_id() -> str:
    """Public by design - this is what the PayPal JS SDK is loaded with
    client-side. Only the secret (never returned here) is sensitive."""
    return _global().get("paypal_client_id", "")


def _api_base() -> str:
    mode = (_global().get("paypal_mode") or "sandbox").strip().lower()
    return (
        "https://api-m.paypal.com"
        if mode == "live"
        else "https://api-m.sandbox.paypal.com"
    )


def _access_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

    settings = _global()
    client_id = settings.get("paypal_client_id", "")
    client_secret = settings.get("paypal_client_secret", "")
    if not client_id or not client_secret:
        raise ValueError("PayPal is not configured. Ask the admin to add credentials in Settings.")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        f"{_api_base()}/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"PayPal auth failed ({resp.status_code}): {resp.text[:300]}")
    tok = resp.json()
    _token_cache["token"] = tok["access_token"]
    # Refresh a minute early rather than exactly on expiry.
    _token_cache["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    return _token_cache["token"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------- #
# Credit packages - admin-editable price, fixed tiers with volume bonuses.
# Bonus credits cost nothing extra to grant since this app's own marginal
# cost per video is near $0 on cPanel; they're a free lever to encourage
# bigger purchases, not a real cost to the business.
# --------------------------------------------------------------------------- #
PACKAGES = {
    "10": {"credits": 10, "label": "10 credits"},
    "25": {"credits": 28, "label": "25 credits (+3 bonus)"},
    "50": {"credits": 60, "label": "50 credits (+10 bonus)"},
    "100": {"credits": 130, "label": "100 credits (+30 bonus)"},
}


def package_price(package_id: str) -> float:
    pkg = PACKAGES.get(package_id)
    if not pkg:
        raise ValueError(f"unknown package: {package_id}")
    price_per_credit = float(_global().get("credit_price_usd") or 0.75)
    # Price is charged on the package's *base* credit count (10/25/50/100),
    # not the bonus-inflated total - the bonus is the incentive, not something
    # the buyer pays extra for.
    base = int(package_id)
    return round(base * price_per_credit, 2)


def create_order(uid: str, package_id: str) -> dict:
    amount = package_price(package_id)
    resp = requests.post(
        f"{_api_base()}/v2/checkout/orders",
        headers=_headers(),
        json={
            "intent": "CAPTURE",
            "purchase_units": [{
                "custom_id": f"{uid}:{package_id}",
                "amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
                "description": PACKAGES[package_id]["label"],
            }],
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"PayPal order creation failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def capture_order(uid: str, order_id: str) -> dict:
    """Server-to-server capture - this, not the client-side approval, is
    what actually grants credits. Idempotent via record_payment_if_new: a
    retried/duplicate capture call for an already-processed order is a
    no-op, never a double-credit."""
    resp = requests.post(
        f"{_api_base()}/v2/checkout/orders/{order_id}/capture",
        headers=_headers(),
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"PayPal capture failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if data.get("status") != "COMPLETED":
        raise RuntimeError(f"PayPal order not completed: {data.get('status')}")

    purchase_unit = (data.get("purchase_units") or [{}])[0]
    custom_id = purchase_unit.get("custom_id") or purchase_unit.get("payments", {}).get(
        "captures", [{}]
    )[0].get("custom_id", "")
    order_uid, _, package_id = (custom_id or "").partition(":")
    if order_uid != uid or package_id not in PACKAGES:
        raise RuntimeError("PayPal order does not match this user/package")

    credits = PACKAGES[package_id]["credits"]
    applied = firestore_db.record_payment_if_new(order_id, uid, credits)
    if applied:
        logger.success(f"credited {uid} with {credits} credits (order {order_id})")
    else:
        logger.info(f"order {order_id} already processed - no-op")
    return {"applied": applied, "credits": credits, "balance": firestore_db.get_user_credits(uid)}


# --------------------------------------------------------------------------- #
# Auto Mode subscription
# --------------------------------------------------------------------------- #
def auto_mode_price() -> float:
    return float(_global().get("auto_mode_price_usd") or 29.0)


def _billing_plan_id() -> str:
    plan_id = _global().get("paypal_auto_mode_plan_id", "")
    if not plan_id:
        raise ValueError(
            "No PayPal Billing Plan is configured for Auto Mode yet. "
            "Create one in the PayPal dashboard and paste its ID into Settings."
        )
    return plan_id


def create_subscription(uid: str) -> dict:
    resp = requests.post(
        f"{_api_base()}/v1/billing/subscriptions",
        headers=_headers(),
        json={
            "plan_id": _billing_plan_id(),
            "custom_id": uid,
        },
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"PayPal subscription creation failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def verify_webhook_signature(headers: dict, body: bytes) -> bool:
    """PayPal signs every webhook delivery; this calls PayPal's own
    verification endpoint rather than reimplementing signature checking,
    which is what PayPal's own docs recommend. Subscription state must only
    ever change from a verified event - never from the unauthenticated
    client-side redirect-back, since PayPal-side cancellations/renewal
    failures have no client present to report them."""
    webhook_id = _global().get("paypal_webhook_id", "")
    if not webhook_id:
        logger.warning("no paypal_webhook_id configured - refusing to trust any webhook")
        return False
    import json as _json

    resp = requests.post(
        f"{_api_base()}/v1/notifications/verify-webhook-signature",
        headers=_headers(),
        json={
            "auth_algo": headers.get("paypal-auth-algo", ""),
            "cert_url": headers.get("paypal-cert-url", ""),
            "transmission_id": headers.get("paypal-transmission-id", ""),
            "transmission_sig": headers.get("paypal-transmission-sig", ""),
            "transmission_time": headers.get("paypal-transmission-time", ""),
            "webhook_id": webhook_id,
            "webhook_event": _json.loads(body),
        },
        timeout=30,
    )
    if not resp.ok:
        logger.error(f"webhook signature verification request failed ({resp.status_code}): {resp.text[:300]}")
        return False
    return resp.json().get("verification_status") == "SUCCESS"


def handle_webhook_event(event: dict) -> None:
    event_type = event.get("event_type", "")
    resource = event.get("resource", {})

    if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
        uid = resource.get("custom_id", "")
        if uid:
            firestore_db.set_auto_mode_subscription(uid, True)
            logger.success(f"Auto Mode subscription activated for {uid}")
    elif event_type in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.EXPIRED"):
        uid = resource.get("custom_id", "")
        if uid:
            firestore_db.set_auto_mode_subscription(uid, False)
            logger.info(f"Auto Mode subscription ended for {uid} ({event_type})")
    else:
        logger.info(f"unhandled PayPal webhook event: {event_type}")
