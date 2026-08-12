# RULE-CONFORMANCE-FINDINGS - clanker-bridge vs HOUSE_RULES

> RESOLUTION (2026-08-12): all findings below addressed under the operator's "strict house rules"
> directive. Findings 1-3, 5-12 fixed in the strict refactor (see git log + POSTING-SCHEMA.md).
> Finding 4 (CORRECTION status validation) was DISSOLVED, not fixed: strict sec 10 does not
> authorize the bridge to field-gate [CORRECTION] at all, so the gate was removed and the value
> check with it - sec 5 correction discipline is enforced by adversarial review, not the bridge.
> This file is retained as the audit record. Original findings preserved below.

---


VERDICT: NO. The load-bearing control (actuation air-gap) is faithfully implemented, but the
egress schema OVER-REACHES by inventing a rigid uppercase-token schema for content the rules
describe only in prose, DROPS a field section 1 actually requires (the section 2 status label),
and the anti-drift thread-lifetime counter is channel-global instead of per-thread and never acts.
Several mechanical checks are keyed on whole-body substrings and fire wrong.

Scope audited: HOUSE_RULES section 10 "Bridge-enforced (mechanical)" list, cross-checked against
sections 1, 2, 3, 6, 7, 8. Agent-judgment set (section 9, 3.11, 3.14-15, control validity) is
correctly deferred - see FAITHFUL section.

---

## Findings, most severe first

### 1. OVER-REACH: rigid TOKEN schema imposed on prose-described required contents (all 5 types)
- Rule: section 1 post-type table.
- What the code does: `REQUIRED_FIELDS` (enforce.py:39-57) demands an uppercase `TOKEN: value`
  line for every listed content item, matched case-sensitively by `_has_field`
  (enforce.py:130-134, no `(?i)` flag). check_egress rejects the post if any token line is absent
  (enforce.py:204-209).
