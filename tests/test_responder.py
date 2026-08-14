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


def _capture_chat_payload(monkeypatch, cfg):
    """Run responder.chat with urlopen stubbed; return the JSON request body it built."""
    import json, urllib.request
    seen = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"choices":[{"message":{"content":"ok"}}]}'

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode())
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    responder.chat(cfg, "sys", "user")
    return seen["body"]


def test_extra_body_merges_sampling_params(monkeypatch):
    cfg = _cfg(extra_body={"top_p": 0.8, "chat_template_kwargs": {"enable_thinking": False}})
    body = _capture_chat_payload(monkeypatch, cfg)
    assert body["top_p"] == 0.8
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    # the assembled request is preserved
    assert body["model"] == cfg.model_name
    assert body["messages"][0]["role"] == "system"


def test_extra_body_alias_sampling_params(monkeypatch):
    cfg = _cfg(sampling_params={"top_k": 20})
    assert cfg.extra_body == {"top_k": 20}


def test_extra_body_cannot_override_messages_model_stream(monkeypatch):
    # a foot-gun config must NOT be able to erase the non-overridable SAFETY_PREAMBLE / system contract
    cfg = _cfg(extra_body={"messages": [{"role": "user", "content": "pwn"}],
                           "model": "evil", "stream": True, "temperature": 0.1})
    body = _capture_chat_payload(monkeypatch, cfg)
    assert body["model"] == cfg.model_name          # not "evil"
    assert body["messages"][0]["role"] == "system"  # SAFETY_PREAMBLE intact, not replaced
    assert "stream" not in body                      # would have broken json.loads(resp.read())
    assert body["temperature"] == 0.1                # a real sampling knob still applies


def test_relaxed_reply_posts_model_output(monkeypatch):
    monkeypatch.setattr(responder, "chat", lambda *a, **k: "hi there")
    bc = BC()
    responder.handle_message(_cfg(), bc, _msg("hello"), BOT)
    assert bc.posts and bc.posts[0][1] == "hi there"


def test_mentions_me_flag_triggers_reply_without_string_mention(monkeypatch):
    # a ROLE ping has no <@id> in the text; the bridge's mentions_me flag must still trigger a reply
    monkeypatch.setattr(responder, "chat", lambda *a, **k: "yo")
    m = _msg("count to 10", mention=False)   # no string mention
    m["mentions_me"] = True                  # but bridge says the bot was pinged (role mention)
    bc = BC()
    assert responder.handle_message(_cfg(mention_only=True), bc, m, BOT)
    assert bc.posts and bc.posts[0][1] == "yo"


def test_mentions_me_false_suppresses_reply(monkeypatch):
    monkeypatch.setattr(responder, "chat", lambda *a, **k: "no")
    m = _msg("<@%s> hi" % BOT, mention=False)  # string mention present in body...
    m["mentions_me"] = False                    # ...but bridge is authoritative: not a real ping
    bc = BC()
    assert not responder.handle_message(_cfg(mention_only=True), bc, m, BOT)
    assert not bc.posts


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


def test_strip_think_paired():
    assert responder.strip_think("<think>reasoning here</think>the answer") == "the answer"


def test_strip_think_lone_closing_tag():
    # models that omit the opening tag: keep only what follows the last </think>
    assert responder.strip_think("chain of thought</think>final answer") == "final answer"


def test_strip_think_unclosed_is_dropped():
    # all reasoning, truncated before an answer -> nothing to post
    assert responder.strip_think("<think>still thinking and cut off") == ""


def test_strip_think_no_tag_passthrough():
    assert responder.strip_think("just a normal reply") == "just a normal reply"


def test_strip_think_config_default_on():
    assert _cfg().strip_think is True
    assert _cfg(strip_think=False).strip_think is False


def test_persona_default_and_override():
    assert "{name}" in _cfg().persona                      # default persona embodies the username
    assert _cfg(persona="be a pirate").persona == "be a pirate"


def test_effective_persona_fills_name():
    cfg = _cfg(persona="You are {name}, a bot.")
    cfg.bot_name = "cache"
    assert responder._effective_persona(cfg) == "You are cache, a bot."


