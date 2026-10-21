"""VibeRescue — Stuck Project Diagnostic, Secret-Masked Env Audit & Handoff Bundler.

Addresses the #1 and #3 validated Vibe Coder pain points (Report 1 `P1` & `P3`):
- `P1`: "Chat says fixed, but my local/deployed environment still fails."
- `P3`: "Post-export deployment, Supabase RLS, `.env` mismatch, and freelancer handoff cliff."

Key Guarantees:
1. Read-Only & Zero Secret Leakage: Never reads or emits raw secret values from `.env*` files.
   Only variable names (keys) and structural presence/empty status are recorded.
2. High-Signal Blocker Detection:
   - `missing_env_key`: Code references `process.env.X` / `import.meta.env.X` / `os.environ['X']`
     or `.env.example` declares `X`, but local `.env` / `.env.local` is missing `X`.
   - `client_secret_exposure`: Server-only secret key (`SERVICE_ROLE`, `SECRET_KEY`, `DATABASE_URL`,
     `PRIVATE_KEY`, `OPENAI_API_KEY`, `STRIPE_SECRET`) referenced inside client-side UI code or
     exposed via `NEXT_PUBLIC_` / `VITE_` prefixes.
   - `supabase_rls_or_auth_gap`: SQL migrations create tables (`CREATE TABLE`) without
     `ENABLE ROW LEVEL SECURITY` or use overly permissive `USING (true)` policies.
   - `competing_lockfiles`: Multiple package lockfiles (`package-lock.json`, `pnpm-lock.yaml`,
     `yarn.lock`, `bun.lockb`) co-existing and causing cloud deploy build mismatches.
   - `instruction_or_script_drift`: Integrates `audit_repository` to catch broken `CLAUDE.md` /
     `AGENTS.md` / `package.json` script contracts.
3. Outputs both a NOMAD-style `RESCUE_BRIEF.md` (for human developers or agent handoff) and a
   surgical `ONE_PROMPT_RESCUE.md` repair prompt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from .audit import EXCLUDED_DIRS, audit_repository

ENV_FILE_CANDIDATES = (
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.example",
    ".env.sample",
    ".env.template",
)

LOCKFILE_CANDIDATES = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "bun.lock",
)

DEPLOY_CONFIG_CANDIDATES = (
    "vercel.json",
    "netlify.toml",
    "fly.toml",
    "render.yaml",
    "railway.json",
    "Dockerfile",
    "docker-compose.yml",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "vite.config.ts",
    "vite.config.js",
    "supabase/config.toml",
    "prisma/schema.prisma",
)

CODE_SCAN_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".svelte", ".vue"}

# Matches process.env.KEY, import.meta.env.KEY, os.environ.get("KEY"), os.environ["KEY"], os.getenv("KEY")
PROCESS_ENV_RE = re.compile(
    r"\b(?:process\.env|import\.meta\.env)\.([A-Z][A-Z0-9_]{2,})\b"
)
PY_ENV_RE = re.compile(
    r"\bos\.(?:environ\.get|getenv)\s*\(\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]|"
    r"\bos\.environ\s*\[\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]\s*\]"
)

ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

BUILTIN_ENV_KEYS = {
    "NODE_ENV",
    "TZ",
    "PWD",
    "HOME",
    "PATH",
    "PORT",
    "CI",
    "VERCEL",
    "VERCEL_ENV",
    "VERCEL_URL",
    "BASE_URL",
    "MODE",
    "DEV",
    "PROD",
    "SSR",
}

SENSITIVE_SERVER_KEY_PATTERNS = (
    "SERVICE_ROLE",
    "SECRET_KEY",
    "PRIVATE_KEY",
    "DATABASE_URL",
    "DIRECT_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "STRIPE_SECRET",
    "STRIPE_WEBHOOK_SECRET",
    "SUPABASE_SERVICE_ROLE_KEY",
    "JWT_SECRET",
    "SESSION_SECRET",
    "ADMIN_PASSWORD",
)

CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[A-Za-z0-9_\"']+\.)?([A-Za-z0-9_\"']+)",
    re.IGNORECASE,
)
ENABLE_RLS_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:ONLY\s+)?(?:[A-Za-z0-9_\"']+\.)?([A-Za-z0-9_\"']+)\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)
PERMISSIVE_POLICY_RE = re.compile(
    r"CREATE\s+POLICY\s+[^\n;]+USING\s*\(\s*true\s*\)",
    re.IGNORECASE | re.DOTALL,
)

SECRET_VALUE_REDACT_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}|"
    r"postgres(?:ql)?://[^\s'\"`]+|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})"
)


def mask_sensitive_text(text: str) -> str:
    """Redact any accidental token or connection string pattern from free text."""
    if not text:
        return ""
    return SECRET_VALUE_REDACT_RE.sub("[REDACTED_SECRET]", text)


def _parse_env_keys_only(env_path: Path) -> Dict[str, str]:
    """Return {KEY: 'set' | 'empty' | 'placeholder'} without ever storing the value."""
    result: Dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return result
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = ENV_LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1].strip()
        if not val:
            result[key] = "empty"
        elif any(tok in val.lower() for tok in ("your_", "your-", "xxx", "placeholder", "changeme", "<", "todo")):
            result[key] = "placeholder"
        else:
            result[key] = "set"
    return result


def _is_client_side_file(rel_path: str, content: str) -> bool:
    norm = rel_path.replace("\\", "/")
    lower_path = norm.lower()
    if any(seg in lower_path for seg in ("/api/", "/server/", "server.", ".server.", "/migrations/", "/scripts/", "middleware.")):
        return False
    if re.search(r"^\s*['\"]use client['\"]", content, re.MULTILINE):
        return True
    if norm.endswith((".tsx", ".jsx", ".vue", ".svelte")):
        if any(seg in lower_path for seg in ("components/", "pages/", "views/", "client/", "src/ui/")):
            return True
    return False


def _detect_stack(root: Path) -> Dict[str, Any]:
    stack: List[str] = []
    frameworks: List[str] = []
    database: List[str] = []
    pkg_json_path = root / "package.json"
    scripts: List[str] = []
    if pkg_json_path.exists():
        stack.append("Node/JS")
        try:
            pkg = json.loads(pkg_json_path.read_text(encoding="utf-8", errors="ignore"))
            deps = {}
            for sec in ("dependencies", "devDependencies"):
                if isinstance(pkg.get(sec), dict):
                    deps.update(pkg[sec])
            scripts = sorted((pkg.get("scripts") or {}).keys())
            if "next" in deps:
                frameworks.append("Next.js")
            if "react" in deps:
                frameworks.append("React")
            if "vite" in deps:
                frameworks.append("Vite")
            if "@supabase/supabase-js" in deps or "@supabase/ssr" in deps:
                database.append("Supabase")
            if "prisma" in deps or "@prisma/client" in deps:
                database.append("Prisma")
            if "drizzle-orm" in deps:
                database.append("Drizzle")
            if "stripe" in deps:
                frameworks.append("Stripe")
            if "tailwindcss" in deps:
                frameworks.append("TailwindCSS")
        except Exception:
            pass
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        stack.append("Python")
    if (root / "supabase").is_dir():
        if "Supabase" not in database:
            database.append("Supabase")
    if (root / "prisma").is_dir():
        if "Prisma" not in database:
            database.append("Prisma")
    return {
        "languages": stack or ["Unknown"],
        "frameworks": frameworks,
        "database": database,
        "npmScripts": scripts,
    }


def _git_summary(root: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "isGitRepo": False,
        "branch": None,
        "headSha": None,
        "dirtyFileCount": 0,
        "modifiedFiles": [],
    }
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if proc.returncode != 0:
            return info
        info["isGitRepo"] = True
        info["branch"] = proc.stdout.strip()
        sha_proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if sha_proc.returncode == 0:
            info["headSha"] = sha_proc.stdout.strip()
        st_proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if st_proc.returncode == 0:
            lines = [ln.strip() for ln in st_proc.stdout.splitlines() if ln.strip()]
            info["dirtyFileCount"] = len(lines)
            info["modifiedFiles"] = [ln[3:].strip() if len(ln) > 3 else ln for ln in lines[:25]]
    except Exception:
        pass
    return info


def build_rescue_bundle(
    repo_path: str | Path,
    write_files: bool = False,
    output_dir: Optional[str | Path] = None,
    output_format: str = "json",
) -> Dict[str, Any]:
    """Scan a project directory and build a secret-safe VibeRescue diagnostic & handoff bundle."""
    started = time.perf_counter()
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Target repository directory does not exist: {root}")

    stack_info = _detect_stack(root)
    git_info = _git_summary(root)

    # 1. Inspect .env* files (keys only, zero secret values)
    env_files_found: Dict[str, Dict[str, str]] = {}
    for env_name in ENV_FILE_CANDIDATES:
        ep = root / env_name
        if ep.exists() and ep.is_file():
            env_files_found[env_name] = _parse_env_keys_only(ep)

    active_env_keys: Set[str] = set()
    for env_name in (".env", ".env.local", ".env.development", ".env.production"):
        for k, status in env_files_found.get(env_name, {}).items():
            if status == "set":
                active_env_keys.add(k)

    example_env_keys: Set[str] = set()
    for env_name in (".env.example", ".env.sample", ".env.template"):
        example_env_keys.update(env_files_found.get(env_name, {}).keys())

    # 2. Scan code for referenced environment variables & client secret exposures
    code_env_refs: Dict[str, List[str]] = {}
    blockers: List[Dict[str, Any]] = []
    scanned_files = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".git")]
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if fpath.is_symlink():
                continue
            rel = str(fpath.relative_to(root)).replace("\\", "/")
            ext = fpath.suffix.lower()
            if ext in CODE_SCAN_EXTENSIONS:
                scanned_files += 1
                try:
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                is_client = _is_client_side_file(rel, content)
                for lineno, line in enumerate(content.splitlines(), start=1):
                    for m in PROCESS_ENV_RE.finditer(line):
                        key = m.group(1)
                        if key not in BUILTIN_ENV_KEYS:
                            code_env_refs.setdefault(key, []).append(f"{rel}:{lineno}")
                        if is_client and any(pat in key.upper() for pat in SENSITIVE_SERVER_KEY_PATTERNS):
                            blockers.append({
                                "kind": "client_secret_exposure",
                                "severity": "critical",
                                "file": rel,
                                "line": lineno,
                                "key": key,
                                "message": f"Server secret `{key}` is referenced inside client-side file `{rel}:{lineno}`.",
                                "fixHint": f"Move `{key}` usage to a server route/action (`app/api/...` or server component) and never expose it with `NEXT_PUBLIC_` or `VITE_`.",
                            })
                    for m in PY_ENV_RE.finditer(line):
                        key = m.group(1) or m.group(2)
                        if key and key not in BUILTIN_ENV_KEYS:
                            code_env_refs.setdefault(key, []).append(f"{rel}:{lineno}")

            # 3. Check SQL migrations for Supabase RLS gaps
            if ext == ".sql" and ("migration" in rel.lower() or "supabase" in rel.lower() or "schema" in rel.lower()):
                try:
                    sql_text = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                created_tables = {m.group(1).strip("\"'").lower() for m in CREATE_TABLE_RE.finditer(sql_text)}
                rls_tables = {m.group(1).strip("\"'").lower() for m in ENABLE_RLS_RE.finditer(sql_text)}
                unprotected = sorted(created_tables - rls_tables)
                if unprotected:
                    blockers.append({
                        "kind": "supabase_rls_or_auth_gap",
                        "severity": "critical",
                        "file": rel,
                        "line": 1,
                        "tables": unprotected,
                        "message": f"SQL migration `{rel}` creates table(s) `{', '.join(unprotected)}` without `ENABLE ROW LEVEL SECURITY`.",
                        "fixHint": f"Add `ALTER TABLE <table_name> ENABLE ROW LEVEL SECURITY;` and explicit `auth.uid()` policies in `{rel}`.",
                    })
                if PERMISSIVE_POLICY_RE.search(sql_text):
                    blockers.append({
                        "kind": "supabase_rls_or_auth_gap",
                        "severity": "warning",
                        "file": rel,
                        "line": 1,
                        "message": f"SQL file `{rel}` contains an unrestricted `USING (true)` policy.",
                        "fixHint": "Verify whether `USING (true)` is intentional for public read-only data or if it leaks tenant rows.",
                    })

    # 4. Check missing or placeholder env keys
    all_required_keys = sorted((set(code_env_refs.keys()) | example_env_keys) - BUILTIN_ENV_KEYS)
    missing_env_keys: List[Dict[str, Any]] = []
    for key in all_required_keys:
        if any(pat in key.upper() for pat in SENSITIVE_SERVER_KEY_PATTERNS) and key.startswith(("NEXT_PUBLIC_", "VITE_")):
            blockers.append({
                "kind": "client_secret_exposure",
                "severity": "critical",
                "file": (code_env_refs.get(key) or [".env"])[0].split(":")[0],
                "line": 1,
                "key": key,
                "message": f"Public prefix `{key}` wraps a sensitive secret pattern.",
                "fixHint": f"Rename `{key}` to remove `NEXT_PUBLIC_`/`VITE_` and access it only on the server.",
            })
        if env_files_found and key not in active_env_keys:
            refs = code_env_refs.get(key, [".env.example"])
            missing_env_keys.append({"key": key, "referencedAt": refs[:5]})
            blockers.append({
                "kind": "missing_env_key",
                "severity": "critical" if key in code_env_refs else "warning",
                "file": refs[0].split(":")[0],
                "line": int(refs[0].split(":")[1]) if ":" in refs[0] and refs[0].split(":")[1].isdigit() else 1,
                "key": key,
                "message": f"Environment variable `{key}` is required by `{refs[0]}` but is missing or empty in local `.env`/`.env.local`.",
                "fixHint": f"Set `{key}` in `.env.local` and in your hosting provider's Environment Variables settings (Vercel/Netlify/Supabase).",
            })

    # 5. Check competing lockfiles (classic P1 "works locally, fails in deploy" culprit)
    present_lockfiles = [lf for lf in LOCKFILE_CANDIDATES if (root / lf).exists()]
    if len(present_lockfiles) > 1:
        blockers.append({
            "kind": "competing_lockfiles",
            "severity": "warning",
            "file": present_lockfiles[0],
            "line": 1,
            "lockfiles": present_lockfiles,
            "message": f"Multiple competing lockfiles detected: `{', '.join(present_lockfiles)}`.",
            "fixHint": "Keep only ONE lockfile matching your deployment package manager so Vercel/Netlify does not install mismatched dependency trees.",
        })

    # 6. Include instruction/script drift from `audit_repository`
    audit_res = audit_repository(root, output_format="json")
    for af in audit_res.get("findings", [])[:15]:
        rule_id = af.get("ruleId") or af.get("kind") or "drift"
        blockers.append({
            "kind": f"instruction_drift:{rule_id}",
            "severity": "critical" if af.get("severity") == "error" else af.get("severity", "warning"),
            "file": af.get("docPath", "README.md"),
            "line": af.get("line", 1),
            "target": af.get("targetPath") or af.get("target") or "",
            "message": af.get("message", "Instruction drift detected."),
            "fixHint": af.get("remediation") or af.get("suggestion") or "Align documentation anchor with current codebase.",
        })

    deploy_configs = [cfg for cfg in DEPLOY_CONFIG_CANDIDATES if (root / cfg).exists()]
    critical_count = sum(1 for b in blockers if b.get("severity") == "critical")
    warning_count = sum(1 for b in blockers if b.get("severity") != "critical")

    if critical_count == 0 and warning_count == 0:
        readiness = "CLEAN_READY"
        complexity = "NONE"
    elif critical_count <= 2 and len(blockers) <= 5:
        readiness = "NEEDS_ONE_PROMPT_RESCUE"
        complexity = "LOW_TO_MEDIUM"
    else:
        readiness = "HIGH_RISK_HANDOFF_RECOMMENDED"
        complexity = "HIGH_RESCUE_OR_HANDOFF"

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    bundle_id = hashlib.sha256(f"{root}:{git_info.get('headSha')}:{len(blockers)}".encode("utf-8")).hexdigest()[:12]

    bundle: Dict[str, Any] = {
        "ok": True,
        "bundleId": f"rescue-{bundle_id}",
        "repoName": root.name,
        "repoPath": str(root),
        "scannedCodeFiles": scanned_files,
        "elapsedMs": elapsed_ms,
        "secretSafety": {
            "rawEnvValuesIncluded": False,
            "redactionPolicy": "strict_keys_only",
        },
        "stack": stack_info,
        "git": git_info,
        "deployConfigs": deploy_configs,
        "presentLockfiles": present_lockfiles,
        "envAudit": {
            "envFilesDetected": list(env_files_found.keys()),
            "requiredKeys": all_required_keys,
            "activeSetKeyCount": len(active_env_keys),
            "missingOrEmptyKeys": missing_env_keys,
        },
        "summary": {
            "readiness": readiness,
            "estimatedComplexity": complexity,
            "totalBlockers": len(blockers),
            "criticalCount": critical_count,
            "warningCount": warning_count,
        },
        "blockers": blockers,
    }

    bundle["rescueBriefMarkdown"] = format_rescue_markdown(bundle)
    bundle["onePromptRescueMarkdown"] = format_one_prompt_rescue(bundle)

    if write_files:
        out_root = Path(output_dir).expanduser().resolve() if output_dir else root
        out_root.mkdir(parents=True, exist_ok=True)
        brief_path = out_root / "RESCUE_BRIEF.md"
        prompt_path = out_root / "ONE_PROMPT_RESCUE.md"
        json_path = out_root / "rescue-bundle.json"
        brief_path.write_text(bundle["rescueBriefMarkdown"], encoding="utf-8")
        prompt_path.write_text(bundle["onePromptRescueMarkdown"], encoding="utf-8")
        serializable = {k: v for k, v in bundle.items() if k not in ("rescueBriefMarkdown", "onePromptRescueMarkdown")}
        json_path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")
        bundle["writtenFiles"] = {
            "rescueBrief": str(brief_path),
            "onePromptRescue": str(prompt_path),
            "rescueBundleJson": str(json_path),
        }

    if output_format == "markdown":
        bundle["formatted"] = bundle["rescueBriefMarkdown"]
    elif output_format == "prompt":
        bundle["formatted"] = bundle["onePromptRescueMarkdown"]

    return bundle


def format_rescue_markdown(bundle: Dict[str, Any]) -> str:
    """Render a NOMAD-style 1-page Handoff & Diagnostic Brief (`RESCUE_BRIEF.md`)."""
    summary = bundle.get("summary", {})
    stack = bundle.get("stack", {})
    git_info = bundle.get("git", {})
    env_audit = bundle.get("envAudit", {})
    blockers = bundle.get("blockers", [])

    lines = [
        f"# 🛟 VibeRescue Diagnostic & Handoff Brief (`{bundle.get('repoName', 'project')}`)",
        "",
        f"- **Bundle ID**: `{bundle.get('bundleId')}` · **Readiness**: `{summary.get('readiness')}`",
        f"- **Secret Safety**: `ZERO_SECRET_VALUES_INCLUDED` (Only `.env` key names & structural diffs recorded)",
        f"- **Tech Stack**: `{', '.join(stack.get('languages', []))}` · Frameworks: `{', '.join(stack.get('frameworks', []) or ['None'])}` · DB: `{', '.join(stack.get('database', []) or ['None'])}`",
        f"- **Git State**: Branch `{git_info.get('branch') or 'N/A'}` (`{git_info.get('headSha') or 'uncommitted'}`) · Dirty files: `{git_info.get('dirtyFileCount', 0)}`",
        f"- **Deploy Configs**: `{', '.join(bundle.get('deployConfigs', []) or ['None detected'])}` · Lockfiles: `{', '.join(bundle.get('presentLockfiles', []) or ['None'])}`",
        "",
        "---",
        "",
        f"## 1. Blocker Summary ({summary.get('totalBlockers', 0)} total: {summary.get('criticalCount', 0)} critical, {summary.get('warningCount', 0)} warnings)",
        "",
    ]

    if not blockers:
        lines.append("✅ **No environment, secret exposure, RLS, or instruction drift blockers detected.** This project is ready for deployment or clean handoff.")
    else:
        lines.append("| Severity | Category | Location | Issue & Actionable Fix |")
        lines.append("|---|---|---|---|")
        for b in blockers:
            sev = "🔴 CRITICAL" if b.get("severity") == "critical" else "🟡 WARN"
            loc = f"`{b.get('file', '.')}:{b.get('line', 1)}`"
            msg = mask_sensitive_text(str(b.get("message", "")))
            hint = mask_sensitive_text(str(b.get("fixHint", "")))
            lines.append(f"| {sev} | `{b.get('kind')}` | {loc} | {msg} **→ Fix:** {hint} |")

    lines.extend([
        "",
        "## 2. Environment Variable Contract (Keys Only · Values Redacted)",
        "",
        f"- **Detected `.env` files**: `{', '.join(env_audit.get('envFilesDetected', []) or ['None'])}`",
        f"- **Required Keys in Code/Example ({len(env_audit.get('requiredKeys', []))})**: `{', '.join(env_audit.get('requiredKeys', []) or ['None'])}`",
    ])
    missing = env_audit.get("missingOrEmptyKeys", [])
    if missing:
        lines.append("- **Missing or Unset Keys**:")
        for item in missing:
            lines.append(f"  - ❌ `{item['key']}` (referenced at `{', '.join(item.get('referencedAt', []))}`)")
    else:
        lines.append("- ✅ All referenced environment variable keys are accounted for.")

    lines.extend([
        "",
        "## 3. Next Step (Self-Rescue or Handoff)",
        "",
        "1. **Fix with AI Coding Agent**: Paste `ONE_PROMPT_RESCUE.md` directly into Claude Code, Cursor, or Codex to fix only the blockers listed above without touching unrelated working files.",
        "2. **Hand Off to Engineer / Rescue Service**: Share this `RESCUE_BRIEF.md` and `rescue-bundle.json` (0 secrets included) so another developer can reproduce and resolve the issue in under 5 minutes.",
        "",
    ])
    return "\n".join(lines)


def format_one_prompt_rescue(bundle: Dict[str, Any]) -> str:
    """Generate a surgical, copy-pasteable repair prompt for Claude Code / Cursor / Codex."""
    blockers = bundle.get("blockers", [])
    repo_name = bundle.get("repoName", "project")
    if not blockers:
        return (
            f"# One-Prompt Rescue (`{repo_name}`)\n\n"
            "No blockers were detected by VibeRescue. Your environment keys, lockfiles, and instruction anchors are aligned.\n"
        )

    tasks_md = []
    for idx, b in enumerate(blockers, start=1):
        tasks_md.append(
            f"{idx}. **[{b.get('severity', 'warning').upper()} · `{b.get('kind')}`]** at `{b.get('file', '.')}:{b.get('line', 1)}`\n"
            f"   - Problem: {mask_sensitive_text(str(b.get('message', '')))}\n"
            f"   - Required Fix: {mask_sensitive_text(str(b.get('fixHint', '')))}"
        )

    tasks_block = "\n".join(tasks_md)
    return f"""# 🛠️ One-Prompt Surgical Rescue (`{repo_name}`)

