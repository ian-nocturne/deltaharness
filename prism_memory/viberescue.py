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
import shutil
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

CODE_SCAN_EXTENSIONS = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".py", ".svelte", ".vue"}

# Matches process.env.KEY, process.env["KEY"], import.meta.env.KEY, import.meta.env["KEY"]
PROCESS_ENV_RE = re.compile(
    r"\b(?:process\.env|import\.meta\.env)(?:\.([A-Z][A-Z0-9_]{2,})\b|\[\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]\s*\])"
)
# Heuristic fallback only: an env object read that is not `.KEY` / `?.KEY` / `["KEY"]` (whole-object spread,
# alias or computed key). In a client file this is unverifiable and reported fail-closed.
DYNAMIC_ENV_RE = re.compile(
    r"\b(?:process\.env|import\.meta\.env)\b(?!\s*(?:\?\.)?\s*\.\s*[A-Za-z_$])(?!\s*(?:\?\.)?\s*\[\s*(['\"])[A-Za-z_][A-Za-z0-9_]*\1\s*\])"
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
    r"CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:(\"(?:[^\"]|\"\")+\"|[A-Za-z0-9_]+)\.)?(\"(?:[^\"]|\"\")+\"|[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
ENABLE_RLS_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?:(\"(?:[^\"]|\"\")+\"|[A-Za-z0-9_]+)\.)?(\"(?:[^\"]|\"\")+\"|[A-Za-z0-9_]+)\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)
ALTER_RLS_RE = re.compile(
    r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?(?:(\"(?:[^\"]|\"\")+\"|[A-Za-z0-9_]+)\.)?(\"(?:[^\"]|\"\")+\"|[A-Za-z0-9_]+)\s+(ENABLE|DISABLE)\s+ROW\s+LEVEL\s+SECURITY",
    re.IGNORECASE,
)
CREATE_POLICY_ON_RE = re.compile(
    r"CREATE\s+POLICY\s+[\s\S]*?\bON\s+(?:(\"(?:[^\"]|\"\")+\"|[A-Za-z0-9_]+)\.)?(\"(?:[^\"]|\"\")+\"|[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
PERMISSIVE_POLICY_RE = re.compile(
    r"CREATE\s+POLICY\s+[^;]+?\bUSING\s*\(\s*true\s*\)",
    re.IGNORECASE | re.DOTALL,
)

SECRET_VALUE_REDACT_RE = re.compile(
    r"(?<![A-Za-z0-9])(sk_(?:live|test)_[A-Za-z0-9_-]{8,}|rk_(?:live|test)_[A-Za-z0-9_-]{8,}|whsec_[A-Za-z0-9_-]{8,}|"
    r"sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{5,})?|"
    r"postgres(?:ql)?://[^\s'\"`\[\]\\]{3,}|mysql://[^\s'\"`\[\]\\]{3,}|mongodb(?:\+srv)?://[^\s'\"`\[\]\\]{3,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{16,}|glpat-[A-Za-z0-9_-]{16,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|xox[baprse]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}|"
    r"npm_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{30,}|polar_(?:oat|pat|sk)_[A-Za-z0-9_-]{16,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def mask_sensitive_text(text: str) -> str:
    """Redact any accidental token or connection string pattern from free text."""
    if not text:
        return ""
    return SECRET_VALUE_REDACT_RE.sub("[REDACTED_SECRET]", str(text))


# Public alias used by external audits and integrations.
scrub_text_secrets = mask_sensitive_text


def _scrub_recursive(value: Any) -> Any:
    """Recursively scrub all string fields in dicts/lists to guarantee zero token pattern leakage."""
    if isinstance(value, str):
        return mask_sensitive_text(value)
    if isinstance(value, list):
        return [_scrub_recursive(item) for item in value]
    if isinstance(value, dict):
        return {mask_sensitive_text(str(k)): _scrub_recursive(v) for k, v in value.items()}
    return value


def _sql_ident_part(part: Optional[str]) -> str:
    """PostgreSQL identifier rules: "Quoted" keeps its case (with "" unescaped); unquoted folds to lower case."""
    part = part or ""
    if len(part) >= 2 and part.startswith('"') and part.endswith('"'):
        return part[1:-1].replace('""', '"')
    return part.strip('"').lower()


def _sql_table_key(schema: Optional[str], name: Optional[str]) -> str:
    """Schema-qualified table identity as an unambiguous (schema, table) pair encoded as JSON.

    Unqualified names resolve to `public` (Supabase default search_path). Joining with "." would make
    `"a.b".c` and `a."b.c"` collide, so the two parts are never concatenated.
    """
    if not name:
        return ""
    return json.dumps([_sql_ident_part(schema) if schema else "public", _sql_ident_part(name)], ensure_ascii=False)


def _sql_table_label(key: str) -> str:
    """Display name: bare for the default `public` schema, schema-qualified otherwise (quoted when needed)."""
    try:
        schema, name = json.loads(key)
    except (ValueError, TypeError):
        return key

    def q(part: str) -> str:
        return part if re.fullmatch(r"[a-z_][a-z0-9_]*", part) else '"' + part.replace('"', '""') + '"'

    return q(name) if schema == "public" else f"{q(schema)}.{q(name)}"


_SQL_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def blank_sql_non_executable(content: str) -> str:
    """Return SQL with comments, string literals and deferred function bodies blanked.

    Line breaks are preserved so reported line numbers stay exact.
    - `-- ...` and nested `/* ... */` comments are removed.
    - `'...'`, `E'...'` and quoted literals are blanked, so `SELECT 'ALTER TABLE t ENABLE ROW LEVEL SECURITY'`
      is not counted as a real statement.
    - `$tag$ ... $tag$` bodies are blanked (function bodies run later, not at migration time), except the
      body of a `DO` block, which executes immediately and is kept.
    - Double-quoted identifiers are kept.
    """
    out: List[str] = []
    i = 0
    n = len(content)

    def _blank(segment: str) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in segment)

    while i < n:
        ch = content[i]
        nxt = content[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = content.find("\n", i)
            end = n if end == -1 else end
            out.append(_blank(content[i:end]))
            i = end
            continue
        if ch == "/" and nxt == "*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if content.startswith("/*", j):
                    depth += 1
                    j += 2
                elif content.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append(_blank(content[i:j]))
            i = j
            continue
        if ch == '"':
            j = i + 1
            while j < n:
                if content[j] == '"':
                    if j + 1 < n and content[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            out.append(content[i:j + 1])
            i = j + 1
            continue
        if ch == "'":
            backslash_escapes = i > 0 and content[i - 1] in "eE" and (i < 2 or not (content[i - 2].isalnum() or content[i - 2] == "_"))
            j = i + 1
            while j < n:
                if backslash_escapes and content[j] == "\\":
                    j += 2
                    continue
                if content[j] == "'":
                    if j + 1 < n and content[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(_blank(content[i:j + 1]))
            i = j + 1
            continue
        if ch == "$":
            m = _SQL_DOLLAR_TAG_RE.match(content, i)
            if m and (i == 0 or not (content[i - 1].isalnum() or content[i - 1] == "_")):
                tag = m.group(0)
                close = content.find(tag, m.end())
                close = n if close == -1 else close
                body_end = min(n, close + len(tag))
                preceding = "".join(out)[-80:]
                is_do_block = bool(re.search(r"\bDO\s*(?:LANGUAGE\s+\w+\s*)?$", preceding, re.IGNORECASE))
                if is_do_block:
                    out.append(" " * len(tag))
                    out.append(blank_sql_non_executable(content[m.end():close]))
                    out.append(" " * (body_end - close))
                else:
                    out.append(_blank(content[i:body_end]))
                i = body_end
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_code_comments_preserve_lines(content: str, ext: str) -> str:
    """Blank out comments while keeping exact line numbers for accurate line reporting."""
    if ext == ".py":
        lines = []
        for ln in content.splitlines():
            if ln.lstrip().startswith("#"):
                lines.append("")
            else:
                lines.append(re.sub(r"\s+#.*$", "", ln))
        return "\n".join(lines)
    if ext == ".sql":
        return blank_sql_non_executable(content)
    # JS/TS/JSX/TSX/Vue/Svelte
    from .code_anchors import _blank_js_comments_preserve_lines
    blanked, _ = _blank_js_comments_preserve_lines(content)
    return blanked


def _parse_env_keys_only(env_path: Path) -> Dict[str, str]:
    """Return {KEY: 'set' | 'empty' | 'placeholder'} without storing raw secret values."""
    result: Dict[str, str] = {}
    try:
        with env_path.open("r", encoding="utf-8", errors="ignore") as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                m = ENV_LINE_RE.match(line)
                if not m:
                    continue
                key = m.group(1)
                rhs = m.group(2).strip()
                if (rhs.startswith('"') and rhs.endswith('"')) or (rhs.startswith("'") and rhs.endswith("'")):
                    rhs = rhs[1:-1].strip()
                if not rhs:
                    result[key] = "empty"
                elif any(tok in rhs.lower() for tok in ("your_", "your-", "xxx", "placeholder", "changeme", "<", "todo")):
                    result[key] = "placeholder"
                else:
                    result[key] = "set"
    except OSError:
        return result
    return result


def _is_public_env_key(key: str) -> bool:
    return key.startswith(("NEXT_PUBLIC_", "VITE_")) or key == "NODE_ENV"


_DIRECTIVE_RE = re.compile(r"""\s*(['"])((?:\\.|(?!\1).)*)\1\s*;?""")


def _has_use_client_directive(clean_content: str) -> bool:
    """Directive prologue (heuristic): leading string-literal statements, e.g. `"use strict"; "use client";`."""
    pos = 0
    while True:
        m = _DIRECTIVE_RE.match(clean_content, pos)
        if not m:
            return False
        if m.group(2) == "use client":
            return True
        pos = m.end()


def _is_client_side_file(rel_path: str, clean_content: str) -> bool:
    # An explicit "use client" directive always wins over path-based guesses (B77).
    if _has_use_client_directive(clean_content):
        return True
    norm = rel_path.replace("\\", "/")
    lower_path = norm.lower()
    if any(seg in lower_path for seg in ("/api/", "/server/", "server.", ".server.", "/migrations/", "/scripts/", "middleware.")):
        return False
    has_use_client = _has_use_client_directive(clean_content)
    if has_use_client:
        return True
    # Respect Next.js `import "server-only"` guard when `"use client"` is absent
    if re.search(r"(?:import\s+['\"]server-only['\"]|require\s*\(\s*['\"]server-only['\"]\s*\))", clean_content):
        return False
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
            lines = [ln for ln in st_proc.stdout.splitlines() if ln.strip()]
            info["dirtyFileCount"] = len(lines)
            # Porcelain v1: 2 status chars + 1 space + path. Do not strip before slicing (status may start with a space).
            info["modifiedFiles"] = [ln[3:].strip() if len(ln) > 3 else ln.strip() for ln in lines[:25]]
    except Exception:
        pass
    return info


_SQL_U = r"[A-Za-z_\u0080-\uffff][A-Za-z0-9_$\u0080-\uffff]*"
_SQL_Q = r'"(?:[^"]|"")+"'
_SQL_P = rf"(?:{_SQL_Q}|{_SQL_U})"
_SQL_IDENT = rf"(?:({_SQL_P})\s*\.\s*)?({_SQL_P})"


def _sql_part(part: str) -> str:
    if len(part) >= 2 and part.startswith('"') and part.endswith('"'):
        return part[1:-1].replace('""', '"')
    return re.sub(r"[A-Z]", lambda m: m.group(0).lower(), part)  # PostgreSQL folds ASCII only


_SQL_NEUTRAL = [re.compile(x, re.IGNORECASE) for x in (
    r"^CREATE\s+(?:UNIQUE\s+)?INDEX\b", r"^CREATE\s+EXTENSION\b",
    r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE|TRIGGER|VIEW|TYPE|DOMAIN|SEQUENCE|AGGREGATE|OPERATOR)\b",
    r"^CREATE\s+CONSTRAINT\s+TRIGGER\b", r"^CREATE\s+ROLE\b",
    r"^(?:GRANT|REVOKE|COMMENT\s+ON|INSERT|UPDATE|DELETE|TRUNCATE|ANALYZE|VACUUM|NOTIFY|LOCK|BEGIN|START\s+TRANSACTION|COMMIT|END|SAVEPOINT|RELEASE|VALUES)\b",
    r"^ALTER\s+(?:FUNCTION|PROCEDURE|TYPE|SEQUENCE|INDEX|VIEW|DOMAIN|EXTENSION|DEFAULT\s+PRIVILEGES|TRIGGER)\b",
    r"^DROP\s+(?:INDEX|FUNCTION|PROCEDURE|TRIGGER|VIEW|TYPE|SEQUENCE|DOMAIN|EXTENSION|ROLE)\b",
)]


def _sql_event_pass(docs: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Allowlist RLS model across migrations (same rules as the doctor's SQL mode, Round 9).

    Each statement is modelled (tables, RLS, policies, search_path, schemas), known-neutral for table/RLS state,
    or UNVERIFIED (ROLLBACK, SET LOCAL search_path, SELECT INTO, DO/EXECUTE, DROP/ALTER SCHEMA, renames,
    CREATE TABLE AS, unparsed DDL, unknown statement types). Identity is a (schema, table) pair.
    """
    tables: Dict[str, Dict[str, Any]] = {}
    unverified: List[Dict[str, Any]] = []
    permissive: List[Tuple[str, int, str]] = []
    search_path = ["public"]
    flags = re.IGNORECASE
    known_schemas = {"public"}  # plus schemas created in the migrations; others have unknown existence
    ddl_functions: Set[str] = set()  # functions whose body changes tables/RLS/policies or runs dynamic SQL
    ddl_body = re.compile(r"\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|SCHEMA|POLICY)\b|\bEXECUTE\b|\bROW\s+LEVEL\s+SECURITY\b|\bsearch_path\b", flags)
    state = {"unknown": False}

    def eff_path():
        return [x for x in search_path if x not in ("$user", "pg_catalog", "pg_temp")]

    def resolve(schema, name, for_create):
        t = _sql_part(name)
        state["unknown"] = False
        if schema:
            return json.dumps([_sql_part(schema), t], ensure_ascii=False)
        if not for_create and t in temp_tables:
            # PostgreSQL searches pg_temp first unless search_path names it explicitly; then the listed order applies.
            if "pg_temp" not in search_path:
                return json.dumps(["pg_temp", t], ensure_ascii=False)
            for sc in search_path:
                if sc == "pg_temp":
                    return json.dumps(["pg_temp", t], ensure_ascii=False)
                if sc in ("$user", "pg_catalog"):
                    continue
                if sc not in known_schemas:
                    state["unknown"] = True
                    return json.dumps([sc, t], ensure_ascii=False)
                k = json.dumps([sc, t], ensure_ascii=False)
                if k in tables:
                    return k
            return json.dumps(["pg_temp", t], ensure_ascii=False)
        saw_unknown = False
        for sc in eff_path():
            if sc not in known_schemas:
                saw_unknown = True
                if for_create:
                    state["unknown"] = True
                    return json.dumps([sc, t], ensure_ascii=False)
                continue
            k = json.dumps([sc, t], ensure_ascii=False)
            if for_create or k in tables:
                state["unknown"] = saw_unknown
                return k
        state["unknown"] = saw_unknown
        return json.dumps(["public", t], ensure_ascii=False)

    def parse_path(value):
        out = []
        for x in (v.strip() for v in value.split(",")):
            if x:
                out.append(x[1:-1] if x.startswith("'") and x.endswith("'") else _sql_part(x))
        return out

    # Pre-pass: collect function/procedure bodies ($tag$...$tag$ or '...'). A function is unsafe if its body changes
    # tables/RLS, runs dynamic SQL, cannot be read, or (fixed point) calls another unsafe function.
    fn_bodies: Dict[str, Optional[str]] = {}
    for _rel, raw_doc in docs:
        for m in re.finditer(rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+{_SQL_IDENT}", raw_doc, flags):
            name, rest = _sql_part(m.group(2)), raw_doc[m.end():]
            dollar = re.search(r"\bAS\s+(\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)([\s\S]*?)\1", rest, flags)
            quoted = re.search(r"\bAS\s+'((?:[^']|'')*)'", rest, flags)
            end_m = re.search(r";\s*(?:CREATE|ALTER|DROP|SELECT|INSERT|UPDATE|DELETE|GRANT|COMMENT|$)", rest, flags)
            end = end_m.start() if end_m else -1
            picks = sorted(x for x in ((dollar.start(), dollar.group(2)) if dollar else None, (quoted.start(), quoted.group(1).replace("''", "'")) if quoted else None) if x)
            lang = re.search(r"\bLANGUAGE\s+'?([A-Za-z_][A-Za-z0-9_]*)'?", rest if end < 0 else rest[:end], flags)
            analysable = not lang or lang.group(1).lower() in ("sql", "plpgsql")  # C / other languages: body is not SQL
            fn_bodies[name] = picks[0][1] if analysable and picks and (end < 0 or picks[0][0] < end) else None
    for name, body in fn_bodies.items():
        if body is None or ddl_body.search(body):
            ddl_functions.add(name)

    def calls_fn(text, fn):
        return re.search(r'(?:^|[^A-Za-z0-9_$"])"?' + re.escape(fn) + r'"?\s*\(', text, flags) is not None

    changed = True
    while changed:
        changed = False
        for name, body in fn_bodies.items():
            if name not in ddl_functions and body is not None and any(calls_fn(body, fn) for fn in ddl_functions):
                ddl_functions.add(name)
                changed = True
    temp_tables: Set[str] = set()  # unqualified names that resolve to a session temp table first

    for rel, raw in docs:
        sql = blank_sql_non_executable(raw)
        line_of = lambda idx: sql[:idx].count("\n") + 1  # noqa: E731

        def unv(idx, why):
            unverified.append({"file": rel, "line": line_of(idx), "reason": f"SQL {why}"})

        for m in PERMISSIVE_POLICY_RE.finditer(sql):
            pol = re.search(rf"\bON\s+{_SQL_IDENT}", m.group(0), flags)
            permissive.append((rel, line_of(m.start()), _sql_table_label(resolve(pol.group(1), pol.group(2), False)) if pol else ""))
        for m in re.finditer(r"set_config\s*\(\s*'search_path'", raw, flags):
            unv(m.start(), "search_path changed via set_config")
        start = 0
        for i in range(len(sql) + 1):
            if i < len(sql) and sql[i] != ";":
                continue
            chunk = sql[start:i]
            stripped = chunk.strip()
            idx = start + (len(chunk) - len(chunk.lstrip()))
            raw_st = raw[idx:i]
            start = i + 1
            if not stripped:
                continue
            st = stripped
            if not re.match(r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\b", st, flags):
                called = next((fn for fn in ddl_functions if calls_fn(st, fn)), None)
                if called:
                    unv(idx, f"calls function '{called}' whose body changes tables/RLS, runs dynamic SQL or cannot be read")
            m = re.match(rf"^CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_SQL_IDENT}\s*(?:\(|PARTITION\b|OF\b)", st, flags)
            if m:
                key = resolve(m.group(1), m.group(2), True)
                if state["unknown"]:
                    unv(idx, "unqualified CREATE TABLE resolves through a search_path schema not created in these migrations")
                    continue
                tables.setdefault(key, {"file": rel, "line": line_of(idx), "rls": False, "policies": set(), "disabledAt": None})
                continue
            mt = re.match(rf"^CREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP|TEMPORARY)\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_SQL_IDENT}", st, flags)
            if mt:
                temp_tables.add(_sql_part(mt.group(2)))  # session-local, but it shadows unqualified names
                continue
            if re.match(r"^CREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP|TEMPORARY)\s+TABLE\b", st, flags):
                unv(idx, "temporary table name not parsed")
                continue
            if re.match(r"^CREATE\b[^(]*\bTABLE\b", st, flags):
                unv(idx, "CREATE TABLE form not modelled (AS / LIKE-only / unparsed)")
                continue
            m = re.match(rf"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_SQL_IDENT}\s+(ENABLE|DISABLE|FORCE|NO\s+FORCE)\s+ROW\s+LEVEL\s+SECURITY\s*$", st, flags)
            if m:
                t = tables.get(resolve(m.group(1), m.group(2), False))
                act = re.sub(r"\s+", " ", m.group(3).upper())
                if state["unknown"]:
                    unv(idx, "unqualified ALTER TABLE resolves through a search_path schema not created in these migrations")
                    if t is not None:
                        t["unknown"] = True
                elif t is not None:
                    if act == "ENABLE":  # FORCE alone does not enable RLS
                        t["rls"] = True
                    elif act == "DISABLE":
                        t["rls"] = False
                        t["disabledAt"] = (rel, line_of(idx))
                continue
            if re.match(r"^ALTER\s+TABLE\b", st, flags):
                if re.search(r"\bROW\s+LEVEL\s+SECURITY\b|\bRENAME\s+TO\b|\bSET\s+SCHEMA\b", st, flags):
                    unv(idx, "ALTER TABLE combines RLS with other actions or changes table identity")
                    mt = re.match(rf"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{_SQL_IDENT}", st, flags)
                    t = tables.get(resolve(mt.group(1), mt.group(2), False)) if mt else None
                    if t is not None:
                        t["unknown"] = True  # RLS state no longer known: UNVERIFIED, not a FAIL
                continue
            m = re.match(rf"^CREATE\s+POLICY\s+({_SQL_P})\s+ON\s+{_SQL_IDENT}", st, flags)
            if m:
                t = tables.get(resolve(m.group(2), m.group(3), False))
                if state["unknown"]:
                    unv(idx, "unqualified CREATE POLICY resolves through a search_path schema not created in these migrations")
                    if t is not None:
                        t["unknown"] = True
                elif t is not None:
                    t["policies"].add(_sql_part(m.group(1)))
                continue
            m = re.match(rf"^DROP\s+POLICY\s+(?:IF\s+EXISTS\s+)?({_SQL_P})\s+ON\s+{_SQL_IDENT}", st, flags)
            if m:
                t = tables.get(resolve(m.group(2), m.group(3), False))
                if t is not None:
                    t["policies"].discard(_sql_part(m.group(1)))
                continue
            if re.match(r"^ALTER\s+POLICY\b", st, flags):
                if re.search(r"\bRENAME\b", st, flags):
                    unv(idx, "policy rename not modelled")
                continue
            m = re.match(r"^DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\s\S]*?)\s*(?:CASCADE|RESTRICT)?\s*$", st, flags)
            if m:
                for target in (x.strip() for x in m.group(1).split(",")):
                    mm = re.fullmatch(_SQL_IDENT, target)
                    if not mm:
                        unv(idx, "DROP TABLE target not parsed")
                        continue
                    if not mm.group(1) and _sql_part(mm.group(2)) in temp_tables:
                        temp_tables.discard(_sql_part(mm.group(2)))
                        continue
                    tables.pop(resolve(mm.group(1), mm.group(2), False), None)
                continue
            if re.match(r"^SET\s+LOCAL\s+search_path\b", st, flags):
                unv(idx, "SET LOCAL search_path (transaction-scoped) not modelled")
                continue
            m = re.match(r"^SET\s+(?:SESSION\s+)?search_path\s*(?:TO\b|=)", st, flags)
            if m:
                pth = parse_path(raw_st[m.end():])
                if pth:
                    search_path = pth
                else:
                    unv(idx, "search_path value not parsed")
                continue
            if re.match(r"^(?:RESET\s+(?:search_path|ALL)|DISCARD\s+ALL)\b", st, flags):
                search_path = ["public"]
                continue
            m = re.match(rf"^CREATE\s+SCHEMA\s+(?:IF\s+NOT\s+EXISTS\s+)?({_SQL_P})", st, flags)
            if m:
                known_schemas.add(_sql_part(m.group(1)))
                continue
            if re.match(r"^ROLLBACK\b", st, flags):
                unv(idx, "ROLLBACK (undone statements are not modelled)")
                continue
            if re.match(r"^(?:SELECT|WITH)\b", st, flags):
                if re.search(r"\bINTO\b", re.sub(r"\bINSERT\s+INTO\b", "", st, flags=flags), flags):
                    unv(idx, "SELECT INTO creates a table")
                continue
            if re.match(r"^(?:SET|RESET|SHOW)\b", st, flags):
                continue
            if re.match(r"^ALTER\s+(?:ROLE|USER|DATABASE)\b", st, flags):
                if re.search(r"\bsearch_path\b", st, flags):
                    unv(idx, "search_path changed via ALTER ... SET")
                continue
            if not any(rx.match(st) for rx in _SQL_NEUTRAL):
                unv(idx, "statement type not modelled: " + " ".join(st.split()[:3]).upper())
    tables = {k: v for k, v in tables.items() if not v.get("unknown")}
    return {
        "created": {k: (v["file"], v["line"]) for k, v in tables.items()},
        "enabled": {k for k, v in tables.items() if v["rls"]},
        "disabled": {k: v["disabledAt"] for k, v in tables.items() if not v["rls"] and v["disabledAt"]},
        "policies": {k for k, v in tables.items() if v["policies"]},
        "permissive": permissive,
        "unverified": unverified,
    }


JS_AST_EXTENSIONS = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
_JS_ENV_HELPER = Path(__file__).resolve().parent / "js_env_refs.cjs"


def _typescript_env_scan(root: Path, rel_files: List[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Run the TypeScript-AST env scanner. Returns (analysis_info, per_file_results).

    Uses the scanned project's own `typescript` (or DELTAHARNESS_TYPESCRIPT). Without Node or TypeScript the
    caller keeps its heuristic lexer and the bundle says so (`jsAnalysis.mode = heuristic-lexer`).
    """
    info: Dict[str, Any] = {"mode": "heuristic-lexer", "filesAnalyzed": 0}
    if not rel_files:
        info["mode"] = "not-applicable"
        return info, {}
    node = os.environ.get("DELTAHARNESS_NODE") or shutil.which("node")
    if not node or not _JS_ENV_HELPER.is_file():
        info["reason"] = "node not found" if not node else "js_env_refs.cjs missing"
        return info, {}
    try:
        proc = subprocess.run([node, str(_JS_ENV_HELPER), str(root)], input=json.dumps(rel_files), capture_output=True, text=True, timeout=180)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        info["reason"] = f"ast scanner failed: {str(exc)[:120]}"
        return info, {}
    if data.get("mode") != "typescript-ast":
        info["reason"] = data.get("reason") or f"ast scanner exit {proc.returncode}"
        return info, {}
    files = {k: v for k, v in (data.get("files") or {}).items() if isinstance(v, dict) and "error" not in v}
    info.update({"mode": "typescript-ast", "typescriptVersion": data.get("typescriptVersion"), "filesAnalyzed": len(files),
                 "filesFailed": sorted(k for k, v in (data.get("files") or {}).items() if not isinstance(v, dict) or "error" in v)})
    return info, files


def build_rescue_bundle(
    repo_path: str | Path,
    write_files: bool = False,
    output_dir: Optional[str | Path] = None,
    output_format: str = "json",
) -> Dict[str, Any]:
    """Scan a project directory and build a secret-masked VibeRescue diagnostic & handoff bundle."""
    started = time.perf_counter()
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Target repository directory does not exist: {root}")

    stack_info = _detect_stack(root)
    git_info = _git_summary(root)

    # 1. Inspect .env* files (key names & presence status only)
    env_files_found: Dict[str, Dict[str, str]] = {}
    for env_name in ENV_FILE_CANDIDATES:
        ep = root / env_name
        if ep.exists() and ep.is_file():
            env_files_found[env_name] = _parse_env_keys_only(ep)

    active_env_files = [
        name for name in (".env", ".env.local", ".env.development", ".env.production")
        if name in env_files_found
    ]
    active_env_keys: Set[str] = set()
    for env_name in active_env_files:
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
    scanned_sql_files = 0

    # Cross-file SQL migration state aggregation
    sql_created_tables: Dict[str, Tuple[str, int]] = {}
    sql_rls_enabled_tables: Set[str] = set()
    sql_rls_disabled_tables: Dict[str, Tuple[str, int]] = {}
    sql_policy_tables: Set[str] = set()
    sql_docs: List[Tuple[str, str]] = []
    unverified_items: List[Dict[str, Any]] = []

    from .code_anchors import _blank_js_comments_preserve_lines

    js_rel_files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".git"))
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if not fpath.is_symlink() and fpath.suffix.lower() in JS_AST_EXTENSIONS:
                js_rel_files.append(str(fpath.relative_to(root)).replace("\\", "/"))
    js_analysis, js_ast = _typescript_env_scan(root, js_rel_files)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".git"))
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if fpath.is_symlink():
                continue
            rel = str(fpath.relative_to(root)).replace("\\", "/")
            ext = fpath.suffix.lower()
            if ext in CODE_SCAN_EXTENSIONS:
                scanned_files += 1
                try:
                    raw_content = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                clean_content = _strip_code_comments_preserve_lines(raw_content, ext)
                if ext != ".py":
                    no_str_content, _ = _blank_js_comments_preserve_lines(raw_content, blank_strings=True)
                    no_str_lines = no_str_content.splitlines()
                else:
                    no_str_lines = clean_content.splitlines()
                is_client = _is_client_side_file(rel, clean_content)
                ast_file = js_ast.get(rel)
                if ast_file is not None:
                    # Real parser result: the directive prologue decides "use client"; the regex directive
                    # heuristic is dropped, path heuristics remain.
                    directive_by_heuristic = _has_use_client_directive(clean_content)
                    is_client = bool(ast_file.get("isClient")) or (is_client and not directive_by_heuristic)
                    # Unverifiable is UNVERIFIED, not BLOCKED (Round 9 contract).
                    if int(ast_file.get("parseDiagnostics") or 0) > 0:
                        unverified_items.append({"file": rel, "line": 1, "reason": f"{ast_file.get('parseDiagnostics')} syntax error(s): env usage cannot be verified"})
                    if is_client:
                        for dyn in ast_file.get("unverified", ast_file.get("dynamic", [])):
                            dyn_line = int(dyn.get("line", 0)) if isinstance(dyn, dict) else int(dyn)
                            why = dyn.get("reason", "unsupported env usage") if isinstance(dyn, dict) else "unsupported env usage"
                            unverified_items.append({"file": rel, "line": dyn_line, "reason": f"client env usage outside the supported forms ({why})"})
                    for ref in ast_file.get("refs", []):
                        key = str(ref.get("key") or "")
                        lineno = int(ref.get("line") or 0)
                        if not key:
                            continue
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
                        elif is_client and not _is_public_env_key(key):
                            # Same policy as the doctor: a client component may read only public-prefixed variables.
                            blockers.append({
                                "kind": "client_nonpublic_env",
                                "severity": "critical",
                                "file": rel,
                                "line": lineno,
                                "key": key,
                                "message": f"Client-side file `{rel}:{lineno}` reads non-public env `{key}` (only NEXT_PUBLIC_*, VITE_* and NODE_ENV are public).",
                                "fixHint": f"Read `{key}` in server code, or rename it with a public prefix only if it is truly public.",
                            })
                    continue
                if is_client and ext != ".py":
                    # Closed world: without the real parser a client component cannot be certified.
                    unverified_items.append({"file": rel, "line": 1, "reason": "client component checked only by the heuristic lexer (install the project's typescript or set DELTAHARNESS_TYPESCRIPT)"})
                for lineno, line in enumerate(clean_content.splitlines(), start=1):
                    ns_line = no_str_lines[lineno - 1] if lineno - 1 < len(no_str_lines) else line
                    for m in PROCESS_ENV_RE.finditer(line):
                        # Ignore matches where `process.env` / `import.meta.env` itself is inside a string literal
                        if m.start() < len(ns_line) and not ns_line[m.start():m.start() + 6].strip():
                            continue
                        key = m.group(1) or m.group(2)
                        if not key:
                            continue
                        if key not in BUILTIN_ENV_KEYS:
                            code_env_refs.setdefault(key, []).append(f"{rel}:{lineno}")
                        # Heuristic findings never decide (Round 9): report them as UNVERIFIED, not BLOCKED.
                        if is_client and not _is_public_env_key(key):
                            unverified_items.append({"file": rel, "line": lineno, "reason": f"heuristic: client file appears to read non-public env `{key}`"})
                    if is_client and ext != ".py":
                        for m in DYNAMIC_ENV_RE.finditer(line):
                            if m.start() < len(ns_line) and not ns_line[m.start():m.start() + 6].strip():
                                continue  # inside a string literal
                            unverified_items.append({"file": rel, "line": lineno, "reason": "heuristic: env object read without a literal key"})
                    for m in PY_ENV_RE.finditer(line):
                        key = m.group(1) or m.group(2)
                        if key and key not in BUILTIN_ENV_KEYS:
                            code_env_refs.setdefault(key, []).append(f"{rel}:{lineno}")

            # 3. Collect SQL tables, RLS enablement/disablement, and policies across all .sql files (after stripping SQL comments)
            if ext == ".sql":
                scanned_sql_files += 1
                try:
                    raw_sql = fpath.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                sql_docs.append((rel, raw_sql))

    # SQL: one ordered event pass over all migrations (file-name order), mirroring the doctor's model.
    sql_state = _sql_event_pass(sorted(sql_docs))
    sql_created_tables.update(sql_state["created"])
    sql_rls_enabled_tables.update(sql_state["enabled"])
    sql_rls_disabled_tables.update(sql_state["disabled"])
    sql_policy_tables.update(sql_state["policies"])
    unverified_items.extend(sql_state["unverified"])
    for rel_p, line_p, tbl in sql_state["permissive"]:
        tbl_suffix = f" on `{tbl}`" if tbl else ""
        blockers.append({
            "kind": "supabase_rls_or_auth_gap",
            "severity": "warning",
            "file": rel_p,
            "line": line_p,
            "message": f"SQL file `{rel_p}:{line_p}` contains an unrestricted `USING (true)` policy{tbl_suffix}.",
            "fixHint": "Verify whether `USING (true)` is intentional for public read-only data or if it leaks tenant rows.",
        })

    # Evaluate cross-migration SQL RLS state
    if sql_created_tables or sql_rls_disabled_tables:
        unprotected_by_file: Dict[Tuple[str, int], List[str]] = {}
        rls_without_policy_by_file: Dict[Tuple[str, int], List[str]] = {}
        for t_name, (rel_f, ln_no) in sorted(sql_created_tables.items()):
            if t_name not in sql_rls_enabled_tables:
                loc = sql_rls_disabled_tables.get(t_name, (rel_f, ln_no))
                unprotected_by_file.setdefault(loc, []).append(_sql_table_label(t_name))
            elif t_name not in sql_policy_tables:
                rls_without_policy_by_file.setdefault((rel_f, ln_no), []).append(_sql_table_label(t_name))
        for (rel_f, ln_no), tbls in sorted(unprotected_by_file.items()):
            blockers.append({
                "kind": "supabase_rls_or_auth_gap",
                "severity": "critical",
                "file": rel_f,
                "line": ln_no,
                "tables": tbls,
                "message": f"SQL schema leaves table(s) `{', '.join(tbls)}` (at `{rel_f}:{ln_no}`) with Row Level Security disabled or missing across migrations.",
                "fixHint": f"Ensure `ALTER TABLE <table_name> ENABLE ROW LEVEL SECURITY;` remains enabled and explicit `auth.uid()` policies exist for `{', '.join(tbls)}`.",
            })
        for (rel_f, ln_no), tbls in sorted(rls_without_policy_by_file.items()):
            blockers.append({
                "kind": "supabase_rls_enabled_without_policy",
                "severity": "warning",
                "file": rel_f,
                "line": ln_no,
                "tables": tbls,
                "message": f"Table(s) `{', '.join(tbls)}` enable RLS but define zero `CREATE POLICY` statements across SQL files (default-deny will block all client queries).",
                "fixHint": f"Add explicit `CREATE POLICY` statements (`FOR SELECT/INSERT/UPDATE/DELETE`) for `{', '.join(tbls)}`.",
            })

    # 4. Check missing, placeholder, or unverified env keys
    all_required_keys = sorted((set(code_env_refs.keys()) | example_env_keys) - BUILTIN_ENV_KEYS)
    missing_env_keys: List[Dict[str, Any]] = []
    unverified_env_keys: List[Dict[str, Any]] = []
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
        if active_env_files:
            if key not in active_env_keys:
                refs = code_env_refs.get(key, [".env.example"])
                missing_env_keys.append({"key": key, "referencedAt": refs[:5]})
                blockers.append({
                    "kind": "missing_env_key",
                    "severity": "critical" if key in code_env_refs else "warning",
                    "file": refs[0].split(":")[0],
                    "line": int(refs[0].split(":")[1]) if ":" in refs[0] and refs[0].split(":")[1].isdigit() else 1,
                    "key": key,
                    "message": f"Environment variable `{key}` is required by `{refs[0]}` but is missing or empty in local `.env`/`.env.local`.",
                    "fixHint": f"Set `{key}` in `.env.local` and in your hosting provider's Environment Variables settings.",
                })
        else:
            refs = code_env_refs.get(key, [".env.example"])
            unverified_env_keys.append({"key": key, "referencedAt": refs[:5]})
            blockers.append({
                "kind": "unverified_env_key",
                "severity": "warning",
                "file": refs[0].split(":")[0],
                "line": int(refs[0].split(":")[1]) if ":" in refs[0] and refs[0].split(":")[1].isdigit() else 1,
                "key": key,
                "message": f"Environment variable `{key}` is referenced at `{refs[0]}`, but no local `.env`/`.env.local` file exists to verify its presence.",
                "fixHint": f"Create a local `.env.local` or verify that `{key}` is injected in your deployment environment.",
            })

    # 5. Check competing lockfiles
    present_lockfiles = [lf for lf in LOCKFILE_CANDIDATES if (root / lf).exists()]
    if len(present_lockfiles) > 1:
        blockers.append({
            "kind": "competing_lockfiles",
            "severity": "warning",
            "file": present_lockfiles[0],
            "line": 1,
            "lockfiles": present_lockfiles,
            "message": f"Multiple competing lockfiles detected: `{', '.join(present_lockfiles)}`.",
            "fixHint": "Keep only ONE lockfile matching your deployment package manager so cloud builds do not install mismatched dependency trees.",
        })

    # 6. Include instruction/script drift from `audit_repository`
    audit_res = audit_repository(root, output_format="json")
    audit_findings = audit_res.get("findings", [])
    for af in audit_findings[:25]:
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
    total_scanned_targets = scanned_files + scanned_sql_files + len(env_files_found) + int(audit_res.get("scannedDocsCount", 0))

    if total_scanned_targets == 0:
        readiness = "NO_TARGETS_FOUND"
        complexity = "UNVERIFIED_EMPTY"
        exit_code = 2
    elif critical_count == 0 and unverified_items:
        # Closed world: something could not be verified, so the result is never reported as passed.
        readiness = "UNVERIFIED"
        complexity = "NEEDS_VERIFICATION"
        exit_code = 1
    elif critical_count == 0 and unverified_env_keys:
        readiness = "ENV_UNVERIFIED"
        complexity = "NEEDS_ENV_VERIFICATION"
        exit_code = 1
    elif critical_count == 0 and warning_count == 0:
        readiness = "STATIC_CHECKS_PASSED"
        complexity = "NONE"
        exit_code = 0
    elif critical_count == 0:
        readiness = "STATIC_CHECKS_WITH_WARNINGS"
        complexity = "LOW"
        exit_code = 1
    elif critical_count <= 2 and len(blockers) <= 5:
        readiness = "BLOCKED"
        complexity = "LOW_TO_MEDIUM"
        exit_code = 1
    else:
        readiness = "BLOCKED"
        complexity = "HIGH_RESCUE_OR_HANDOFF"
        exit_code = 1

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    bundle_id = hashlib.sha256(f"{root}:{git_info.get('headSha')}:{len(blockers)}".encode("utf-8")).hexdigest()[:12]

    out_root = Path(output_dir).expanduser().resolve() if output_dir else root
    brief_path = out_root / "RESCUE_BRIEF.md"
    prompt_path = out_root / "ONE_PROMPT_RESCUE.md"
    json_path = out_root / "rescue-bundle.json"

    raw_bundle: Dict[str, Any] = {
        "ok": exit_code == 0,
        "exitCode": exit_code,
        "readiness": readiness,
        "bundleId": f"rescue-{bundle_id}",
        "repoName": root.name,
        "repoPath": str(root),
        "scannedCodeFiles": scanned_files,
        "scannedSqlFiles": scanned_sql_files,
        "elapsedMs": elapsed_ms,
        "secretSafety": {
            "rawEnvValuesIncluded": False,
            "redactionPolicy": "recursive_token_and_env_key_only_scrub",
            "verifiedOutputClean": True,
            "scopeNote": "Local .env values are never included, and known API token patterns in paths/messages are scrubbed.",
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
            "unverifiedKeys": unverified_env_keys,
        },
        "jsAnalysis": js_analysis,
        "unverified": unverified_items,
        "summary": {
            "readiness": readiness,
            "jsAnalysisMode": js_analysis.get("mode"),
            "unverifiedCount": len(unverified_items),
            "estimatedComplexity": complexity,
            "totalBlockers": len(blockers),
            "criticalCount": critical_count,
            "warningCount": warning_count,
        },
        "blockers": blockers,
    }
    if write_files:
        raw_bundle["writtenFiles"] = {
            "rescueBrief": str(brief_path),
            "onePromptRescue": str(prompt_path),
            "rescueBundleJson": str(json_path),
        }

    # Apply recursive secret scrubbing across all fields (including writtenFiles) before formatting or serializing
    bundle: Dict[str, Any] = _scrub_recursive(raw_bundle)
    bundle["rescueBriefMarkdown"] = mask_sensitive_text(format_rescue_markdown(bundle))
    bundle["onePromptRescueMarkdown"] = mask_sensitive_text(format_one_prompt_rescue(bundle))

    # Post-serialization verification check
    serialized_check = json.dumps(
        {k: v for k, v in bundle.items() if k not in ("rescueBriefMarkdown", "onePromptRescueMarkdown")},
        ensure_ascii=False,
    )
    bundle["secretSafety"]["verifiedOutputClean"] = not bool(
        SECRET_VALUE_REDACT_RE.search(serialized_check)
        or SECRET_VALUE_REDACT_RE.search(bundle["rescueBriefMarkdown"])
        or SECRET_VALUE_REDACT_RE.search(bundle["onePromptRescueMarkdown"])
    )

    if write_files:
        out_root.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(bundle["rescueBriefMarkdown"], encoding="utf-8")
        prompt_path.write_text(bundle["onePromptRescueMarkdown"], encoding="utf-8")
        serializable = {k: v for k, v in bundle.items() if k not in ("rescueBriefMarkdown", "onePromptRescueMarkdown")}
        json_path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")

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
    readiness = summary.get("readiness", "UNVERIFIED")

    lines = [
        f"# 🛟 VibeRescue Static Diagnostic & Handoff Brief (`{bundle.get('repoName', 'project')}`)",
        "",
        f"- **Bundle ID**: `{bundle.get('bundleId')}` · **Static Readiness**: `{readiness}`",
        f"- **Secret Redaction**: `.env` values omitted + known token patterns (`sk-*`, `eyJ*`, `ghp_*`, `AKIA*`) recursively scrubbed",
        f"- **Tech Stack**: `{', '.join(stack.get('languages', []))}` · Frameworks: `{', '.join(stack.get('frameworks', []) or ['None'])}` · DB: `{', '.join(stack.get('database', []) or ['None'])}`",
        f"- **Git State**: Branch `{git_info.get('branch') or 'N/A'}` (`{git_info.get('headSha') or 'uncommitted'}`) · Dirty files: `{git_info.get('dirtyFileCount', 0)}`",
        f"- **Deploy Configs**: `{', '.join(bundle.get('deployConfigs', []) or ['None detected'])}` · Lockfiles: `{', '.join(bundle.get('presentLockfiles', []) or ['None'])}`",
        "",
        "---",
        "",
        f"## 1. Static Blocker Summary ({summary.get('totalBlockers', 0)} total: {summary.get('criticalCount', 0)} critical, {summary.get('warningCount', 0)} warnings)",
        "",
    ]

    if readiness == "NO_TARGETS_FOUND":
        lines.append("⚠️ **No code, SQL, `.env`, or instruction files were found in this directory.** Static checks could not be performed.")
    elif not blockers:
        lines.append("✅ **Static checks passed (0 static blockers detected).** Note: Static inspection does not replace runtime auth, webhook, or deployment verification.")
    else:
        lines.append("| Severity | Category | Location | Issue & Actionable Fix |")
        lines.append("|---|---|---|---|")
        for b in blockers:
            sev = "🔴 CRITICAL" if b.get("severity") == "critical" else "🟡 WARN"
            loc = f"`{mask_sensitive_text(str(b.get('file', '.')))}:{b.get('line', 1)}`"
            msg = mask_sensitive_text(str(b.get("message", "")))
            hint = mask_sensitive_text(str(b.get("fixHint", "")))
            lines.append(f"| {sev} | `{b.get('kind')}` | {loc} | {msg} **→ Fix:** {hint} |")

    lines.extend([
        "",
        "## 2. Environment Variable Contract (Keys Only · Values Omitted)",
        "",
        f"- **Detected `.env` files**: `{', '.join(env_audit.get('envFilesDetected', []) or ['None'])}`",
        f"- **Required Keys in Code/Example ({len(env_audit.get('requiredKeys', []))})**: `{', '.join(env_audit.get('requiredKeys', []) or ['None'])}`",
    ])
    missing = env_audit.get("missingOrEmptyKeys", [])
    unverified = env_audit.get("unverifiedKeys", [])
    if missing:
        lines.append("- **Missing or Unset Keys in Local `.env`**:")
        for item in missing:
            lines.append(f"  - ❌ `{item['key']}` (referenced at `{', '.join(item.get('referencedAt', []))}`)")
    elif unverified:
        lines.append("- **Unverified Keys (No Local `.env` File Found)**:")
        for item in unverified:
            lines.append(f"  - ⚠️ `{item['key']}` (referenced at `{', '.join(item.get('referencedAt', []))}` — verify runtime injection)")
    else:
        lines.append("- ✅ All referenced environment variable keys are present in local `.env` files.")

    lines.extend([
        "",
        "## 3. Next Step (Self-Rescue or Handoff)",
        "",
        "1. **Fix with AI Coding Agent**: Paste `ONE_PROMPT_RESCUE.md` into Claude Code, Cursor, or Codex to address the static blockers above.",
        "2. **Verify Runtime Behavior**: After patching static blockers, run your test suite, build command, and actual login/webhook/RLS checks.",
        "",
    ])
    return "\n".join(lines)


def format_one_prompt_rescue(bundle: Dict[str, Any]) -> str:
    """Generate a surgical, copy-pasteable repair prompt for Claude Code / Cursor / Codex."""
    blockers = bundle.get("blockers", [])
    repo_name = mask_sensitive_text(str(bundle.get("repoName", "project")))
    readiness = bundle.get("summary", {}).get("readiness", "UNVERIFIED")
    if readiness == "NO_TARGETS_FOUND":
        return f"# One-Prompt Rescue (`{repo_name}`)\n\nNo code or configuration files were found in `{repo_name}`.\n"
    if not blockers:
        return (
            f"# One-Prompt Rescue (`{repo_name}`)\n\n"
            "No static blockers were detected by VibeRescue. Verify runtime build and end-to-end tests before deploying.\n"
        )

    tasks_md = []
    for idx, b in enumerate(blockers, start=1):
        tasks_md.append(
            f"{idx}. **[{b.get('severity', 'warning').upper()} · `{b.get('kind')}`]** at `{mask_sensitive_text(str(b.get('file', '.')))}:{b.get('line', 1)}`\n"
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
        description="VibeRescue — Secret-Masked Static Blocker Diagnostic & Handoff Bundler",
    )
    sub = parser.add_subparsers(dest="subcommand")

    pack_p = sub.add_parser("pack", help="Scan a project and generate a secret-masked rescue bundle")
    pack_p.add_argument("path", nargs="?", default=".", help="Target repository directory (default: .)")
    pack_p.add_argument("--write", action="store_true", help="Write RESCUE_BRIEF.md, ONE_PROMPT_RESCUE.md, and rescue-bundle.json")
    pack_p.add_argument("--output-dir", default=None, help="Optional directory to write bundle files")
    pack_p.add_argument("--format", default="markdown", choices=["json", "markdown", "prompt"], help="Console output format")
    pack_p.add_argument("--exit-zero", action="store_true", help="Always exit with code 0 even when blockers are detected")

    args = parser.parse_args(argv)
    target = getattr(args, "path", ".") if args.subcommand else "."
    write_files = getattr(args, "write", False)
    out_dir = getattr(args, "output_dir", None)
    fmt = getattr(args, "format", "markdown")
    exit_zero = getattr(args, "exit_zero", False)

    try:
        res = build_rescue_bundle(target, write_files=write_files, output_dir=out_dir, output_format=fmt)
        if fmt == "json":
            printable = {k: v for k, v in res.items() if k not in ("rescueBriefMarkdown", "onePromptRescueMarkdown")}
            print(json.dumps(printable, indent=2, ensure_ascii=False))
        elif fmt == "prompt":
            print(res["onePromptRescueMarkdown"])
        else:
            print(res["rescueBriefMarkdown"])
        if exit_zero:
            return 0
        return int(res.get("exitCode", 1))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
