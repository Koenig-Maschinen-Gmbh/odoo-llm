# ADR — Live Thread Tool Resolution (and the path to prompt/capability resolution)

- **Status:** Accepted — Phase 1 implemented (CAP-01); Phase 2 (CAP-02) contract
  pinned below + in progress. Phases 3–4 planned.
- **Date:** 2026-07-23
- **Owner:** König AI master thread (sole owner of `addons_apexive/odoo-llm` fork
  + the koenig AI stack).
- **Scope of this ADR:** how an `llm.thread` resolves the **tool set** it offers
  the model, with a forward plan for capability gating (Phase 2) and prompt
  assembly (Phase 3). It supersedes the implicit "snapshot on assistant-select"
  behaviour.
- **Reference (do not re-copy):** the decision record + evidence live in
  `addons_koenig/koenig_ai/docs_dev/workbench/tool-architecture/RESEARCH_2026-07-23_TOOL_CAPABILITY_PROMPT_ARCHITECTURE.md`
  (§11 = findings + finalized architecture; §11.5 = hard constraints). This ADR
  pins the fork-side contract only.

---

## Context

`llm.thread.tool_ids` is a real M2M **snapshot** copied from `assistant.tool_ids`
at assistant-selection time (`set_assistant()` `models/llm_thread.py` →
`[(6, 0, assistant.tool_ids.ids)]`; the form `_onchange_assistant_id` does the
same). Execution then reads that per-thread snapshot in three places:

1. `_prepare_chat_kwargs()` → `"tools": self.tool_ids` (what the model is offered).
2. `llm_tool/models/mail_message.py` `create_tool_message()` — validates the
   requested tool is "in the thread".
3. `llm_tool/models/mail_message.py` `execute_tool_call()` — finds+executes the
   tool from the thread's set.

Duplicated state that must be kept in sync rots. Live evidence (intranettest,
2026-07-23): **325/334 threads (97%) carry a stale `tool_ids`** vs their
assistant's current set. A tool added to an assistant after a thread was created
never reaches that thread — the model literally answers "I don't have that tool."
This is the "web search doesn't reach the model" symptom, quantified.

The industry standard is the opposite of a snapshot: MCP resolves tools **live**
(`tools/list` + `notifications/tools/list_changed`), and every major tool-use API
passes `tools` **per request**. It is also the OCB idiom: capability
(`ir.model.access`, `ir.rule`, group membership) is resolved live at the point of
use, never copied per record. The snapshot is the un-Odoo-like part.

---

## Decision

Introduce a single resolution seam, **`llm.thread._effective_tools()`**, that
execution reads instead of the raw `tool_ids` column. Resolve the set **live**
from the bound assistant, with a tiny explicit per-thread deviation.

### Target model (Phase 1 — shipped)

- **`llm_thread` (base):** `_effective_tools(self) -> llm.tool` returns
  `self.tool_ids` unchanged (byte-for-byte old behaviour when `llm_assistant` is
  absent). This is the seam every execution reader calls, so `llm_tool` (which
  does not depend on `llm_assistant`) can call it safely.
- **`llm_assistant` (override):**
  `effective = (assistant.tool_ids | tool_ids_extra) - tool_ids_disabled`, where
  `base = assistant.tool_ids` when an assistant is bound, else `super()`
  (the raw column). Two new M2M override fields on `llm.thread`:
  - `tool_ids_extra` — tools added on top of the assistant's set;
  - `tool_ids_disabled` — tools removed from the assistant's set.
  Both are normally **empty** — a plain user chat carries neither and therefore
  always sees its assistant's **current** tools. This kills the 97% staleness
  permanently, with **no migration**.
- **Execution readers moved to the seam:** `_prepare_chat_kwargs` and both
  `mail_message` validation/execution sites now read `_effective_tools()`.
