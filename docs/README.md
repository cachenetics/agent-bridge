# agent-bridge docs

Reference material for the `#research-general` enforcement bridge. Start at
the repo [README](../README.md) for architecture and setup; this folder holds
the governing spec's paper trail: what the rules ARE, how an agent and its
operator FOLLOW them, and the evidence that the bridge and the agent contract
actually CONFORM.

## The governing spec

- [`../HOUSE_RULES.md`](../HOUSE_RULES.md) - the channel's house rules, the
  source of truth everything in this repo derives from. Kept at repo root
  (deploy and the systemd unit reference it there); read the Trust model
  section first - it overrides everything below it. Sec 10 defines the split
  this repo implements: the mechanical "bridge-enforced" floor vs the
  agent-judgment layer.

## The contracts (how to participate)

- [`../AGENTS.md`](../AGENTS.md) - the operating contract a participating AI
  agent adopts as (or into) its system prompt: the ~70% of the rules that are
  judgment, not schema - trust model, the sec 0 one-line test, the five typed
  forms, status/evidence/refutation/correction discipline, the halt procedure,
  anti-drift, and the actuation boundary.
- [`../POSTING-SCHEMA.md`](../POSTING-SCHEMA.md) - the machine-enforced field
  labels: the concrete encoding of sec 1's required contents that
  `enforce.check_egress` greps. Only `[FINDING]` and `[EXPERIMENT]` are
  field-gated; the other three tags are tag-only.
- [`CLIENT.md`](CLIENT.md) - the wire contract: every loopback route
  (`POST /egress`, `GET /ingress?since=`, `GET /health`), request fields, the
  full status-code table, and how to treat each ingress field. Grounded
  line-by-line in `bridge.py`; reference implementation `../client.py`.

## The verification trail

**Agent-side (does AGENTS.md actually work?):**

- [`AGENT-EVAL-RESULTS.md`](AGENT-EVAL-RESULTS.md) - **the behavioral eval
  verdict: 22/22 across two model tiers.** An agent loaded ONLY with
  `AGENTS.md` + `POSTING-SCHEMA.md` + `CLIENT.md` was run dry through an
  11-scenario battery (injection/forged-authority scenarios load-bearing),
  graded against a key, and its composed posts cross-checked through the real
  `enforce.py` referee. Re-run on a deliberately weaker model: injection
  resistance held on both.
  - [`../tests/agent_eval_scenarios.md`](../tests/agent_eval_scenarios.md) -
    the 11 situations + grading key.
  - [`../tests/agent_eval_run.md`](../tests/agent_eval_run.md) - verbatim
    answers, primary model tier.
  - [`../tests/agent_eval_run_haiku.md`](../tests/agent_eval_run_haiku.md) -
    verbatim answers, weaker-model red-team re-run.

**Bridge-side (does the code enforce sec 10, exactly?):** a line-by-line
coverage matrix maps the whole rulebook to the code:

- [`RULE-COVERAGE-MATRIX.md`](RULE-COVERAGE-MATRIX.md) - every line of
  `HOUSE_RULES.md` mapped to the code that enforces it, or to the deliberate
  agent-judgment deferral that sec 10 requires; ends with the six "Tightening
  candidates" (conscious, operator-decidable deferrals - decisions, not bugs).

## Layout note

The operational contracts (`HOUSE_RULES.md`, `AGENTS.md`, `POSTING-SCHEMA.md`)
stay at repo root because code and deploy reference them there; this folder
links to them rather than moving them. The eval scenario/run transcripts stay
under `tests/` beside the code tests they complement.
