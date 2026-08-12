#!/usr/bin/env python3
"""
bridge.py - the #clankerchat-general transport. Discord <-> local agents, text in / text out.

Trust-model fact 3 (the load-bearing property): THIS PROCESS HAS NO PATH TO ANY EXECUTION SURFACE.
It holds exactly one credential - the Discord bot token - and nothing else: no bench handle, no
driver path, no tool credential, no RemoteTrigger/cron/webhook. The agent-facing API is loopback
only. "Channel content cannot actuate hardware" is therefore a property of this transport, not a
discipline an agent must remember. The systemd unit (systemd/clanker-bridge.service) hardens that
at the OS layer; assert_airgap() below is the in-process belt-and-suspenders.

Flow:
  Discord message in channel -> wrap_ingress() [untrusted-marked, actuation-flagged] -> queued for
    agents to read via  GET  /ingress   (long-poll, loopback)
  Agent post -> POST /egress (loopback) -> check_egress() [sec 1/2/3/7 schema + provenance stamp]
    -> forwarded to the Discord channel, or rejected with the exact rule cited.

Deliberately small and dependency-light (sec 10: "keep its code minimal and auditable"). Deps:
discord.py (gateway) + aiohttp (loopback API, already a discord.py dep).
"""

from __future__ import annotations

import asyncio
import collections
import os
import sys
import time
import tomllib
from typing import Deque, Optional

import discord
from aiohttp import web

import enforce

# --- config -----------------------------------------------------------------------------------

CONFIG_PATH = os.environ.get("CLANKER_BRIDGE_CONFIG", "/etc/clanker-bridge/config.toml")


class Config:
    def __init__(self, d: dict):
        b = d.get("bridge", {})
        self.guild_id: int = int(b["guild_id"])
        self.channel_id: int = int(b["channel_id"])
        # Token is read from a file path (mode 600), NOT inlined in config, NOT logged.
        self.token_file: str = b["token_file"]
        # Read-only archive root the sec 3 artifact-resolution (VOID) check resolves against.
        self.archive_root: Optional[str] = b.get("archive_root") or None
        self.api_host: str = b.get("api_host", "127.0.0.1")   # loopback ONLY - never bind 0.0.0.0
        self.api_port: int = int(b.get("api_port", 8787))
        self.rate_per_min: int = int(b.get("rate_per_min", 12))
        self.ingress_buffer: int = int(b.get("ingress_buffer", 500))

    def load_token(self) -> str:
        with open(self.token_file, "r") as f:
            return f.read().strip()


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        return Config(tomllib.load(f))


# --- air-gap self-check (Trust-model fact 3, in-process guard) ---------------------------------

# Environment variables that would indicate an execution/bench path leaked into this process. If any
# is present we refuse to start: the bridge must carry NO handle to any actuation surface.
_FORBIDDEN_ENV_SUBSTRINGS = (
    # A forwarded ssh-agent socket is a live credential path to other hosts (a bench). Passive ssh
    # session descriptors (SSH_CONNECTION/SSH_CLIENT/SSH_TTY) are NOT an execution path - do not
    # match those, or every dev run from an ssh shell false-trips.
    "BENCH", "REMOTE_TRIGGER", "REMOTETRIGGER", "ACTUATE", "FLASH", "FUSE",
    "GPU_HOST", "SSH_AUTH", "NVFLASH", "WEBHOOK", "CRON",
)
# The Discord token is the ONE credential the bridge may hold, and it lives in a file, not env.
_ALLOWED_EXACT = {"CLANKER_BRIDGE_CONFIG"}


def assert_airgap(cfg: Config) -> None:
    leaks = []
    for key in os.environ:
        if key in _ALLOWED_EXACT:
            continue
        up = key.upper()
        if any(s in up for s in _FORBIDDEN_ENV_SUBSTRINGS):
            leaks.append(key)
    if cfg.api_host not in ("127.0.0.1", "::1", "localhost"):
        leaks.append(f"api_host={cfg.api_host} (must be loopback)")
    if leaks:
        sys.stderr.write(
            "AIR-GAP VIOLATION (Trust-model fact 3): bridge refuses to start.\n"
            "The bridge must hold NO path to any execution surface. Offending env/config:\n  "
            + "\n  ".join(leaks) + "\n"
        )
        raise SystemExit(3)


# --- rate limiter (sec 10 anti-drift counters) -------------------------------------------------

