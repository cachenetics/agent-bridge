"""
Transport tests for bridge.py - pins the refactor's stateful fixes that live ONLY in the transport
(pass-1 findings 3/5, pass-2 H1/M3), with a stubbed Discord client (no network). Run:
  python -m pytest tests/test_bridge.py -q
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Transport tests need the runtime deps (discord, aiohttp). Skip cleanly if absent so a bare
# `pytest tests/` still runs the referee suite - but CI (.gitlab-ci.yml) installs the deps so these
# ALWAYS run there. A check that never runs is not a check; CI is the authoritative gate.
pytest.importorskip("discord")
pytest.importorskip("aiohttp")

import bridge  # noqa: E402
import enforce  # noqa: E402


# --- fakes ------------------------------------------------------------------------------------
class FakeChannel:
    def __init__(self, cid, parent_id=None):
        self.id = cid
        self.parent_id = parent_id
        self.sent = []       # list of (content, has_file)

    async def send(self, content=None, file=None):
        self.sent.append((content, file is not None))


class FakeUser:
    def __init__(self, uid=999):
        self.id = uid

    def __str__(self):
        return "clanker-bot#0001"


class FakeAuthor:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name

    def __eq__(self, other):
        return isinstance(other, FakeAuthor) and other.id == self.id


class FakeMessage:
    def __init__(self, author, channel, content):
        self.author = author
        self.channel = channel
        self.content = content


class FakeClient:
    def __init__(self, channel):
        self.user = FakeUser()
        self._channel = channel

    def is_ready(self):
        return True

    def get_channel(self, cid):
        return self._channel if self._channel.id == cid else None


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload
        self.query = {}

    async def json(self):
        return self._payload


def _bridge(archive_root=None):
    cfg = bridge.Config({"bridge": {"channel_id": 123, "token_file": "/dev/null",
                                    "archive_root": archive_root, "rate_per_min": 12}})
    b = bridge.Bridge(cfg)
    chan = FakeChannel(123)
    b.client = FakeClient(chan)
    return b, chan


def _resp(r):
    return r.status, json.loads(r.body)


def _finding(**kw):
    from test_enforce import _good_finding
    return _good_finding(**kw)


# --- gating -----------------------------------------------------------------------------------
def test_closed_thread_gates_egress():
    b, _ = _bridge()
    b._thread_state(123).closed = True
    status, body = _resp(asyncio.run(b.handle_egress(FakeRequest({"body": _finding()}))))
    assert status == 409 and "closed" in body["reason"]


def test_halted_thread_gates_egress():
    b, _ = _bridge()
    b._thread_state(123).halted = True
    status, body = _resp(asyncio.run(b.handle_egress(FakeRequest({"body": _finding()}))))
    assert status == 409 and "halted" in body["reason"]


def test_halt_token_egress_halts_thread():
    b, chan = _bridge()
    status, _ = _resp(asyncio.run(b.handle_egress(FakeRequest({"body": enforce.HALT_TOKEN}))))
    assert status == 200 and b._thread_state(123).halted


def test_hyphen_halt_token_egress_halts_thread():
    # A: the hyphen variant an AI agent would naturally type must halt the thread too.
    b, chan = _bridge()
    hyphen = enforce.HALT_TOKEN.replace("—", "-")
    status, _ = _resp(asyncio.run(b.handle_egress(FakeRequest({"body": hyphen}))))
    assert status == 200 and b._thread_state(123).halted


# --- valid post posts + fans out to siblings (pass-1 finding 5) --------------------------------
def test_valid_finding_posts_and_fans_out():
    b, chan = _bridge()
    status, body = _resp(asyncio.run(b.handle_egress(FakeRequest(
        {"body": _finding(), "agent_handle": "agentA"}))))
    assert status == 200 and body["ok"]
    assert len(chan.sent) == 1                       # posted to Discord once
    fanned = [m for m in b._ingress if m["self_origin"]]
    assert len(fanned) == 1                          # sibling agents can see it via /ingress
    assert "UNTRUSTED CHANNEL INPUT" in fanned[0]["text"]


# --- pass-2 H1: over-length routing still validates schema first -------------------------------
def test_overlength_valid_finding_uploads_attachment():
    b, chan = _bridge()
    pad = "\n" + "\n".join(f"pad {i}" for i in range(35))
    status, body = _resp(asyncio.run(b.handle_egress(FakeRequest({"body": _finding() + pad}))))
    assert status == 200 and body.get("routed_as_attachment")
    assert chan.sent and chan.sent[0][1] is True     # sent WITH a file attachment


def test_overlength_malformed_finding_still_rejected():
    b, chan = _bridge()
    pad = "\n" + "\n".join(f"pad {i}" for i in range(35))
    bad = _finding().replace("STATUS: MEASURED\n", "") + pad
    status, body = _resp(asyncio.run(b.handle_egress(FakeRequest({"body": bad}))))
    assert status == 422 and not chan.sent           # not posted, not attached


# --- pass-2 M3: malformed floods do not charge the rate limiter --------------------------------
def test_malformed_flood_does_not_starve_valid_poster():
    b, chan = _bridge()
    for _ in range(20):                              # 20 malformed, all rejected pre-rate-charge
        s, _b = _resp(asyncio.run(b.handle_egress(FakeRequest({"body": "no tag here"}))))
        assert s == 422
    status, body = _resp(asyncio.run(b.handle_egress(FakeRequest({"body": _finding()}))))
    assert status == 200 and body["ok"]             # a valid post still gets through


# --- per-thread lifetime + announce-once (pass-1 finding 3) ------------------------------------
def test_announce_closed_fires_once_via_on_message():
    b, chan = _bridge()
    author = FakeAuthor(7, "peer")
    for _ in range(enforce.THREAD_NO_YIELD_LIMIT):
        asyncio.run(b.on_message(FakeMessage(author, chan, "just chatter, no tag")))
    closed_notices = [c for (c, _f) in chan.sent if c.startswith(enforce.THREAD_CLOSED_PREFIX)]
    assert b._thread_state(123).closed and len(closed_notices) == 1
    # one more message must not announce again
    asyncio.run(b.on_message(FakeMessage(author, chan, "still chatter")))
    closed_notices = [c for (c, _f) in chan.sent if c.startswith(enforce.THREAD_CLOSED_PREFIX)]
    assert len(closed_notices) == 1


def test_bot_own_messages_are_not_ingested():
    b, chan = _bridge()
    # a message whose author == the bot must be skipped (no ingress, no double count)
    asyncio.run(b.on_message(FakeMessage(b.client.user, chan, "[FINDING] my own echo")))
    assert len(b._ingress) == 0


# --- rate limiter window ----------------------------------------------------------------------
def test_rate_limiter_evicts_after_window():
    rl = bridge.RateLimiter(2)
    assert rl.allow(1000.0) and rl.allow(1000.5)
    assert not rl.allow(1001.0)                     # third within the minute is blocked
    assert rl.allow(1061.1)                         # after 60s the window has evicted
