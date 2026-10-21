# DeltaHarness (`prism-memory`)

**Zero-Setup Instruction Drift Auditor, Session Delta Memory Engine & Vibe-Coder Rescue Kit**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg)](pyproject.toml)
[![Zero Dependencies](https://img.shields.io/badge/Core_Deps-Zero_(Stdlib_Only)-success.svg)](pyproject.toml)

Coding agents (`Claude Code`, `Cursor`, `Codex`, `Windsurf`) silently break when repository files move, function signatures change, `.env` keys go missing, or context compaction wipes architectural constraints.

**DeltaHarness** provides three zero-setup tools in a single pure-Python package:

1. **`deltaharness audit` (Open-Source Core)**: Audits `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/*.mdc`, `README.md`, and `SKILL.md` against your actual AST symbols (`Python`, `TypeScript`, `JavaScript`), `package.json` scripts, and `Makefile` targets. Outputs JSON, Markdown, or GitHub Code Scanning **SARIF v2.1.0**.
2. **`viberescue` (`deltaharness rescue`)**: Packages stuck AI-built projects into a secret-masked diagnostic bundle (`RESCUE_BRIEF.md` + `ONE_PROMPT_RESCUE.md`). Detects missing `.env` keys referenced in code, exposed client secrets, Supabase RLS gaps, and competing lockfiles without ever leaking secret values.
3. **`anti-slop` (`deltaharness anti-slop`)**: Scans web/app codebases (`HTML`, `CSS`, `TSX`, `JSX`, `Vue`, `Svelte`, `Markdown`) for 7 unmistakable "AI Slop" signals (purple radial-gradient blobs, emoji UI chrome, fractional pixel font sizes, 3+ nested bordered cards, blocked viewport zoom, missing design tokens, and robotic Korean translation-ese copy) and generates a Reference-First `DESIGN.md` + `design-tokens.css` lock.

---

## ⚡ 1-Minute Quickstart

### 1. Install (Zero External Dependencies Required)

```bash
pip install -e .
```

*(Optional)* Enable local ONNX `e5-small` semantic vector acceleration:
```bash
pip install -e ".[semantic]"
```

### 2. Audit Your Repo Instructions (`CLAUDE.md` / `AGENTS.md` / `.cursorrules`)

Run a zero-setup static & AST contract audit on any repository:

```bash
# Output human-readable Markdown report
deltaharness audit . --format markdown

# Git-diff blast radius check (e.g., against main branch)
deltaharness audit . --base HEAD~1 --head HEAD --format markdown

# Export SARIF v2.1.0 for GitHub Code Scanning
deltaharness audit . --format sarif > deltaharness-audit.sarif
```

### 3. Package a Stuck Project for Rescue or Handoff (`viberescue`)

When an AI-coded app says *"Fixed!"* in chat but fails on login, DB, or deployment:

```bash
# Generates secret-masked RESCUE_BRIEF.md & ONE_PROMPT_RESCUE.md
viberescue pack . --write

# Or run via deltaharness CLI
deltaharness rescue . --format markdown
```

### 4. Score & Fix "AI Slop" UI and Copy (`anti-slop`)

Check your frontend or landing page for telltale AI Slop before shipping:

```bash
# Compute 0-100 Slop Score and file:line violations
anti-slop scan . --format markdown

# Lock a Reference-First Design System (linear-precision, stripe-editorial, or apple-minimal)
anti-slop lock . --archetype linear-precision
```

---

## 🤖 GitHub Action Integration (30 Seconds)

Add `.github/workflows/deltaharness-audit.yml` to block broken agent instructions and environment drift on every Pull Request:

```yaml
name: DeltaHarness Audit
on: [pull_request]

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: ./
        with:
          mode: "audit"
          repo-path: "."
          base-ref: ${{ github.event.pull_request.base.sha }}
          fail-on-drift: "true"
```

---

## 🧠 Bitemporal Memory & Self-Healing Skill Engine

Beyond static auditing, DeltaHarness includes a complete local-first memory & procedural skill runtime:

- **Sub-15ms Hybrid Retrieval**: In-memory BM25 inverted postings + optional binary float32 vector matrix + 2-Hop Personalized PageRank associative recall.
- **Session Delta-Prompting**: Compresses multi-turn agent context by **66.9%–96%+** by transmitting only state deltas when `coreHash` is unchanged.
- **Execution-Verified Procedural Skills**: Synthesizes reusable `SKILL.md` domain harnesses from solved debugging trajectories and automatically demotes stale skills when referenced code anchors drift.

```bash
# Run the 4-Epoch × 4-Baseline Repo-Evolution Benchmark
deltaharness repo-benchmark

# Check environment & SQLite WAL health
deltaharness doctor
```

---

## 📄 License & Provenance

- Licensed under the **Apache License 2.0** ([LICENSE](LICENSE)).
- Third-party notices and optional model licenses are documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
