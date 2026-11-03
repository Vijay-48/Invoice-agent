"""The API key: with API_KEY set, every request must carry it, as `X-API-Key: <key>` or
`Authorization: Bearer <key>`. Without it the API is open to anyone who can reach it (a warning is
logged on start): fine on one PC, not on a server.

Open without the key, because the caller can't send it and has its own proof:
  /healthz, /readyz                       load balancers and monitors
  /api/v1/webhook/...                     Twilio (X-Twilio-Signature; the call's WebSocket has a token)
  /webhooks/composio                      Composio (signed webhook)
  /api/v1/users/{id}/gmail/callback       the browser, sent back by Google's consent screen via Composio

The key is one shared secret, not a login: X-User-Id still says who acts (D-101).
"""
import hmac
import logging
import re

from starlette.responses import JSONResponse

from app.config import settings

log = logging.getLogger("security")
# /followups/action: the one-click links in reminder emails, opened from a mail app (signed, D-107)
OPEN = re.compile(r"^/(healthz|readyz)$|^/api/v1/webhook/|^/webhooks/composio$|^/api/v1/users/\d+/gmail/callback$"
                  r"|^/api/v1/followups/action$")


def is_open(path: str) -> bool:
    return bool(OPEN.match(path))


def _given_key(headers: dict[bytes, bytes]) -> str:
    if key := headers.get(b"x-api-key"):
        return key.decode("latin-1")
    auth = headers.get(b"authorization", b"").decode("latin-1")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


class ApiKeyMiddleware:
    """Plain ASGI middleware, so it covers WebSockets as well as HTTP and adds no per-request overhead."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not settings.api_key or is_open(scope["path"]):
            return await self.app(scope, receive, send)
        given = _given_key(dict(scope["headers"]))
        if given and hmac.compare_digest(given.encode(), settings.api_key.encode()):
            return await self.app(scope, receive, send)
        log.warning("Refused %s %s: missing or wrong API key", scope.get("method", "WS"), scope["path"])
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await JSONResponse({"detail": "Missing or wrong API key (send X-API-Key)"}, status_code=401)(scope, receive, send)


def warn_if_open() -> None:
    if not settings.api_key:
        log.warning("API_KEY is not set: anyone who can reach this server can use the API as any user. "
                    "Set API_KEY before exposing it beyond this PC.")
