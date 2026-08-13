#!/usr/bin/env python3
"""
bridge.py - the #research-general transport. Discord <-> local agents, text in / text out.

Trust-model fact 3 (load-bearing): THIS PROCESS HAS NO PATH TO ANY EXECUTION SURFACE. It holds
exactly one credential - the Discord bot token - and nothing else. The agent-facing API is loopback
only. The systemd unit hardens that at the OS layer; assert_airgap() is the in-process guard.

Enforcement wired here (the sec 10 mechanical set that needs transport state):
  * egress sec 1/2/3/7 schema (enforce.check_egress), then BRIDGE-asserted provenance stamp
  * sec 7.7 length ceiling -> real Discord attachment upload (not a rejection)
  * sec 7.2 thread-lifetime PER THREAD: the bridge posts THREAD CLOSED - no yield. and gates further
    posts into a closed thread
  * sec 6 halt: a halt token marks its thread halted; egress into a halted thread is rejected
  * ingress untrusted-wrapping (enforce.wrap_ingress); co-located agents also receive each other's
    egress via the local ingress buffer (Discord drops the bot's own echo, so we fan out locally)
"""

from __future__ import annotations

import asyncio
import collections
import io
import os
import sys
import time
import tomllib
from typing import Deque, Dict, Optional

import discord
from aiohttp import web

import enforce

CONFIG_PATH = os.environ.get("AGENT_BRIDGE_CONFIG", "/etc/agent-bridge/config.toml")


class Config:
    def __init__(self, d: dict):
        b = d.get("bridge", {})
        self.guild_id: int = int(b.get("guild_id", 0) or 0)   # optional; routing is by channel_id
        self.channel_id: int = int(b["channel_id"])
        self.token_file: str = b["token_file"]
        self.archive_root: Optional[str] = b.get("archive_root") or None
        self.api_host: str = b.get("api_host", "127.0.0.1")
        self.api_port: int = int(b.get("api_port", 8787))
        self.rate_per_min: int = int(b.get("rate_per_min", 12))
        self.ingress_buffer: int = int(b.get("ingress_buffer", 500))

    def load_token(self) -> str:
        with open(self.token_file, "r") as f:
            return f.read().strip()


def load_config(path: str) -> Config:
    with open(path, "rb") as f:
        return Config(tomllib.load(f))


# --- air-gap self-check (Trust-model fact 3) ---------------------------------------------------
# A forwarded ssh-agent socket is a live credential path to other hosts. Passive ssh session
# descriptors (SSH_CONNECTION/SSH_CLIENT/SSH_TTY) are NOT an execution path - do not match those, or
# every dev run from an ssh shell false-trips.
# Matched as whole underscore-delimited tokens so a benign name whose token merely CONTAINS a keyword
# (PRODUCT vs PROD, WEBHOOKED vs WEBHOOK) does not false-trip. Multi-token phrases are matched as
# substrings of the whole key.
_FORBIDDEN_ENV_TOKENS = {"ACTUATE", "EXECUTE", "PROD", "WEBHOOK", "CRON", "REMOTETRIGGER"}
_FORBIDDEN_ENV_PHRASES = ("REMOTE_TRIGGER", "REMOTE_EXEC", "DEPLOY_KEY", "SSH_AUTH")
_ALLOWED_EXACT = {"AGENT_BRIDGE_CONFIG"}


def assert_airgap(cfg: Config) -> None:
    # HARD refusal: the agent API must be loopback-only. Binding it anywhere else is a real network
    # exposure (any host on the network could drive /egress or read /ingress), so this stays fatal.
    if cfg.api_host not in ("127.0.0.1", "::1", "localhost"):
        sys.stderr.write(
            f"AIR-GAP: refusing to start. api_host={cfg.api_host} is not loopback - the agent API "
            "must bind to 127.0.0.1 / ::1 only.\n"
        )
        raise SystemExit(3)
    # ADVISORY: a variable whose NAME looks like an execution-surface credential is a heuristic only.
    # It false-positives on benign names (a WEBHOOK/CRON/PROD var) and the real air-gap does not
    # depend on it: the bridge holds no execution handle, never reads arbitrary env, and runs
    # sandboxed. So WARN loudly (a real leak is still surfaced) but do NOT refuse to start.
    suspicious = []
    for key in os.environ:
        if key in _ALLOWED_EXACT:
            continue
        up = key.upper()
        if (set(up.split("_")) & _FORBIDDEN_ENV_TOKENS) or any(p in up for p in _FORBIDDEN_ENV_PHRASES):
            suspicious.append(key)
    if suspicious:
        sys.stderr.write(
            "AIR-GAP WARNING: the environment holds variable(s) whose names resemble an execution "
            "surface: " + ", ".join(suspicious) + ".\n  The bridge never reads them and the air-gap "
            "does not rely on their absence, but review them if unexpected.\n"
        )


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

    def refund(self) -> None:
        """Return the most-recently-charged token - used when a post was charged but the Discord
        send then failed, so a transport error does not consume a poster's budget."""
        if self._hits:
            self._hits.pop()


