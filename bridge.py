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

# Surfaced in GET /health so a fleet can spot version skew across the channel. Bump the MINOR on any
# additive wire-contract change (a new /egress field, a new response key), the MAJOR on a breaking
# one (a removed/renamed field or status code). Patch for internal fixes with no contract change.
BRIDGE_VERSION = "1.6.0"


class Config:
    def __init__(self, d: dict):
        b = d.get("bridge", {})
        self.guild_id: int = int(b.get("guild_id", 0) or 0)   # optional; routing is by channel_id
        # Channels the bridge serves, each with a mode: "enforced" (full HOUSE_RULES) or "relaxed"
        # (free chat - relay + air-gap + provenance, but NO sec 1 schema gate and NO sec 7.2 lifecycle).
        # Back-compat: a single `channel_id` is one enforced channel.
        self.channels: dict = {}
        for ch in (b.get("channels") or []):
            self.channels[int(ch["id"])] = str(ch.get("mode", "enforced")).lower()
        if not self.channels:
            self.channels[int(b["channel_id"])] = "enforced"
        # Primary/default egress target (first configured), used when a post names no channel/thread.
        self.channel_id: int = next(iter(self.channels))
        self.token_file: str = b["token_file"]
        self.archive_root: Optional[str] = b.get("archive_root") or None
        self.api_host: str = b.get("api_host", "127.0.0.1")
        self.api_port: int = int(b.get("api_port", 8787))
        self.rate_per_min: int = int(b.get("rate_per_min", 12))
        self.ingress_buffer: int = int(b.get("ingress_buffer", 500))
        # Relaxed-channel replies over Discord's 2000-char cap are split into up to max_chunks messages;
        # beyond that they ride as a single file attachment (so nothing floods the channel).
        self.max_chunks: int = int(b.get("max_chunks", 4))
        # Server-side /ingress long-poll window (seconds a poll blocks before returning empty).
        self.poll_timeout_secs: float = float(b.get("poll_timeout_secs", 25.0))

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


def _is_forum(channel) -> bool:
    """A Discord forum channel - every post in it IS a thread, so egress with no thread_id means
    'start a new post' rather than 'send to the channel root' (forums have no free-text root)."""
    return getattr(channel, "type", None) == discord.ChannelType.forum


def _chunk_message(text: str, size: int = 1900):
    """Split text into <=size-char chunks for Discord's 2000-char cap, preferring newline then space
    boundaries so words/lines aren't cut mid-token; hard-splits only if a single run exceeds size."""
    chunks, rest = [], text
    while len(rest) > size:
        window = rest[:size]
        cut = window.rfind("\n")
        if cut < size // 2:
            cut = window.rfind(" ")
        if cut < size // 2:
            cut = size
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


