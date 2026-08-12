# RULE-CONFORMANCE-FINDINGS-PASS3 - clanker-bridge vs HOUSE_RULES (third, adversarial pass)

> RESOLUTION (2026-08-12): all four findings addressed. N1 (length ceiling exempted [ARTIFACT])
> FIXED - the exemption is removed; an over-length [ARTIFACT] now routes to attachment like every
> other tag, pinned by 2 tests. N2 (SAMPLE_COUNT via bare int) FIXED - now requires a canonical
> ^[0-9]+$, rejecting "2_000"/"+2"/"-5"/unicode digits, pinned. N3 (rate token charged on a failed
> send) FIXED - RateLimiter.refund() returns the token if channel.send raises (502). N4 (transport
> tests uncollectable without deps + no CI) FIXED - .gitlab-ci.yml installs runtime+dev deps and runs
> the full suite; test_bridge.py importorskips so a bare `pytest tests/` skips cleanly while CI is the
> authoritative gate. The pass-3 "confirmed sound" verification of the H1/M1/M2/M3 fixes stands.
> 48 tests pass (CI env); deploy self-check green. Retained as record.

---


VERDICT: WITH-CAVEATS. The four pass-2 fixes (H1 schema-before-length, M1 STATUS first-token
parse, M2 isfile+jail, M3 rate-after-validation) are SOUND and introduce no regressions - I tried
hard to break each and could not. The load-bearing controls (actuation air-gap, bridge-asserted
provenance, untrusted ingress wrapping, per-thread lifetime + halt/closed gating) hold. ONE
pre-existing mechanical gap remains that both prior passes waved through as "faithful": the sec 7.7
length ceiling EXEMPTS the `[ARTIFACT]` tag (enforce.py:259), so an agent can dump unbounded inline
content by tagging it `[ARTIFACT]`, bypassing a named sec 10 mechanical duty (N1 below). Everything
else I probed is either correct or a semantic item the bridge rightly defers.

Scope: re-derived the sec 10 "Bridge-enforced (mechanical)" list, checked line-by-line against
enforce.py + bridge.py, then attacked the pass-2 fixes and hunted fresh breaks. enforce.py suite is
green (35/35). test_bridge.py is uncollectable in this env (no discord/aiohttp); transport fixes
audited by reading (see "Test soundness" - they PIN, not vacuous).

---

## Findings, most severe first

### N1. UNDER-ENFORCE (NEW / re-opened): sec 7.7 length ceiling exempts `[ARTIFACT]`, permitting unbounded inline content
- Rule: sec 7.7 "More than ~30 lines -> it is an [ARTIFACT]. Attach the file / link the doc, post a
  3-line abstract." sec 10 lists "the length ceiling (sec 7) auto-routes to an attachment" as a
  BRIDGE-ENFORCED mechanical duty. Line-counting is mechanical; nothing in sec 7.7 or sec 10 carves
  out the `[ARTIFACT]` tag.
- Code: enforce.py:259 `if body.count("\n") + 1 > LENGTH_CEILING_LINES and tag != "[ARTIFACT]":`.
  The `tag != "[ARTIFACT]"` clause skips length routing for artifact-tagged posts entirely.
- Concrete breaking input (verified): a 501-line post `"[ARTIFACT] firmware dump\n" + 500 raw hex
  lines` returns `ok=True, route_as_attachment=False` - it posts INLINE in full. An identical-length
  `[HYPOTHESIS]` returns `ok=False, route_as_attachment=True` (correctly attached). So an agent
  bypasses the length ceiling on ANY volume of content just by choosing the `[ARTIFACT]` tag.
