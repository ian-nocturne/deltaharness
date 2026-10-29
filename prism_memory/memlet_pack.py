"""Routlet × Memlet Pro Harness Pack generator (`memlet pack`).

`memlet pack [--stack all|nextjs-supabase|fastapi-postgres|swift-ios] [--output DIR] [--dry-run]` renders a
stack-specific agent harness bundle into `<DIR>/<stack>/`:

- `CLAUDE.md`: stack constitution (AST symbol anchor conventions, session delta rules, human gates).
- `AGENTS.md`: cross-vendor execution contract (Claude Code, Codex, Gemini, Cursor) and verification commands.
- `SKILL.md`: step-by-step procedural skill with pre-flight checks, execution receipt and rollback steps.
- `PITFALLS.md`: five recorded dead-ends under a `Rejected Approaches / Dead-Ends` heading, so
  `memlet handover` / `memlet import` carry them to the next model.
- `routlet-preset.json`: Routlet `performance` / `balanced` / `efficiency` model routing preset.
- `anti-slop-tokens.json`: Linear / Stripe / Toss dark & light palettes, a 4-step type scale and the
  WCAG contrast ratio of every text pair (all >= 4.5:1, checked at build time).
- `MANIFEST.json`: SHA-256 and byte size of every file above, version and quickstart.

Without `--output` (or with `--dry-run`) every file and digest is computed in memory and nothing is written.
Output is deterministic: no timestamps, host paths, network calls or LLM calls.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .memlet_wiki import MEMLET_VERSION

PACK_PRODUCT = "Routlet × Memlet Pro Harness Pack"
PACK_SCHEMA = "memlet-pack/v1"
PACK_STACKS = ("nextjs-supabase", "fastapi-postgres", "swift-ios")
PACK_CHOICES = ("all",) + PACK_STACKS
PACK_FILES = ("CLAUDE.md", "AGENTS.md", "SKILL.md", "PITFALLS.md", "routlet-preset.json", "anti-slop-tokens.json")
MANIFEST_FILE = "MANIFEST.json"
MIN_CONTRAST = 4.5

_COMMON_GATES = (
    "Spending money: paid APIs, auto-recharge, plan upgrades or new cloud resources.",
    "Secrets: creating, rotating, printing or committing keys, tokens or `.env*` values.",
    "History rewrites: `git push --force`, branch deletion, or any push to the default branch.",
    "Destructive data operations: dropping tables, bulk deletes, truncation or irreversible backfills.",
    "Major dependency upgrades and new third-party services.",
)

_COMMON_DELTA = (
    "Start every session with `memlet handover . --format markdown` and treat stale anchors as unknown, not as rules.",
    "Before editing a symbol, re-run `memlet wiki .`; an anchor that turned `stale` means re-read the code first.",
    "End every session with `memlet handover . --next-step \"...\" --rejected \"...\" --write` so dead-ends travel.",
    "Carry only verified constraints forward; approval for one action never extends to the next session or model.",
)

STACKS: Dict[str, Dict[str, Any]] = {
    "nextjs-supabase": {
        "title": "Next.js App Router + Supabase",
        "summary": "Server Components, Route Handlers and Server Actions on Supabase Postgres with Row Level Security.",
        "languages": ["TypeScript", "SQL"],
        "anchors": [
            "src/lib/supabase/server.ts::createClient",
            "src/app/api/billing/route.ts::POST",
            "src/app/(dashboard)/actions.ts::updateProfile",
            "supabase/migrations/<timestamp>_<name>.sql",
        ],
        "rules": [
            "Server code reads the user with `supabase.auth.getUser()` (verified), never with `getSession()` alone.",
            "The `service_role` key lives only in server-only modules (`import \"server-only\"`); never behind `NEXT_PUBLIC_`.",
            "Every new table ships with `ENABLE ROW LEVEL SECURITY` and explicit per-role policies in the same migration.",
            "Schema changes are new files from `supabase migration new`; applied migrations are never edited.",
            "User-specific data is rendered dynamically or cached with the user ID in the key.",
        ],
        "gates": [
            "`supabase db push` or any migration applied to a shared or production project.",
            "Changing RLS policies, auth providers, JWT settings or storage bucket visibility.",
            "Vercel / hosting environment variable or domain changes.",
        ],
        "verify": [
            "pnpm lint",
            "pnpm typecheck",
            "pnpm test",
            "supabase db lint",
            "memlet check . --format markdown",
        ],
        "preflight": [
            "`supabase status` shows the local stack running; never point a session at production credentials.",
            "`git status` is clean or the diff is understood; note the base commit in the receipt.",
            "List every table the change reads or writes and the RLS policy that guards it.",
        ],
        "steps": [
            "Write the migration with `supabase migration new <name>`, including RLS enablement and policies.",
            "Apply locally with `supabase db reset` and regenerate types (`supabase gen types typescript --local`).",
            "Implement server code in Route Handlers / Server Actions using the server client; keep keys server-only.",
            "Add a test that signs in as two different users and proves neither can read the other's rows.",
            "Run the verification commands and record the receipt.",
        ],
        "rollback": [
            "Revert the commit; for an unapplied migration, delete only the new migration file.",
            "For an applied local migration run `supabase db reset` from the previous commit.",
            "Never hand-edit a migration that already ran on a shared project; write a new down/fix migration.",
        ],
        "pitfalls": [
            ("rls-bypass-service-role",
             "Using the `service_role` key in client code or a `NEXT_PUBLIC_` variable to silence RLS errors.",
             "It bypasses every policy and ships an admin key to the browser.",
             "Fix the policy; keep `service_role` in server-only modules for trusted jobs."),
            ("table-without-rls",
             "Creating a table without `ENABLE ROW LEVEL SECURITY`, or with a `USING (true)` policy for `authenticated`.",
             "Any signed-in user can read or write every row through the public API.",
             "Enable RLS in the same migration and scope policies with `auth.uid()`."),
            ("trusting-getsession-on-server",
             "Making server-side auth decisions from `getSession()` cookie data.",
             "The cookie is not revalidated against the auth server and can be spoofed.",
             "Call `getUser()` on the server and authorize from its result."),
            ("cross-user-cache",
             "Caching per-user responses with a shared fetch cache, `unstable_cache` or static rendering.",
             "One user's data is served to another user.",
             "Render dynamically or include the user ID in the cache key and tag."),
            ("editing-applied-migrations",
             "Editing a migration file that already ran instead of adding a new one.",
             "Local, preview and production schemas drift silently.",
             "Add a new migration with `supabase migration new` and keep history append-only."),
        ],
        "reviewFocus": ["RLS policies and `auth.uid()` scoping", "server/client boundary for keys", "cache keys"],
        "escalate": ["supabase/migrations/**", "**/*.sql", "src/lib/supabase/**", "middleware.ts"],
    },
    "fastapi-postgres": {
        "title": "FastAPI + async SQLAlchemy 2.x + PostgreSQL",
        "summary": "Async FastAPI services on SQLAlchemy 2.x `AsyncSession`, asyncpg and Alembic migrations.",
        "languages": ["Python", "SQL"],
        "anchors": [
            "app/db/session.py::get_session",
            "app/api/routes/orders.py::create_order",
            "app/services/billing.py::BillingService.charge",
            "alembic/versions/<revision>_<name>.py",
        ],
        "rules": [
            "One `AsyncSession` per request via a dependency that uses `async with`; sessions are never shared across tasks.",
            "Relationships are loaded explicitly (`selectinload` / `joinedload`); default lazy loading is `raise`.",
            "`async def` endpoints never call blocking drivers or clients; use async clients or `run_in_threadpool`.",
            "Schema changes go through reviewed Alembic revisions; `create_all()` is for tests only.",
            "The session factory is `async_sessionmaker(..., expire_on_commit=False)` and transactions use `session.begin()`.",
        ],
        "gates": [
            "`alembic upgrade` / `downgrade` against any shared or production database.",
            "Changing connection pool sizes, database roles or grants.",
            "Background job schedules that touch production data.",
        ],
        "verify": [
            "ruff check .",
            "mypy app",
            "pytest -q",
            "alembic check",
            "memlet check . --format markdown",
        ],
        "preflight": [
            "Tests run against a disposable Postgres (container or `pytest` fixture), never a shared database.",
            "`alembic current` matches `alembic heads`; record the revision in the receipt.",
            "List each endpoint touched and the session dependency it uses.",
        ],
        "steps": [
            "Add or change models, then `alembic revision --autogenerate -m <name>` and review the generated operations.",
            "Load every relationship the endpoint returns explicitly in the query.",
            "Implement the endpoint with the request-scoped session dependency and `async with session.begin()`.",
            "Add a test that runs concurrent requests and asserts no session is reused across them.",
            "Run the verification commands and record the receipt.",
        ],
        "rollback": [
            "Revert the commit; for an unapplied revision delete only the new file under `alembic/versions/`.",
            "For a locally applied revision run `alembic downgrade -1` before reverting.",
            "Shared databases roll back only through a reviewed down revision behind the human gate.",
        ],
        "pitfalls": [
            ("shared-async-session",
             "Sharing one `AsyncSession` across `asyncio.gather` tasks or keeping it in a module global.",
             "Concurrent use raises \"another operation is in progress\" and leaks connections from the pool.",
             "Create one session per request or per task through `async_sessionmaker`."),
            ("implicit-lazy-load",
             "Touching a lazy relationship inside async code after the query returned.",
             "It triggers implicit IO and fails with `MissingGreenlet` or N+1 queries.",
             "Use `selectinload` / `joinedload` and set `lazy=\"raise\"` on relationships."),
            ("blocking-call-in-async-endpoint",
             "Calling `psycopg2`, `requests` or `time.sleep` inside an `async def` endpoint.",
             "The event loop stalls and every concurrent request waits.",
             "Use asyncpg / httpx async clients or `run_in_threadpool`."),
            ("create-all-instead-of-migrations",
             "Running `Base.metadata.create_all()` at startup or applying unreviewed autogenerate output.",
             "Production schema diverges from migration history and destructive diffs slip through.",
             "Use reviewed Alembic revisions; keep `create_all()` in test fixtures."),
            ("expired-after-commit",
             "Reading ORM attributes after commit with the default `expire_on_commit=True` in async code.",
             "Expired attributes reload implicitly and fail outside the greenlet context.",
             "Configure `expire_on_commit=False` and return data before the session closes."),
        ],
        "reviewFocus": ["session lifetime per request", "eager loading", "migration operations"],
        "escalate": ["alembic/versions/**", "app/db/**", "app/models/**"],
    },
    "swift-ios": {
        "title": "Swift 6 + SwiftUI (iOS)",
        "summary": "SwiftUI apps with Swift 6 strict concurrency, `@MainActor` view models and structured `Task` lifetimes.",
        "languages": ["Swift"],
        "anchors": [
            "App/Features/Feed/FeedViewModel.swift::FeedViewModel.load",
            "App/Core/Networking/APIClient.swift::APIClient.send",
            "App/Features/Feed/FeedView.swift::FeedView",
            "App.xcodeproj (build settings: SWIFT_STRICT_CONCURRENCY)",
        ],
        "rules": [
            "View models that drive UI are `@MainActor`; UI state is never mutated from a background context.",
            "Strict concurrency stays `complete`; warnings are fixed, not silenced with `@unchecked Sendable` or `nonisolated(unsafe)`.",
            "Async work starts from `.task {}` so SwiftUI cancels it with the view; long-lived tasks capture `self` weakly.",
            "Views own state with `@State` / `@StateObject`; injected objects use `@Bindable` / `@ObservedObject`.",
            "Never block the main thread on async work (`DispatchQueue.main.sync`, semaphores).",
        ],
        "gates": [
            "TestFlight / App Store Connect submission, signing identities and provisioning profiles.",
            "Entitlements, privacy manifests (`PrivacyInfo.xcprivacy`) and permission usage strings.",
            "In-app purchase products and pricing.",
        ],
        "verify": [
            "xcodebuild -scheme App -destination 'platform=iOS Simulator,name=iPhone 17' build",
            "xcodebuild -scheme App -destination 'platform=iOS Simulator,name=iPhone 17' test",
            "swiftlint --strict",
            "memlet check . --format markdown",
        ],
        "preflight": [
            "Build settings show `SWIFT_STRICT_CONCURRENCY = complete` and Swift 6 language mode.",
            "The scheme builds cleanly before the change; record the Xcode version in the receipt.",
            "List each type crossing an actor boundary and whether it is `Sendable`.",
        ],
        "steps": [
            "Mark the view model `@MainActor` and keep networking in a non-UI actor or service.",
            "Start async loading from `.task {}` and check `Task.isCancelled` before publishing results.",
            "Make values crossing actors `Sendable` structs instead of sharing reference types.",
            "Add a test that runs the async path with the Thread Sanitizer or main-actor assertions enabled.",
            "Run the verification commands and record the receipt.",
        ],
        "rollback": [
            "Revert the commit and rebuild the scheme to confirm the previous state compiles.",
            "Revert build-setting changes in the same commit as the code that needed them.",
            "Never roll back by lowering strict concurrency checking.",
        ],
        "pitfalls": [
            ("background-ui-mutation",
             "Updating `@Published` / `@Observable` UI state from a `URLSession` callback or background task.",
             "It races the main actor and produces runtime warnings, torn state or crashes.",
             "Mark the view model `@MainActor` and hop with `await MainActor.run` only where needed."),
            ("silencing-sendable-errors",
             "Using `Task.detached`, `@unchecked Sendable` or `nonisolated(unsafe)` to silence Swift 6 errors.",
             "The compiler check is gone but the data race remains.",
             "Restructure ownership: actors for shared mutable state, `Sendable` value types across boundaries."),
            ("main-thread-blocking",
             "Calling `DispatchQueue.main.sync` or waiting on a semaphore for async work from the main actor.",
             "It deadlocks or freezes the UI.",
             "Stay async end-to-end and await the result."),
            ("state-ownership-reset",
             "Owning a model with `@ObservedObject` or building `@StateObject` from changing init parameters.",
             "SwiftUI recreates or drops the model on parent updates, so state resets.",
             "Own with `@State` / `@StateObject`, inject with `@Bindable` / `@ObservedObject`."),
            ("unbounded-task-lifetime",
             "Starting long-lived `Task {}` in a view model that captures `self` strongly with no cancellation.",
             "Work keeps running after dismissal, leaks the model and publishes into dead views.",
             "Start work from `.task {}` or store and cancel the task; capture `self` weakly."),
        ],
        "reviewFocus": ["actor isolation of UI state", "Sendable boundaries", "task cancellation"],
        "escalate": ["**/*ViewModel.swift", "**/*.xcodeproj/**", "**/*.entitlements", "**/PrivacyInfo.xcprivacy"],
    },
}

# Routlet assignments. Tier labels describe planning and review depth, not measured price or quality.
_OPUS = {"host": "Claude Code", "model": "Claude Opus 5.5", "modelId": "claude-opus-5-5"}
_FABLE = {"host": "Claude Code", "model": "Fable 5.1", "modelId": "claude-fable-5-1"}
_ASTRA = {"host": "Codex", "model": "GPT-6 Astra", "modelId": None}
_SOL = {"host": "Codex", "model": "GPT-6.1 Sol", "modelId": None}
ROUTLET_OPTIONS: Dict[str, Dict[str, Any]] = {
    "performance": {
        "intent": "Highest review depth: Claude plans and reviews every diff, Codex implements.",
        "plan": _OPUS, "implement": _SOL, "review": _OPUS,
        "reviewScope": "every-diff",
    },
    "balanced": {
        "intent": "Default: Claude Opus plans, Codex implements, Fable reviews; escalation paths get Opus review.",
        "plan": _OPUS, "implement": _ASTRA, "review": _FABLE,
        "reviewScope": "every-diff",
    },
    "efficiency": {
        "intent": "Lightest loop: Fable plans and reviews, Codex implements; review only escalation paths.",
        "plan": _FABLE, "implement": _ASTRA, "review": _FABLE,
        "reviewScope": "escalation-paths-only",
    },
}

# Design tokens. Every (foreground, background) pair below is checked against MIN_CONTRAST at build time.
PALETTES: Dict[str, Dict[str, Dict[str, str]]] = {
    "linear": {
        "dark": {"bg": "#08090A", "surface": "#141516", "border": "#2A2C30", "text": "#F7F8F8",
                 "textMuted": "#9CA0A8", "accent": "#5E6AD2", "accentText": "#FFFFFF"},
        "light": {"bg": "#FFFFFF", "surface": "#F4F5F8", "border": "#DCDEE3", "text": "#1A1B1E",
                  "textMuted": "#5C6068", "accent": "#4651B8", "accentText": "#FFFFFF"},
    },
    "stripe": {
        "dark": {"bg": "#0A2540", "surface": "#11304F", "border": "#24486B", "text": "#FFFFFF",
                 "textMuted": "#ADBDCC", "accent": "#80E9FF", "accentText": "#0A2540"},
        "light": {"bg": "#FFFFFF", "surface": "#F6F9FC", "border": "#E3E8EE", "text": "#0A2540",
                  "textMuted": "#425466", "accent": "#533AFD", "accentText": "#FFFFFF"},
    },
    "toss": {
        "dark": {"bg": "#17171C", "surface": "#202027", "border": "#33333D", "text": "#FFFFFF",
                 "textMuted": "#A8A8B2", "accent": "#4D94FF", "accentText": "#101013"},
        "light": {"bg": "#FFFFFF", "surface": "#F2F4F6", "border": "#E5E8EB", "text": "#191F28",
                  "textMuted": "#4E5968", "accent": "#1B64DA", "accentText": "#FFFFFF"},
    },
}
_CONTRAST_PAIRS = (("text", "bg"), ("text", "surface"), ("textMuted", "bg"), ("textMuted", "surface"), ("accentText", "accent"))
TYPE_SCALE = (
    {"token": "caption", "sizePx": 12, "lineHeight": 1.5, "weight": 500},
    {"token": "body", "sizePx": 15, "lineHeight": 1.6, "weight": 400},
    {"token": "title", "sizePx": 20, "lineHeight": 1.4, "weight": 600},
    {"token": "display", "sizePx": 28, "lineHeight": 1.25, "weight": 700},
)
_PLATFORM_TOKENS = {
    "nextjs-supabase": {"format": "css-custom-properties", "prefix": "--ms-", "fontStack": "Inter, Pretendard, system-ui, sans-serif"},
    "fastapi-postgres": {"format": "css-custom-properties", "prefix": "--ms-", "fontStack": "Inter, Pretendard, system-ui, sans-serif",
                         "note": "For server-rendered admin pages and generated API docs themes."},
    "swift-ios": {"format": "asset-catalog-colors", "prefix": "ms", "fontStack": "SF Pro (system)",
                  "note": "Map the type scale to Dynamic Type styles (.caption, .body, .title3, .title) instead of fixed sizes."},
}
_BANNED_SLOP = (
    "purple/indigo radial-gradient hero blobs",
    "emoji used as UI chrome or icons",
    "glassmorphism cards nested 3+ levels deep",
    "fractional pixel font sizes",
    "text pairs below 4.5:1 contrast",
)


def _relative_luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 2.x contrast ratio between two `#RRGGBB` colors, rounded to two decimals."""
    a, b = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return round((a + 0.05) / (b + 0.05), 2)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _bullets(items) -> List[str]:
    return [f"- {item}" for item in items]


