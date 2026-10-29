"""Memlet zero-daemon onboarding (`memlet init`) and AST-anchored Markdown wiki (`memlet wiki`).

`memlet init [path] [--dry-run]` inspects a repository and (unless `--dry-run`) creates
`.memlet/config.json`, `.memlet/wiki/INDEX.md` and the local ledger `.memlet/ledger.sqlite3`,
pinning every `file::Symbol` anchor found in agent instruction files (`CLAUDE.md`, `AGENTS.md`, ...)
to its current AST, full-signature and required-signature hash.

`memlet wiki [path] [--dry-run]` reads every Markdown page under `.memlet/wiki/` (human edits
included), re-resolves each anchor against the current code, rewrites the generated status block of
`INDEX.md` and a bounded per-page status header on every other page, and records pages whose body
changed since the last recorded hash as `user_edit` events in the ledger. Anchor states:

- `active`: symbol resolves and its pinned AST hash still matches.
- `compatible-unverified`: symbol resolves, but it is unpinned, its body changed while the pinned
  call signature is unchanged, only optional parameters were added (required signature unchanged),
  or its language has no structural extractor.
- `stale`: file or symbol is gone, the file no longer parses, or the required signature changed.

`memlet import [path] [--from auto|claude-md|cursorrules|ai-memory|claude-mem] [--dry-run]` pulls rules
from existing instruction / memory files into `.memlet/wiki/imported-rules.md`, tagging every rule with
the live state of the code it references. `memlet handover` builds a signed, budgeted (<500 token)
handover capsule that carries only verified constraints and recorded dead-ends to the next model.
`run_refactoring_comparison_benchmark()` (`memlet benchmark`) replays four refactoring scenarios in a
temporary directory and compares raw Markdown, an unverified wiki, and Memlet.

Command output is deterministic (ledger rows carry UTC timestamps, reports do not); no command
starts a daemon, calls a network or LLM, or opens `.env*` files or other secret material.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .audit import BACKTICK_FILE_RE, EXCLUDED_DIRS, EXPLICIT_ANCHOR_RE, IGNORED_FILE_TOKENS, audit_repository
from .code_anchors import extract_file_symbol_hashes, extract_file_symbol_signatures

MEMLET_VERSION = "8.4.0"
CONFIG_SCHEMA_VERSION = 1
MEMLET_DIR = ".memlet"
WIKI_DIR = ".memlet/wiki"
CONFIG_PATH = ".memlet/config.json"
INDEX_PATH = ".memlet/wiki/INDEX.md"
LEDGER_PATH = ".memlet/ledger.sqlite3"
ANCHOR_STATES = ("active", "compatible-unverified", "stale")

STATUS_BEGIN = "<!-- memlet:status:begin -->"
STATUS_END = "<!-- memlet:status:end -->"
PAGE_STATUS_BEGIN = "<!-- memlet-page-status:start -->"
PAGE_STATUS_END = "<!-- memlet-page-status:end -->"
PIN_RE = re.compile(
    r"<!--\s*memlet:anchor\s+(?P<path>[^\s:]+)::(?P<symbol>[A-Za-z_$][\w.$-]*)"
    r"\s+ast=(?P<ast>[0-9a-f]{6,64})(?:\s+sig=(?P<sig>[0-9a-f]{6,64}))?(?:\s+req=(?P<req>[0-9a-f]{6,64}))?\s*-->"
)
_STATUS_BLOCK_RE = re.compile(r"<!-- memlet:status:(?:begin|start) -->.*?" + re.escape(STATUS_END), re.S)
_PAGE_STATUS_RE = re.compile(re.escape(PAGE_STATUS_BEGIN) + r".*?" + re.escape(PAGE_STATUS_END) + r"\n?", re.S)
_LEDGER_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS anchor_snapshots (anchor TEXT PRIMARY KEY, path TEXT, symbol TEXT, status TEXT,"
    " ast_hash TEXT, sig_hash TEXT, req_sig_hash TEXT, updated_at TEXT)",
    "CREATE TABLE IF NOT EXISTS wiki_events (id INTEGER PRIMARY KEY AUTOINCREMENT, page TEXT, content_sha256 TEXT,"
    " event_type TEXT, recorded_at TEXT)",
)
_HASH_LEN = 16

# Agent instruction files (rules agents actually load), as opposed to general docs.
TOP_INSTRUCTION_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    ".github/copilot-instructions.md",
)
INSTRUCTION_GLOBS = (".cursor/rules/*.md", ".cursor/rules/*.mdc", ".github/instructions/*.md")
NESTED_INSTRUCTION_NAMES = ("CLAUDE.md", "AGENTS.md", "SKILL.md")


def _is_secret_path(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    return name.startswith(".env") or name.endswith((".pem", ".key")) or name in ("id_rsa", "id_ed25519", ".npmrc", ".pypirc", ".netrc")


def _rel(root: Path, p: Path) -> str:
    return str(p.relative_to(root)).replace("\\", "/")


def _git_head(root: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", sha) else None


def discover_instruction_files(root: Path) -> List[str]:
    """Return sorted repo-relative paths of agent instruction files (never `.env*`)."""
    found = set()

    def _add(p: Path) -> None:
        try:
            if p.is_symlink() or not p.is_file():
                return
            rel = _rel(root, p)
        except (OSError, ValueError):
            return
        if not _is_secret_path(rel):
            found.add(rel)

    for rel in TOP_INSTRUCTION_FILES:
        _add(root / rel)
    for pat in INSTRUCTION_GLOBS:
        for p in root.glob(pat):
            _add(p)
    for name in NESTED_INSTRUCTION_NAMES:
        for p in root.rglob(name):
            try:
                parts = p.relative_to(root).parts
            except ValueError:
                continue
            if len(parts) <= 4 and not any(part in EXCLUDED_DIRS or part == MEMLET_DIR for part in parts):
                _add(p)
    return sorted(found)


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _extract_anchors(text: str, source: str) -> List[Dict[str, Any]]:
    """Find `path::Symbol` anchors and `memlet:anchor` pins in a Markdown document."""
    out: List[Dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for m in PIN_RE.finditer(line):
            out.append({"path": m.group("path"), "symbol": m.group("symbol"), "source": source,
                        "line": line_no, "pin": {"ast": m.group("ast"), "sig": m.group("sig"), "req": m.group("req")}})
        scrubbed = PIN_RE.sub("", line)
        for m in EXPLICIT_ANCHOR_RE.finditer(scrubbed):
            if "::" not in m.group(0):
                continue
            path = m.group("path")
            out.append({"path": path[2:] if path.startswith("./") else path,
                        "symbol": m.group("symbol").rstrip(".-"), "source": source, "line": line_no, "pin": None})
    return out


def _merge_anchors(raw: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for a in raw:
        key = (a["path"], a["symbol"])
        entry = merged.setdefault(key, {"path": a["path"], "symbol": a["symbol"], "pin": None, "sources": []})
        loc = f"{a['source']}:{a['line']}"
        if a["pin"] and entry["pin"] is None:
            entry["pin"] = a["pin"]
        if not a["pin"] and loc not in entry["sources"]:
            entry["sources"].append(loc)
    return [merged[k] for k in sorted(merged)]


def _req_sig_hash(sig: Dict[str, Any]) -> Optional[str]:
    """Hash of the required call contract only: adding a defaulted parameter leaves it unchanged."""
    if "reqArgs" not in sig:
        return None
    payload = {k: sig.get(k) for k in ("kind", "isAsync", "reqArgs", "kwonlyReq", "methods")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:_HASH_LEN]


def _current_fingerprint(root: Path, rel_path: str, symbol: str) -> Dict[str, Any]:
    """Resolve an anchor against the working tree; returns resolution plus current hashes."""
    if rel_path.startswith("/") or ".." in Path(rel_path).parts or _is_secret_path(rel_path):
        return {"resolution": "rejected", "reason": "path_outside_repo_or_secret"}
    target = root / rel_path
    try:
        target.resolve().relative_to(root)
    except (OSError, ValueError):
        return {"resolution": "rejected", "reason": "path_outside_repo_or_secret"}
    sig_info = extract_file_symbol_signatures(target)
    if not sig_info.get("__file_exists__"):
        return {"resolution": "missing", "reason": "missing_file"}
    resolution = sig_info.get("__resolution__")
    if resolution == "parse_error" or not sig_info.get("__syntax_ok__", True):
        return {"resolution": "parse_error", "reason": "syntax_error"}
    if resolution in ("unsupported", "oversized"):
        return {"resolution": resolution, "reason": "unsupported_language"}
    sig = (sig_info.get("symbols") or {}).get(symbol)
    if sig is None:
        return {"resolution": "missing", "reason": "missing_symbol"}
    ast_hash = (extract_file_symbol_hashes(target).get(symbol) or sig.get("normAstHash") or sig.get("sigHash") or "")
    return {
        "resolution": "exact",
        "ast": ast_hash[:_HASH_LEN],
        "sig": (sig.get("sigHash") or "")[:_HASH_LEN],
        "req": _req_sig_hash(sig),
    }


def _same_hash(a: Optional[str], b: Optional[str]) -> bool:
    n = min(len(a or ""), len(b or ""), _HASH_LEN)
    return n >= 6 and a[:n] == b[:n]


def classify_anchor(root: Path, anchor: Dict[str, Any]) -> Dict[str, Any]:
    cur = _current_fingerprint(root, anchor["path"], anchor["symbol"])
    pin = anchor.get("pin")
    if cur["resolution"] in ("missing", "parse_error", "rejected"):
        status, reason = "stale", cur["reason"]
    elif cur["resolution"] != "exact":
        status, reason = "compatible-unverified", cur["reason"]
    elif not pin:
        status, reason = "compatible-unverified", "unpinned"
    elif _same_hash(cur["ast"], pin["ast"]):
        status, reason = "active", "ast_hash_match"
    elif not (pin.get("sig") and cur["sig"]) or _same_hash(cur["sig"], pin["sig"]):
        status, reason = "compatible-unverified", "body_changed_signature_compatible"
    elif pin.get("req") and _same_hash(cur.get("req"), pin["req"]):
        status, reason = "compatible-unverified", "optional_args_added_signature_compatible"
    else:
        status, reason = "stale", "signature_drift"
    return {
        "anchor": f"{anchor['path']}::{anchor['symbol']}",
        "path": anchor["path"],
        "symbol": anchor["symbol"],
        "status": status,
        "reason": reason,
        "pinned": bool(pin),
        "currentAstHash": cur.get("ast"),
        "currentSigHash": cur.get("sig"),
        "currentReqSigHash": cur.get("req"),
        "sources": list(anchor.get("sources") or []),
    }


def _summarize(anchors: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {s: 0 for s in ANCHOR_STATES}
    for a in anchors:
        counts[a["status"]] += 1
    counts["total"] = len(anchors)
    return counts


def _audit_summary(root: Path) -> Dict[str, Any]:
    try:
        rep = audit_repository(root)
    except Exception as exc:  # onboarding must not fail on an audit edge case
        return {"scannedDocsCount": 0, "referencesChecked": 0, "errorCount": 0, "warningCount": 0,
                "countsByRule": {}, "error": type(exc).__name__}
    return {
        "scannedDocsCount": rep.get("scannedDocsCount", 0),
        "referencesChecked": rep.get("referencesChecked", 0),
        "errorCount": rep.get("errorCount", 0),
        "warningCount": rep.get("warningCount", 0),
        "countsByRule": dict(sorted((rep.get("countsByRule") or {}).items())),
    }


def _status_block(anchors: List[Dict[str, Any]], summary: Dict[str, int]) -> str:
    lines = [
        STATUS_BEGIN,
        "## Anchor health",
        "",
        f"- active: {summary['active']} · compatible-unverified: {summary['compatible-unverified']} · stale: {summary['stale']} (total {summary['total']})",
        "",
    ]
    if anchors:
        lines += ["| Status | Anchor | Reason |", "| :--- | :--- | :--- |"]
        lines += [f"| `{a['status']}` | `{a['anchor']}` | {a['reason']} |" for a in anchors]
    else:
        lines.append("No `file::Symbol` anchors yet. Write one in any page, e.g. `src/app.py::main`.")
    lines.append(STATUS_END)
    return "\n".join(lines)


def _render_index(head: Optional[str], instruction_files: List[str], anchors: List[Dict[str, Any]],
                  classified: List[Dict[str, Any]], summary: Dict[str, int], audit: Dict[str, Any]) -> str:
    lines = [
        "# Memlet Wiki Index",
        "",
        "Generated by `memlet init`. Pages under `.memlet/wiki/` are plain Markdown: edit or add them freely,",
        "then run `memlet wiki` to re-check every `` `path::Symbol` `` anchor against the current code.",
        "Pins (`memlet:anchor` comments) record the AST and signature hash seen at pin time; delete a pin and",
        "run `memlet init --force` to re-pin after you have verified a change.",
        "",
        f"- Repository head: `{head}`" if head else "- Repository head: (not a git repository)",
        "- Instruction files: " + (", ".join(f"`{f}`" for f in instruction_files) if instruction_files else "(none found)"),
        f"- `memlet check`: {audit['scannedDocsCount']} docs, {audit['referencesChecked']} references, "
        f"{audit['errorCount']} errors, {audit['warningCount']} warnings",
        "",
        "## Code anchors",
        "",
    ]
    if not anchors:
        lines.append("(no anchors found in instruction files)")
    for a in anchors:
        key = f"{a['path']}::{a['symbol']}"
        src = ", ".join(f"`{s}`" for s in a["sources"])
        lines.append(f"- `{key}` (from {src})")
        if a["pin"]:
            sig = f" sig={a['pin']['sig']}" if a["pin"]["sig"] else ""
            req = f" req={a['pin']['req']}" if a["pin"].get("req") else ""
            lines.append(f"  <!-- memlet:anchor {key} ast={a['pin']['ast']}{sig}{req} -->")
    lines += ["", _status_block(classified, summary), ""]
    return "\n".join(lines)


def _page_header(counts: Dict[str, int]) -> str:
    return "\n".join([
        PAGE_STATUS_BEGIN,
        f"> **Memlet AST Status**: active={counts['active']}, compatible-unverified={counts['compatible-unverified']}, stale={counts['stale']}",
        PAGE_STATUS_END,
    ])


def _with_page_header(text: str, counts: Dict[str, int]) -> str:
    """Replace the page status header in place, or insert it after the `# Title` line (else at the top)."""
    block = _page_header(counts) + "\n"
    if _PAGE_STATUS_RE.search(text):
        return _PAGE_STATUS_RE.sub(lambda _m: block, text, count=1)
    first, _, rest = text.partition("\n")
    if first.startswith("# "):
        return first + "\n" + block + rest
    return block + text


def _page_body_sha256(text: str) -> str:
    """Hash of the human-owned body: generated status header/blocks are excluded."""
    body = _STATUS_BLOCK_RE.sub("", _PAGE_STATUS_RE.sub("", text))
    return hashlib.sha256(body.rstrip().encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger_ready(conn: sqlite3.Connection) -> None:
    for stmt in _LEDGER_SCHEMA:
        conn.execute(stmt)


def _last_page_hashes(conn: sqlite3.Connection) -> Dict[str, str]:
    rows = conn.execute(
        "SELECT page, content_sha256 FROM wiki_events WHERE id IN (SELECT MAX(id) FROM wiki_events GROUP BY page)"
    )
    return {page: sha for page, sha in rows}


def _record_page(conn: sqlite3.Connection, page: str, sha: str, event_type: str, now: str) -> None:
    conn.execute(
        "INSERT INTO wiki_events (page, content_sha256, event_type, recorded_at) VALUES (?, ?, ?, ?)",
        (page, sha, event_type, now),
    )


def _sync_snapshots(conn: sqlite3.Connection, classified: List[Dict[str, Any]], now: str) -> bool:
    """Upsert the latest observed state of every anchor; returns True when any row changed."""
    existing = {
        row[0]: tuple(row[1:])
        for row in conn.execute("SELECT anchor, path, symbol, status, ast_hash, sig_hash, req_sig_hash FROM anchor_snapshots")
    }
    changed = False
    for c in classified:
        row = (c["path"], c["symbol"], c["status"], c["currentAstHash"], c["currentSigHash"], c["currentReqSigHash"])
        if existing.get(c["anchor"]) != row:
            conn.execute("INSERT OR REPLACE INTO anchor_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (c["anchor"], *row, now))
            changed = True
    return changed


def _read_ledger_hashes(ledger: Path) -> Dict[str, str]:
    """Read-only view of the last recorded page hashes (used by `--dry-run`)."""
    if ledger.is_symlink() or not ledger.is_file():
        return {}
    try:
        with contextlib.closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as conn:
            return _last_page_hashes(conn)
    except sqlite3.Error:
        return {}


def _load_config_pins(root: Path) -> Dict[str, Any]:
    try:
        config = json.loads((root / CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pins = config.get("pinnedAnchors") if isinstance(config, dict) else None
    return pins if isinstance(pins, dict) else {}


def _format_markdown(title: str, report: Dict[str, Any]) -> str:
    s = report["anchorSummary"]
    lines = [
        f"## {title}",
        "",
        f"- **Repository**: `{report['repoPath']}`",
        f"- **Head**: `{report.get('repoHead') or 'n/a'}`",
        f"- **Anchors**: {s['active']} active · {s['compatible-unverified']} compatible-unverified · {s['stale']} stale",
    ]
    if "instructionFiles" in report:
        lines.append("- **Instruction files**: " + (", ".join(f"`{f}`" for f in report["instructionFiles"]) or "(none)"))
    if report.get("pages") is not None:
        lines.append("- **Wiki pages**: " + (", ".join(f"`{p}`" for p in report["pages"]) or "(none)"))
    if report.get("actions"):
        lines += ["", "### Actions", ""] + [f"- `{a['op']}` `{a['path']}`" for a in report["actions"]]
    if report.get("anchors"):
        lines += ["", "| Status | Anchor | Reason |", "| :--- | :--- | :--- |"]
        lines += [f"| `{a['status']}` | `{a['anchor']}` | {a['reason']} |" for a in report["anchors"]]
    return "\n".join(lines) + "\n"


def _base_config(head: Optional[str], instruction_files: List[str]) -> Dict[str, Any]:
    return {
        "schemaVersion": CONFIG_SCHEMA_VERSION,
        "tool": "memlet",
        "version": MEMLET_VERSION,
        "repoHead": head,
        "instructionFiles": instruction_files,
        "wikiDir": WIKI_DIR,
        "indexPage": INDEX_PATH,
        "ledgerDb": LEDGER_PATH,
        "pinnedAnchors": {},
        "anchorStates": list(ANCHOR_STATES),
        "secretsPolicy": "never-read-env-or-secret-files",
        "daemon": False,
    }


def _resolve_root(repo_path) -> Path:
    root = Path(repo_path or ".").expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"repository path not found: {root}")
    return root


def init_project(repo_path=".", *, dry_run: bool = False, force: bool = False, output_format: str = "json") -> Dict[str, Any]:
    """`memlet init`: onboard a repo into `.memlet/` (read-only when `dry_run`)."""
    root = _resolve_root(repo_path)
    head = _git_head(root)
    instruction_files = discover_instruction_files(root)
    raw: List[Dict[str, Any]] = []
    for rel in instruction_files:
        raw.extend(_extract_anchors(_read_text(root / rel), rel))
    anchors = []
    for a in _merge_anchors(raw):
        # Pin every resolvable anchor to the AST/signature hash it has right now.
        cur = _current_fingerprint(root, a["path"], a["symbol"])
        pin = ({"ast": cur["ast"], "sig": cur["sig"] or None, "req": cur["req"]}
               if cur["resolution"] == "exact" and cur["ast"] else None)
        anchors.append(dict(a, pin=pin))
    classified = [classify_anchor(root, a) for a in anchors]
    summary = _summarize(classified)
    audit = _audit_summary(root)
    config = _base_config(head, instruction_files)
    config["pinnedAnchors"] = {
        f"{a['path']}::{a['symbol']}": {"astHash": a["pin"]["ast"], "sigHash": a["pin"]["sig"], "reqSigHash": a["pin"]["req"]}
        for a in anchors if a["pin"]
    }
    index_text = _render_index(head, instruction_files, anchors, classified, summary, audit)
    config_text = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    actions: List[Dict[str, str]] = []
    plan = [(CONFIG_PATH, config_text, True), (INDEX_PATH, index_text, force)]
    for rel, text, overwrite in plan:
        target = root / rel
        exists = target.is_file()
        if exists and target.read_text(encoding="utf-8", errors="ignore") == text:
            op = "unchanged"
        elif exists and not overwrite:
            op = "kept"
        else:
            op = "update" if exists else "create"
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        if dry_run and op in ("create", "update"):
            op = "would-" + op
        actions.append({"op": op, "path": rel})

    ledger = root / LEDGER_PATH
    index_written = actions[-1]["op"] in ("create", "update")
    if ledger.is_symlink():
        op = "kept"
    elif dry_run:
        op = "would-update" if ledger.is_file() else "would-create"
    else:
        existed = ledger.is_file()
        now = _utc_now()
        with contextlib.closing(sqlite3.connect(str(ledger))) as conn:
            with conn:
                _ledger_ready(conn)
                changed = _sync_snapshots(conn, classified, now)
                index = root / INDEX_PATH
                if index.is_file() and not index.is_symlink():
                    sha = _page_body_sha256(_read_text(index))
                    last = _last_page_hashes(conn).get(INDEX_PATH)
                    # A kept INDEX.md with a recorded hash is left for `memlet wiki` to sync as a user edit.
                    if last != sha and (index_written or last is None):
                        _record_page(conn, INDEX_PATH, sha, "init", now)
                        changed = True
        op = "update" if existed and changed else ("unchanged" if existed else "create")
    actions.append({"op": op, "path": LEDGER_PATH})

    report: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "command": "init",
        "dryRun": bool(dry_run),
        "repoPath": str(root),
        "repoHead": head,
        "instructionFiles": instruction_files,
        "config": config,
        "actions": actions,
        "anchorSummary": summary,
        "anchors": classified,
        "audit": audit,
        "next": ["memlet wiki", "memlet check"],
    }
    if (output_format or "json").lower() == "markdown":
        report["formatted"] = _format_markdown("Memlet Init" + (" (dry run)" if dry_run else ""), report)
    return report


def wiki_status(repo_path=".", *, dry_run: bool = False, output_format: str = "json") -> Dict[str, Any]:
    """`memlet wiki`: re-check AST anchors in `.memlet/wiki/*.md`, refresh the INDEX status block and
    per-page status headers, and sync human page edits into `.memlet/ledger.sqlite3`."""
    root = _resolve_root(repo_path)
    wiki_dir = root / WIKI_DIR
    initialized = wiki_dir.is_dir() and not wiki_dir.is_symlink()
    raw: List[Dict[str, Any]] = []
    pages: List[str] = []
    texts: Dict[str, str] = {}
    if initialized:
        for p in sorted(wiki_dir.rglob("*.md")):
            if p.is_symlink() or not p.is_file():
                continue
            rel = _rel(root, p)
            pages.append(rel)
            texts[rel] = _read_text(p)
            raw.extend(_extract_anchors(_STATUS_BLOCK_RE.sub("", _PAGE_STATUS_RE.sub("", texts[rel])), rel))
    else:
        # Not initialized: read-only preview over instruction files, nothing is written.
        for rel in discover_instruction_files(root):
            raw.extend(_extract_anchors(_read_text(root / rel), rel))
    merged = _merge_anchors(raw)
    if initialized:
        # Pins written before `req=` existed fall back to the required-signature hash in config.json.
        config_pins = _load_config_pins(root)
        for a in merged:
            cp = config_pins.get(f"{a['path']}::{a['symbol']}")
            if a["pin"] and not a["pin"].get("req") and isinstance(cp, dict) and _same_hash(a["pin"]["ast"], cp.get("astHash")):
                a["pin"] = dict(a["pin"], req=cp.get("reqSigHash"))
    classified = [classify_anchor(root, a) for a in merged]
    summary = _summarize(classified)

    actions: List[Dict[str, str]] = []
    index = root / INDEX_PATH
    if initialized and index.is_file() and not index.is_symlink():
        old = _read_text(index)
        block = _status_block(classified, summary)
        new = _STATUS_BLOCK_RE.sub(lambda _m: block, old) if _STATUS_BLOCK_RE.search(old) else old.rstrip("\n") + "\n\n" + block + "\n"
        if new == old:
            actions.append({"op": "unchanged", "path": INDEX_PATH})
        elif dry_run:
            actions.append({"op": "would-update", "path": INDEX_PATH})
        else:
            index.write_text(new, encoding="utf-8")
            actions.append({"op": "update", "path": INDEX_PATH})

    status_by_key = {c["anchor"]: c["status"] for c in classified}
    page_summaries: Dict[str, Dict[str, int]] = {}
    for a in raw:
        counts = page_summaries.setdefault(a["source"], {s: 0 for s in ANCHOR_STATES})
        counts[status_by_key[f"{a['path']}::{a['symbol']}"]] += 0 if a["pin"] else 1

    # Every page other than INDEX.md carries a bounded status header for its own anchors.
    for rel in pages:
        if rel == INDEX_PATH:
            continue
        old = texts[rel]
        new = _with_page_header(old, page_summaries.get(rel) or {s: 0 for s in ANCHOR_STATES})
        if new == old:
            actions.append({"op": "unchanged", "path": rel})
        elif dry_run:
            actions.append({"op": "would-update", "path": rel})
        else:
            (root / rel).write_text(new, encoding="utf-8")
            texts[rel] = new
            actions.append({"op": "update", "path": rel})

    # Bi-directional sync: a page body that differs from its last recorded hash is a human edit.
    ledger = root / LEDGER_PATH
    ledger_synced = False
    user_edited: List[str] = []
    if initialized and not ledger.is_symlink():
        page_hashes = {rel: _page_body_sha256(texts[rel]) for rel in pages}
        if dry_run:
            last = _read_ledger_hashes(ledger)
            user_edited = [rel for rel in pages if last.get(rel) != page_hashes[rel] and not (rel == INDEX_PATH and rel not in last)]
        else:
            existed = ledger.is_file()
            now = _utc_now()
            with contextlib.closing(sqlite3.connect(str(ledger))) as conn:
                with conn:
                    _ledger_ready(conn)
                    last = _last_page_hashes(conn)
                    changed = False
                    for rel in pages:
                        if last.get(rel) == page_hashes[rel]:
                            continue
                        event = "init" if rel == INDEX_PATH and rel not in last else "user_edit"
                        _record_page(conn, rel, page_hashes[rel], event, now)
                        if event == "user_edit":
                            user_edited.append(rel)
                        changed = True
                    changed = _sync_snapshots(conn, classified, now) or changed
            ledger_synced = True
            actions.append({"op": "update" if existed and changed else ("unchanged" if existed else "create"), "path": LEDGER_PATH})

    report: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "command": "wiki",
        "dryRun": bool(dry_run),
        "initialized": initialized,
        "repoPath": str(root),
        "repoHead": _git_head(root),
        "wikiDir": WIKI_DIR,
        "pages": pages if initialized else None,
        "actions": actions,
        "ledgerSynced": ledger_synced,
        "userEditedPages": user_edited,
        "anchorSummary": summary,
        "pageSummaries": [dict(page=k, **v) for k, v in sorted(page_summaries.items())],
        "anchors": classified,
    }
    if not initialized:
        report["hint"] = "Run `memlet init` to create .memlet/wiki/; showing a read-only preview of instruction-file anchors."
    if (output_format or "json").lower() == "markdown":
        report["formatted"] = _format_markdown("Memlet Wiki", report)
    return report


# ---------------------------------------------------------------------------
# `memlet import`: bring existing instruction / memory files under AST verification.

IMPORT_PAGE = ".memlet/wiki/imported-rules.md"
HANDOVER_PAGE = ".memlet/wiki/HANDOVER.md"
IMPORT_SOURCES = ("auto", "claude-md", "cursorrules", "ai-memory", "claude-mem")
_MEMORY_DIRS = {"ai-memory": ".ai-memory", "claude-mem": ".claude-mem"}
_GENERATED_PAGES = (INDEX_PATH, IMPORT_PAGE, HANDOVER_PAGE)
RULE_STATES = ANCHOR_STATES + ("unanchored",)
_MAX_SOURCE_FILES = 400
_MAX_SOURCE_BYTES = 1_000_000
_MAX_RULES_PER_FILE = 200
_RULE_TEXT_LIMIT = 300
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(?P<text>\S.*)$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")
_DEAD_END_HEADING_RE = re.compile(r"\b(?:rejected|dead[\s-]?ends?)\b", re.I)


def _memory_dir_files(root: Path, dirname: str) -> List[str]:
    """Markdown files under a memory-tool directory (`.ai-memory/`, `.claude-mem/`), bounded and secret-free."""
    base = root / dirname
    if base.is_symlink() or not base.is_dir():
        return []
    out: List[str] = []
    for p in sorted(base.rglob("*.md")):
        if len(out) >= _MAX_SOURCE_FILES:
            break
        try:
            rel = _rel(root, p)
            p.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        parts = Path(rel).parts
        if p.is_symlink() or not p.is_file() or len(parts) > 8 or _is_secret_path(rel):
            continue
        if any(part in EXCLUDED_DIRS for part in parts):
            continue
        out.append(rel)
    return out


def import_source_files(root: Path, source: str = "auto") -> List[str]:
    """Repo-relative files `memlet import --from <source>` reads (never `.env*`)."""
    source = (source or "auto").lower()
    if source not in IMPORT_SOURCES:
        raise ValueError(f"unknown import source: {source} (expected one of {', '.join(IMPORT_SOURCES)})")
    instruction_files = discover_instruction_files(root)
    if source == "auto":
        picked = instruction_files + _memory_dir_files(root, ".ai-memory") + _memory_dir_files(root, ".claude-mem")
    elif source == "claude-md":
        picked = [f for f in instruction_files if f.rsplit("/", 1)[-1] in ("CLAUDE.md", "AGENTS.md", "GEMINI.md")]
    elif source == "cursorrules":
        picked = [f for f in instruction_files if f == ".cursorrules" or f.startswith(".cursor/rules/")]
    else:
        picked = _memory_dir_files(root, _MEMORY_DIRS[source])
    return sorted(set(picked))


def _clean_ref_path(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path


def _line_refs(text: str) -> List[Tuple[str, Optional[str]]]:
    """`path::Symbol` anchors and backticked file paths in one rule, in order of appearance."""
    refs: List[Tuple[str, Optional[str]]] = []
    scrubbed = PIN_RE.sub("", text)
    for m in EXPLICIT_ANCHOR_RE.finditer(scrubbed):
        if "::" in m.group(0):
            refs.append((_clean_ref_path(m.group("path")), m.group("symbol").rstrip(".-")))
    for m in BACKTICK_FILE_RE.finditer(scrubbed):
        path = _clean_ref_path(m.group("path"))
        if not m.group("symbol") and path.lower() not in IGNORED_FILE_TOKENS:
            refs.append((path, None))
    return list(dict.fromkeys(refs))


def _extract_rules(text: str, source: str) -> List[Dict[str, Any]]:
    """List items (or, in a file without lists, plain lines) outside code fences, with their section."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":  # `.mdc` / memory-file front matter
        for i in range(1, min(len(lines), 60)):
            if lines[i].strip() == "---":
                lines = [""] * (i + 1) + lines[i + 1:]
                break
    has_bullets = any(_BULLET_RE.match(line) for line in lines)
    rules: List[Dict[str, Any]] = []
    section, in_fence = "", False
    for line_no, line in enumerate(lines, start=1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        heading = _HEADING_RE.match(line)
        if in_fence or heading:
            section = heading.group("title").strip() if heading and not in_fence else section
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(("<!--", "|", ">")):
            continue
        bullet = _BULLET_RE.match(line)
        body = PIN_RE.sub("", bullet.group("text") if bullet else stripped).strip()
        refs = _line_refs(body)
        if not body or not (bullet or not has_bullets or refs):
            continue
        rules.append({"source": source, "line": line_no, "section": section,
                      "text": body[:_RULE_TEXT_LIMIT], "refs": refs})
        if len(rules) >= _MAX_RULES_PER_FILE:
            break
    return rules


def _rules_from_files(root: Path, files: Iterable[str]) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    for rel in files:
        p = root / rel
        try:
            if p.stat().st_size > _MAX_SOURCE_BYTES:
                continue
        except OSError:
            continue
        rules.extend(_extract_rules(_read_text(p), rel))
    return rules


def _unsafe_rel(path: str) -> bool:
    return path.startswith("/") or ".." in Path(path).parts or _is_secret_path(path)


def _resolve_ref_path(root: Path, source: str, path: str) -> str:
    """Root-relative first; a nested instruction file may also reference paths relative to itself."""
    if _unsafe_rel(path) or (root / path).exists():
        return path
    parent = Path(source).parent.as_posix()
    if parent not in ("", "."):
        candidate = f"{parent}/{path}"
        if not _unsafe_rel(candidate) and (root / candidate).exists():
            return candidate
    return path


def _classify_path(root: Path, path: str) -> Dict[str, Any]:
    if _unsafe_rel(path):
        status, reason = "stale", "path_outside_repo_or_secret"
    else:
        try:
            (root / path).resolve().relative_to(root)
            exists = (root / path).exists()
        except (OSError, ValueError):
            exists = False
        status, reason = ("active", "path_exists") if exists else ("stale", "missing_path")
    return {"anchor": path, "path": path, "symbol": None, "status": status, "reason": reason, "pinned": False,
            "pinSource": None, "currentAstHash": None, "currentSigHash": None, "currentReqSigHash": None}


def _config_pins(root: Path) -> Dict[str, Dict[str, Optional[str]]]:
    pins = {}
    for key, cp in _load_config_pins(root).items():
        if isinstance(cp, dict) and cp.get("astHash"):
            pins[key] = {"ast": cp["astHash"], "sig": cp.get("sigHash"), "req": cp.get("reqSigHash")}
    return pins


def _verify_rules(root: Path, rules: List[Dict[str, Any]], pins: Dict[str, Dict[str, Any]],
                  pin_source: str = "config") -> List[Dict[str, Any]]:
    """Classify each referenced anchor once and tag every rule with its worst anchor state.

    Unpinned symbol anchors are pinned to the hash they have right now (as `memlet init` does), so a
    later drift is only detectable once a pin has been recorded in `.memlet/`."""
    cache: Dict[str, Dict[str, Any]] = {}
    for r in rules:
        keys: List[str] = []
        for raw_path, symbol in r["refs"]:
            path = _resolve_ref_path(root, r["source"], raw_path)
            key = f"{path}::{symbol}" if symbol else path
            if key not in cache:
                if symbol:
                    pin, src = pins.get(key), pin_source
                    if pin is None:
                        cur = _current_fingerprint(root, path, symbol)
                        ok = cur["resolution"] == "exact" and cur.get("ast")
                        pin = {"ast": cur["ast"], "sig": cur["sig"] or None, "req": cur["req"]} if ok else None
                        src = "current" if ok else None
                    entry = classify_anchor(root, {"path": path, "symbol": symbol, "pin": pin})
                    entry.update(pin=pin, pinSource=src)
                else:
                    entry = _classify_path(root, path)
                entry["sources"] = []
                cache[key] = entry
            loc = f"{r['source']}:{r['line']}"
            if loc not in cache[key]["sources"]:
                cache[key]["sources"].append(loc)
            if key not in keys:
                keys.append(key)
        states = {cache[k]["status"] for k in keys}
        r["anchors"] = keys
        r["status"] = next((s for s in ("stale", "compatible-unverified", "active") if s in states), "unanchored")
        r["id"] = "rule-" + hashlib.sha256(f"{r['source']}\n{r['text']}".encode("utf-8")).hexdigest()[:12]
    return [cache[k] for k in sorted(cache)]


def _public_anchor(a: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in a.items() if k != "pin"}


def _public_rule(r: Dict[str, Any], by_key: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": r["id"], "source": r["source"], "line": r["line"], "section": r["section"], "text": r["text"],
        "status": r["status"],
        "anchors": [{"anchor": k, "status": by_key[k]["status"], "reason": by_key[k]["reason"]} for k in r["anchors"]],
    }


def _rule_counts(rules: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {s: 0 for s in RULE_STATES}
    for r in rules:
        counts[r["status"]] += 1
    return counts


def _render_import_page(files: List[str], rules: List[Dict[str, Any]], by_key: Dict[str, Dict[str, Any]],
                        counts: Dict[str, int], broken: List[str]) -> str:
    lines = [
        "# Imported Rules",
        "",
        "<!-- Generated by `memlet import`; re-running it regenerates this page. Keep your own notes on other pages. -->",
        "",
        "- Sources: " + (", ".join(f"`{f}`" for f in files) if files else "(none found)"),
        f"- Rules: {len(rules)} imported · {counts['active']} active · {counts['compatible-unverified']} compatible-unverified"
        f" · {counts['stale']} stale · {counts['unanchored']} unanchored",
        "- Broken paths: " + (", ".join(f"`{b}`" for b in broken) if broken else "none"),
    ]
    pinned_here = set()
    for source in files:
        page_rules = [r for r in rules if r["source"] == source]
        if not page_rules:
            continue
        lines += ["", f"## `{source}`", ""]
        for r in page_rules:
            notes = [f"`{k}`: {by_key[k]['reason']}" for k in r["anchors"] if by_key[k]["status"] != "active"]
            suffix = f"L{r['line']}" + ("; " + "; ".join(notes) if notes else "")
            lines.append(f"- `{r['status']}` {r['text']} _({suffix})_")
            for k in r["anchors"]:
                pin = by_key[k].get("pin")
                if pin and k not in pinned_here:
                    pinned_here.add(k)
                    sig = f" sig={pin['sig']}" if pin.get("sig") else ""
                    req = f" req={pin['req']}" if pin.get("req") else ""
                    lines.append(f"  <!-- memlet:anchor {k} ast={pin['ast']}{sig}{req} -->")
    return "\n".join(lines) + "\n"


def _write_generated_page(root: Path, rel: str, text: str, dry_run: bool) -> str:
    target = root / rel
    if any(p.is_symlink() for p in (root / MEMLET_DIR, root / WIKI_DIR, target)):
        return "kept"
    exists = target.is_file()
    if exists and _PAGE_STATUS_RE.sub("", _read_text(target)) == text:
        return "unchanged"
    if dry_run:
        return "would-update" if exists else "would-create"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return "update" if exists else "create"


def _escape_cell(text: str, limit: int = 100) -> str:
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    return text.replace("|", "\\|")


def import_rules(repo_path=".", *, source: str = "auto", dry_run: bool = False, output_format: str = "json") -> Dict[str, Any]:
    """`memlet import`: import rules from `CLAUDE.md` / `AGENTS.md` / `.cursorrules` / `.cursor/rules/*.mdc` /
    `.ai-memory/**/*.md` / `.claude-mem/**/*.md` and audit every code anchor they mention right away."""
    root = _resolve_root(repo_path)
    source = (source or "auto").lower()
    head = _git_head(root)
    files = import_source_files(root, source)
    rules = _rules_from_files(root, files)
    anchors = _verify_rules(root, rules, _config_pins(root))
    by_key = {a["anchor"]: a for a in anchors}
    counts = _rule_counts(rules)
    broken = sorted({a["path"] for a in anchors if a["reason"] in ("missing_path", "missing_file")})
    page_text = _render_import_page(files, rules, by_key, counts, broken)

    actions = [{"op": _write_generated_page(root, IMPORT_PAGE, page_text, dry_run), "path": IMPORT_PAGE}]
    cfg_path = root / CONFIG_PATH
    config: Any = None
    if cfg_path.is_file() and not cfg_path.is_symlink():
        try:
            config = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = False  # unreadable config is left alone
    if config is False or cfg_path.is_symlink():
        actions.append({"op": "kept", "path": CONFIG_PATH})
    else:
        existed = isinstance(config, dict)
        config = config if existed else _base_config(head, discover_instruction_files(root))
        pinned = dict(config.get("pinnedAnchors") or {})
        for a in anchors:
            # Existing pins are never auto-healed; only newly seen, currently active anchors are pinned.
            if a["symbol"] and a["status"] == "active" and a.get("pin") and a["anchor"] not in pinned:
                pinned[a["anchor"]] = {"astHash": a["pin"]["ast"], "sigHash": a["pin"]["sig"], "reqSigHash": a["pin"]["req"]}
        config["pinnedAnchors"] = dict(sorted(pinned.items()))
        config["imports"] = {"from": source, "page": IMPORT_PAGE, "sourceFiles": files}
        text = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if existed and _read_text(cfg_path) == text:
            op = "unchanged"
        elif dry_run:
            op = "would-update" if existed else "would-create"
        else:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(text, encoding="utf-8")
            op = "update" if existed else "create"
        actions.append({"op": op, "path": CONFIG_PATH})

    ledger = root / LEDGER_PATH
    if ledger.is_symlink():
        op = "kept"
    elif dry_run:
        op = "would-update" if ledger.is_file() else "would-create"
    else:
        existed = ledger.is_file()
        now = _utc_now()
        with contextlib.closing(sqlite3.connect(str(ledger))) as conn:
            with conn:
                _ledger_ready(conn)
                changed = _sync_snapshots(conn, [a for a in anchors if a["symbol"]], now)
                sha = _page_body_sha256(page_text)
                if _last_page_hashes(conn).get(IMPORT_PAGE) != sha:
                    _record_page(conn, IMPORT_PAGE, sha, "import", now)
                    changed = True
        op = "update" if existed and changed else ("unchanged" if existed else "create")
    actions.append({"op": op, "path": LEDGER_PATH})

    report: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "command": "import",
        "dryRun": bool(dry_run),
        "from": source,
        "repoPath": str(root),
        "repoHead": head,
        "page": IMPORT_PAGE,
        "sourcesScanned": files,
        "sourcesScannedCount": len(files),
        "importedCount": len(rules),
        "activeCount": counts["active"],
        "compatibleUnverifiedCount": counts["compatible-unverified"],
        "staleCount": counts["stale"],
        "unanchoredCount": counts["unanchored"],
        "brokenPathsCount": len(broken),
        "brokenPaths": broken,
        "anchorSummary": _summarize(anchors),
        "rules": [_public_rule(r, by_key) for r in rules],
        "anchors": [_public_anchor(a) for a in anchors],
        "actions": actions,
        "next": ["memlet wiki", "memlet handover"],
    }
    if (output_format or "json").lower() == "markdown":
        lines = [
            "## Memlet Import" + (" (dry run)" if dry_run else ""),
            "",
            f"- **Repository**: `{root}`",
            f"- **Head**: `{head or 'n/a'}`",
            f"- **Sources scanned**: {len(files)}" + (" (" + ", ".join(f"`{f}`" for f in files) + ")" if files else ""),
            f"- **Rules**: {len(rules)} imported · {counts['active']} active · {counts['compatible-unverified']} compatible-unverified"
            f" · {counts['stale']} stale · {counts['unanchored']} unanchored · {len(broken)} broken paths",
            "",
            "### Actions",
            "",
        ] + [f"- `{a['op']}` `{a['path']}`" for a in actions]
        if rules:
            lines += ["", "| Status | Rule | Source |", "| :--- | :--- | :--- |"]
            lines += [f"| `{r['status']}` | {_escape_cell(r['text'])} | `{r['source']}:{r['line']}` |" for r in rules]
        report["formatted"] = "\n".join(lines) + "\n"
    return report


# ---------------------------------------------------------------------------
# `memlet handover`: a signed capsule that only carries context Memlet could verify.

HANDOVER_SCHEMA = "routlet-memlet-handover/v1"
HANDOVER_TOKEN_BUDGET = 500
AUTHORITY_BOUNDARY = "read-only-context-only: execution and external side-effect approvals are NOT inherited"
HANDOVER_SIGNED_FIELDS = ("schema", "repoHead", "activeConstraints", "reverifyBeforeUse", "quarantinedStaleAnchors",
                          "rejectedApproaches", "executionProof", "nextAction", "authorityBoundary", "omitted")
_HANDOVER_LIST_CAP = 40
_HANDOVER_TEXT_LIMIT = 160
_NEXT_ACTION_LIMIT = 300
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")
_HEX_RUN_RE = re.compile(r"[0-9a-f]{16,}")


def estimate_tokens(text: str) -> int:
    """Conservative offline token estimate: ~4 chars per word piece, 2 per hex digest char pair, 1 per symbol."""
    total = 0
    for m in _TOKEN_RE.finditer(text):
        s = m.group(0)
        if len(s) == 1:
            total += 1
        elif _HEX_RUN_RE.fullmatch(s):
            total += -(-len(s) // 2)
        else:
            total += -(-len(s) // 4)
    return total


def handover_signature(capsule: Dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of the signed capsule fields."""
    payload = {k: capsule.get(k) for k in HANDOVER_SIGNED_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _blank_lines(m: "re.Match[str]") -> str:
    """Replace a generated block with blank lines so rule line numbers stay accurate."""
    return "\n" * m.group(0).count("\n")


def _git_modified_count(root: Path) -> Optional[int]:
    """Working-tree entries from `git status --porcelain`, ignoring Memlet's own `.memlet/` files."""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                             cwd=str(root), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    count = 0
    for line in out.stdout.splitlines():
        path = line[3:].strip().strip('"').split(" -> ")[-1]
        if path and not (path == MEMLET_DIR + "/" or path.startswith(MEMLET_DIR + "/")):
            count += 1
    return count


def _render_handover(c: Dict[str, Any], signature: str) -> str:
    proof = c["executionProof"]
    health = proof["anchorHealth"]
    modified = proof["modifiedFilesCount"]
    lines = [
        "## Routlet × Memlet Verified Handover Capsule",
        "",
        f"- Repo head: `{c['repoHead'] or 'n/a'}`",
        f"- Signature: `{signature}`",
        f"- Authority: {c['authorityBoundary']}",
        "",
        "### Active constraints (AST-verified)",
    ]
    lines += [f"- {a['rule']} (`{a['source']}`)" for a in c["activeConstraints"]] or ["- (none)"]
    if c["reverifyBeforeUse"]:
        lines += ["", "### Re-verify before use (compatible-unverified)"]
        lines += [f"- `{a['anchor']}`: {a['reason']}" for a in c["reverifyBeforeUse"]]
    lines += ["", "### Quarantined stale anchors (not handed over)"]
    lines += [f"- `{a['anchor']}`: {a['reason']}" for a in c["quarantinedStaleAnchors"]] or ["- (none)"]
    lines += ["", "### Rejected approaches (do not retry)"]
    lines += [f"- {a['approach']}" for a in c["rejectedApproaches"]] or ["- (none)"]
    lines += [
        "",
        "### Execution proof",
        f"- Modified files: {'n/a' if modified is None else modified} · anchors: {health['active']} active · "
        f"{health['compatible-unverified']} compatible-unverified · {health['stale']} stale",
        f"- Rules blocked as stale: {proof['staleRulesBlocked']} · unanchored rules not carried: {proof['unanchoredRulesOmitted']}",
    ]
    dropped = {k: v for k, v in c["omitted"].items() if v}
    if dropped:
        lines.append("- Omitted for token budget: " + ", ".join(f"{k} {v}" for k, v in sorted(dropped.items())))
    lines += ["", "### Next action", f"- {c['nextAction'] or '(not specified)'}"]
    return "\n".join(lines) + "\n"


def build_handover(repo_path=".", *, next_step: Optional[str] = None, rejected: Optional[Iterable[str]] = None,
                   output_format: str = "json", write: bool = False) -> Dict[str, Any]:
    """`memlet handover`: deterministic, signed (<500 token) Routlet × Memlet handover capsule.

    Only rules whose anchors are all `active` are carried as constraints; `stale` anchors are quarantined
    and `compatible-unverified` ones are listed for re-verification. Recorded dead-ends (bullets under a
    `Rejected Approaches` / `Dead-Ends` heading) and `--rejected` items travel as rejected approaches."""
    root = _resolve_root(repo_path)
    head = _git_head(root)
    rules = _rules_from_files(root, import_source_files(root, "auto"))
    page_pins: Dict[str, Dict[str, Any]] = {}
    wiki_dir = root / WIKI_DIR
    if wiki_dir.is_dir() and not wiki_dir.is_symlink():
        for p in sorted(wiki_dir.rglob("*.md")):
            if p.is_symlink() or not p.is_file():
                continue
            rel = _rel(root, p)
            text = _read_text(p)
            for a in _extract_anchors(text, rel):
                if a["pin"]:
                    page_pins.setdefault(f"{a['path']}::{a['symbol']}", a["pin"])
            if rel not in _GENERATED_PAGES:
                body = _STATUS_BLOCK_RE.sub(_blank_lines, _PAGE_STATUS_RE.sub(_blank_lines, text))
                rules.extend(_extract_rules(body, rel))
    dead_ends = [r for r in rules if _DEAD_END_HEADING_RE.search(r["section"])]
    dead_ids = {id(r) for r in dead_ends}
    constraints, seen = [], set()
    for r in rules:
        norm = re.sub(r"\s+", " ", r["text"]).strip().lower()
        if id(r) in dead_ids or norm in seen:
            continue
        seen.add(norm)
        constraints.append(r)
    pins = dict(page_pins)
    pins.update(_config_pins(root))
    anchors = _verify_rules(root, constraints, pins, pin_source="pinned")
    by_key = {a["anchor"]: a for a in anchors}
    lists: Dict[str, List[Dict[str, Any]]] = {
        "activeConstraints": [
            {"rule": _clip(r["text"], _HANDOVER_TEXT_LIMIT), "anchors": r["anchors"], "source": f"{r['source']}:{r['line']}"}
            for r in constraints if r["status"] == "active"
        ],
        "reverifyBeforeUse": [{"anchor": a["anchor"], "reason": a["reason"]} for a in anchors if a["status"] == "compatible-unverified"],
        "quarantinedStaleAnchors": [{"anchor": a["anchor"], "reason": a["reason"]} for a in anchors if a["status"] == "stale"],
        "rejectedApproaches": [],
    }
    seen_rejected = set()
    for text, src in [(t, "user") for t in (rejected or [])] + [(r["text"], f"{r['source']}:{r['line']}") for r in dead_ends]:
        text = _clip(str(text), _HANDOVER_TEXT_LIMIT)
        if text and text.lower() not in seen_rejected:
            seen_rejected.add(text.lower())
            lists["rejectedApproaches"].append({"approach": text, "source": src})
    omitted = {k: max(0, len(v) - _HANDOVER_LIST_CAP) for k, v in lists.items()}
    lists = {k: v[:_HANDOVER_LIST_CAP] for k, v in lists.items()}
    counts = _rule_counts(constraints)
    proof = {
        "modifiedFilesCount": _git_modified_count(root),
        "anchorHealth": _summarize(anchors),
        "staleRulesBlocked": counts["stale"],
        "unanchoredRulesOmitted": counts["unanchored"],
    }
    next_action = _clip(next_step, _NEXT_ACTION_LIMIT) if next_step else None
    # Trim the least safety-critical items first until the rendered capsule fits the token budget.
    drop_order = ("reverifyBeforeUse", "quarantinedStaleAnchors", "activeConstraints", "rejectedApproaches")
    while True:
        capsule = {"schema": HANDOVER_SCHEMA, "repoHead": head, **lists, "executionProof": proof,
                   "nextAction": next_action, "authorityBoundary": AUTHORITY_BOUNDARY, "omitted": dict(omitted)}
        signature = handover_signature(capsule)
        markdown = _render_handover(capsule, signature)
        tokens = estimate_tokens(markdown)
        victim = next((k for k in drop_order if lists[k]), None)
        if tokens < HANDOVER_TOKEN_BUDGET or victim is None:
            break
        lists[victim] = lists[victim][:-1]
        omitted[victim] += 1

    actions: List[Dict[str, str]] = []
    if write:
        page = "# Memlet Handover\n\n" + markdown
        op = _write_generated_page(root, HANDOVER_PAGE, page, dry_run=False)
        actions.append({"op": op, "path": HANDOVER_PAGE})
        ledger = root / LEDGER_PATH
        if ledger.is_symlink():
            actions.append({"op": "kept", "path": LEDGER_PATH})
        else:
            existed = ledger.is_file()
            now = _utc_now()
            with contextlib.closing(sqlite3.connect(str(ledger))) as conn:
                with conn:
                    _ledger_ready(conn)
                    _sync_snapshots(conn, [a for a in anchors if a["symbol"]], now)
                    _record_page(conn, HANDOVER_PAGE, _page_body_sha256(page), "handover", now)
            actions.append({"op": "update" if existed else "create", "path": LEDGER_PATH})

    report: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "command": "handover",
        "repoPath": str(root),
        **capsule,
        "capsuleSignature": signature,
        "signedFields": list(HANDOVER_SIGNED_FIELDS),
        "tokenEstimate": tokens,
        "tokenBudget": HANDOVER_TOKEN_BUDGET,
        "written": bool(write),
        "actions": actions,
    }
    if (output_format or "json").lower() == "markdown":
        report["formatted"] = markdown
    return report


# ---------------------------------------------------------------------------
# Reproducible head-to-head refactoring & handover benchmark (`memlet benchmark`).

BENCHMARK_ID = "memlet-refactoring-handover/v1"
BENCH_NOW = "2026-10-29"  # fixed clock so the unverified wiki's aging rule is reproducible
BENCH_ENTRY_DATE = "2026-10-20"
BENCH_AGING_DAYS = 90
BENCHMARK_MODES = (
    ("raw-markdown", "Raw Markdown", "Static CLAUDE.md / .cursorrules injected verbatim; no code check."),
    ("unverified-wiki", "Unverified Wiki", "Timestamped Markdown wiki with 90-day aging and a file-exists check; "
                                           "no AST, signature, or required-parameter check."),
    ("memlet", "Memlet", "AST anchors + reqSigHash pins + handover quarantine."),
)
BENCHMARK_SCENARIOS: Tuple[Dict[str, Any], ...] = (
    {
        "id": "signature-break",
        "title": "Breaking change: def charge(user_id) -> def charge(account_id, currency)",
        "before": {"billing.py": "def charge(user_id):\n    return {'user': user_id}\n"},
        "after": {"billing.py": "def charge(account_id, currency):\n    return {'account': account_id, 'currency': currency}\n"},
        "rules": ["Charge customers with `billing.py::charge` as `charge(user_id)`."],
        "stale": ["billing.py::charge"],
    },
    {
        "id": "optional-arg",
        "title": "Optional argument: def route(task) -> def route(task, effort=\"high\")",
        "before": {"router.py": "def route(task):\n    return task\n"},
        "after": {"router.py": "def route(task, effort=\"high\"):\n    return task, effort\n"},
        "rules": ["Dispatch every task through `router.py::route`."],
        "compatible": ["router.py::route"],
    },
    {
        "id": "file-move",
        "title": "File move: legacy_auth.py -> auth/session.py",
        "before": {"legacy_auth.py": "def create_session(user):\n    return user\n"},
        "after": {"legacy_auth.py": None, "auth/session.py": "def create_session(user):\n    return user\n"},
        "rules": ["Create login sessions with `legacy_auth.py::create_session`."],
        "stale": ["legacy_auth.py::create_session"],
    },
    {
        "id": "cross-vendor-handover",
        "title": "Cross-vendor handover after a refactor, with recorded dead-ends",
        "before": {"payments.py": "def refund(charge_id):\n    return charge_id\n\n\ndef legacy_retry(charge_id):\n    return refund(charge_id)\n"},
        "after": {"payments.py": "def refund(charge_id):\n    return charge_id\n"},
        "rules": ["Issue refunds via `payments.py::refund`.", "Retry failed refunds with `payments.py::legacy_retry`."],
        "stale": ["payments.py::legacy_retry"],
        "deadEnds": [
            "Retrying refunds inside the webhook handler double-charges customers.",
            "Mocking the payment gateway in integration tests hid the idempotency bug.",
        ],
        "handover": True,
    },
)


def _apply_files(root: Path, files: Dict[str, Optional[str]]) -> None:
    for rel, text in files.items():
        target = root / rel
        if text is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


def _rate(hit: int, total: int) -> float:
    return round(hit / total, 4) if total else 0.0


def _days_between(a: str, b: str) -> int:
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def _bench_raw(root: Path, sc: Dict[str, Any]) -> Dict[str, Any]:
    text = _read_text(root / "CLAUDE.md")
    injected = [r["text"] for r in _extract_rules(text, "CLAUDE.md")]
    return {"injected": injected, "handoverContext": text, "handoverActive": injected, "rejected": []}


def _bench_unverified_wiki(root: Path, sc: Dict[str, Any]) -> Dict[str, Any]:
    entries = [{"text": t, "updated": BENCH_ENTRY_DATE, "kind": "rule"} for t in sc["rules"]]
    entries += [{"text": t, "updated": BENCH_ENTRY_DATE, "kind": "note"} for t in sc.get("deadEnds", [])]
    kept = []
    for e in entries:
        fresh = _days_between(e["updated"], BENCH_NOW) <= BENCH_AGING_DAYS
        paths_ok = all((root / p).exists() for p, _sym in _line_refs(e["text"]))
        if fresh and paths_ok:
            kept.append(e)
    injected = [e["text"] for e in kept if e["kind"] == "rule"]
    context = "\n".join(e["text"] for e in kept)
    return {"injected": injected, "handoverContext": context, "handoverActive": injected, "rejected": []}


def _bench_memlet(root: Path, sc: Dict[str, Any]) -> Dict[str, Any]:
    imported = import_rules(root, source="claude-md", dry_run=True)
    injected = [r["text"] for r in imported["rules"] if r["status"] != "stale"]
    out: Dict[str, Any] = {"injected": injected, "handoverContext": "", "handoverActive": injected, "rejected": []}
    if sc.get("handover"):
        cap = build_handover(root, next_step="Finish the refund flow refactor.")
        out["handoverContext"] = _render_handover(cap, cap["capsuleSignature"])
        out["handoverActive"] = [a["rule"] for a in cap["activeConstraints"]]
        out["rejected"] = [a["approach"] for a in cap["rejectedApproaches"]]
        out["tokenEstimate"] = cap["tokenEstimate"]
    return out


def _score_mode(mode: str, sc: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    def mentions(texts: Iterable[str], anchor: str) -> bool:
        return any(anchor in t for t in texts)

    stale = sc.get("stale", [])
    compatible = sc.get("compatible", [])
    score = {
        "scenario": sc["id"],
        "staleInjected": sum(mentions(result["injected"], a) for a in stale),
        "staleTotal": len(stale),
        "falsePositives": sum(not mentions(result["injected"], a) for a in compatible),
        "compatibleTotal": len(compatible),
        "handoverPrevented": 0,
        "handoverHazards": 0,
    }
    if sc.get("handover"):
        hazards = 0
        prevented = 0
        for dead_end in sc.get("deadEnds", []):
            hazards += 1
            carried = dead_end in result["rejected"] if mode == "memlet" else dead_end in result["handoverContext"]
            prevented += int(carried)
        for anchor in stale:
            hazards += 1
            prevented += int(not mentions(result["handoverActive"], anchor))
        score.update(handoverPrevented=prevented, handoverHazards=hazards)
    return score


def run_refactoring_comparison_benchmark(output_format: str = "json") -> Dict[str, Any]:
    """Replay 4 refactoring scenarios in a temp dir and score raw Markdown, an unverified wiki, and Memlet.

    Deterministic apart from `elapsedMs`; no network, no LLM, nothing written outside the temp dir."""
    runners = {"raw-markdown": _bench_raw, "unverified-wiki": _bench_unverified_wiki, "memlet": _bench_memlet}
    scores: Dict[str, List[Dict[str, Any]]] = {m: [] for m, _l, _d in BENCHMARK_MODES}
    elapsed: Dict[str, float] = {m: 0.0 for m in scores}
    with tempfile.TemporaryDirectory(prefix="memlet-bench-") as td:
        for sc in BENCHMARK_SCENARIOS:
            root = Path(td, sc["id"]).resolve()
            root.mkdir()
            _apply_files(root, sc["before"])
            (root / "CLAUDE.md").write_text("# Rules\n\n" + "".join(f"- {r}\n" for r in sc["rules"]), encoding="utf-8")
            init_project(root)  # pins the pre-refactor AST / signature hashes
            if sc.get("deadEnds"):
                notes = "# Session notes\n\n## Dead-Ends\n\n" + "".join(f"- {d}\n" for d in sc["deadEnds"])
                (root / WIKI_DIR / "session.md").write_text(notes, encoding="utf-8")
            _apply_files(root, sc["after"])
            for mode, runner in runners.items():
                started = time.perf_counter()
                result = runner(root, sc)
                elapsed[mode] += (time.perf_counter() - started) * 1000
                scores[mode].append(_score_mode(mode, sc, result))

    modes = []
    for mode, label, description in BENCHMARK_MODES:
        rows = scores[mode]
        total = {k: sum(r[k] for r in rows) for k in ("staleInjected", "staleTotal", "falsePositives",
                                                       "compatibleTotal", "handoverPrevented", "handoverHazards")}
        modes.append({
            "mode": mode,
            "label": label,
            "description": description,
            "staleProcedureInjectionRate": _rate(total["staleInjected"], total["staleTotal"]),
            "staleProceduresInjected": total["staleInjected"],
            "staleProceduresTotal": total["staleTotal"],
            "optionalArgFalsePositiveRate": _rate(total["falsePositives"], total["compatibleTotal"]),
            "optionalArgFalsePositives": total["falsePositives"],
            "optionalArgEvolutionsTotal": total["compatibleTotal"],
            "handoverDeadEndPreventionRate": _rate(total["handoverPrevented"], total["handoverHazards"]),
            "handoverHazardsPrevented": total["handoverPrevented"],
            "handoverHazardsTotal": total["handoverHazards"],
            "externalLlmCalls": 0,
            "elapsedMs": round(elapsed[mode], 2),
            "perScenario": rows,
        })
    digest_src = [{k: v for k, v in m.items() if k != "elapsedMs"} for m in modes]
    report: Dict[str, Any] = {
        "ok": True,
        "exitCode": 0,
        "command": "benchmark",
        "benchmark": BENCHMARK_ID,
        "fixture": f"self-contained temp dir, {len(BENCHMARK_SCENARIOS)} scenarios, fixed clock {BENCH_NOW}",
        "scenarios": [{"id": s["id"], "title": s["title"], "staleAnchors": s.get("stale", []),
                       "compatibleAnchors": s.get("compatible", []), "deadEnds": len(s.get("deadEnds", []))}
                      for s in BENCHMARK_SCENARIOS],
        "modes": modes,
        "externalLlmCalls": 0,
        "resultDigest": "sha256:" + hashlib.sha256(json.dumps(digest_src, sort_keys=True).encode("utf-8")).hexdigest(),
    }
    if (output_format or "json").lower() == "markdown":
        report["formatted"] = format_benchmark_markdown(report)
    return report


def format_benchmark_markdown(report: Dict[str, Any]) -> str:
    def pct(rate: float, hit: int, total: int) -> str:
        return f"{rate * 100:.1f}% ({hit}/{total})".replace(".0%", "%")

    lines = [
        "## Memlet Refactoring & Handover Benchmark",
        "",
        f"- **Benchmark**: `{report['benchmark']}` ({report['fixture']})",
        f"- **External LLM calls**: {report['externalLlmCalls']}",
        f"- **Result digest**: `{report['resultDigest']}`",
        "",
        "| Mode | Stale procedure injection | Optional-arg false positives | Handover dead-end prevention | Elapsed |",
        "| :--- | :---: | :---: | :---: | ---: |",
    ]
    for m in report["modes"]:
        lines.append(
            f"| {m['label']} | {pct(m['staleProcedureInjectionRate'], m['staleProceduresInjected'], m['staleProceduresTotal'])} | "
            f"{pct(m['optionalArgFalsePositiveRate'], m['optionalArgFalsePositives'], m['optionalArgEvolutionsTotal'])} | "
            f"{pct(m['handoverDeadEndPreventionRate'], m['handoverHazardsPrevented'], m['handoverHazardsTotal'])} | "
            f"{m['elapsedMs']:.1f} ms |"
        )
    lines += ["", "Scenarios:", ""] + [f"{i}. {s['title']}" for i, s in enumerate(report["scenarios"], start=1)]
    lines += ["", "Modes:", ""] + [f"- **{m['label']}**: {m['description']}" for m in report["modes"]]
    return "\n".join(lines) + "\n"
