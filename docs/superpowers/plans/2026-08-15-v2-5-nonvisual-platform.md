# V2.5 非视觉平台实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development`
> task-by-task. The controller also uses `superpowers:using-git-worktrees`,
> `superpowers:test-driven-development`, `superpowers:systematic-debugging`,
> `superpowers:verification-before-completion`, and
> `superpowers:finishing-a-development-branch` at the gates defined below. Steps use checkbox
> (`- [ ]`) syntax for execution tracking; official status remains in `docs/Progress.md`.

**Goal:** Implement the complete V2.5 non-visual contract, backend, database, AI assets, Android data
layer, and Release build pipeline without editing visual UI-owned files.

**Architecture:** The outer FastAPI repository owns V2.5 resource semantics and server state. The
nested Android repository consumes one typed `domain/v25` boundary and contains no duplicated
business rules. Each implementation task is assigned to one fresh subagent in exactly one Git
repository; review depth is selected by risk instead of applying the full review loop mechanically.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, Alembic, Pydantic v2, pytest, ruff, mypy, Kotlin,
Android/Gradle, coroutines, `kotlinx.serialization` or the networking serialization already selected
by the nested project.

## 1. Authority and global constraints

Read these once before dispatching Task 1 and record their commit/hash in both ledgers:

1. `docs/PRD/V2.5/prd_v2_5.md` and its seven module PRDs are the product authority.
2. `docs/Architecture/v2.5-target-architecture.md` is the target technical contract.
3. `docs/Progress.md` is the only official status and DONE evidence map.
4. Until Task 2 completes atomically, current `structure-contract.md`, `openapi.yaml`, and
   `database-design.md` remain V2.4 implementation facts; no subagent may describe target fields as
   deployed early.
5. Schema ↔ OpenAPI ↔ ORM ↔ migration must stay synchronized. HTTP handlers never expose ORM
   objects directly.
6. API keys are accepted only through the existing masked API-key flow and used only in
   `main/infra/llm/**`; plaintext must never enter logs, responses, task details, fixtures, reports,
   command arguments, or Git.
7. Code owns identifiers, quantities, ratios, state transitions, transactions, visibility, retry,
   and scheduling. LLM assets own source-grounded semantic planning and card wording only.
8. All behavior changes use RED → GREEN → REFACTOR. A test must name the production break it
   catches and exercise real behavior; mock only the slow/external boundary.
9. An unexpected test/build/runtime failure triggers `superpowers:systematic-debugging` before any
   fix. After three failed hypotheses, stop and escalate the architectural issue instead of stacking
   a fourth speculative fix.
10. Implementer subagents do not modify PRD, Architecture, Progress, visual UI, screenshots, themes,
    drawables, or `ui/AppViewModel.kt`. During Task 2, the primary non-visual controller—not an
    implementer subagent—owns the three machine-contract document edits.
11. Preserve all pre-existing dirty changes. In particular, nested `.gitignore` and
    `Front/app/build.gradle.kts` are user-owned until Task 13 performs an explicit three-way review.
12. Never run backend tests outside Conda environment `shanka-backend`; never install dependencies
    into base/system Python.
13. Release has no runtime Mock/demo data, test mode, editable server address, placeholder success,
    or Debug entry. Debug may retain isolated fixtures that Release cannot reach.
14. Do not commit `.env`, database files, logs, APK signing material, SDD workspaces, generated test
    artifacts, or the sample PDFs under `res/`.

## 2. Superpowers execution protocol

### 2.1 Preflight and two worktrees

The controller must use `superpowers:using-git-worktrees` before implementation. The confirmed PRD,
Architecture, Progress, and this plan must first exist on a stable contract-baseline commit; otherwise
a new worktree would silently start without the authority being implemented.

Create or verify two isolated worktrees, never one cross-repository worktree:

| Context | Git root | Suggested branch | Manual fallback path | Owns tasks |
| --- | --- | --- | --- | --- |
| Backend | `/home/kbzz1/shanka_backend` | `codex/v25-nonvisual-backend` | `/home/kbzz1/shanka_backend/.claude/worktrees/v25-nonvisual-backend` | 2–11, 14, and 15 backend checks |
| Android data | `/home/kbzz1/shanka_backend/frontend-app` | `codex/v25-nonvisual-data` | `/home/kbzz1/shanka_backend/.claude/worktrees/v25-nonvisual-data` | 1, 12–13 and 15 Android checks |

