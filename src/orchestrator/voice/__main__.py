"""Run the voice bridge.

    python -m orchestrator.voice
    python -m orchestrator.voice --transport discord

Credentials come from the same place everything else's do: files in the
per-identity secrets directory that bootstrap materialises from 1Password.
Nothing is read from a command line, and nothing but paths from the
environment.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tomllib
from pathlib import Path

from ..registry import ConfigError, load
from ..supervisor import Supervisor
from .realtime import RealtimeSession

VOICE_IDENTITY = "voice"

log = logging.getLogger("orchestrator.voice")


def read_secret(root: Path, name: str) -> str | None:
    try:
        value = (root / VOICE_IDENTITY / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def load_voice_config(config_dir: Path) -> dict:
    path = config_dir / "voice.toml"
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh).get("voice", {})


async def run(transport, session: RealtimeSession) -> None:
    await session.connect()
    # The transport pumps microphone audio in; the session pumps events
    # out. Neither finishes on its own, so whichever stops first ends the
    # call and cancels the other.
    tasks = [asyncio.create_task(transport.run(session)), asyncio.create_task(session.run())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.result()  # re-raise whatever ended the call
    finally:
        await session.close()


def build_transport(name: str, voice_cfg: dict, secrets_root: Path):
    if name == "discord":
        from .transport.discord import DiscordTransport

        token = read_secret(secrets_root, "discord_bot_token")
        channel_id = voice_cfg.get("discord_channel_id")
        missing = [
            label
            for label, value in (
                (f"{secrets_root}/voice/discord_bot_token", token),
                ("discord_channel_id in config/voice.toml", channel_id),
            )
            if not value
        ]
        if missing:
            return None, missing
        return DiscordTransport(token=token, channel_id=int(channel_id)), []

    if name == "loopback":
        from .transport.loopback import LoopbackTransport

        # Stay up long enough to read the API's answer. Without this the
        # check only proves the key was accepted, not the configuration.
        return LoopbackTransport(linger=voice_cfg.get("loopback_seconds", 5.0)), []

    return None, [f"unknown transport: {name}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator.voice", description=__doc__)
    parser.add_argument(
        "--transport",
        default="discord",
        help="discord (default), or loopback for a smoke test with no audio",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        config = load()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    voice_cfg = load_voice_config(config.config_dir)
    openai_key = read_secret(config.secrets_root, "openai_api_key")
    transport, missing = build_transport(args.transport, voice_cfg, config.secrets_root)
    if not openai_key:
        missing = [f"{config.secrets_root}/voice/openai_api_key", *missing]

    if missing:
        print("cannot start, missing:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        print("\nadd the references to .env, then re-run bootstrap.sh", file=sys.stderr)
        return 2

    from ..mcp_server import build_server

    server = build_server(Supervisor.create(config))
    session = RealtimeSession(
        api_key=openai_key,
        server=server,
        on_audio=transport.play,
        on_interrupt=transport.interrupt,
        model=voice_cfg.get("model", RealtimeSession.model),
        voice=voice_cfg.get("voice", RealtimeSession.voice),
        extra_instructions=voice_cfg.get("extra_instructions"),
    )

    try:
        asyncio.run(run(transport, session))
    except KeyboardInterrupt:
        log.info("call ended")
        return 0

    if args.transport == "loopback":
        if session.configured.is_set():
            log.info("loopback check passed: credentials and session configuration are good")
            return 0
        log.error(
            "connected, but the API never acknowledged the session configuration -- "
            "look for an error above; protocol.py holds every wire constant"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