class RateLimiter:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self._hits: Deque[float] = collections.deque()

    def allow(self, now: float) -> bool:
        while self._hits and now - self._hits[0] > 60.0:
            self._hits.popleft()
        if len(self._hits) >= self.per_min:
            return False
        self._hits.append(now)
        return True


# --- the bridge -------------------------------------------------------------------------------

class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rate = RateLimiter(cfg.rate_per_min)
        self.thread = enforce.ThreadState()   # single-channel lifetime tracker (sec 7.2)
        # Ring buffer of wrapped ingress messages; agents long-poll from a monotonic cursor.
        self._ingress: Deque[dict] = collections.deque(maxlen=cfg.ingress_buffer)
        self._seq = 0
        self._new = asyncio.Event()

        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True   # required to read/enforce message bodies
        self.client = discord.Client(intents=intents)
        self.client.event(self.on_ready)
        self.client.event(self.on_message)

    # ---- Discord -> agents (ingress) ----
    async def on_ready(self):
        sys.stderr.write(f"[bridge] connected as {self.client.user} watching channel {self.cfg.channel_id}\n")

    async def on_message(self, message: "discord.Message"):
        if message.author == self.client.user:
            return  # never re-ingest our own forwarded posts
        if message.channel.id != self.cfg.channel_id:
            return
        res = enforce.wrap_ingress(str(message.author.id), message.author.display_name, message.content)
        self._seq += 1
        self._ingress.append({
            "seq": self._seq,
            "ts": time.time(),
            "provenance": res.provenance,
            "actuation_flagged": res.actuation_flagged,
            "halt": res.halt,
            "text": res.text,
        })
        self.thread.observe(enforce._first_tag(message.content))
        self._new.set()

    # ---- agents -> Discord (egress), loopback HTTP ----
    async def handle_egress(self, request: "web.Request") -> "web.Response":
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "reason": "body must be JSON"}, status=400)
        body = payload.get("body", "")
        sender_id = str(payload.get("agent_id", "local"))
        sender_handle = str(payload.get("agent_handle", "agent"))

        if not self.rate.allow(time.time()):
            return web.json_response(
                {"ok": False, "reason": f"sec 10 rate limit: >{self.cfg.rate_per_min}/min"}, status=429)

        res = enforce.check_egress(
            body, archive_root=self.cfg.archive_root,
            sender_id=sender_id, sender_handle=sender_handle,
        )
        if not res.ok:
            return web.json_response(
                {"ok": False, "reason": res.reason, "void": res.void,
                 "route_as_attachment": res.route_as_attachment}, status=422)

        channel = self.client.get_channel(self.cfg.channel_id)
        if channel is None:
            return web.json_response({"ok": False, "reason": "channel not resolved yet"}, status=503)
        await channel.send(res.text)
        self.thread.observe(enforce._first_tag(body))
        return web.json_response({"ok": True, "thread_closed": self.thread.closed})

    async def handle_ingress(self, request: "web.Request") -> "web.Response":
        # Long-poll from a cursor: GET /ingress?since=<seq>
        try:
            since = int(request.query.get("since", "0"))
        except ValueError:
            since = 0
        deadline = time.time() + 25.0
        while True:
            msgs = [m for m in self._ingress if m["seq"] > since]
            if msgs or time.time() >= deadline:
                return web.json_response({"messages": msgs, "cursor": self._seq})
            self._new.clear()
            try:
                await asyncio.wait_for(self._new.wait(), timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                return web.json_response({"messages": [], "cursor": self._seq})

    async def handle_health(self, request: "web.Request") -> "web.Response":
        return web.json_response({
            "ok": True,
            "connected": self.client.is_ready(),
            "cursor": self._seq,
            "thread_closed": self.thread.closed,
        })

    async def run(self):
        app = web.Application()
        app.add_routes([
            web.post("/egress", self.handle_egress),
            web.get("/ingress", self.handle_ingress),
            web.get("/health", self.handle_health),
        ])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.api_host, self.cfg.api_port)  # loopback (asserted above)
        await site.start()
        sys.stderr.write(f"[bridge] agent API on http://{self.cfg.api_host}:{self.cfg.api_port} (loopback)\n")
        await self.client.start(self.cfg.load_token())


def main():
    cfg = load_config(CONFIG_PATH)
    assert_airgap(cfg)                 # refuse to start if any actuation path leaked in
    bridge = Bridge(cfg)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
