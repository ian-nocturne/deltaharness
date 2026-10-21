"""Anti-Slop Ship Gate (`anti-slop`) — UI & Korean Copy Anti-Slop Scanner and Reference-First Design Lock.

Addresses the validated Vibe Coder & Maker pain point (`P6` in Report 1):
- Instant user distrust caused by unmistakable "AI Slop" aesthetics (purple/indigo radial gradients,
  emoji-stuffed UI chrome, fractional pixel font sizes, 3+ nested bordered cards, blocked viewport zoom,
  missing design token discipline, and robotic Korean translation-ese copy).

Capabilities:
1. `scan_anti_slop(target_path)`:
   - Computes a deterministic `slopScore` (0–100, where 0 is cleanest reference-grade UI and 100 is heavy AI slop)
     and a `grade` (`A+`, `A`, `B`, `C`, `D`, `F`).
   - Detects 7 concrete categories of AI Slop with `file:line` citations.
   - Generates a copy-pasteable `ANTI_SLOP_PATCH_PROMPT.md` to fix every violation.
2. `lock_design_system(target_path, archetype="linear-precision")`:
   - Generates `DESIGN.md` (with Reference Study & Anti-References) and `design-tokens.css` (4-step font scale,
     3-step surface hierarchy, `.status-dot-sm` contract, and tabular numerals) so coding agents stay locked
     to a category-defining design system.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional

from .audit import EXCLUDED_DIRS

UI_EXTENSIONS = {".html", ".htm", ".css", ".tsx", ".jsx", ".vue", ".svelte"}
COPY_EXTENSIONS = {".md", ".mdx", ".html", ".htm", ".tsx", ".jsx", ".vue", ".svelte"}

# 1. Purple / Indigo / Radial Blob AI Gradient Slop
PURPLE_SLOP_RE = re.compile(
    r"(?:from-(?:purple|indigo|violet|fuchsia)-[456789]00|"
    r"via-(?:purple|indigo|violet|fuchsia)-[456789]00|"
    r"to-(?:purple|indigo|violet|fuchsia|pink)-[456789]00|"
    r"--ambient-mesh\s*:\s*radial-gradient|"
    r"radial-gradient\s*\([^)]*(?:#8b5cf6|#6366f1|#a855f7|rgba\(\s*139\s*,\s*92\s*,\s*246))",
    re.IGNORECASE,
)

# 2. Fractional Pixel Font Size Slop (10.1px .. 14.9px in CSS, JSX fontSize, or Tailwind text-[...])
FRACTIONAL_FONT_RE = re.compile(
    r"(?:(?:font-size|fontSize)\s*:\s*['\"]?(?:10\.[1-9]|11\.[1-9]|12\.[1-9]|13\.[1-9]|14\.[1-9])px\b|"
    r"text-\[(?:10\.[1-9]|11\.[1-9]|12\.[1-9]|13\.[1-9]|14\.[1-9])px\])"
)

# 3. Viewport Zoom Block Slop
VIEWPORT_ZOOM_BLOCK_RE = re.compile(
    r"<meta[^>]+name=[\"']viewport[\"'][^>]+(?:user-scalable\s*=\s*no|maximum-scale\s*=\s*1(?:\.0)?\b)",
    re.IGNORECASE,
)

# 4. Emoji Chrome Slop inside UI controls/headings
EMOJI_CHROME_RE = re.compile(
    r"(?:<(?:button|a|h1|h2|h3|h4|span|nav|header|label)[^>]*>[^<\n]{0,40}?"
    r"(?:🚀|✨|⚡|🔥|💡|📋|🟢|🟡|⚙️|💭|✴️|🎉|💎|🤖|🌟))|"
    r"(?:✴️\s*작업\s*진행\s*중|💭\s*사고\s*과정\s*보기|⚡️\s*실행한\s*도구|📋\s*사양\s*계약|🟢\s*확정\s*사양|🟡\s*미정\s*사항)"
)

# 5. Excessive Card-in-Card / Glow Slop
HEAVY_CARD_SLOP_RE = re.compile(
    r"(?:shadow-2xl[^>\"\n]*backdrop-blur|backdrop-blur[^>\"\n]*shadow-2xl|"
    r"border-[24]\s+border-(?:purple|indigo|violet|pink)-)"
)

# 6. High-Signal Korean AI Translation-ese / Marketing Slop Patterns
KOREAN_AI_SLOP_PATTERNS = (
    (re.compile(r"혁신적인\s+(?:솔루션|플랫폼|경험|패러다임)"), "AI 상투어 ('혁신적인 솔루션/플랫폼')"),
    (re.compile(r"극대화(?:합니다|하세요|할\s*수\s*있습니다|하며)"), "번역투 어휘 ('극대화~')"),
    (re.compile(r"다양한\s+[가-힣A-Za-z0-9_]+\s*을\(를\)\s*제공합니다"), "기계적 템플릿 ('다양한 ~을(를) 제공합니다')"),
    (re.compile(r"[가-힣]+에\s+있어서\s+[가-힣]+"), "번역투 조사 ('~에 있어서')"),
    (re.compile(r"새로운\s+지평을\s+열"), "AI 과장 클리셰 ('새로운 지평을 열~')"),
    (re.compile(r"완벽한\s+조화를\s+이루"), "AI 과장 클리셰 ('완벽한 조화를 이루~')"),
)

REQUIRED_TOKENS = (
    "--font-caption",
    "--font-body",
    "--font-title",
    "--font-display",
    "--surface-canvas",
    "--surface-card",
    "--surface-sheet",
    ".status-dot-sm",
)

ARCHETYPE_PRESETS: Dict[str, Dict[str, Any]] = {
    "linear-precision": {
        "name": "Linear / Vercel Geist Precision Minimal",
        "benchmarks": [
            {
                "product": "Linear",
                "whyItWorks": "High-contrast monochrome surfaces with 1px subtle hairlines and zero decorative blobs.",
                "keyBorrowing": "Use `--surface-canvas`, `--surface-card`, and `1px solid var(--token-border-subtle)` without nested box shadows.",
            },
            {
                "product": "Vercel Geist",
                "whyItWorks": "Strict 4-step typography scale and tabular numerals for technical clarity.",
                "keyBorrowing": "Lock font sizes to 12px/14px/16px/28px (`--font-caption` to `--font-display`) and enforce `tabular-nums`.",
            },
            {
                "product": "Raycast / Cursor",
                "whyItWorks": "Monochrome vector stroke icons and 6px semantic status dots instead of noisy emojis.",
                "keyBorrowing": "Replace all emoji chrome (`🚀`, `✨`, `⚙️`) with `.status-dot-sm` and 1.5px stroke SVGs.",
            },
        ],
        "antiReferences": [
            "Purple/indigo radial-gradient background blobs (`from-purple-500`, `via-indigo-500`)",
            "Emoji icons inside buttons, tabs, or card headers (`🚀`, `✨`, `📋`, `⚙️`)",
            "Fractional pixel font sizes (`11.5px`, `13.2px`) and 3+ nested bordered cards",
            "Robotic Korean AI translation-ese (`~에 있어서`, `극대화합니다`, `혁신적인 솔루션`)",
        ],
        "light": {
            "--surface-canvas": "#FAFAFA",
            "--surface-card": "#FFFFFF",
            "--surface-subtle": "#F4F4F5",
            "--surface-sheet": "rgba(255, 255, 255, 0.88)",
            "--token-border-subtle": "#E4E4E7",
            "--token-border-strong": "#D4D4D8",
            "--text-primary": "#09090B",
            "--text-secondary": "#3F3F46",
            "--text-muted": "#71717A",
            "--accent-primary": "#18181B",
        },
        "dark": {
            "--surface-canvas": "#09090B",
            "--surface-card": "#121215",
            "--surface-subtle": "#18181B",
            "--surface-sheet": "rgba(18, 18, 21, 0.88)",
            "--token-border-subtle": "#27272A",
            "--token-border-strong": "#3F3F46",
            "--text-primary": "#FAFAFA",
            "--text-secondary": "#A1A1AA",
            "--text-muted": "#71717A",
            "--accent-primary": "#FAFAFA",
        },
    },
    "stripe-editorial": {
        "name": "Stripe Press / Editorial High-Trust",
        "benchmarks": [
            {
                "product": "Stripe Docs & Press",
                "whyItWorks": "Warm alabaster canvas, crisp ink contrast, and restrained semantic accents.",
                "keyBorrowing": "Pair `#FBFBF9` warm paper canvas with `#111827` deep ink and structured 1px hairlines.",
            },
            {
                "product": "Read.cv / Notion",
                "whyItWorks": "Generous whitespace rhythm (8px/16px/24px) and zero neon gradient clutter.",
                "keyBorrowing": "Separate content sections with whitespace and hairline dividers instead of nested cards.",
            },
            {
                "product": "Toss Product Copy",
                "whyItWorks": "1 sentence 1 judgment, active Korean verbs, zero translation-ese.",
                "keyBorrowing": "Apply ASD-STE100 + Toss clarity rules to all user-facing headings and CTAs.",
            },
        ],
        "antiReferences": [
            "Neon dark-mode purple glow cards (`shadow-2xl` + `border-purple-500`)",
            "Emoji bullet lists in hero sections",
            "Blocked mobile viewport pinch-to-zoom (`user-scalable=no`)",
        ],
        "light": {
            "--surface-canvas": "#FBFBF9",
            "--surface-card": "#FFFFFF",
            "--surface-subtle": "#F3F3EE",
            "--surface-sheet": "rgba(255, 255, 255, 0.92)",
            "--token-border-subtle": "#E5E5E0",
            "--token-border-strong": "#D1D1C7",
            "--text-primary": "#111827",
            "--text-secondary": "#374151",
            "--text-muted": "#6B7280",
            "--accent-primary": "#0F172A",
        },
        "dark": {
            "--surface-canvas": "#0B0F17",
            "--surface-card": "#111827",
            "--surface-subtle": "#1E293B",
            "--surface-sheet": "rgba(17, 24, 39, 0.90)",
            "--token-border-subtle": "#1E293B",
            "--token-border-strong": "#334155",
            "--text-primary": "#F8FAFC",
            "--text-secondary": "#CBD5E1",
            "--text-muted": "#64748B",
            "--accent-primary": "#38BDF8",
        },
    },
}


def scan_anti_slop(
    target_path: str | Path,
    output_format: str = "json",
) -> Dict[str, Any]:
    """Scan a directory or file for UI and copy AI Slop and return a 0-100 Slop Score."""
    started = time.perf_counter()
    target = Path(target_path).expanduser().resolve()
    if not target.exists():
        raise ValueError(f"Target path does not exist: {target}")

    root_dir = target if target.is_dir() else target.parent
    findings: List[Dict[str, Any]] = []
    passed_checks: List[str] = []

    # Check DESIGN.md and design-tokens.css lock when scanning a directory with UI files
    files_to_scan: List[Path] = []
    if target.is_file():
        files_to_scan.append(target)
    else:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDED_DIRS and d not in {"tests", "docs", "artifacts", "backups", "logs"}
            ]
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                if fpath.is_symlink():
                    continue
                if fpath.name in {
                    "verify_design_harness.py",
                    "ui_anti_slop_gate.py",
                    "anti_slop.py",
                    "study_design_system.py",
                    "DESIGN.md",
                    "RESCUE_BRIEF.md",
                    "ONE_PROMPT_RESCUE.md",
                    "ANTI_SLOP_PATCH_PROMPT.md",
                    "CLAUDE.md",
                    "AGENTS.md",
                    "HANDOFF.md",
                    "THIRD_PARTY_NOTICES.md",
                }:
                    continue
                if fpath.name.startswith("test_"):
                    continue
                if fpath.suffix.lower() in (UI_EXTENSIONS | COPY_EXTENSIONS):
                    files_to_scan.append(fpath)

    has_ui_files = any(f.suffix.lower() in UI_EXTENSIONS for f in files_to_scan)
    tokens_file = root_dir / "design-tokens.css"
    design_md_file = root_dir / "DESIGN.md"

    if has_ui_files and target.is_dir():
        if not design_md_file.exists() or not tokens_file.exists():
            findings.append({
                "category": "missing_design_lock",
                "severity": "warning",
                "weight": 15,
                "file": "DESIGN.md" if not design_md_file.exists() else "design-tokens.css",
                "line": 1,
                "snippet": "Missing Reference-First design lock files",
                "message": "UI files exist without a locked `DESIGN.md` and `design-tokens.css` contract.",
                "fixHint": "Run `anti-slop lock . --archetype linear-precision` to generate `DESIGN.md` and `design-tokens.css` before styling components.",
            })
        else:
            t_txt = tokens_file.read_text(encoding="utf-8", errors="ignore")
            missing_t = [t for t in REQUIRED_TOKENS if t not in t_txt]
            if missing_t:
                findings.append({
                    "category": "missing_design_lock",
                    "severity": "warning",
                    "weight": 10,
                    "file": "design-tokens.css",
                    "line": 1,
                    "snippet": ", ".join(missing_t),
                    "message": f"`design-tokens.css` is missing required scale/surface tokens: `{', '.join(missing_t)}`.",
                    "fixHint": "Re-lock `design-tokens.css` with 4-step typography and 3-step surface variables.",
                })
            else:
                passed_checks.append("DESIGN_LOCK: `DESIGN.md` and `design-tokens.css` 4-step font & 3-step surface contract verified.")

    for fpath in files_to_scan:
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(fpath.relative_to(root_dir)).replace("\\", "/") if fpath.is_relative_to(root_dir) else fpath.name
        ext = fpath.suffix.lower()

        for lineno, line in enumerate(content.splitlines(), start=1):
            if ext in UI_EXTENSIONS:
                m_purple = PURPLE_SLOP_RE.search(line)
                if m_purple:
                    findings.append({
                        "category": "purple_radial_blob_slop",
                        "severity": "critical",
                        "weight": 12,
                        "file": rel,
                        "line": lineno,
                        "snippet": m_purple.group(0)[:80],
                        "message": f"Purple/indigo AI gradient or radial blob detected (`{m_purple.group(0)[:50]}`).",
                        "fixHint": "Replace with neutral architectural surface tokens (`var(--surface-canvas)`, `var(--surface-card)`) and 1px subtle borders.",
                    })

                m_frac = FRACTIONAL_FONT_RE.search(line)
                if m_frac:
                    findings.append({
                        "category": "fractional_font_slop",
                        "severity": "warning",
                        "weight": 6,
                        "file": rel,
                        "line": lineno,
                        "snippet": m_frac.group(0),
                        "message": f"Fractional pixel font-size rule (`{m_frac.group(0)}`) breaks typographic hierarchy.",
                        "fixHint": "Use the 4-step scale (`var(--font-caption)` 12px, `var(--font-body)` 14px, `var(--font-title)` 16px, `var(--font-display)`).",
                    })

                if VIEWPORT_ZOOM_BLOCK_RE.search(line):
                    findings.append({
                        "category": "viewport_zoom_block",
                        "severity": "critical",
                        "weight": 15,
                        "file": rel,
                        "line": lineno,
                        "snippet": line.strip()[:80],
                        "message": "Viewport meta tag blocks pinch-to-zoom (`user-scalable=no` or `maximum-scale=1.0`).",
                        "fixHint": "Remove `user-scalable=no` and `maximum-scale=1.0`; use 16px (`--font-title`) on inputs to prevent iOS auto-zoom.",
                    })

                m_emoji = EMOJI_CHROME_RE.search(line)
                if m_emoji:
                    findings.append({
                        "category": "emoji_chrome_slop",
                        "severity": "warning",
                        "weight": 8,
                        "file": rel,
                        "line": lineno,
                        "snippet": m_emoji.group(0)[:80],
                        "message": "Emoji used as UI chrome/button/heading icon.",
                        "fixHint": "Replace decorative UI emojis with monochrome 1.5px stroke SVGs or `.status-dot-sm` semantic indicators.",
                    })

                m_card = HEAVY_CARD_SLOP_RE.search(line)
                if m_card:
                    findings.append({
                        "category": "nested_card_box_slop",
                        "severity": "warning",
                        "weight": 8,
                        "file": rel,
                        "line": lineno,
                        "snippet": m_card.group(0)[:80],
                        "message": "Heavy glow/blur card styling (`shadow-2xl` + `backdrop-blur` or thick neon border).",
                        "fixHint": "Use a flat `--surface-card` container with `1px solid var(--token-border-subtle)` and max 2 nesting levels.",
                    })

            if ext in COPY_EXTENSIONS:
                for pat_re, label in KOREAN_AI_SLOP_PATTERNS:
                    m_ko = pat_re.search(line)
                    if m_ko:
                        findings.append({
                            "category": "korean_ai_copy_slop",
                            "severity": "warning",
                            "weight": 5,
                            "file": rel,
                            "line": lineno,
                            "snippet": m_ko.group(0),
                            "message": f"Korean AI translation-ese / cliché detected: {label} (`{m_ko.group(0)}`).",
                            "fixHint": "Rewrite using Korean ASD-STE100 + Toss clarity style (1 sentence 1 judgment, concrete active verb, zero cliché).",
                        })

    # Compute deterministic 0-100 Slop Score (capped at 100)
    raw_points = sum(f.get("weight", 5) for f in findings)
    slop_score = min(100, raw_points)
    if slop_score == 0:
        grade = "A+"
        verdict = "ZERO_SLOP_REFERENCE_GRADE"
    elif slop_score <= 12:
        grade = "A"
        verdict = "CLEAN_MINOR_POLISH"
    elif slop_score <= 28:
        grade = "B"
        verdict = "MODERATE_AI_TRACES"
    elif slop_score <= 50:
        grade = "C"
        verdict = "VISIBLE_AI_SLOP"
    elif slop_score <= 75:
        grade = "D"
        verdict = "HEAVY_AI_SLOP"
    else:
        grade = "F"
        verdict = "UNMISTAKABLE_TEMPLATE_SLOP"

    by_category: Dict[str, int] = {}
    for f in findings:
        cat = f.get("category", "other")
        by_category[cat] = by_category.get(cat, 0) + 1

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    result: Dict[str, Any] = {
        "ok": True,
        "targetPath": str(target),
        "scannedFiles": len(files_to_scan),
        "elapsedMs": elapsed_ms,
        "slopScore": slop_score,
        "cleanlinessScore": 100 - slop_score,
        "grade": grade,
        "verdict": verdict,
        "findingCount": len(findings),
        "byCategory": by_category,
        "passedChecks": passed_checks,
        "findings": findings[:50],
    }
    result["markdownReport"] = format_anti_slop_markdown(result)
    result["patchPromptMarkdown"] = format_anti_slop_patch_prompt(result)
    if output_format == "markdown":
        result["formatted"] = result["markdownReport"]
    elif output_format == "prompt":
        result["formatted"] = result["patchPromptMarkdown"]
    return result


def format_anti_slop_markdown(result: Dict[str, Any]) -> str:
    """Render a shareable Anti-Slop Receipt Card & violation table in Markdown."""
    score = result.get("slopScore", 0)
    clean_score = result.get("cleanlinessScore", 100)
    grade = result.get("grade", "A+")
    verdict = result.get("verdict", "")
    findings = result.get("findings", [])

    lines = [
        f"# 🎨 Anti-Slop Ship Gate Receipt (`{Path(result.get('targetPath', '.')).name}`)",
        "",
        f"- **Design Cleanliness Score**: **{clean_score} / 100** (Slop Index: `{score}/100`) · **Grade**: **`{grade}`** (`{verdict}`)",
        f"- **Scanned Files**: `{result.get('scannedFiles', 0)}` · **Elapsed**: `{result.get('elapsedMs', 0)} ms`",
        "",
    ]

    if result.get("passedChecks"):
        for p in result["passedChecks"]:
            lines.append(f"- ✅ {p}")
        lines.append("")

    if not findings:
        lines.append("🎉 **0 AI Slop violations found!** Typography, surface hierarchy, viewport accessibility, and copy pass Reference-First gates.")
    else:
        lines.extend([
            f"## Detected AI Slop Violations ({result.get('findingCount', 0)})",
            "",
            "| Category | Location | Trigger Snippet | Prescription |",
            "|---|---|---|---|",
        ])
        for f in findings[:25]:
            loc = f"`{f.get('file')}:{f.get('line')}`"
            snip = str(f.get("snippet", "")).replace("|", "\\|")
            hint = str(f.get("fixHint", "")).replace("|", "\\|")
            lines.append(f"| `{f.get('category')}` | {loc} | `{snip}` | {hint} |")

    lines.append("")
    return "\n".join(lines)


def format_anti_slop_patch_prompt(result: Dict[str, Any]) -> str:
    """Render a copy-pasteable prompt for coding agents to strip AI Slop from the target project."""
    findings = result.get("findings", [])
    if not findings:
        return "# Anti-Slop Patch Prompt\n\nNo violations detected — nothing to patch.\n"

    items = []
    for idx, f in enumerate(findings[:25], start=1):
        items.append(
            f"{idx}. `{f.get('file')}:{f.get('line')}` (`{f.get('category')}`): "
            f"Found `{f.get('snippet')}` → {f.get('fixHint')}"
        )
    body = "\n".join(items)
    return f"""# 🧹 Anti-Slop Surgical De-Slop Prompt