- [ ] Detect whether the controller is already in a linked worktree; do not create a worktree inside
      another merely from its path name.
- [ ] Prefer the host's native worktree mechanism. If unavailable, use the table's explicit fallback
      paths only after `git check-ignore` proves outer `/.claude/worktrees/` is ignored. Do not add
      nested `.worktrees/` or edit the nested repository's dirty `.gitignore` merely to host a worktree.
- [ ] Record base branch, base commit, worktree path, plan absolute path, and plan SHA-256 in each
      repository's ledger.
- [ ] Run fresh clean-baseline tests: backend contract suite in the outer worktree and Android unit
      tests in the nested worktree. If the baseline is red, report evidence and ask whether to diagnose
      or proceed; do not relabel a pre-existing failure as a task regression.
- [ ] Create the plan-scoped SDD workspace separately in each Git root with `sdd-workspace`. Never
      share task briefs or review packages across the two repositories.

Each ledger starts with exactly:

```text
# SDD ledger — plan: /absolute/path/to/docs/superpowers/plans/2026-08-15-v2-5-nonvisual-platform.md
```

Then record per task: `BASE`, implementer model, review tier, report path, commits, verification
commands/results, findings, repair rounds, and terminal status.

### 2.2 Review depth is risk-based

The controller assigns a concrete available model name at dispatch time; leaving model selection
implicit is forbidden. Use the strongest available model for H tasks and final review, and a capable
implementation model for L/M tasks.

| Tier | When | Execution path |
| --- | --- | --- |
| L — light | Typed bridge or versioned asset change whose behavior is fully guarded | Fresh implementer → self-review/report → controller inspects diff and reruns named verification. No separate reviewer by default. |
| M — normal | One bounded API/service/data feature with ordinary persistence or integration risk | Fresh implementer → one reviewer performs combined spec + code-quality review → original implementer fixes only Critical/Important findings → scoped re-review only when such findings existed. |
| H — load-bearing | Cross-layer atomic contract, migration, publication, or concurrency invariant | Fresh strongest implementer → one combined reviewer → original implementer repair/re-review loop. Rounds 1–3 resume it; rounds 4–5 use a fresh stronger implementer. After round 5, adjudicate residuals; a load-bearing gap is `BLOCKED`. |
| G — gate | Evidence collection with no planned product implementation | Verification subagent gathers evidence; controller independently reruns release-critical commands and inspects both diffs. Findings route to their owning task, not patched directly by the controller. |

This plan deliberately does **not** require a separate specification reviewer followed by a separate
code-quality reviewer for every task. One reviewer combines both lenses for M/H tasks. L tasks avoid
review dispatch unless the controller finds a contract ambiguity, security issue, or diff outside
scope; if found, promote the task to M and record why.

### 2.3 Per-task controller loop

For every numbered task:

1. Record the owning repository's current `BASE`; confirm the other repository is not touched.
2. Generate `task-N-brief.md` with the Superpowers `task-brief` script. Send the implementer only the
   brief, relevant authority paths, worktree path, concrete model, and report path—not the whole chat.
3. The implementer reads all applicable `AGENTS.md`, follows the task's RED/GREEN steps, commits only
   owned files, self-reviews the diff, and writes a report containing tests and remaining concerns.
4. Apply the task's L/M/H policy. The controller never silently fixes implementation code itself.
5. Before moving on, use `superpowers:verification-before-completion`: inspect Git diff/commits and
   rerun the task's named command fresh. An agent report alone is not evidence.
6. Mark the ledger `DONE`, `DONE_WITH_CONCERNS`, `NEEDS_CONTEXT`, or `BLOCKED`. Do not update
   `docs/Progress.md` yet.

Only one non-visual implementation subagent runs at a time. The separate visual lane may continue in
its own worktree. Tasks execute in numeric order unless a dependency explicitly says otherwise.

### 2.4 Final review and branch finish

After Task 15 verification:

- [ ] Generate one whole-branch review package for the backend worktree and another for the Android
      data worktree, each from its recorded baseline to current HEAD.
- [ ] Dispatch one strongest final reviewer per repository, sequentially. Review authority coverage,
      security, transactions, failure recovery, test credibility, file ownership, and unrelated diff.
- [ ] Allow at most one final fix wave per repository, implemented by a fresh subagent and followed by
      one scoped re-review. Adjudicate any residual finding explicitly.
- [ ] Delete only this plan's ignored SDD workspace after final review is clean and evidence has been
      copied into the controller handoff; do not delete tracked plan history.
