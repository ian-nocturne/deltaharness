"""Zero-Setup Repository Procedure & Instruction Drift Auditor (`deltaharness audit`).

Scans repository instruction files (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`,
`.cursor/rules/*.mdc`, `.github/copilot-instructions.md`, `README.md`, `docs/**/*.md`,
`memory_skills/**/SKILL.md`) without requiring prior SQLite or task initialization.

Detects:
- `missing_path`: Referenced repository file path no longer exists.
- `missing_script`: `npm/pnpm/yarn/bun run <script>` or `make <target>` missing from `package.json` / `Makefile`.
- `missing_symbol`: Referenced function, class, method, interface, or script symbol missing from target file.
- `signature_drift`: Breaking parameter arity, required keyword argument, interface field, or git-diff contract drift.
- `unsupported_anchor`: Symbol anchor attached to a language file with `resolution == "unsupported"`.

Supports Git-diff blast radius analysis (`--base <ref> --head <ref>`) and outputs
structured JSON, human-readable Markdown, or GitHub Code Scanning SARIF v2.1.0.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from .code_anchors import (
    extract_file_symbol_hashes,
    extract_file_symbol_signatures,
    is_breaking_symbol_change,
)

EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    "__pycache__",
    ".next",
    ".nuxt",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "site-packages",
}

KNOWN_CODE_EXTENSIONS = (
    "json",
    "yaml",
    "toml",
    "tsx",
    "jsx",
    "mjs",
    "cjs",
    "mdc",
    "sql",
    "yml",
    "py",
    "ts",
    "js",
    "sh",
    "md",
    "go",
    "rs",
    "tf",
)

EXT_PATTERN = "|".join(KNOWN_CODE_EXTENSIONS)

# Matches explicit `path/to/file.ext::Symbol` or `path/to/file.ext#Symbol`
EXPLICIT_ANCHOR_RE = re.compile(
    rf"(?P<path>(?:[A-Za-z0-9_.-]+/)*(?:Makefile|makefile|GNUmakefile)|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:{EXT_PATTERN})(?![A-Za-z0-9_]))"
    r"(?:(?:::|#)(?P<symbol>[A-Za-z_][A-Za-z0-9_.-]*))"
)

# Matches backticked file paths like `src/auth.ts`, `payment_gateway.py`, `Makefile`
BACKTICK_FILE_RE = re.compile(
    rf"`(?P<path>(?:\./)?(?:(?:[A-Za-z0-9_.-]+/)*(?:Makefile|makefile|GNUmakefile)|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:{EXT_PATTERN})(?![A-Za-z0-9_])))(?:::|#(?P<symbol>[A-Za-z_][A-Za-z0-9_.-]*))?`"
)

# Matches markdown links `[label](path/to/file.ext)`
MD_LINK_FILE_RE = re.compile(
    rf"\[[^\]]+\]\((?P<path>(?!https?://|mailto:|file://|#)(?:\./)?(?:(?:[A-Za-z0-9_.-]+/)*(?:Makefile|makefile|GNUmakefile)|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:{EXT_PATTERN})(?![A-Za-z0-9_])))(?:#(?P<anchor>[A-Za-z0-9_.-]+))?\)"
)

# Matches unbackticked repo paths that contain at least one directory separator `dir/file.ext`
SLASH_PATH_RE = re.compile(
    rf"(?<![A-Za-z0-9_/:.-])(?P<path>(?:[A-Za-z0-9_.-]+/)+(?:Makefile|makefile|GNUmakefile|[A-Za-z0-9_.-]+\.(?:{EXT_PATTERN})(?![A-Za-z0-9_])))(?:::([A-Za-z_][A-Za-z0-9_.-]*))?"
)

# Matches npm / pnpm / yarn / bun script invocations
NPM_SCRIPT_RE = re.compile(
    r"(?:`|^|\s)(?:npm|pnpm|yarn|bun)\s+run\s+(?P<script>[A-Za-z0-9_:.-]+)"
)
YARN_SHORT_SCRIPT_RE = re.compile(
    r"`(?:yarn|pnpm)\s+(?P<script>(?!install\b|add\b|remove\b|dlx\b|exec\b|init\b|why\b|config\b|run\b|tsx\b|--|-)[A-Za-z0-9_:.-]+)`"
    r"|^\s*(?:\$\s*)?(?:yarn|pnpm)\s+(?P<script2>(?!install\b|add\b|remove\b|dlx\b|exec\b|init\b|why\b|config\b|run\b|tsx\b|create\b|up\b|update\b|link\b|unlink\b|list\b|outdated\b|audit\b|licenses\b|pack\b|publish\b|rebuild\b|store\b|env\b|patch\b|dedupe\b|fetch\b|import\b|approve-builds\b|--|-)[A-Za-z0-9_:.-]+)\s*$"
)

# Matches `make <target>` inside backticks or explicit shell command lines
MAKE_TARGET_RE = re.compile(
    r"`make\s+(?P<target>(?!-)[A-Za-z0-9_.-]+)`|^\s*(?:\$\s*)?make\s+(?P<target2>(?!-)[A-Za-z0-9_.-]+)\s*$"
)

MAKE_PROSE_STOPWORDS = {
    "a",
    "an",
    "the",
    "sure",
    "it",
    "this",
    "that",
    "existing",
    "new",
    "changes",
    "change",
    "use",
    "sense",
    "up",
    "out",
    "for",
    "from",
    "with",
    "into",
    "all",
    "any",
    "some",
    "more",
    "less",
    "your",
    "our",
    "their",
    "them",
}

# Matches inline function calls like `charge_customer(a, b)` or `chargeCustomer(u1, 100, { currency: 'USD' })`
INLINE_CALL_RE = re.compile(
    r"\b(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<args>[^()]*)\)"
)

# False-positive path tokens to ignore (version numbers, common tech nouns with dots, template placeholders)
IGNORED_FILE_TOKENS = {
    "node.js",
    "next.js",
    "vue.js",
    "react.js",
    "three.js",
    "chart.js",
    "d3.js",
    "express.js",
    "nest.js",
    "nuxt.js",
    "vite.js",
    "rollup.js",
    "webpack.js",
    "references/provenance.json",
    "claude.md",
    "agents.md",
    "skill.md",
    "design.md",
    "design-tokens.css",
    "rescue_brief.md",
    "one_prompt_rescue.md",
    "anti_slop_patch_prompt.md",
    "deltaharness-audit.sarif",
    "package.json",
    "makefile",
    "pyproject.toml",
    "kebab-case.ts",
    "kebab-case.tsx",
    "kebab-case.test.ts",
    "kebab-case.test-d.ts",
    "usepagename.ts",
    "usecomponentname.ts",
    "componentname/componentname.tsx",
}

PYTHON_BUILTINS_AND_COMMON = {
    "print",
    "len",
    "range",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "min",
    "max",
    "sum",
    "round",
    "sorted",
    "enumerate",
    "zip",
    "isinstance",
    "getattr",
    "setattr",
    "hasattr",
    "open",
    "Path",
    "json",
    "if",
    "for",
    "while",
    "return",
    "assert",
    "require",
    "import",
}


def _discover_instruction_docs(repo_root: Path) -> List[Path]:
    """Find instruction, rule, runbook, and skill markdown files in repo_root."""
    found: List[Path] = []
    seen: Set[str] = set()

    def _add(p: Path) -> None:
        try:
            if p.is_symlink() or not p.is_file():
                return
            resolved = str(p.resolve())
            if resolved not in seen:
                seen.add(resolved)
                found.append(p)
        except OSError:
            pass

    top_Names = (
        "CLAUDE.md",
        "AGENTS.md",
        ".cursorrules",
        "README.md",
        "CONTRIBUTING.md",
        "RUNBOOK.md",
        ".github/copilot-instructions.md",
    )
    for rel in top_Names:
        _add(repo_root / rel)

    for glob_pat in (
        ".cursor/rules/*.md",
        ".cursor/rules/*.mdc",
        ".github/instructions/*.md",
        "docs/**/*.md",
        "runbooks/**/*.md",
        "memory_skills/**/SKILL.md",
    ):
        for p in sorted(repo_root.glob(glob_pat)):
            if any(part in EXCLUDED_DIRS for part in p.relative_to(repo_root).parts):
                continue
            _add(p)

    # Also check nested CLAUDE.md / AGENTS.md up to depth 3
    for p in sorted(repo_root.rglob("*.md")):
        try:
            rel_parts = p.relative_to(repo_root).parts
        except ValueError:
            continue
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        if len(rel_parts) <= 4 and p.name in ("CLAUDE.md", "AGENTS.md", "SKILL.md"):
            _add(p)

    return found


def _parse_call_arguments(arg_str: str) -> Tuple[int, List[str], bool]:
    """Parse an inline call argument string `a, b, key=val` into (pos_count, kw_names, is_definition_like).

    If the call looks like a type signature definition `a: int, b: str = 1` rather than an invocation
    or formal parameter list, we still extract parameter count accurately.
    """
    raw = arg_str.strip()
    if not raw:
        return 0, [], False
    # Split top-level commas respecting brackets/braces/quotes
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    in_quote: Optional[str] = None
    for ch in raw:
        if in_quote:
            current.append(ch)
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
            current.append(ch)
        elif ch in ("(", "[", "{", "<"):
            depth += 1
            current.append(ch)
        elif ch in (")", "]", "}", ">"):
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())

    pos_count = 0
    kw_names: List[str] = []
    is_def_like = False
    for p in parts:
        if not p or p in ("self", "cls"):
            continue
        if p.startswith("*") or p.startswith("..."):
            is_def_like = True
            continue
        # Check for Python/TS type annotation `param: Type` vs keyword arg `key=val`
        if ":" in p and ("=" not in p or p.index(":") < p.index("=")):
            is_def_like = True
            pos_count += 1
            continue
        if "=" in p and not p.startswith(("=", "!")):
            k = p.split("=", 1)[0].strip()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                kw_names.append(k)
                continue
        pos_count += 1
    return pos_count, kw_names, is_def_like


def _check_inline_call_vs_signature(
    sym_name: str,
    pos_count: int,
    kw_names: List[str],
    is_def_like: bool,
    sig: Dict[str, Any],
) -> Optional[str]:
    """Compare an inline call `sym(a, b, k=1)` in documentation against the current code signature `sig`."""
    kind = sig.get("kind", "function")
    if kind not in ("function", "async_function"):
        return None
    req_args = list(sig.get("reqArgs") or [])
    all_args = list(sig.get("allArgs") if sig.get("allArgs") is not None else req_args)
    min_pos = len(req_args)
    max_pos = len(all_args)
    has_varargs = bool(sig.get("hasVararg", sig.get("has_varargs", False)))
    has_varkw = bool(sig.get("hasKwarg", sig.get("has_varkw", False)))
    pos_names = all_args
    kwonly_req = set(sig.get("kwonlyReq") or sig.get("kwonly_req") or [])
    kwonly_all = set(sig.get("kwonlyAll") or sig.get("kwonly_opt") or [])

    if is_def_like:
        total_documented = pos_count + len(kw_names)
        if total_documented < min_pos + len(kwonly_req):
            return (
                f"문서 시그니처 `{sym_name}` 인자 수({total_documented}개)가 현재 코드 필수 인자 "
                f"(필수 위치 인자 {min_pos}개: {req_args}, 필수 키워드 인자 {sorted(kwonly_req)})와 일치하지 않아요."
            )
        return None

    satisfied_pos_by_kw = sum(1 for k in kw_names if k in pos_names)
    effective_pos = pos_count + satisfied_pos_by_kw
    if pos_count == 0 and not kw_names:
        # Empty `func()` in documentation prose is standard shorthand for naming a function
        return None
    if effective_pos < min_pos:
        return (
            f"`{sym_name}` 호출의 위치 인자({effective_pos}개)가 최소 필수 인자({min_pos}개: {req_args})보다 적어요."
        )
    if not has_varargs and pos_count > max_pos:
        return (
            f"`{sym_name}` 호출의 위치 인자({pos_count}개)가 최대 허용 위치 인자({max_pos}개: {all_args})를 초과해요."
        )

    missing_req_kw = [k for k in sorted(kwonly_req) if k not in kw_names]
    if missing_req_kw:
        return (
            f"`{sym_name}` 호출에 필수 키워드 전용 인자({missing_req_kw})가 누락되었어요."
        )

    if kw_names and not has_varkw and pos_names:
        allowed_kw = set(pos_names) | kwonly_req | kwonly_all
        unknown_kw = [k for k in kw_names if k not in allowed_kw]
        if unknown_kw:
            return (
                f"`{sym_name}` 호출에 존재하지 않는 키워드 인자({unknown_kw})가 사용되었어요."
            )
    return None


def _compute_git_diff_changes(
    repo_root: Path,
    base_ref: str,
    head_ref: str = "HEAD",
) -> Dict[str, Any]:
    """Inspect git diff between base_ref and head_ref and compute file & symbol contract deltas."""
    diff_info: Dict[str, Any] = {
        "baseRef": base_ref,
        "headRef": head_ref,
        "changedFiles": {},
    }
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-status", base_ref, head_ref],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if proc.returncode != 0:
            diff_info["error"] = (proc.stderr or proc.stdout or "").strip()
            return diff_info
    except (OSError, subprocess.SubprocessError) as exc:
        diff_info["error"] = str(exc)
        return diff_info

    for raw_line in (proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0].strip()
        rel_path = parts[-1].strip()
        old_rel_path = parts[1].strip() if len(parts) > 2 else rel_path

        file_delta: Dict[str, Any] = {
            "status": status,
            "path": rel_path,
            "deletedSymbols": [],
            "breakingSymbols": {},
            "compatibleSymbols": [],
        }

        # Load base file content via `git show <base_ref>:<old_rel_path>`
        base_content: Optional[str] = None
        if not status.startswith("A"):
            try:
                p_show = subprocess.run(
                    ["git", "show", f"{base_ref}:{old_rel_path}"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                )
                if p_show.returncode == 0:
                    base_content = p_show.stdout
            except (OSError, subprocess.SubprocessError):
                base_content = None

        # Load head file content (working tree if head_ref == "WORKTREE" or "HEAD" with local file, else git show)
        head_path = repo_root / rel_path
        if status.startswith("D") or not head_path.is_file():
            diff_info["changedFiles"][rel_path] = file_delta
            continue

        if base_content is not None:
            suffix = head_path.suffix or head_path.name
            with tempfile.NamedTemporaryFile("w", suffix=f"_{head_path.name}", encoding="utf-8", delete=True) as tf:
                tf.write(base_content)
                tf.flush()
                base_sigs = extract_file_symbol_signatures(Path(tf.name)).get("symbols") or {}
                base_hashes = extract_file_symbol_hashes(Path(tf.name))
            curr_sigs = extract_file_symbol_signatures(head_path).get("symbols") or {}
            curr_hashes = extract_file_symbol_hashes(head_path)

            for sym, old_sig in base_sigs.items():
                if sym not in curr_sigs:
                    file_delta["deletedSymbols"].append(sym)
                elif base_hashes.get(sym) != curr_hashes.get(sym):
                    is_brk, reason = is_breaking_symbol_change(old_sig, curr_sigs[sym])
                    if is_brk:
                        file_delta["breakingSymbols"][sym] = reason
                    else:
                        file_delta["compatibleSymbols"].append(sym)

        diff_info["changedFiles"][rel_path] = file_delta
    return diff_info


def audit_repository(
    repo_path: str | Path,
    *,
    base_ref: Optional[str] = None,
    head_ref: str = "HEAD",
    output_format: str = "json",
) -> Dict[str, Any]:
    """Zero-setup repository instruction & skill procedure audit (`deltaharness audit`)."""
    t0 = time.perf_counter()
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"감사할 저장소 경로를 찾지 못했어요: {root}")

    docs = _discover_instruction_docs(root)
    sig_cache: Dict[str, Dict[str, Any]] = {}
    hash_cache: Dict[str, Dict[str, str]] = {}

    def _get_sigs(p: Path) -> Dict[str, Any]:
        key = str(p.resolve())
        if key not in sig_cache:
            sig_cache[key] = extract_file_symbol_signatures(p)
        return sig_cache[key]

    def _get_hashes(p: Path) -> Dict[str, str]:
        key = str(p.resolve())
        if key not in hash_cache:
            hash_cache[key] = extract_file_symbol_hashes(p)
        return hash_cache[key]

    # Pre-load root package.json scripts and Makefile targets if present
    pkg_json_path = root / "package.json"
    pkg_scripts: Optional[Dict[str, Any]] = None
    if pkg_json_path.is_file():
        pkg_scripts = _get_sigs(pkg_json_path).get("symbols") or {}

    # Pre-load workspace package.json scripts (packages/*, apps/*, examples/*)
    workspace_pkg_scripts: Dict[str, Dict[str, Any]] = {}
    for ws_glob in ("packages/*/package.json", "apps/*/package.json", "examples/*/package.json"):
        for wp in sorted(root.glob(ws_glob)):
            if wp.is_file():
                rel_dir = str(wp.parent.relative_to(root)).replace("\\", "/")
                workspace_pkg_scripts[rel_dir] = _get_sigs(wp).get("symbols") or {}

    makefile_path = root / "Makefile"
    make_targets: Optional[Dict[str, Any]] = None
    if makefile_path.is_file():
        make_targets = _get_sigs(makefile_path).get("symbols") or {}

    # Pre-index repo files by basename and relative path suffix for monorepo/subpackage resolution
    by_basename: Dict[str, List[Path]] = {}
    all_rel_paths: List[Tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDED_DIRS and not d.startswith(".git") and not d.startswith(".venv")
        ]
        for fn in filenames:
            p_full = Path(dirpath) / fn
            try:
                rel_s = str(p_full.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            by_basename.setdefault(fn, []).append(p_full)
            all_rel_paths.append((rel_s, p_full))

    git_diff_data: Optional[Dict[str, Any]] = None
    if base_ref:
        git_diff_data = _compute_git_diff_changes(root, base_ref=base_ref, head_ref=head_ref)

    findings: List[Dict[str, Any]] = []
    seen_finding_keys: Set[Tuple[Any, ...]] = set()
    blast_radius: List[Dict[str, Any]] = []
    total_references_checked = 0

    def _add_finding(
        *,
        rule_id: str,
        severity: str,
        doc_rel: str,
        line_no: int,
        target_path: str,
        symbol: Optional[str],
        message: str,
        remediation: str,
    ) -> None:
        key = (rule_id, doc_rel, line_no, target_path, symbol or "")
        if key in seen_finding_keys:
            return
        seen_finding_keys.add(key)
        findings.append({
            "ruleId": rule_id,
            "severity": severity,
            "docPath": doc_rel,
            "line": line_no,
            "targetPath": target_path,
            "symbol": symbol,
            "message": message,
            "remediation": remediation,
        })

    if git_diff_data and git_diff_data.get("error"):
        _add_finding(
            rule_id="invalid_git_ref",
            severity="error",
            doc_rel="(git-diff)",
            line_no=1,
            target_path=str(base_ref or ""),
            symbol=str(head_ref or "HEAD"),
            message=f"Git diff 참조(`{base_ref}..{head_ref}`)를 해석할 수 없어요: {git_diff_data['error']}",
            remediation="존재하는 Git 브랜치·태그·커밋 SHA를 `--base` 및 `--head`에 지정하세요.",
        )

    cd_cmd_re = re.compile(r"(?:^|[`;&|]\s*|\$\s*)cd\s+([A-Za-z0-9_./-]+)")
    pkg_dir_hint_re = re.compile(
        r"(?:inside\s+(?:the\s+)?package\s+(?:directory|folder)|in\s+(?:the\s+)?package\s+(?:directory|folder)|in\s+each\s+package|package\s+(?:directory|folder)|packages/[A-Za-z0-9_.-]+|examples/[A-Za-z0-9_.-]+|apps/[A-Za-z0-9_.-]+)",
        re.IGNORECASE,
    )
    tutorial_create_re = re.compile(
        r"(?:create\s+(?:a\s+|an\s+)?(?:new\s+)?(?:[A-Za-z0-9_+-]+\s+)?(?:file|server|script|block|module|component)|"
        r"save\s+(?:it\s+|new\s+cases\s+)?(?:as|under)|"
        r"for\s+example,\s+`[^`]+`\s+generates\s+`|"
        r"touch\s+|cat\s*<<|파일을\s*생성|새로\s*만들)",
        re.IGNORECASE,
    )
    prohibitive_line_re = re.compile(
        r"(?:\b(?:do\s+not|don't|never|avoid)\s+(?:create|use|add|place|put|import|reference|write|commit)\b|"
        r"\b(?:bad\s+example|anti-pattern|instead\s+of\s+creating)\b)",
        re.IGNORECASE,
    )
    external_repo_re = re.compile(
        r"(?:\bin\s+the\s+(?:public\s+|private\s+)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\s+repo\b|"
        r"\bnot\s+here\s*:|\bin\s+another\s+repo(?:sitory)?\b)",
        re.IGNORECASE,
    )
    ref_link_def_re = re.compile(r"^\s*\[[^\]]+\]:\s*https?://", re.IGNORECASE)
    code_fence_title_re = re.compile(r"^\s*```[A-Za-z0-9_-]*\s+title=", re.IGNORECASE)
    run_bare_re = re.compile(
        r"(?:^\s*(?:\$\s*)?(?:run|execute|python3?|node|tsx|ts-node|bash|sh|zsh|pytest|ruby|go\s+run)\b|"
        r"\b(?:run|execute)\s+`[^`]+`|\b(?:python3?|node|tsx|ts-node|bash|sh|pytest)\s+[A-Za-z0-9_.-]+)",
        re.IGNORECASE,
    )
    root_or_other_ctx_re = re.compile(
        r"(?:\bin\s+the\s+root\b|\broot\s+folder\b|\brepo(?:sitory)?\s+root\b|"
        r"\bother\s+(?:\w+\s+)?examples?\b|\banother\s+example\b)",
        re.IGNORECASE,
    )

    for doc_path in docs:
        try:
            doc_rel = str(doc_path.relative_to(root))
            raw_doc_text = doc_path.read_text(encoding="utf-8", errors="replace")
            # Strip HTML comments <!-- ... --> while preserving exact line numbers
            clean_doc_text = re.sub(
                r"<!--[\s\S]*?-->",
                lambda m: "\n" * m.group(0).count("\n"),
                raw_doc_text,
            )
            lines = clean_doc_text.splitlines()
        except OSError:
            continue

        active_cwd: Path = doc_path.parent
        explicit_cd: bool = False
        in_fence: bool = False
        package_context_active: bool = False
        tutorial_created_files: Set[str] = set()
        recent_tutorial_lines: int = 0

        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue

            if recent_tutorial_lines > 0:
                recent_tutorial_lines -= 1

            if stripped.startswith("```"):
                if in_fence:
                    in_fence = False
                    active_cwd = doc_path.parent
                    explicit_cd = False
                else:
                    in_fence = True

            # Reset active_cwd on Markdown headings or when explicitly switching back to root/other example
            if stripped.startswith("#") or root_or_other_ctx_re.search(line):
                active_cwd = doc_path.parent
                explicit_cd = False
                if stripped.startswith("#"):
                    package_context_active = bool(pkg_dir_hint_re.search(stripped))
                else:
                    package_context_active = True

            if pkg_dir_hint_re.search(line):
                package_context_active = True

            # Track `cd <subdir>` changes within code blocks or command instructions
            for m_cd in cd_cmd_re.finditer(line):
                cd_arg = m_cd.group(1).strip().strip("`'\"")
                if not cd_arg:
                    continue
                for base_cand in (active_cwd, root, doc_path.parent):
                    cand_dir = (base_cand / cd_arg).resolve()
                    if cand_dir.is_dir() and cand_dir.is_relative_to(root):
                        active_cwd = cand_dir
                        explicit_cd = active_cwd.resolve() != doc_path.parent.resolve()
                        break

            # 1. Check npm / pnpm / yarn / bun script references against active_cwd or root package.json
            if pkg_scripts is not None or workspace_pkg_scripts:
                script_matches: List[str] = []
                for m_npm in NPM_SCRIPT_RE.finditer(line):
                    script_matches.append(m_npm.group("script"))
                for m_yarn in YARN_SHORT_SCRIPT_RE.finditer(line):
                    scr_val = m_yarn.group("script") or m_yarn.group("script2")
                    if scr_val:
                        script_matches.append(scr_val)
                for scr in script_matches:
                    total_references_checked += 1
                    cwd_pkg = active_cwd / "package.json"
                    cwd_scripts = (_get_sigs(cwd_pkg).get("symbols") or {}) if cwd_pkg.is_file() else {}
                    found_in_cwd = scr in cwd_scripts
                    found_in_root = (not explicit_cd) and (pkg_scripts is not None and scr in pkg_scripts)
                    found_in_ws = False
                    if not explicit_cd and not found_in_cwd and not found_in_root and package_context_active and workspace_pkg_scripts:
                        found_in_ws = any(scr in ws_s for ws_s in workspace_pkg_scripts.values())
                    if not (found_in_cwd or found_in_root or found_in_ws):
                        target_pkg_rel = (
                            str(cwd_pkg.relative_to(root)).replace("\\", "/")
                            if cwd_pkg.is_file() and cwd_pkg.is_relative_to(root)
                            else "package.json"
                        )
                        avail_keys = sorted((cwd_scripts if explicit_cd else (cwd_scripts or pkg_scripts or {})).keys())[:8]
                        _add_finding(
                            rule_id="missing_script",
                            severity="error",
                            doc_rel=doc_rel,
                            line_no=idx,
                            target_path=target_pkg_rel,
                            symbol=scr,
                            message=f"`{doc_rel}:{idx}`에서 참조한 스크립트 `{scr}`가 `{target_pkg_rel}`의 `scripts`에 존재하지 않아요.",
                            remediation=f"`{target_pkg_rel}`의 `scripts` 목록({avail_keys})에 맞춰 문서의 실행 명령을 수정하세요.",
                        )

            # 2. Check `make <target>` references against Makefile
            if make_targets is not None:
                for m_mk in MAKE_TARGET_RE.finditer(line):
                    tgt = m_mk.group("target") or m_mk.group("target2")
                    if not tgt or tgt.lower() in MAKE_PROSE_STOPWORDS:
                        continue
                    total_references_checked += 1
                    if tgt not in make_targets:
                        _add_finding(
                            rule_id="missing_script",
                            severity="error",
                            doc_rel=doc_rel,
                            line_no=idx,
                            target_path="Makefile",
                            symbol=tgt,
                            message=f"`{doc_rel}:{idx}`에서 참조한 Make 타겟 `{tgt}`가 `Makefile`에 정의되어 있지 않아요.",
                            remediation=f"`Makefile`의 정의된 타겟({sorted(make_targets.keys())[:8]})과 문서 명령을 동기화하세요.",
                        )

            # Skip file reference extraction on Markdown reference-link definitions, code fence title headers, prohibitive/anti-pattern lines, or explicit external-repo lines
            if (
                ref_link_def_re.match(line)
                or code_fence_title_re.match(line)
                or prohibitive_line_re.search(line)
                or external_repo_re.search(line)
            ):
                continue

            is_tutorial_create_line = bool(tutorial_create_re.search(line))
            if is_tutorial_create_line:
                recent_tutorial_lines = 4

            # Strip Markdown reference-style link keys `[label][ref_id]` so `ref_id` is not misparsed as a local path
            line_for_paths = re.sub(r"(\[[^\]]+\])\[[^\]]+\]", r"\1", line)

            # 3. Extract file & explicit symbol references on this line
            line_refs: List[Tuple[str, Optional[str]]] = []
            for m_exp in EXPLICIT_ANCHOR_RE.finditer(line_for_paths):
                sym = m_exp.group("symbol")
                if sym and re.match(r"^L\d+", sym):
                    sym = None
                line_refs.append((m_exp.group("path"), sym))
            for m_bt in BACKTICK_FILE_RE.finditer(line_for_paths):
                sym = m_bt.group("symbol")
                if sym and re.match(r"^L\d+", sym):
                    sym = None
                line_refs.append((m_bt.group("path"), sym))
            for m_md in MD_LINK_FILE_RE.finditer(line_for_paths):
                sym = m_md.group("anchor")
                if sym and re.match(r"^L\d+", sym):
                    sym = None
                line_refs.append((m_md.group("path"), sym))
            for m_sl in SLASH_PATH_RE.finditer(line_for_paths):
                line_refs.append((m_sl.group("path"), m_sl.group(2)))

            seen_line_paths: Set[Tuple[str, Optional[str]]] = set()
            resolved_line_files: List[Tuple[str, Path, Dict[str, Any]]] = []
            for raw_p, exp_sym in line_refs:
                clean_p = raw_p.strip()
                while clean_p.startswith("./"):
                    clean_p = clean_p[2:]
                clean_p = clean_p.lstrip("/")
                if not clean_p or (exp_sym is None and clean_p.lower() in IGNORED_FILE_TOKENS):
                    continue
                if is_tutorial_create_line:
                    tutorial_created_files.add(clean_p)
                    continue
                if clean_p in tutorial_created_files:
                    continue
                if clean_p.lower().endswith((".md", ".mdx", ".rst", ".txt", ".html", ".htm")):
                    # Markdown section heading anchors (#section-name) are not code symbol anchors
                    exp_sym = None
                if clean_p.startswith((
                    "http://",
                    "https://",
                    "github.com/",
                    "www.github.com/",
                    "gitlab.com/",
                    "raw.githubusercontent.com/",
                    "Users/",
                    "var/",
                    "tmp/",
                    "private/",
                    "node_modules/",
                    "dist/",
                    "build/",
                    ".cache/",
                    ".auth/",
                    "output/",
                    "work/",
                    "memories/",
                    "wt/",
                )):
                    continue
                if f"@{clean_p}" in line or re.match(r"^[A-Z][A-Z0-9_]{1,}/", clean_p):
                    continue
                first_seg = clean_p.split("/", 1)[0]
                if "/" in clean_p and ("." in first_seg or first_seg in ("React", "Vue", "Angular", "Svelte")):
                    continue
                lower_p = clean_p.lower()
                if (
                    "path/to/" in lower_p
                    or "/path/" in lower_p
                    or "my_provider/" in lower_p
                    or "my_block" in lower_p
                    or "/__generated__/" in lower_p
                    or lower_p.startswith("__generated__/")
                    or "2025-06-15-" in lower_p
                    or lower_p in ("src/config/database.ts", "src/db/client.ts")
                    or lower_p.endswith(("/foo.py", "/test_foo.py", "/bar.py", "/example.py", "/example.ts"))
                    or lower_p in ("foo.py", "bar.py", "test_foo.py", "example.py", "example.ts")
                ):
                    continue
                if (clean_p, exp_sym) in seen_line_paths:
                    continue
                seen_line_paths.add((clean_p, exp_sym))
                total_references_checked += 1

                # Resolve relative to active_cwd (from `cd <subdir>`), repo root, doc parent, .github/workflows, or monorepo subpaths
                candidate = active_cwd / clean_p
                if not explicit_cd:
                    if not candidate.is_file() and (root / clean_p).is_file():
                        candidate = root / clean_p
                    if not candidate.is_file() and (doc_path.parent / clean_p).is_file():
                        candidate = doc_path.parent / clean_p
                    if not candidate.is_file() and "/" not in clean_p and clean_p.endswith((".yml", ".yaml")):
                        wf_cand = root / ".github" / "workflows" / clean_p
                        if wf_cand.is_file():
                            candidate = wf_cand
                    if not candidate.is_file():
                        if "/" in clean_p:
                            suffix_match = "/" + clean_p.lstrip("/")
                            for rel_s, p_full in all_rel_paths:
                                if rel_s == clean_p or rel_s.endswith(suffix_match):
                                    candidate = p_full
                                    break
                        elif clean_p in by_basename and exp_sym is None:
                            candidate = by_basename[clean_p][0]
                else:
                    if not candidate.is_file() and (root / clean_p).is_file():
                        try:
                            rel_cwd = str(active_cwd.resolve().relative_to(root)).replace("\\", "/")
                            if clean_p.startswith(rel_cwd + "/"):
                                candidate = root / clean_p
                        except ValueError:
                            pass

                if not candidate.exists() or not candidate.is_file():
                    # Ignore bare filenames without directory prefix unless an explicit symbol anchor or execution verb on a code file is present
                    if exp_sym is None and "/" not in clean_p:
                        if raw_p.strip().startswith("./") or not clean_p.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")) or not run_bare_re.search(line):
                            continue
                    _add_finding(
                        rule_id="missing_path",
                        severity="error",
                        doc_rel=doc_rel,
                        line_no=idx,
                        target_path=clean_p,
                        symbol=exp_sym,
                        message=f"`{doc_rel}:{idx}`에서 참조한 파일 `{clean_p}`를 저장소에서 찾을 수 없어요.",
                        remediation=f"삭제되었거나 이동된 경로 `{clean_p}`를 최신 파일 경로로 교체하세요.",
                    )
                    continue

                try:
                    rel_target = (
                        str(candidate.resolve().relative_to(root))
                        if candidate.resolve().is_relative_to(root)
                        else str(candidate.relative_to(root))
                    )
                except ValueError:
                    rel_target = clean_p
                sig_info = _get_sigs(candidate)
                resolution = sig_info.get("__resolution__", "unsupported")
                syntax_ok = sig_info.get("__syntax_ok__", True)
                symbols_map = sig_info.get("symbols") or {}
                resolved_line_files.append((rel_target, candidate, sig_info))

                if resolution == "parse_error" or (not syntax_ok and resolution in ("exact", "contract")):
                    _add_finding(
                        rule_id="syntax_error",
                        severity="error",
                        doc_rel=doc_rel,
                        line_no=idx,
                        target_path=rel_target,
                        symbol=exp_sym,
                        message=f"`{doc_rel}:{idx}`에서 참조한 코드 파일 `{rel_target}`에 구문 오류(Syntax/Parse Error)가 있어요.",
                        remediation=f"`{rel_target}`의 괄호/문자열/구문 오류를 먼저 수리하세요.",
                    )
                    continue

                if exp_sym:
                    if resolution == "unsupported":
                        _add_finding(
                            rule_id="unsupported_anchor",
                            severity="warning",
                            doc_rel=doc_rel,
                            line_no=idx,
                            target_path=rel_target,
                            symbol=exp_sym,
                            message=f"`{doc_rel}:{idx}`의 `{rel_target}::{exp_sym}` 앵커는 현재 AST/계약 파서가 지원하지 않는 언어(`{sig_info.get('__language__', 'unknown')}`)예요.",
                            remediation="해당 언어 파일은 파일 해시 변경 시 수동 검토 대상(`unsupported_anchor`)으로 분류돼요.",
                        )
                    elif exp_sym not in symbols_map:
                        _add_finding(
                            rule_id="missing_symbol",
                            severity="error",
                            doc_rel=doc_rel,
                            line_no=idx,
                            target_path=rel_target,
                            symbol=exp_sym,
                            message=f"`{doc_rel}:{idx}`에서 참조한 심볼 `{exp_sym}`이(가) `{rel_target}` 파일에 존재하지 않아요.",
                            remediation=f"`{rel_target}`의 현재 심볼({sorted(symbols_map.keys())[:8]})에 맞게 문서를 갱신하세요.",
                        )

            # 4. If at least one supported code file was referenced on this line, inspect inline `func(...)` calls on the same line!
            for rel_target, candidate, sig_info in resolved_line_files:
                resolution = sig_info.get("__resolution__", "unsupported")
                if resolution not in ("exact", "contract"):
                    continue
                symbols_map = sig_info.get("symbols") or {}
                target_stem = Path(rel_target).stem
                is_example_script = rel_target.startswith("examples/") or "/examples/" in rel_target
                for m_call in INLINE_CALL_RE.finditer(line):
                    call_name = m_call.group("name")
                    if call_name in PYTHON_BUILTINS_AND_COMMON:
                        continue
                    if call_name.startswith("__") and not call_name.endswith("__"):
                        continue
                    pos_cnt, kw_list, is_def_like = _parse_call_arguments(m_call.group("args"))
                    if call_name in symbols_map:
                        drift_msg = _check_inline_call_vs_signature(
                            call_name, pos_cnt, kw_list, is_def_like, symbols_map[call_name]
                        )
                        if drift_msg:
                            _add_finding(
                                rule_id="signature_drift",
                                severity="error",
                                doc_rel=doc_rel,
                                line_no=idx,
                                target_path=rel_target,
                                symbol=call_name,
                                message=f"`{doc_rel}:{idx}`의 `{rel_target}::{call_name}` 호출 계약 불일치: {drift_msg}",
                                remediation=f"`{rel_target}`의 최신 `{call_name}` 시그니처에 맞춰 인자 목록을 업데이트하세요.",
                            )
                    else:
                        # Only flag `missing_symbol` for inline calls when explicitly qualified with target module stem
                        # (explicit `path::symbol` and `path#symbol` anchors are already checked above).
                        is_stem_qualified = f"{target_stem}.{call_name}(" in line
                        if len(resolved_line_files) == 1 and is_stem_qualified:
                            _add_finding(
                                rule_id="missing_symbol",
                                severity="error",
                                doc_rel=doc_rel,
                                line_no=idx,
                                target_path=rel_target,
                                symbol=call_name,
                                message=f"`{doc_rel}:{idx}`에서 호출한 `{call_name}(...)` 심볼을 `{rel_target}`에서 찾을 수 없어요.",
                                remediation=f"`{rel_target}`에서 이름이 변경되었거나 삭제된 함수 `{call_name}` 참조를 수정하세요.",
                            )

                # 5. Git-diff blast radius mapping for this line
                if git_diff_data and rel_target in git_diff_data.get("changedFiles", {}):
                    f_delta = git_diff_data["changedFiles"][rel_target]
                    impacted_syms: List[str] = []
                    for del_sym in f_delta.get("deletedSymbols", []):
                        if re.search(rf"\b{re.escape(del_sym)}\b", line):
                            impacted_syms.append(f"deleted:{del_sym}")
                            _add_finding(
                                rule_id="missing_symbol",
                                severity="error",
                                doc_rel=doc_rel,
                                line_no=idx,
                                target_path=rel_target,
                                symbol=del_sym,
                                message=f"Git diff(`{base_ref}..{head_ref}`)에서 삭제된 `{rel_target}::{del_sym}` 심볼을 `{doc_rel}:{idx}`가 여전히 참조하고 있어요.",
                                remediation=f"`{doc_rel}:{idx}`에서 삭제된 `{del_sym}` 절차를 제거하거나 대체 API로 갱신하세요.",
                            )
                    for brk_sym, brk_reason in (f_delta.get("breakingSymbols") or {}).items():
                        if re.search(rf"\b{re.escape(brk_sym)}\b", line):
                            impacted_syms.append(f"breaking:{brk_sym}({brk_reason})")
                            _add_finding(
                                rule_id="signature_drift",
                                severity="error",
                                doc_rel=doc_rel,
                                line_no=idx,
                                target_path=rel_target,
                                symbol=brk_sym,
                                message=f"Git diff(`{base_ref}..{head_ref}`)에서 `{rel_target}::{brk_sym}`의 호출 계약이 변경(`{brk_reason}`)되어 `{doc_rel}:{idx}` 절차가 깨졌어요.",
                                remediation=f"`{doc_rel}:{idx}`의 `{brk_sym}` 사용법을 최신 커밋 계약에 맞게 갱신하고 재검증하세요.",
                            )
                    blast_radius.append({
                        "docPath": doc_rel,
                        "line": idx,
                        "targetPath": rel_target,
                        "gitStatus": f_delta.get("status"),
                        "impactedSymbols": impacted_syms,
                    })

        # 6. If this document is a `SKILL.md` with a sibling `references/provenance.json`, audit its stored anchors too!
        if doc_path.name == "SKILL.md":
            prov_path = doc_path.parent / "references" / "provenance.json"
            if prov_path.is_file():
                try:
                    prov = json.loads(prov_path.read_text(encoding="utf-8"))
                    ref_files = prov.get("referencedFiles") or []
                    for entry in ref_files:
                        if not isinstance(entry, dict) or not entry.get("path"):
                            continue
                        total_references_checked += 1
                        raw_entry_path = str(entry["path"])
                        p_ref = Path(raw_entry_path)
                        if not p_ref.is_absolute():
                            p_ref = root / raw_entry_path
                        rel_ref = (
                            str(p_ref.resolve().relative_to(root))
                            if p_ref.exists() and p_ref.resolve().is_relative_to(root)
                            else raw_entry_path
                        )
                        if not p_ref.is_file():
                            _add_finding(
                                rule_id="missing_path",
                                severity="error",
                                doc_rel=doc_rel,
                                line_no=1,
                                target_path=rel_ref,
                                symbol=None,
                                message=f"`{doc_rel}`(`provenance.json`)이 추적 중인 파일 `{rel_ref}`이(가) 존재하지 않아요.",
                                remediation="스킬의 참조 파일 경로를 갱신하거나 스킬을 재생성하세요.",
                            )
                            continue
                        sig_info = _get_sigs(p_ref)
                        curr_sigs = sig_info.get("symbols") or {}
                        curr_hashes = _get_hashes(p_ref)
                        resolution = sig_info.get("__resolution__", "unsupported")
                        tracked_syms = entry.get("symbols") if isinstance(entry.get("symbols"), dict) else {}
                        tracked_sigs = entry.get("symbolSignatures") if isinstance(entry.get("symbolSignatures"), dict) else {}
                        if resolution == "unsupported" and tracked_syms:
                            for s_name in tracked_syms:
                                _add_finding(
                                    rule_id="unsupported_anchor",
                                    severity="warning",
                                    doc_rel=doc_rel,
                                    line_no=1,
                                    target_path=rel_ref,
                                    symbol=s_name,
                                    message=f"`{doc_rel}`의 `{rel_ref}::{s_name}` 앵커는 지원되지 않는 언어(`{sig_info.get('__language__', 'unknown')}`)예요.",
                                    remediation="지원 언어(Python/TS/JS/Makefile/package.json) 앵커 또는 실행 검증 명령을 연결하세요.",
                                )
                        for s_name, old_sha in tracked_syms.items():
                            if s_name not in curr_hashes:
                                _add_finding(
                                    rule_id="missing_symbol",
                                    severity="error",
                                    doc_rel=doc_rel,
                                    line_no=1,
                                    target_path=rel_ref,
                                    symbol=s_name,
                                    message=f"`{doc_rel}`(`provenance.json`)이 추적 중인 심볼 `{rel_ref}::{s_name}`이(가) 삭제되었어요.",
                                    remediation=f"`{doc_rel}` 절차를 최신 심볼에 맞게 수정하세요.",
                                )
                            elif curr_hashes.get(s_name) != old_sha and s_name in tracked_sigs and s_name in curr_sigs:
                                is_brk, brk_reason = is_breaking_symbol_change(tracked_sigs[s_name], curr_sigs[s_name])
                                if is_brk:
                                    _add_finding(
                                        rule_id="signature_drift",
                                        severity="error",
                                        doc_rel=doc_rel,
                                        line_no=1,
                                        target_path=rel_ref,
                                        symbol=s_name,
                                        message=f"`{doc_rel}`(`provenance.json`)의 `{rel_ref}::{s_name}` 시그니처가 호환되지 않게 변경(`{brk_reason}`)되었어요.",
                                        remediation=f"`{doc_rel}` 절차를 업데이트하고 `verify`를 다시 실행하세요.",
                                    )
                except (OSError, json.JSONDecodeError):
                    pass

    error_count = sum(1 for f in findings if f["severity"] == "error")
    warning_count = sum(1 for f in findings if f["severity"] == "warning")
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    by_rule: Dict[str, int] = {}
    for f in findings:
        by_rule[f["ruleId"]] = by_rule.get(f["ruleId"], 0) + 1

    report: Dict[str, Any] = {
        "ok": error_count == 0,
        "exitCode": 0 if error_count == 0 else 1,
        "repoPath": str(root),
        "scannedDocsCount": len(docs),
        "scannedInstructionFiles": len(docs),
        "scannedDocs": [str(p.relative_to(root)) for p in docs],
        "referencesChecked": total_references_checked,
        "totalReferencesChecked": total_references_checked,
        "findingsCount": len(findings),
        "findingCount": len(findings),
        "errorCount": error_count,
        "warningCount": warning_count,
        "countsByRule": by_rule,
        "findings": findings,
        "blastRadius": blast_radius,
        "gitDiff": git_diff_data,
        "elapsedMs": elapsed_ms,
    }

    fmt = (output_format or "json").strip().lower()
    if fmt == "markdown":
        report["formatted"] = format_audit_markdown(report)
    elif fmt == "sarif":
        report["sarif"] = format_audit_sarif(report)
        report["formatted"] = json.dumps(report["sarif"], ensure_ascii=False, indent=2)
    return report


def format_audit_markdown(report: Dict[str, Any]) -> str:
    """Format an `audit_repository` report as a GitHub/PR Markdown summary."""
    status_badge = "PASS (0 broken contracts)" if report.get("ok") else f"FAIL ({report.get('errorCount', 0)} errors)"
    lines = [
        f"## DeltaHarness Procedure & Instruction Audit — {status_badge}",
        "",
        f"- **Repository**: `{report.get('repoPath')}`",
        f"- **Scanned Docs**: {report.get('scannedDocsCount', 0)} files ({report.get('referencesChecked', 0)} references checked in {report.get('elapsedMs', 0)} ms)",
        f"- **Findings**: {report.get('errorCount', 0)} errors, {report.get('warningCount', 0)} warnings",
        "",
    ]
    findings = report.get("findings") or []
    if not findings:
        lines.append("모든 지침 문서(`CLAUDE.md`, `AGENTS.md`, `SKILL.md` 등)와 실제 코드·스크립트 시그니처가 일치해요.")
    else:
        lines.extend([
            "| Severity | Rule | Location | Target | Message |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        for f in findings:
            loc = f"`{f['docPath']}:{f['line']}`"
            tgt = f"`{f['targetPath']}::{f['symbol']}`" if f.get("symbol") else f"`{f['targetPath']}`"
            lines.append(f"| **{f['severity'].upper()}** | `{f['ruleId']}` | {loc} | {tgt} | {f['message']} |")
    blast = report.get("blastRadius") or []
    if blast:
        lines.extend([
            "",
            "### Git-Diff Blast Radius",
            "",
        ])
        for b in blast:
            syms = ", ".join(b.get("impactedSymbols") or []) or "file-level reference"
            lines.append(f"- `{b['docPath']}:{b['line']}` ← `{b['targetPath']}` (`{b.get('gitStatus')}`: {syms})")
    return "\n".join(lines) + "\n"


def format_audit_sarif(report: Dict[str, Any]) -> Dict[str, Any]:
    """Format an `audit_repository` report as SARIF v2.1.0 for GitHub Code Scanning."""
    rules = [
        {
            "id": "missing_path",
            "name": "MissingReferencedFilePath",
            "shortDescription": {"text": "Instruction document references a non-existent repository file path."},
        },
        {
            "id": "missing_script",
            "name": "MissingPackageOrMakeScript",
            "shortDescription": {"text": "Instruction document invokes a script/target missing from package.json or Makefile."},
        },
        {
            "id": "missing_symbol",
            "name": "MissingCodeSymbol",
            "shortDescription": {"text": "Instruction document references a function, class, or interface missing from target code file."},
        },
        {
            "id": "signature_drift",
            "name": "BreakingCallSignatureDrift",
            "shortDescription": {"text": "Instruction document call contract drifts from current code signature."},
        },
        {
            "id": "unsupported_anchor",
            "name": "UnsupportedLanguageAnchor",
            "shortDescription": {"text": "Symbol anchor attached to a file without structural AST contract support."},
        },
        {
            "id": "invalid_git_ref",
            "name": "InvalidGitComparisonRef",
            "shortDescription": {"text": "Specified Git base or head ref could not be resolved by git diff."},
        },
        {
            "id": "syntax_error",
            "name": "TargetFileSyntaxError",
            "shortDescription": {"text": "Referenced source file contains a syntax or parse error."},
        },
    ]
    results = []
    for f in report.get("findings") or []:
        level = "error" if f.get("severity") == "error" else "warning"
        results.append({
            "ruleId": f["ruleId"],
            "level": level,
            "message": {"text": f"{f['message']} ({f.get('remediation', '')})".strip()},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": f["docPath"]},
                        "region": {"startLine": max(1, int(f.get("line") or 1))},
                    }
                }
            ],
        })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "DeltaHarness",
                        "informationUri": "https://github.com/ian-nocturne/deltaharness",
                        "version": "8.2.0",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }


audit_repo = audit_repository


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="DeltaHarness Procedure & Instruction Auditor")
    parser.add_argument("repo_positional", nargs="?", default=None, help="Target repository path")
    parser.add_argument("--repo-path", default=None, help="Target repository path")
    parser.add_argument("--base", default=None, help="Git base ref for blast-radius diff")
    parser.add_argument("--head", default="HEAD", help="Git head ref for blast-radius diff")
    parser.add_argument("--format", default="json", choices=["json", "markdown", "sarif"], help="Output format")
    parser.add_argument("--exit-zero", action="store_true", help="Always return exit code 0 even when errors are found")
    args = parser.parse_args(argv)

    target = args.repo_path or args.repo_positional or "."
    res = audit_repository(target, base_ref=args.base, head_ref=args.head, output_format=args.format)
    if args.format in ("markdown", "sarif") and "formatted" in res:
        print(res["formatted"])
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    if args.exit_zero:
        return 0
    return int(res.get("exitCode", 0 if res.get("ok", True) else 1))


if __name__ == "__main__":
    import sys
    sys.exit(main())

