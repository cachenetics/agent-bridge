# RULE-CONFORMANCE-FINDINGS-PASS4 - clanker-bridge vs HOUSE_RULES (fourth, adversarial pass)

VERDICT: YES. The four pass-3 fixes (N1 [ARTIFACT] length routing, N2 canonical SAMPLE_COUNT,
N3 rate refund, N4 CI/importorskip) are correct and regression-free - I attacked each with concrete
inputs and could not break them. A fresh adversarial sweep across the whole surface (thread/state
routing, halt/closed gating, provenance forgeability, artifact-path jail, air-gap, deploy/systemd,
integer/regex/unicode edges) turned up NO rule violation, NO regression, and NO reachable execution
surface. The convergence is real: 12 -> 1 -> 1 -> 0 substantive. This is a clean pass.

Scope: re-derived the sec 10 "Bridge-enforced (mechanical)" list, checked line-by-line against
enforce.py + bridge.py + POSTING-SCHEMA.md, then ran the enforce suite (38/38 green) and executed
targeted adversarial probes (transcribed below). The agent-judgment set (sec 9, 3.11, 3.14-15,
control-validity, register-value-grepable) is correctly deferred, and the operator's INTENTIONAL
non-flag list is honored.

---

## Pass-3 fixes VETTED - all SOUND (concrete attempts that failed to break them)

### N1 - length ceiling no longer exempts [ARTIFACT] (enforce.py:264) - SOUND
- Code: `if body.count("\n") + 1 > LENGTH_CEILING_LINES:` with the `and tag != "[ARTIFACT]"` clause
  gone. Length check sits AFTER the [FINDING]/[EXPERIMENT] field-gating block (enforce.py:215-258),
  so the H1 schema-before-length ordering is preserved.
- Verified: SHORT `[ARTIFACT]` (`"[ARTIFACT] fuse dump v1.2 sha abcd"`) -> ok=True,
  route_as_attachment=False (posts inline). A 40-line `[ARTIFACT]` -> ok=False,
  route_as_attachment=True, tag="[ARTIFACT]" (attaches). Matches sec 7.7 exactly.
- Attachment path handles the tag: bridge.py:222 builds the abstract with `res.tag or '[ARTIFACT]'`
  and bridge.py:225 uploads the file; `[ARTIFACT]` is a plain tag through that path, no special case.
- No schema-order interaction: `[ARTIFACT]` is a TAG_ONLY type, so it never enters the field-gate
  block; it falls straight to the length check. No bypass of any field/STATUS/VOID check exists for
  it (there are none to bypass). No regression to the H1 fix (over-length malformed FINDING still
  422s, not attached - re-verified below).

### N2 - SAMPLE_COUNT canonical `^[0-9]+$` (enforce.py:245) - SOUND
- Verified REJECTED: `2_000`, `+2`, `-5`, `2.0`, `2 0`, `0x2`, `1e3`, `""`, and unicode `٢`
  (Arabic-Indic 2). Note the class is ASCII `[0-9]`, NOT `\d`, so `re.fullmatch(r"[0-9]+","٢")`
  is False - unicode digits cannot smuggle a value. `fullmatch` anchors both ends.
- Verified ACCEPTED: `2`, `10`, `0`, `000002` (leading zeros are canonical digits; int()=2), and a
  value with leading whitespace after the colon (`\s*` before the capture normalizes it) - all
  correct, no valid post wrongly rejected.
- PROVEN gate intact: `PROVEN` at SAMPLE_COUNT `1` and `0` both REJECTED; `2` and `000002` ACCEPTED;
  `SINGLE_SAMPLE_OK` field still lifts a single-sample PROVEN. The `label=="PROVEN" and sample<=1`
  check keys on the parsed int, so no leading-zero / large-value form evades it.

### N3 - RateLimiter.refund() on send exception (bridge.py:103-107, 227, 239) - SOUND
- Charge happens once at bridge.py:210 (`rate.allow`), AFTER all validation, before either send.
- Both send paths refund on failure: attachment branch (bridge.py:225 send / :227 refund+502) and
  normal branch (bridge.py:237 send / :239 refund+502). No third send path.
- No double-refund: each branch refunds at most once then returns. No charge-without-send-and-
  without-refund: between `rate.allow` and the sends there is no early return that skips both.
- Refund-without-prior-charge: refund guards `if self._hits:` and is only ever called on the failed-
  send path that is downstream of a charge in the same call, so the deque is non-empty.
- Cross-request identity nuance (benign): refund() pops the NEWEST hit, which under interleaved
  awaits could be a different request's token than the one that failed. This does NOT corrupt the
  limiter because it is COUNT-based and all hits are fungible within the 60s window: N charges minus
  M refunds = M-failures fewer, i.e. the surviving count equals the number of successful sends,
  regardless of which specific timestamp is popped. Not a finding.