- [ ] Use `superpowers:finishing-a-development-branch`: rerun full tests, confirm base branches, then
      offer exactly merge locally / push and create PR / keep branches as-is. Integration remains the
      user's decision.
- [ ] Only after actual integration and independent evidence review may the main integrator update
      `docs/Progress.md` package status.

## 3. Task-to-Progress mapping

| Progress package | Plan tasks |
| --- | --- |
| NV-00 Android `domain/v25` bridge | Task 1 |
| NV-01 contract promotion and migration | Task 2 |
| NV-02 profile, preferences, learning projects | Tasks 3–4 |
| NV-03 generation state and atomic publication | Tasks 5–6 |
| NV-04 AI assets and quality | Task 7 |
| NV-05 deletion undo and AI rewrite | Tasks 8–9 |
| NV-06 today plan, review, and stats | Tasks 10–11 |
| NV-07 Android data and Release configuration | Tasks 12–13 |
| NV-08 platform regression evidence | Tasks 14–15 + final review |

---

## Task 1: Create the Android V2.5 typed bridge

**Progress package:** NV-00
**Repository:** Android data
**Review tier:** L
**Dependencies:** Contract-baseline commit only

**Files:**

- Create: `Front/app/src/main/java/com/qiuzhao/flashcards/domain/v25/V25Models.kt`
- Create: `Front/app/src/main/java/com/qiuzhao/flashcards/domain/v25/V25Repository.kt`
- Create: `Front/app/src/test/java/com/qiuzhao/flashcards/domain/v25/V25ContractTest.kt`

**Interfaces produced:** `V25Repository` is the only boundary visual `AppViewModel` may consume. It
exposes typed auth profile/preferences, learning projects, generation tasks, decks/cards, today plan,
review rating, browse filters, deletion batches, rewrite previews, and stats states. It must represent
loading/empty/recoverable failure without Android UI types and must not contain HTTP/JSON code.

- [ ] Write compile-time/unit contract tests that instantiate the exact V2.5 enum values and all
      success/empty/failure result variants consumed by V-02 through V-06.
- [ ] Run the focused test and confirm RED because `domain.v25` does not exist.
- [ ] Implement immutable Kotlin models and suspend repository methods matching the target
      Architecture; represent timestamps as ISO-8601/`Instant` at the boundary and ratios as integers.
- [ ] From `Front/`, run
      `./gradlew testDebugUnitTest --tests '*V25ContractTest'`, then `./gradlew test`; both must exit 0.
- [ ] Commit only the three owned files and write the task report. The controller checks no UI,
      networking, Gradle, `.gitignore`, or existing dirty file entered the diff.

## Task 2: Promote the V2.5 machine contract and database atomically

**Progress package:** NV-01
**Repository:** Backend
**Review tier:** H
**Dependencies:** Task 1 type names are available for comparison

**Files:**

- Controller modify: `docs/Architecture/structure-contract.md`
- Controller modify: `docs/Architecture/openapi.yaml`
- Controller modify: `docs/Architecture/database-design.md`
- Controller modify: root `.gitignore` to ignore `/releases/`
- Implementer modify/create: V2.5 resources under `main/domain/` and `main/app/schemas/`
- Modify: `main/infra/db/models.py`
- Create: the Alembic-generated `main/migrations/versions/*_v2_5_contract.py`; generate its revision ID
  from the runtime head and record the exact filename in the task ledger
- Modify/Create: `main/tests/contract/test_*_schemas_guard.py`
- Modify: `main/tests/contract/test_orm_database_guard.py`
- Modify: `main/tests/integration/test_alembic_migration.py`

**Interfaces produced:** The V2.5 resource names, enum values, constraints, error payloads, and tables
defined in target Architecture sections 3–6 become the single machine contract. Later tasks may add
behavior but may not rename these fields unilaterally.

- [ ] Add failing contract tests for Schema ↔ OpenAPI parity, ORM ↔ database parity, allowed-zero
      10%-step ratios summing to 100, `APPLICATION → DEEP_QUESTION`, task-state migration,
      `STAGED/PUBLISHED`, foreign keys, indexes, and V2.5 error codes.
- [ ] Add failing migration tests for a fresh database and a copied V2.4 database containing legacy
      cards/tasks; verify preservation, mappings, defaults, and downgrade/forward behavior required by
      repository migration policy.