Paste this prompt into **Claude Code**, **Cursor**, or **Windsurf** to eliminate AI Slop:

```markdown
Please de-slop this project by resolving the following {len(findings)} deterministic UI & Copy violations.
Follow `DESIGN.md` and `design-tokens.css` strictly (4-step font scale, 3-step mineral surfaces, 1px hairline borders, zero purple radial blobs, zero UI chrome emojis, and clear Korean ASD-STE100 + Toss copy):

{body}
```
"""


def lock_design_system(
    target_path: str | Path,
    archetype: str = "linear-precision",
    project_name: Optional[str] = None,
    write_files: bool = True,
) -> Dict[str, Any]:
    """Generate Reference-First `DESIGN.md` and `design-tokens.css` in the target project root."""
    root = Path(target_path).expanduser().resolve()
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    spec = ARCHETYPE_PRESETS.get(archetype)
    if not spec:
        raise ValueError(f"Unknown archetype `{archetype}`. Choose from: {', '.join(ARCHETYPE_PRESETS.keys())}")

    proj = project_name or root.name
    today = datetime.date.today().isoformat()
    lt = spec["light"]
    dk = spec["dark"]

    bench_blocks = []
    for idx, b in enumerate(spec["benchmarks"], start=1):
        bench_blocks.append(
            f"### 1.{idx} {b['product']}\n"
            f"- **핵심 작동 원리 (Why it works)**: {b['whyItWorks']}\n"
            f"- **우리 시스템에 이식한 규칙 (Key Borrowing)**: {b['keyBorrowing']}"
        )
    bench_md = "\n\n".join(bench_blocks)
    anti_md = "\n".join(f"- ❌ **{item}**" for item in spec["antiReferences"])

    design_md = f"""# {proj} Design System (`DESIGN.md`)

