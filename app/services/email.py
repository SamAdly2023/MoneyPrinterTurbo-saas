"""
Transactional email (welcome email to new users, admin new-signup alerts).

SMTP credentials are admin-managed and shared (app_config/global in Firestore),
the same pattern as the YouTube/TikTok OAuth app credentials in publish.py -
every account's emails go out through the same configured mailbox/provider.
If SMTP isn't configured yet, sends are skipped with a warning rather than
failing the signup flow itself.
"""

import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from loguru import logger

from app.services import firestore_db
from app.services.auth import ADMIN_EMAILS

BRAND_PURPLE = "#6c5ce7"
BRAND_BG = "#0b0f19"
BRAND_PANEL = "#141b2d"
BRAND_TEXT = "#e7ecf5"
BRAND_MUTED = "#8b98b5"


def _global() -> dict:
    return firestore_db.get_global_settings()


def _base_url() -> str:
    return (_global().get("publish_base_url") or "https://vidzy.web.app").rstrip("/")


def _logo_url() -> str:
    return f"{_base_url()}/logo.png"


def _smtp_settings() -> dict:
    g = _global()
    return {
        "host": g.get("smtp_host", ""),
        "port": int(g.get("smtp_port") or 587),
        "username": g.get("smtp_username", ""),
        "password": g.get("smtp_password", ""),
        "from_email": g.get("smtp_from_email") or g.get("smtp_username", ""),
        "from_name": g.get("smtp_from_name") or "Vidzy",
    }