- Why the prior "faithful" ruling is wrong: a COMPLIANT `[ARTIFACT]` post is a metadata block (sec 1
  requires only "what it is, provenance, image identity + version/build + hash, what a reader should
  do") - inherently short; the actual dump/firmware is the ATTACHED file, never pasted. So a
  >30-line `[ARTIFACT]` post is precisely the raw-dump-inline case sec 7.7 forbids. The exemption
  therefore never permits a compliant post - it only ever permits a violation.
- Severity: LOW-MEDIUM. This is an anti-drift / channel-spam control, not a trust or actuation
  bypass; the air-gap is untouched. But it is a named sec 10 mechanical duty that is silently unmet
  for one tag, and the docstring claims the ceiling is enforced.
- Fix: drop the `and tag != "[ARTIFACT]"` clause so over-length `[ARTIFACT]` also routes to
  attachment. The bridge attachment path already handles this tag (bridge.py:216 uses
  `res.tag or '[ARTIFACT]'`), so removal Just Works and matches sec 7.7 exactly. Add a test:
  over-length `[ARTIFACT]` -> route_as_attachment True.

### N2. LOW hardening: SAMPLE_COUNT accepts underscores / unicode digits / leading `+` / negatives via bare `int()`
- Rule: sec 1 "SAMPLE_COUNT is an integer"; sec 2/3 "PROVEN needs SAMPLE_COUNT > 1".
- Code: enforce.py:242-245 `int((_field_value(body,"SAMPLE_COUNT") or "").strip())`.
- Verified parses: `"2_000"->2000`, `"1_1"->11`, `"+2"->2`, `"-5"->-5`, `"٢"(Arabic 2)->2`.
  Rejected correctly: `"2.0"`, `"2 0"` (ValueError -> "must be an integer").
- Impact assessment: NOT exploitable to fake a single-sample PROVEN - the PROVEN gate is
  `sample <= 1`, and every accepted form still yields the true numeric value (`-5<=1` rejects,
  `1_1`==11>1 is a genuine 11). The only residue is record hygiene: `SAMPLE_COUNT: 1_1` reads to a
  human as ambiguous while the bridge silently treats it as 11. Semantic (the human reads the body),
  so this is a hardening note, not a rule violation.
- Severity: LOW. Fix (optional): reject a SAMPLE_COUNT that is not `^-?\d+$` before `int()`, and
  reject negatives, so the stored/displayed value is canonical.

### N3. LOW: rate token can be charged without a post landing on a `channel.send` exception
- Rule: sec 10 rate limits; M3's intent was "charge only on a post that reaches Discord".
- Code: bridge.py:204 charges, then bridge.py:218 / bridge.py:224 `await channel.send(...)`. If
  `send` raises (Discord 5xx/network), the token is consumed but nothing is posted and the buffer/
  observe steps never run; the handler surfaces a 500.
- Impact: the fixed direction (M3 - malformed floods do NOT charge) is fully correct and pinned;
  this is only the narrow inverse on a transport exception, i.e. an availability nick, not a
  trust/schema hole. Arguably acceptable (the send was genuinely attempted).
- Severity: LOW. Fix (optional): wrap `channel.send` and refund the token on failure, or charge
  after a successful send.

### N4. LOW (durability): the transport tests that PIN the pass-2 fixes are uncollectable without dev deps, and no CI config guarantees they run
- Code/repo: test_bridge.py imports `discord` + `aiohttp` (via bridge.py); these live only in
  requirements-dev.txt, and the repo carries NO CI yaml. In this environment `pytest tests/` errors
  at collection ("No module named 'discord'") and ONLY test_enforce runs.
- Why it matters: H1, M3, announce-once, and self-fanout are transport-layer fixes whose ONLY guard
  is test_bridge.py. A check that never runs is not a check ([[feedback_a_check_that_cannot_fail_is_not_a_check]]).
  If CI omits the dev deps, a future edit can silently reintroduce H1/M3 with a green board.
- Severity: LOW. Fix: add a CI job that installs requirements-dev.txt and runs the full suite, and
  fail collection loudly (or gate) if the transport tests are skipped.

---

## Pass-2 fixes re-examined - CONFIRMED SOUND (attempted breaks that failed)

### H1 (schema validated BEFORE length ceiling) - SOUND
- Order in check_egress: empty -> first_tag -> (tag None => control-line/reject) -> FINDING/EXPERIMENT
  field-gating + STATUS + SAMPLE + CLAIM_KIND + direct-VOID (enforce.py:215-255) -> THEN length
  ceiling (enforce.py:259). No path reaches the ceiling before validation for a field-gated type.
- Verified: over-length VALID finding -> route_as_attachment (attaches). Over-length finding missing
  STATUS -> 422, not attached. Over-length direct-claim with missing artifact -> void, not attached.
  Tag-only types (HYPOTHESIS/CORRECTION) over length -> attachment; `[ARTIFACT]` -> exempt (that
  exemption is N1, a separate pre-existing issue, not an H1 regression). No bypass found.

### M1 (STATUS first-token parse) - SOUND
- `raw.upper().replace(",", " ").replace("/", " ").split()`, `label = parts[0] if parts else ""`.
- Verified every adversarial input: `"proven"`->accepted (upper); `"PROVEN\tREVIEW_PENDING"` and
  `"PROVEN  REVIEW_PENDING"`->accepted (tabs/multi-space); `"PROVEN REVIEW_PENDING REVIEW_CLEARED"`
  ->accepted (both markers allowed - nonsensical but not a ladder violation, deferred); `"PROVEN:
  whatever"`->rejected (first token "PROVEN:" not in ladder); `"PROVEN/MEASURED"`->rejected
  (unexpected trailing "MEASURED"); `","` and empty-after-replace->rejected ("got empty", no
  IndexError); `"STATUS_JUNK PROVEN"`->rejected (junk first token). Empty/whitespace STATUS is caught
  upstream as a missing field (`_field_value` requires `\S`).
- PROVEN+SAMPLE rule fires on the FIRST token in every case: sample=1 with `"PROVEN"`, `"proven"`,
  `"PROVEN REVIEW_PENDING"`, `"PROVEN\tREVIEW_CLEARED"` ALL rejected with the sample-count reason.
  There is no reordering that hides PROVEN from the gate: a marker cannot precede the ladder token
  (it would fail the ladder check as parts[0]).

### M2 (`os.path.isfile` + jail `candidate.startswith(root + os.sep)`) - SOUND
- Verified against a real tmp archive: direct file under root (`x.log`) RESOLVES (dropping the old
  `candidate == root` acceptance did NOT break the normal case - a file under root always has the
  `root + os.sep` prefix); nested file (`run/y.log`) resolves; empty file -> VOID; directory `"."`
  -> VOID (jailed: candidate==root fails `startswith(root+sep)`); `"../etc/passwd"`,
  `"run/../../../etc/passwd"`, and a real symlink to `/etc/passwd` -> all VOID (realpath canonicalizes
  before the jail check, so `..` and symlink escapes are all rejected). Empty/`#`-only artifact
  values resolve to root and are rejected. No escape found.

### M3 (rate limit after validation, before send) - SOUND
- handle_egress order: parse -> halted/closed 409 -> check_egress (422 on reject, route_as_attachment
  falls through) -> channel resolve (503) -> `rate.allow` (429) -> send (attachment or normal). A
  rejected post never reaches `rate.allow`; both send paths are downstream of the single charge.
  No send-without-charge; no charge-without-send except the N3 send-exception edge.

---

## Test soundness - do the transport tests PIN or pass vacuously?

Checked each against a mental revert of its target fix (would it still be green with the bug back?):

- `test_overlength_malformed_finding_still_rejected` (H1): PINS. Under revert (length before
  field-gate) the STATUS-less over-length finding returns route_as_attachment -> handle_egress
  uploads -> 200 with a file; the test asserts 422 and empty `chan.sent`. FAILS under revert. Good.
  (Its companion `test_overlength_valid_finding_uploads_attachment` does NOT distinguish H1 - a valid
  finding attaches under both orderings - but it legitimately pins the attachment mechanism, and the
  malformed test carries the H1 direction.)
- `test_malformed_flood_does_not_starve_valid_poster` (M3): PINS. Under revert (charge before
  validate) the 13th malformed post returns 429, not 422, tripping the loop's `assert s == 422`; and
  the final valid post would 429 instead of 200. FAILS under revert (two ways). Good.
- `test_announce_closed_fires_once_via_on_message`: PINS. Requires exactly one THREAD CLOSED notice
  after 10 no-yield messages and still one after an 11th; a per-message re-announce or a missing
  early-return would break the count.
- `test_valid_finding_posts_and_fans_out` (pass-1 finding 5): PINS. `fanned` must be length 1 with
  the UNTRUSTED banner; a reverted self-fanout yields 0. Good.
- `test_closed_thread_gates_egress` / `test_halted_thread_gates_egress` / `test_halt_token_egress_
  halts_thread` / `test_bot_own_messages_are_not_ingested` / `test_rate_limiter_evicts_after_window`:
  all PIN their respective behaviors (each would flip 409<->200 or change buffer/flag state on
  revert).
- In enforce suite: `test_overlength_finding_missing_field_is_rejected_not_attached` and
  `test_overlength_direct_void_not_bypassed_by_padding` both PIN H1 (assert `not route_as_attachment`
  under padding). `test_artifact_directory_is_void` PINS M2 (a dir would resolve under the old
  exists+getsize). `test_status_review_pending_marker_allowed` /
  `test_review_marker_does_not_lift_proven_sample_rule` PIN M1 both directions.

No vacuous transport test found. Gap: none of the shipped tests pin N1 (the `[ARTIFACT]` length
exemption) - that behavior is currently "tested" only implicitly and in the wrong direction.

---

## Fresh break attempts that did NOT find a violation (air-gap and schema held)

- Label smuggling (leading whitespace, CRLF, label inside a fenced block, label inside the ARTIFACT
  value): `_field_value` matches `(?im)^\s*LABEL[:=]` and `_artifact_resolves` takes only
  `path.split()[0]`. An agent CAN satisfy its own labels from a code fence, but that is the agent
  forging its own schema-compliance, which sec 10 explicitly defers to adversarial review
  (well-formed != correct). Not a trust bypass. Trailing junk after the artifact path is ignored.
- CRLF / tabs: `.` matches `\r`, `_field_value` strips it, `str.split()` splits on tabs; line count
  uses `\n`. All tolerated, no parse corruption.
- Second `STATUS:` line: `re.search` keys on the FIRST match, so the labeled STATUS governs; a
  contradicting VERDICT prose is semantic and deferred. Not a mechanical bypass.
- first_tag vs leading whitespace/newlines: `body.lstrip()` before `startswith`; whitespace-only
  bodies rejected upstream. A halt token with any tail is rejected (must be exact `==`); a THREAD
  CLOSED control line matches by exact prefix (intended, to allow the "+ one line learned").
- THREAD CLOSED egress partial-prefix -> sets `st.closed` (bridge.py:230): intended L6 behavior
  (agent-declared close). Note the asymmetry - an INGRESS THREAD CLOSED from another fleet does NOT
  set `st.closed` (on_message has no such branch); only the auto no-yield counter does. This is
  arguably CORRECT under the Trust model (sec 3.14: a peer's closure is a claim to verify, never
  inherited), so I do not flag it. (By contrast an ingress exact HALT token DOES halt the local
  thread - that matches sec 6's "any agent enforces halt immediately"; a hostile fleet can grief a
  productive thread with a halt token, but that griefing vector is inherent to sec 6's unilateral
  halt design and the remedy - a fresh tagged post - is spec'd. Not a bridge bug.)
- Async races on `_seq` / `_ingress` / `threads`: single event loop; every mutation of `_seq`
  (`+= 1` in `_buffer_ingress`) and `st.observe` is synchronous with no interleaved `await`, so no
  torn counter and no double-announce (observe early-returns once `closed`, and the crossing message
  sets `closed` before the `await _announce_closed`). The long-poll lost-wakeup (pass-2 L4) is closed
  by clear-before-read. Multi-consumer `_new` wakes all waiters; a clear by one does not drop the
  buffered message for the other.
- Reaching an execution surface via transport: the process holds one credential (Discord token from
  a file), the agent API is loopback-and-asserted, and there is no subprocess/eval/file-write/bench/
  cron/webhook path. `_artifact_resolves` does read-only `realpath/isfile/getsize` jailed under
  archive_root and returns only a boolean (a weak existence oracle confined to the agent's own
  archive subtree - negligible). Attachment upload is in-memory `io.BytesIO`, no disk write.
  assert_airgap inspects env KEYS (a credential smuggled in an env VALUE is inert - no code consumes
  it). Air-gap holds.
- Attachment path ignores `st.observe`'s return (bridge.py:220 vs the normal path's
  `if st.observe(...): announce`): harmless, because every tag that can route to attachment
  (FINDING/EXPERIMENT/HYPOTHESIS/CORRECTION) is a YIELD_TAG, so observe always RESETS the counter and
  returns False - it can never be the crossing message. Fragile if YIELD_TAGS ever changes; worth a
  one-line comment, not a finding.

---

## Confirmed sound (net)

The pass-2 fixes H1/M1/M2/M3 are correct and regression-free. The load-bearing air-gap, provenance
stamping, ingress untrusted-wrapping, per-thread lifetime + halt/closed gating, and the correct
deferral of the sec 9 / 3.11 / 3.14-15 / control-validity agent-judgment layer all hold. The single
substantive item is N1 (the `[ARTIFACT]` length exemption) - a pre-existing, low-medium mechanical
gap both prior passes accepted; fixing it (delete one clause) makes the answer YES. N2/N3/N4 are LOW
hardening notes with no rule violation.
