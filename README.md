# Memlet (`memlet`)

**Agent memory that checks whether what it remembers is still true — a self-verifying, code-anchored memory engine and instruction drift guard for coding agents.**

> Twin to **Routlet** under Ian · Nocturne Lab: *Routlet picks the route. Memlet carries the verified context.*
> Formerly **DeltaHarness** / **Ieum Memory** / PRISM Memory. Legacy command names `ieum-memory`, `deltaharness`, and `prism-memory` still work as aliases for this release.

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg)](pyproject.toml)
[![Zero Dependencies](https://img.shields.io/badge/Core_Deps-Zero_(Stdlib_Only)-success.svg)](pyproject.toml)

Coding agents (`Claude Code`, `Cursor`, `Codex`, `Windsurf`) silently break when repository files move, function signatures change, `.env` keys go missing, or context compaction wipes architectural constraints. Most memory tools make this worse in one specific way: they keep injecting a memory long after the code it describes has changed.

**Memlet** anchors memories and instructions to the code they talk about, and refuses to hand over context it cannot verify. It ships in a single pure-Python package (`Apache-2.0`, stdlib-only core) with two sub-plugins:

1. **`memlet check` (Instruction & Contract Checker, alias `audit`)**: Audits `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `README.md`, and `SKILL.md` against repository files, symbols (`Python` via stdlib `ast`, `TypeScript`/`JavaScript` via comment-stripped contract & balanced-delimiter parser), monorepo `package.json` scripts (tracking `cd <subdir>` context), and `Makefile` targets. Outputs JSON, Markdown, or GitHub Code Scanning **SARIF v2.1.0**, and returns non-zero exit code (`1`) when broken contracts are found.
2. **`viberescue` (`memlet rescue`)**: Packages stuck AI-built projects into a secret-scrubbed static diagnostic bundle (`RESCUE_BRIEF.md` + `ONE_PROMPT_RESCUE.md`). Detects missing `.env` keys referenced in code (`process.env.KEY` and `process.env["KEY"]`), client-side secret exposure (respecting `import "server-only"`), cross-migration Supabase RLS & `CREATE POLICY` gaps, and competing lockfiles while recursively scrubbing secret tokens.
3. **`anti-slop` (`memlet anti-slop`)**: Scans web/app codebases (`HTML`, `CSS`, `TS`, `TSX`, `JS`, `JSX`, `MJS`, `Vue`, `Svelte`, `Markdown`) for 8 static UI & copy signals: purple radial-gradient blobs (including CSS custom property resolution `var(--blob)`), WCAG 2.1 low-contrast text/button pairs (`< 3.0:1`), emoji UI chrome, fractional pixel font sizes, 3+ nested bordered cards, blocked viewport zoom, missing/empty design token locks, and robotic Korean translation-ese copy.

---

## 🧭 Where Memlet Fits Among Agent Memory Tools

There are good agent memory tools already. Memlet is not trying to be a general chat-memory SDK; it is built for one job: **keeping a coding agent's memory honest about the current state of the repository.**

| Tool | What it does well | What Memlet adds for coding agents |
| :--- | :--- | :--- |
| **ai-memory** | Single binary, Git-backed Markdown wiki that people can read and edit, no external LLM by default, broad agent hook coverage. | Each wiki entry carries a code anchor (file, symbol signature, commit). When the anchored symbol changes, the entry is marked `stale` instead of being injected as-is. |
| **Mem0** | One-line SDK, automatic fact extraction from conversations, vector + graph retrieval. | No LLM call on the default write path, so negations and exact constraints are stored verbatim. Current state is a deterministic projection of a lossless ledger tied to branches and commits. |
| **Letta** (MemGPT) | Self-editing core/archival memory blocks, agent file system, sleep-time reorganization. | Sits on top of the CLI/IDE you already use (Claude Code, Codex, Cursor) instead of replacing the agent runtime. Procedural skills are promoted only after an execution receipt. |
| **Zep / Graphiti** | Bi-temporal knowledge graph (event time vs. ingestion time), fact supersession and edge invalidation. | The same `at` / `known_at` / supersession model in a single-file local SQLite ledger with in-memory postings — no graph database, no extraction pipeline to operate. |
| **claude-mem** | Session lifecycle hooks, progressive disclosure (short index first, full body on demand). | Fully offline default path, Session Delta-Prompting (send only what changed), and anchor checks before anything is disclosed. |

*Comparison reflects each project's public repository and docs as of October 2026. Features change quickly — check each project's own documentation before choosing.*

### How Memlet combines the pieces

- **Human-readable Markdown wiki, synced both ways (`.memlet/wiki/`)** — `memlet init` writes `.memlet/config.json` (pinned anchor hashes), `.memlet/wiki/INDEX.md`, and a local SQLite ledger `.memlet/ledger.sqlite3` (`anchor_snapshots` + `wiki_events`). People read, diff, and edit `.memlet/wiki/*.md` directly; `memlet wiki` compares each page body with its last recorded hash and records a changed or new page as a `user_edit` event in that ledger.
- **Zero-LLM, local, bi-temporal ledger** — Every memory keeps when it was true (`at`) and when the system learned it (`known_at`), with `supersedes` / `retract` lineage. Point-in-time recall (`--at`, `--known-at`) and "why did this change?" both come from the ledger, not from a model's summary.
- **AST symbol & signature anchoring** — Memories and skills that reference code store the file path, symbol signature, and commit. On each check they resolve to:
  - `active` — anchor still matches;
  - `compatible-unverified` — body changed or an optional argument was added; re-verify before trusting;
  - `stale` — symbol removed, required arguments changed, or file moved; blocked from automatic injection.
- **Session Delta-Prompting** — Within one session, if `coreHash` is unchanged only the delta is sent; a compaction or session restart triggers a fresh baseline.
- **5-factor admission & poisoning quarantine** — Utility and source trust are scored separately and never summed, so text that arrived from untrusted external input cannot be promoted into a behavioral rule just by being repeated.
- **Import what you already have** — `memlet import` reads rules from `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `.ai-memory/**/*.md`, and `.claude-mem/**/*.md`, checks every code anchor in them right away, and writes the result to one wiki page with a status tag per rule.
- **Cross-Vendor Handover Capsules** — When you hit a rate limit and move from one agent to another, `memlet handover` builds a signed capsule under 500 tokens: only constraints whose anchors are still `active`, stale anchors listed as quarantined, rejected approaches (dead ends), modified-file count, and the next step. Execution and billing approval are never carried over to the next model. (`memlet capsule` remains for budgeted task-memory capsules.)

---

## ⚡ 1-Minute Quickstart

### 1. Install & Initialize (Zero External Dependencies Required)

```bash
pip install git+https://github.com/ian-nocturne/deltaharness.git

# One-command, zero-daemon setup: .memlet/config.json, .memlet/ledger.sqlite3, .memlet/wiki/INDEX.md
memlet init .
```

From a source checkout, run the standalone test suite:

```bash
pip install -e .
python3 -B -m unittest -v test_memory_skills
```

*(Optional)* Enable local ONNX `e5-small` semantic vector acceleration:
```bash
pip install -e ".[semantic]"
```

### 2. Check Your Repo Instructions (`CLAUDE.md` / `AGENTS.md` / `.cursorrules`)

Run a zero-setup static & contract audit on any repository:

```bash
# Output human-readable Markdown report (exits 1 if errors > 0, or pass --exit-zero)
memlet check . --format markdown

# Git-diff blast radius check (flags invalid_git_ref if base ref does not exist)
memlet check . --base HEAD~1 --head HEAD --format markdown

# Export SARIF v2.1.0 for GitHub Code Scanning
memlet check . --format sarif > memlet-check.sarif
```

### 3. Project Memory into a Readable Wiki (`memlet wiki`)

```bash
# Refreshes the INDEX.md status table, adds a per-page AST status header
# (active / compatible-unverified / stale), and records page edits in .memlet/ledger.sqlite3
memlet wiki .
```

### 4. Import Existing Rules & Hand Over Verified Context (`memlet import` / `memlet handover`)

```bash
# Preview: tag every imported rule active / compatible-unverified / stale (or unanchored)
memlet import . --dry-run --format markdown

# Write .memlet/wiki/imported-rules.md, pin anchors in .memlet/config.json, log an import event
memlet import . --from auto        # or: claude-md | cursorrules | ai-memory | claude-mem

# Signed handover capsule for the next model (Claude / Codex / Gemini); stale anchors are left out
memlet handover . --next-step "Finish the refund refactor" \
  --rejected "Retrying inside the webhook handler double-charges" --format markdown

# Also save it as .memlet/wiki/HANDOVER.md and record a handover event in the ledger
memlet handover . --next-step "Finish the refund refactor" --write
```

The capsule carries `repoHead`, `capsuleSignature` (SHA-256 over the canonical payload), `activeConstraints`, `quarantinedStaleAnchors`, `rejectedApproaches` (your `--rejected` items plus bullets under `## Rejected Approaches` / `## Dead-Ends` in wiki pages), `executionProof` (modified-file count and anchor health), `nextAction`, and `authorityBoundary: "read-only-context-only: execution and external side-effect approvals are NOT inherited"`. Rules without any code anchor are counted but not carried, because there is nothing to verify them against.

### 5. Package a Stuck Project for Static Triage (`viberescue`)

When an AI-coded app says *"Fixed!"* in chat but fails on login, DB, or deployment:

```bash
# Generates secret-scrubbed RESCUE_BRIEF.md & ONE_PROMPT_RESCUE.md
viberescue pack . --write

# Or run via the memlet CLI
memlet rescue . --format markdown
```

**Readiness States (Honest Static Classification)**:
- `NO_TARGETS_FOUND` (exit `2`): Empty directory or zero scannable code/SQL/config files.
- `BLOCKED` (exit `1`): One or more critical static blockers (e.g., client secret exposure, missing `.env` key when `.env` is present, table without `ENABLE ROW LEVEL SECURITY`).
- `ENV_UNVERIFIED` (exit `0`): Zero critical code/SQL blockers, but code references environment variables while no local `.env` file is present to verify values.
- `STATIC_CHECKS_PASSED` (exit `0`): All static code, SQL, lockfile, and instruction checks passed. This is a static result, not a guarantee that the live deployment works.

### 6. Score & Fix "AI Slop" UI and Copy (`anti-slop`)

Check your frontend or landing page before shipping:

```bash
# Compute 0-100 Slop Score and file:line violations (exits 2 on empty dir, 1 on findings)
anti-slop scan . --format markdown

# Or run via the memlet CLI
memlet anti-slop . --format markdown

# Lock a Reference-First Design System (linear-precision, stripe-editorial, or apple-minimal)
# Protects existing DESIGN.md / design-tokens.css unless --force is passed (creates .bak)
anti-slop lock . --archetype linear-precision
```

### 7. Inspect Memory & Routing in One Page (`memlet ui`)

The **Nocturne Memory & Routing Inspector** renders one self-contained HTML file (no CDN, no network, no daemon) with the repository's AST anchor health, the refactoring & handover benchmark, the current handover capsule, and the Routlet performance / balanced / efficiency matrix. Every repository string is HTML-escaped and absolute host paths are left out.

```bash
memlet ui .                                   # writes .memlet/inspector.html after `memlet init` (alias: memlet inspector .)
memlet ui . --output memlet-inspector.html    # or any other path
```

### 8. Start a New Project With a Stack Harness (`memlet pack`)

The **Routlet × Memlet Pro Harness Pack** writes one bundle per stack (`nextjs-supabase`, `fastapi-postgres`, `swift-ios`) with 7 files: a `CLAUDE.md` constitution (anchor conventions, session delta rules, human gates), a cross-vendor `AGENTS.md`, a `SKILL.md` with pre-flight checks, receipt and rollback, a `PITFALLS.md` of 5 recorded dead-ends that `memlet handover` carries as rejected approaches, a Routlet routing preset, Linear / Stripe / Toss design tokens (every text pair >= 4.5:1), and a `MANIFEST.json` with SHA-256 checksums.

```bash
memlet pack --list                                   # stack metadata
memlet pack --stack all --format markdown            # compute files and digests in memory, write nothing
memlet pack --stack nextjs-supabase --output ./harness-packs
```

### Command Reference

| Command | Purpose |
| :--- | :--- |
| `memlet init .` | Zero-daemon setup: `.memlet/config.json`, `.memlet/ledger.sqlite3`, and `.memlet/wiki/INDEX.md`. |
| `memlet check .` (alias `audit`) | Static instruction & memory drift audit (Markdown / JSON / SARIF). |
| `memlet wiki .` | Re-checks wiki anchors, writes per-page AST status headers, and syncs page edits into the ledger. |
| `memlet import . [--from auto\|claude-md\|cursorrules\|ai-memory\|claude-mem] [--dry-run]` | Imports existing rules and tags each with its live anchor status. |
| `memlet handover . [--next-step "..."] [--rejected "..."] [--format markdown] [--write]` | Signed, <500-token handover capsule with stale anchors quarantined. |
| `memlet benchmark [--format markdown]` (alias `compare-bench`) | Reproducible refactoring & handover comparison (see below). |
| `memlet rescue .` (or `viberescue pack .`) | Secret-scrubbed static triage bundle for stuck projects. |
| `memlet anti-slop .` (or `anti-slop scan .`) | UI & copy slop score with `file:line` findings. |
| `memlet ui . [--output FILE]` (alias `inspector`) | Self-contained, escaped HTML inspector: AST health, benchmark, handover, Routlet matrix. |
| `memlet pack [--stack all\|nextjs-supabase\|fastapi-postgres\|swift-ios] [--output DIR] [--dry-run] [--list]` | Stack harness bundles with a SHA-256 `MANIFEST.json`; no `--output` means nothing is written. |
| `memlet --standalone capsule --plan "..." --files prism_memory/audit.py` | Budgeted cross-vendor handover capsule. |
| `memlet --standalone doctor` | Environment & SQLite WAL health check. |

Every `memlet` command is also reachable through the legacy aliases `ieum-memory`, `deltaharness`, and `prism-memory`.

---

## 🧪 Reproducible Refactoring & Handover Benchmark

`memlet benchmark --format markdown` builds four small repositories in a temporary directory, records rules before a refactor, applies the refactor, and measures what each approach would still feed the next agent. It makes no network or LLM calls and prints a `resultDigest` that is identical on every run (only `elapsedMs` varies).

Scenarios: (1) breaking signature change `def charge(user_id)` → `def charge(account_id, currency)`; (2) optional argument `def route(task)` → `def route(task, effort="high")`; (3) file move from a legacy auth module to an `auth/` package; (4) cross-vendor handover after a refactor, with two recorded dead-ends.

| Mode | Stale procedure injection | Optional-arg false positives | Handover dead-end prevention |
| :--- | :---: | :---: | :---: |
| Raw Markdown (static `CLAUDE.md` / `.cursorrules`) | 100% (3/3) | 0% (0/1) | 0% (0/3) |
| Unverified Wiki (timestamps, 90-day aging, file-exists check) | 66.7% (2/3) | 0% (0/1) | 66.7% (2/3) |
| Memlet (AST anchors + `reqSigHash` pins + handover quarantine) | **0% (0/3)** | **0% (0/1)** | **100% (3/3)** |

All three modes make 0 external LLM calls. Read the numbers for what they are: these are four synthetic fixtures built to exercise specific failure modes, and "Unverified Wiki" is a simplified model of a timestamped Markdown wiki, not a run of any particular product. The unverified wiki's 0% false-positive rate comes from not checking signatures at all, which is also why it lets the breaking change through.

---

## 📊 Real-World Open-Source Repository Audit Verification

> **Category sample:** the public-repository numbers below come from 4 repositories pinned at fixed commits (`vercel/ai`, `openai-agents-python`, `AutoGPT`, `claude-code`; 784 docs, 2,745 references, 10 findings). They are not a guarantee for other repositories. "0 false positives" means the 10 findings in this sample were checked by hand.

`memlet check` was run against 4 full public AI repositories at the pinned commits below (see `real-oss-audit-results.json`) to verify both true-positive drift detection and monorepo/tutorial false-positive suppression:

| Repository (pinned commit) | Scanned Docs | References Checked | Errors Found | Verification Summary |
| :--- | :---: | :---: | :---: | :--- |
| `vercel/ai` (`e741656`) | 17 (`AGENTS.md`, `CONTRIBUTING.md`, `skills/**`) | 150 | **1 (`missing_path`)** | Detects 1 real stale path (`CONTRIBUTING.md:86` → `src/stream-text/openai.ts`, refactored to `src/stream-text/openai/basic.ts`) while suppressing negative examples (`Do not create...`) and resolving monorepo workspace scripts with **0 false positives in this sample**. |
| `openai/openai-agents-python` (`64cd5df`) | 414 (`AGENTS.md`, `docs/**`) | 1,161 | **0** | `Makefile` targets, `pyproject.toml`, and 414 multi-language docs resolve with **0 false positives in this sample**. |
| `Significant-Gravitas/AutoGPT` (`8d4a965`) | 351 (`CLAUDE.md`, `AGENTS.md`, `.claude/skills/**`, `docs/**`) | 1,432 | **9 (`missing_path`)** | Detects 9 real deleted/moved paths in the Significant-Gravitas/AutoGPT repo (e.g., `.github/copilot-instructions.md:173` → `frontend/lib/supabase/middleware.ts`, `docs/AGENTS.md:18` → `docs/integrations/basic.md`, `docs/content/forge/get-started.md:22` → `classic/forge/agent/forge_agent.py`, `.claude/skills/vercel-react-best-practices/SKILL.md:114` → `rules/_sections.md`) while suppressing external-repo and code-fence `title=` examples with **0 false positives in this sample**. |
| `anthropics/claude-code` (`d014fc0`) | 2 (`CLAUDE.md`, `README.md`) | 2 | **0** | Resolves `.github/workflows/claude.yml` and repository instructions with **0 false positives in this sample**. |

---

## 🔍 Scope & Static Analysis Boundaries

Memlet's checker is a **fast, deterministic, zero-network static analyzer** (`~10–60ms` on typical repos; `~0.4–5s` on 350+ doc monorepos). Knowing what it checks—and what requires a runtime test runner—keeps CI gates trustworthy. Passing static checks is not a live production guarantee:

| Capability | Static Method Used | Known Boundary |
| :--- | :--- | :--- |
| **Python Symbols** | Stdlib `ast.parse` (functions, classes, methods, args, defaults, decorators) | Dynamic `getattr` / runtime monkeypatching is not evaluated. |
| **TS/JS Symbols** | Comment-stripped contract extractor + balanced delimiter syntax check (`functions`, `arrow functions`, `React.memo`/`forwardRef`, `classes`, `interfaces`, `types`) | Does not run `tsc` full type-inference across external `node_modules`. |
| **SQL RLS Audit** | Cross-migration aggregation of `CREATE TABLE`, `ENABLE ROW LEVEL SECURITY`, `CREATE POLICY`, and multiline `USING (true)` across all `.sql` files | Evaluates static SQL migration files; does not connect to a live remote Supabase Postgres instance. |
| **WCAG Contrast & Slop** | Static CSS custom property resolver (`:root` / `[data-theme="dark"]`) + WCAG 2.1 relative luminance contrast ratio + HTML nesting depth parser | Evaluates static CSS/HTML/JSX rules; does not launch a headless Chromium layout engine. |

---

## 🤖 GitHub Action Integration

Add `.github/workflows/memlet-check.yml` to gate Pull Requests on instruction & memory drift, deploy blockers, or UI slop:

```yaml
name: Memlet Check Gate
on: [pull_request]

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: ian-nocturne/deltaharness@main
        with:
          mode: "audit" # 'audit', 'rescue', 'anti-slop', or 'all'
          repo-path: "."
          base-ref: ${{ github.event.pull_request.base.sha }}
          fail-on-drift: "true"
```

Inputs, outputs (`status`, `finding-count`, `total-finding-count`, `slop-score`), and the default SARIF file name (`deltaharness-audit.sarif`) are unchanged from earlier releases, so existing workflows keep working.

---

## 🧠 Bi-Temporal Memory & Self-Healing Skill Engine

Beyond static checking, Memlet includes a complete local-first memory & procedural skill runtime (`StandaloneStore` + `MemoryEngine`):

- **Sub-15ms Hybrid Retrieval**: In-memory BM25 inverted postings + optional binary float32 vector matrix + 2-Hop Personalized PageRank associative recall.
- **Session Delta-Prompting**: Compresses multi-turn agent context by transmitting only state deltas when `coreHash` is unchanged.
- **Execution-Verified Procedural Skills**: Synthesizes reusable `SKILL.md` domain harnesses from solved debugging trajectories and automatically demotes stale skills when referenced code anchors drift.

```bash
# Run the 4-Epoch × 4-Baseline Repo-Evolution Benchmark
memlet --standalone repo-benchmark

# Check environment & SQLite WAL health
memlet --standalone doctor
```

---

## 📄 License & Provenance

- Licensed under the **Apache License, Version 2.0** ([LICENSE](LICENSE)).
- Third-party notices and optional model licenses are documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