- **Locked Archetype**: `{archetype}` — **{spec['name']}**
- **Last Verified**: `{today}`
- **Enforcement Gate**: `anti-slop scan .`

---

## 1. Reference Study & Benchmark Lineage (레퍼런스 스터디)

{bench_md}

---

## 2. Anti-References (명시적 금지 안티패턴 · Zero-Slop Rules)

{anti_md}

---

## 3. Token Hierarchy (`design-tokens.css`)

- **4-Step Typography**: `--font-caption` (`0.75rem` / 12px), `--font-body` (`0.875rem` / 14px), `--font-title` (`1rem` / 16px), `--font-display` (`clamp(1.5rem, 2.5vw, 2.25rem)`).
- **3-Step Surface**: `--surface-canvas` (`{lt['--surface-canvas']}`), `--surface-card` (`{lt['--surface-card']}`), `--surface-sheet` (`{lt['--surface-sheet']}`).
- **Status Indicator**: `.status-dot-sm` (6px semantic circle, zero emoji chrome).
"""

    tokens_css = f"""/* ==========================================================================
   {proj} Reference-First Design Tokens (`{archetype}`)
   Source of Truth: DESIGN.md
   Enforced by: anti-slop scan .
   ========================================================================== */

:root,
html[data-theme="light"] {{
  --font-caption: 0.75rem;
  --font-body: 0.875rem;
  --font-title: 1rem;
  --font-display: clamp(1.5rem, 2.5vw, 2.25rem);

  --text-xs: var(--font-caption);
  --text-sm: var(--font-body);
  --text-base: var(--font-title);
  --text-lg: 1.25rem;

  --surface-canvas: {lt['--surface-canvas']};
  --surface-card: {lt['--surface-card']};
  --surface-subtle: {lt['--surface-subtle']};
  --surface-sheet: {lt['--surface-sheet']};

  --token-border-subtle: {lt['--token-border-subtle']};
  --token-border-strong: {lt['--token-border-strong']};

  --text-primary: {lt['--text-primary']};
  --text-secondary: {lt['--text-secondary']};
  --text-muted: {lt['--text-muted']};
  --accent-primary: {lt['--accent-primary']};
}}