### N4 - .gitlab-ci.yml + importorskip (test_bridge.py:19-20) - SOUND, no masking
- CI runs the transport tests: `.gitlab-ci.yml` does `pip install -r requirements.txt
  -r requirements-dev.txt`. discord.py and aiohttp live in requirements.txt (dev has only pytest),
  so both ARE installed in CI; `importorskip("discord")`/`("aiohttp")` pass and the transport suite
  runs. Locally (no deps) it skips cleanly, as intended.
- importorskip does NOT mask a real bug: only `discord` and `aiohttp` are importorskip-guarded;
  `import bridge` and `import enforce` are PLAIN imports. Proven empirically (scratchpad n4): with the
  guarded deps present, a syntax/import error in the unguarded module raises a pytest COLLECTION ERROR
  (non-zero exit, red CI), NOT a skip. So a bug in bridge.py/enforce.py fails loud in CI. importorskip
  skips ONLY when discord/aiohttp themselves are genuinely absent - the intended dep gate, never a
  code-bug mask.
- Residual (already accepted, not a new finding): if a future edit removed discord/aiohttp from
  requirements.txt, CI would go green with the transport tests silently skipped (importorskip returns
  0 on skip). Pass-3 accepted this by relying on requirements.txt; the alternative (fail-collection-
  on-skip) was noted-optional. Not re-raising - it is the documented, accepted N4 residual.

---

## Fresh hunt - attempted breaks, ALL held (no violation found)

### Per-thread state routing (root channel vs a real Discord thread) - no confusion, no collision
- `on_message` keys state on `tid = message.channel.id` (bridge.py:162): a root-channel message uses
  `channel_id`; a thread message uses the THREAD's own snowflake id, watched via
  `parent_id == channel_id` (bridge.py:129-133). Root and threads therefore get DISTINCT tids -
  Discord snowflakes are globally unique, so no root/thread state collision is constructible.
- `handle_egress` keys on `tid = int(thread_id or channel_id)` (bridge.py:187) and re-checks
  `_watched(channel)` after `get_channel` (bridge.py:204-206). An agent choosing a target thread is
  posting where it intends; there is no path to attribute a post to a thread it did not name. A
  falsy/omitted thread_id defaults to root; a non-integer 400s.
