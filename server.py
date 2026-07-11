"""Minimal MCP server exposing a single `send_email` tool over SMTP.

The server implements NO authentication of its own by design. It is meant to run
on an internal network, fronted by an identity-aware authorization proxy (e.g.
Pomerium in MCP mode) that authenticates and authorizes every request before it
reaches `/mcp`. See README.md.

Configuration is entirely via environment variables (see .env.example).
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

logger = logging.getLogger("email-mcp")

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
POMERIUM_IDENTITY_HEADER = os.environ.get("POMERIUM_IDENTITY_HEADER", "x-pomerium-jwt-assertion")

# Send a one-off test email on startup to verify the SMTP configuration. On
# failure the error is logged verbosely (SMTP transcript + traceback) and the
# server keeps running.
STARTUP_TEST_EMAIL = os.environ.get("STARTUP_TEST_EMAIL", "false").lower() == "true"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

mcp = FastMCP("email-mcp", host=HOST, port=PORT)


def _build_message(
    subject: str,
    html: str,
    to: str | None = None,
    text: str | None = None,
) -> tuple[str, EmailMessage]:
    """Validate inputs and construct the email. Returns (recipient, message).

    Raises on config/validation problems (missing recipient, disallowed
    recipient, missing credentials) — i.e. everything *before* the network.
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

    return recipient, msg


def _deliver(recipient: str, msg: EmailMessage) -> None:
    """Open the SMTP connection and send the message (the network phase)."""
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


def _send_email(
    subject: str,
    html: str,
    to: str | None = None,
    text: str | None = None,
) -> str:
    """Build and send an HTML email via the configured SMTP relay; return recipient."""
    recipient, msg = _build_message(subject, html, to, text)
    _deliver(recipient, msg)
    return recipient


def _describe_smtp_error(exc: Exception) -> str:
    """Summarize why an SMTP send failed, without dumping the message.

    smtplib auth/response errors carry the server's reply code and text (e.g.
    535 'Username and Password not accepted' for a wrong user / unauthorized);
    connection errors surface as OSError. This extracts just that reason.
    """
    code = getattr(exc, "smtp_code", None)
    smtp_error = getattr(exc, "smtp_error", None)
    if smtp_error is not None:
        if isinstance(smtp_error, bytes):
            smtp_error = smtp_error.decode("utf-8", "replace")
        return f"{type(exc).__name__} ({code}): {smtp_error}"
    return f"{type(exc).__name__}: {exc}"


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
    recipient = _send_email(subject, html, to, text)
    return f"Email sent to {recipient}."


def _send_startup_test_email() -> None:
    """Send a one-off test email at startup to verify SMTP config.

    Never raises. Message construction is not logged; only the SMTP send is
    diagnosed on failure (auth/connection reason — wrong user, unauthorized,
    connection refused, etc.), then the server starts anyway.
    """
    logger.info("STARTUP_TEST_EMAIL enabled — sending startup test email...")

    try:
        recipient, msg = _build_message(
            subject="email-mcp startup test",
            html="<p>✅ The <strong>email-mcp</strong> server started and can send mail.</p>",
            text="The email-mcp server started and can send mail.",
        )
    except Exception as exc:
        logger.error("Startup test email skipped — invalid configuration: %s", exc)
        return

    try:
        _deliver(recipient, msg)
    except Exception as exc:
        logger.error(
            "Startup test email FAILED to send to %s via %s:%s as %s — %s. "
            "The server will keep running; fix the SMTP settings and restart to retest.",
            recipient,
            SMTP_HOST,
            SMTP_PORT,
            SMTP_USER or "<unset>",
            _describe_smtp_error(exc),
        )
        return

    logger.info("Startup test email sent successfully to %s.", recipient)


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if STARTUP_TEST_EMAIL:
        _send_startup_test_email()

    if REQUIRE_POMERIUM_IDENTITY:
        _run_with_identity_gate()
    else:
        mcp.run(transport="streamable-http")