def _numbered(items) -> List[str]:
    return [f"{i}. {item}" for i, item in enumerate(items, start=1)]


def _render_claude(stack: str, s: Dict[str, Any]) -> str:
    lines = [
        f"# CLAUDE.md — {s['title']} Constitution",
        "",
        f"<!-- Generated by memlet pack v{MEMLET_VERSION} ({PACK_PRODUCT}, stack: {stack}). Edit freely; re-run `memlet init .` to pin anchors. -->",
        "",
        f"{s['summary']}",
        "",
        "## AST Symbol Anchor Conventions",
        "",
        "- Refer to code as `path::Symbol` (methods as `path::Type.method`) so `memlet wiki` can pin and re-verify each anchor.",
        "- After `memlet init .`, every anchor is pinned to its AST and signature hash; `stale` means the rule no longer applies as written.",
        "- Replace the example anchors below with real symbols from this repository before relying on them.",
        "",
        "```text",
        *s["anchors"],
        "```",
        "",
        "## Stack Rules",
        "",
        *_bullets(s["rules"]),
        "",
        "## Session Delta Rules",
        "",
        *_bullets(_COMMON_DELTA),
        "",
        "## Human Gates (stop and ask before doing any of these)",
        "",
        *_bullets(s["gates"]),
        *_bullets(_COMMON_GATES),
        "",
        "## Verification",
        "",
        "```sh",
        *s["verify"],
        "```",
        "",
        "Recorded dead-ends for this stack live in `PITFALLS.md`; read them before proposing a fix.",
    ]
    return "\n".join(lines) + "\n"


