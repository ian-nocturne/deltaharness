# DeltaHarness (`prism-memory`)

**Zero-Setup Instruction Drift Auditor, Session Delta Memory Engine & Vibe-Coder Rescue Kit**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg)](pyproject.toml)
[![Zero Dependencies](https://img.shields.io/badge/Core_Deps-Zero_(Stdlib_Only)-success.svg)](pyproject.toml)

Coding agents (`Claude Code`, `Cursor`, `Codex`, `Windsurf`) silently break when repository files move, function signatures change, `.env` keys go missing, or context compaction wipes architectural constraints.

**DeltaHarness** provides three zero-setup tools in a single pure-Python package (`Apache-2.0`):

1. **`deltaharness audit` (Instruction & Contract Auditor)**: Audits `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `README.md`, and `SKILL.md` against repository files, symbols (`Python` via stdlib `ast`, `TypeScript`/`JavaScript` via comment-stripped contract & balanced-delimiter parser), monorepo `package.json` scripts (tracking `cd <subdir>` context), and `Makefile` targets. Outputs JSON, Markdown, or GitHub Code Scanning **SARIF v2.1.0**, and returns non-zero exit code (`1`) when broken contracts are found.
2. **`viberescue` (`deltaharness rescue`)**: Packages stuck AI-built projects into a secret-scrubbed static diagnostic bundle (`RESCUE_BRIEF.md` + `ONE_PROMPT_RESCUE.md`). Detects missing `.env` keys referenced in code (`process.env.KEY` and `process.env["KEY"]`), client-side secret exposure (respecting `import "server-only"`), cross-migration Supabase RLS & `CREATE POLICY` gaps, and competing lockfiles while recursively scrubbing secret tokens.
3. **`anti-slop` (`deltaharness anti-slop`)**: Scans web/app codebases (`HTML`, `CSS`, `TS`, `TSX`, `JS`, `JSX`, `MJS`, `Vue`, `Svelte`, `Markdown`) for 8 static UI & copy signals: purple radial-gradient blobs (including CSS custom property resolution `var(--blob)`), WCAG 2.1 low-contrast text/button pairs (`< 3.0:1`), emoji UI chrome, fractional pixel font sizes, 3+ nested bordered cards, blocked viewport zoom, missing/empty design token locks, and robotic Korean translation-ese copy.

---

## ⚡ 1-Minute Quickstart

### 1. Install & Run Standalone Tests (Zero External Dependencies Required)

```bash
pip install -e .
python3 -B -m unittest -v test_memory_skills
```

*(Optional)* Enable local ONNX `e5-small` semantic vector acceleration:
```bash
pip install -e ".[semantic]"
```

### 2. Audit Your Repo Instructions (`CLAUDE.md` / `AGENTS.md` / `.cursorrules`)

Run a zero-setup static & contract audit on any repository:

```bash
# Output human-readable Markdown report (exits 1 if errors > 0, or pass --exit-zero)
deltaharness audit . --format markdown

# Git-diff blast radius check (flags invalid_git_ref if base ref does not exist)
deltaharness audit . --base HEAD~1 --head HEAD --format markdown

# Export SARIF v2.1.0 for GitHub Code Scanning
deltaharness audit . --format sarif > deltaharness-audit.sarif
```

### 3. Package a Stuck Project for Static Triage (`viberescue`)

When an AI-coded app says *"Fixed!"* in chat but fails on login, DB, or deployment:

```bash
# Generates secret-scrubbed RESCUE_BRIEF.md & ONE_PROMPT_RESCUE.md
viberescue pack . --write

# Or run via deltaharness CLI
deltaharness rescue . --format markdown
```

**Readiness States (Honest Static Classification)**:
- `NO_TARGETS_FOUND` (exit `2`): Empty directory or zero scannable code/SQL/config files.
- `BLOCKED` (exit `1`): One or more critical static blockers (e.g., client secret exposure, missing `.env` key when `.env` is present, table without `ENABLE ROW LEVEL SECURITY`).
- `ENV_UNVERIFIED` (exit `0`): Zero critical code/SQL blockers, but code references environment variables while no local `.env` file is present to verify values.
- `STATIC_CHECKS_PASSED` (exit `0`): All static code, SQL, lockfile, and instruction checks passed.

### 4. Score & Fix "AI Slop" UI and Copy (`anti-slop`)

Check your frontend or landing page before shipping:

```bash
# Compute 0-100 Slop Score and file:line violations (exits 2 on empty dir, 1 on findings)
anti-slop scan . --format markdown

# Lock a Reference-First Design System (linear-precision, stripe-editorial, or apple-minimal)
# Protects existing DESIGN.md / design-tokens.css unless --force is passed (creates .bak)
anti-slop lock . --archetype linear-precision
```

---

## 📊 Real-World Open-Source Repository Audit Verification

Tested against 4 full public AI repositories at `HEAD` (see `real-oss-audit-results.json`) to verify both true-positive drift detection and monorepo/tutorial false-positive suppression:

| Repository (`HEAD`) | Scanned Docs | References Checked | Errors Found | Verification Summary |
| :--- | :---: | :---: | :---: | :--- |
| `vercel/ai` (`e741656`) | 17 (`AGENTS.md`, `CONTRIBUTING.md`, `skills/**`) | 149 | **3 (`missing_path`)** | Detects 3 real stale paths (`AGENTS.md:94` & `CONTRIBUTING.md:86` → `src/stream-text/openai.ts`; `skills/develop-ai-functions-example/SKILL.md:47` → `generate-text/openai.ts`) with **0 false positives** across monorepo workspace scripts and JS regex utilities. |
| `openai/openai-agents-python` (`64cd5df`) | 414 (`AGENTS.md`, `docs/**`) | 1,164 | **0** | `Makefile` targets, `pyproject.toml`, and 414 multi-language docs resolve with **0 false positives**. |
| `Significant-Gravitas/AutoGPT` (`8d4a965`) | 351 (`CLAUDE.md`, `AGENTS.md`, `.claude/skills/**`, `docs/**`) | 1,476 | **14 (`missing_path`)** | Detects 14 real deleted/moved paths (e.g., `.github/copilot-instructions.md:173` → `frontend/lib/supabase/middleware.ts`, `autogpt_platform/backend/AGENTS.md:135` → `tools/release.py`, `docs/AGENTS.md:18` → `docs/integrations/basic.md`) with **0 false positives**. |
| `anthropics/claude-code` (`d014fc0`) | 2 (`CLAUDE.md`, `README.md`) | 2 | **0** | Resolves `.github/workflows/claude.yml` and repository instructions with **0 false positives**. |

---

## 🔍 Scope & Static Analysis Boundaries

DeltaHarness is a **fast, deterministic, zero-network static analyzer** (`< 50ms` per repo). Knowing what it checks—and what requires a runtime test runner—keeps CI gates trustworthy:

| Capability | Static Method Used | Known Boundary |
| :--- | :--- | :--- |
| **Python Symbols** | Stdlib `ast.parse` (functions, classes, methods, args, defaults, decorators) | Dynamic `getattr` / runtime monkeypatching is not evaluated. |
| **TS/JS Symbols** | Comment-stripped contract extractor + balanced delimiter syntax check (`functions`, `arrow functions`, `React.memo`/`forwardRef`, `classes`, `interfaces`, `types`) | Does not run `tsc` full type-inference across external `node_modules`. |
| **SQL RLS Audit** | Cross-migration aggregation of `CREATE TABLE`, `ENABLE ROW LEVEL SECURITY`, `CREATE POLICY`, and multiline `USING (true)` across all `.sql` files | Evaluates static SQL migration files; does not connect to a live remote Supabase Postgres instance. |
| **WCAG Contrast & Slop** | Static CSS custom property resolver (`:root` / `[data-theme="dark"]`) + WCAG 2.1 relative luminance contrast ratio + HTML nesting depth parser | Evaluates static CSS/HTML/JSX rules; does not launch a headless Chromium layout engine. |

---

## 🤖 GitHub Action Integration

Add `.github/workflows/deltaharness-audit.yml` to gate Pull Requests on instruction drift, deploy blockers, or UI slop:

```yaml
name: DeltaHarness Gate
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

---

## 🧠 Bitemporal Memory & Self-Healing Skill Engine

Beyond static auditing, DeltaHarness includes a complete local-first memory & procedural skill runtime (`StandaloneStore` + `MemoryEngine`):

- **Sub-15ms Hybrid Retrieval**: In-memory BM25 inverted postings + optional binary float32 vector matrix + 2-Hop Personalized PageRank associative recall.
- **Session Delta-Prompting**: Compresses multi-turn agent context by transmitting only state deltas when `coreHash` is unchanged.
- **Execution-Verified Procedural Skills**: Synthesizes reusable `SKILL.md` domain harnesses from solved debugging trajectories and automatically demotes stale skills when referenced code anchors drift.

```bash
# Run the 4-Epoch × 4-Baseline Repo-Evolution Benchmark
deltaharness repo-benchmark

# Check environment & SQLite WAL health
deltaharness doctor --standalone
```

---

## 📄 License & Provenance

- Licensed under the **Apache License, Version 2.0** ([LICENSE](LICENSE)).
- Third-party notices and optional model licenses are documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
