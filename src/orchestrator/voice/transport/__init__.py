"""How audio gets to and from the person.

Discord is the transport today, and it is worth being clear-eyed about why
that is a bet: **Discord has never officially supported receiving voice.**
py-cord's recording support and discord-ext-voice-recv both work by
reading a surface Discord does not document, has broken before, and is
actively changing -- their end-to-end encrypted voice protocol is exactly
the kind of change that ends it.

For one person's own bot that is an acceptable trade: it gives push-to-talk
from a pocket, anywhere, for nothing. But it is a trade with a known
failure mode, so the seam is real rather than notional. A transport owns
its own audio formats and hands the session 24 kHz mono PCM16 in both
directions; nothing above this package knows Discord exists.

If receive breaks, the replacements are already identified:

- **SIP.** The Realtime API accepts calls over SIP directly -- point a
  trunk at ``sip:$PROJECT_ID@sip.api.openai.com;transport=tls`` and accept
  the call from a webhook. A real phone number, on a supported API, working
  with no data connection. The costs are per-minute billing and a publicly
  reachable webhook, which this design has so far avoided needing.
- **A WebRTC or WebSocket page** over Tailscale. No third party in the
  audio path, but a browser tab needs the screen on, which is the reason
  Discord won in the first place.

Either is an adapter in this package. Nothing else moves.
"""

from .base import Transport

__all__ = ["Transport"]