def _admin_notify_email() -> str:
    g = _global()
    configured = (g.get("admin_notify_email") or "").strip()
    if configured:
        return configured
    return next(iter(ADMIN_EMAILS), "")


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Send one HTML email. Returns False (and logs) instead of raising -
    a broken mail server must never take down signup or job processing."""
    if not to_email:
        return False
    smtp = _smtp_settings()
    if not smtp["host"] or not smtp["username"] or not smtp["password"]:
        logger.warning("email not sent (SMTP not configured in admin Settings): " + subject)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f'{smtp["from_name"]} <{smtp["from_email"]}>'
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        if smtp["port"] == 465:
            server = smtplib.SMTP_SSL(smtp["host"], smtp["port"], timeout=20)
        else:
            server = smtplib.SMTP(smtp["host"], smtp["port"], timeout=20)
            server.starttls()
        try:
            server.login(smtp["username"], smtp["password"])
            server.sendmail(smtp["from_email"], [to_email], msg.as_string())
        finally:
            server.quit()
        logger.success(f"email sent to {to_email}: {subject}")
        return True
    except Exception as e:  # noqa: BLE001 - email failures must never break the caller
        logger.error(f"failed to send email to {to_email}: {e}")
        return False


def _email_shell(inner_html: str) -> str:
    """Shared HTML wrapper - logo header, dark card body, signature footer."""
    return f"""
    <div style="background:{BRAND_BG};padding:32px 16px;font-family:'Segoe UI',Arial,sans-serif;">
      <div style="max-width:520px;margin:0 auto;">
        <div style="text-align:center;margin-bottom:22px;">
          <img src="{_logo_url()}" alt="Vidzy" width="72" height="72"
               style="border-radius:16px;display:inline-block;" />
        </div>
        <div style="background:{BRAND_PANEL};border-radius:16px;padding:32px 28px;color:{BRAND_TEXT};">
          {inner_html}
        </div>
        <p style="text-align:center;color:{BRAND_MUTED};font-size:12px;margin-top:22px;">
          Vidzy - AI video studio &middot; <a href="{_base_url()}" style="color:{BRAND_MUTED}">{_base_url().replace('https://', '')}</a>
        </p>
      </div>
    </div>
    """.strip()


def send_welcome_email(to_email: str) -> bool:
    inner = f"""
      <h1 style="margin:0 0 6px;font-size:22px;color:#fff;">Welcome to Vidzy! 🎬</h1>
      <p style="margin:0 0 22px;color:{BRAND_MUTED};font-size:14px;">
        Your account is ready. Here's how to get your first video out the door.
      </p>
      <table role="presentation" style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="padding:10px 0;vertical-align:top;width:34px;">
            <div style="width:26px;height:26px;border-radius:8px;background:{BRAND_PURPLE};color:#fff;
                        text-align:center;line-height:26px;font-weight:700;font-size:13px;">1</div>
          </td>
          <td style="padding:10px 0;font-size:14px;">
            <strong style="color:#fff;">Create your first video</strong><br/>
            <span style="color:{BRAND_MUTED};">Go to <em>New Script</em>, give it a subject (or write your own script), and add it to the queue.</span>
          </td>
        </tr>
        <tr>
          <td style="padding:10px 0;vertical-align:top;">
            <div style="width:26px;height:26px;border-radius:8px;background:{BRAND_PURPLE};color:#fff;
                        text-align:center;line-height:26px;font-weight:700;font-size:13px;">2</div>
          </td>
          <td style="padding:10px 0;font-size:14px;">
            <strong style="color:#fff;">Turn on Auto Mode</strong><br/>
            <span style="color:{BRAND_MUTED};">Let Vidzy keep inventing and rendering new Shorts on its own, hands-free.</span>
          </td>
        </tr>
        <tr>
          <td style="padding:10px 0;vertical-align:top;">
            <div style="width:26px;height:26px;border-radius:8px;background:{BRAND_PURPLE};color:#fff;
                        text-align:center;line-height:26px;font-weight:700;font-size:13px;">3</div>
          </td>
          <td style="padding:10px 0;font-size:14px;">
            <strong style="color:#fff;">Connect YouTube</strong><br/>
            <span style="color:{BRAND_MUTED};">One click from your Dashboard - then publish finished videos automatically with a title and description.</span>
          </td>
        </tr>
        <tr>
          <td style="padding:10px 0;vertical-align:top;">
            <div style="width:26px;height:26px;border-radius:8px;background:{BRAND_PURPLE};color:#fff;
                        text-align:center;line-height:26px;font-weight:700;font-size:13px;">4</div>
          </td>
          <td style="padding:10px 0;font-size:14px;">
            <strong style="color:#fff;">Try Clips</strong><br/>
            <span style="color:{BRAND_MUTED};">Upload a long-form video and let AI cut it into ready-to-post Shorts.</span>
          </td>
        </tr>
      </table>
      <div style="text-align:center;margin-top:26px;">
        <a href="{_base_url()}" style="display:inline-block;background:linear-gradient(135deg,#6c5ce7,#a06bff);
           color:#fff;text-decoration:none;font-weight:700;font-size:14px;padding:12px 26px;border-radius:11px;">
          Open Vidzy
        </a>
      </div>
      <p style="margin:28px 0 0;color:{BRAND_MUTED};font-size:13px;">
        Questions? Just reply to this email - we're happy to help.<br/><br/>
        - The Vidzy Team
      </p>
    """
    return send_email(to_email, "Welcome to Vidzy - here's how to get started", _email_shell(inner))


def notify_admin_new_signup(user_email: str, provider: str) -> bool:
    admin_email = _admin_notify_email()
    if not admin_email:
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    inner = f"""
      <h1 style="margin:0 0 14px;font-size:19px;color:#fff;">New Vidzy signup</h1>
      <p style="margin:0;font-size:14px;color:{BRAND_TEXT};">
        <strong>{user_email}</strong> just created an account via <strong>{provider}</strong>.
      </p>
      <p style="margin:14px 0 0;font-size:12.5px;color:{BRAND_MUTED};">{now}</p>
      <div style="margin-top:22px;">
        <a href="{_base_url()}/admin" style="display:inline-block;background:{BRAND_PURPLE};
           color:#fff;text-decoration:none;font-weight:700;font-size:13px;padding:10px 20px;border-radius:10px;">
          View in Admin Dashboard
        </a>
      </div>
    """
    return send_email(admin_email, f"New Vidzy signup: {user_email}", _email_shell(inner))