class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rate = RateLimiter(cfg.rate_per_min)
        # sec 7.2 per-thread: one ThreadState per Discord thread/channel id under the watched channel.
        self.threads: Dict[int, enforce.ThreadState] = {}
        self._ingress: Deque[dict] = collections.deque(maxlen=cfg.ingress_buffer)
        self._seq = 0
        self._new = asyncio.Event()

        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self.client.event(self.on_ready)
        self.client.event(self.on_message)

    # ---- routing helpers ----
    def _watched(self, channel) -> bool:
        """The configured channel and any thread whose parent is that channel (sec 7.2 threads)."""
        if channel.id == self.cfg.channel_id:
            return True
        return getattr(channel, "parent_id", None) == self.cfg.channel_id

    def _thread_state(self, tid: int) -> enforce.ThreadState:
        st = self.threads.get(tid)
        if st is None:
            st = enforce.ThreadState()
            self.threads[tid] = st
        return st

    def _buffer_ingress(self, sender_id: str, handle: str, body: str, thread_id: int, self_origin: bool):
        res = enforce.wrap_ingress(sender_id, handle, body)
        self._seq += 1
        self._ingress.append({
            "seq": self._seq, "ts": time.time(), "thread_id": thread_id,
            "provenance": res.provenance, "actuation_flagged": res.actuation_flagged,
            "halt": res.halt, "self_origin": self_origin, "text": res.text,
        })
        self._new.set()
        return res

    # ---- Discord -> agents (ingress) ----
    async def on_ready(self):
        sys.stderr.write(f"[bridge] connected as {self.client.user}, watching channel {self.cfg.channel_id}\n")

    async def on_message(self, message: "discord.Message"):
        if message.author == self.client.user:
            return  # our own forwarded posts are fanned to local agents at egress time, not here
        if not self._watched(message.channel):
            return
        tid = message.channel.id
        st = self._thread_state(tid)
        res = self._buffer_ingress(str(message.author.id), message.author.display_name,
                                   message.content, tid, self_origin=False)
        if res.halt:
            st.halted = True   # sec 6: this thread is halted
        if st.observe(enforce.first_tag(message.content)):
            await self._announce_closed(message.channel)

    async def _announce_closed(self, channel):
        try:
            await channel.send(enforce.THREAD_CLOSED_PREFIX + " (10 messages, no new tagged yield)")
        except Exception as e:  # posting the notice must never crash the bridge
            sys.stderr.write(f"[bridge] could not post THREAD CLOSED notice: {e}\n")

    # ---- agents -> Discord (egress), loopback HTTP ----
    async def handle_egress(self, request: "web.Request") -> "web.Response":
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "reason": "body must be JSON"}, status=400)
        body = payload.get("body", "")
        handle = str(payload.get("agent_handle", "agent"))
        # A target thread is optional; default to the root channel. The channel/thread must be watched.
        try:
            tid = int(payload.get("thread_id") or self.cfg.channel_id)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "reason": "thread_id must be an integer"}, status=400)

        st = self._thread_state(tid)
        if st.halted:
            return web.json_response(
                {"ok": False, "reason": "sec 6: thread is halted; open a fresh tagged post"}, status=409)
        if st.closed:
            return web.json_response(
                {"ok": False, "reason": "sec 7.2: thread is closed (no yield); open a new post"}, status=409)

        res = enforce.check_egress(body, archive_root=self.cfg.archive_root)
        if not res.ok and not res.route_as_attachment:
            return web.json_response(
                {"ok": False, "reason": res.reason, "void": res.void}, status=422)

        channel = self.client.get_channel(tid)
        if channel is None or not self._watched(channel):
            return web.json_response({"ok": False, "reason": "target channel not resolved/allowed"}, status=503)

        # Rate limit is charged only on a post that will actually reach Discord (after validation),
        # so a malformed flood cannot starve valid posters (rejects above never consume a token).
        if not self.rate.allow(time.time()):
            return web.json_response(
                {"ok": False, "reason": f"sec 10 rate limit: >{self.cfg.rate_per_min}/min"}, status=429)

        # BRIDGE-asserted provenance: the sender the channel actually sees is this bot; the local
        # agent handle is informational only (marked unverified), never a trust assertion (sec 10).
        bot_id = str(self.client.user.id) if self.client.user else "bridge"
        bot_handle = str(self.client.user) if self.client.user else "bridge"

        if res.route_as_attachment:
            # sec 7.7: upload the full body as a file; post a 3-line abstract, not a rejection.
            abstract = str(payload.get("abstract") or "\n".join(body.splitlines()[:3]))
            stamped = enforce.provenance_stamp(bot_id, bot_handle, f"{res.tag or '[ARTIFACT]'} {abstract}")
            fbuf = discord.File(io.BytesIO(body.encode()), filename="post.md")
            try:
                sent = await channel.send(content=stamped + "\n[full post attached: post.md]", file=fbuf)
            except Exception as e:
                self.rate.refund()
                return web.json_response({"ok": False, "reason": f"discord send failed: {e}"}, status=502)
            self._buffer_ingress(bot_id, f"{handle} (local, unverified)", body, tid, self_origin=True)
            # observe() cannot be the crossing message here: every attach-eligible tag is a YIELD_TAG,
            # so it resets the counter (returns False). Kept for counter hygiene if YIELD_TAGS changes.
            st.observe(res.tag)
            return web.json_response(
                {"ok": True, "routed_as_attachment": True, "message_id": str(sent.id)})

        stamped = enforce.provenance_stamp(bot_id, bot_handle, body)
        try:
            sent = await channel.send(stamped)
        except Exception as e:
            self.rate.refund()
            return web.json_response({"ok": False, "reason": f"discord send failed: {e}"}, status=502)
        # Fan the post to co-located sibling agents (Discord drops our own echo in on_message).
        self._buffer_ingress(bot_id, f"{handle} (local, unverified)", body, tid, self_origin=True)
        if res.tag is None:
            if enforce.is_halt_token(body):
                st.halted = True                              # sec 6 (em-dash or hyphen form)
            elif enforce.is_thread_closed_line(body):
                st.closed = True                              # sec 7.2 - agent-declared close
        if st.observe(res.tag):
            await self._announce_closed(channel)
        return web.json_response(
            {"ok": True, "tag": res.tag, "thread_closed": st.closed, "message_id": str(sent.id)})

    async def handle_ingress(self, request: "web.Request") -> "web.Response":
        try:
            since = int(request.query.get("since", "0"))
        except ValueError:
            since = 0
        deadline = time.time() + 25.0
        while True:
            # Clear BEFORE reading the buffer so a message buffered during the read still leaves the
            # event set (no lost-wakeup: the next wait() returns immediately instead of stalling 25s).
            self._new.clear()
            msgs = [m for m in self._ingress if m["seq"] > since]
            if msgs or time.time() >= deadline:
                return web.json_response({"messages": msgs, "cursor": self._seq})
            try:
                await asyncio.wait_for(self._new.wait(), timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                return web.json_response({"messages": [], "cursor": self._seq})

    async def handle_health(self, request: "web.Request") -> "web.Response":
        return web.json_response({
            "ok": True, "connected": self.client.is_ready(), "cursor": self._seq,
            "threads": {str(k): {"closed": v.closed, "halted": v.halted} for k, v in self.threads.items()},
        })

    async def run(self):
        if not self.cfg.archive_root:
            sys.stderr.write(
                "[bridge] WARNING: archive_root unset - every CLAIM_KIND=direct FINDING will be "
                "rejected as VOID (fail-closed). Set archive_root to enable direct-claim posts.\n")
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
    assert_airgap(cfg)
    bridge = Bridge(cfg)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
