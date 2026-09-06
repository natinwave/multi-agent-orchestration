"""Call screening.

This is the door. The endpoint must be publicly reachable for calls to
arrive at all, so it will be found and poked, and everything behind it can
start jobs and hand out credentials. These tests are what make "only my
phone gets answered" true rather than intended.

Fabricated numbers and secrets throughout. secret-scan: allow
"""

from __future__ import annotations

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

SECRET = "whsec-fabricated-signing-secret"
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


def sign(body: bytes, timestamp: str | None = None, secret: str = SECRET) -> tuple[str, str]:
    timestamp = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return digest, timestamp


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
    assert verify_signature(body, sig, ts, SECRET) is True


def test_a_wrong_secret_does_not_verify() -> None:
    body = incoming()
    sig, ts = sign(body, secret="a-different-secret-entirely")
    assert verify_signature(body, sig, ts, SECRET) is False


def test_a_tampered_body_does_not_verify() -> None:
    """Someone swapping the caller for their own number is the attack this
    stops."""
    sig, ts = sign(incoming(caller=MINE))
    assert verify_signature(incoming(caller=THEIRS), sig, ts, SECRET) is False


def test_an_old_signature_does_not_verify() -> None:
    """A captured request must not be replayable later to make the phone
    ring at three in the morning."""
    body = incoming()
    old = str(int(time.time()) - MAX_SIGNATURE_AGE_SECONDS - 60)
    sig, ts = sign(body, timestamp=old)
    assert verify_signature(body, sig, ts, SECRET) is False


def test_a_future_timestamp_does_not_verify() -> None:
    body = incoming()
    ahead = str(int(time.time()) + MAX_SIGNATURE_AGE_SECONDS + 60)
    sig, ts = sign(body, timestamp=ahead)
    assert verify_signature(body, sig, ts, SECRET) is False


@pytest.mark.parametrize("bad", [None, "", "not-a-signature"])
def test_missing_or_junk_signatures_are_refused(bad) -> None:
    body = incoming()
    _, ts = sign(body)
    assert verify_signature(body, bad, ts, SECRET) is False


def test_a_missing_timestamp_is_refused() -> None:
    body = incoming()
    sig, _ = sign(body)
    assert verify_signature(body, sig, None, SECRET) is False


def test_a_non_numeric_timestamp_is_refused() -> None:
    body = incoming()
    sig, _ = sign(body)
    assert verify_signature(body, sig, "yesterday", SECRET) is False


def test_a_signature_list_is_accepted() -> None:
    """Providers rotate secrets by sending several signatures at once."""
    body = incoming()
    sig, ts = sign(body)
    assert verify_signature(body, f"v1={sig},v1=deadbeef", ts, SECRET) is True


# --- the screening decision ------------------------------------------------


def test_my_own_number_is_answered(screen: CallScreen) -> None:
    body = incoming(caller=MINE)
    sig, ts = sign(body)
    decision = screen.screen(body, sig, ts)
    assert decision.accept is True
    assert decision.call_id == "call_abc123"


def test_anyone_else_is_declined(screen: CallScreen) -> None:
    body = incoming(caller=THEIRS)
    sig, ts = sign(body)
    decision = screen.screen(body, sig, ts)
    assert decision.accept is False
    assert decision.status_code == DECLINE
    assert "not on the list" in decision.reason


def test_an_empty_whitelist_answers_nobody() -> None:
    """Default-deny. A misconfiguration leaves the phone silent, never
    open to the world."""
    screen = CallScreen(allowed=(), signing_secret=SECRET)
    body = incoming(caller=MINE)
    sig, ts = sign(body)
    decision = screen.screen(body, sig, ts)
    assert decision.accept is False
    assert "allowed_callers" in decision.reason


def test_an_unsigned_request_is_declined_whoever_it_claims_to_be(
    screen: CallScreen,
) -> None:
    """The whole point of the signature: a forged webhook from the right
    number must still not ring."""
    body = incoming(caller=MINE)
    assert screen.screen(body, None, None).accept is False


def test_a_forged_body_from_my_number_is_declined(screen: CallScreen) -> None:
    body = incoming(caller=MINE)
    decision = screen.screen(body, "v1=" + "0" * 64, str(int(time.time())))
    assert decision.accept is False
    assert "signature" in decision.reason


def test_the_reply_to_a_forged_request_reveals_nothing(screen: CallScreen) -> None:
    """A prober should not learn whether the call_id or the number was
    otherwise acceptable."""
    decision = screen.screen(incoming(caller=MINE), "v1=bad", str(int(time.time())))
    assert decision.call_id == ""
    assert decision.caller == ""


def test_a_different_event_type_is_ignored(screen: CallScreen) -> None:
    body = json.dumps({"type": "realtime.call.ended", "data": {}}).encode()
    sig, ts = sign(body)
    assert screen.screen(body, sig, ts).accept is False


def test_malformed_json_is_declined(screen: CallScreen) -> None:
    body = b"{not json at all"
    sig, ts = sign(body)
    assert screen.screen(body, sig, ts).accept is False


def test_a_call_with_no_id_is_declined(screen: CallScreen) -> None:
    body = incoming(call_id="")
    sig, ts = sign(body)
    assert screen.screen(body, sig, ts).accept is False


def test_several_numbers_can_be_allowed() -> None:
    other = "+14255559999"
    screen = CallScreen(allowed=(MINE, other), signing_secret=SECRET)
    for number in (MINE, other):
        body = incoming(caller=number)
        sig, ts = sign(body)
        assert screen.screen(body, sig, ts).accept is True, number


def test_a_number_written_differently_in_config_still_matches() -> None:
    screen = CallScreen(allowed=("(425) 555-1212",), signing_secret=SECRET)
    body = incoming(caller="+14255551212")
    sig, ts = sign(body)
    assert screen.screen(body, sig, ts).accept is True


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