def _render_agents(stack: str, s: Dict[str, Any]) -> str:
    lines = [
        f"# AGENTS.md — {s['title']} Cross-Vendor Execution Contract",
        "",
        f"<!-- Generated by memlet pack v{MEMLET_VERSION} (stack: {stack}). Shared by Claude Code, Codex, Gemini and Cursor. -->",
        "",
        "## Scope",
        "",
        "- This contract applies to Claude Code, Codex, Gemini CLI and Cursor agents alike; vendor-specific files may add rules but never relax these.",
        "- `CLAUDE.md` holds the stack constitution; this file holds the execution contract every vendor follows.",
        "",
        "## Execution Contract",
        "",
        "- Read the latest handover (`memlet handover . --format markdown`) before the first edit.",
        "- Work only inside the task's file scope; list any file outside it in the receipt with a reason.",
        "- Do not cross a human gate listed in `CLAUDE.md`; stop and ask instead.",
        "- Report outcomes faithfully: failing commands are reported with their output, skipped steps are named.",
        "- On a model or vendor switch, pass a fresh handover capsule; approvals never carry over.",
        "",
        "## Vendor Notes",
        "",
        "- Claude Code: reads `CLAUDE.md` automatically; keep `AGENTS.md` referenced from it.",
        "- Codex: reads `AGENTS.md` automatically; run the verification block before reporting done.",
        "- Gemini CLI: point `GEMINI.md` at this file or copy the Execution Contract section into it.",
        "- Cursor: add a `.cursor/rules/memlet.mdc` rule that references this file.",
        "",
        "## Review Focus",
        "",
        *_bullets(s["reviewFocus"]),
        "",
        "## Verification Commands",
        "",
        "```sh",
        *s["verify"],
        "```",
        "",
        "A task is done only when every command above exits 0 and the `SKILL.md` receipt is filled in.",
    ]
    return "\n".join(lines) + "\n"


