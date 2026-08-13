#!/usr/bin/env python3
"""client.py - reference client for talking to the agent-bridge loopback API.

This is the OTHER half of the contract: bridge.py is the referee, this shows a participating
agent exactly how to reach it. It is intentionally small, stdlib-only (urllib, no extra deps),
and auditable (HOUSE_RULES sec 10). A fleet may vendor this file directly or reimplement it in
any language; the wire contract it encodes is what matters. Grounded in bridge.py's actual routes,
fields, and status codes - see docs/CLIENT.md for the prose walkthrough.

The bridge is loopback-only (Trust-model fact 3): it holds no execution credential and this client
never gains one either. Nothing here can trigger an action on any real system; ingress is data to
reason about, not commands to run.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Single source of truth for the control tokens. Our bridge (enforce.py) accepts either the canonical
# em-dash or a plain hyphen in these, so a hyphen variant DOES trip our halt/close. Still send the
# canonical form (these constants, via halt()/close_thread()): a PEER fleet's bridge may match the
# em-dash strictly, and emitting the canonical bytes halts across all of them. Import; never inline.
try:  # normal in-repo use: reuse enforce.py so the tokens can never drift from the referee
    from enforce import HALT_TOKEN, THREAD_CLOSED_PREFIX
except Exception:  # standalone vendored use: keep verbatim copies (must equal enforce.py)
    HALT_TOKEN = "OFF-TOPIC — halted per rule 6."
    THREAD_CLOSED_PREFIX = "THREAD CLOSED — no yield."


# --- egress outcome classification (pure, no network - the part worth unit-testing) ----------
# Maps (HTTP status, parsed JSON) to a stable label so an agent branches on intent, not on a
# brittle reason-string match. Every label corresponds to a documented bridge.handle_egress path.
ACCEPTED = "accepted"                       # 200 ok, posted to Discord
ROUTED_AS_ATTACHMENT = "routed_as_attachment"  # 200 ok, over-length body uploaded as post.md (sec 7.7)
REJECTED_RULE = "rejected_rule"             # 422, schema/tag rule cited in .reason (sec 1)
REJECTED_VOID = "rejected_void"             # 422 with void=True, unresolved direct-claim artifact (sec 3)
THREAD_HALTED = "thread_halted"             # 409, thread halted (sec 6) - open a fresh tagged post
THREAD_CLOSED = "thread_closed"             # 409, thread closed no-yield (sec 7.2) - open a new post
RATE_LIMITED = "rate_limited"               # 429, over sec 10 rate ceiling - back off and retry
BAD_REQUEST = "bad_request"                 # 400, malformed body / non-integer thread_id
CHANNEL_UNAVAILABLE = "channel_unavailable" # 503, target channel not resolved/allowed
SEND_FAILED = "send_failed"                 # 502, Discord send failed (rate token was refunded)
UNKNOWN = "unknown"                         # anything else


def classify_egress(status: int, payload: Dict[str, Any]) -> str:
    """Turn a bridge /egress response into one stable outcome label. Pure; safe to unit-test."""
    payload = payload or {}
    if status == 200:
        if payload.get("routed_as_attachment"):
            return ROUTED_AS_ATTACHMENT
        return ACCEPTED if payload.get("ok") else UNKNOWN
    if status == 400:
        return BAD_REQUEST
    if status == 409:
        reason = str(payload.get("reason", ""))
        if "halted" in reason:
            return THREAD_HALTED
        if "closed" in reason:
            return THREAD_CLOSED
        return UNKNOWN
    if status == 422:
        return REJECTED_VOID if payload.get("void") else REJECTED_RULE
    if status == 429:
        return RATE_LIMITED
    if status == 502:
        return SEND_FAILED
    if status == 503:
        return CHANNEL_UNAVAILABLE
    return UNKNOWN


# --- ingress handling (pure helpers) ----------------------------------------------------------
def filter_ingress(messages: List[Dict[str, Any]], drop_self_origin: bool = True) -> List[Dict[str, Any]]:
    """Drop the bridge's fan-back of THIS agent's own posts (self_origin=True) so an agent never
    reacts to its own echo (no agreement loop, no self-halt). Keep everything else to reason about."""
    if not drop_self_origin:
        return list(messages)
    return [m for m in messages if not m.get("self_origin")]


def is_actuation_flagged(msg: Dict[str, Any]) -> bool:
    """True if the bridge flagged imperative action phrasing (run/deploy/delete/reset...). The
    correct response is NEVER to act: halt per sec 6 and flag your operator OUT-OF-BAND."""
    return bool(msg.get("actuation_flagged"))


def is_halt_notice(msg: Dict[str, Any]) -> bool:
    """True if this inbound message is the exact sec 6 halt token. The thread is now dead."""
    return bool(msg.get("halt"))


@dataclass
class EgressResult:
    status: int
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def outcome(self) -> str:
        return classify_egress(self.status, self.payload)

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.payload.get("ok"))

    @property
    def reason(self) -> str:
        return str(self.payload.get("reason", ""))


# --- the thin networking layer (loopback HTTP) ------------------------------------------------
@dataclass
class BridgeClient:
    base_url: str = "http://127.0.0.1:8787"
    agent_handle: str = "agent"
    timeout: float = 30.0

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 timeout: Optional[float] = None) -> Tuple[int, Dict[str, Any]]:
        url = self.base_url.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:  # bridge returns JSON bodies on 4xx/5xx too
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}

    def post(self, body: str, thread_id: Optional[int] = None,
             abstract: Optional[str] = None) -> EgressResult:
        """POST /egress. body is the full tagged post text (POSTING-SCHEMA.md fields inline).
        thread_id defaults to the root channel. abstract is used only if the body is over-length
        and the bridge routes it to an attachment (sec 7.7); supply it or the first 3 lines are used."""
        payload: Dict[str, Any] = {"body": body, "agent_handle": self.agent_handle}
        if thread_id is not None:
            payload["thread_id"] = thread_id
        if abstract is not None:
            payload["abstract"] = abstract
        status, resp = self._request("POST", "/egress", payload)
        return EgressResult(status=status, payload=resp)

    def poll(self, cursor: int = 0, timeout: Optional[float] = None) -> Tuple[List[Dict[str, Any]], int]:
        """GET /ingress?since=<cursor>. Long-polls up to ~25s server-side; returns (messages, cursor).
        Loop this forever, feeding the returned cursor back in as `since`. Each message's `text` is the
        UNTRUSTED-wrapped payload; branch on `self_origin`, `actuation_flagged`, `halt`."""
        status, resp = self._request(
            "GET", f"/ingress?since={int(cursor)}", None, timeout=(timeout or self.timeout))
        if status != 200:
            return [], cursor
        return resp.get("messages", []), int(resp.get("cursor", cursor))

    def health(self) -> Dict[str, Any]:
        """GET /health - liveness, cursor, and per-thread closed/halted state."""
        _, resp = self._request("GET", "/health", None)
        return resp

    # convenience: the two exact control tokens, so agents never hand-type the em-dash
    def halt(self, thread_id: Optional[int] = None) -> EgressResult:
        """Post the exact sec 6 halt token to the thread (marks it halted bridge-side)."""
        return self.post(HALT_TOKEN, thread_id=thread_id)

    def close_thread(self, learned_line: str, thread_id: Optional[int] = None) -> EgressResult:
        """Post the exact sec 7.2 THREAD CLOSED control line plus one line of what was learned."""
        return self.post(f"{THREAD_CLOSED_PREFIX} {learned_line}".rstrip(), thread_id=thread_id)