- [ ] Run the focused contract/migration tests and confirm the expected RED failures.
- [ ] The primary controller derives and edits the three machine-contract documents directly from the
      confirmed target Architecture. The implementer updates domain enums, Pydantic schemas, ORM, and
      one generated Alembic revision against that target. Do not expose routes whose service behavior
      does not yet exist.
- [ ] Treat the controller's contract-document diff and the implementer's code/migration diff as one
      non-separable promotion unit: review and verify their combined tree, and never integrate either
      side independently.
- [ ] Verify `git check-ignore releases/app-release.apk` succeeds after adding the root artifact rule;
      `res/` remains reserved for sample PDFs and is not an APK output location.
- [ ] Run contract and migration tests, then backend full `pytest`, `ruff format --check`, `ruff check`,
      and `mypy`; all commands use `conda run -n shanka-backend`.
- [ ] Commit the atomic promotion. Generate one combined review package; keep the task open until all
      load-bearing findings are resolved or explicitly `BLOCKED` under the H policy.

## Task 3: Implement profile and user preferences

**Progress package:** NV-02
**Repository:** Backend
**Review tier:** M
**Dependencies:** Task 2

**Files:**

- Modify: `main/app/api/auth.py`, `main/app/schemas/auth.py`
- Modify: `main/services/auth/service.py`
- Create: `main/app/api/preferences.py`, `main/app/schemas/preferences.py`
- Create: `main/services/preferences/service.py`
- Inspect and modify only if a failing V2.5 test requires it: `main/app/api/api_key.py`,
  `main/services/api_key/service.py`
- Modify: `main/app/main.py`
- Create: `main/tests/integration/test_profile_preferences_api.py`

**Interfaces produced:** `GET/PATCH /auth/me` and `GET/PATCH /preferences` from Architecture 4.1,
including nickname, preset avatar ID, read-only email, coverage mode, integer difficulty ratios, daily
goal, learning timezone, and current project reference.

- [ ] Write RED integration cases for defaults (`BALANCED`, 40/40/20, 50 cards/day), partial patch,
      nickname length/character/trim rules, full read-only email, default `mood_01`, exactly 12 allowed
      preset avatar IDs, ratio steps/sum/zero, daily goal 10–200 by 10, invalid IANA timezone,
      cross-user isolation, last-success-wins, and idempotent retry.
- [ ] Add or retain V2.5-facing API-key regressions for available, invalid, insufficient balance,
      temporarily unverifiable, and unset states; a failed new key must not replace the old valid key,
      and no response/log/report may contain plaintext.
- [ ] Implement schemas, service transaction, and thin handlers; email remains auth-owned and cannot be
      patched. API-key fields do not enter profile/preferences payloads.
- [ ] Run focused integration tests and relevant auth/API-key regressions.
- [ ] Commit, issue one combined review, fix Critical/Important findings, and rerun the focused suite
      before the controller records completion.

## Task 4: Implement learning projects, PDFs, chapters, and project settings

**Progress package:** NV-02
**Repository:** Backend
**Review tier:** M
**Dependencies:** Tasks 2–3

**Files:**

- Create: `main/app/api/projects.py`, `main/app/schemas/projects.py`
- Create: `main/services/projects/service.py`
- Modify: `main/services/pdf/service.py`, `main/services/pdf/parser.py`
- Modify: `main/infra/storage/local.py`, `main/app/main.py`
- Create: `main/tests/integration/test_projects_api.py`
- Create: `main/tests/integration/test_project_deletion.py`

**Interfaces produced:** Architecture 4.2 project CRUD, PDF upload continuity, chapter selection/deletion,
project study settings and current-project selection. Cards/decks are project children; a project may
contain multiple generation tasks.

- [ ] Write RED cases for upload→project creation, editable default name, cross-device retrieval,
      multiple tasks per project, chapter scope, active-task deletion conflict, and cross-user denial.
- [ ] Write RED deletion cases for both user decisions: keep already published decks/cards while
      deleting PDF/chapters/tasks, or delete the entire aggregate. Storage failure must roll back
      metadata or leave a retryable cleanup record—never claim success with a half-deleted project.
- [ ] Implement short database transactions and storage compensation. Reuse one PDF business model;
      compatibility `/pdfs` routes must delegate rather than create a second semantic path.
- [ ] Run focused project/PDF tests plus existing PDF acceptance tests; commit and apply M review.

## Task 5: Implement durable generation configuration and task lifecycle

