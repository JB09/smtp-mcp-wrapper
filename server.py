"""Minimal MCP server exposing a single `send_email` tool over SMTP.

The server implements NO authentication of its own by design. It is meant to run
on an internal network, fronted by an identity-aware authorization proxy (e.g.
Pomerium in MCP mode) that authenticates and authorizes every request before it
reaches `/mcp`. See README.md.

Configuration is entirely via environment variables (see .env.example).
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# --- Configuration (all from env; secrets injected at runtime, never baked in) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM") or SMTP_USER
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "")
DEFAULT_TO = os.environ.get("DEFAULT_TO", "")
ALLOWED_TO = [a.strip().lower() for a in os.environ.get("ALLOWED_TO", "").split(",") if a.strip()]

# Optional app-layer backstop. The external proxy is still REQUIRED regardless.
REQUIRE_POMERIUM_IDENTITY = os.environ.get("REQUIRE_POMERIUM_IDENTITY", "false").lower() == "true"
# Header Pomerium sets on authenticated requests (JWT assertion of the identity).
POMERIUM_IDENTITY_HEADER = os.environ.get("POMERIUM_IDENTITY_HEADER", "x-pomerium-assertion")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

mcp = FastMCP("email-mcp", host=HOST, port=PORT)


@mcp.tool()
def send_email(
    subject: str,
    html: str,
    to: str | None = None,
    text: str | None = None,
) -> str:
    """Send an HTML email via the configured SMTP relay.

    Args:
        subject: The email subject line.
        html: The HTML body of the email.
        to: Recipient address. Falls back to DEFAULT_TO when omitted. Must be in
            the ALLOWED_TO allowlist when one is configured.
        text: Optional plain-text alternative body. A generic placeholder is used
            when omitted so non-HTML clients still see something sensible.

    Returns:
        A short confirmation string naming the recipient.
    """
    recipient = (to or DEFAULT_TO).strip()
    if not recipient:
        raise ValueError("No recipient: pass `to` or set DEFAULT_TO.")

    # Recipient hard-limit: even a misused tool cannot mail outside the allowlist.
    if ALLOWED_TO and recipient.lower() not in ALLOWED_TO:
        raise ValueError(
            f"Recipient {recipient!r} is not permitted. "
            f"Allowed recipients: {', '.join(ALLOWED_TO)}."
        )

    if not (SMTP_USER and SMTP_PASS):
        raise RuntimeError("SMTP_USER and SMTP_PASS must be configured to send mail.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM)) if MAIL_FROM_NAME else MAIL_FROM
    msg["To"] = recipient
    msg.set_content(text or "This message requires an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

    return f"Email sent to {recipient}."


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> PlainTextResponse:
    """Unauthenticated liveness probe used by Docker/compose healthchecks."""
    return PlainTextResponse("ok")


def _run_with_identity_gate() -> None:
    """Serve the MCP app with an app-layer Pomerium-identity requirement on /mcp.

    This is defense-in-depth only; the external authorization proxy remains the
    primary and required gate. `/healthz` stays open so healthchecks keep working.
    """
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware

    app = mcp.streamable_http_app()

    async def require_identity(request: Request, call_next):
        if request.url.path.startswith("/mcp") and not request.headers.get(POMERIUM_IDENTITY_HEADER):
            return PlainTextResponse(
                "Missing authorization proxy identity header.", status_code=401
            )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=require_identity)
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    if REQUIRE_POMERIUM_IDENTITY:
        _run_with_identity_gate()
    else:
        mcp.run(transport="streamable-http")
