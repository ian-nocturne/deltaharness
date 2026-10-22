"""Anti-Slop Ship Gate (`anti-slop`) — Static UI & Korean Copy Rule Scanner and Reference-First Design Lock.

Capabilities:
1. `scan_anti_slop(target_path)`:
   - Scans `.html`, `.htm`, `.css`, `.tsx`, `.jsx`, `.js`, `.mjs`, `.vue`, `.svelte`, `.md`, `.mdx` after stripping comments.
   - Resolves CSS custom properties (`var(--token)`) across `:root`, `[data-theme="light"]`, and `[data-theme="dark"]`.
   - Computes WCAG 2.1 contrast ratios on CSS background/text color pairs (`low_contrast_text`).
   - Detects purple/indigo radial blobs (direct or via CSS variables), fractional pixel font sizes,
     attribute-order-independent viewport zoom blocks, UI chrome emojis, 3+ nested bordered cards,
     and Korean AI translation-ese copy patterns.
2. `lock_design_system(target_path, archetype="linear-precision", force=False)`:
   - Generates `DESIGN.md` and `design-tokens.css` (with explicit `--accent-text` contrast pairs).
   - Refuses to overwrite existing `DESIGN.md` / `design-tokens.css` unless `--force` is passed (which creates `.bak` backups).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .audit import EXCLUDED_DIRS

UI_EXTENSIONS = {".html", ".htm", ".css", ".tsx", ".jsx", ".js", ".mjs", ".vue", ".svelte"}
COPY_EXTENSIONS = {".md", ".mdx", ".html", ".htm", ".tsx", ".jsx", ".js", ".mjs", ".vue", ".svelte"}

# 1. Purple / Indigo / Radial Blob AI Gradient Slop
PURPLE_SLOP_RE = re.compile(
    r"(?:from-(?:purple|indigo|violet|fuchsia)-[456789]00|"
    r"via-(?:purple|indigo|violet|fuchsia)-[456789]00|"
    r"to-(?:purple|indigo|violet|fuchsia|pink)-[456789]00|"
    r"--ambient-mesh\s*:\s*radial-gradient|"
    r"(?:radial-gradient|linear-gradient)\s*\([^;{}]*(?:#8b5cf6|#6366f1|#a855f7|#7c3aed|#9333ea|rgba?\(\s*(?:139\s*,\s*92\s*,\s*246|99\s*,\s*102\s*,\s*241|168\s*,\s*85\s*,\s*247)))",
    re.IGNORECASE,
)

# 2. Fractional Pixel Font Size Slop (10.1px .. 14.9px in CSS, JSX fontSize, or Tailwind text-[...])
FRACTIONAL_FONT_RE = re.compile(
    r"(?:(?:font-size|fontSize)\s*:\s*['\"]?(?:10\.[1-9]|11\.[1-9]|12\.[1-9]|13\.[1-9]|14\.[1-9])\d*px\b|"
    r"text-\[(?:10\.[1-9]|11\.[1-9]|12\.[1-9]|13\.[1-9]|14\.[1-9])\d*px\])"
)

# 3. Viewport Zoom Block Slop (attribute-order independent on <meta ...>)
META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
VIEWPORT_NAME_RE = re.compile(r"\bname\s*=\s*[\"']viewport[\"']", re.IGNORECASE)
VIEWPORT_BLOCK_CONTENT_RE = re.compile(
    r"(?:user-scalable\s*=\s*['\"]?no\b|maximum-scale\s*=\s*1(?:\.0)?\b)",
    re.IGNORECASE,
)

# 4. Emoji Chrome Slop inside UI controls/headings
EMOJI_CHROME_RE = re.compile(
    r"(?:<(?:button|a|h1|h2|h3|h4|span|nav|header|label)\b[^>]*>[^<\n]{0,60}?"
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
            "--accent-text": "#FAFAFA",
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
            "--accent-text": "#09090B",
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
            "--accent-text": "#FFFFFF",
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
            "--accent-text": "#090D16",
        },
    },
    "toss-clarity": {
        "name": "Toss / Karrot High-Contrast Clarity",
        "benchmarks": [
            {
                "product": "Toss App & Design System (TDS)",
                "whyItWorks": "1 sentence 1 judgment copy hierarchy, bold numeric contrast, and clean squircle cards without nested borders.",
                "keyBorrowing": "Use `#F2F4F6` cool gray canvas with pure `#FFFFFF` surface cards and `#3182F6` high-trust action accent.",
            },
            {
                "product": "Karrot (당근) & KakaoBank",
                "whyItWorks": "Generous touch targets, zero decorative gradient blobs, and immediate status readability.",
                "keyBorrowing": "Enforce 4-step integer typography scale and `.status-dot-sm` semantic indicators.",
            },
            {
                "product": "Korean ASD-STE100 + Toss Writing",
                "whyItWorks": "Explains mechanism before conclusion using active Korean verbs (`해요체`) and zero AI translation-ese.",
                "keyBorrowing": "Eliminate `~에 있어서`, `극대화합니다`, and `혁신적인 솔루션` across all UI copy.",
            },
        ],
        "antiReferences": [
            "Purple/indigo radial-gradient background blobs",
            "Fractional pixel font sizes (`10.5px`, `11.5px`, `12.5px`)",
            "Blocked mobile viewport pinch-to-zoom (`user-scalable=no`)",
            "Robotic Korean AI translation-ese copy",
        ],
        "light": {
            "--surface-canvas": "#F2F4F6",
            "--surface-card": "#FFFFFF",
            "--surface-subtle": "#E5E8EB",
            "--surface-sheet": "rgba(255, 255, 255, 0.94)",
            "--token-border-subtle": "#E5E8EB",
            "--token-border-strong": "#D1D6DB",
            "--text-primary": "#191F28",
            "--text-secondary": "#4E5968",
            "--text-muted": "#8B95A1",
            "--accent-primary": "#3182F6",
            "--accent-text": "#FFFFFF",
        },
        "dark": {
            "--surface-canvas": "#101214",
            "--surface-card": "#171A1E",
            "--surface-subtle": "#20242A",
            "--surface-sheet": "rgba(23, 26, 30, 0.92)",
            "--token-border-subtle": "#262B33",
            "--token-border-strong": "#333A44",
            "--text-primary": "#F9FAFB",
            "--text-secondary": "#B0B8C1",
            "--text-muted": "#6B7684",
            "--accent-primary": "#3182F6",
            "--accent-text": "#FFFFFF",
        },
    },
    "apple-minimal": {
        "name": "Apple Human Interface / Neutral Glass & SF Hierarchy",
        "benchmarks": [
            {
                "product": "Apple Developer Documentation & macOS System Settings",
                "whyItWorks": "Neutral stone/graphite surfaces, clear typographic weight contrast, and zero neon noise.",
                "keyBorrowing": "Use `#F5F5F7` canvas, `#FFFFFF` grouped cards, and `#0071E3` semantic action links.",
            },
            {
                "product": " Keynote / Apple Newsroom",
                "whyItWorks": "Strict 4-step font hierarchy with generous line-height and tabular numbers.",
                "keyBorrowing": "Lock font scale to 12px/14px/16px/28px and avoid nested bordered boxes.",
            },
            {
                "product": "Korean ASD-STE100 + Toss Writing",
                "whyItWorks": "Concise active Korean sentences with zero machine translation artifacts.",
                "keyBorrowing": "Write direct user-facing labels without emoji clutter.",
            },
        ],
        "antiReferences": [
            "Purple/indigo radial-gradient background blobs",
            "Emoji icons inside navigation bars or CTA buttons",
            "Fractional pixel font sizes (`11.5px`, `13.5px`)",
            "3+ nested bordered cards",
        ],
        "light": {
            "--surface-canvas": "#F5F5F7",
            "--surface-card": "#FFFFFF",
            "--surface-subtle": "#E8E8ED",
            "--surface-sheet": "rgba(255, 255, 255, 0.88)",
            "--token-border-subtle": "#D2D2D7",
            "--token-border-strong": "#86868B",
            "--text-primary": "#1D1D1F",
            "--text-secondary": "#515154",
            "--text-muted": "#86868B",
            "--accent-primary": "#0071E3",
            "--accent-text": "#FFFFFF",
        },
        "dark": {
            "--surface-canvas": "#000000",
            "--surface-card": "#1C1C1E",
            "--surface-subtle": "#2C2C2E",
            "--surface-sheet": "rgba(28, 28, 30, 0.88)",
            "--token-border-subtle": "#38383A",
            "--token-border-strong": "#48484A",
            "--text-primary": "#F5F5F7",
            "--text-secondary": "#A1A1A6",
            "--text-muted": "#6E6E73",
            "--accent-primary": "#2997FF",
            "--accent-text": "#000000",
        },
    },
}


def _strip_ui_comments_preserve_lines(text: str, ext: str) -> str:
    """Strip HTML, CSS, or JS/TS comments while preserving exact line numbers."""
    # Always strip HTML comments for HTML/MD/Vue/Svelte/JSX/TSX
    out = re.sub(r"<!--[\s\S]*?-->", lambda m: "\n" * m.group(0).count("\n"), text)
    if ext in (".css", ".html", ".htm"):
        out = re.sub(r"/\*[\s\S]*?\*/", lambda m: "\n" * m.group(0).count("\n"), out)
        return out
    if ext in (".js", ".mjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte"):
        from .code_anchors import _blank_js_comments_preserve_lines
        blanked, _ = _blank_js_comments_preserve_lines(out)
        return blanked
    return out


def _parse_hex_rgb(color_str: str) -> Optional[Tuple[int, int, int]]:
    """Parse #RGB or #RRGGBB into (r, g, b) 0..255."""
    s = (color_str or "").strip()
    m = re.match(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", s)
    if not m:
        return None
    hx = m.group(1)
    if len(hx) == 3:
        return (int(hx[0] * 2, 16), int(hx[1] * 2, 16), int(hx[2] * 2, 16))
    return (int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16))