def _render_skill(stack: str, s: Dict[str, Any]) -> str:
    lines = [
        "---",
        f"name: memlet-{stack}-change",
        f"description: Verified change procedure for {s['title']} with pre-flight checks, execution receipt and rollback.",
        "---",
        "",
        f"# Skill — Ship a verified change on {s['title']}",
        "",
        "## Pre-flight Checks",
        "",
        *_numbered(s["preflight"]),
        "",
        "## Procedure",
        "",
        *_numbered(s["steps"]),
        "",
        "## Execution Receipt (required before reporting done)",
        "",
        "```text",
        "base commit:      <sha>",
        "files changed:    <list>",
        "commands run:     <command> -> exit <code>   (one line per verification command)",
        "human gates hit:  <none | gate + who approved>",
        "dead-ends found:  <none | one line each, also passed to memlet handover --rejected>",
        "```",
        "",
        "## Rollback",
        "",
        *_numbered(s["rollback"]),
    ]
    return "\n".join(lines) + "\n"


def _render_pitfalls(stack: str, s: Dict[str, Any]) -> str:
    lines = [
        f"# {s['title']} — Top 5 Pitfalls",
        "",
        f"<!-- Generated by memlet pack v{MEMLET_VERSION} (stack: {stack}). Copy to `.memlet/wiki/pitfalls.md` so"
        " `memlet handover` carries these as rejected approaches, or to `.ai-memory/pitfalls.md` for `memlet import`. -->",
        "",
        "## Rejected Approaches / Dead-Ends",
        "",
    ]
    lines += [f"- [{pid}] {rejected}" for pid, rejected, _why, _instead in s["pitfalls"]]
    lines += ["", "## Why Each Approach Fails and What to Do Instead", "", "| ID | Failure | Instead |", "| --- | --- | --- |"]
    lines += [f"| `{pid}` | {why} | {instead} |" for pid, _rejected, why, instead in s["pitfalls"]]
    lines += ["", "## Pass Them Along Explicitly", "", "```sh", "memlet handover . --next-step \"<next step>\" \\"]
    lines += [f"  --rejected \"{pid}\" \\" for pid, *_ in s["pitfalls"][:-1]]
    lines += [f"  --rejected \"{s['pitfalls'][-1][0]}\"", "```"]
    return "\n".join(lines) + "\n"