**Progress package:** NV-03
**Repository:** Backend
**Review tier:** M
**Dependencies:** Tasks 2 and 4

**Files:**

- Modify: `main/app/api/tasks.py`, `main/app/api/samples.py`
- Modify: `main/app/schemas/tasks.py`, `main/app/schemas/samples.py`
- Modify: `main/services/tasks/service.py`, `main/services/tasks/executor.py`
- Modify: `main/services/generation/samples.py`
- Create: `main/tests/integration/test_v25_task_lifecycle.py`
- Create: `main/tests/integration/test_v25_sample_persistence.py`

**Interfaces produced:** Architecture 4.3 create/read/list/configure/sample/confirm/abandon/retry/delete
semantics and the seven user-visible task states. Task, source PDF, chapter scope, and generation config
survive page changes, app exit, and device changes.

- [ ] Write RED state-transition tables covering every legal/illegal transition, automatic save,
      resuming a draft/sample task, configuration change invalidating the prior sample, and abandon.
- [ ] Write RED cases proving disabled difficulty segments generate no sample and 1–3 samples correspond
      only to enabled segments. No card-count estimate appears in API configuration.
- [ ] Implement state validation centrally in the service. Remove user pause/resume/cancel endpoints;
      keep internal worker lease recovery distinct from product state.
- [ ] Run task/sample focused tests and background-continuity regressions; commit and apply M review.

## Task 6: Guarantee staged generation and atomic publication

**Progress package:** NV-03
**Repository:** Backend
**Review tier:** H
**Dependencies:** Tasks 4–5

**Files:**

- Modify: `main/services/tasks/executor.py`
- Modify: `main/services/generation/batches.py`, `main/services/generation/planning_executor.py`
- Modify: `main/services/cards/service.py`, `main/services/decks/service.py`
- Create: `main/tests/integration/test_generation_atomic_publish.py`
- Modify: `main/tests/integration/test_concurrency.py`

**Interfaces produced:** Formal generation writes cards as `STAGED`; success atomically publishes a
non-empty complete batch, while failure exposes none. Retry links to the failed task and publishes one
complete replacement result. Deleting a task deletes all cards generated by that task.

- [ ] Write RED tests proving ordinary card/deck/study/stats queries exclude `STAGED` and
      `delete_batch_id != NULL` cards through one shared visibility predicate.
- [ ] Write RED failure-injection cases at planning, generation, validation, persistence, and publish;
      every failure marks the whole task failed and exposes zero partial cards.
- [ ] Write RED concurrency cases for duplicate confirmation, worker retry, retry-after-failure,
      task deletion, and zero-valid-card output. LLM calls must not hold a SQLite write transaction.
- [ ] Implement staged writes, short atomic publish, idempotent task transitions, retry linkage, and
      cleanup. Success response is only `COMPLETED` with final generated count.
- [ ] Run focused failure/concurrency suites, all cards/decks/tasks tests, then full backend tests and
      static checks. Commit and complete H review; no unresolved publication/visibility finding may be
      downgraded to a concern.

## Task 7: Version the V2.5 Planner, Generator, and scoring assets

**Progress package:** NV-04
**Repository:** Backend
**Review tier:** L, promoted to M if manifest/schema behavior changes beyond Architecture
**Dependencies:** Task 6 contracts

**Files:**

- Create: `agent_evolution/prompts/v4/planner.md`
- Create: `agent_evolution/prompts/v4/generator.md`
- Create: `agent_evolution/prompts/v4/rewrite.md`
- Create: `agent_evolution/schemas/v3/planner-output.schema.json`
- Create: `agent_evolution/schemas/v3/generator-output.schema.json`
- Create: `agent_evolution/rubrics/v3/rubric.md`
- Create: `agent_evolution/rubrics/v3/scoring-prompt.md`
- Modify: `agent_evolution/manifest.json`, `agent_evolution/CHANGELOG.md`
- Create: `main/tests/contract/test_prompt_assets_v4.py`

**Interfaces produced:** Planner emits source-grounded semantic units labeled
`CORE/IMPORTANT/LOW_FREQUENCY`; coverage modes select semantic scope, not target quantity. Generator
maps `DEEP_QUESTION` only to open questions whose back is a reference approach.

- [ ] Write failing executable asset/manifest guards for exact version alignment, schema validity,
      allowed labels, source chunk IDs, and forbidden count/cost/pause semantics.
- [ ] Add new version directories; never mutate historical asset versions. Update manifest and
      CHANGELOG together.
