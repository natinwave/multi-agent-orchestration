"""Answering the phone.

The Realtime API accepts calls over SIP: point a trunk at
``sip:$PROJECT_ID@sip.api.openai.com``, and when someone dials the number
OpenAI posts a ``realtime.call.incoming`` webhook. You accept or reject.

Two things make this a better fit than Discord, beyond the fact that
Discord voice reception is broken:

- **OpenAI bridges the audio itself.** For a SIP call this process never
  touches a sample. No resampling, no jitter buffer, no library reading an
  undocumented surface. Compare ``audio.py`` and ``playback.py``, which
  exist solely to feed Discord.
- **The webhook is a door.** Every call arrives with its caller in the SIP
  headers and is declined by default. Nothing is answered, and nothing is
  billed, unless the number is one you listed.

The residual risk worth naming: a SIP ``From`` header is not a
cryptographic identity. Through a real telco trunk, caller ID for
PSTN-originated calls is hard to forge and generally trustworthy, but it
is not proof. That is why the webhook signature is checked as well, why
filtering at the trunk is recommended in the README, and why granting a
credential still needs a spoken confirmation regardless of who is calling.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CallDecision",
    "CallScreen",
    "SipListener",
    "accept_payload",
    "verify_signature",
    "caller_of",
    "normalise_number",
    "ACCEPT_URL",
    "REJECT_URL",
    "ATTACH_URL",
    "DECLINE",
]

log = logging.getLogger("orchestrator.voice.sip")

ACCEPT_URL = "https://api.openai.com/v1/realtime/calls/{call_id}/accept"
REJECT_URL = "https://api.openai.com/v1/realtime/calls/{call_id}/reject"
ATTACH_URL = "wss://api.openai.com/v1/realtime?call_id={call_id}"

#: SIP status for a call we will not take. 603 is "Decline" -- a definite
#: no, rather than 486 "Busy" which invites a caller to try again.
DECLINE = 603

#: Webhook timestamps older than this are refused, so a captured request
#: cannot be replayed later to make the phone ring.
MAX_SIGNATURE_AGE_SECONDS = 300

_DIGITS = re.compile(r"\D+")


def normalise_number(value: str) -> str:
    """Reduce a phone number or SIP URI to comparable digits.

    ``sip:+1 (425) 555-1212@host`` and ``+14255551212`` are the same
    number written by different systems, and a whitelist that failed to
    see that would decline the owner's own phone.
    """
    if not value:
        return ""
    local = value.split("@", 1)[0]
    local = local.split(":", 1)[-1] if ":" in local else local
    return _DIGITS.sub("", local)


def caller_of(event: dict) -> str:
    """The calling number from a realtime.call.incoming webhook."""
    headers = ((event or {}).get("data") or {}).get("sip_headers") or []
    for header in headers:
        if str(header.get("name", "")).lower() == "from":
            return str(header.get("value", ""))
    return ""


def verify_signature(
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    secret: str,
    now: float | None = None,
) -> bool:
    """Whether this really came from OpenAI, recently.

    The endpoint has to be publicly reachable for calls to arrive at all,
    so it will be found and poked. Without this, anyone who found the URL
    could make the phone ring by posting a plausible body.
    """
    if not (signature and timestamp and secret):
        return False

    try:
        age = (time.time() if now is None else now) - float(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(age) > MAX_SIGNATURE_AGE_SECONDS:
        return False

    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()

    # Any of the signatures offered may match, and comparison is
    # constant-time so the endpoint does not leak the correct value one
    # byte at a time.
    for candidate in re.split(r"[,\s]+", signature.strip()):
        candidate = candidate.split("=", 1)[-1] if "=" in candidate else candidate
        if hmac.compare_digest(candidate, expected):
            return True
    return False


@dataclass(frozen=True)
class CallDecision:
    """What to do with one incoming call, and why."""

    accept: bool
    reason: str
    call_id: str = ""
    caller: str = ""

    @property
    def status_code(self) -> int | None:
        return None if self.accept else DECLINE


@dataclass
class CallScreen:
    """Decides which calls are answered.

    Default-deny: an empty whitelist accepts nothing. A misconfiguration
    should leave the phone silent, never open.
    """

    allowed: tuple[str, ...] = ()
    signing_secret: str = ""
    #: Compare only the last N digits, so +1 425 555 1212 and 425 555 1212
    #: are the same phone. Full E.164 comparison would decline a caller
    #: whose trunk omits the country code.
    significant_digits: int = 10
    _allowed_digits: tuple[str, ...] = field(default_factory=tuple, repr=False)

    def __post_init__(self) -> None:
        self._allowed_digits = tuple(
            self._tail(normalise_number(n)) for n in self.allowed if normalise_number(n)
        )

    def _tail(self, digits: str) -> str:
        return digits[-self.significant_digits :] if digits else ""

    def screen(
        self,
        body: bytes,
        signature: str | None = None,
        timestamp: str | None = None,
        now: float | None = None,
    ) -> CallDecision:
        """Screen one raw webhook request."""
        if self.signing_secret and not verify_signature(
            body, signature, timestamp, self.signing_secret, now=now
        ):
            # Deliberately says nothing about the call: a forged request
            # should learn nothing from the reply.
            return CallDecision(False, "signature did not verify")

        try:
            event = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return CallDecision(False, "body was not JSON")

        if event.get("type") != "realtime.call.incoming":
            return CallDecision(False, f"not an incoming call: {event.get('type')!r}")

        call_id = str((event.get("data") or {}).get("call_id") or "")
        caller = caller_of(event)
        if not call_id:
            return CallDecision(False, "no call_id", caller=caller)

        if not self._allowed_digits:
            return CallDecision(
                False,
                "no numbers are allowed to call: set allowed_callers in config/voice.toml",
                call_id=call_id,
                caller=caller,
            )

        tail = self._tail(normalise_number(caller))
        if tail and tail in self._allowed_digits:
            return CallDecision(True, "caller is allowed", call_id=call_id, caller=caller)

        return CallDecision(
            False, "caller is not on the list", call_id=call_id, caller=caller
        )


class SipListener:
    """Waits for the phone to ring, screens the caller, runs the call.

    The webhook server is a stdlib threading server rather than a
    framework: it serves exactly one path, does one thing, and adding a
    web framework to a project whose core is deliberately dependency-free
    would be a poor trade for that.

    Accepted calls are handed to the asyncio loop, where the session
    attaches by call_id and answers tool calls. Audio never comes near
    this process.
    """

    def __init__(
        self,
        screen: CallScreen,
        api_key: str,
        on_call,
        host: str = "127.0.0.1",
        port: int = 8787,
        path: str = "/sip",
    ) -> None:
        self.screen_ = screen
        self.api_key = api_key
        self.on_call = on_call  # async callable taking a call_id
        self.host, self.port, self.path = host, port, path
        self._loop: Any = None
        self._server: Any = None

    async def run(self) -> None:
        """Serve until stopped. Blocks."""
        import asyncio
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self._loop = asyncio.get_running_loop()
        listener = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - http.server's naming
                if self.path.rstrip("/") != listener.path.rstrip("/"):
                    self._reply(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length)
                decision = listener.screen_.screen(
                    body,
                    self.headers.get("webhook-signature"),
                    self.headers.get("webhook-timestamp"),
                )
                # 200 either way: the caller of this endpoint is OpenAI,
                # and the accept/reject is expressed by the API call we
                # make below, not by this status code.
                self._reply(200, {"received": True})
                listener._decide(decision)

            def _reply(self, status: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass  # our own logging is more useful than the default

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        log.info(
            "waiting for calls on http://%s:%s%s -- %d number(s) allowed",
            self.host,
            self.port,
            self.path,
            len(self.screen_.allowed),
        )
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            self._server.shutdown()

    def _decide(self, decision: CallDecision) -> None:
        """Called on a server thread; hands accepted calls to the loop."""
        import asyncio

        if not decision.accept:
            # The raw From header AND the digits it reduces to. A first
            # call is often declined because the trunk presents the number
            # in a shape the whitelist was not written for, and the fix is
            # simply to copy the digits below into allowed_callers.
            digits = normalise_number(decision.caller)
            log.warning(
                "declined call from %s%s: %s",
                decision.caller or "an unknown number",
                f" (digits: {digits})" if digits else "",
                decision.reason,
            )
            if decision.call_id:
                self._post(REJECT_URL.format(call_id=decision.call_id),
                           {"status_code": DECLINE})
            return

        log.info("answering call from %s", decision.caller)
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.on_call(decision.call_id), self._loop)

    def _post(self, url: str, payload: dict) -> dict | None:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            log.error("%s -> HTTP %s: %s", url, exc.code, exc.read()[:200])
        except Exception as exc:  # noqa: BLE001 - a failed call must not kill the listener
            log.error("%s -> %s", url, exc)
        return None

    def accept(self, call_id: str, payload: dict) -> bool:
        """Answer the call with this session configuration."""
        return self._post(ACCEPT_URL.format(call_id=call_id), payload) is not None


def accept_payload(
    instructions: str,
    tools: list[dict],
    model: str,
    voice: str,
) -> dict:
    """Session configuration, sent when accepting rather than after.

    A SIP call is already up when we attach to it, so there is no window
    in which to send session.update first -- the model has to know its
    instructions and tools before it says hello.
    """
    return {
        "type": "realtime",
        "model": model,
        "instructions": instructions,
        "tools": tools,
        "tool_choice": "auto",
        "audio": {"output": {"voice": voice}},
    }