def _resolve_forum_tags(channel, names):
    """Map requested tag NAMES to the forum's ForumTag objects (best-effort; unknown names ignored)."""
    if not names:
        return []
    want = {str(n).strip().lower() for n in names}
    return [t for t in (getattr(channel, "available_tags", None) or [])
            if getattr(t, "name", "").lower() in want]


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
    def _channel_mode(self, channel) -> Optional[str]:
        """Mode of this channel (or of its parent, for threads / forum posts), or None if unwatched.
        'enforced' = full HOUSE_RULES; 'relaxed' = free chat (relay only)."""
        if channel.id in self.cfg.channels:
            return self.cfg.channels[channel.id]
        pid = getattr(channel, "parent_id", None)
        if pid in self.cfg.channels:
            return self.cfg.channels[pid]
        return None

    def _watched(self, channel) -> bool:
        """Any configured channel, and any thread whose parent is a configured channel."""
        return self._channel_mode(channel) is not None

    def _thread_state(self, tid: int) -> enforce.ThreadState:
        st = self.threads.get(tid)
        if st is None:
            st = enforce.ThreadState()
            self.threads[tid] = st
        return st

    def _buffer_ingress(self, sender_id: str, handle: str, body: str, thread_id: int,
                        self_origin: bool, mode: Optional[str] = None, mentions_me: bool = False):
        res = enforce.wrap_ingress(sender_id, handle, body)
        self._seq += 1
        self._ingress.append({
            "seq": self._seq, "ts": time.time(), "thread_id": thread_id, "mode": mode,
            "author": handle, "body": body,   # raw components, for building display/context transcripts
            "mentions_me": mentions_me,       # authoritative: user-mention OR role-mention of a bot role
            "provenance": res.provenance, "actuation_flagged": res.actuation_flagged,
            "halt": res.halt, "self_origin": self_origin, "text": res.text,
        })
        self._new.set()
        return res

    def _mentions_me(self, message) -> bool:
        """True if this message pings the bot - directly (@user) OR via a role the bot holds (@role).
        Discord surfaces a role ping as a role_mention, not a user mention, so a role-only ping is
        invisible to naive @user matching; resolve it against the bot's own roles here."""
        me = self.client.user
        if me is not None and me in getattr(message, "mentions", []):
            return True
        guild = getattr(message, "guild", None)
        member = getattr(guild, "me", None) if guild is not None else None
        if member is not None:
            bot_roles = getattr(member, "roles", [])
            if any(r in bot_roles for r in getattr(message, "role_mentions", [])):
                return True
        return False   # deliberately NOT @everyone - the bot ignores broadcast pings
        return res

    # ---- Discord -> agents (ingress) ----
    async def on_ready(self):
        watched = ", ".join(f"{cid}:{mode}" for cid, mode in self.cfg.channels.items())
        sys.stderr.write(f"[bridge] connected as {self.client.user}, watching {watched}\n")

    async def on_message(self, message: "discord.Message"):
        if message.author == self.client.user:
            return  # our own forwarded posts are fanned to local agents at egress time, not here
        if not self._watched(message.channel):
            return
        tid = message.channel.id
        mode = self._channel_mode(message.channel)
        res = self._buffer_ingress(str(message.author.id), message.author.display_name,
                                   message.content, tid, self_origin=False, mode=mode,
                                   mentions_me=self._mentions_me(message))
        # sec 6/sec 7.2 lifecycle only applies to ENFORCED channels; relaxed (free chat) is relay-only.
        if mode != "enforced":
            return
        st = self._thread_state(tid)
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
        # Target resolution: thread_id (reply into a thread/post) takes precedence, else channel_id
        # (a specific configured channel root), else the primary channel. A forum root with no
        # thread_id == "start a new post" (a forum post IS a thread).
        explicit_thread = payload.get("thread_id")
        explicit_channel = payload.get("channel_id")
        try:
            if explicit_thread is not None:
                tid = int(explicit_thread)
            elif explicit_channel is not None:
                tid = int(explicit_channel)
            else:
                tid = self.cfg.channel_id
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "reason": "thread_id/channel_id must be an integer"}, status=400)

        # Resolve the channel first: its MODE decides the ruleset. 'relaxed' channels (free chat) skip
        # the sec 1 schema gate and the sec 6/7.2 lifecycle entirely - relay + air-gap + rate-limit only.
        channel = self.client.get_channel(tid)
        mode = self._channel_mode(channel) if channel is not None else None
        if channel is None or mode is None:
            return web.json_response({"ok": False, "reason": "target channel not resolved/allowed"}, status=503)
        if mode == "relaxed":
            return await self._egress_relaxed(channel, tid, body, handle)

        res = enforce.check_egress(body, archive_root=self.cfg.archive_root)
        if not res.ok and not res.route_as_attachment:
            return web.json_response(
                {"ok": False, "reason": res.reason, "void": res.void}, status=422)

        # A forum channel with no thread_id == "start a new post" (a forum post IS a thread).
        new_forum_post = _is_forum(channel) and explicit_thread is None
        title = payload.get("title")
        if new_forum_post and not title:
            return web.json_response(
                {"ok": False, "reason": "forum: a new post needs a `title` (or a `thread_id` to reply)"},
                status=400)

        # Halt/close gating applies only to an EXISTING thread or the text-channel root state - never
        # to a brand-new forum post (which has no prior state).
        if not new_forum_post:
            st = self._thread_state(tid)
            if st.halted:
                return web.json_response(
                    {"ok": False, "reason": "sec 6: thread is halted; open a fresh tagged post"}, status=409)
            if st.closed:
                return web.json_response(
                    {"ok": False, "reason": "sec 7.2: thread is closed (no yield); open a new post"}, status=409)

        # Rate limit is charged only on a post that will actually reach Discord (after validation).
        if not self.rate.allow(time.time()):
            return web.json_response(
                {"ok": False, "reason": f"sec 10 rate limit: >{self.cfg.rate_per_min}/min"}, status=429)

        # BRIDGE-asserted provenance: the sender the channel sees is this bot; the local agent handle
        # is informational only (marked unverified), never a trust assertion (sec 10).
        bot_id = str(self.client.user.id) if self.client.user else "bridge"
        bot_handle = str(self.client.user) if self.client.user else "bridge"

        if res.route_as_attachment:
            # sec 7.7: the full body becomes a file; the message carries a 3-line abstract.
            abstract = str(payload.get("abstract") or "\n".join(body.splitlines()[:3]))
            content = enforce.provenance_stamp(
                bot_id, bot_handle, f"{res.tag or '[ARTIFACT]'} {abstract}") + "\n[full post attached: post.md]"
            fbuf = discord.File(io.BytesIO(body.encode()), filename="post.md")
        else:
            content = enforce.provenance_stamp(bot_id, bot_handle, body)
            fbuf = None

        # Deliver: create a forum post (name=title) or send a message (reply / text-channel root).
        try:
            if new_forum_post:
                created = await channel.create_thread(
                    name=str(title), content=content, file=fbuf,
                    applied_tags=_resolve_forum_tags(channel, payload.get("tags")))
                sent, tid = created.message, created.thread.id   # tid is now the new post's id
            elif fbuf is not None:
                sent = await channel.send(content=content, file=fbuf)
            else:
                sent = await channel.send(content)
        except Exception as e:
            self.rate.refund()
            return web.json_response({"ok": False, "reason": f"discord send failed: {e}"}, status=502)

        # Fan the post to co-located sibling agents (Discord drops our own echo in on_message).
        self._buffer_ingress(bot_id, f"{handle} (local, unverified)", body, tid,
                             self_origin=True, mode="enforced")
        st = self._thread_state(tid)   # for a new forum post, tid is now that post's thread id
        if res.tag is None:
            if enforce.is_halt_token(body):
                st.halted = True                              # sec 6 (em-dash or hyphen form)
            elif enforce.is_thread_closed_line(body):
                st.closed = True                              # sec 7.2 - agent-declared close
        if st.observe(res.tag) and not new_forum_post:
            await self._announce_closed(channel)

        out = {"ok": True, "tag": res.tag, "thread_closed": st.closed, "message_id": str(sent.id)}
        if new_forum_post:
            out["thread_id"] = str(tid)          # the new post's id, so the agent can reply into it
        if res.route_as_attachment:
            out["routed_as_attachment"] = True
        return web.json_response(out)

    async def _egress_relaxed(self, channel, tid: int, body: str, handle: str) -> "web.Response":
        """Free-chat relay for a 'relaxed' channel: no sec 1 schema gate, no sec 6/7.2 lifecycle.
        The load-bearing air-gap (loopback bind + sandbox) and the outbound rate limit still apply;
        the bot's own identity is the attribution, so the body is relayed raw (no provenance stamp).
        Inbound is still wrapped untrusted at ingress regardless of channel mode."""
        if not body:
            return web.json_response({"ok": False, "reason": "body must be non-empty"}, status=400)
        if not self.rate.allow(time.time()):
            return web.json_response(
                {"ok": False, "reason": f"sec 10 rate limit: >{self.cfg.rate_per_min}/min"}, status=429)
        bot_id = str(self.client.user.id) if self.client.user else "bridge"
        # Discord's 2000-char cap: chunk into readable messages up to max_chunks; beyond that, attach.
        chunks = _chunk_message(body) if len(body) > 1900 else [body]
        attach = len(chunks) > self.cfg.max_chunks
        try:
            if attach:
                abstract = "\n".join(body.splitlines()[:3])
                sent = await channel.send(
                    content=abstract + "\n[full message attached: message.md]",
                    file=discord.File(io.BytesIO(body.encode()), filename="message.md"))
            else:
                sent = None
                for ch in chunks:
                    s = await channel.send(ch)
                    if sent is None:
                        sent = s   # report the FIRST message's id
        except Exception as e:
            self.rate.refund()
            return web.json_response({"ok": False, "reason": f"discord send failed: {e}"}, status=502)
        # Fan to co-located siblings (Discord drops our own echo in on_message).
        self._buffer_ingress(bot_id, f"{handle} (local, unverified)", body, tid,
                             self_origin=True, mode="relaxed")
        out = {"ok": True, "relaxed": True, "message_id": str(sent.id)}
        if attach:
            out["routed_as_attachment"] = True
        elif len(chunks) > 1:
            out["chunks"] = len(chunks)
        return web.json_response(out)

    async def handle_ingress(self, request: "web.Request") -> "web.Response":
        try:
            since = int(request.query.get("since", "0"))
        except ValueError:
            since = 0
        deadline = time.time() + self.cfg.poll_timeout_secs
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
        me = self.client.user
        bot_id = str(me.id) if me else None
        # The name to advertise is what the CHANNEL sees, not the raw account username: a per-guild
        # nickname or a global display name can differ from user.name. Prefer the guild member's
        # display_name (nick > global_name > username); fall back to the account name.
        bot_name = None
        if me is not None:
            bot_name = getattr(me, "global_name", None) or getattr(me, "name", None)
            get_guild = getattr(self.client, "get_guild", None)
            guild = get_guild(self.cfg.guild_id) if (get_guild and self.cfg.guild_id) else None
            member = getattr(guild, "me", None) if guild is not None else None
            if member is not None:
                bot_name = member.display_name
        return web.json_response({
            "ok": True, "version": BRIDGE_VERSION, "connected": self.client.is_ready(),
            "cursor": self._seq, "bot_id": bot_id, "bot_name": bot_name,
            "channels": {str(cid): mode for cid, mode in self.cfg.channels.items()},
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
