"""Nocturne Memory & Routing Inspector (`memlet ui` / `memlet inspector`).

`build_inspector_payload(repo_path)` gathers live, read-only telemetry from a repository (anchor health via
`wiki_status(dry_run=True)`, the instruction audit, the `.memlet/ledger.sqlite3` snapshots and wiki events, a
non-writing handover capsule and the reproducible refactoring benchmark) next to a bundled showcase workspace
("Next.js + FastAPI Multi-Agent Repo", 12 anchors, 3 refactor scenarios) so an uninitialized repo still has
something to explore. `render_inspector_html(payload)` turns that payload into one self-contained, dark-mode
HTML file: no CDN, no web fonts, no network calls (a CSP forbids them), every dynamic string passed through
`html.escape(..., quote=True)` and every `/Users/<name>` / `/home/<name>` path scrubbed to `<repo>`.
`export_inspector_html(repo_path, output_path)` writes it to `.memlet/inspector.html` (or `output_path`).

Nothing here writes to the inspected repository except the exported HTML file itself.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import memlet_wiki
from .audit import audit_repository

INSPECTOR_SCHEMA = "nocturne-memlet-inspector/v1"
INSPECTOR_FILE = ".memlet/inspector.html"
REPO_PLACEHOLDER = "<repo>"
EVIDENCE_AS_OF = "2026-10-29"
_STATES = memlet_wiki.ANCHOR_STATES
_MAX_LEDGER_SNAPSHOTS = 500
_MAX_LEDGER_EVENTS = 200
_MAX_AUDIT_FINDINGS = 25
_HOME_PATH_RE = re.compile(r"(?:/private)?/(?:Users|home)/[^/\s\"'<>`]+")


# ---------------------------------------------------------------------------
# Scrubbing


def _scrub_text(text: str, root: Optional[Path]) -> str:
    if root is not None:
        for variant in {str(root), os.path.realpath(str(root))}:
            if variant and variant != "/":
                text = text.replace(variant, REPO_PLACEHOLDER)
    return _HOME_PATH_RE.sub(REPO_PLACEHOLDER, text)


def _scrub(value: Any, root: Optional[Path] = None) -> Any:
    """Deep-copy `value`, replacing the repo root and any home-directory path in every string."""
    if isinstance(value, str):
        return _scrub_text(value, root)
    if isinstance(value, dict):
        return {_scrub(k, root) if isinstance(k, str) else k: _scrub(v, root) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, root) for v in value]
    return value


def _e(value: Any) -> str:
    return html.escape("—" if value is None or value == "" else str(value), quote=True)


# ---------------------------------------------------------------------------
# Live telemetry


def _read_ledger(root: Path) -> Dict[str, Any]:
    ledger = root / memlet_wiki.LEDGER_PATH
    out: Dict[str, Any] = {"present": False, "anchorSnapshots": [], "wikiEvents": [], "eventCounts": {}}
    if ledger.is_symlink() or not ledger.is_file():
        return out
    out["present"] = True
    try:
        with contextlib.closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            with contextlib.suppress(sqlite3.Error):
                out["anchorSnapshots"] = [
                    {"anchor": r[0], "path": r[1], "symbol": r[2], "status": r[3], "astHash": r[4],
                     "sigHash": r[5], "reqSigHash": r[6], "updatedAt": r[7]}
                    for r in conn.execute(
                        "SELECT anchor, path, symbol, status, ast_hash, sig_hash, req_sig_hash, updated_at"
                        " FROM anchor_snapshots ORDER BY anchor LIMIT ?", (_MAX_LEDGER_SNAPSHOTS,))
                ]
            with contextlib.suppress(sqlite3.Error):
                out["wikiEvents"] = [
                    {"id": r[0], "page": r[1], "contentSha256": (r[2] or "")[:16], "eventType": r[3], "recordedAt": r[4]}
                    for r in conn.execute(
                        "SELECT id, page, content_sha256, event_type, recorded_at FROM wiki_events"
                        " ORDER BY id DESC LIMIT ?", (_MAX_LEDGER_EVENTS,))
                ]
                out["eventCounts"] = {t: n for t, n in conn.execute(
                    "SELECT event_type, COUNT(*) FROM wiki_events GROUP BY event_type ORDER BY event_type")}
    except sqlite3.Error as exc:
        out["error"] = type(exc).__name__
    return out


def _audit_block(root: Path) -> Dict[str, Any]:
    rep = audit_repository(root)
    findings = []
    for f in (rep.get("findings") or [])[:_MAX_AUDIT_FINDINGS]:
        if isinstance(f, dict):
            findings.append({k: f.get(k) for k in ("rule", "severity", "file", "line", "message", "reference") if k in f})
    return {
        "scannedDocsCount": rep.get("scannedDocsCount", 0),
        "referencesChecked": rep.get("referencesChecked", 0),
        "errorCount": rep.get("errorCount", 0),
        "warningCount": rep.get("warningCount", 0),
        "countsByRule": dict(sorted((rep.get("countsByRule") or {}).items())),
        "findings": findings,
    }


def _handover_block(capsule: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("schema", "repoHead", "activeConstraints", "reverifyBeforeUse", "quarantinedStaleAnchors",
            "rejectedApproaches", "executionProof", "nextAction", "authorityBoundary", "omitted",
            "capsuleSignature", "tokenEstimate", "tokenBudget")
    out = {k: capsule.get(k) for k in keep}
    out["markdown"] = capsule.get("formatted") or ""
    return out


def _live_workspace(root: Path, errors: List[str]) -> Dict[str, Any]:
    ws: Dict[str, Any] = {
        "id": "live",
        "label": "Live Repository",
        "subtitle": root.name,
        "initialized": False,
        "anchorSummary": {s: 0 for s in _STATES} | {"total": 0},
        "anchors": [],
    }
    try:
        status = memlet_wiki.wiki_status(root, dry_run=True)
        ws["initialized"] = bool(status.get("initialized"))
        ws["repoHead"] = status.get("repoHead")
        ws["anchorSummary"] = status.get("anchorSummary") or ws["anchorSummary"]
        ws["anchors"] = [
            {k: a.get(k) for k in ("anchor", "status", "reason", "pinned", "currentAstHash", "currentSigHash",
                                   "currentReqSigHash", "sources")}
            for a in status.get("anchors") or []
        ]
        ws["pageSummaries"] = status.get("pageSummaries") or []
        if status.get("hint"):
            ws["hint"] = status["hint"]
    except Exception as exc:  # the console must render even when one probe fails
        errors.append(f"wiki_status: {type(exc).__name__}")
    try:
        ws["audit"] = _audit_block(root)
    except Exception as exc:
        errors.append(f"audit: {type(exc).__name__}")
        ws["audit"] = None
    ws["ledger"] = _read_ledger(root)
    try:
        ws["handover"] = _handover_block(memlet_wiki.build_handover(root, output_format="markdown"))
    except Exception as exc:
        errors.append(f"handover: {type(exc).__name__}")
        ws["handover"] = None
    return ws


# ---------------------------------------------------------------------------
# Showcase workspace (bundled, deterministic)

_DEMO_ANCHORS = (
    # (anchor, status, reason, rule)
    ("api/payments.py::refund", "active", "ast_hash_match", "Refunds: `api/payments.py::refund`."),
    ("api/auth.py::create_session", "active", "ast_hash_match", "Sessions: `api/auth.py::create_session`."),
    ("api/deps.py::get_db", "active", "ast_hash_match", "DB access: `api/deps.py::get_db`."),
    ("api/models.py::User", "active", "ast_hash_match", "User schema: `api/models.py::User`."),
    ("web/lib/supabase.ts::createClient", "active", "ast_hash_match", "Browser DB: `web/lib/supabase.ts::createClient`."),
    ("web/app/api/checkout/route.ts::POST", "active", "ast_hash_match", "Checkout: `web/app/api/checkout/route.ts::POST`."),
    ("api/router.py::route", "compatible-unverified", "optional_args_added_signature_compatible", "Dispatch every agent task through `api/router.py::route`."),
    ("web/components/CheckoutForm.tsx::CheckoutForm", "compatible-unverified", "body_changed_signature_compatible", "Render payment UI only via `web/components/CheckoutForm.tsx::CheckoutForm`."),
    ("web/middleware.ts::middleware", "compatible-unverified", "unpinned", "Auth redirects live in `web/middleware.ts::middleware`."),
    ("api/billing.py::charge", "stale", "signature_drift", "Charge customers with `api/billing.py::charge` as `charge(user_id)`."),
    ("api/legacy_auth.py::create_session", "stale", "missing_file", "Legacy sessions use `api/legacy_auth.py::create_session`."),
    ("api/refund.py::legacy_retry", "stale", "missing_symbol", "Retry failed refunds with `api/refund.py::legacy_retry`."),
)
_DEMO_SCENARIOS = (
    ("signature-break", "def charge(user_id) → def charge(account_id, currency)", "api/billing.py::charge", "stale"),
    ("optional-arg", "def route(task) → def route(task, effort=\"high\")", "api/router.py::route", "compatible-unverified"),
    ("file-move", "api/legacy_auth.py → api/auth/session.py", "api/legacy_auth.py::create_session", "stale"),
)
_DEMO_DEAD_ENDS = (
    "Retrying refunds in the webhook double-charges.",
    "Mocking the gateway hid the idempotency bug.",
)
_DEMO_EVENTS = (
    ("2026-10-20T09:12:04Z", "init", ".memlet/wiki/INDEX.md"),
    ("2026-10-20T09:12:31Z", "import", ".memlet/wiki/imported-rules.md"),
    ("2026-10-22T14:03:57Z", "user_edit", ".memlet/wiki/payments.md"),
    ("2026-10-24T11:45:10Z", "user_edit", ".memlet/wiki/frontend.md"),
    ("2026-10-27T17:20:42Z", "import", ".memlet/wiki/imported-rules.md"),
    ("2026-10-28T08:02:19Z", "user_edit", ".memlet/wiki/session.md"),
    ("2026-10-29T10:31:05Z", "handover", ".memlet/wiki/HANDOVER.md"),
)


def _demo_hash(*parts: str) -> str:
    return hashlib.sha256(":".join(("nocturne-demo",) + parts).encode("utf-8")).hexdigest()[:16]


def _demo_workspace() -> Dict[str, Any]:
    anchors = []
    for anchor, status, reason, _rule in _DEMO_ANCHORS:
        gone = reason in ("missing_file", "missing_symbol")
        anchors.append({
            "anchor": anchor,
            "status": status,
            "reason": reason,
            "pinned": reason != "unpinned",
            "currentAstHash": None if gone else _demo_hash(anchor, "ast"),
            "currentSigHash": None if gone else _demo_hash(anchor, "sig"),
            "currentReqSigHash": None if gone or anchor.endswith(".tsx::CheckoutForm") else _demo_hash(anchor, "req"),
            "sources": ["CLAUDE.md" if anchor.startswith("api/") else "AGENTS.md"],
        })
    summary = {s: sum(1 for a in anchors if a["status"] == s) for s in _STATES}
    summary["total"] = len(anchors)
    snapshots = [{"anchor": a["anchor"], "path": a["anchor"].split("::")[0], "symbol": a["anchor"].split("::")[1],
                  "status": a["status"], "astHash": a["currentAstHash"], "sigHash": a["currentSigHash"],
                  "reqSigHash": a["currentReqSigHash"], "updatedAt": "2026-10-29T10:31:05Z"} for a in anchors]
    events = [{"id": i + 1, "page": page, "contentSha256": _demo_hash(page, ts), "eventType": kind, "recordedAt": ts}
              for i, (ts, kind, page) in enumerate(_DEMO_EVENTS)][::-1]
    counts: Dict[str, int] = {}
    for ev in events:
        counts[ev["eventType"]] = counts.get(ev["eventType"], 0) + 1

    capsule: Dict[str, Any] = {
        "schema": memlet_wiki.HANDOVER_SCHEMA,
        "repoHead": _demo_hash("head")[:12],
        "activeConstraints": [{"rule": rule, "anchors": [a], "source": "CLAUDE.md:%d" % (i + 3)}
                              for i, (a, s, _r, rule) in enumerate(_DEMO_ANCHORS) if s == "active"],
        "reverifyBeforeUse": [{"anchor": a, "reason": r} for a, s, r, _ in _DEMO_ANCHORS if s == "compatible-unverified"],
        "quarantinedStaleAnchors": [{"anchor": a, "reason": r} for a, s, r, _ in _DEMO_ANCHORS if s == "stale"],
        "rejectedApproaches": [{"approach": d, "source": ".memlet/wiki/session.md:%d" % (i + 5)}
                               for i, d in enumerate(_DEMO_DEAD_ENDS)],
        "executionProof": {"modifiedFilesCount": 7, "anchorHealth": dict(summary), "staleRulesBlocked": summary["stale"],
                           "unanchoredRulesOmitted": 2},
        "nextAction": "Migrate charge() callers, then re-pin.",
        "authorityBoundary": memlet_wiki.AUTHORITY_BOUNDARY,
        "omitted": {"activeConstraints": 0, "reverifyBeforeUse": 0, "quarantinedStaleAnchors": 0, "rejectedApproaches": 0},
    }
    # Same budget rule as `memlet handover`: trim the least safety-critical items until the capsule fits.
    drop_order = ("reverifyBeforeUse", "quarantinedStaleAnchors", "activeConstraints", "rejectedApproaches")
    while True:
        signature = memlet_wiki.handover_signature(capsule)
        try:
            markdown = memlet_wiki._render_handover(capsule, signature)
        except Exception:
            markdown = "## Routlet × Memlet Handover\n\nSignature: `%s`\n" % signature
            break
        victim = next((k for k in drop_order if capsule[k]), None)
        if memlet_wiki.estimate_tokens(markdown) < memlet_wiki.HANDOVER_TOKEN_BUDGET or victim is None:
            break
        capsule[victim] = capsule[victim][:-1]
        capsule["omitted"] = dict(capsule["omitted"], **{victim: capsule["omitted"][victim] + 1})
    handover = dict(capsule, capsuleSignature=signature, markdown=markdown,
                    tokenEstimate=memlet_wiki.estimate_tokens(markdown), tokenBudget=memlet_wiki.HANDOVER_TOKEN_BUDGET)

    return {
        "id": "demo",
        "label": "Interactive Showcase",
        "subtitle": "Demo Workspace: Next.js + FastAPI Multi-Agent Repo",
        "initialized": True,
        "anchorSummary": summary,
        "anchors": anchors,
        "scenarios": [{"id": i, "title": t, "anchor": a, "outcome": o} for i, t, a, o in _DEMO_SCENARIOS],
        "audit": {"scannedDocsCount": 4, "referencesChecked": 29, "errorCount": 3, "warningCount": 3,
                  "countsByRule": {"missing_file": 1, "missing_symbol": 1, "signature_drift": 1}, "findings": []},
        "ledger": {"present": True, "anchorSnapshots": snapshots, "wikiEvents": events, "eventCounts": counts},
        "handover": handover,
    }


# ---------------------------------------------------------------------------
# Benchmark, simulator, Routlet matrix, Pro packs


def _benchmark_block(errors: List[str]) -> Optional[Dict[str, Any]]:
    try:
        rep = memlet_wiki.run_refactoring_comparison_benchmark()
    except Exception as exc:
        errors.append(f"benchmark: {type(exc).__name__}")
        return None
    return {
        "benchmark": rep.get("benchmark"),
        "fixture": rep.get("fixture"),
        "resultDigest": rep.get("resultDigest"),
        "externalLlmCalls": rep.get("externalLlmCalls", 0),
        "scenarios": rep.get("scenarios") or [],
        "modes": [{k: m.get(k) for k in ("mode", "label", "description", "staleProcedureInjectionRate",
                                         "staleProceduresInjected", "staleProceduresTotal",
                                         "optionalArgFalsePositiveRate", "handoverDeadEndPreventionRate",
                                         "handoverHazardsPrevented", "handoverHazardsTotal")}
                  for m in rep.get("modes") or []],
    }


SIMULATOR_DEFAULTS = {
    "turns": 20, "turnsMin": 1, "turnsMax": 120,
    "rules": 40, "rulesMin": 5, "rulesMax": 400,
    "tokensPerRule": 42,          # full rule text + anchor
    "indexTokensPerRule": 8,      # stage 1: one-line index entry
    "disclosedRuleShare": 0.2,    # stage 2: rules expanded on demand
    "deltaRuleShare": 0.03,       # rules whose anchors change between turns
    "deltaHeaderTokens": 24,
    "priceUsdPerMTok": [3.0, 5.0, 15.0],
}

ROUTLET_MATRIX: Dict[str, Any] = {
    "evidenceAsOf": EVIDENCE_AS_OF,
    "evidenceSource": "Routlet spec-memlet-003 dated evidence snapshot (routing inputs; not re-measured by this console)",
    "families": [
        {"id": "claude", "name": "Claude", "models": ["Claude Opus 5.5", "Claude Fable 5.1"],
         "lane": "Planning, repo-scale implementation, review", "signals": ["livebench-coding", "swe-bench-pro"]},
        {"id": "codex", "name": "Codex", "models": ["GPT-6 Astra", "GPT-6.1 Sol"],
         "lane": "Terminal-heavy implementation, long tool loops", "signals": ["terminal-bench-2"]},
        {"id": "antigravity", "name": "Antigravity", "models": ["Antigravity agent"],
         "lane": "Browser & OS-level verification, UI QA", "signals": ["osworld"]},
    ],
    "benchmarks": [
        {"id": "livebench-coding", "name": "LiveBench Coding", "score": 96.3, "display": "96.3", "family": "claude"},
        {"id": "terminal-bench-2", "name": "Terminal-Bench 2.0", "score": 81.4, "display": "81.4%", "family": "codex"},
        {"id": "swe-bench-pro", "name": "SWE-Bench Pro", "score": 66.8, "display": "66.8%", "family": "claude"},
        {"id": "osworld", "name": "OSWorld", "score": 78.2, "display": "78.2%", "family": "antigravity"},
    ],
    "topologies": [
        {"id": "performance", "label": "Performance", "summary": "Highest-effort lane per role; Memlet re-verifies every hop.",
         "lanes": [("Plan / spec", "Claude Opus 5.5", "high"), ("Implement", "Codex GPT-6 Astra", "xhigh"),
                   ("Review", "Claude Fable 5.1", "high"), ("Browser / OS QA", "Antigravity", "always"),
                   ("Memory gate", "Memlet signed capsule", "full re-verify per hop")]},
        {"id": "balanced", "label": "Balanced", "summary": "Default: strong implementer, lighter review, QA on UI diffs.",
         "lanes": [("Plan / spec", "Claude Opus 5.5", "medium"), ("Implement", "Codex GPT-6.1 Sol", "high"),
                   ("Review", "Claude Opus 5.5", "medium"), ("Browser / OS QA", "Antigravity", "on UI diff"),
                   ("Memory gate", "Memlet session delta-prompting", "delta per turn")]},
        {"id": "efficiency", "label": "Efficiency", "summary": "Fewest hops; Memlet 2-stage disclosure keeps context small.",
         "lanes": [("Plan + implement", "Codex GPT-6.1 Sol", "medium"), ("Review", "Claude Opus 5.5", "low"),
                   ("Browser / OS QA", "Antigravity", "release only"),
                   ("Memory gate", "Memlet 2-stage progressive disclosure", "index first, expand on demand")]},
    ],
}

PRO_PACKS = [
    {"id": "nextjs-supabase", "name": "Next.js + Supabase", "stack": "App Router · Route Handlers · Supabase Auth/RLS",
     "rules": 38, "anchors": 24,
     "guards": ["Route handler signature pins", "Service-role key never in client bundles", "RLS policy drift check"]},
    {"id": "fastapi-postgres", "name": "FastAPI + Postgres", "stack": "FastAPI · SQLAlchemy 2 · Alembic",
     "rules": 41, "anchors": 27,
     "guards": ["Depends() required-signature pins", "Alembic migration ↔ model drift", "Session lifecycle dead-ends"]},
    {"id": "swift-ios", "name": "Swift iOS", "stack": "SwiftUI · SwiftData · StoreKit 2",
     "rules": 33, "anchors": 19,
     "guards": ["Info.plist privacy-key checklist", "StoreKit transaction handling rules",
                "Swift anchors stay compatible-unverified until a structural extractor ships"]},
]


def build_inspector_payload(repo_path: str = ".") -> Dict[str, Any]:
    """Collect live, read-only telemetry for `repo_path` plus the bundled showcase workspace.

    All strings are scrubbed: the repo root and any home-directory path become `<repo>`."""
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"repository path not found: {root.name or root}")
    errors: List[str] = []
    live = _live_workspace(root, errors)
    demo = _demo_workspace()
    live_total = int((live.get("anchorSummary") or {}).get("total") or 0)
    payload: Dict[str, Any] = {
        "ok": True,
        "schema": INSPECTOR_SCHEMA,
        "memletVersion": memlet_wiki.MEMLET_VERSION,
        "repoName": root.name,
        "repoPath": REPO_PLACEHOLDER,
        "defaultWorkspace": "live" if live_total else "demo",
        "showcaseReason": None if live_total else
            "Live repository has 0 file::Symbol anchors (run `memlet init`); opening the showcase workspace.",
        "workspaces": {"live": live, "demo": demo},
        "benchmark": _benchmark_block(errors),
        "simulator": dict(SIMULATOR_DEFAULTS),
        "routletMatrix": ROUTLET_MATRIX,
        "proPacks": PRO_PACKS,
        "pricing": {"inspector": "$19/mo Pro", "harnessPack": "$29 Pro Pack"},
        "externalNetworkCalls": 0,
        "errors": errors,
    }
    return _scrub(payload, root)


# ---------------------------------------------------------------------------
# Rendering

_CSS = """
:root{--bg:#090A0F;--card:#12141C;--card2:#171A24;--line:rgba(255,255,255,0.08);--line2:rgba(255,255,255,0.14);
--fg:#E6E8EF;--mut:#8B90A0;--dim:#5C6170;--ok:#10B981;--warn:#F59E0B;--bad:#F43F5E;--acc:#6366F1;
--sans:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 var(--sans);
-webkit-font-smoothing:antialiased}
a{color:var(--acc)}code,.mono{font-family:var(--mono);font-size:12.5px}
.wrap{max-width:1240px;margin:0 auto;padding:28px 24px 64px}
header.top{display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:space-between;
padding-bottom:20px;border-bottom:1px solid var(--line)}
.brand{display:flex;gap:12px;align-items:center}.logo{width:30px;height:30px;border-radius:8px;
background:linear-gradient(135deg,#6366F1,#10B981);box-shadow:0 0 0 1px var(--line2) inset}
.brand h1{font-size:16px;margin:0;letter-spacing:-.01em}.brand p{margin:0;color:var(--mut);font-size:12.5px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border:1px solid var(--line);border-radius:999px;
color:var(--mut);font-size:12px;font-family:var(--mono);white-space:nowrap}.pill.acc{color:#C7C9FF;border-color:rgba(99,102,241,.45);
background:rgba(99,102,241,.10)}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:18px 0 6px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;padding:3px;background:var(--card)}
.seg button{all:unset;cursor:pointer;padding:6px 12px;border-radius:7px;color:var(--mut);font-size:13px}
.seg button[aria-pressed=true]{background:var(--card2);color:var(--fg);box-shadow:0 0 0 1px var(--line2) inset}
.seg button:focus-visible,.tabs button:focus-visible,.btn:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.notice{margin:10px 0 0;padding:10px 14px;border:1px solid rgba(99,102,241,.35);background:rgba(99,102,241,.08);
border-radius:10px;color:#C7C9FF;font-size:13px}
.tabs{display:flex;gap:4px;margin:22px 0 18px;border-bottom:1px solid var(--line);overflow-x:auto}
.tabs button{all:unset;cursor:pointer;padding:10px 14px;color:var(--mut);font-size:13.5px;border-bottom:2px solid transparent;
white-space:nowrap}.tabs button[aria-selected=true]{color:var(--fg);border-bottom-color:var(--acc)}
.grid{display:grid;gap:16px;grid-template-columns:minmax(0,1fr)}.grid>*{min-width:0}.g2{grid-template-columns:repeat(2,minmax(0,1fr))}.g3{grid-template-columns:repeat(3,minmax(0,1fr))}
.g4{grid-template-columns:repeat(auto-fit,minmax(110px,1fr))}.gh{grid-template-columns:300px minmax(0,1fr)}
@media (max-width:900px){.g2,.g3,.gh{grid-template-columns:minmax(0,1fr)}}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
.card h3{margin:0 0 4px;font-size:13.5px;letter-spacing:-.005em}.sub{color:var(--mut);font-size:12.5px;margin:0 0 14px}
.kpi{font-size:26px;font-weight:600;letter-spacing:-.02em;font-variant-numeric:tabular-nums}.kpi small{font-size:12px;color:var(--mut);font-weight:400}
.lbl{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.06em}
.ringbox{display:flex;gap:18px;align-items:center}.legend{display:grid;gap:8px;font-size:13px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;vertical-align:middle}
.d-active{background:var(--ok)}.d-compatible-unverified{background:var(--warn)}.d-stale{background:var(--bad)}
.stack{display:flex;height:8px;border-radius:99px;overflow:hidden;background:rgba(255,255,255,.05);margin-top:14px}
.stack span{display:block;height:100%}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.05em}
tbody tr:hover{background:rgba(255,255,255,.02)}.tablewrap{overflow-x:auto}
.badge{display:inline-flex;align-items:center;gap:6px;padding:2px 8px;border-radius:999px;font-size:12px;font-family:var(--mono);
white-space:nowrap;border:1px solid}
.b-active{color:var(--ok);border-color:rgba(16,185,129,.35);background:rgba(16,185,129,.08)}
.b-compatible-unverified{color:var(--warn);border-color:rgba(245,158,11,.35);background:rgba(245,158,11,.08)}
.b-stale{color:var(--bad);border-color:rgba(244,63,94,.35);background:rgba(244,63,94,.08)}
.b-neutral{color:#C7C9FF;border-color:rgba(99,102,241,.35);background:rgba(99,102,241,.08)}
.hash{color:var(--mut)}.empty{color:var(--dim);text-align:center;padding:26px}
.timeline{list-style:none;margin:0;padding:0}.timeline li{display:grid;grid-template-columns:14px 1fr auto;gap:10px;padding:8px 0;
border-bottom:1px solid var(--line);align-items:baseline}.timeline li:last-child{border-bottom:0}
.ev-init{background:var(--acc)}.ev-import{background:#22D3EE}.ev-user_edit{background:var(--warn)}.ev-handover{background:#A78BFA}
.ev-other{background:var(--dim)}
.bars{display:grid;gap:14px}.bar .row{display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px}
.track{height:10px;border-radius:99px;background:rgba(255,255,255,.05);overflow:hidden}.fill{height:100%;border-radius:99px}
input[type=range]{width:100%;accent-color:var(--acc)}select{background:var(--card2);color:var(--fg);border:1px solid var(--line2);
border-radius:8px;padding:6px 8px;font:inherit}
.formula{font-family:var(--mono);font-size:12px;color:var(--mut);background:rgba(255,255,255,.02);border:1px solid var(--line);
border-radius:10px;padding:10px 12px;white-space:pre-wrap;margin:14px 0 0}
ul.items{list-style:none;margin:0;padding:0;display:grid;gap:8px}ul.items li{padding:10px 12px;border:1px solid var(--line);
border-radius:10px;background:var(--card2);font-size:13px}ul.items li .src{display:block;color:var(--dim);font-family:var(--mono);font-size:11.5px;margin-top:3px}
.blocked li{border-color:rgba(244,63,94,.25)}.verified li{border-color:rgba(16,185,129,.22)}.deadend li{border-color:rgba(245,158,11,.22)}
.btn{all:unset;cursor:pointer;display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:9px;font-size:13px;
background:var(--acc);color:#fff;font-weight:500}.btn.ghost{background:transparent;border:1px solid var(--line2);color:var(--fg)}
textarea.md{width:100%;min-height:200px;margin-top:12px;background:#0C0D13;color:var(--mut);border:1px solid var(--line);
border-radius:10px;padding:12px;font:12px/1.5 var(--mono);resize:vertical}
.sig{font-family:var(--mono);font-size:12px;word-break:break-all;color:#C7C9FF}
.price{font-size:13px;color:var(--mut)}.price b{color:var(--fg);font-size:20px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);color:var(--dim);font-size:12px;display:flex;
flex-wrap:wrap;gap:12px;justify-content:space-between}
[hidden]{display:none!important}
body[data-ws=live] [data-ws=demo],body[data-ws=demo] [data-ws=live]{display:none!important}
"""

_JS = r"""
(function(){
  var body=document.body;
  function all(sel,root){return Array.prototype.slice.call((root||document).querySelectorAll(sel));}
  function press(group,btn){all('button',group).forEach(function(b){b.setAttribute('aria-pressed',b===btn?'true':'false');});}
  function applyFilter(){
    var f=body.getAttribute('data-filter')||'all';
    all('tr[data-status]').forEach(function(tr){tr.hidden=!(f==='all'||tr.getAttribute('data-status')===f);});
    all('[data-anchor-table]').forEach(function(t){
      var shown=all('tr[data-status]',t).filter(function(tr){return !tr.hidden;}).length;
      var e=t.querySelector('[data-empty-filter]');if(e){e.hidden=shown!==0||all('tr[data-status]',t).length===0;}
      var c=t.querySelector('[data-shown]');if(c){c.textContent=String(shown);}
    });
  }
  all('[data-ws-switch] button').forEach(function(b){b.addEventListener('click',function(){
    body.setAttribute('data-ws',b.getAttribute('data-value'));press(b.parentNode,b);applyFilter();});});
  all('[data-filter-switch] button').forEach(function(b){b.addEventListener('click',function(){
    body.setAttribute('data-filter',b.getAttribute('data-value'));press(b.parentNode,b);applyFilter();});});
  var tabs=all('[role=tab]');
  function selectTab(t){tabs.forEach(function(x){var on=x===t;x.setAttribute('aria-selected',on?'true':'false');
    x.tabIndex=on?0:-1;document.getElementById(x.getAttribute('aria-controls')).hidden=!on;});}
  tabs.forEach(function(t,i){t.addEventListener('click',function(){selectTab(t);});
    t.addEventListener('keydown',function(ev){var d=ev.key==='ArrowRight'?1:ev.key==='ArrowLeft'?-1:0;
      if(d){ev.preventDefault();var n=tabs[(i+d+tabs.length)%tabs.length];selectTab(n);n.focus();}});});
  all('[data-topology-switch] button').forEach(function(b){b.addEventListener('click',function(){
    press(b.parentNode,b);var v=b.getAttribute('data-value');
    all('[data-topology]').forEach(function(p){p.hidden=p.getAttribute('data-topology')!==v;});});});
  all('[data-copy]').forEach(function(b){b.addEventListener('click',function(){
    var ta=document.getElementById(b.getAttribute('data-copy'));var txt=ta.value;var old=b.textContent;
    function done(ok){b.textContent=ok?'Copied ✓':'Select & copy manually';setTimeout(function(){b.textContent=old;},1600);}
    if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(txt).then(function(){done(true);},function(){ta.select();done(document.execCommand('copy'));});}
    else{ta.focus();ta.select();var ok=false;try{ok=document.execCommand('copy');}catch(e){}done(ok);}
  });});
  var sim=document.getElementById('sim');
  if(sim){
    var cfg=JSON.parse(sim.getAttribute('data-config'));
    var turns=document.getElementById('sim-turns'),rules=document.getElementById('sim-rules'),price=document.getElementById('sim-price');
    function fmt(n){return Math.round(n).toLocaleString('en-US');}
    function out(id,v){document.getElementById(id).textContent=v;}
    function calc(){
      var T=+turns.value,R=+rules.value,P=+price.value,k=cfg.tokensPerRule;
      var base=T*R*k;
      var mem=R*cfg.indexTokensPerRule+Math.ceil(R*cfg.disclosedRuleShare)*k+(T-1)*(cfg.deltaHeaderTokens+Math.max(1,Math.ceil(R*cfg.deltaRuleShare))*k);
      mem=Math.min(mem,base);var saved=base-mem;
      out('sim-turns-v',String(T));out('sim-rules-v',String(R));
      out('sim-base',fmt(base));out('sim-mem',fmt(mem));out('sim-saved',fmt(saved));
      out('sim-pct',(base?saved/base*100:0).toFixed(1)+'%');
      out('sim-usd','$'+(saved*P/1e6).toFixed(4));out('sim-usd-k','$'+(saved*P/1e6*1000).toFixed(2));
      document.getElementById('sim-bar-mem').style.width=(base?mem/base*100:0).toFixed(1)+'%';
    }
    [turns,rules,price].forEach(function(el){el.addEventListener('input',calc);});calc();
  }
  applyFilter();
})();
"""


def _status(value: Any) -> str:
    return value if value in _STATES else "compatible-unverified"


def _badge(status: str, text: Optional[str] = None) -> str:
    icon = {"active": "●", "compatible-unverified": "◐", "stale": "○"}.get(status, "●")
    return f'<span class="badge b-{_e(status)}">{icon} {_e(text or status)}</span>'


def _pct(rate: Any) -> float:
    try:
        return max(0.0, min(100.0, float(rate) * 100))
    except (TypeError, ValueError):
        return 0.0


def _health_card(ws: Dict[str, Any]) -> str:
    s = ws.get("anchorSummary") or {}
    total = int(s.get("total") or 0)
    r = 52
    circ = 2 * math.pi * r
    arcs, offset = [], 0.0
    colors = {"active": "#10B981", "compatible-unverified": "#F59E0B", "stale": "#F43F5E"}
    for st in _STATES:
        n = int(s.get(st) or 0)
        if total and n:
            length = circ * n / total
            arcs.append(f'<circle cx="64" cy="64" r="{r}" fill="none" stroke="{colors[st]}" stroke-width="12" '
                        f'stroke-dasharray="{length:.2f} {circ:.2f}" stroke-dashoffset="{-offset:.2f}" '
                        f'transform="rotate(-90 64 64)"/>')
            offset += length
    healthy = (int(s.get("active") or 0) / total * 100) if total else 0.0
    ring = (f'<svg width="128" height="128" viewBox="0 0 128 128" role="img" aria-label="anchor health">'
            f'<circle cx="64" cy="64" r="{r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="12"/>'
            + "".join(arcs) +
            f'<text x="64" y="62" text-anchor="middle" fill="#E6E8EF" font-size="22" font-weight="600" '
            f'font-family="Inter,system-ui,sans-serif">{healthy:.0f}%</text>'
            f'<text x="64" y="80" text-anchor="middle" fill="#8B90A0" font-size="10" '
            f'font-family="Inter,system-ui,sans-serif">ACTIVE</text></svg>')
    legend = "".join(f'<div><span class="dot d-{st}"></span>{_e(st)} · <b>{int(s.get(st) or 0)}</b></div>' for st in _STATES)
    stack = "".join(f'<span style="width:{(int(s.get(st) or 0) / total * 100) if total else 0:.1f}%;background:{colors[st]}"></span>'
                    for st in _STATES)
    hint = f'<p class="sub" style="margin:12px 0 0">{_e(ws.get("hint"))}</p>' if ws.get("hint") else ""
    return (f'<div class="card"><h3>AST anchor health</h3><p class="sub">{_e(ws.get("subtitle"))} · {total} anchors</p>'
            f'<div class="ringbox">{ring}<div class="legend">{legend}</div></div><div class="stack">{stack}</div>{hint}</div>')


def _audit_card(ws: Dict[str, Any]) -> str:
    a = ws.get("audit")
    if not a:
        return '<div class="card"><h3>Instruction audit</h3><p class="sub">Audit unavailable for this workspace.</p></div>'
    kpis = [("Docs scanned", a.get("scannedDocsCount")), ("References checked", a.get("referencesChecked")),
            ("Errors", a.get("errorCount")), ("Warnings", a.get("warningCount"))]
    cells = "".join(f'<div><div class="lbl">{_e(k)}</div><div class="kpi">{_e(v if v is not None else 0)}</div></div>' for k, v in kpis)
    rules = a.get("countsByRule") or {}
    chips = " ".join(f'<span class="pill">{_e(k)} · {_e(v)}</span>' for k, v in rules.items()) or '<span class="pill">no findings</span>'
    led = ws.get("ledger") or {}
    led_line = (f'SQLite ledger: {len(led.get("anchorSnapshots") or [])} anchor snapshots · '
                f'{sum((led.get("eventCounts") or {}).values())} wiki events') if led.get("present") else \
        "SQLite ledger: not present (.memlet/ledger.sqlite3 is created by `memlet init`)"
    return (f'<div class="card"><h3>Instruction audit &amp; ledger</h3><p class="sub">{_e(led_line)}</p>'
            f'<div class="grid g4">{cells}</div><div style="margin-top:14px;display:flex;flex-wrap:wrap;gap:6px">{chips}</div></div>')


def _anchor_table(ws: Dict[str, Any]) -> str:
    rows = []
    for a in ws.get("anchors") or []:
        st = _status(a.get("status"))
        rows.append(
            f'<tr data-status="{_e(st)}"><td>{_badge(st)}</td><td class="mono">{_e(a.get("anchor"))}</td>'
            f'<td class="mono hash">{_e(a.get("currentAstHash"))}</td><td class="mono hash">{_e(a.get("currentSigHash"))}</td>'
            f'<td class="mono hash">{_e(a.get("currentReqSigHash"))}</td><td class="mono">{_e(a.get("reason"))}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="6" class="empty">No <code>file.py::Symbol</code> anchors found. '
                    'Run <code>memlet init</code>, or switch to the Interactive Showcase.</td></tr>')
    total = len(ws.get("anchors") or [])
    return (f'<div class="card" data-anchor-table><h3>Anchors</h3><p class="sub">Showing <span data-shown>{total}</span> of {total} · '
            f'hashes are 16-hex prefixes of the current AST / full-signature / required-signature fingerprints</p>'
            f'<div class="tablewrap"><table><thead><tr><th>Status</th><th>Anchor</th><th>currentAstHash</th>'
            f'<th>currentSigHash</th><th>currentReqSigHash</th><th>Reason</th></tr></thead><tbody>{"".join(rows)}'
            f'<tr data-empty-filter hidden><td colspan="6" class="empty">No anchors match this status filter.</td></tr>'
            f'</tbody></table></div></div>')


def _timeline_card(ws: Dict[str, Any]) -> str:
    led = ws.get("ledger") or {}
    events = led.get("wikiEvents") or []
    known = ("init", "import", "user_edit", "handover")
    items = "".join(
        f'<li><span class="dot ev-{ev.get("eventType") if ev.get("eventType") in known else "other"}"></span>'
        f'<div><span class="badge b-neutral">{_e(ev.get("eventType"))}</span> <span class="mono">{_e(ev.get("page"))}</span>'
        f'<span class="mono hash" style="display:block;font-size:11.5px">sha256 {_e(ev.get("contentSha256"))}…</span></div>'
        f'<span class="mono hash">{_e(ev.get("recordedAt"))}</span></li>' for ev in events)
    if not items:
        items = '<li style="display:block" class="empty">No wiki_events recorded yet.</li>'
    counts = " ".join(f'<span class="pill">{_e(k)} · {_e(v)}</span>' for k, v in (led.get("eventCounts") or {}).items())
    return (f'<div class="card"><h3>Bi-directional wiki ledger · <span class="mono">wiki_events</span></h3>'
            f'<p class="sub">Human edits to <code>.memlet/wiki/*.md</code> are hashed and recorded; newest first. {counts}</p>'
            f'<ul class="timeline">{items}</ul></div>')


def _tab_health(payload: Dict[str, Any]) -> str:
    parts = []
    for wid, ws in payload["workspaces"].items():
        extra = ""
        if ws.get("scenarios"):
            sc = "".join(f'<li>{_badge(_status(s.get("outcome")))} <b>{_e(s.get("id"))}</b> · {_e(s.get("title"))}'
                         f'<span class="src">{_e(s.get("anchor"))}</span></li>' for s in ws["scenarios"])
            extra = f'<div class="card"><h3>3 refactor scenarios in this workspace</h3><p class="sub">How each refactor is classified.</p><ul class="items">{sc}</ul></div>'
        parts.append(f'<div data-ws="{_e(wid)}" class="grid"><div class="grid g2">{_health_card(ws)}{_audit_card(ws)}</div>'
                     f'{_anchor_table(ws)}<div class="grid g2">{_timeline_card(ws)}{extra or _snapshot_card(ws)}</div></div>')
    return "".join(parts)


def _snapshot_card(ws: Dict[str, Any]) -> str:
    snaps = (ws.get("ledger") or {}).get("anchorSnapshots") or []
    rows = "".join(f'<tr><td>{_badge(_status(s.get("status")))}</td><td class="mono">{_e(s.get("anchor"))}</td>'
                   f'<td class="mono hash">{_e(s.get("updatedAt"))}</td></tr>' for s in snaps[:40])
    if not rows:
        rows = '<tr><td colspan="3" class="empty">No anchor_snapshots rows.</td></tr>'
    return (f'<div class="card"><h3>Ledger · <span class="mono">anchor_snapshots</span></h3>'
            f'<p class="sub">Latest observed state per anchor ({len(snaps)} rows).</p><div class="tablewrap"><table>'
            f'<thead><tr><th>Status</th><th>Anchor</th><th>Updated</th></tr></thead><tbody>{rows}</tbody></table></div></div>')


def _tab_benchmark(payload: Dict[str, Any]) -> str:
    b = payload.get("benchmark")
    if b:
        palette = {"raw-markdown": "#F43F5E", "unverified-wiki": "#F59E0B", "memlet": "#10B981"}
        bars = []
        for m in b.get("modes") or []:
            stale = _pct(m.get("staleProcedureInjectionRate"))
            block = _pct(m.get("handoverDeadEndPreventionRate"))
            color = palette.get(m.get("mode"), "#6366F1")
            bars.append(
                f'<div class="bar"><div class="row"><b>{_e(m.get("label"))}</b><span class="mono">{stale:.1f}% stale injection · '
                f'{block:.1f}% hazard block</span></div><div class="track"><div class="fill" style="width:{stale:.1f}%;background:{color}"></div></div>'
                f'<div class="track" style="margin-top:6px;height:6px"><div class="fill" style="width:{block:.1f}%;background:#6366F1"></div></div>'
                f'<p class="sub" style="margin:6px 0 0">{_e(m.get("description"))}</p></div>')
        scen = "".join(f'<li><b>{_e(s.get("id"))}</b> · {_e(s.get("title"))}</li>' for s in b.get("scenarios") or [])
        bench = (f'<div class="card"><h3>Refactoring comparison benchmark</h3><p class="sub"><span class="mono">{_e(b.get("benchmark"))}</span> · '
                 f'{_e(b.get("fixture"))} · external LLM calls: {_e(b.get("externalLlmCalls"))}</p><div class="bars">{"".join(bars)}</div>'
                 f'<p class="sub" style="margin:14px 0 0">Top bar: stale procedures injected (lower is better). Thin bar: handover dead-end hazards blocked (higher is better).</p>'
                 f'<p class="sig" style="margin-top:6px">{_e(b.get("resultDigest"))}</p></div>'
                 f'<div class="card"><h3>Scenarios (replayed live in a temp dir)</h3><ul class="items">{scen}</ul></div>')
    else:
        bench = '<div class="card"><h3>Refactoring comparison benchmark</h3><p class="sub">Benchmark unavailable.</p></div>'
    sim = payload.get("simulator") or SIMULATOR_DEFAULTS
    cfg = json.dumps({k: sim[k] for k in ("tokensPerRule", "indexTokensPerRule", "disclosedRuleShare",
                                          "deltaRuleShare", "deltaHeaderTokens")}, sort_keys=True)
    prices = "".join(f'<option value="{float(p):.2f}"{" selected" if i == 0 else ""}>${float(p):.2f} / 1M input tokens</option>'
                     for i, p in enumerate(sim.get("priceUsdPerMTok") or [3.0]))
    simulator = (
        f'<div class="card" id="sim" data-config="{_e(cfg)}"><h3>Delta token simulator</h3>'
        f'<p class="sub">Session Delta-Prompting + 2-Stage Progressive Disclosure vs. re-injecting every rule every turn. '
        f'Simulation with the assumptions below — not a measured result.</p>'
        f'<div class="grid g2"><label><div class="row" style="display:flex;justify-content:space-between"><span>Turns per session</span>'
        f'<span class="mono" id="sim-turns-v"></span></div><input id="sim-turns" type="range" min="{int(sim["turnsMin"])}" '
        f'max="{int(sim["turnsMax"])}" value="{int(sim["turns"])}"></label>'
        f'<label><div class="row" style="display:flex;justify-content:space-between"><span>Repo rule count</span>'
        f'<span class="mono" id="sim-rules-v"></span></div><input id="sim-rules" type="range" min="{int(sim["rulesMin"])}" '
        f'max="{int(sim["rulesMax"])}" value="{int(sim["rules"])}"></label></div>'
        f'<div style="margin:12px 0"><select id="sim-price" aria-label="input token price">{prices}</select></div>'
        f'<div class="grid g3"><div><div class="lbl">Raw re-injection</div><div class="kpi mono" id="sim-base"></div></div>'
        f'<div><div class="lbl">Memlet</div><div class="kpi mono" style="color:#10B981" id="sim-mem"></div></div>'
        f'<div><div class="lbl">Saved</div><div class="kpi mono" id="sim-saved"></div><small class="mono" id="sim-pct"></small></div></div>'
        f'<div class="track" style="margin-top:14px"><div class="fill" id="sim-bar-mem" style="width:0;background:#10B981"></div></div>'
        f'<div class="grid g2" style="margin-top:14px"><div><div class="lbl">Saved per session</div><div class="kpi mono" id="sim-usd"></div></div>'
        f'<div><div class="lbl">Saved per 1,000 sessions</div><div class="kpi mono" id="sim-usd-k"></div></div></div>'
        f'<div class="formula">raw    = turns × rules × {int(sim["tokensPerRule"])}\n'
        f'memlet = rules × {int(sim["indexTokensPerRule"])}   (stage 1 index)\n'
        f'       + ⌈rules × {sim["disclosedRuleShare"]}⌉ × {int(sim["tokensPerRule"])}   (stage 2 on-demand)\n'
        f'       + (turns − 1) × ({int(sim["deltaHeaderTokens"])} + max(1, ⌈rules × {sim["deltaRuleShare"]}⌉) × {int(sim["tokensPerRule"])})   (delta)</div></div>')
    return f'<div class="grid g2"><div class="grid">{bench}</div>{simulator}</div>'


def _items(entries: List[Dict[str, Any]], main: str, sub: str, empty: str, cls: str) -> str:
    if not entries:
        return f'<ul class="items {cls}"><li class="empty">{_e(empty)}</li></ul>'
    lis = []
    for it in entries:
        extra = it.get(sub)
        if isinstance(extra, list):
            extra = ", ".join(str(x) for x in extra)
        lis.append(f'<li>{_e(it.get(main))}<span class="src">{_e(extra)}</span></li>')
    return f'<ul class="items {cls}">{"".join(lis)}</ul>'


def _omitted_note(h: Dict[str, Any], key: str, default: str) -> str:
    n = int((h.get("omitted") or {}).get(key) or 0)
    return f"{n} item(s) omitted to keep the capsule under {h.get('tokenBudget')} tokens — see the anchors table." if n else default


def _tab_handover(payload: Dict[str, Any]) -> str:
    parts = []
    for wid, ws in payload["workspaces"].items():
        h = ws.get("handover")
        if not h:
            parts.append(f'<div data-ws="{_e(wid)}" class="card"><h3>Handover capsule</h3><p class="sub">Unavailable.</p></div>')
            continue
        tid = f"capsule-md-{wid}"
        proof = h.get("executionProof") or {}
        health = proof.get("anchorHealth") or {}
        parts.append(
            f'<div data-ws="{_e(wid)}" class="grid">'
            f'<div class="card"><div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between">'
            f'<div><h3>Cross-vendor handover capsule · Routlet × Memlet</h3><p class="sub" style="margin:0">'
            f'<span class="mono">{_e(h.get("schema"))}</span> · head <span class="mono">{_e((h.get("repoHead") or "")[:12])}</span> · '
            f'{_e(h.get("tokenEstimate"))} / {_e(h.get("tokenBudget"))} tokens</p></div>'
            f'<span class="badge b-neutral">🔒 Authority boundary: {_e(h.get("authorityBoundary"))}</span></div>'
            f'<p class="sig" style="margin:12px 0 0">{_e(h.get("capsuleSignature"))}</p>'
            f'<div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px">'
            + "".join(f'<span class="pill">{_e(k)} · {_e(health.get(k, 0))}</span>' for k in _STATES) +
            f'<span class="pill">stale rules blocked · {_e(proof.get("staleRulesBlocked", 0))}</span>'
            f'<span class="pill">modified files · {_e(proof.get("modifiedFilesCount"))}</span>'
            + "".join(f'<span class="pill">omitted for budget · {_e(k)} {_e(v)}</span>'
                      for k, v in sorted((h.get("omitted") or {}).items()) if v) + '</div>'
            + (f'<p class="sub" style="margin:10px 0 0">Next action: {_e(h.get("nextAction"))}</p>' if h.get("nextAction") else "") +
            f'</div><div class="grid g2">'
            f'<div class="card"><h3>{_badge("active", "Active Verified Constraints")}</h3><p class="sub">Carried to the next model — every anchor is <code>active</code>.</p>'
            f'{_items(h.get("activeConstraints") or [], "rule", "source", "No fully verified constraints.", "verified")}</div>'
            f'<div class="card"><h3>{_badge("stale", "Quarantined Stale Anchors (Blocked)")}</h3><p class="sub">Never injected; the receiving model must not follow them.</p>'
            f'{_items(h.get("quarantinedStaleAnchors") or [], "anchor", "reason", _omitted_note(h, "quarantinedStaleAnchors", "Nothing quarantined."), "blocked")}'
            f'<h3 style="margin-top:16px">{_badge("compatible-unverified", "Re-verify before use")}</h3>'
            f'{_items(h.get("reverifyBeforeUse") or [], "anchor", "reason", _omitted_note(h, "reverifyBeforeUse", "Nothing to re-verify."), "")}</div></div>'
            f'<div class="grid g2"><div class="card"><h3>Rejected Approaches (Dead-Ends)</h3><p class="sub">Recorded failures travel with the capsule so the next vendor does not repeat them.</p>'
            f'{_items(h.get("rejectedApproaches") or [], "approach", "source", "No dead-ends recorded.", "deadend")}</div>'
            f'<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px">'
            f'<h3>Markdown capsule</h3><button class="btn" type="button" data-copy="{_e(tid)}">Copy Markdown Capsule</button></div>'
            f'<textarea class="md" id="{_e(tid)}" readonly spellcheck="false">{_e(h.get("markdown") or "")}</textarea></div></div></div>')
    return "".join(parts)


def _tab_routing(payload: Dict[str, Any]) -> str:
    m = payload.get("routletMatrix") or ROUTLET_MATRIX
    bench_by_family: Dict[str, List[Dict[str, Any]]] = {}
    for b in m.get("benchmarks") or []:
        bench_by_family.setdefault(b.get("family"), []).append(b)
    fams = "".join(
        f'<div class="card"><div class="lbl">{_e(f.get("name"))}</div><h3 style="margin-top:4px">{_e(" / ".join(f.get("models") or []))}</h3>'
        f'<p class="sub">{_e(f.get("lane"))}</p>'
        + "".join(f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-top:1px solid var(--line)">'
                  f'<span>{_e(b.get("name"))}</span><span class="kpi" style="font-size:18px">{_e(b.get("display"))}</span></div>'
                  for b in bench_by_family.get(f.get("id"), [])) + '</div>'
        for f in m.get("families") or [])
    topo = m.get("topologies") or []
    buttons = "".join(f'<button type="button" data-value="{_e(t["id"])}" aria-pressed="{"true" if t["id"] == "balanced" else "false"}">'
                      f'{_e(t["label"])}</button>' for t in topo)
    panels = "".join(
        f'<div data-topology="{_e(t["id"])}"{"" if t["id"] == "balanced" else " hidden"}><p class="sub">{_e(t.get("summary"))}</p>'
        f'<div class="tablewrap"><table><thead><tr><th>Role</th><th>Routed to</th><th>Effort / cadence</th></tr></thead><tbody>'
        + "".join(f'<tr><td>{_e(role)}</td><td class="mono">{_e(model)}</td><td class="mono hash">{_e(effort)}</td></tr>'
                  for role, model, effort in t.get("lanes") or []) + '</tbody></table></div></div>'
        for t in topo)
    pricing = payload.get("pricing") or {}
    packs = "".join(
        f'<div class="card"><div style="display:flex;justify-content:space-between;align-items:baseline"><h3>{_e(p.get("name"))}</h3>'
        f'<span class="pill acc">{_e(p.get("id"))}</span></div><p class="sub">{_e(p.get("stack"))}</p>'
        f'<div class="grid g2"><div><div class="lbl">Rules</div><div class="kpi">{_e(p.get("rules"))}</div></div>'
        f'<div><div class="lbl">Anchors</div><div class="kpi">{_e(p.get("anchors"))}</div></div></div>'
        f'<ul class="items" style="margin-top:12px">' + "".join(f'<li>{_e(g)}</li>' for g in p.get("guards") or []) +
        f'</ul><p class="price" style="margin:12px 0 0"><b>{_e(pricing.get("harnessPack"))}</b> · preview</p></div>'
        for p in payload.get("proPacks") or [])
    return (f'<div class="grid"><div class="grid g3">{fams}</div>'
            f'<p class="sub" style="margin:0">Evidence as of {_e(m.get("evidenceAsOf"))} · {_e(m.get("evidenceSource"))}</p>'
            f'<div class="card"><div style="display:flex;flex-wrap:wrap;gap:10px;justify-content:space-between;align-items:center">'
            f'<div><h3>Orchestration topology</h3><p class="sub" style="margin:0">Routlet proposes; you select. Memlet gates every hop.</p></div>'
            f'<div class="seg" data-topology-switch role="group" aria-label="topology">{buttons}</div></div><div style="margin-top:14px">{panels}</div></div>'
            f'<div><h3 style="margin:8px 0 4px;font-size:14px">Pro Harness Packs · preview</h3>'
            f'<p class="sub">Pre-anchored rule sets for common stacks. Contents are a preview and may change.</p>'
            f'<div class="grid g3">{packs}</div></div></div>')


def render_inspector_html(payload: Dict[str, Any]) -> str:
    """Render a fully self-contained dark-mode HTML console (no external requests; CSP-enforced)."""
    payload = _scrub(payload)
    default_ws = payload.get("defaultWorkspace") if payload.get("defaultWorkspace") in ("live", "demo") else "demo"
    demo = payload["workspaces"].get("demo") or {}
    demo_label = f"Interactive Showcase ({int((demo.get('anchorSummary') or {}).get('total') or 0)} Anchors · " \
                 f"{len(demo.get('scenarios') or [])} Refactor Scenarios)"
    ws_buttons = "".join(
        f'<button type="button" data-value="{v}" aria-pressed="{"true" if v == default_ws else "false"}">{_e(label)}</button>'
        for v, label in (("live", "Live Repository"), ("demo", demo_label)))
    filters = "".join(
        f'<button type="button" data-value="{v}" aria-pressed="{"true" if v == "all" else "false"}">{_e(label)}</button>'
        for v, label in (("all", "All"), ("active", "🟢 Active"), ("compatible-unverified", "🟡 Compatible-Unverified"),
                         ("stale", "🔴 Stale")))
    notice = f'<p class="notice">{_e(payload.get("showcaseReason"))}</p>' if payload.get("showcaseReason") else ""
    errs = payload.get("errors") or []
    err_html = f'<p class="notice" style="border-color:rgba(244,63,94,.35);color:#FDA4AF">Some probes failed: {_e("; ".join(errs))}</p>' if errs else ""
    tabs = (("t-health", "AST Anchor Health & Wiki Ledger", _tab_health),
            ("t-bench", "Refactoring Benchmark & Delta Simulator", _tab_benchmark),
            ("t-handover", "Handover Capsule", _tab_handover),
            ("t-routing", "Routing Matrix & Pro Packs", _tab_routing))
    tab_buttons = "".join(
        f'<button type="button" role="tab" id="{tid}-tab" aria-controls="{tid}" aria-selected="{"true" if i == 0 else "false"}" '
        f'tabindex="{0 if i == 0 else -1}">{i + 1} · {_e(label)}</button>' for i, (tid, label, _f) in enumerate(tabs))
    panels = "".join(
        f'<section role="tabpanel" id="{tid}" aria-labelledby="{tid}-tab"{"" if i == 0 else " hidden"}>{fn(payload)}</section>'
        for i, (tid, _l, fn) in enumerate(tabs))
    pricing = payload.get("pricing") or {}
    doc = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; '
        'script-src \'unsafe-inline\'; img-src data:; base-uri \'none\'; form-action \'none\'">'
        '<meta name="referrer" content="no-referrer">'
        f'<title>Nocturne Memory &amp; Routing Inspector · {_e(payload.get("repoName"))}</title>'
        f'<style>{_CSS}</style></head>'
        f'<body data-ws="{default_ws}" data-filter="all"><div class="wrap">'
        f'<header class="top"><div class="brand"><div class="logo" aria-hidden="true"></div><div>'
        f'<h1>Nocturne Memory &amp; Routing Inspector</h1><p>Nocturne Lab · Memlet {_e(payload.get("memletVersion"))} · '
        f'<span class="mono">{_e(payload.get("repoName"))}</span></p></div></div>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap"><span class="pill acc">memlet ui · {_e(pricing.get("inspector"))}</span>'
        f'<span class="pill">read-only</span><span class="pill">0 network calls</span></div></header>'
        f'<div class="controls"><div class="seg" data-ws-switch role="group" aria-label="workspace">{ws_buttons}</div>'
        f'<div class="seg" data-filter-switch role="group" aria-label="anchor status filter">{filters}</div></div>'
        f'{notice}{err_html}<nav class="tabs" role="tablist" aria-label="inspector sections">{tab_buttons}</nav>{panels}'
        f'<footer><span>Generated offline by <span class="mono">memlet ui</span> · schema <span class="mono">{_e(payload.get("schema"))}</span>'
        f'</span><span>Self-contained file · no CDN · host paths scrubbed to <span class="mono">&lt;repo&gt;</span></span></footer>'
        f'</div><script>{_JS}</script></body></html>')
    # Final belt-and-braces pass: no home-directory path may survive in the document.
    return _HOME_PATH_RE.sub(html.escape(REPO_PLACEHOLDER), doc)


# ---------------------------------------------------------------------------
# Export


def _atomic_write(path: Path, text: str) -> int:
    data = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".inspector-", suffix=".html", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)  # replaces a symlink at `path` instead of following it
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return len(data)


def export_inspector_html(repo_path: str = ".", output_path: Optional[str] = None) -> Dict[str, Any]:
    """`memlet ui`: build the payload, render the console and write it.

    Writes to `output_path` when given, else to `<repo>/.memlet/inspector.html` when `.memlet/` exists. Otherwise
    nothing is written and the rendered document is returned under `html`."""
    root = Path(repo_path).expanduser().resolve()
    payload = build_inspector_payload(str(root))
    doc = render_inspector_html(payload)
    memlet_dir = root / memlet_wiki.MEMLET_DIR
    if output_path:
        # Resolve the parent only: a symlink at the target itself is replaced, never followed.
        out = Path(os.path.abspath(os.path.expanduser(output_path)))
        target: Optional[Path] = out.parent.resolve() / out.name
    elif memlet_dir.is_dir() and not memlet_dir.is_symlink():
        target = root / INSPECTOR_FILE
    else:
        target = None
    bytes_written = _atomic_write(target, doc) if target else 0
    live = payload["workspaces"]["live"]
    result: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "command": "ui",
        "outputPath": str(target) if target else None,
        "written": target is not None,
        "defaultWorkspace": payload["defaultWorkspace"],
        "anchorSummary": live.get("anchorSummary"),
        "showcaseAnchorSummary": payload["workspaces"]["demo"].get("anchorSummary"),
        "benchmarkDigest": (payload.get("benchmark") or {}).get("resultDigest"),
        "bytesWritten": bytes_written,
        "htmlBytes": len(doc.encode("utf-8")),
        "externalNetworkCalls": 0,
        "errors": payload.get("errors") or [],
    }
    if target is None:
        result["html"] = doc
        result["hint"] = "No .memlet/ directory: pass --output PATH (or run `memlet init`) to write the inspector file."
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="memlet ui", description="Nocturne Memory & Routing Inspector (self-contained HTML).")
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--output", "-o", default=None, help="HTML output path (default: .memlet/inspector.html)")
    args = parser.parse_args(argv)
    result = export_inspector_html(args.path, args.output)
    result.pop("html", None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["exitCode"])


if __name__ == "__main__":
    sys.exit(main())