def _routlet_preset(stack: str, s: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema": "routlet-preset/v1",
        "product": PACK_PRODUCT,
        "version": MEMLET_VERSION,
        "stack": stack,
        "default": "balanced",
        "options": {name: dict(opt) for name, opt in ROUTLET_OPTIONS.items()},
        "stackTuning": {
            "reviewFocus": list(s["reviewFocus"]),
            "escalateToPerformanceWhenTouching": list(s["escalate"]),
            "verify": list(s["verify"]),
        },
        "guards": {
            "recheckHostAvailabilityBeforeDispatch": True,
            "resolveCodexModelIdAtDispatch": True,
            "paidAutoRecharge": False,
            "humanGates": list(s["gates"]) + list(_COMMON_GATES),
            "handover": "memlet handover . --write",
        },
        "note": "Tier labels describe planning and review depth, not measured cost or quality; refresh Routlet evidence before dispatch.",
    }


def _anti_slop_tokens(stack: str) -> Dict[str, Any]:
    palettes: Dict[str, Any] = {}
    for brand, modes in PALETTES.items():
        palettes[brand] = {}
        for mode, colors in modes.items():
            ratios = {f"{fg}/{bg}": contrast_ratio(colors[fg], colors[bg]) for fg, bg in _CONTRAST_PAIRS}
            low = {k: v for k, v in ratios.items() if v < MIN_CONTRAST}
            if low:
                raise ValueError(f"palette {brand}/{mode} below {MIN_CONTRAST}:1 contrast: {low}")
            palettes[brand][mode] = {"colors": dict(colors), "contrast": ratios}
    return {
        "schema": "memlet-anti-slop-tokens/v1",
        "version": MEMLET_VERSION,
        "stack": stack,
        "minContrast": MIN_CONTRAST,
        "palettes": palettes,
        "typeScale": [dict(t) for t in TYPE_SCALE],
        "spacingPx": [4, 8, 12, 16, 24, 32],
        "radiusPx": {"control": 6, "card": 10},
        "borderPx": 1,
        "platform": dict(_PLATFORM_TOKENS[stack]),
        "banned": list(_BANNED_SLOP),
        "check": "memlet anti-slop . --format markdown",
    }


