"""Baseline browser-security headers for every response.

Pure ASGI (not BaseHTTPMiddleware) so streamed responses - videos under /media, downloads - are passed
through untouched. Existing headers are never overridden, so a route can still set its own.

Deliberately NOT included: Content-Security-Policy. The dashboard relies on inline scripts and several
third-party scripts (Firebase, PayPal); a CSP strict enough to matter would need a nonce rollout and
real browser testing, and a half-working one would break sign-in or payments.
"""

SECURITY_HEADERS = (
    # Stop browsers guessing a file's type (a user-uploaded file served as a script).
    (b"x-content-type-options", b"nosniff"),
    # Pages may only be framed by this site itself (clickjacking).
    (b"x-frame-options", b"SAMEORIGIN"),
    # Don't leak full URLs (which can include job ids) to other sites.
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    # HTTPS only for the next 180 days. No includeSubDomains: mail./cpanel./ftp. share this domain.
    (b"strict-transport-security", b"max-age=15552000"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend((k, v) for k, v in SECURITY_HEADERS if k not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
