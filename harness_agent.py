#!/usr/bin/env python3
"""harness_agent - drive a reasoning harness behind the bridge, with a verification gate.

The bundled `responder` gives one model one turn per message. This is the heavier sibling: it drives
a full agentic *harness* (a coding/agent CLI such as OMP, Claude Code, opencode, ...) through an
investigation loop, and it will not let the harness's output reach a channel until that output has
passed a gate.

The shape, per round:

    task --> [ harness: reason over a corpus ] --> analysis
                                                     |
                          +--------------------------+
                          v
              [ deterministic gate (gate.py) ]  arithmetic + grounding, in Python
                          |  FAIL: feed the exact defects back, revise (next round)
                          v  PASS
              [ LLM adversarial judge (optional) ]  a DIFFERENT prompt, told to refute
                          |  FAIL: feed the defects back, revise
                          v  PASS
              [ promote to the channel via the bridge ]

Termination is truth-grounded: the loop stops because the deterministic checks pass, not because a
model declared itself satisfied. Nothing reaches a channel until a round passes both gates.

Air-gap: the harness sees only the prompt on stdin and returns text on stdout - it never receives the
Discord token and has no channel handle, and *this* process reaches the channel only through the
bridge's loopback `/egress`, like any other client. So nothing the harness emits can touch the
channel except as a gated post. The harness's OWN lockdown (no acting tools, no network) is NOT
enforced here - it is up to the `command` you configure (run it `--no-tools`, no network); this code
only pipes text through a command and cannot sandbox it.

Config lives in the `[harness]` block of `~/.config/agent-bridge/config.toml`; see
`config.example.toml` and the "Connecting a harness" section of the README (OMP worked example).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib

import client as bridge_client

DEFAULT_REASON = (
    "Reason over the attached corpus to answer the task below. Ground every factual claim in the "
    "corpus; tag any inference [INFER] and any missing coverage [gap]. You are doing analysis only - "
    "you cannot run code or touch hardware, so frame results as grounded hypotheses and checks, never "
    "as confirmed facts. End with a fenced code block holding the concise, channel-ready answer.\n\n"
    "TASK:\n{task}"
)

JUDGE_INSTRUCTION = (
    "You are an ADVERSARIAL reviewer. The attached corpus is ground truth. Below is an analysis to "
    "review; your job is to REFUTE it. Flag every claim that is (a) a value/identifier not in the "
    "corpus and not tagged [INFER]/[gap], (b) logically unsound, or (c) a check that would not "
    "actually distinguish its alternatives. Ignore anything correctly tagged as inference. Output "
    'exactly one fenced JSON object: {"verdict":"PASS"|"FAIL","defects":[{"claim":"...","why":"..."}]}. '
    "PASS only if you found no real defect.\n\nANALYSIS TO REVIEW:\n"
)
# NB: JUDGE_INSTRUCTION contains literal JSON braces, so it is concatenated with the analysis, never
# passed through str.format (which would read {"verdict":...} as a format field and raise KeyError).


class HarnessConfig:
    def __init__(self, d: dict):
        h = d.get("harness", {})
        self.command: str = h.get("command", "")           # reads the prompt on stdin, prints analysis
        self.judge_command: str = h.get("judge_command", self.command)
        self.corpus_path: str = os.path.expanduser(h.get("corpus_path", ""))
        self.reason_template: str = h.get("reason_template", DEFAULT_REASON)
        self.max_rounds: int = int(h.get("max_rounds", 3))
        self.use_judge: bool = bool(h.get("judge", True))
        self.timeout_secs: int = int(h.get("timeout_secs", 1800))
        self.wip_channel: int = int(h.get("wip_channel", 0))         # 0 = don't post WIP
        self.promote_channel: int = int(h.get("promote_channel", 0)) # 0 = don't promote
        b = d.get("bridge", {})
        self.bridge_url: str = h.get("bridge_url") or \
            f"http://{b.get('api_host', '127.0.0.1')}:{b.get('api_port', 8787)}"
        self.agent_handle: str = h.get("agent_handle", "harness")
        if not self.command:
            raise SystemExit("harness.command is required (see config.example.toml)")
        if "{task}" not in self.reason_template:
            sys.stderr.write("[harness] warning: reason_template has no {task} placeholder - the task "
                             "will not be inserted\n")


def load_cfg() -> HarnessConfig:
    path = os.environ.get("AGENT_BRIDGE_CONFIG", os.path.expanduser("~/.config/agent-bridge/config.toml"))
    with open(path, "rb") as fh:
        return HarnessConfig(tomllib.load(fh))


def read_corpus(path: str) -> str:
    """Read the corpus text (a file, or every *.md/*.txt under a directory) for the grounding check."""
    if not path or not os.path.exists(path):
        return ""
    if os.path.isfile(path):
        return open(path, encoding="utf-8", errors="replace").read()
    parts = []
    for root, _dirs, files in os.walk(path):
        for f in sorted(files):
            if f.endswith((".md", ".txt")):
                parts.append(open(os.path.join(root, f), encoding="utf-8", errors="replace").read())
    return "\n".join(parts)


def run_harness(command: str, prompt: str, timeout: int) -> str:
    """Run the harness command with `prompt` on stdin; return its stdout (empty on timeout). The
    command is responsible for attaching the corpus and running the harness locked down - see
    config.example.toml. A timeout returns "" so the caller treats it as a failed round, never a crash."""
    try:
        p = subprocess.run(command, shell=True, input=prompt, capture_output=True,
                           text=True, timeout=timeout)
        return p.stdout
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"[harness] command timed out after {timeout}s\n")
        return ""


def extract_block(text: str) -> str | None:
    """Return the last fenced code block (the channel-ready answer), or None."""
    blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
    for b in reversed(blocks):
        if len(b.strip()) > 24:
            return b.strip()
    return None


def llm_judge(cfg: HarnessConfig, analysis: str) -> tuple[bool, str]:
    """Run the adversarial judge; return (passed, defect_text). FAIL-CLOSED: a judge that will not
    return a parseable verdict (after one retry) does NOT pass - an unverifiable claim is not promoted.
    That can stall a run whose judge cannot emit JSON, which is the safe direction for a public gate."""
    for _attempt in range(2):
        out = run_harness(cfg.judge_command, JUDGE_INSTRUCTION + analysis, cfg.timeout_secs)
        m = re.search(r'\{.*"verdict".*\}', out, re.S)
        if not m:
            continue
        try:
            v = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        passed = str(v.get("verdict", "FAIL")).upper() == "PASS" and not v.get("defects")
        dtext = "\n".join(f"- {d.get('claim', '?')}: {d.get('why', '')}" for d in v.get("defects", []))
        return passed, dtext
    return False, "the adversarial judge returned no parseable verdict (fail-closed: not promoted)"


def _post_wip(cfg: HarnessConfig, bc, rnd: int, fails) -> None:
    """Best-effort per-round WIP note to the bots channel; a rejection is logged, never fatal."""
    if not cfg.wip_channel:
        return
    body = (f"[round {rnd}] gate rejected {len(fails)} claim(s), revising:\n"
            + "\n".join(f"- {d['detail']}" for d in fails))
    res = bc.post(body[:1900], channel_id=cfg.wip_channel)
    if not res.ok:
        sys.stderr.write(f"[harness] wip post not accepted: {res.reason or res.outcome}\n")


def investigate(cfg: HarnessConfig, bc, task: str, promote: bool) -> dict:
    corpus = read_corpus(cfg.corpus_path)
    if not corpus:
        sys.stderr.write("[harness] warning: no corpus text for the grounding check "
                         "(harness.corpus_path unset or empty)\n")
    import gate
    feedback = None
    block = None
    for rnd in range(1, cfg.max_rounds + 1):
        sys.stderr.write(f"\n[harness] ===== round {rnd}/{cfg.max_rounds} =====\n")
        prompt = cfg.reason_template.replace("{task}", task)   # not .format: a user template may hold braces
        if feedback:
            prompt += ("\n\nYOUR PRIOR OUTPUT HAD THESE VERIFIED DEFECTS - correct each and keep the "
                       "rest intact:\n" + feedback)
        analysis = run_harness(cfg.command, prompt, cfg.timeout_secs)
        if not analysis.strip():
            sys.stderr.write("[harness] harness produced no output - round failed\n")
            feedback = ("Your previous run produced no output. Return the analysis and end with a "
                        "single fenced code block holding the concise, channel-ready answer.")
            continue

        passed, defects = gate.run_gate(analysis, corpus)
        fails = [d for d in defects if d["severity"] == "FAIL"]
        sys.stderr.write(f"[harness] deterministic gate: {'PASS' if passed else 'FAIL'} "
                         f"({len(fails)} FAIL, {len(defects) - len(fails)} WARN)\n")
        for d in fails:
            sys.stderr.write(f"[harness]   FAIL {d['check']}: {d['detail']}\n")
        if not passed:
            feedback = "\n".join(f"- {d['check']}: {d['detail']}" for d in fails)
            _post_wip(cfg, bc, rnd, fails)
            continue

        # a survivor must carry a fenced channel-ready block; a wall of prose is not promoted
        block = extract_block(analysis)
        if block is None:
            sys.stderr.write("[harness] no fenced channel-ready block - round failed\n")
            feedback = ("End your answer with a single fenced code block (```) holding the concise, "
                        "channel-ready result. It was missing.")
            continue

        if cfg.use_judge:
            jok, jdefects = llm_judge(cfg, block)
            sys.stderr.write(f"[harness] llm judge: {'PASS' if jok else 'FAIL'}\n")
            if not jok:
                feedback = "Adversarial review defects:\n" + jdefects
                continue

        sys.stderr.write("[harness] ===== both gates pass =====\n")
        if promote and cfg.promote_channel:
            res = bc.post(block, channel_id=cfg.promote_channel)
            if not res.ok:
                reason = res.reason or res.outcome
                sys.stderr.write(f"[harness] promotion REJECTED by the bridge: {reason}\n")
                return {"round": rnd, "passed": False, "block": block,
                        "unresolved": f"bridge rejected the post ({reason}); an enforced channel "
                                      "needs a POSTING-SCHEMA-tagged block, or use a relaxed channel"}
            sys.stderr.write(f"[harness] promoted: message {res.payload.get('message_id')}\n")
        return {"round": rnd, "passed": True, "block": block}

    sys.stderr.write("[harness] ===== budget exhausted, not promoted =====\n")
    return {"round": cfg.max_rounds, "passed": False, "block": block, "unresolved": feedback}


def main():
    ap = argparse.ArgumentParser(description="Drive a reasoning harness behind the bridge, gated.")
    ap.add_argument("--task", required=True, help="the investigation task / question")
    ap.add_argument("--promote", action="store_true", help="promote a survivor to the promote channel")
    a = ap.parse_args()
    cfg = load_cfg()
    bc = bridge_client.BridgeClient(base_url=cfg.bridge_url, agent_handle=cfg.agent_handle,
                                    timeout=cfg.timeout_secs + 30)
    result = investigate(cfg, bc, a.task, a.promote)
    print(json.dumps({k: v for k, v in result.items() if k != "block"}))
    if result.get("block"):
        print("\n--- channel-ready block ---\n" + result["block"])
    sys.exit(0 if result.get("passed") else 2)


if __name__ == "__main__":
    main()