def _wcag_contrast_ratio(rgb1: Tuple[int, int, int], rgb2: Tuple[int, int, int]) -> float:
    def _rel_lum(rgb: Tuple[int, int, int]) -> float:
        vals = []
        for c in rgb:
            v = c / 255.0
            vals.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]

    l1 = _rel_lum(rgb1)
    l2 = _rel_lum(rgb2)
    bright, dark = (l1, l2) if l1 >= l2 else (l2, l1)
    return round((bright + 0.05) / (dark + 0.05), 2)


def _extract_css_Regions(text: str, ext: str) -> List[Tuple[int, str]]:
    """Return (start_char_offset, css_text) regions: full text for .css, <style> blocks for .html."""
    if ext == ".css":
        return [(0, text)]
    regions: List[Tuple[int, str]] = []
    for m in re.finditer(r"<style\b[^>]*>([\s\S]*?)</style>", text, re.IGNORECASE):
        regions.append((m.start(1), m.group(1)))
    return regions


def _collect_css_variables(clean_css_texts: List[str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Extract custom properties `--name: value` for light/root and dark scopes in O(N) time."""
    light_vars: Dict[str, str] = {}
    dark_vars: Dict[str, str] = {}
    block_re = re.compile(r"([^{}@;<>]{1,160})\{([^{}]{0,4000})\}")
    var_decl_re = re.compile(r"(--[A-Za-z0-9_-]+)\s*:\s*([^;\n]+);?")
    for txt in clean_css_texts:
        for bm in block_re.finditer(txt):
            selector = bm.group(1).strip().lower()
            body = bm.group(2)
            is_dark = "dark" in selector
            target_map = dark_vars if is_dark else light_vars
            for vm in var_decl_re.finditer(body):
                target_map[vm.group(1).strip()] = vm.group(2).strip()
    # Inherit unset dark vars from light/root vars
    merged_dark = dict(light_vars)
    merged_dark.update(dark_vars)
    return light_vars, merged_dark


def _resolve_css_vars(expr: str, var_map: Dict[str, str]) -> str:
    """Resolve `var(--token)` references up to 3 hops using `var_map`."""
    resolved = expr
    var_ref_re = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)(?:\s*,\s*([^)]+))?\)")
    for _ in range(3):
        if "var(" not in resolved:
            break
        resolved = var_ref_re.sub(
            lambda m: var_map.get(m.group(1), (m.group(2) or "").strip() or m.group(0)),
            resolved,
        )
    return resolved


def _detect_nested_bordered_cards(clean_text: str) -> List[Tuple[int, str]]:
    """Detect 3+ nested HTML/JSX elements that each carry card or explicit border styling."""
    tag_re = re.compile(
        r"<(/?)(div|section|article|aside|main)\b([^>]*?)(/?)>",
        re.IGNORECASE | re.DOTALL,
    )
    card_attr_re = re.compile(
        r"(?:(?<![a-zA-Z0-9_-])(?:[a-zA-Z0-9_]+-)?card(?![-a-zA-Z0-9_])|(?<![a-zA-Z0-9_-])border(?![-a-zA-Z0-9_])|border\s*:\s*[1-9]px)",
        re.IGNORECASE,
    )
    stack: List[Tuple[str, bool, int]] = []
    hits: List[Tuple[int, str]] = []
    reported_lines: set[int] = set()

    for m in tag_re.finditer(clean_text):
        is_close = bool(m.group(1))
        tag_name = m.group(2).lower()
        attrs = m.group(3) or ""
        is_self_close = bool(m.group(4))
        line_no = clean_text[:m.start()].count("\n") + 1

        if is_close:
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx][0] == tag_name:
                    stack.pop(idx)
                    break
            continue

        is_card = bool(card_attr_re.search(attrs))
        if not is_self_close:
            stack.append((tag_name, is_card, line_no))
            card_depth = sum(1 for _, c_flag, _ in stack if c_flag)
            if is_card and card_depth >= 3 and line_no not in reported_lines:
                reported_lines.add(line_no)
                hits.append((line_no, m.group(0).splitlines()[0][:80]))
    return hits


def scan_anti_slop(
    target_path: str | Path,
    output_format: str = "json",
    max_findings: int = 100,
) -> Dict[str, Any]:
    """Scan a directory or file for static UI and copy AI Slop rules."""
    started = time.perf_counter()
    target = Path(target_path).expanduser().resolve()
    if not target.exists():
        raise ValueError(f"Target path does not exist: {target}")

    root_dir = target if target.is_dir() else target.parent
    findings: List[Dict[str, Any]] = []
    passed_checks: List[str] = []

    files_to_scan: List[Path] = []
    if target.is_file():
        files_to_scan.append(target)
    else:
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [
                d for d in dirnames
                if d not in EXCLUDED_DIRS
                and d not in {"tests", "docs", "artifacts", "backups", "logs", "deployment", "dogfood-sandbox", "archive", "before_rescue", "after_rescue"}
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
                    "KOREAN_HUMANIZE_RULES.md",
                    "CLAUDE_AND_CURSOR_DROPIN.md",
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

    elapsed_empty = round((time.perf_counter() - started) * 1000.0, 2)
    if not files_to_scan:
        empty_res: Dict[str, Any] = {
            "ok": False,
            "exitCode": 2,
            "targetPath": str(target),
            "scannedFiles": 0,
            "elapsedMs": elapsed_empty,
            "slopScore": None,
            "cleanlinessScore": None,
            "grade": "UNVERIFIED",
            "verdict": "NO_UI_OR_COPY_FILES_FOUND",
            "findingCount": 0,
            "totalFindingCount": 0,
            "returnedFindingCount": 0,
            "byCategory": {},
            "passedChecks": [],
            "findings": [],
        }
        empty_res["markdownReport"] = format_anti_slop_markdown(empty_res)
        empty_res["patchPromptMarkdown"] = format_anti_slop_patch_prompt(empty_res)
        if output_format == "markdown":
            empty_res["formatted"] = empty_res["markdownReport"]
        elif output_format == "prompt":
            empty_res["formatted"] = empty_res["patchPromptMarkdown"]
        return empty_res

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
            raw_d_txt = design_md_file.read_text(encoding="utf-8", errors="ignore")
            clean_d_txt = _strip_ui_comments_preserve_lines(raw_d_txt, ".md").strip()
            raw_t_txt = tokens_file.read_text(encoding="utf-8", errors="ignore")
            clean_t_txt = _strip_ui_comments_preserve_lines(raw_t_txt, ".css")
            missing_t = []
            for tok in REQUIRED_TOKENS:
                if tok.startswith("--"):
                    if not re.search(rf"{re.escape(tok)}\s*:\s*[^;\s]+", clean_t_txt):
                        missing_t.append(tok)
                else:
                    if not re.search(rf"{re.escape(tok)}\s*\{{", clean_t_txt):
                        missing_t.append(tok)
            if len(clean_d_txt) < 80:
                findings.append({
                    "category": "missing_design_lock",
                    "severity": "warning",
                    "weight": 12,
                    "file": "DESIGN.md",
                    "line": 1,
                    "snippet": "Empty or stub DESIGN.md",
                    "message": "`DESIGN.md` is empty or lacks substantive Reference-First design rules.",
                    "fixHint": "Run `anti-slop lock . --force` to populate `DESIGN.md`.",
                })
            elif missing_t:
                findings.append({
                    "category": "missing_design_lock",
                    "severity": "warning",
                    "weight": 10,
                    "file": "design-tokens.css",
                    "line": 1,
                    "snippet": ", ".join(missing_t),
                    "message": f"`design-tokens.css` is missing active declarations for required tokens: `{', '.join(missing_t)}`.",
                    "fixHint": "Ensure `design-tokens.css` defines active CSS rules (not just comments) for all required tokens.",
                })
            else:
                passed_checks.append("DESIGN_LOCK: `DESIGN.md` and `design-tokens.css` 4-step font & 3-step surface contract verified.")

    # Pre-collect CSS custom properties from design-tokens.css and all scanned CSS/HTML files
    cleaned_file_cache: Dict[Path, str] = {}
    css_sources: List[str] = []
    if tokens_file.is_file():
        css_sources.append(_strip_ui_comments_preserve_lines(tokens_file.read_text(encoding="utf-8", errors="ignore"), ".css"))
    for fpath in files_to_scan:
        try:
            raw_c = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        ext = fpath.suffix.lower()
        clean_c = _strip_ui_comments_preserve_lines(raw_c, ext)
        cleaned_file_cache[fpath] = clean_c
        if ext in (".css", ".html", ".htm"):
            for _, region_txt in _extract_css_Regions(clean_c, ext):
                css_sources.append(region_txt)

    light_vars, dark_vars = _collect_css_variables(css_sources)

    for fpath in files_to_scan:
        clean_content = cleaned_file_cache.get(fpath)
        if clean_content is None:
            continue
        rel = str(fpath.relative_to(root_dir)).replace("\\", "/") if fpath.is_relative_to(root_dir) else fpath.name
        ext = fpath.suffix.lower()

        if ext in UI_EXTENSIONS:
            # Check attribute-order-independent viewport zoom block across <meta ...> tags
            for m_meta in META_TAG_RE.finditer(clean_content):
                tag_str = m_meta.group(0)
                if VIEWPORT_NAME_RE.search(tag_str) and VIEWPORT_BLOCK_CONTENT_RE.search(tag_str):
                    line_no = clean_content[:m_meta.start()].count("\n") + 1
                    findings.append({
                        "category": "viewport_zoom_block",
                        "severity": "critical",
                        "weight": 15,
                        "file": rel,
                        "line": line_no,
                        "snippet": " ".join(tag_str.split())[:80],
                        "message": "Viewport meta tag blocks pinch-to-zoom (`user-scalable=no` or `maximum-scale=1.0`).",
                        "fixHint": "Remove `user-scalable=no` and `maximum-scale=1.0`; use 16px (`--font-title`) on inputs to prevent iOS auto-zoom.",
                    })

            # Check 3+ nested bordered cards in HTML/JSX/TSX/Vue/Svelte
            if ext in {".html", ".htm", ".tsx", ".jsx", ".js", ".mjs", ".vue", ".svelte"}:
                for nest_line, nest_snip in _detect_nested_bordered_cards(clean_content):
                    findings.append({
                        "category": "nested_card_box_slop",
                        "severity": "warning",
                        "weight": 8,
                        "file": rel,
                        "line": nest_line,
                        "snippet": nest_snip,
                        "message": "3+ nested bordered card containers detected.",
                        "fixHint": "Flatten container hierarchy to at most 2 bordered levels using whitespace or 1px hairline dividers.",
                    })

            # Check CSS rule blocks for low contrast background/text pairs (e.g., white text on white button in dark mode)
            if ext in {".css", ".html", ".htm"}:
                rule_block_re = re.compile(r"([^{}@;\n<>]{1,160})\{([^{}]{0,2000})\}")
                bg_prop_re = re.compile(r"(?:^|;|\s)background(?:-color)?\s*:\s*([^;]+)", re.IGNORECASE)
                fg_prop_re = re.compile(r"(?:^|;|\s)color\s*:\s*([^;]+)", re.IGNORECASE)
                for reg_offset, reg_txt in _extract_css_Regions(clean_content, ext):
                    for rb in rule_block_re.finditer(reg_txt):
                        sel = " ".join(rb.group(1).split())
                        body = rb.group(2)
                        m_bg = bg_prop_re.search(body)
                        m_fg = fg_prop_re.search(body)
                        if m_bg and m_fg:
                            raw_bg = m_bg.group(1).strip()
                            raw_fg = m_fg.group(1).strip()
                            for mode_name, vmap in (("light", light_vars), ("dark", dark_vars)):
                                res_bg = _resolve_css_vars(raw_bg, vmap)
                                res_fg = _resolve_css_vars(raw_fg, vmap)
                                rgb_bg = _parse_hex_rgb(res_bg)
                                rgb_fg = _parse_hex_rgb(res_fg)
                                if rgb_bg and rgb_fg:
                                    ratio = _wcag_contrast_ratio(rgb_bg, rgb_fg)
                                    if ratio < 3.0:
                                        line_no = clean_content[: reg_offset + rb.start()].count("\n") + 1
                                        findings.append({
                                            "category": "low_contrast_text",
                                            "severity": "critical",
                                            "weight": 18,
                                            "file": rel,
                                            "line": line_no,
                                            "snippet": f"{sel} ({mode_name}: bg {res_bg} vs text {res_fg} = {ratio}:1)",
                                            "message": f"Unreadable contrast ratio `{ratio}:1` (< 3.0:1) in `{sel}` under `{mode_name}` theme (`{res_bg}` vs `{res_fg}`).",
                                            "fixHint": "Use paired semantic contrast tokens (e.g. `background: var(--accent-primary); color: var(--accent-text);`).",
                                        })
                                        break

        for lineno, line in enumerate(clean_content.splitlines(), start=1):
            if ext in UI_EXTENSIONS:
                expanded_line = _resolve_css_vars(line, dark_vars) if "var(" in line else line
                m_purple = PURPLE_SLOP_RE.search(line) or PURPLE_SLOP_RE.search(expanded_line)
                if m_purple:
                    findings.append({
                        "category": "purple_radial_blob_slop",
                        "severity": "critical",
                        "weight": 12,
                        "file": rel,
                        "line": lineno,
                        "snippet": line.strip()[:80],
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

    raw_points = sum(f.get("weight", 5) for f in findings)
    slop_score = min(100, raw_points)
    if slop_score == 0:
        grade = "A+"
        verdict = "STATIC_STYLE_RULES_PASSED"
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

    returned_findings = findings if max_findings <= 0 else findings[:max_findings]
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    exit_code = 0 if len(findings) == 0 else 1
    result: Dict[str, Any] = {
        "ok": len(findings) == 0,
        "exitCode": exit_code,
        "targetPath": str(target),
        "scannedFiles": len(files_to_scan),
        "elapsedMs": elapsed_ms,
        "slopScore": slop_score,
        "cleanlinessScore": 100 - slop_score,
        "grade": grade,
        "verdict": verdict,
        "findingCount": len(findings),
        "totalFindingCount": len(findings),
        "returnedFindingCount": len(returned_findings),
        "truncated": len(findings) > len(returned_findings),
        "byCategory": by_category,
        "passedChecks": passed_checks,
        "findings": returned_findings,
    }
    result["markdownReport"] = format_anti_slop_markdown(result)
    result["patchPromptMarkdown"] = format_anti_slop_patch_prompt(result)
    if output_format == "markdown":
        result["formatted"] = result["markdownReport"]
    elif output_format == "prompt":
        result["formatted"] = result["patchPromptMarkdown"]
    return result


def format_anti_slop_markdown(result: Dict[str, Any]) -> str:
    """Render a shareable Anti-Slop Static Rule Receipt & violation table in Markdown."""
    if result.get("grade") == "UNVERIFIED":
        return (
            f"# 🎨 Anti-Slop Static Rule Receipt (`{Path(result.get('targetPath', '.')).name}`)\n\n"
            "- **Status**: `UNVERIFIED` (`NO_UI_OR_COPY_FILES_FOUND`)\n"
            "- **Scanned Files**: `0`\n\n"
            "⚠️ No UI (`.html`, `.css`, `.tsx`, `.jsx`, `.js`) or copy (`.md`) files were found in the target path.\n"
        )

    score = result.get("slopScore", 0)
    clean_score = result.get("cleanlinessScore", 100)
    grade = result.get("grade", "A+")
    verdict = result.get("verdict", "")
    findings = result.get("findings", [])
    total_cnt = result.get("totalFindingCount", len(findings))

    lines = [
        f"# 🎨 Anti-Slop Static Rule Receipt (`{Path(result.get('targetPath', '.')).name}`)",
        "",
        f"- **Static Rule Score**: **{clean_score} / 100** (Slop Penalty Index: `{score}/100`) · **Rule Grade**: **`{grade}`** (`{verdict}`)",
        f"- **Scanned Files**: `{result.get('scannedFiles', 0)}` · **Total Findings**: `{total_cnt}` · **Elapsed**: `{result.get('elapsedMs', 0)} ms`",
        "- *Scope Note: Measures compliance with static anti-slop style/contrast/copy rules; does not replace human UX or visual layout testing.*",
        "",
    ]

    if result.get("passedChecks"):
        for p in result["passedChecks"]:
            lines.append(f"- ✅ {p}")
        lines.append("")

    if not findings:
        lines.append("✅ **0 static anti-slop rule violations found.**")
    else:
        shown = min(len(findings), 50)
        lines.extend([
            f"## Detected Rule Violations (showing {shown} of {total_cnt})",
            "",
            "| Category | Location | Trigger Snippet | Prescription |",
            "|---|---|---|---|",
        ])
        for f in findings[:50]:
            loc = f"`{f.get('file')}:{f.get('line')}`"
            snip = str(f.get("snippet", "")).replace("|", "\\|")
            hint = str(f.get("fixHint", "")).replace("|", "\\|")
            lines.append(f"| `{f.get('category')}` | {loc} | `{snip}` | {hint} |")
        if total_cnt > shown:
            lines.append(f"\n*({total_cnt - shown} additional findings omitted in Markdown preview; use `--format json --max-findings 0` for full list.)*")

    lines.append("")
    return "\n".join(lines)


def format_anti_slop_patch_prompt(result: Dict[str, Any]) -> str:
    """Render a copy-pasteable prompt for coding agents to strip AI Slop from the target project."""
    if result.get("grade") == "UNVERIFIED":
        return "# Anti-Slop Patch Prompt\n\nNo UI or copy files were found in the target directory.\n"
    findings = result.get("findings", [])
    total_cnt = result.get("totalFindingCount", len(findings))
    if not findings:
        return "# Anti-Slop Patch Prompt\n\nNo static rule violations detected — nothing to patch.\n"

    items = []
    for idx, f in enumerate(findings[:50], start=1):
        items.append(
            f"{idx}. `{f.get('file')}:{f.get('line')}` (`{f.get('category')}`): "
            f"Found `{f.get('snippet')}` → {f.get('fixHint')}"
        )
    trunc_note = (
        f"\n(Showing first {len(items)} of {total_cnt} total violations. Re-run `anti-slop scan .` after patching to verify any remaining items.)"
        if total_cnt > len(items)
        else ""
    )
    body = "\n".join(items)
    return f"""# 🧹 Anti-Slop Surgical De-Slop Prompt

Paste this prompt into **Claude Code**, **Cursor**, or **Windsurf** to eliminate static UI & Copy violations:

```markdown
Please resolve the following {total_cnt} static UI & Copy violations.
Follow `DESIGN.md` and `design-tokens.css` strictly (4-step font scale, 3-step mineral surfaces, paired `--accent-primary`/`--accent-text` contrast, 1px hairline borders, zero purple radial blobs, zero UI chrome emojis, and clear Korean ASD-STE100 + Toss copy):

{body}{trunc_note}
```
"""


def lock_design_system(
    target_path: str | Path,
    archetype: str = "linear-precision",
    project_name: Optional[str] = None,
    write_files: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """Generate Reference-First `DESIGN.md` and `design-tokens.css` in the target project root."""
    root = Path(target_path).expanduser().resolve()
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    spec = ARCHETYPE_PRESETS.get(archetype)
    if not spec:
        raise ValueError(f"Unknown archetype `{archetype}`. Choose from: {', '.join(ARCHETYPE_PRESETS.keys())}")

    d_path = root / "DESIGN.md"
    t_path = root / "design-tokens.css"
    if write_files and not force and (d_path.exists() or t_path.exists()):
        existing = [p.name for p in (d_path, t_path) if p.exists()]
        raise FileExistsError(
            f"Design lock file(s) already exist ({', '.join(existing)}). "
            "Pass `--force` to back up existing files (`.bak`) and overwrite."
        )

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
- **Contrast-Safe Action Pair**: `--accent-primary` (`{lt['--accent-primary']}`) paired with `--accent-text` (`{lt['--accent-text']}`).
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
  --accent-text: {lt['--accent-text']};
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
  --accent-text: {dk['--accent-text']};
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
    backups = {}
    if write_files:
        if force:
            for p in (d_path, t_path):
                if p.exists():
                    bak = p.with_suffix(p.suffix + ".bak")
                    shutil.copy2(p, bak)
                    backups[p.name] = str(bak)
        d_path.write_text(design_md, encoding="utf-8")
        t_path.write_text(tokens_css, encoding="utf-8")
        written = {"designMd": str(d_path), "designTokensCss": str(t_path)}

    return {
        "ok": True,
        "archetype": archetype,
        "projectName": proj,
        "writtenFiles": written,
        "backups": backups,
        "designMd": design_md,
        "designTokensCss": tokens_css,
    }


def cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="anti-slop",
        description="Anti-Slop Ship Gate — Static UI & Copy Rule Scanner and Reference-First Design Lock",
    )
    sub = parser.add_subparsers(dest="subcommand")

    scan_p = sub.add_parser("scan", help="Scan a project or file for static UI and copy AI Slop rules")
    scan_p.add_argument("path", nargs="?", default=".", help="Target path to scan (default: .)")
    scan_p.add_argument("--format", default="markdown", choices=["json", "markdown", "prompt"])
    scan_p.add_argument("--max-findings", type=int, default=100, help="Max findings to return (0 for unlimited)")
    scan_p.add_argument("--exit-zero", action="store_true", help="Always exit 0 even when violations are found")

    lock_p = sub.add_parser("lock", help="Generate DESIGN.md and design-tokens.css lock files")
    lock_p.add_argument("path", nargs="?", default=".", help="Target directory to lock (default: .)")
    lock_p.add_argument("--archetype", default="linear-precision", choices=list(ARCHETYPE_PRESETS.keys()))
    lock_p.add_argument("--project-name", default=None)
    lock_p.add_argument("--force", action="store_true", help="Back up (.bak) and overwrite existing DESIGN.md / design-tokens.css")

    args = parser.parse_args(argv)
    cmd = args.subcommand or "scan"
    try:
        if cmd == "lock":
            res = lock_design_system(
                getattr(args, "path", "."),
                archetype=getattr(args, "archetype", "linear-precision"),
                project_name=getattr(args, "project_name", None),
                write_files=True,
                force=bool(getattr(args, "force", False)),
            )
            print(json.dumps({"ok": True, "archetype": res["archetype"], "writtenFiles": res["writtenFiles"], "backups": res["backups"]}, indent=2, ensure_ascii=False))
            return 0
        else:
            fmt = getattr(args, "format", "markdown")
            max_f = int(getattr(args, "max_findings", 100))
            exit_zero = bool(getattr(args, "exit_zero", False))
            res = scan_anti_slop(getattr(args, "path", "."), output_format=fmt, max_findings=max_f)
            if fmt == "json":
                printable = {k: v for k, v in res.items() if k not in ("markdownReport", "patchPromptMarkdown")}
                print(json.dumps(printable, indent=2, ensure_ascii=False))
            elif fmt == "prompt":
                print(res["patchPromptMarkdown"])
            else:
                print(res["markdownReport"])
            if exit_zero:
                return 0
            if res.get("grade") == "UNVERIFIED":
                return 2
            if res.get("totalFindingCount", 0) > 0:
                return 1
            return 0
    except FileExistsError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": "FileExistsError"}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
