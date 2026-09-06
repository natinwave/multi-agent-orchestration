"""Call screening.

This is the door. The endpoint must be publicly reachable for calls to
arrive at all, so it will be found and poked, and everything behind it can
start jobs and hand out credentials. These tests are what make "only my
phone gets answered" true rather than intended.

Fabricated numbers and secrets throughout. secret-scan: allow
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from orchestrator.voice.sip import (
    DECLINE,
    MAX_SIGNATURE_AGE_SECONDS,
    CallScreen,
    caller_of,
    normalise_number,
    verify_signature,
)

# A Standard Webhooks secret: base64 key material, whsec_ prefixed.
RAW_KEY = b"fabricated-webhook-signing-key!!"
SECRET = "whsec_" + base64.b64encode(RAW_KEY).decode()
WID = "msg_2KWPBgLlAfxdpx2AI54pPJ85f4W"
MINE = "+14255551212"
THEIRS = "+19995550000"


def incoming(caller: str = MINE, call_id: str = "call_abc123") -> bytes:
    return json.dumps(
        {
            "object": "event",
            "type": "realtime.call.incoming",
            "data": {
                "call_id": call_id,
                "sip_headers": [
                    {"name": "From", "value": f"sip:{caller}@sip.example.com"},
                    {"name": "To", "value": "sip:+18005551212@sip.example.com"},
                    {"name": "Call-ID", "value": "0378208601"},
                ],
            },
        }
    ).encode()


def sign(
    body: bytes,
    timestamp: str | None = None,
    secret: str = SECRET,
    webhook_id: str = WID,
) -> tuple[str, str]:
    """Sign exactly as the Standard Webhooks spec says, not as our code does.

    This is the point. The previous version of this helper reproduced the
    implementation's own mistake -- HMAC over timestamp.body, raw secret,
    hex output -- so every test passed while the real thing rejected every
    genuine call. A verification test that signs the way the code verifies
    proves only that the code is self-consistent.

    Spec: HMAC-SHA256 over "id.timestamp.body", base64, against the
    base64-decoded secret, presented as "v1,<signature>".
    """
    timestamp = timestamp or str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{webhook_id}.{timestamp}.".encode() + body, hashlib.sha256)
    return f"v1,{base64.b64encode(digest.digest()).decode()}", timestamp


@pytest.fixture
def screen() -> CallScreen:
    return CallScreen(allowed=(MINE,), signing_secret=SECRET)


# --- number handling -------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "+14255551212",
        "14255551212",
        "4255551212",
        "sip:+14255551212@sip.example.com",
        "+1 (425) 555-1212",
        "tel:+1-425-555-1212",
    ],
)
def test_the_same_phone_written_differently_is_the_same_phone(written: str) -> None:
    """Trunks and carriers disagree about formatting. A whitelist that did
    not see through that would decline its owner."""
    assert normalise_number(written).endswith("4255551212")


def test_caller_is_read_from_the_from_header() -> None:
    event = json.loads(incoming())
    assert "4255551212" in caller_of(event)


def test_a_missing_from_header_yields_nothing() -> None:
    assert caller_of({"data": {"sip_headers": []}}) == ""
    assert caller_of({}) == ""


# --- signature verification ------------------------------------------------


def test_a_correct_signature_verifies() -> None:
    body = incoming()
    sig, ts = sign(body)
    assert verify_signature(body, sig, ts, SECRET, webhook_id=WID) is True


def test_a_wrong_secret_does_not_verify() -> None:
    body = incoming()
    sig, ts = sign(body, secret="a-different-secret-entirely")
    assert verify_signature(body, sig, ts, SECRET, webhook_id=WID) is False


def test_a_tampered_body_does_not_verify() -> None:
    """Someone swapping the caller for their own number is the attack this
    stops."""
    sig, ts = sign(incoming(caller=MINE))
    assert verify_signature(incoming(caller=THEIRS), sig, ts, SECRET, webhook_id=WID) is False


def test_an_old_signature_does_not_verify() -> None:
    """A captured request must not be replayable later to make the phone
    ring at three in the morning."""
    body = incoming()
    old = str(int(time.time()) - MAX_SIGNATURE_AGE_SECONDS - 60)
    sig, ts = sign(body, timestamp=old)
    assert verify_signature(body, sig, ts, SECRET, webhook_id=WID) is False


def test_a_future_timestamp_does_not_verify() -> None:
    body = incoming()
    ahead = str(int(time.time()) + MAX_SIGNATURE_AGE_SECONDS + 60)
    sig, ts = sign(body, timestamp=ahead)
    assert verify_signature(body, sig, ts, SECRET, webhook_id=WID) is False


@pytest.mark.parametrize("bad", [None, "", "not-a-signature"])
def test_missing_or_junk_signatures_are_refused(bad) -> None:
    body = incoming()
    _, ts = sign(body)
    assert verify_signature(body, bad, ts, SECRET, webhook_id=WID) is False


def test_a_missing_timestamp_is_refused() -> None:
    body = incoming()
    sig, _ = sign(body)
    assert verify_signature(body, sig, None, SECRET, webhook_id=WID) is False


def test_a_non_numeric_timestamp_is_refused() -> None:
    body = incoming()
    sig, _ = sign(body)
    assert verify_signature(body, sig, "yesterday", SECRET, webhook_id=WID) is False


def test_a_signature_list_is_accepted() -> None:
    """The header is a space-delimited list; providers send several while
    rotating secrets, and only one has to match."""
    body = incoming()
    sig, ts = sign(body)
    assert verify_signature(body, f"{sig} v1,deadbeef", ts, SECRET, webhook_id=WID) is True


# --- the screening decision ------------------------------------------------


def test_my_own_number_is_answered(screen: CallScreen) -> None:
    body = incoming(caller=MINE)
    sig, ts = sign(body)
    decision = screen.screen(body, sig, ts, WID)
    assert decision.accept is True
    assert decision.call_id == "call_abc123"


def test_anyone_else_is_declined(screen: CallScreen) -> None:
    body = incoming(caller=THEIRS)
    sig, ts = sign(body)
    decision = screen.screen(body, sig, ts, WID)
    assert decision.accept is False
    assert decision.status_code == DECLINE
    assert "not on the list" in decision.reason


def test_an_empty_whitelist_answers_nobody() -> None:
    """Default-deny. A misconfiguration leaves the phone silent, never
    open to the world."""
    screen = CallScreen(allowed=(), signing_secret=SECRET)
    body = incoming(caller=MINE)
    sig, ts = sign(body)
    decision = screen.screen(body, sig, ts, WID)
    assert decision.accept is False
    assert "allowed_callers" in decision.reason


def test_an_unsigned_request_is_declined_whoever_it_claims_to_be(
    screen: CallScreen,
) -> None:
    """The whole point of the signature: a forged webhook from the right
    number must still not ring."""
    body = incoming(caller=MINE)
    assert screen.screen(body, None, None, WID).accept is False


def test_a_forged_body_from_my_number_is_declined(screen: CallScreen) -> None:
    body = incoming(caller=MINE)
    decision = screen.screen(body, "v1," + "0" * 43 + "=", str(int(time.time())), WID)
    assert decision.accept is False
    assert "signature" in decision.reason


def test_the_reply_to_a_forged_request_reveals_nothing(screen: CallScreen) -> None:
    """A prober should not learn whether the call_id or the number was
    otherwise acceptable."""
    decision = screen.screen(incoming(caller=MINE), "v1,bad", str(int(time.time())), WID)
    assert decision.call_id == ""
    assert decision.caller == ""


def test_a_different_event_type_is_ignored(screen: CallScreen) -> None:
    body = json.dumps({"type": "realtime.call.ended", "data": {}}).encode()
    sig, ts = sign(body)
    assert screen.screen(body, sig, ts, WID).accept is False


def test_malformed_json_is_declined(screen: CallScreen) -> None:
    body = b"{not json at all"
    sig, ts = sign(body)
    assert screen.screen(body, sig, ts, WID).accept is False


def test_a_call_with_no_id_is_declined(screen: CallScreen) -> None:
    body = incoming(call_id="")
    sig, ts = sign(body)
    assert screen.screen(body, sig, ts, WID).accept is False


def test_several_numbers_can_be_allowed() -> None:
    other = "+14255559999"
    screen = CallScreen(allowed=(MINE, other), signing_secret=SECRET)
    for number in (MINE, other):
        body = incoming(caller=number)
        sig, ts = sign(body)
        assert screen.screen(body, sig, ts, WID).accept is True, number


def test_a_number_written_differently_in_config_still_matches() -> None:
    screen = CallScreen(allowed=("(425) 555-1212",), signing_secret=SECRET)
    body = incoming(caller="+14255551212")
    sig, ts = sign(body)
    assert screen.screen(body, sig, ts, WID).accept is True


def test_screening_without_a_secret_still_applies_the_whitelist() -> None:
    """Signature checking can be turned off for a local test, but that must
    not turn the door off with it."""
    screen = CallScreen(allowed=(MINE,), signing_secret="")
    assert screen.screen(incoming(caller=MINE)).accept is True
    assert screen.screen(incoming(caller=THEIRS)).accept is False


def test_a_declined_call_reports_the_digits_to_whitelist(caplog) -> None:
    """The commonest first-run failure is a trunk presenting the number in
    a shape the whitelist was not written for. The log has to say what to
    copy, or the fix is guesswork."""
    import logging

    from orchestrator.voice.sip import SipListener

    listener = SipListener(
        CallScreen(allowed=("+15550000000",), signing_secret=""),
        api_key="unused",
        on_call=None,
    )
    decision = listener.screen_.screen(incoming(caller=MINE))
    with caplog.at_level(logging.WARNING):
        listener._decide(decision)
    assert "4255551212" in caplog.text


# --- the webhook must answer immediately -----------------------------------


def _post(url: str, payload: bytes, timeout: float = 5.0):
    import urllib.request

    return urllib.request.urlopen(
        urllib.request.Request(url, data=payload, method="POST"), timeout=timeout
    )


def _serve(listener, port_holder):
    """Run a listener briefly and return the loop task."""
    import asyncio

    return asyncio.create_task(listener.run())


def test_the_webhook_answers_before_talking_to_openai() -> None:
    """Accepting or rejecting a call is an HTTPS round trip. Doing it while
    the webhook connection is still open held that connection long enough
    for the tunnel in front to give up and return 502 -- so the call was
    never accepted and rang until it timed out.

    The response must land regardless of how slow that round trip is.
    """
    import asyncio
    import time

    from orchestrator.voice.sip import SipListener

    async def scenario() -> float:
        listener = SipListener(
            CallScreen(allowed=(MINE,), signing_secret=""),
            api_key="unused",
            on_call=lambda call_id: asyncio.sleep(0),
            port=8903,
        )
        # Stand in for an OpenAI call that hangs.
        listener._post = lambda url, payload: time.sleep(10) or {}

        task = asyncio.create_task(listener.run())
        await asyncio.sleep(0.3)
        try:
            started = time.monotonic()
            response = _post("http://127.0.0.1:8903/sip", incoming(caller=THEIRS))
            elapsed = time.monotonic() - started
            assert response.status == 200
            return elapsed
        finally:
            task.cancel()

    elapsed = asyncio.run(scenario())
    assert elapsed < 2.0, f"webhook took {elapsed:.1f}s; a proxy would have given up"


def test_an_accepted_call_reaches_the_handler() -> None:
    import asyncio

    from orchestrator.voice.sip import SipListener

    answered: list[str] = []

    async def scenario() -> None:
        async def on_call(call_id: str) -> None:
            answered.append(call_id)

        listener = SipListener(
            CallScreen(allowed=(MINE,), signing_secret=""),
            api_key="unused",
            on_call=on_call,
            port=8904,
        )
        task = asyncio.create_task(listener.run())
        await asyncio.sleep(0.3)
        try:
            _post("http://127.0.0.1:8904/sip", incoming(caller=MINE))
            await asyncio.sleep(0.4)
        finally:
            task.cancel()

    asyncio.run(scenario())
    assert answered == ["call_abc123"]


def test_a_query_string_does_not_break_routing() -> None:
    """Proxies and providers append them; the path is a route, not a URL."""
    import asyncio

    from orchestrator.voice.sip import SipListener

    async def scenario() -> int:
        listener = SipListener(
            CallScreen(allowed=(MINE,), signing_secret=""),
            api_key="unused",
            on_call=lambda call_id: asyncio.sleep(0),
            port=8905,
        )
        task = asyncio.create_task(listener.run())
        await asyncio.sleep(0.3)
        try:
            return _post("http://127.0.0.1:8905/sip?source=openai", incoming()).status
        finally:
            task.cancel()

    assert asyncio.run(scenario()) == 200


def test_an_unknown_path_is_not_served() -> None:
    import asyncio
    import urllib.error

    from orchestrator.voice.sip import SipListener

    async def scenario() -> int:
        listener = SipListener(
            CallScreen(allowed=(MINE,), signing_secret=""),
            api_key="unused",
            on_call=lambda call_id: asyncio.sleep(0),
            port=8906,
        )
        task = asyncio.create_task(listener.run())
        await asyncio.sleep(0.3)
        try:
            _post("http://127.0.0.1:8906/admin", incoming())
            return 200
        except urllib.error.HTTPError as exc:
            return exc.code
        finally:
            task.cancel()

    assert asyncio.run(scenario()) == 404


# --- the spec itself, pinned -----------------------------------------------


def test_the_signed_string_is_id_dot_timestamp_dot_body() -> None:
    """Constructed here from first principles rather than with the helper,
    so a mistake in the helper cannot hide a mistake in the code. Every
    component of this was wrong once and the phone silently refused every
    call as a result."""
    body = b'{"type":"realtime.call.incoming"}'
    ts = str(int(time.time()))
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    mac = hmac.new(key, f"{WID}.{ts}.".encode() + body, hashlib.sha256).digest()
    header = "v1," + base64.b64encode(mac).decode()

    assert verify_signature(body, header, ts, SECRET, webhook_id=WID) is True


def test_the_webhook_id_is_part_of_the_signature() -> None:
    """Omitting it -- as the first implementation did -- still produces a
    plausible-looking HMAC that never matches."""
    body = incoming()
    sig, ts = sign(body)
    assert verify_signature(body, sig, ts, SECRET, webhook_id="msg_something_else") is False
    assert verify_signature(body, sig, ts, SECRET, webhook_id=None) is False


def test_a_hex_signature_is_not_accepted() -> None:
    """The output is base64. Hex was the original mistake."""
    body = incoming()
    ts = str(int(time.time()))
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    hex_sig = hmac.new(key, f"{WID}.{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert verify_signature(body, f"v1,{hex_sig}", ts, SECRET, webhook_id=WID) is False


def test_the_secret_is_base64_decoded_before_use() -> None:
    """Signing with the literal whsec_ string rather than the bytes it
    encodes produces a different MAC entirely."""
    body = incoming()
    ts = str(int(time.time()))
    wrong = base64.b64encode(
        hmac.new(SECRET.encode(), f"{WID}.{ts}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    assert verify_signature(body, f"v1,{wrong}", ts, SECRET, webhook_id=WID) is False


def test_base64_padding_survives_parsing() -> None:
    """Splitting an entry on "=" -- as the first version did -- truncates
    the padding and can never match. Signatures ending in padding are
    common enough that this failed most of the time."""
    from orchestrator.voice.sip import signing_key

    body = incoming()
    ts = str(int(time.time()))
    for attempt in range(40):
        payload = body + b" " * attempt
        mac = hmac.new(
            signing_key(SECRET)[0], f"{WID}.{ts}.".encode() + payload, hashlib.sha256
        ).digest()
        sig = base64.b64encode(mac).decode()
        if sig.endswith("="):
            assert verify_signature(payload, f"v1,{sig}", ts, SECRET, webhook_id=WID) is True
            return
    raise AssertionError("no padded signature found to test")


def test_a_secret_pasted_without_the_prefix_still_works() -> None:
    """Someone copying from a dashboard may not include whsec_, and a
    phone that silently refuses every call is a bad way to learn that."""
    body = incoming()
    sig, ts = sign(body)
    bare = SECRET.removeprefix("whsec_")
    assert verify_signature(body, sig, ts, bare, webhook_id=WID) is True