def _quickstart(stack: str) -> List[str]:
    return [
        f"cp {stack}/CLAUDE.md {stack}/AGENTS.md <repo>/",
        f"mkdir -p <repo>/.claude/skills/memlet-{stack}-change && cp {stack}/SKILL.md <repo>/.claude/skills/memlet-{stack}-change/",
        f"mkdir -p <repo>/.memlet/wiki && cp {stack}/PITFALLS.md <repo>/.memlet/wiki/pitfalls.md",
        "cd <repo> && memlet init . && memlet check . --format markdown",
        "memlet handover . --next-step \"<next step>\" --write",
        f"cd {stack} && shasum -a 256 " + " ".join(PACK_FILES) + "   # compare with MANIFEST.json",
    ]


def _render_stack(stack: str) -> List[Tuple[str, bytes]]:
    s = STACKS[stack]
    files: List[Tuple[str, bytes]] = [
        ("CLAUDE.md", _render_claude(stack, s).encode("utf-8")),
        ("AGENTS.md", _render_agents(stack, s).encode("utf-8")),
        ("SKILL.md", _render_skill(stack, s).encode("utf-8")),
        ("PITFALLS.md", _render_pitfalls(stack, s).encode("utf-8")),
        ("routlet-preset.json", _json_bytes(_routlet_preset(stack, s))),
        ("anti-slop-tokens.json", _json_bytes(_anti_slop_tokens(stack))),
    ]
    manifest = {
        "schema": PACK_SCHEMA,
        "product": PACK_PRODUCT,
        "version": MEMLET_VERSION,
        "stack": stack,
        "title": s["title"],
        "algorithm": "sha256",
        "files": [{"path": name, "bytes": len(data), "sha256": _sha256(data)} for name, data in files],
        "quickstart": _quickstart(stack),
    }
    files.append((MANIFEST_FILE, _json_bytes(manifest)))
    return files


