# smtp-mcp-wrapper

A minimal, self-hosted [MCP](https://modelcontextprotocol.io) server that exposes a
single `send_email` tool. It sends real HTML email through an SMTP relay (e.g. Gmail).
The tool is served over the streamable-HTTP MCP transport at `/mcp`, with an
unauthenticated `/healthz` liveness route. The implementation is intentionally tiny
(stdlib `smtplib`) to keep the audit/attack surface small.

## ⚠️ Security requirement: this server MUST be gated by an authorization service

**This server implements no authentication of its own, by design.** Anyone who can reach
`/mcp` can send email. **Do not expose it directly to the internet or bind it to a public
port.**

It **must** sit behind an identity-aware authorization proxy — such as
**[Pomerium](https://www.pomerium.com/docs/capabilities/mcp) in MCP mode**, or an
equivalent like [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/)
or [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) — that authenticates and
authorizes **every** request before it reaches `/mcp`.

Reference topology:

```
edge tunnel → reverse proxy (TLS) → Pomerium (SSO + allowlist to a single identity) → smtp-mcp-wrapper
                                                                                        (internal network only)
```

The provided `docker-compose.yml` deliberately publishes **no host ports** and attaches
the container only to the proxy's internal Docker network, so the server is unreachable
except through the authorization proxy.

**Defense in depth already built in** (these complement, they do not replace, the proxy):

- `ALLOWED_TO` hard-limits recipients, so even a misused tool cannot mail outside the
  allowlist.
- Setting `REQUIRE_POMERIUM_IDENTITY=true` makes the app itself reject `/mcp` requests
  that arrive without a Pomerium identity header (`x-pomerium-assertion` by default) — a
  backstop in case the proxy is ever misconfigured or bypassed.

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and fill in
real values. `.env` is git-ignored and must stay that way — it holds the SMTP password.
Nothing secret is baked into the image (credentials are injected at runtime), which is why
the published container image can safely be public.

| Variable | Default | Description |
| --- | --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP relay host. |
| `SMTP_PORT` | `587` | SMTP relay port (STARTTLS). |
| `SMTP_USER` | — | SMTP username. |
| `SMTP_PASS` | — | SMTP password. For Gmail, use an **App Password**. |
| `MAIL_FROM` | `SMTP_USER` | From address. |
| `MAIL_FROM_NAME` | — | Optional display name for the From header. |
| `DEFAULT_TO` | — | Recipient used when the tool's `to` argument is omitted. |
| `ALLOWED_TO` | — | Comma-separated recipient allowlist. Empty = any recipient allowed. |
| `REQUIRE_POMERIUM_IDENTITY` | `false` | Also enforce a Pomerium identity header at the app layer. |
| `POMERIUM_IDENTITY_HEADER` | `x-pomerium-assertion` | Header checked when the above is `true`. |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | Server bind address/port. |

### The `send_email` tool

```
send_email(subject: str, html: str, to?: str, text?: str) -> str
```

Sends an HTML email. `to` falls back to `DEFAULT_TO` and must be within `ALLOWED_TO` when
that allowlist is set. `text` is an optional plain-text alternative for non-HTML clients.

## Run

```sh
cp .env.example .env      # then edit .env with real values
docker compose up -d
```

Health check:

```sh
docker compose exec email-mcp \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8080/healthz').read())"
# -> b'ok'
```

Then add the `email-mcp` route to your authorization proxy (pathless upstream, e.g.
`to: http://email-mcp:8080`, so the `/mcp` path passes through) and connect your MCP
client to `https://<your-host>/mcp`.

## Maintenance

Patches flow with near-zero manual effort:

- **Dependabot** (`.github/dependabot.yml`) watches `requirements.txt`, the Dockerfile base
  image, and the workflow's actions, opening upgrade PRs weekly. Also enable Dependabot
  **security updates** in the repo's Settings → Code security.
- **CI** (`.github/workflows/build.yml`) builds and pushes the image to GHCR on push to
  `main`, on Dependabot PRs, via manual dispatch, and **weekly (Mon 06:00 UTC) with
  `no-cache`** so the OS and Python patches are genuinely refreshed even without code
  changes.
- On the host, pull the rebuilt image with [Watchtower](https://containrrr.dev/watchtower/)
  (the compose file already sets the opt-in label) or a cron running
  `docker compose pull && docker compose up -d`.

## Links

- Pomerium — [MCP support](https://www.pomerium.com/docs/capabilities/mcp)
- Pomerium — [Protect an MCP server](https://www.pomerium.com/docs/capabilities/mcp/protect-mcp-server)
- [Dependabot configuration options](https://docs.github.com/code-security/dependabot/dependabot-version-updates/configuration-options-for-the-dependabot.yml-file)
