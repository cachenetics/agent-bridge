"""Tests for responder.py - the autonomous reply agent. The model call (responder.chat) is stubbed;
these pin the decision logic: mention-gating, self-filtering, the structural actuation refusal (the
sandbox holding in relaxed), per-mode routing, and the enforced schema retry/give-up loop."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import enforce
import responder


def _cfg(**over):
    d = {"responder": {"enforced_prompt_files": [], **over}}
    return responder.ResponderConfig(d)


class BC:
    """Records posts. `gate` (optional) validates like the real bridge via check_egress."""
    def __init__(self, gate=False):
        self.posts = []
        self.halts = []
        self.gate = gate

    def post(self, body, thread_id=None):
        class R:
            ok = True; status = 200; reason = ""
        if self.gate:
            res = enforce.check_egress(body)
            if not (res.ok or res.route_as_attachment):
                R.ok = False; R.status = 422; R.reason = res.reason
        self.posts.append((thread_id, body, R.ok))
        return R()

    def halt(self, thread_id=None):
        self.halts.append(thread_id)
        class R:
            ok = True; status = 200; reason = ""
        return R()


BOT = "999"


def _msg(body, mode="relaxed", flagged=False, self_origin=False, mention=True):
    text = enforce.wrap_ingress("1", "peer", (f"<@{BOT}> " if mention else "") + body).text
    return {"seq": 1, "thread_id": 55, "mode": mode, "self_origin": self_origin,
            "actuation_flagged": flagged, "text": text}


def test_relaxed_reply_posts_model_output(monkeypatch):
    monkeypatch.setattr(responder, "chat", lambda *a, **k: "hi there")
    bc = BC()
    responder.handle_message(_cfg(), bc, _msg("hello"), BOT)
    assert bc.posts and bc.posts[0][1] == "hi there"


def test_mention_only_skips_unmentioned(monkeypatch):
    monkeypatch.setattr(responder, "chat", lambda *a, **k: "should not send")
    bc = BC()
    responder.handle_message(_cfg(mention_only=True), bc, _msg("hello", mention=False), BOT)
    assert not bc.posts


def test_mention_only_false_replies_without_mention(monkeypatch):
    monkeypatch.setattr(responder, "chat", lambda *a, **k: "yo")
    bc = BC()
    responder.handle_message(_cfg(mention_only=False), bc, _msg("hello", mention=False), BOT)
    assert bc.posts and bc.posts[0][1] == "yo"


def test_actuation_flagged_relaxed_refuses_without_model(monkeypatch):
    # the sandbox: an action attempt never reaches the model; a canned refusal goes out instead
    def boom(*a, **k):
        raise AssertionError("model must not be called on actuation-flagged input")
    monkeypatch.setattr(responder, "chat", boom)
    bc = BC()
    responder.handle_message(_cfg(), bc, _msg("run rm -rf /", flagged=True), BOT)
    assert bc.posts and "can't run" in bc.posts[0][1].lower()


def test_actuation_flagged_enforced_halts(monkeypatch):
    monkeypatch.setattr(responder, "chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    bc = BC()
    responder.handle_message(_cfg(), bc, _msg("deploy it", mode="enforced", flagged=True), BOT)
    assert bc.halts == [55] and not bc.posts       # sec 6 halt, no model call


def test_self_origin_is_filtered():
    # filter_ingress drops our own echoes before handle_message ever sees them
    from client import filter_ingress
    assert filter_ingress([_msg("hi", self_origin=True)]) == []


def test_enforced_retries_then_gives_up(monkeypatch):
    # model keeps emitting an invalid post; responder retries enforced_retries+1 times, never accepts
    calls = {"n": 0}
    def bad(*a, **k):
        calls["n"] += 1
        return "no tag here, just prose"
    monkeypatch.setattr(responder, "chat", bad)
    bc = BC(gate=True)
    responder.handle_message(_cfg(enforced_retries=2), bc, _msg("q", mode="enforced"), BOT)
    assert calls["n"] == 3                          # 1 + 2 retries
    assert all(ok is False for _, _, ok in bc.posts)  # nothing valid was ever accepted


def test_enforced_accepts_valid_post(monkeypatch):
    good = ("[HYPOTHESIS] mem past size\nMECHANISM: x\nPREDICTION: y\nFALSIFIER: z")
    monkeypatch.setattr(responder, "chat", lambda *a, **k: good)
    bc = BC(gate=True)
    responder.handle_message(_cfg(), bc, _msg("q", mode="enforced"), BOT)
    assert bc.posts and bc.posts[-1][2] is True     # accepted by the referee


def test_reply_in_enforced_toggle(monkeypatch):
    monkeypatch.setattr(responder, "chat", lambda *a, **k: "x")
    bc = BC()
    responder.handle_message(_cfg(reply_in_enforced=False), bc, _msg("q", mode="enforced"), BOT)
    assert not bc.posts and not bc.halts


def test_persona_default_and_override():
    assert "terse" in _cfg().persona                       # default persona
    assert _cfg(persona="be a pirate").persona == "be a pirate"