def list_packs() -> Dict[str, Any]:
    """Metadata for every available stack pack."""
    packs = {
        stack: {
            "title": s["title"],
            "summary": s["summary"],
            "languages": list(s["languages"]),
            "files": list(PACK_FILES) + [MANIFEST_FILE],
            "pitfalls": [pid for pid, *_ in s["pitfalls"]],
            "verify": list(s["verify"]),
        }
        for stack, s in STACKS.items()
    }
    return {"ok": True, "exitCode": 0, "product": PACK_PRODUCT, "version": MEMLET_VERSION,
            "stacks": list(PACK_STACKS), "packs": packs}


def _write_file(path: Path, data: bytes) -> str:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"refusing to overwrite non-regular file: {path.name}")
    if path.exists():
        if path.read_bytes() == data:
            return "unchanged"
        op = "update"
    else:
        op = "create"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return op


def _format_markdown(res: Dict[str, Any]) -> str:
    mode = "written" if res["written"] else "dry run"
    lines = [
        f"## {PACK_PRODUCT} v{res['version']} ({mode})",
        "",
        "| Stack | Files | Bytes | MANIFEST sha256 |",
        "| --- | ---: | ---: | --- |",
    ]
    for p in res["packs"]:
        lines.append(f"| `{p['stack']}` | {len(p['files'])} | {p['totalBytes']} | `{p['manifestSha256'][:16]}` |")
    lines += [
        "",
        f"- **Total**: {res['fileCount']} files, {res['totalBytes']} bytes",
        f"- **Pack digest**: `{res['manifestDigest']}`",
    ]
    if res["written"]:
        lines.append(f"- **Output**: `{res['outputDir']}/<stack>/`")
    else:
        lines.append("- Nothing was written. Pass `--output <dir>` to write the bundles.")
    return "\n".join(lines) + "\n"