- Cross-source consistency: an ingress halt token in thread T and a local egress into T share the
  same `st` (keyed by T's id), so the halt correctly blocks local egress into T. No wrong-thread
  attribution between the ingress and egress paths.

### Halt / closed gating - no bypass, no improper reopen
- Closed thread: `st.closed` 409s egress (bridge.py:196-197); `observe()` early-returns once closed
  (enforce.py:122-123), so it cannot re-fire or reset. No reopen path exists; the on-topic core is a
  fresh post to a NEW thread id, exactly as sec 6/7.2 specify.
- Halted thread: `st.halted` 409s ALL egress incl. tagged posts (bridge.py:192-194) - correct, since
  sec 6 says a halted thread is dead and reopening is a fresh post, not resurrection of the same one.
  No un-halt / state-reset path.
- Asymmetry confirmed correct (matches pass-3): an INGRESS `THREAD CLOSED` from an external fleet does
  NOT set `st.closed` (on_message has no such branch) - right per sec 3.14 (a peer's closure is a
  claim to verify, never inherited). An INGRESS exact HALT token DOES set `st.halted` - right per sec
  6 (any agent enforces halt immediately). The halt-griefing vector is inherent to sec 6's unilateral
  halt design, with the spec'd remedy (fresh tagged post); it is faithful referee behavior, not a
  bridge defect.

### Provenance forgeability - none
- Egress is stamped with the BOT's transport id (bridge.py:216-217, 222, 235); the local agent handle
  is explicitly `(local, unverified)` (bridge.py:229, 242), never a trust assertion.
- A body that embeds a fake `[PROV sender=operator ...]` line does not spoof: the receiving bridge
  wraps the entire inbound content inside `--- begin/end untrusted body ---` delimiters with its OWN
  bridge-asserted PROVENANCE banner outside them (enforce.py:280-292). Any in-body PROV line - real
  (from the sending bridge) or forged - lands INSIDE the untrusted delimiters; only the receiver's
  banner is authoritative. Content-level authority markers are neutralized on every ingress incl. the
  local self-fanout (strip_authority_markers). No forge path.

### Air-gap / execution surface - unreachable
- The process holds one credential (Discord token from a 0600 file, never env, never logged), a
  loopback-only aiohttp API asserted at startup (bridge.py:79-80), and no subprocess/eval/os.system/
  bench/cron/webhook/RemoteTrigger handle. `_artifact_resolves` is the only agent-path filesystem
  touch: read-only realpath/isfile/getsize, symlink-jailed under archive_root, returning a bool - a
  weak existence oracle confined to the agent's OWN archive subtree (same trust domain). Attachment
  upload is in-memory io.BytesIO, no disk write.
- Artifact jail re-verified against a real tmp archive: `run/x.log` resolves; `nope.log`, `empty.log`
  (0-byte), `.`, `..`, `../../etc/passwd`, `run/../../../etc/passwd`, a symlink to /etc/passwd, and an
  absolute `/etc/passwd` ALL -> VOID (realpath canonicalizes before the `startswith(root+os.sep)`
  jail check); fragment/trailing-junk stripped; fail-closed VOID when archive_root is unset.
- assert_airgap env-scan is a tripwire, not the wall: it matches whole underscore tokens
  (BENCH/ACTUATE/FLASH/FUSE/NVFLASH/WEBHOOK/CRON/REMOTETRIGGER) + phrases (REMOTE_TRIGGER/GPU_HOST/
  SSH_AUTH). A creatively-named key (e.g. NVBENCH_HOST) would slip the token match, but a bench
  address in an env VALUE is INERT - no code reads it. The real control is the code holding no
  execution handle, which holds.
- systemd unit (systemd/clanker-bridge.service): NoNewPrivileges, ProtectSystem=strict,
  ProtectHome=read-only, PrivateDevices=true (no /dev bench node), MemoryDenyWriteExecute,
  RestrictAddressFamilies=AF_INET/AF_INET6/AF_UNIX, ReadOnlyPaths on the config. AF_UNIX is permitted
  (asyncio/aiohttp need it) but no code opens a bench unix socket, so it is not an actuation path.
  deploy.sh writes the token to a 0600 file, runs the air-gap self-check before enabling, and refuses
  to start the service while the token placeholder is present. No air-gap weakening in deploy/systemd.

### Integer / regex / unicode / length edges - clean
- SAMPLE_COUNT `[0-9]+` fullmatch (ASCII, unicode-safe as above). STATUS first-token parse
  (enforce.py:226-237) rejects `PROVEN:junk`, `PROVEN MEASURED`, `REVIEW_PENDING PROVEN`; accepts
  `PROVEN`, `proven`, `PROVEN REVIEW_PENDING`, `PROVEN, REVIEW_PENDING`, `MEASURED/REVIEW_CLEARED`.
  No IndexError on empty-after-replace. No regex in the module has catastrophic backtracking (all
  quantifiers bounded or linear; `_ACTUATION_RE` uses bounded `[^.\n]{0,30}`).
- Control-line exactness: the halt token requires the exact em-dash string (`==`); a hyphen variant
  or a halt-plus-tail is rejected as untyped. `THREAD CLOSED — no yield.` matches by exact prefix (to
  allow the "+ one line learned"). first_tag requires the tag as the leading token after lstrip; a
  mid-body tag is not recognized.
- Length ceiling is line-based per sec 7.7. A single enormous one-line body passes the line ceiling
  (1 line) but, if >2000 chars, fails at `channel.send` (Discord cap) -> caught -> refund + 502, so it
  is never silently posted. Enforcing a char ceiling instead would be over-reach vs the line-based
  rule; not a gap.

### H1/M1/M2/M3 regression re-check (the earlier fixes, still holding)
- Over-length malformed FINDING (missing STATUS, padded past 30 lines) -> 422, not attached (H1).
- Over-length direct-claim with a missing artifact -> VOID, not attached (H1 + VOID).
- Directory / empty-file / traversal / symlink artifacts -> VOID (M2 isfile+jail).
- Rejected posts never reach `rate.allow` (M3); the charge is downstream of validation.

---

## Yield-novelty and register-grepable (correctly NOT flagged)

- The thread-lifetime counter resets on any YIELD_TAG presence (enforce.py:130-131); an agent posting
  an empty-but-tagged `[HYPOTHESIS]` every 9 messages keeps a thread alive. "New tagged yield"
  (novelty, sec 7.2) is SEMANTIC and is the agent-judgment layer; the bridge mechanically detects tag
  presence only. Gating novelty would be over-reach. Deferred correctly.
- The direct-only scope of the VOID check (sec 10 names `CLAIM_KIND=direct`) means an
  inference/elimination FINDING with a non-resolving ARTIFACT passes the bridge - faithful to sec 10's
  narrower mandate; broadening it to sec 3's general "any cited path" would be over-enforcement.
  Deferred correctly.

---

## Net

No substantive finding. The three pass-3-era load-bearing controls (actuation air-gap, bridge-
asserted provenance, untrusted ingress wrapping) and the stateful transport duties (per-thread
lifetime + halt/closed gating, rate limiting) are faithful; the pass-3 fixes N1-N4 are correct and
regression-free; and the agent-judgment layer is correctly deferred. The bar was "perfect"; on this
audit it is met. Recommend closing the conformance-audit arc.