- [ ] Evaluate compact fixed fixtures for sparse, normal, and dense chapters across 精简/均衡/充分覆盖;
      verify scope semantics, low-frequency inclusion, allowed limited repetition when knowledge is
      sparse, and source grounding. Record quality observations without claiming exact card counts.
- [ ] Run contract guards and generation validation/scoring tests. Commit; controller verifies diff,
      manifest resolution, and quality record. Promote to M if a code/schema compatibility question is
      discovered.

## Task 8: Implement durable 10-second deletion batches

**Progress package:** NV-05
**Repository:** Backend
**Review tier:** M
**Dependencies:** Tasks 2 and 6

**Files:**

- Modify: `main/app/api/cards.py`, `main/app/schemas/cards.py`
- Create: `main/services/cards/deletion.py`
- Create: `main/tests/integration/test_card_deletion_batches.py`

**Interfaces produced:** Delete creates/merges a server-authoritative pending batch with `undo_until`;
pending batches survive app restart, undo restores the full batch, and an idempotent finalizer makes
expired cards permanently invisible. V2.5 has no recycle bin.

- [ ] Write RED tests using an injected clock for single delete, consecutive merge, pending retrieval,
      9.999-second undo, expired undo, restart recovery, finalizer rerun, and two-device races.
- [ ] Implement deletion as visibility marking plus batch state, not client timer deletion. Reuse the
      shared card visibility predicate from Task 6.
- [ ] Run focused tests plus cards/study/stats visibility regressions. Commit and apply M review.

## Task 9: Implement two-stage AI card rewrite

**Progress package:** NV-05
**Repository:** Backend
**Review tier:** M
**Dependencies:** Tasks 6–7

**Files:**

- Modify: `main/app/api/cards.py`, `main/app/schemas/cards.py`
- Modify: `main/services/cards/rewrite.py`
- Create: `main/tests/integration/test_card_rewrite_preview_apply.py`

**Interfaces produced:** Create a persisted preview without changing the card; apply replaces front/back
only when `base_card_version` still matches; cancel is idempotent. Direct editing stays immediate and
does not add an extra reset-schedule warning.

- [ ] Write RED cases for preview success/failure, unavailable source, custom requirement, cancel,
      duplicate apply, concurrent direct edit, deleted card, cross-user access, and API-key-safe errors.
- [ ] Implement preview/apply/cancel with compare-and-swap version checking and a short apply
      transaction. Keep the original card unchanged on every generation or apply failure.
- [ ] Run focused rewrite/API-key/log-redaction tests. Commit and apply M review.

## Task 10: Implement server-authoritative today plan and review flows

**Progress package:** NV-06
**Repository:** Backend
**Review tier:** M, promoted to H if FSRS-6 scheduling semantics must change
**Dependencies:** Tasks 3–6 and 8

**Files:**

- Modify: `main/app/api/review.py`, `main/app/schemas/review.py`
- Modify: `main/services/review/service.py`, `main/services/scheduling/scheduler.py`
- Create: `main/services/study/service.py`, `main/app/api/study.py`, `main/app/schemas/study.py`
- Create: `main/tests/integration/test_today_study_plan.py`
- Create: `main/tests/integration/test_free_browse.py`

**Interfaces produced:** `GET /study/today` returns the current project's due-first queue and new cards
up to the daily goal. Both first learning and review use `AGAIN/HARD/GOOD/EASY`. Free browse supports
position/stable-random and five filters without writing ratings or schedule state. Independent decks
can start their own due review outside the current project.

- [ ] Write RED cases for due-first ordering, overdue backlog beyond target, new-card fill, current
      project chapter scope, daily reset by confirmed IANA timezone, duplicate rating idempotency, and
      deleted/staged exclusion.
- [ ] Write RED free-browse cases for position order, session-stable random, three content
      difficulties, unmastered/mastered, and proof that review events/schedule/today count remain
      unchanged.
- [ ] Keep FSRS-6 interval calculation in the existing scheduler and orchestration in services. If a
      desired behavior conflicts with FSRS invariants, promote to H and record the decision before code.
- [ ] Run focused study/review tests plus existing scheduler/review acceptance tests. Commit and apply
      the selected review tier.

## Task 11: Implement real, timezone-safe statistics

**Progress package:** NV-06
**Repository:** Backend
**Review tier:** M
**Dependencies:** Tasks 3, 6, 8, and 10