def build_harness_pack(stack: str = "all", output_dir: Optional[str] = None, dry_run: bool = False,
                       output_format: str = "json") -> Dict[str, Any]:
    """Render one or all stack packs; write them to `<output_dir>/<stack>/` unless `dry_run` or no `output_dir`."""
    stack = (stack or "all").strip().lower()
    if stack not in PACK_CHOICES:
        raise ValueError(f"unknown stack: {stack} (expected one of {', '.join(PACK_CHOICES)})")
    if output_format not in ("json", "markdown"):
        raise ValueError(f"unknown format: {output_format} (expected json or markdown)")
    stacks = list(PACK_STACKS) if stack == "all" else [stack]
    write = output_dir is not None and not dry_run
    out_root = Path(output_dir).expanduser() if write else None
    if out_root is not None and out_root.exists() and not out_root.is_dir():
        raise ValueError("output path exists and is not a directory")

    packs: List[Dict[str, Any]] = []
    for name in stacks:
        files = _render_stack(name)
        entries = [{"path": f"{name}/{fname}", "bytes": len(data), "sha256": _sha256(data)} for fname, data in files]
        if out_root is not None:
            pack_dir = out_root / name
            pack_dir.mkdir(parents=True, exist_ok=True)
            for entry, (fname, data) in zip(entries, files):
                entry["op"] = _write_file(pack_dir / fname, data)
        packs.append({
            "stack": name,
            "title": STACKS[name]["title"],
            "files": entries,
            "totalBytes": sum(e["bytes"] for e in entries),
            "manifestSha256": entries[-1]["sha256"],
        })

    digest_input = "".join(f"{p['stack']}/{MANIFEST_FILE} {p['manifestSha256']}\n" for p in packs)
    res: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "product": PACK_PRODUCT,
        "version": MEMLET_VERSION,
        "stack": stack,
        "stacks": stacks,
        "dryRun": not write,
        "written": write,
        "outputDir": str(output_dir) if write else None,
        "packs": packs,
        "fileCount": sum(len(p["files"]) for p in packs),
        "totalBytes": sum(p["totalBytes"] for p in packs),
        "manifestDigest": _sha256(digest_input.encode("utf-8")),
    }
    res["formatted"] = _format_markdown(res)
    return res