- **Expert dispatch consumes the same seam** (koenig orchestrator): a sub-thread
  runs a **curated subset** `tools` (its own `tool_ids`, or the master set minus
  `koenig_dispatch_expert` for capability-bridge experts). It expresses that
  curation as a deviation so `_effective_tools() == tools` exactly:
  `tool_ids_disabled = assistant.tool_ids - tools`,
  `tool_ids_extra = tools - assistant.tool_ids`. This preserves the ISSUE-2b
  recursion fix (dispatch tool stays out of the sub-thread) through the new path.

### Resolver contract (the seam)

`_effective_tools()` is the **single gating point** for tool availability. It:

- returns an `llm.tool` recordset (never `None`);
- runs on the **hot path** (every turn) → must stay cheap: in-memory recordset
  set operations over already-prefetched M2M relations. No `search` /
  `read_group` / SQL. (Verified: Phase 1 does 0 extra queries beyond the M2M
  prefetch.)
- is **as-user** — it must never be `sudo()`'d; capability is resolved with the
  caller's rights (only the provider-key read is sudo'd, at its call site).

Phase 2 widens the same method into the full resolver
(`registry ∩ assistant ∩ consent ∩ model capability ∩ budget ∩ context`) — see
"Consequences → Forward plan". The **execute-time hard-check** in the tool's own
`execute()` remains as defense-in-depth (TOOL-02/03 ACL degradation lands there),
so the resolver filtering and the tool self-check are two independent layers.

### Prompt-fragment interface (Phase 3 — forward reference only)

Not implemented here. Phase 3 replaces the two uncoordinated prompt hooks
(`llm.provider._prepare_prepend_messages` + `llm.thread.get_prepend_messages`,
which koenig patches in four places) with one deterministic assembler where each
tool/knowledge source contributes an ordered `_system_prompt_fragment()`. The
KNOW-04 domain block shipped 2026-07-23 folds into that assembler. Recorded here
so the Phase 1 field/method names are chosen to not block it.

### Back-compat / migration

- **No `tool_ids` backfill migration.** It would be thrown away the moment live
  resolution lands and would clobber deliberate per-thread deviations.
- `tool_ids` is **retained** as a legacy column: it is the fallback base for
  no-assistant threads and survives for display/telemetry. When an assistant is
  bound it is **ignored by execution** (vestigial). It may be dropped in a later
  phase once display/telemetry read `_effective_tools()`; not dropped now to
  avoid a display regression.
- The koenig orchestrator `create()` already defaults new threads to the master
  assistant (this is why 0 persisted threads have a NULL assistant). It still
  writes a `tool_ids` snapshot there; that snapshot is now vestigial (execution
  derives live from `assistant_id`) and is left in place for display continuity.

### Audit / reproducibility

The snapshot was **not** an intentional audit mechanism (no prior ADR argues
snapshot-vs-live; it is an inherited apexive default). The audit trail — "what
tools did this turn actually use" — already exists independently in the
orchestrator's `tool_call` telemetry (per-task). Live resolution for execution +
per-turn telemetry for audit is the correct split; no reader depends on the
snapshot column semantically.

---

## The M2M relation-table constraint (VERIFIED — corrects a prior note)

A continuation note claimed the new override fields "MUST use auto relations
(no explicit `relation=`)". **That is inverted for this case.** Grounded in OCB
`odoo/fields.py` (`Many2many.setup_nonrelated`, ~4966–5005) and verified by a
clean `-i llm_assistant` on a disposable DB:

- The **auto** relation name is `"{sorted(model._table, comodel._table)}_rel"` —
  it ignores the field name. `llm.thread` already has `tool_ids`
  (`llm.thread`↔`llm.tool` → `llm_thread_llm_tool_rel`). A **second/third** auto
  M2M to `llm.tool` on the same model would claim the **same** name →
  `TypeError: Many2many fields ... use the same table and columns`. So auto is
  impossible for the override fields.
- The override fields therefore use **explicit, distinct `relation=`** names
  (`llm_thread_tool_disabled_rel`, `llm_thread_tool_extra_rel`) with **auto
  columns** (no explicit `column1`/`column2`).
