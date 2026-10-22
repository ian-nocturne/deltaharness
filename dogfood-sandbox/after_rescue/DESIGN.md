# Media Control Tower Design System (`DESIGN.md`)

- **Locked Archetype**: `linear-precision` — **Linear / Vercel Geist Precision Minimal**
- **Last Verified**: `2026-10-21`
- **Enforcement Gate**: `anti-slop scan .`

---

## 1. Reference Study & Benchmark Lineage (레퍼런스 스터디)

### 1.1 Linear
- **핵심 작동 원리 (Why it works)**: High-contrast monochrome surfaces with 1px subtle hairlines and zero decorative blobs.
- **우리 시스템에 이식한 규칙 (Key Borrowing)**: Use `--surface-canvas`, `--surface-card`, and `1px solid var(--token-border-subtle)` without nested box shadows.

### 1.2 Vercel Geist
- **핵심 작동 원리 (Why it works)**: Strict 4-step typography scale and tabular numerals for technical clarity.
- **우리 시스템에 이식한 규칙 (Key Borrowing)**: Lock font sizes to 12px/14px/16px/28px (`--font-caption` to `--font-display`) and enforce `tabular-nums`.

### 1.3 Raycast / Cursor
- **핵심 작동 원리 (Why it works)**: Monochrome vector stroke icons and 6px semantic status dots instead of noisy emojis.
- **우리 시스템에 이식한 규칙 (Key Borrowing)**: Replace all emoji chrome (`🚀`, `✨`, `⚙️`) with `.status-dot-sm` and 1.5px stroke SVGs.

---

## 2. Anti-References (명시적 금지 안티패턴 · Zero-Slop Rules)

- ❌ **Purple/indigo radial-gradient background blobs (`from-purple-500`, `via-indigo-500`)**
- ❌ **Emoji icons inside buttons, tabs, or card headers (`🚀`, `✨`, `📋`, `⚙️`)**
- ❌ **Fractional pixel font sizes (`11.5px`, `13.2px`) and 3+ nested bordered cards**
- ❌ **Robotic Korean AI translation-ese (`~에 있어서`, `극대화합니다`, `혁신적인 솔루션`)**

---

## 3. Token Hierarchy (`design-tokens.css`)

- **4-Step Typography**: `--font-caption` (`0.75rem` / 12px), `--font-body` (`0.875rem` / 14px), `--font-title` (`1rem` / 16px), `--font-display` (`clamp(1.5rem, 2.5vw, 2.25rem)`).
- **3-Step Surface**: `--surface-canvas` (`#FAFAFA`), `--surface-card` (`#FFFFFF`), `--surface-sheet` (`rgba(255, 255, 255, 0.88)`).
- **Status Indicator**: `.status-dot-sm` (6px semantic circle, zero emoji chrome).
