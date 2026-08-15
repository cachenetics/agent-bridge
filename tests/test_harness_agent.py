"""Tests for harness_agent - the gated investigation loop. Fully offline: the harness is a fake that
returns canned text (routed reason-vs-judge by prompt), and the bridge is a stub that records posts and
returns a configured EgressResult. No Discord, no OMP, no network. Covers extract_block, the fail-closed
judge, and every branch of the loop (deterministic FAIL blocks + feeds back, empty/unfenced rounds fail,
clean promotes and checks the post, an enforced rejection is not a false success, budget exhaustion)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import client  # noqa: E402
import harness_agent as ha  # noqa: E402

# canned harness outputs -------------------------------------------------------------------------
CLEAN = "Analysis: every claim grounded.\n```\n[FINDING] the reverter is component X.\n```\n"
BAD_BIT = ("REG_A stock 0x00440000; the 0x00450000 write differs from stock only at bit 19.\n"
           "```\n[FINDING] component X, bit 19.\n```\n")   # bit_delta FAIL (real delta is bit 16)
NO_BLOCK = "A wall of prose with a conclusion but no fenced channel-ready block anywhere."
PASS_JSON = '```json\n{"verdict":"PASS","defects":[]}\n```'
FAIL_JSON = '```json\n{"verdict":"FAIL","defects":[{"claim":"X","why":"unsupported"}]}\n```'
GARBAGE = "I could not produce a verdict, sorry."


class FakeHarness:
    """Routes a call to the reason queue or the judge queue by inspecting the prompt."""
    def __init__(self, reason, judge=None):
        self.reason = list(reason)
        self.judge = list(judge or [])
        self.prompts = []

    def __call__(self, command, prompt, timeout):
        self.prompts.append(prompt)
        if "ADVERSARIAL reviewer" in prompt:
            if not self.judge:
                raise AssertionError("judge queue exhausted - test under-queued")
            return self.judge.pop(0)
        if not self.reason:
            raise AssertionError("reason queue exhausted - test under-queued")
        return self.reason.pop(0)


class StubBridge:
    def __init__(self, result):
        self.result = result
        self.posts = []

    def post(self, body, channel_id=None, **kw):
        self.posts.append({"body": body, "channel_id": channel_id})
        return self.result


def cfg(**over):
    h = {"command": "true", "corpus_path": "", "max_rounds": 3, "judge": False,
         "promote_channel": 555, "wip_channel": 0}
    h.update(over)
    return ha.HarnessConfig({"harness": h})


def ok_result():
    return client.EgressResult(200, {"ok": True, "message_id": 42})


def reject_result():
    return client.EgressResult(422, {"ok": False, "reason": "must start with a tag"})


# --- extract_block --------------------------------------------------------------------------------
def test_extract_block_returns_the_last_long_fence():
    text = ("```\nfirst fenced block, an early snippet over twenty-four chars\n```\n"
            "prose\n```\nsecond fenced block, the real channel-ready answer here\n```\n")
    assert ha.extract_block(text) == "second fenced block, the real channel-ready answer here"


def test_extract_block_skips_a_trailing_tiny_fence():
    # last-fence-wins must still skip a too-short trailing fence and take the earlier real answer
    text = ("```\nthe real answer, comfortably over the twenty-four char minimum\n```\n```\nok\n```\n")
    assert ha.extract_block(text) == "the real answer, comfortably over the twenty-four char minimum"


def test_extract_block_none_when_absent():
    assert ha.extract_block("no fences here at all") is None


# --- llm_judge (fail-closed) ----------------------------------------------------------------------
def test_judge_pass(monkeypatch):
    fake = FakeHarness(reason=[], judge=[PASS_JSON])
    monkeypatch.setattr(ha, "run_harness", fake)
    ok, defects = ha.llm_judge(cfg(judge=True), "some analysis")
    assert ok and defects == ""
    assert "some analysis" in fake.prompts[0]   # the analysis must actually reach the judge


def test_judge_pass_with_defects_does_not_pass(monkeypatch):
    j = '```json\n{"verdict":"PASS","defects":[{"claim":"X","why":"contradiction"}]}\n```'
    monkeypatch.setattr(ha, "run_harness", FakeHarness(reason=[], judge=[j]))
    ok, defects = ha.llm_judge(cfg(judge=True), "a")
    assert not ok and "contradiction" in defects   # a PASS verdict with defects is not a pass


def test_judge_retry_then_succeeds(monkeypatch):
    monkeypatch.setattr(ha, "run_harness", FakeHarness(reason=[], judge=[GARBAGE, PASS_JSON]))
    ok, _ = ha.llm_judge(cfg(judge=True), "a")
    assert ok   # first reply unparseable, retry parses PASS


def test_judge_fail(monkeypatch):
    monkeypatch.setattr(ha, "run_harness", FakeHarness(reason=[], judge=[FAIL_JSON]))
    ok, defects = ha.llm_judge(cfg(judge=True), "some analysis")
    assert not ok and "unsupported" in defects


def test_judge_unparseable_is_fail_closed(monkeypatch):
    monkeypatch.setattr(ha, "run_harness", FakeHarness(reason=[], judge=[GARBAGE, GARBAGE]))
    ok, defects = ha.llm_judge(cfg(judge=True), "some analysis")
    assert not ok and "fail-closed" in defects


# --- investigate loop -----------------------------------------------------------------------------
def test_clean_round_promotes_and_checks_post(monkeypatch):
    fake = FakeHarness(reason=[CLEAN])
    monkeypatch.setattr(ha, "run_harness", fake)
    bc = StubBridge(ok_result())
    r = ha.investigate(cfg(), bc, "task", promote=True)
    assert r["passed"] and r["round"] == 1
    assert len(bc.posts) == 1 and bc.posts[0]["channel_id"] == 555
    assert "[FINDING] the reverter is component X." in bc.posts[0]["body"]


def test_deterministic_fail_blocks_then_revises(monkeypatch):
    fake = FakeHarness(reason=[BAD_BIT, CLEAN])
    monkeypatch.setattr(ha, "run_harness", fake)
    bc = StubBridge(ok_result())
    r = ha.investigate(cfg(), bc, "task", promote=True)
    assert r["passed"] and r["round"] == 2
    assert len(bc.posts) == 1                     # nothing posted on the failed round
    # the failing bit and the revise instruction were fed into round 2's prompt
    assert "bit_delta" in fake.prompts[1] and "VERIFIED DEFECTS" in fake.prompts[1]


def test_empty_output_is_a_failed_round(monkeypatch):
    monkeypatch.setattr(ha, "run_harness", FakeHarness(reason=[""]))
    bc = StubBridge(ok_result())
    r = ha.investigate(cfg(max_rounds=1), bc, "task", promote=True)
    assert not r["passed"] and bc.posts == []


def test_unfenced_output_is_not_promoted(monkeypatch):
    monkeypatch.setattr(ha, "run_harness", FakeHarness(reason=[NO_BLOCK]))
    bc = StubBridge(ok_result())
    r = ha.investigate(cfg(max_rounds=1), bc, "task", promote=True)
    assert not r["passed"] and bc.posts == []


def test_enforced_rejection_is_not_a_false_success(monkeypatch):
    monkeypatch.setattr(ha, "run_harness", FakeHarness(reason=[CLEAN]))
    bc = StubBridge(reject_result())
    r = ha.investigate(cfg(), bc, "task", promote=True)
    assert not r["passed"]
    assert len(bc.posts) == 1 and "rejected" in r["unresolved"]


def test_budget_exhausted_never_promotes(monkeypatch):
    monkeypatch.setattr(ha, "run_harness", FakeHarness(reason=[BAD_BIT, BAD_BIT]))
    bc = StubBridge(ok_result())
    r = ha.investigate(cfg(max_rounds=2), bc, "task", promote=True)
    assert not r["passed"] and bc.posts == []


def test_judge_fail_then_pass(monkeypatch):
    monkeypatch.setattr(ha, "run_harness", FakeHarness(reason=[CLEAN, CLEAN], judge=[FAIL_JSON, PASS_JSON]))
    bc = StubBridge(ok_result())
    r = ha.investigate(cfg(judge=True), bc, "task", promote=True)
    assert r["passed"] and r["round"] == 2 and len(bc.posts) == 1


def test_wip_post_is_best_effort(monkeypatch):
    # a rejected WIP post must not crash the loop
    fake = FakeHarness(reason=[BAD_BIT, CLEAN])
    monkeypatch.setattr(ha, "run_harness", fake)
    bc = StubBridge(reject_result())   # every post is rejected, including the WIP note
    r = ha.investigate(cfg(wip_channel=777), bc, "task", promote=False)
    # round 1 posts a WIP note (rejected, logged), round 2 passes; promote=False so no promote post
    assert r["passed"] and any(p["channel_id"] == 777 for p in bc.posts)


def test_reason_template_with_braces_does_not_crash(monkeypatch):
    # a user reason_template containing literal braces must not crash the loop (.replace, not .format)
    fake = FakeHarness(reason=[CLEAN])
    monkeypatch.setattr(ha, "run_harness", fake)
    c = cfg(reason_template='Consider the shape {"k": 1} then answer {task}')
    r = ha.investigate(c, StubBridge(ok_result()), "the-task", promote=True)
    assert r["passed"]
    assert "the-task" in fake.prompts[0] and '{"k": 1}' in fake.prompts[0]


# --- read_corpus ----------------------------------------------------------------------------------
def test_read_corpus_file(tmp_path):
    p = tmp_path / "c.md"
    p.write_text("hello corpus")
    assert ha.read_corpus(str(p)) == "hello corpus"


def test_read_corpus_directory_reads_md_and_txt_only(tmp_path):
    (tmp_path / "a.md").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    (tmp_path / "skip.py").write_text("ignored code")
    out = ha.read_corpus(str(tmp_path))
    assert "alpha" in out and "beta" in out and "ignored" not in out


def test_read_corpus_missing_path_is_empty():
    assert ha.read_corpus("/nonexistent/path/xyzzy") == ""