html[data-theme="dark"] {{
  --surface-canvas: {dk['--surface-canvas']};
  --surface-card: {dk['--surface-card']};
  --surface-subtle: {dk['--surface-subtle']};
  --surface-sheet: {dk['--surface-sheet']};

  --token-border-subtle: {dk['--token-border-subtle']};
  --token-border-strong: {dk['--token-border-strong']};

  --text-primary: {dk['--text-primary']};
  --text-secondary: {dk['--text-secondary']};
  --text-muted: {dk['--text-muted']};
  --accent-primary: {dk['--accent-primary']};
}}

.status-dot-sm {{
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 9999px;
  background-color: currentColor;
}}

.tabular-nums {{
  font-variant-numeric: tabular-nums;
}}
"""

    written = {}
    if write_files:
        d_path = root / "DESIGN.md"
        t_path = root / "design-tokens.css"
        d_path.write_text(design_md, encoding="utf-8")
        t_path.write_text(tokens_css, encoding="utf-8")
        written = {"designMd": str(d_path), "designTokensCss": str(t_path)}

    return {
        "ok": True,
        "archetype": archetype,
        "projectName": proj,
        "writtenFiles": written,
        "designMd": design_md,
        "designTokensCss": tokens_css,
    }


def cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="anti-slop",
        description="Anti-Slop Ship Gate — UI & Copy Slop Scanner and Reference-First Design Lock",
    )
    sub = parser.add_subparsers(dest="subcommand")

    scan_p = sub.add_parser("scan", help="Scan a project or file for UI and copy AI Slop")
    scan_p.add_argument("path", nargs="?", default=".", help="Target path to scan (default: .)")
    scan_p.add_argument("--format", default="markdown", choices=["json", "markdown", "prompt"])

    lock_p = sub.add_parser("lock", help="Generate DESIGN.md and design-tokens.css lock files")
    lock_p.add_argument("path", nargs="?", default=".", help="Target directory to lock (default: .)")
    lock_p.add_argument("--archetype", default="linear-precision", choices=list(ARCHETYPE_PRESETS.keys()))
    lock_p.add_argument("--project-name", default=None)

    args = parser.parse_args(argv)
    cmd = args.subcommand or "scan"
    try:
        if cmd == "lock":
            res = lock_design_system(
                getattr(args, "path", "."),
                archetype=getattr(args, "archetype", "linear-precision"),
                project_name=getattr(args, "project_name", None),
                write_files=True,
            )
            print(json.dumps({"ok": True, "archetype": res["archetype"], "writtenFiles": res["writtenFiles"]}, indent=2, ensure_ascii=False))
            return 0
        else:
            fmt = getattr(args, "format", "markdown")
            res = scan_anti_slop(getattr(args, "path", "."), output_format=fmt)
            if fmt == "json":
                printable = {k: v for k, v in res.items() if k not in ("markdownReport", "patchPromptMarkdown")}
                print(json.dumps(printable, indent=2, ensure_ascii=False))
            elif fmt == "prompt":
                print(res["patchPromptMarkdown"])
            else:
                print(res["markdownReport"])
            return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
