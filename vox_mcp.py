#!/usr/bin/env python3
"""Optional MCP server wrapping `vox`, for MCP-native agents (Claude Desktop,
etc.). The CLI is the primary interface; this just exposes the same engine as
tools. It reuses the warm daemon, so calls are fast and never overlap.

Run:   ./.venv/bin/python -m pip install mcp   # one-time
       ./.venv/bin/python vox_mcp.py

Register in an MCP client (stdio transport), e.g. claude_desktop_config.json:
  "vox": { "command": "/abs/path/vox/.venv/bin/python",
             "args": ["/abs/path/vox/vox_mcp.py"] }
"""

from __future__ import annotations

import sys

import vox  # the engine + daemon client live here

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.exit("vox_mcp: the 'mcp' package is required.  pip install mcp")

mcp = FastMCP("vox")


@mcp.tool()
def speak_text(text: str, voice: str = vox.DEFAULT_VOICE,
               speed: float = vox.DEFAULT_SPEED, wait: bool = False) -> str:
    """Read text aloud on the user's Mac to get their attention or give a spoken
    update. Use for things the user should hear, not read.

    Args:
        text: what to say (keep it short and spoken — a sentence or two).
        voice: voice id; default af_bella. See list_voices for options.
        speed: 0.5–2.0, default 1.1.
        wait: if true, block until speech finishes; default returns immediately.
    """
    ok = vox.speak_via_daemon(text, voice, vox.clamp_speed(speed), wait, "auto", quiet=True)
    if not ok:
        vox.Engine(engine="auto", quiet=True).speak(text, voice, vox.clamp_speed(speed))
    return f"spoke: {text[:80]}"


@mcp.tool()
def stop() -> str:
    """Stop any speech in progress and clear the queue."""
    vox._request({"cmd": "stop"})
    return "stopped"


@mcp.tool()
def list_voices() -> dict:
    """List available neural voice ids and their descriptions."""
    return dict(vox.VOICES)


if __name__ == "__main__":
    mcp.run()
