"""The voice layer: a phone call to your agents.

Runs on the orchestration host. Your phone is a microphone and a speaker;
everything else happens here, which is why the MCP server stays on stdio
and never opens a port.

    phone (Discord app, anywhere)
       | voice channel
    Discord  <->  bot on this host  <->  gpt-realtime
                        |
                     in-process MCP -> supervisor -> agents
"""