Copy and paste the prompt block below into **Claude Code**, **Cursor**, or **Codex**:

```markdown
You are executing a bounded VibeRescue surgical repair on `{repo_name}`.

STRICT GUARDRAILS (DO NOT VIOLATE):
1. Do NOT refactor, restyle, or modify any file that is not directly required to resolve the specific blockers listed below.
2. Do NOT hardcode secret values or API keys into source files; keep all secrets in `.env.local` / server environment variables and update `.env.example` with placeholder keys only.
3. After applying each fix, verify that existing build/test scripts in `package.json` / `pyproject.toml` still pass.

BLOCKERS TO RESOLVE ({len(blockers)} items):
{tasks_block}

Please fix these items in order and report the exact file diff and verification command output for each item.
```
"""


def cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="viberescue",
        description="VibeRescue — Secret-Safe Project Blocker Diagnostic & Handoff Bundler",
    )
    sub = parser.add_subparsers(dest="subcommand")

    pack_p = sub.add_parser("pack", help="Scan a project and generate a secret-safe rescue bundle")
    pack_p.add_argument("path", nargs="?", default=".", help="Target repository directory (default: .)")
    pack_p.add_argument("--write", action="store_true", help="Write RESCUE_BRIEF.md, ONE_PROMPT_RESCUE.md, and rescue-bundle.json")
    pack_p.add_argument("--output-dir", default=None, help="Optional directory to write bundle files")
    pack_p.add_argument("--format", default="markdown", choices=["json", "markdown", "prompt"], help="Console output format")

    args = parser.parse_args(argv)
    target = getattr(args, "path", ".") if args.subcommand else "."
    write_files = getattr(args, "write", False)
    out_dir = getattr(args, "output_dir", None)
    fmt = getattr(args, "format", "markdown")

    try:
        res = build_rescue_bundle(target, write_files=write_files, output_dir=out_dir, output_format=fmt)
        if fmt == "json":
            printable = {k: v for k, v in res.items() if k not in ("rescueBriefMarkdown", "onePromptRescueMarkdown")}
            print(json.dumps(printable, indent=2, ensure_ascii=False))
        elif fmt == "prompt":
            print(res["onePromptRescueMarkdown"])
        else:
            print(res["rescueBriefMarkdown"])
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(cli())