**Files:**

- Modify: `main/app/api/stats.py`, `main/app/schemas/stats.py`
- Modify: `main/services/stats/service.py`
- Create: `main/tests/integration/test_v25_stats_dashboard.py`
- Create: `main/tests/integration/test_v25_stats_performance.py`

**Interfaces produced:** `/stats/dashboard` derives timezone and weekly goal from server preferences and
returns rating count, unique completed cards, weekly goal completion, mastery, history, and honest empty
states without client-supplied timezone/goal parameters.

- [ ] Write RED cases for rating-count versus unique-card semantics, repeated ratings, midnight and DST
      boundaries, no prior-week denominator, current-project changes, independent-deck reviews,
      staged/deleted cards, and cross-user isolation.
- [ ] Implement database aggregation with explicit indices/query plans where needed; never replace
      missing data with fabricated 0% or fixed date arrays.
- [ ] Seed the Architecture baseline volume and measure the named dashboard query. Record dataset,
      hardware/environment, command, and elapsed/P95 evidence; do not make a performance claim from an
      empty database.
- [ ] Run focused stats/acceptance tests and performance evidence command. Commit and apply M review.

## Task 12: Implement the Android V2.5 remote data layer

**Progress package:** NV-07
**Repository:** Android data
**Review tier:** M
**Dependencies:** Tasks 1–11 API contract is stable

**Files:**

- Create: `Front/app/src/main/java/com/qiuzhao/flashcards/data/remote/v25/V25Dtos.kt`
- Create: `Front/app/src/main/java/com/qiuzhao/flashcards/data/remote/v25/V25Api.kt`
- Create: `Front/app/src/main/java/com/qiuzhao/flashcards/data/remote/v25/RemoteV25Repository.kt`
- Create: `Front/app/src/test/java/com/qiuzhao/flashcards/data/remote/v25/V25RepositoryContractTest.kt`
- Create: `Front/app/src/test/java/com/qiuzhao/flashcards/data/remote/v25/V25SerializationTest.kt`

**Interfaces produced:** `RemoteV25Repository` implements Task 1's interface and maps every OpenAPI
payload/error to typed domain results. It uses existing bearer session and idempotency mechanisms;
visual code sees no DTO, HTTP status, or JSON object.

- [ ] Write RED serialization fixtures from the committed OpenAPI examples for success, empty,
      validation failure, expired auth, conflict, pending deletion, rewrite preview, and stats.
- [ ] Write RED repository tests for auth headers, idempotency keys on mutations, cancellation,
      recoverable failures, and no plaintext API-key logging.
- [ ] Implement DTOs, API calls, mapping, and repository. Do not modify `ui/**`, `MainActivity.kt`,
      Gradle, visible resources, or existing `RemoteFlashcards.kt` beyond a non-visual adapter explicitly
      required by the repository constructor.
- [ ] Run focused repository/serialization tests, then full Android unit tests. Commit and apply M
      review; compare the public interface against Task 1 and backend OpenAPI.

## Task 13: Harden Release environment and reproducible APK output

**Progress package:** NV-07
**Repository:** Android data
**Review tier:** M, promoted to H if signing or secret handling changes
**Dependencies:** Task 12; visual merge is not required for configuration tests

**Files:**

- Controller import from the original worktree: user-owned `.gitignore` signing exclusions and
  `Front/app/build.gradle.kts` signing setup
- Implementer modify after that import: `Front/app/build.gradle.kts`
- Create: `scripts/build-release.sh`
- Create: `Front/app/src/test/java/com/qiuzhao/flashcards/data/remote/v25/ReleaseConfigTest.kt`
- Modify: `Front/app/build.gradle.kts` build-type `BuildConfig` fields; do not add a second environment
  configuration mechanism

**Interfaces produced:** Debug may target an explicit local environment; Release fixes
`https://shanka.kbzz1.top` at build time, exposes no server editor/test switch, and copies the signed
APK atomically to `/home/kbzz1/shanka_backend/releases/app-release.apk` with version `2.5.0`.

- [ ] Before dispatch, the controller captures the exact original-worktree diffs for nested
      `.gitignore` and `Front/app/build.gradle.kts`, records their hashes, and imports those user-owned
      hunks into the isolated branch as a preservation baseline. The implementer may build on them but
      must not rewrite, omit, or claim those hunks; any conflict promotes the task to H.