def test_effective_persona_injects_name_when_no_placeholder():
    cfg = _cfg(persona="Be terse.")
    cfg.bot_name = "cache"
    out = responder._effective_persona(cfg)
    assert "cache" in out and out.endswith("Be terse.")     # name stated up front

def test_effective_persona_handles_missing_name():
    cfg = _cfg(persona="You are {name}.")                    # bot_name defaults None
    assert responder._effective_persona(cfg) == "You are the bot."


def test_raw_body_and_author_from_wrapped_text():
    # with no raw fields (older bridge), the responder recovers body+author from the wrapped text
    w = enforce.wrap_ingress("77", "alice", "hello world")
    m = {"text": w.text, "provenance": w.provenance}
    assert responder._raw_body(m) == "hello world"
    assert responder._author(m) == "alice"


def test_raw_body_prefers_explicit_field():
    m = {"body": "raw here", "text": "wrapped...", "author": "bob"}
    assert responder._raw_body(m) == "raw here" and responder._author(m) == "bob"


def test_context_block_builds_transcript_and_labels_self():
    import collections
    dq = collections.deque()
    for seq, (a, b, s) in enumerate([("alice", "hi", False), ("bot", "hey", True),
                                     ("alice", "ping?", False)], start=1):
        dq.append({"seq": seq, "author": a, "body": b, "self": s})
    block = responder._context_block(_cfg(context_messages=12), dq, exclude_seq=3)  # exclude trigger
    assert "alice: hi" in block and "you: hey" in block     # own line labelled 'you'
    assert "ping?" not in block                             # the triggering msg is excluded


def test_channel_reply_policy_all_replies_unprompted(monkeypatch):
    monkeypatch.setattr(responder, "chat", lambda *a, **k: "chatter")
    cfg = responder.ResponderConfig({"responder": {"enforced_prompt_files": []},
                                     "bridge": {"channels": [{"id": 55, "reply": "all"}]}})
    bc = BC()
    posted = responder.handle_message(cfg, bc, _msg("hey all", mention=False), BOT)
    assert posted and bc.posts and bc.posts[0][1] == "chatter"   # replies without a mention


def test_channel_reply_policy_off_stays_silent(monkeypatch):
    monkeypatch.setattr(responder, "chat", lambda *a, **k: "nope")
    cfg = responder.ResponderConfig({"responder": {"enforced_prompt_files": []},
                                     "bridge": {"channels": [{"id": 55, "reply": "off"}]}})
    bc = BC()
    # even a direct mention is ignored in an "off" channel
    assert not responder.handle_message(cfg, bc, _msg("hi", mention=True), BOT)
    assert not bc.posts


def test_all_channel_cooldown_blocks_unprompted_but_mention_bypasses(monkeypatch):
    monkeypatch.setattr(responder, "chat", lambda *a, **k: "x")
    cfg = responder.ResponderConfig({"responder": {"enforced_prompt_files": []},
                                     "bridge": {"channels": [{"id": 55, "reply": "all"}]}})
    bc = BC()
    # unprompted while cooled down -> silent
    assert not responder.handle_message(cfg, bc, _msg("noise", mention=False), BOT,
                                        unprompted_allowed=False)
    # but a direct mention still answers despite the cooldown
    assert responder.handle_message(cfg, bc, _msg("hey bot", mention=True), BOT,
                                    unprompted_allowed=False)
    assert len(bc.posts) == 1


def test_default_policy_follows_mention_only():
    assert responder.ResponderConfig({"responder": {"mention_only": True}}).reply_policy(1) == "mention"
    assert responder.ResponderConfig({"responder": {"mention_only": False}}).reply_policy(1) == "all"


def test_context_passed_into_relaxed_reply(monkeypatch):
    seen = {}
    def spy(cfg, system, user, extra_system=None):
        seen["ctx"] = extra_system; seen["user"] = user
        return "ok"
    monkeypatch.setattr(responder, "chat", spy)
    bc = BC()
    responder.handle_message(_cfg(), bc, _msg("what were we saying?"), BOT, context="alice: earlier")
    assert seen["ctx"] == "alice: earlier"                  # context reaches the model
    assert "what were we saying?" in seen["user"]