- **Mock interaction:** `llm.thread.mock` (`_name` + `_inherit="llm.thread"`,
  `wizards/llm_prompt_test.py`) prototype-copies every M2M. With an explicit
  relation the copy would reuse the parent's physical table but resolve
  `column1` from its own table (`llm_thread_mock_id`), leaving a broken FK/read.
  The mock therefore **redefines both fields as non-stored** (`store=False`,
  trivial compute → empty) so it never creates/reads those tables. The mock
  never runs tool resolution, so this is invisible.
- **Verification:** `-i llm_assistant` on `test_cap01` → registry loaded, exit 0,
  three distinct relation tables created with correct `(llm_thread_id,
  llm_tool_id)` columns; a shell probe confirmed live derivation, disabled,
  extra, and no-assistant fallback all behave.

---

## Consequences

### Positive

- The 97% snapshot staleness is eliminated permanently, with no migration and no
  "re-select your assistant" nudges. Every future tool rollout is instant for all
  existing + new threads.
- One seam (`_effective_tools()`) is the single source of truth for tool
  availability across the base loop, the `mail_message` validate/execute path,
  and orchestrator expert dispatch — ready to grow into the full resolver.
- Odoo-aligned (live capability resolution) and MCP-aligned (live tool set).
- Backward-compatible: no-assistant threads and non-`llm_assistant` installs are
  unchanged; the `tool_ids` column still works as the no-assistant base.

### Negative / trade-offs

- `tool_ids` becomes vestigial when an assistant is bound (two meanings: base for
  no-assistant threads; ignored otherwise). Documented; scheduled for removal
  once display/telemetry move to the seam.
- Expert dispatch now writes two deviation sets per sub-thread instead of one
  snapshot. Cheap (transient sub-thread) and keeps a single resolution path.
- `llm_letta`'s `letta_sync_agent_tools(..., thread.tool_ids)` still reads the
  raw column (external-agent sync, not used at König, untested here). Flagged as a
  follow-up; left untouched to avoid changing an unexercised provider path.

### Forward plan (sequencing + test plan)

- **Phase 1 (this ADR — done):** `_effective_tools()` + override fields + all
  execution readers + expert dispatch consumption + consent-banner Hoot smoke
  test. Tests: fork `TransactionCase` for live derivation / disabled / extra /
  no-assistant fallback / `_prepare_chat_kwargs` wiring / `mail_message`
  validation; orchestrator test that expert dispatch yields `_effective_tools()
  == curated tools` and excludes the dispatch tool for inheriting experts.
- **Phase 2:** tool registry + bundles/tags on the existing
  `_get_available_implementations` seam; widen `_effective_tools()` into the full
  resolver (consent ∩ capability ∩ budget ∩ context); retire `assistant_attach`
  seeds + self-heal hooks; fold TOOL-02/03 ACL degradation into the execute-time
  hard-check.
- **Phase 3:** deterministic prompt-fragment assembler; fold in KNOW-04 v2 +
  memory + policy/brief + per-tool guidance; consolidate the 3-way consent gate.