- [ ] Add a failing build/config test proving Release has the formal base URL, Debug-only local override,
      no Release fixture flag, and version 2.5.0. Do not put signing secrets in source or reports.
- [ ] Implement `BuildConfig` build-type configuration and an atomic output script that verifies Release variant,
      signature presence, version, output path, and SHA-256 before replacement. A failed build leaves
      the previous APK intact.
- [ ] Run Android tests, `assembleDebug`, and Release configuration/build verification possible with
      the provided local signing environment. Absence of signing authority is reported as a gate, not
      bypassed with a fake success.
- [ ] Commit only reviewed configuration/script/test hunks and apply the selected review tier.

## Task 14: Upgrade the black-box test platform to V2.5

**Progress package:** NV-08
**Repository:** Backend
**Review tier:** M
**Dependencies:** Tasks 2–11

**Files:**

- Modify: `test-platform/shanka/client.py`
- Create: `test-platform/scenarios/flow/v25_core_flow.py`
- Create: `test-platform/scenarios/flow/v25_recovery.py`
- Modify: `test-platform/runner/suites.py`
- Create: `test-platform/tests/test_scenarios_v25.py`
- Modify: `test-platform/AGENTS.md` scenario map only after the scenarios are runnable

**Interfaces produced:** The stdlib-only black-box platform has a `v25` suite that validates the
non-visual Release flow against HTTP: auth/profile/preferences, project/PDF/chapters, persisted sample
task, complete generation, today rating, deletion undo, rewrite preview/apply, and real statistics.
The recovery scenario validates app/device-independent retrieval, failed-task retry, and zero partial
visibility. It also verifies logout/relogin does not cancel an active generation task. It retains
existing production/cost confirmation gates and never logs credentials or card content.

- [ ] Write RED stdlib tests with the existing stub client for exact request order, idempotency keys,
      cleanup behavior, failure counting, safe logging, and the distinction between the zero-LLM
      recovery suite and cost-confirmed generation suite.
- [ ] Update the shared client only for reusable typed-safe request helpers; keep it pure stdlib and do
      not import backend implementation packages.
- [ ] Implement `v25_core_flow` and `v25_recovery`, register them as a separate `v25` suite, and retain
      `--confirm-prod` plus derived `--confirm-cost` refusal. Do not weaken or silently reinterpret the
      old quick/full/live suites.
- [ ] Run `python3 -m unittest discover -s test-platform/tests`, then exercise the suite selector with
      stub/local-safe inputs. Confirm scenario failures produce non-zero exit status and no fake PASS.
- [ ] Commit platform code/tests/map together and apply M review. A live DeepSeek run is deferred to
      Task 15 and still requires its explicit cost/environment gates.

## Task 15: Run platform regression and prepare integration evidence

**Progress package:** NV-08
**Repositories:** Backend and Android data, verified separately
**Review tier:** G
**Dependencies:** Tasks 1–14; final signed/connected-device checks wait for V-LANE integration

**Evidence produced:** Two repository commit ranges, full command outputs, migration evidence, AI
quality record, performance dataset/result, APK version/signature/SHA-256 when available, device run
when available, and an explicit list of unverified conditions. No product code is planned in this task.

- [ ] Dispatch a verification subagent to inspect both task ledgers and build a requirement-to-evidence
      checklist. It may run read-only commands/tests but must not patch failures.
- [ ] In the backend worktree run fresh: full `pytest`, `ruff format --check`, `ruff check`, `mypy`,
      fresh-database migration, copied-V2.4 migration, manifest guards, and the recorded performance
      scenario. Run test-platform self-tests and the V2.5 black-box suite with its environment/cost
      gates. Capture exit codes and counts.
- [ ] In the Android data worktree run fresh: unit tests, `assembleDebug`, Release config checks, and
      available Release build/signature/version/hash checks. Do not claim signed APK completion without
      the actual artifact evidence.
- [ ] Inspect each repository's recorded-baseline-to-HEAD diff and `git status --short`. Prove no UI-owned
      file, credential, database, log, sample PDF, generated APK, SDD artifact, or unrelated user change
      entered a commit.
- [ ] Route every failure to the owning task's implementer under that task's tier. After repairs, rerun
      the exact failed command plus affected regressions; do not replace the G gate with ad-hoc fixes.
- [ ] Record which NV-08 checks still require the visual branch, signed Release authority, or target
      Android device, including the PRD's 30-minute stability run. Hand the evidence to the main
      integrator, then execute the two final whole-branch reviews from section 2.4.
