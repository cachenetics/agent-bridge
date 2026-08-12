# RULE-CONFORMANCE-FINDINGS-PASS2 - clanker-bridge vs HOUSE_RULES (second, adversarial pass)

> RESOLUTION (2026-08-12): all actionable findings addressed. H1 (length-before-field-gating
> bypass) FIXED - check_egress now validates schema/STATUS/SAMPLE/VOID before the length ceiling,
> pinned by 3 regression tests. M1 (STATUS review marker) FIXED - ladder keyed on first token,
> trailing REVIEW_PENDING/REVIEW_CLEARED allowed. M2 (directory-as-artifact) FIXED - now isfile.
> M3 (rate charged on rejects) FIXED - charged only on a post that reaches Discord. L1 (no transport
> tests) FIXED - tests/test_bridge.py added (12 tests, stubbed client). L3/L4/L6 FIXED (whole-token
> air-gap match, long-poll wakeup race, agent-declared THREAD CLOSED). L2 partially (added "just do
> it"/"do it now"; residual advisory imperfection accepted - the air-gap is the real control). L5
> (ring-buffer drop under load) ACCEPTED as documented at-least-window delivery. Retained as record.

---


VERDICT: WITH-CAVEATS. The load-bearing controls (actuation air-gap, bridge-asserted provenance,
untrusted ingress wrapping, per-thread lifetime + halt gating) are faithfully implemented and the
prior audit's structural bugs are genuinely fixed. But ONE named sec 10 mechanical duty is now
under-enforced: the sec 7.7 length-routing runs BEFORE field-gating, so any over-length
[FINDING]/[EXPERIMENT] silently skips the required-field, STATUS-ladder, PROVEN/SAMPLE, and
CLAIM_KIND=direct VOID checks. That is a real hole (H1). Plus a small over-reach on STATUS (M1),
a directory-passes-as-artifact gap (M2), and the entire transport layer (bridge.py) has ZERO tests
pinning the refactor's fixes. Fix H1 and the answer becomes YES.

Scope audited: HOUSE_RULES sec 10 "Bridge-enforced (mechanical)" list, re-derived and checked line
by line against enforce.py + bridge.py, cross-referenced to sec 1/2/3/6/7/8 and POSTING-SCHEMA.md.
Agent-judgment set (sec 9, 3.11, 3.14-15, control validity) is correctly deferred (see FAITHFUL).

Re-derived sec 10 mechanical list and status:
- Actuation air-gap ....................... FAITHFUL (bridge.py assert_airgap + loopback + 1 credential)
- Provenance stamping ..................... FAITHFUL (bridge-asserted id on ingress + egress)
- Ingress wrapping + actuation flag ....... FAITHFUL (advisory regex has residual holes, L2)
- Egress: valid tag ....................... FAITHFUL
- Egress: FINDING/EXPERIMENT field-gate ... UNDER-ENFORCED for over-length posts (H1)
- Egress: length ceiling -> attachment .... IMPLEMENTED but mis-ordered vs field-gate (H1)
- Egress: direct artifact unresolved=VOID . FAITHFUL, but (a) bypassed by H1, (b) dir passes (M2)
- Anti-drift: thread-lifetime ............. FAITHFUL per-thread + announce-once (fixed from pass 1)
- Anti-drift: rate limit .................. PRESENT but global + charged on rejects (M3)
- Halt-token handling ..................... FAITHFUL

---

## Findings, most severe first

### H1. UNDER-ENFORCE: length-routing precedes field-gating, so an over-length FINDING/EXPERIMENT skips its entire schema (and the VOID check)
- Rule: sec 10 "[FINDING]/[EXPERIMENT] posts are rejected unless the sec 1 required fields are
  present" AND "a CLAIM_KIND=direct post whose cited artifact path does not resolve is rejected as
  VOID". Both are named mechanical egress duties.
- What the code does: `check_egress` applies the length ceiling at enforce.py:211-215 and RETURNS
  (`route_as_attachment=True`) BEFORE the field-gating block (enforce.py:222-254). handle_egress
  then treats route_as_attachment as success and uploads the body (bridge.py:210-218) without ever
  calling the field/STATUS/SAMPLE/VOID logic.
- Concrete failing input: a `[FINDING]` with `CLAIM_KIND: direct`, `ARTIFACT: does/not/exist.log`,
  and NONE of the other required fields, padded past 30 lines:
  ```
  [FINDING] padded
  CLAIM_KIND: direct
  ARTIFACT: totally/missing.log
  <28 more lines of any prose>
  ```
  `body.count("\n")+1 > 30` and `tag != "[ARTIFACT]"` -> returns route_as_attachment. The post is
  ACCEPTED and uploaded, even though (a) it is missing STATUS/VERDICT/VERDICT_BASIS/... and (b) its
  direct-claim artifact does not resolve (should be VOID). Every FINDING/EXPERIMENT schema guarantee
  sec 10 promises evaporates for any post >=31 lines. Trivially self-triggered by padding.
- Severity: HIGH. It is the one place the refactor genuinely under-enforces a sec 10 mechanical
  rule, and the docstring/POSTING-SCHEMA both claim these checks hold.
- Fix: run field-gating (and STATUS/SAMPLE/CLAIM_KIND/VOID) FIRST; apply the length ceiling only
  after the post is schema-valid. i.e. move the enforce.py:211-215 block to AFTER the
  enforce.py:222-254 validation, or fall through to validation and set route_as_attachment on an
  otherwise-ok result. A well-formed over-length FINDING then attaches; a malformed one is still
  rejected. Add a test: over-length FINDING missing a field -> not ok, not route_as_attachment.

### M1. OVER-REACH: STATUS value must be exactly one ladder token, rejecting the sec 2 `PROVEN`+`REVIEW_PENDING` appended form
- Rule: sec 2 - review state is APPENDED to the status ("append REVIEW_PENDING ... A
  PROVEN/REVIEW_PENDING finding is fully PROVEN"); the marker "never raises or lowers the status
  label". So `PROVEN REVIEW_PENDING` is a rule-compliant status.
- What the code does: enforce.py:230-234 takes the whole STATUS value, `.upper()`, and requires
  `status in STATUS_LADDER` (exact membership). `_field_value` captures `(\S.*)`, i.e. the entire
  rest of the line.
- Concrete failing input: an otherwise-valid multi-sample finding with
  `STATUS: PROVEN REVIEW_PENDING` (or `PROVEN, REVIEW_PENDING`, or `PROVEN/REVIEW_PENDING`) is
  rejected with "STATUS must be a ladder token". POSTING-SCHEMA.md defines no REVIEW field, so an
  agent that follows sec 2 literally (append to status) is bounced. This is precisely the "over-
  gating rejects rule-compliant posts" failure the strict directive warns against.
- Severity: MEDIUM (a compliant path exists - put the marker on its own ignored line - so it is a
  footgun/ambiguity, not a hard wall; but the schema and sec 2 disagree on the record).
- Fix: key the ladder check on the FIRST whitespace token of the STATUS value
  (`status.split()[0]`), and optionally allow a trailing `REVIEW_PENDING`/`REVIEW_CLEARED` marker;
  or add a documented REVIEW field to POSTING-SCHEMA and keep STATUS single-token. Pin with a test.

### M2. WEAK CHECK: `_artifact_resolves` accepts a directory (and any non-empty node), so `ARTIFACT: .` passes the direct-claim VOID gate
- Rule: sec 3 / sec 3.2 - a cited artifact must resolve to real, non-empty evidence, else VOID.
- What the code does: enforce.py:174 returns `os.path.exists(candidate) and os.path.getsize(candidate) > 0`.
  `getsize` on a directory returns its inode size (commonly 4096, always > 0), and `exists` is true.
- Concrete failing input: `[FINDING] ... CLAIM_KIND: direct ... ARTIFACT: .` (or any directory that
  exists under archive_root) passes the VOID check - a directory is not an archived artifact file.
  `candidate == root` is even explicitly allowed at enforce.py:172.
- Severity: LOW-MEDIUM (the semantic "is it the right artifact" is deferred, but "resolves to a
  non-empty FILE" is the mechanical intent, and a bare `.` should not satisfy it).
- Fix: use `os.path.isfile(candidate) and os.path.getsize(candidate) > 0`; drop the `candidate == root`
  acceptance. Add a test: `ARTIFACT: .` with CLAIM_KIND=direct -> VOID.

### M3. Rate limiter is process-global and is charged before validation, so one agent's malformed flood starves all threads/agents
- Rule: sec 10 "rate limits" (granularity unspecified).
- What the code does: a single `RateLimiter` for the whole bridge (bridge.py:105, 184), consulted
  BEFORE `check_egress` (bridge.py:184 precedes bridge.py:196). A rejected (422) post still consumes
  a token; there is no per-agent or per-thread bucket.
- Concrete scenario: agent X POSTs 12 malformed bodies in a minute (all 422). The shared 12/min
  budget is exhausted; agent Y's valid FINDING in a different thread gets 429. A lazy/hostile agent
  can suppress everyone else's egress with junk that never even posts.
- Severity: LOW-MEDIUM (availability, not a trust/actuation bypass).
- Fix: charge the limiter only on a post that actually reaches Discord (move the `rate.allow` check
  to just before `channel.send`, after validation), and/or key a limiter per agent_handle/thread.

### L1. bridge.py transport has ZERO tests - every refactor fix (per-thread keying, announce-once, halt/closed gating, self-fanout, rate limiter) is unpinned
- Rule: this is a test-suite completeness finding against sec 10's transport-state duties.
- What the code does: all 28 tests exercise enforce.py only. bridge.py (Bridge, ThreadState wiring,
  handle_egress attachment path, `_announce_closed` fire-once, halt/closed 409 gating, local self-
  fanout dedup, RateLimiter, long-poll) is entirely untested.
- Why it matters: pass-1 findings 3 and 5 were BOTH transport bugs (global counter, mutually-deaf
  siblings). The refactor's claimed fixes for exactly those live only in bridge.py and nothing pins
  them - a future edit can silently reintroduce either. `enforce.ThreadState.observe` is tested, but
  the bridge's use of it (announce exactly once, gate egress on closed/halted, per-thread keying by
  `message.channel.id`) is not.
- Severity: LOW (no live defect proven here beyond H1/M3) but a real durability gap.
- Fix: add async tests for handle_egress/handle_ingress with a stubbed discord client: assert
  THREAD CLOSED fires once and gates subsequent egress (409); halt token 409s the thread; a self-
  origin egress lands in the ingress buffer for siblings exactly once; over-length routes to the
  attachment branch with ok True. Also pin RateLimiter window eviction.

### L2. Actuation-phrasing flag: residual false negatives an attacker phrases around; benign false positives
- Rule: sec 6/8/10 ingress "imperative-actuation phrasing is flagged (never auto-executed)". This
  is ADVISORY - the air-gap, not this flag, is the real control - so severity is capped LOW.
- What the code does: `_ACTUATION_RE` (enforce.py:84-93) requires an actuation verb bound to a
  hardware object within 20-30 chars.
- False negatives (not flagged): "just do it" (the exact sec 8 rushed-go example), any actuation
  where the object is >30 chars from the verb ("flash, once the bench frees up and you have a
  minute, the vbios"), and verbless actuation ("send 0xDE to the fuse register on your board").
- False positives (spuriously flagged): the `write` verb over benign hardware nouns - "write the
  firmware notes", "write results to the card log" -> flagged. Acceptable (over-flag is safe).
- Severity: LOW. Fix: accept the imperfection explicitly (agent judgment is the backstop) or add
  "just do it"/"do it now" and a bare verb+"it" heuristic; not load-bearing.

### L3. `assert_airgap` env matching is substring-based and over-broad (fail-closed)
- Rule: Trust-model fact 3 - no execution-surface path.
- What the code does: bridge.py:76 flags any env key whose upper() CONTAINS a forbidden substring.
  "FUSE" is a substring of REFUSE/CONFUSE; "CRON" of MICRON/SYNCRON...; "FLASH" of FLASHLIGHT.
- Effect: a benignly-named env var can refuse startup. This is the SAFE direction (fail-closed, the
  bridge just will not boot), not a bypass - noted for operability only.
- Severity: LOW. Fix: match whole tokens (split key on `_`) or an explicit allowlist of exact keys,
  rather than raw substring.

### L4. Long-poll `handle_ingress` has a lost-wakeup window (bounded by the 25s deadline)
- What the code does: bridge.py:236-244 computes `msgs` (empty), then `self._new.clear()`, then
  awaits `_new.wait()`. A message buffered between the list-comp and the clear() sets then-cleared
  `_new`; the waiter blocks until the next message or the 25s timeout.
- Effect: up to ~25s added latency on a race; no message is lost (it stays in the buffer and the
  next poll returns it). Severity: LOW. Fix: check the buffer again after clear(), or clear before
  the read.

### L5. Ingress ring buffer drops under load; a slow agent skips seqs silently
- What the code does: `_ingress = deque(maxlen=ingress_buffer)` (bridge.py:109). handle_ingress
  returns `cursor: self._seq`; a slow consumer advances `since` to the cursor and never sees dropped
  messages. Acceptable for chat, but adversarial review (sec 4) assumes every agent sees every post.
- Severity: LOW. Fix: document the at-least-buffer-window delivery guarantee, or block/paginate
  instead of silently dropping.

### L6. A manually posted `THREAD CLOSED - no yield.` egress does not set `st.closed`
- What the code does: an agent egressing the THREAD CLOSED control line hits the tag=None control
  path (ok, tag None) and only `st.observe(None)` runs (bridge.py:226); `st.closed` is set solely by
  the bridge's own 10-message auto-close. So after an agent declares a thread closed, the bridge
  keeps accepting posts into it.
- Severity: LOW (arguably agent-judgment; sec 10 lists only automatic thread-lifetime tracking as
  the bridge duty). Fix if desired: on an egress THREAD CLOSED control line, set `st.closed = True`.

---

## Attempted smuggle / bypass traces (air-gap held except via H1)

- Forge provenance: egress is stamped with the bot's transport id (bridge.py:207-208, 220), NOT the
  payload handle; the local handle is explicitly marked "(local, unverified)" (bridge.py:216, 223).
  Content-level authority markers are neutralized on every ingress path including the self-fanout
  (via wrap_ingress -> strip_authority_markers). No forge path found.
- Smuggle actuation past the air-gap: the process holds one credential (Discord token from file),
  the agent API is loopback-only and asserted so, and there is no bench/trigger/cron/webhook handle.
  The actuation "flag" is advisory only; even a total miss opens no execution path. Air-gap holds.
- Bypass a field check via code block / label inside a value: `_field_value` presence-matching keys
  off `^\s*LABEL[:=]` multiline, so an agent CAN satisfy a label from a fenced block in its own post
  - but that is the agent forging its own schema-compliance, which sec 10 explicitly leaves to
  adversarial review (well-formed != correct). Not a trust bypass. The one real bypass of the schema
  itself is H1 (padding past the length ceiling), not label placement.
- first_tag requires the tag as the leading token (enforce.py:136-142), and untyped bodies are
  rejected unless they are the exact halt token or a THREAD CLOSED-prefixed line - no untyped
  smuggle except the (deferred, semantic) "THREAD CLOSED - no yield. <off-topic tail>", which pass 1
  already noted and is agent-judgment.

## Confirmed faithful (correctly implemented, no defect)

- Actuation air-gap (sec 10 load-bearing): assert_airgap refuses to start on any forbidden env
  substring or non-loopback api_host (bridge.py:71-86); one credential, no execution handle. Solid.
- Per-thread lifetime tracking (fixes pass-1 finding 3): `Dict[int, ThreadState]` keyed by
  `message.channel.id` / thread parent (bridge.py:108, 122-133, 155-162); `observe` returns True on
  the crossing message only, and `_announce_closed` posts the notice exactly once (the bot's own
  announce is dropped by on_message, no recursion). Closed/halted threads 409 further egress
  (bridge.py:189-194).
- Local self-fanout (fixes pass-1 finding 5): egress is injected into the ingress buffer tagged
  self_origin so co-located siblings see each other's posts without relying on the dropped Discord
  echo (bridge.py:216, 223); exactly one copy, no cross-bridge echo loop.
- Length ceiling now actually uploads an attachment + abstract instead of bouncing (fixes pass-1
  finding 7) - bridge.py:210-218 (the ORDER relative to field-gating is the H1 defect, but the
  attachment mechanism itself is correct and [ARTIFACT] is correctly exempt, enforce.py:211).
- STATUS ladder now enforced (fixes pass-1 finding 2): STATUS_LADDER is live at enforce.py:230-234
  (M1 is that it is slightly too strict, not that it is missing).
- SAMPLE_COUNT parsed as int; PROVEN needs >1 or a labeled SINGLE_SAMPLE_OK field, not a substring
  (fixes pass-1 finding 6) - enforce.py:240-249; UNPROVEN no longer false-trips (tested).
- Authority-marker strip now matches inline, not just line-start (fixes pass-1 finding 9) -
  enforce.py:75-78, applied on every ingress incl. self-fanout.
- CLAIM_KIND=direct artifact VOID with a realpath symlink jail + non-empty check
  (enforce.py:161-174) - correct except the directory-passes gap (M2) and the H1 bypass.
- Field-gating restricted to [FINDING]/[EXPERIMENT]; [HYPOTHESIS]/[ARTIFACT]/[CORRECTION] are
  tag-only (enforce.py:46-61, 217-219). This is the correct strict reading of sec 10 - NOT re-flagged.
- Correct deferral of the agent-judgment set: no sec 9 scope check, no 3.11/3.14-15, no control-
  validity judgment, no "cited register/value grepable in artifact". No faked semantic checks.
- Halt token exact-match incl. the literal em-dash string (enforce.py:67, 146-148, 262); THREAD
  CLOSED control line recognized. Tests pin both directions for the checks they cover.
