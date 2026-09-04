"""Run the voice bridge.

    python -m orchestrator.voice

Credentials come from the same place everything else's do: files in the
per-identity secrets directory that bootstrap materialises from 1Password.
Nothing is read from the environment and nothing is passed on a command
line.
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
from .discord_bot import VoiceBridge, build_bot
from .realtime import RealtimeSession

VOICE_IDENTITY = "voice"


def read_secret(root: Path, name: str) -> str | None:
    try:
        value = (root / VOICE_IDENTITY / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator.voice", description=__doc__)
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

    voice_cfg = {}
    voice_path = config.config_dir / "voice.toml"
    if voice_path.exists():
        with voice_path.open("rb") as fh:
            voice_cfg = tomllib.load(fh).get("voice", {})

    openai_key = read_secret(config.secrets_root, "openai_api_key")
    discord_token = read_secret(config.secrets_root, "discord_bot_token")
    channel_id = voice_cfg.get("discord_channel_id")

    missing = [
        name
        for name, value in (
            (f"{config.secrets_root}/voice/openai_api_key", openai_key),
            (f"{config.secrets_root}/voice/discord_bot_token", discord_token),
            ("discord_channel_id in config/voice.toml", channel_id),
        )
        if not value
    ]
    if missing:
        print("cannot start, missing:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        print("\nadd the references to .env and re-run bootstrap.sh", file=sys.stderr)
        return 2

    from ..mcp_server import build_server

    server = build_server(Supervisor.create(config))

    async def session_factory(loop):
        session = RealtimeSession(
            api_key=openai_key,
            server=server,
            on_audio=lambda pcm: bridge.on_model_audio(pcm),
            on_interrupt=lambda: bridge.on_interrupt(),
            model=voice_cfg.get("model", RealtimeSession.model),
            voice=voice_cfg.get("voice", RealtimeSession.voice),
            extra_instructions=voice_cfg.get("extra_instructions"),
        )
        bridge = VoiceBridge(session, loop)
        session.on_audio = bridge.on_model_audio
        session.on_interrupt = bridge.on_interrupt
        await session.connect()
        return bridge, session

    bot = build_bot(session_factory, int(channel_id), discord_token)
    bot.run(discord_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