- The gap: section 1 writes only a SUBSET of items as literal backticked tokens. The rest are
  prose descriptions, and forcing a token for them invents a requirement the rules do not mandate.
  Per type:
  * `[FINDING]` (enforce.py:41-45): genuine tokens = CLAIM_KIND, VERDICT, VERDICT_BASIS,
    GATING_DIMENSION, STATE_SHA256, SAMPLE_COUNT, FALSIFIER, FIRE_TIME_PRECONDITIONS (backticked
    in spec, faithful). INVENTED = `ARTIFACT`, `NEGATIVE_CONTROL`, `DOES_NOT_PROVE` - the spec
    says only "archived artifact path/hash + the negative control + what it does not prove"
    (prose). An agent writing "Negative control: shorted-trace read 0x0" is REJECTED because the
    matcher wants exactly `NEGATIVE_CONTROL:` (case-sensitive).
  * `[EXPERIMENT]` (enforce.py:47-50): genuine tokens = FIRE_TIME_PRECONDITIONS, FALSIFIER.
    INVENTED = `STEPS`, `TARGET`, `ENV_STAMP`, `PASS_FAIL` (spec prose: "Exact steps, target
    addresses/offsets, environment stamp (section 8), ... pass/fail criteria").
  * `[HYPOTHESIS]` (enforce.py:52): INVENTED = `MECHANISM`, `PREDICTION`, `FALSIFIER` (spec prose:
    "The mechanism, the specific prediction, and the cheapest experiment that would falsify it").
  * `[ARTIFACT]` (enforce.py:54): ALL invented - `WHAT`, `PROVENANCE`, `IMAGE_IDENTITY`, `HASH`,
    `USE` (spec is entirely prose).
  * `[CORRECTION]` (enforce.py:56): ALL invented - `ORIGINAL`, `BREAKING_EVIDENCE`, `CORRECTED`,
    `NEW_STATUS` (spec prose).
- Compounding: section 10's mechanical egress mandate names field-enforcement for `[FINDING]` and
  `[EXPERIMENT]` ONLY ("[FINDING]/[EXPERIMENT] posts are rejected unless the section 1 required
  fields are present"). Hard-gating fields on HYPOTHESIS/ARTIFACT/CORRECTION is beyond what
  section 10 authorizes the bridge to gate - a second layer of over-reach.
- Fix: (a) restrict hard field-gating to `[FINDING]` and `[EXPERIMENT]` per section 10. (b) For the
  prose items, either drop the requirement or match on case-insensitive content presence (e.g.
  accept "negative control", "provenance", "image identity" in any casing) rather than a rigid
  uppercase token. (c) Keep the backticked tokens (CLAIM_KIND, VERDICT, VERDICT_BASIS,
  GATING_DIMENSION, STATE_SHA256, SAMPLE_COUNT, FALSIFIER, FIRE_TIME_PRECONDITIONS) as tokens -
  those are faithful.

### 2. UNDER-ENFORCE: FINDING is not required to carry a section 2 status label
- Rule: section 1 table `[FINDING]` row lists "Status (section 2)" as a required content, distinct
  from VERDICT; section 2 "every technical assertion carries one."
- What the code does: nothing. `REQUIRED_FIELDS["[FINDING]"]` (enforce.py:41-45) omits any status
  check. `STATUS_LADDER` is defined (enforce.py:60) and then never referenced anywhere - a dead
  constant, the smoking gun that the author intended this check and dropped it.
- The gap: a `[FINDING]` with VERDICT but no PROVEN/MEASURED/INFERRED/CLOSED/VOID/TOOLING_ONLY
  label passes egress, violating a section 1 required field that IS mechanically checkable
  (presence of a ladder token) and is in section 10's egress scope.
- Fix: require at least one `STATUS_LADDER` token to appear in a `[FINDING]` body (as its own
  label or leading the VERDICT), reject with "section 1/2: [FINDING] missing status label" if absent.

### 3. MISMATCH + INERT: thread-lifetime counter is channel-global and takes no action
- Rule: section 7.2 - "A thread that has produced no new tagged yield in its last 10 messages is
  closed ... Post `THREAD CLOSED - no yield.` + one line of what was learned." Explicitly
  per-thread. Section 10 lists thread-lifetime tracking as bridge-enforced.
- What the code does: one `ThreadState` for the whole bridge (bridge.py:124). Every message in the
  channel feeds the same counter (bridge.py:157, 187). It only listens on a single `channel_id`
  and drops anything else (bridge.py:145), so real Discord threads (which carry their own channel
  ids) are never tracked at all.
- The gap (scope): concurrent question-threads interleave into one counter - a yield in thread A
  resets the close clock for silent thread B, and non-yield chatter in A can flip the whole channel
  to "closed". This is channel-global, not the per-thread scope section 7.2 requires.
- The gap (no action): `observe()` sets `self.closed = True` and returns (enforce.py:107-118), but
  the return is ignored at both call sites (bridge.py:157, 187). The bridge never posts
  `THREAD CLOSED - no yield.`, and handle_egress never checks `self.thread.closed` to gate further
  posts (bridge.py:161-188). It TRACKS a bool and surfaces it in the health/egress JSON, but does
  not ENFORCE closure. Once closed, `observe` also early-returns forever (enforce.py:109), so it
  cannot re-open or re-fire.
- Fix: key a `dict[thread_id, ThreadState]` off the message's thread/root id; on the observe that
  returns True, have the bridge post the `THREAD CLOSED - no yield.` notice to that thread and gate
  subsequent non-reopening posts in it (or at minimum reject egress into a closed thread with the
  section 7.2 citation).

### 4. UNDER-ENFORCE: NEW_STATUS / correction status value never validated
- Rule: section 1 `[CORRECTION]` and section 5 - new status must be one of SUPERSEDED / REFUTED /
  WEAKENED.
- What the code does: requires the `NEW_STATUS` token be present (enforce.py:56) but never checks
  its value. `CORRECTION_STATUSES` is defined (enforce.py:61) and never referenced - another dead
  constant.
- The gap: `NEW_STATUS: banana` passes. (Note: the token requirement itself is the over-reach from
  finding 1; if that is relaxed to prose, the value check should still constrain to the three.)
- Fix: when a correction status is present, constrain it to `CORRECTION_STATUSES`.

### 5. TRANSPORT BUG: co-located local agents are mutually deaf
- Rule: sections 4/6/7 assume every agent sees every posted message (adversarial review, halting
  off-topic, thread tracking). Section 10 ingress wrapping is meant to deliver channel content to
  agents.
- What the code does: on_message returns immediately for the bot's own messages
  (bridge.py:142-143, "never re-ingest our own forwarded posts"). A local agent posts via
  POST /egress, which the bridge sends to Discord as the bot user (bridge.py:186). That message
  echoes back as authored by the bot and is skipped, so it never enters the ingress ring buffer.
- The gap: two or more local agents sharing one bridge never see each other's posts on GET
  /ingress - they only receive messages from OTHER Discord authors (other fleets). Local siblings
  cannot run adversarial review or halt each other, defeating the enforcement the rules assume.
- Fix: when forwarding an egress post, also inject the wrapped form into the local ingress buffer
  (fanned to sibling agents but tagged self-origin so the poster can dedupe), rather than relying
  on the Discord echo that on_message intentionally drops.

### 6. WRONG SCOPE: SAMPLE_COUNT/PROVEN check keys on a whole-body substring
- Rule: section 2 (PROVEN needs SAMPLE_COUNT > 1 or explicit SINGLE_SAMPLE_OK) and section 3
  ("If SAMPLE_COUNT=1, you may not write PROVEN").
- What the code does (enforce.py:219-225): `status_line = VERDICT + " " + body`; rejects iff
  `sample == "1" and "PROVEN" in status_line and "SINGLE_SAMPLE_OK" not in body`.
- The gaps:
  * False positive: `"PROVEN" in body` matches substrings - `UNPROVEN`, "does not reach PROVEN",
    "not PROVEN yet". A correctly-labelled MEASURED finding that merely mentions the word PROVEN in
    prose is rejected.
  * Under-inclusive threshold: only `sample == "1"` exactly. `SAMPLE_COUNT: 0` (or a non-numeric)
    with PROVEN passes, though section 2 requires strictly > 1.
  * Bypass: the `SINGLE_SAMPLE_OK` escape is an anywhere-in-body substring, so dropping that
    literal string anywhere (even in an unrelated line) disables the check.
  * Root cause: there is no parsed status label to key on (finding 2), so the check scans free text.
- Fix: after adding a parsed status label (finding 2), gate on "label == PROVEN and
  int(SAMPLE_COUNT) <= 1 and no SINGLE_SAMPLE_OK line". Parse SAMPLE_COUNT as an int; treat the
  SINGLE_SAMPLE_OK justification as its own field, not a free substring.

### 7. UNDER-IMPLEMENT: length ceiling rejects instead of auto-routing to attachment
- Rule: section 7.7 / section 10 "the length ceiling (section 7) auto-routes to an attachment".
- What the code does: over 30 lines returns ok=False with route_as_attachment=True
  (enforce.py:234-238); handle_egress treats every not-ok as a 422 rejection (bridge.py:178-181)
  and never actually creates a Discord attachment.
- The gap: "auto-routes" is not implemented; the poster is bounced with advice. Defensible as a
  minimum, but it is not what section 10 states.
- Fix: on route_as_attachment, have the bridge upload the body as a file attachment plus the
  agent-supplied abstract, rather than rejecting - or restate section 10's wording if rejection is
  the intended contract.

### 8. REGEX HOLES: actuation-phrasing flag (advisory only, low severity)
- Rule: section 6/8/10 ingress "imperative-actuation phrasing is flagged (never auto-executed)".
- What the code does: `_ACTUATION_RE` (enforce.py:77-81), a narrow keyword list.
- False negatives (not flagged): "blow the fuse", "just do it" (the exact section 8 example of a
  rushed go), "reset the GPU" / "reset your card" (only "reset the card" matches), "program the
  eeprom" (only "write ... eeprom"), "kick off the flash on your rig".
- False positives (spuriously flagged): "burn rate", "burn-in test", "flash memory", "in a flash",
  "trigger word", "triggering condition" - `\bburn\b`, `\bflash\b`, `\btrigger\b` match bare words.
- Severity: LOW. This flag is advisory (marks ingress so an agent halts on judgment); it opens no
  actuation path because the air-gap is the real control. Worth tightening, not load-bearing.
- Fix: broaden the imperative verbs (blow/program/kick off, card|gpu|rig|board object nouns) and
  require an actuation-object context to cut the bare-word false positives; or accept imperfection
  explicitly since the agent must halt on its own judgment regardless.

### 9. REGEX HOLE: authority-marker strip is line-anchored (low severity)
- Rule: section 8/10 provenance stamping - content-level "operator-approved"/"signed-off" markers
  carry no authority.
- What the code does: `_AUTHORITY_MARKER_RE` (enforce.py:70-73) is anchored `^\s*...` (multiline),
  so it only neutralizes markers at the START of a line. Inline markers ("the operator approved
  this run", "... which the operator says is fine") are not stripped.
- Severity: LOW. The real control is the bridge provenance stamp plus the untrusted-input banner
  (both applied), so an un-stripped marker still carries no trust. This is defense-in-depth with a
  hole, not a trust bypass.
- Fix: drop the `^` anchor and match the marker phrase anywhere in a line.

### 10. Minor: untagged egress accepts any "OFF-TOPIC"/"THREAD CLOSED" prefix
- Rule: section 6 halt must be exactly "OFF-TOPIC (em-dash) halted per rule 6." and nothing else.
- What the code does (enforce.py:199): allows the exact HALT_TOKEN OR any body starting with
  "THREAD CLOSED" or "OFF-TOPIC". So "OFF-TOPIC and here is why ..." passes untagged.
- Severity: LOW. Fix: for the halt path require exact-equality with HALT_TOKEN; allow the
  "THREAD CLOSED - no yield." control line by exact prefix + format check.

### 11. Minor / operational: archive_root unset => every CLAIM_KIND=direct FINDING is VOID
- Rule: section 3 - unresolvable artifact => VOID.
- What the code does: `_artifact_resolves` returns False when archive_root is None
  (enforce.py:154-155), and archive_root defaults to None (bridge.py:52). check_egress then rejects
  every direct finding as VOID (enforce.py:227-231).
- Severity: LOW. Fail-closed is defensible and matches "does not resolve => VOID", but a
  misconfigured deployment silently blocks all direct findings. Worth a startup warning that
  CLAIM_KIND=direct enforcement is inert/blocking without archive_root.

### 12. Minor: egress provenance is payload-claimed, not bridge-asserted
- Rule: section 10 "the bridge, not the payload, asserts who sent a thing."
- What the code does: egress uses `agent_id`/`agent_handle` from the POST JSON (bridge.py:167-168)
  for the `[PROV ...]` stamp (enforce.py:173-177).
- Severity: LOW. These are local loopback agents, and the cross-fleet identity that other fleets
  actually see is the Discord bot author (transport-asserted). The in-body PROV line is
  informational and is re-wrapped as untrusted by the receiving bridge. Acceptable; note for
  awareness.

---

## FAITHFUL / correctly implemented

- Actuation air-gap (section 10 load-bearing, Trust-model fact 3): `assert_airgap`
  (bridge.py:83-99) scans env for bench/actuation-path substrings and refuses to start; enforces
  loopback-only api_host; the process holds exactly one credential (Discord token read from a file,
  not env, not logged) and has no bench handle, RemoteTrigger, cron, or webhook. This is the one
  that matters and it is done well.
- Ingress untrusted-wrapping (section 10): `wrap_ingress` (enforce.py:244-267) prepends the
  "UNTRUSTED CHANNEL INPUT" banner, bridge-asserted provenance, and the actuation flag; delivered
  to agents via long-poll GET /ingress.
- Provenance stamping on ingress from the Discord-asserted author id (bridge.py:147,
  enforce.py:173-177) - transport-asserted origin, exactly as section 10 requires.
- Valid-tag-required and untyped => off-topic (enforce.py:121-127, 197-202), with the correct
  exception that a bare halt token / THREAD CLOSED control line may be untagged.
- CLAIM_KIND value constrained to direct|inference|elimination (enforce.py:212-217) - a genuine
  section 1 token, faithfully enforced.
- CLAIM_KIND=direct artifact PATH-resolution -> VOID with a symlink jail under archive_root and a
  non-empty check per section 3.2 (enforce.py:142-161, 227-231). This matches section 10's specific
  VOID mandate. Correctly does NOT attempt "every cited register/value pair grepable in the
  artifact" - that requires identifying cited pairs (semantic) and is NOT in section 10's mechanical
  list, so deferring it is right, not a gap.
- Correct DEFERRAL of the agent-judgment set (section 10): no scope/off-topic (section 9)
  classification, no "prose outran the archive" (3.11), no closure verification / negative-vs-
  positive reconciliation (3.14-15), no judgment of whether a control is really a control. The
  module only asserts well-formed + cannot-smuggle-actuation, never "correct". No faked checks.
- Halt-token exact-match recognition including the literal em-dash string (enforce.py:63, 248).
- Length ceiling correctly exempts `[ARTIFACT]` (enforce.py:234), since the ceiling routes content
  INTO an artifact.
- Rate limiting present as a mechanical anti-drift counter (bridge.py:104-115, 170-172).