- **Phase 4:** clean re-hardening (PROV telemetry first; the eval harness must
  exercise the real new-conversation + pre-existing-thread paths — the prior G1
  harness called `set_assistant()` and so always had fresh tools, masking issue
  #4).

---

## Phase 2 (CAP-02) — tool registry (bundles) + one resolver (fork contract)

Widens the single seam into the resolver. Koenig build plan + migration:
`addons_koenig/koenig_ai/docs_dev/workbench/tool-architecture/PLAN_CAP02_TOOL_REGISTRY_RESOLVER.md`.

### New fork model + fields

- **`llm.tool.bundle`** (`llm_tool`): `name`, `code` (UNIQUE), `active`,
  `sequence`, `description`, `tool_ids` (M2M → `llm.tool`,
  relation `llm_tool_bundle_tool_rel`). A named capability pack.
- **`llm.tool`**: inverse `bundle_ids` (same relation, display) +
  `requires_capability` (Char, e.g. `multimodal`) — declarative capability gate.
- **`llm.assistant`**: `bundle_ids` (M2M → `llm.tool.bundle`,
  relation `llm_assistant_tool_bundle_rel`) — the assistant's bundle
  subscriptions. New method `_resolved_tools()` =
  `(tool_ids | bundle_ids.filtered(active).tool_ids).filtered(active)` (live,
  cheap, prefetched-M2M unions — no `search`).

### Resolver split (the seam is unchanged for readers)

`_effective_tools()` stays the single execution seam but is now
`candidate ∩ per-tool availability gates`:

- Base `llm.thread._candidate_tools()` → `self.tool_ids` (raw column).
- Base `llm.thread._effective_tools()` →
  `self._candidate_tools().filtered(lambda t: t._ai_is_available_for(self))`.
- `llm_assistant.llm.thread._candidate_tools()` (REPLACES the CAP-01
  `_effective_tools` override) →
  `(assistant._resolved_tools() if assistant else super()) | tool_ids_extra) - tool_ids_disabled`.
- `llm.tool._ai_is_available_for(thread)` — as-user gate, base = `active`
  (registry) + `requires_capability` vs `thread.model_id.model_use`; tool
  implementations override (super() first) for consent/context. Cheap: no
  query beyond prefetched fields.

The **execute-time hard-check** in the tool's own `execute()` remains as
defense-in-depth (TOOL-02/03 ACL degradation lands there / in the shared RAG
`source_mixin`), so resolver-filter and tool-self-check are two independent
layers.

### Consent, budget, context (decisions)

- **Consent → resolver hide-gate** for tools that declare a persistent per-user
  consent (koenig web tools check `res.users.x_ai_web_search_consent` via the
  `_ai_is_available_for` override). Deliberate behavior change: consent-missing
  tools are **hidden** (no wasted round-trip) instead of offered + refused
  in-band; the execute-time hard-check stays. Full 3-way consolidation is CAP-03.
- **Budget / rate-limit → execute-time** (already in-band). A per-turn budget
  query violates the hot-path cheapness constraint (§11.5); the resolver treats
  budget as the execute-time defense layer, not an offer-set filter.
- **Context/anchor gate** — extension point only (`_ai_is_available_for`); not
  implemented (no current consumer).

### Migration / back-compat

- Fully backward compatible: with no bundles + no `requires_capability`, the
  resolver is byte-for-byte CAP-01.
- Retires `assistant_attach.xml` + self-heal `hooks.py` (koenig). A koenig
  migration moves web tools from `assistant.tool_ids` to a `web_research`
  bundle subscription (one transaction, live-state-preserving, no tool loss).

### M2M relation-table discipline (carried from CAP-01)

`llm.thread.mock` (prototype child) copies every M2M — but the new M2M fields
live on `llm.tool.bundle` / `llm.assistant`, **not** on `llm.thread`, so the
mock is unaffected. The new relations use explicit distinct names
(`llm_tool_bundle_tool_rel`, `llm_assistant_tool_bundle_rel`); none collides
with an existing same-model auto M2M.

## Alternatives rejected

1. **Backfill `tool_ids` on all threads (additive).** Throwaway once live
   resolution lands; clobbers deliberate per-thread deviation. (Also the
   web-search thread's open question (a) — answered NO.)
2. **`tool_ids`-as-override-when-non-empty** (prefer the column when set, else
   assistant). Re-introduces the bug: 97% of threads have a non-empty **stale**
   column, so execution would keep reading stale tools. The column must be
   ignored for execution when an assistant is bound.
3. **`is_expert_subthread` special-case branch** in `_effective_tools()`
   (return the raw column for expert sub-threads, assistant-derived otherwise).
   Simpler (no new fields, no mock work) and avoids the M2M constraint, but it is
   a flag-branch wart, gives the column two live meanings, and offers no general
   per-thread deviation for user threads. Rejected in favour of the explicit
   `extra`/`disabled` override model, which is general and matches the finalized
   architecture (§11.6).
4. **Auto M2M relations for the override fields** (per the continuation note).
   Impossible — same-model collision with the existing auto `tool_ids`
   (verified). Explicit distinct relations + mock non-stored override is correct.
