"""Polyglot structural symbol anchoring, call-contract extraction, and breaking-change guard.

Supports:
- Python (.py): Exact AST call-contract & docstring-invariant body hash
- TypeScript / JavaScript (.ts, .tsx, .js, .jsx, .mjs, .cjs): Function, arrow function, class, method, interface, and type contracts
- package.json: npm/pnpm/yarn script contracts (`scripts.<name>`)
- Makefile / *.mk: Make target contracts
- Explicit resolution status (`exact`, `contract`, `unsupported`, `parse_error`) so unsupported files are never silently treated as verified symbols.
"""
import ast
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_FILE_HASH_AST_CACHE = {}
_FILE_SIGNATURE_CACHE = {}

_TS_JS_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_MAKE_NAMES = {"makefile", "gnumakefile"}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _digest(value) -> str:
    return _sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def cached_file_sha(file_path) -> str:
    """Fast stat-keyed file SHA-256 helper."""
    p = Path(file_path)
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        cached = _FILE_HASH_AST_CACHE.get(key)
        if cached is not None:
            return cached[0]
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        if len(_FILE_HASH_AST_CACHE) > 512:
            _FILE_HASH_AST_CACHE.clear()
        _FILE_HASH_AST_CACHE[key] = (sha, None)
        return sha
    except OSError:
        return ""


def _strip_docstring_from_body(body_nodes):
    """Return body nodes without a leading docstring Expr(Constant(str)) so comment/docstring-only edits are detected."""
    if (
        body_nodes
        and isinstance(body_nodes[0], ast.Expr)
        and isinstance(getattr(body_nodes[0], "value", None), ast.Constant)
        and isinstance(body_nodes[0].value.value, str)
    ):
        rest = body_nodes[1:]
        return rest if rest else [ast.Pass()]
    return body_nodes


def normalized_symbol_ast_hash(node) -> str:
    """Compute a docstring/comment/lineno-invariant AST hash for a function or class."""
    try:
        cloned = copy.copy(node)
        if hasattr(cloned, "body") and isinstance(cloned.body, list):
            cloned.body = _strip_docstring_from_body(cloned.body)
        return _sha256_text(ast.dump(cloned, annotate_fields=True, include_attributes=False))
    except Exception:
        return _sha256_text(ast.dump(node, annotate_fields=True, include_attributes=False))


def _node_call_signature(node):
    """Extract complete call-contract signature metadata from a Python AST FunctionDef/AsyncFunctionDef/ClassDef."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raw_posonly = [a.arg for a in getattr(node.args, "posonlyargs", []) or []]
        raw_regular = [a.arg for a in getattr(node.args, "args", []) or []]
        pos_names = list(raw_posonly) + list(raw_regular)
        if pos_names and pos_names[0] in ("self", "cls"):
            if raw_posonly and raw_posonly[0] in ("self", "cls"):
                raw_posonly = raw_posonly[1:]
            elif raw_regular and raw_regular[0] in ("self", "cls"):
                raw_regular = raw_regular[1:]
            pos_names = pos_names[1:]
        num_defaults = len(getattr(node.args, "defaults", []) or [])
        req_count = max(0, len(pos_names) - num_defaults)
        req_args = pos_names[:req_count]
        opt_args = pos_names[req_count:]
        kwonly_nodes = list(getattr(node.args, "kwonlyargs", []) or [])
        kw_defaults = list(getattr(node.args, "kw_defaults", []) or [])
        kwonly_req = [
            kw.arg for kw, d in zip(kwonly_nodes, kw_defaults)
            if d is None
        ]
        kwonly_all = [kw.arg for kw in kwonly_nodes]
        has_vararg = bool(getattr(node.args, "vararg", None))
        has_kwarg = bool(getattr(node.args, "kwarg", None))
        is_async = isinstance(node, ast.AsyncFunctionDef)
        sig_payload = {
            "kind": "function",
            "isAsync": is_async,
            "posonlyArgs": raw_posonly,
            "regularArgs": raw_regular,
            "reqArgs": req_args,
            "optArgs": opt_args,
            "allArgs": pos_names,
            "kwonlyReq": kwonly_req,
            "kwonlyAll": kwonly_all,
            "hasVararg": has_vararg,
            "hasKwarg": has_kwarg,
            "normAstHash": normalized_symbol_ast_hash(node),
        }
        sig_payload["sigHash"] = _digest({
            "kind": "function",
            "isAsync": is_async,
            "posonlyArgs": raw_posonly,
            "reqArgs": req_args,
            "allArgs": pos_names,
            "kwonlyReq": kwonly_req,
            "kwonlyAll": kwonly_all,
            "hasVararg": has_vararg,
            "hasKwarg": has_kwarg,
        })
        return sig_payload
    if isinstance(node, ast.ClassDef):
        methods = sorted({
            child.name
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not (child.name.startswith("_") and child.name != "__init__")
        })
        init_sig = None
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == "__init__":
                init_sig = _node_call_signature(child)
                break
        sig_payload = {
            "kind": "class",
            "methods": methods,
            "posonlyArgs": init_sig["posonlyArgs"] if init_sig else [],
            "regularArgs": init_sig["regularArgs"] if init_sig else [],
            "reqArgs": init_sig["reqArgs"] if init_sig else [],
            "optArgs": init_sig["optArgs"] if init_sig else [],
            "allArgs": init_sig["allArgs"] if init_sig else [],
            "kwonlyReq": init_sig["kwonlyReq"] if init_sig else [],
            "kwonlyAll": init_sig["kwonlyAll"] if init_sig else [],
            "hasVararg": init_sig["hasVararg"] if init_sig else False,
            "hasKwarg": init_sig["hasKwarg"] if init_sig else False,
            "normAstHash": normalized_symbol_ast_hash(node),
        }
        sig_payload["sigHash"] = _digest({
            "kind": "class",
            "methods": methods,
            "posonlyArgs": sig_payload["posonlyArgs"],
            "reqArgs": sig_payload["reqArgs"],
            "allArgs": sig_payload["allArgs"],
            "kwonlyReq": sig_payload["kwonlyReq"],
            "kwonlyAll": sig_payload["kwonlyAll"],
        })
        return sig_payload
    return None


def _collect_scoped_ast_nodes(body, prefix="", out=None):
    if out is None:
        out = []
    for node in body or []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = getattr(node, "name", "")
            if not name:
                continue
            qname = f"{prefix}.{name}" if prefix else name
            out.append((qname, name, bool(prefix), node))
            child_prefix = f"{qname}.<locals>" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else qname
            _collect_scoped_ast_nodes(getattr(node, "body", []), child_prefix, out)
        else:
            for attr in ("body", "orelse", "finalbody"):
                sub_body = getattr(node, attr, None)
                if isinstance(sub_body, list):
                    _collect_scoped_ast_nodes(sub_body, prefix, out)
            for handler in getattr(node, "handlers", None) or []:
                h_body = getattr(handler, "body", None)
                if isinstance(h_body, list):
                    _collect_scoped_ast_nodes(h_body, prefix, out)
    return out


def _split_top_level_commas(text: str):
    """Split parameter string by top-level commas, ignoring commas inside <...>, (...), {...}, [...]."""
    parts = []
    buf = []
    depth_angle = 0
    depth_paren = 0
    depth_brace = 0
    depth_brack = 0
    for ch in text or "":
        if ch == "<":
            depth_angle += 1
        elif ch == ">" and depth_angle > 0:
            depth_angle -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")" and depth_paren > 0:
            depth_paren -= 1
        elif ch == "{":
            depth_brace += 1
        elif ch == "}" and depth_brace > 0:
            depth_brace -= 1
        elif ch == "[":
            depth_brack += 1
        elif ch == "]" and depth_brack > 0:
            depth_brack -= 1
        elif ch == "," and depth_angle == 0 and depth_paren == 0 and depth_brace == 0 and depth_brack == 0:
            token = "".join(buf).strip()
            if token:
                parts.append(token)
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_ts_js_params(raw_params: str):
    """Parse TypeScript/JavaScript parameter list into reqArgs, optArgs, allArgs, hasVararg."""
    req_args = []
    opt_args = []
    all_args = []
    has_vararg = False
    for idx, tok in enumerate(_split_top_level_commas(raw_params)):
        cleaned = re.sub(r"/\*.*?\*/", "", tok).strip()
        if not cleaned:
            continue
        # Remove access modifiers in TS constructors (public, private, protected, readonly)
        cleaned = re.sub(r"^(?:public|private|protected|readonly)\s+", "", cleaned).strip()
        if cleaned.startswith("..."):
            has_vararg = True
            continue
        has_default = False
        depth = 0
        for i, c in enumerate(cleaned):
            if c in "<({[":
                depth += 1
            elif c in ">)}]" and depth > 0:
                depth -= 1
            elif c == "=" and depth == 0 and (i == 0 or cleaned[i - 1] != "=") and (i + 1 >= len(cleaned) or cleaned[i + 1] not in ("=", ">")):
                has_default = True
                cleaned = cleaned[:i].strip()
                break
        # Now split name from `: Type` at depth 0
        param_head = cleaned.split(":", 1)[0].strip()
        is_optional = has_default or param_head.endswith("?")
        param_name = param_head.rstrip("?").strip()
        if not param_name or param_name == "this":
            continue
        if param_name.startswith("{") or param_name.startswith("["):
            param_name = f"destructured_{idx}"
        all_args.append(param_name)
        if is_optional and not opt_args and req_args:
            opt_args.append(param_name)
        elif is_optional:
            opt_args.append(param_name)
        else:
            req_args.append(param_name)
    return req_args, opt_args, all_args, has_vararg


def _extract_brace_block(src_text: str, open_brace_idx: int) -> str:
    """Extract balanced `{ ... }` block starting at open_brace_idx."""
    if open_brace_idx < 0 or open_brace_idx >= len(src_text) or src_text[open_brace_idx] != "{":
        return ""
    depth = 0
    in_str = None
    escaped = False
    for idx in range(open_brace_idx, len(src_text)):
        ch = src_text[idx]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src_text[open_brace_idx:idx + 1]
    return src_text[open_brace_idx:]


def _blank_js_comments_preserve_lines(text: str, *, blank_strings: bool = False) -> Tuple[str, bool]:
    """Replace JS/TS comments (and optionally string/template/regex interiors) with spaces (preserving newlines) and check syntax balance."""
    out: list[str] = []
    n = len(text)
    i = 0
    in_str: Optional[str] = None
    in_regex = False
    in_regex_class = False
    in_line_comment = False
    in_block_comment = False
    stack: list[str] = []
    pairs = {")": "(", "}": "{", "]": "["}
    syntax_ok = True
    last_non_ws = ""
    last_word = ""

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append("\n")
            else:
                out.append(" ")
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                out.append("  ")
                in_block_comment = False
                i += 2
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue

        if in_regex:
            if ch == "\\" and i + 1 < n:
                out.append("  " if blank_strings else text[i:i + 2])
                i += 2
                continue
            if ch == "\n":
                # Unterminated regex literal at newline is a JS/TS syntax error
                syntax_ok = False
                in_regex = False
                in_regex_class = False
                out.append("\n")
            elif ch == "[" and not in_regex_class:
                in_regex_class = True
                out.append(" " if blank_strings else ch)
            elif ch == "]" and in_regex_class:
                in_regex_class = False
                out.append(" " if blank_strings else ch)
            elif ch == "/" and not in_regex_class:
                in_regex = False
                last_non_ws = "/"
                out.append("/")
            else:
                out.append(" " if blank_strings else ch)
            i += 1
            continue

        if in_str is not None:
            if ch == "\\" and i + 1 < n:
                out.append("  " if blank_strings else text[i:i + 2])
                i += 2
                continue
            if ch == "\n":
                if in_str in ("'", '"'):
                    syntax_ok = False
                    in_str = None
                out.append("\n")
            elif ch == in_str:
                out.append(ch)
                in_str = None
                last_non_ws = ch
            else:
                out.append(" " if blank_strings else ch)
            i += 1
            continue

        if ch == "/" and nxt == "/":
            out.append("  ")
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            out.append("  ")
            in_block_comment = True
            i += 2
            continue

        # Regex literal start (never after `<` or `>` or before `>` which is JSX `/>`)
        if (
            ch == "/"
            and nxt not in ("/", "*", "=", ">", " ", "\t", "\n", "\r", "")
            and (
                not last_non_ws
                or last_non_ws in "=(,:[!&|?{;~+-*%^"
                or last_word in ("return", "case", "throw", "delete", "void", "typeof", "yield", "await")
            )
        ):
            in_regex = True
            in_regex_class = False
            out.append("/")
            i += 1
            continue

        if ch in ("'", '"', "`"):
            in_str = ch
            out.append(ch)
            i += 1
            continue

        if ch in ("(", "{", "["):
            stack.append(ch)
        elif ch in (")", "}", "]"):
            if not stack or stack[-1] != pairs[ch]:
                syntax_ok = False
            else:
                stack.pop()

        if ch.isalpha() or ch in ("_", "$"):
            # Track identifier words for `return /regex/`
            j = i
            while j < n and (text[j].isalnum() or text[j] in ("_", "$")):
                j += 1
            last_word = text[i:j]
            last_non_ws = text[j - 1]
            out.append(text[i:j])
            i = j
            continue

        if not ch.isspace():
            last_non_ws = ch
            last_word = ""
        out.append(ch)
        i += 1

    if in_block_comment or in_str is not None or in_regex or stack:
        syntax_ok = False

    blanked = "".join(out)
    # Detect empty assignment `const x = ;` or `x = ;`
    if syntax_ok and re.search(r"(?<![=!<>+\-*/%&|^])=\s*;", blanked):
        syntax_ok = False

    return blanked, syntax_ok


def _strip_js_comments(text: str) -> str:
    blanked, _ = _blank_js_comments_preserve_lines(text, blank_strings=False)
    return "\n".join(ln.strip() for ln in blanked.splitlines() if ln.strip())


def _build_ts_func_sig(is_async: bool, raw_params: str, body_text: str) -> dict:
    req_args, opt_args, all_args, has_vararg = _parse_ts_js_params(raw_params)
    norm_body = _sha256_text(_strip_js_comments(body_text))
    sig = {
        "kind": "function",
        "isAsync": bool(is_async),
        "posonlyArgs": [],
        "regularArgs": all_args,
        "reqArgs": req_args,
        "optArgs": opt_args,
        "allArgs": all_args,
        "kwonlyReq": [],
        "kwonlyAll": [],
        "hasVararg": has_vararg,
        "hasKwarg": False,
        "normAstHash": norm_body,
    }
    sig["sigHash"] = _digest({
        "kind": "function",
        "isAsync": bool(is_async),
        "reqArgs": req_args,
        "allArgs": all_args,
        "hasVararg": has_vararg,
    })
    return sig


def _extract_ts_js_signatures(raw_src_text: str) -> Tuple[bool, dict]:
    """Extract function, arrow function, HOC wrapper, class, method, interface, and type contracts from TS/JS."""
    src_text, syntax_ok = _blank_js_comments_preserve_lines(raw_src_text, blank_strings=True)
    sigs = {}

    # 1. Standard function declarations: [export] [default] [async] function Name[<T>](params)
    func_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:declare[ \t]+)?(async[ \t]+)?function[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)[ \t]*(?:<[^>({\n]*>)?[ \t]*\(([^)]*)\)"
    )
    for m in func_pat.finditer(src_text):
        is_async = bool(m.group(1))
        name = m.group(2)
        raw_params = m.group(3) or ""
        brace_pos = src_text.find("{", m.end())
        body_block = _extract_brace_block(src_text, brace_pos) if brace_pos != -1 else m.group(0)
        sigs[name] = _build_ts_func_sig(is_async, raw_params, body_block)

    # 2. Arrow function / function expression assignments (supports both `(a, b) =>` and single-identifier `x =>`)
    arrow_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)[ \t]*(?::[^=\n]+)?=\s*(async[ \t]+)?(?:<[^>({\n]*>\s*)?(?:\(([^)]*)\)|([A-Za-z_$][A-Za-z0-9_$]*))\s*(?::[^=\n>]+)?=>"
    )
    for m in arrow_pat.finditer(src_text):
        name = m.group(1)
        is_async = bool(m.group(2))
        raw_params = m.group(3) if m.group(3) is not None else (m.group(4) or "")
        rest = src_text[m.end():].lstrip()
        if rest.startswith("{"):
            brace_pos = src_text.find("{", m.end())
            body_block = _extract_brace_block(src_text, brace_pos)
        else:
            body_block = rest.splitlines()[0] if rest else m.group(0)
        sigs[name] = _build_ts_func_sig(is_async, raw_params, body_block)

    # 2b. React / generic HOC wrappers (supports single or nested `memo(forwardRef(...))`, `withAuth(...)`, multiline)
    hoc_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:(?:const|let|var)[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)[ \t]*(?::[^=\n]+)?=\s*)?"
        r"(?:[A-Za-z_$][A-Za-z0-9_$.]*\s*(?:<[^>(\n]*>)?\s*\(\s*)+"
        r"(?:(async[ \t]+)?function(?:[ \t]+([A-Za-z_$][A-Za-z0-9_$]*))?\s*\(([^)]*)\)|(async[ \t]+)?(?:\(([^)]*)\)|([A-Za-z_$][A-Za-z0-9_$]*))\s*=>)"
    )
    for m in hoc_pat.finditer(src_text):
        var_name = m.group(1)
        fn_async = bool(m.group(2) or m.group(5))
        fn_name = m.group(3)
        raw_params = (
            m.group(4)
            if m.group(4) is not None
            else (m.group(6) if m.group(6) is not None else (m.group(7) or ""))
        )
        brace_pos = src_text.find("{", m.end())
        body_block = _extract_brace_block(src_text, brace_pos) if brace_pos != -1 else m.group(0)
        sig = _build_ts_func_sig(fn_async, raw_params, body_block)
        if var_name:
            sigs[var_name] = sig
        if fn_name:
            sigs[fn_name] = sig

    # 3. Classes and their methods: [export] [default] [abstract] class Name
    class_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:abstract[ \t]+)?class[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)[^{;\n]*\{"
    )
    method_pat = re.compile(
        r"(?m)^[ \t]*(?:(?:public|private|protected|static|readonly|override|abstract)[ \t]+)*(async[ \t]+)?([A-Za-z_$][A-Za-z0-9_$]*)[ \t]*(?:<[^>({\n]*>)?[ \t]*\(([^)]*)\)[ \t]*(?::[^{\n;]+)?\{"
    )
    reserved_keywords = {"if", "for", "while", "switch", "catch", "function", "return", "sizeof", "typeof"}
    by_short_method = {}
    for cm in class_pat.finditer(src_text):
        cls_name = cm.group(1)
        brace_pos = cm.end() - 1
        cls_block = _extract_brace_block(src_text, brace_pos)
        methods = []
        ctor_sig = None
        for mm in method_pat.finditer(cls_block):
            m_async = bool(mm.group(1))
            m_name = mm.group(2)
            if m_name in reserved_keywords:
                continue
            m_params = mm.group(3) or ""
            m_brace = cls_block.find("{", mm.end() - 1)
            m_body = _extract_brace_block(cls_block, m_brace) if m_brace != -1 else mm.group(0)
            msig = _build_ts_func_sig(m_async, m_params, m_body)
            if m_name == "constructor":
                ctor_sig = msig
                continue
            if not m_name.startswith("_") and not m_name.startswith("#"):
                methods.append(m_name)
            qname = f"{cls_name}.{m_name}"
            sigs[qname] = msig
            if m_name not in sigs:
                by_short_method.setdefault(m_name, []).append((qname, msig))
        methods_sorted = sorted(set(methods))
        cls_sig = {
            "kind": "class",
            "methods": methods_sorted,
            "posonlyArgs": [],
            "regularArgs": ctor_sig["regularArgs"] if ctor_sig else [],
            "reqArgs": ctor_sig["reqArgs"] if ctor_sig else [],
            "optArgs": ctor_sig["optArgs"] if ctor_sig else [],
            "allArgs": ctor_sig["allArgs"] if ctor_sig else [],
            "kwonlyReq": [],
            "kwonlyAll": [],
            "hasVararg": ctor_sig["hasVararg"] if ctor_sig else False,
            "hasKwarg": False,
            "normAstHash": _sha256_text(_strip_js_comments(cls_block)),
        }
        cls_sig["sigHash"] = _digest({
            "kind": "class",
            "methods": methods_sorted,
            "reqArgs": cls_sig["reqArgs"],
            "allArgs": cls_sig["allArgs"],
        })
        sigs[cls_name] = cls_sig

    for short_name, entries in by_short_method.items():
        if short_name in sigs:
            continue
        if len(entries) == 1:
            sigs[short_name] = entries[0][1]
        else:
            sub_sigs = {qn: sg for qn, sg in entries}
            sigs[short_name] = {
                "kind": "ambiguous_method",
                "qualified": sorted(sub_sigs.keys()),
                "subSignatures": sub_sigs,
                "sigHash": _digest({qn: sg.get("sigHash") for qn, sg in sorted(sub_sigs.items())}),
                "normAstHash": _digest({qn: sg.get("normAstHash") for qn, sg in sorted(sub_sigs.items())}),
            }

    # 4. Interfaces: [export] interface Name { field: type; optField?: type; }
    iface_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?interface[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)[^{;\n]*\{"
    )
    prop_pat = re.compile(r"(?m)^[ \t]*(?:readonly[ \t]+)?([A-Za-z_$][A-Za-z0-9_$]*)(\?)?[ \t]*:")
    for im in iface_pat.finditer(src_text):
        iface_name = im.group(1)
        brace_pos = im.end() - 1
        iface_block = _extract_brace_block(src_text, brace_pos)
        req_fields = []
        opt_fields = []
        for pm in prop_pat.finditer(iface_block):
            fname = pm.group(1)
            is_opt = bool(pm.group(2))
            if is_opt:
                opt_fields.append(fname)
            else:
                req_fields.append(fname)
        all_fields = req_fields + opt_fields
        isig = {
            "kind": "interface",
            "isAsync": False,
            "posonlyArgs": [],
            "regularArgs": all_fields,
            "reqArgs": req_fields,
            "optArgs": opt_fields,
            "allArgs": all_fields,
            "kwonlyReq": [],
            "kwonlyAll": [],
            "hasVararg": False,
            "hasKwarg": False,
            "normAstHash": _sha256_text(_strip_js_comments(iface_block)),
        }
        isig["sigHash"] = _digest({
            "kind": "interface",
            "reqArgs": req_fields,
            "allArgs": all_fields,
        })
        sigs[iface_name] = isig

    # 5. Type aliases: [export] type Name = ...
    type_pat = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?type[ \t]+([A-Za-z_$][A-Za-z0-9_$]*)[ \t]*(?:<[^=]+>)?[ \t]*=[ \t]*([^\n;]+)"
    )
    for tm in type_pat.finditer(src_text):
        tname = tm.group(1)
        if tname in sigs:
            continue
        rhs = tm.group(2).strip()
        tsig = {
            "kind": "type",
            "isAsync": False,
            "posonlyArgs": [],
            "regularArgs": [],
            "reqArgs": [],
            "optArgs": [],
            "allArgs": [],
            "kwonlyReq": [],
            "kwonlyAll": [],
            "hasVararg": False,
            "hasKwarg": False,
            "normAstHash": _sha256_text(rhs),
            "sigHash": _sha256_text(rhs),
        }
        sigs[tname] = tsig

    return syntax_ok, sigs


def _extract_package_json_signatures(src_text: str):
    """Extract npm/pnpm/yarn script contracts from package.json."""
    try:
        data = json.loads(src_text)
    except Exception:
        return False, {}
    if not isinstance(data, dict):
        return False, {}
    scripts = data.get("scripts")
    sigs = {}
    if isinstance(scripts, dict):
        for s_name, cmd in sorted(scripts.items()):
            cmd_str = str(cmd or "").strip()
            h = _sha256_text(cmd_str)
            entry = {
                "kind": "script",
                "command": cmd_str,
                "isAsync": False,
                "posonlyArgs": [],
                "regularArgs": [],
                "reqArgs": [],
                "optArgs": [],
                "allArgs": [],
                "kwonlyReq": [],
                "kwonlyAll": [],
                "hasVararg": False,
                "hasKwarg": False,
                "normAstHash": h,
                "sigHash": h,
            }
            sigs[s_name] = entry
            sigs[f"script:{s_name}"] = entry
    return True, sigs


def _extract_makefile_signatures(src_text: str) -> dict:
    """Extract Make target contracts from Makefile / *.mk."""
    sigs = {}
    target_pat = re.compile(r"(?m)^([A-Za-z0-9_.-]+)[ \t]*:(?!=)[ \t]*([^\n#]*)")
    for m in target_pat.finditer(src_text):
        tname = m.group(1).strip()
        if tname.startswith("."):
            continue
        prereqs = [x.strip() for x in (m.group(2) or "").split() if x.strip()]
        # Collect indented recipe lines immediately following
        rest_lines = src_text[m.end():].splitlines()
        recipe = []
        for ln in rest_lines:
            if ln.startswith("\t") or ln.startswith("  "):
                recipe.append(ln.strip())
            elif not ln.strip() or ln.strip().startswith("#"):
                continue
            else:
                break
        body_hash = _sha256_text("\n".join(prereqs + recipe))
        entry = {
            "kind": "make_target",
            "prereqs": prereqs,
            "isAsync": False,
            "posonlyArgs": [],
            "regularArgs": [],
            "reqArgs": [],
            "optArgs": [],
            "allArgs": [],
            "kwonlyReq": [],
            "kwonlyAll": [],
            "hasVararg": False,
            "hasKwarg": False,
            "normAstHash": body_hash,
            "sigHash": _digest({"kind": "make_target", "target": tname, "prereqs": prereqs}),
        }
        sigs[tname] = entry
        sigs[f"make:{tname}"] = entry
    return sigs


def extract_file_symbol_signatures(file_path):
    """Extract call-contract signatures for functions/classes/interfaces/scripts/targets in file_path."""
    p = Path(file_path)
    try:
        if p.is_symlink() or not p.is_file():
            return {
                "__file_exists__": False,
                "__syntax_ok__": False,
                "__resolution__": "missing",
                "__language__": "unknown",
                "symbols": {},
            }
        st = p.stat()
        if st.st_size > 2 * 1024 * 1024:
            return {
                "__file_exists__": True,
                "__syntax_ok__": True,
                "__resolution__": "oversized",
                "__language__": "unknown",
                "symbols": {},
            }
        key = (str(p), st.st_mtime_ns, st.st_size)
        cached = _FILE_SIGNATURE_CACHE.get(key)
        if cached is not None:
            return cached
        raw_bytes = p.read_bytes()
        src_text = raw_bytes.decode("utf-8", errors="ignore")
    except OSError:
        return {
            "__file_exists__": False,
            "__syntax_ok__": False,
            "__resolution__": "missing",
            "__language__": "unknown",
            "symbols": {},
        }

    sigs = {}
    syntax_ok = True
    resolution = "unsupported"
    language = "unsupported"
    suffix_lower = p.suffix.lower()
    name_lower = p.name.lower()

    if suffix_lower == ".py":
        language = "python"
        resolution = "exact"
        try:
            tree = ast.parse(src_text)
            scoped_nodes = _collect_scoped_ast_nodes(tree.body)
            top_level_names = {name for qname, name, is_nested, _ in scoped_nodes if not is_nested}
            by_short = {}
            for qname, name, is_nested, node in scoped_nodes:
                sig = _node_call_signature(node)
                if sig is None:
                    continue
                sigs[qname] = sig
                if is_nested and name not in top_level_names:
                    by_short.setdefault(name, []).append((qname, sig))
            for short_name, entries in by_short.items():
                if len(entries) == 1:
                    sigs[short_name] = entries[0][1]
                else:
                    sub_sigs = {qn: sg for qn, sg in entries}
                    sigs[short_name] = {
                        "kind": "ambiguous_method",
                        "qualified": sorted(sub_sigs.keys()),
                        "subSignatures": sub_sigs,
                        "sigHash": _digest({qn: sg.get("sigHash") for qn, sg in sorted(sub_sigs.items())}),
                        "normAstHash": _digest({qn: sg.get("normAstHash") for qn, sg in sorted(sub_sigs.items())}),
                    }
        except SyntaxError:
            syntax_ok = False
            resolution = "parse_error"
    elif suffix_lower in _TS_JS_EXTS:
        language = "typescript" if suffix_lower in (".ts", ".tsx") else "javascript"
        syntax_ok, sigs = _extract_ts_js_signatures(src_text)
        resolution = "contract" if syntax_ok else "parse_error"
    elif name_lower == "package.json":
        language = "package_json"
        ok_json, sigs = _extract_package_json_signatures(src_text)
        syntax_ok = ok_json
        resolution = "contract" if ok_json else "parse_error"
    elif name_lower in _MAKE_NAMES or suffix_lower == ".mk":
        language = "makefile"
        resolution = "contract"
        sigs = _extract_makefile_signatures(src_text)
    else:
        resolution = "unsupported"
        language = suffix_lower.lstrip(".") or "unknown"

    res = {
        "__file_exists__": True,
        "__syntax_ok__": syntax_ok,
        "__resolution__": resolution,
        "__language__": language,
        "symbols": sigs,
    }
    if len(_FILE_SIGNATURE_CACHE) > 512:
        _FILE_SIGNATURE_CACHE.clear()
    _FILE_SIGNATURE_CACHE[key] = res
    return res


def extract_file_symbol_hashes(file_path):
    """Extract per-symbol SHA256 hashes from a source file via AST (Python) or polyglot contract extractor."""
    p = Path(file_path)
    try:
        if p.is_symlink() or not p.is_file():
            return {}
        st = p.stat()
        if st.st_size > 2 * 1024 * 1024:
            return {}
        key = (str(p), st.st_mtime_ns, st.st_size)
        cached = _FILE_HASH_AST_CACHE.get(key)
        if cached is not None and cached[1] is not None:
            return cached[1]
        raw_bytes = p.read_bytes()
        sha = cached[0] if cached is not None else hashlib.sha256(raw_bytes).hexdigest()
        src_text = raw_bytes.decode("utf-8", errors="ignore")
    except OSError:
        return {}
    symbol_hashes = {}
    suffix_lower = p.suffix.lower()
    if suffix_lower == ".py":
        try:
            tree = ast.parse(src_text)
            lines = src_text.splitlines()
            scoped_nodes = _collect_scoped_ast_nodes(tree.body)
            top_level_names = {name for qname, name, is_nested, _ in scoped_nodes if not is_nested}
            by_short = {}
            for qname, name, is_nested, node in scoped_nodes:
                try:
                    norm_dump = ast.dump(node, annotate_fields=True, include_attributes=False)
                    hval = _sha256_text(norm_dump)
                except Exception:
                    start_ln = max(0, getattr(node, "lineno", 1) - 1)
                    end_ln = getattr(node, "end_lineno", start_ln + 1)
                    block = "\n".join(ln.strip() for ln in lines[start_ln:end_ln] if ln.strip() and not ln.strip().startswith("#"))
                    hval = _sha256_text(block)
                symbol_hashes[qname] = hval
                if is_nested and name not in top_level_names:
                    by_short.setdefault(name, []).append((qname, hval))
            for short_name, entries in by_short.items():
                if len(entries) == 1:
                    symbol_hashes[short_name] = entries[0][1]
                else:
                    symbol_hashes[short_name] = _digest({qn: hv for qn, hv in sorted(entries)})
            _FILE_HASH_AST_CACHE[key] = (sha, symbol_hashes)
            return symbol_hashes
        except SyntaxError:
            pass
    sig_info = extract_file_symbol_signatures(p)
    for sym_name, sig in (sig_info.get("symbols") or {}).items():
        symbol_hashes[sym_name] = sig.get("normAstHash") or sig.get("sigHash") or ""
    _FILE_HASH_AST_CACHE[key] = (sha, symbol_hashes)
    return symbol_hashes


def is_breaking_symbol_change(old_sig, curr_sig):
    """Determine if a symbol evolution breaks existing call contracts (returns (is_breaking, reason)).
    Covers positional, optional, keyword-only, posonly (/), vararg (*args/**kwargs), async, class method, and TS interface contracts.
    """
    if curr_sig is None:
        return True, "symbol_deleted"
    if not isinstance(old_sig, dict) or not isinstance(curr_sig, dict):
        return True, "missing_signature_baseline"
    if old_sig.get("kind") == "ambiguous_method" or curr_sig.get("kind") == "ambiguous_method":
        old_subs = old_sig.get("subSignatures") or {}
        curr_subs = curr_sig.get("subSignatures") or {}
        if not old_subs or not curr_subs:
            return True, "ambiguous_symbol_scope_changed"
        any_evolved = False
        for qn, old_s in sorted(old_subs.items()):
            if qn not in curr_subs:
                return True, f"qualified_symbol_deleted:{qn}"
            brk, rsn = is_breaking_symbol_change(old_s, curr_subs[qn])
            if brk:
                return True, f"{rsn}:{qn}"
            if rsn != "docstring_or_comment_only":
                any_evolved = True
        return False, ("compatible_evolution" if any_evolved else "docstring_or_comment_only")
    if old_sig.get("kind") and curr_sig.get("kind") and old_sig["kind"] != curr_sig["kind"]:
        return True, f"symbol_kind_changed:{old_sig['kind']}->{curr_sig['kind']}"
    if ("isAsync" in old_sig or "isAsync" in curr_sig) and bool(old_sig.get("isAsync")) != bool(curr_sig.get("isAsync")):
        return True, "async_contract_changed"

    if old_sig.get("kind") == "interface":
        old_req_set = set(old_sig.get("reqArgs") or [])
        curr_req_set = set(curr_sig.get("reqArgs") or [])
        old_all_set = set(old_sig.get("allArgs") or [])
        curr_all_set = set(curr_sig.get("allArgs") or [])
        added_req = sorted(curr_req_set - old_req_set)
        if added_req:
            return True, f"interface_required_field_added:{','.join(added_req)}"
        removed_fields = sorted(old_all_set - curr_all_set)
        if removed_fields:
            return True, f"interface_field_removed:{','.join(removed_fields)}"
        if old_sig.get("normAstHash") == curr_sig.get("normAstHash"):
            return False, "docstring_or_comment_only"
        return False, "compatible_evolution"

    old_req = list(old_sig.get("reqArgs") or [])
    curr_req = list(curr_sig.get("reqArgs") or [])
    old_all = list(old_sig.get("allArgs") if old_sig.get("allArgs") is not None else old_req)
    curr_all = list(curr_sig.get("allArgs") if curr_sig.get("allArgs") is not None else curr_req)

    # 1. New required positional parameters added -> breaking
    if len(curr_req) > len(old_req):
        added = [a for a in curr_req if a not in old_req] or curr_req[len(old_req):]
        return True, f"required_positional_added:{','.join(added)}"

    # 2. Current required positional parameters must match old positional prefix
    if curr_req != old_req[:len(curr_req)]:
        return True, f"required_positional_renamed:{','.join(old_req)}->{','.join(curr_req)}"

    # 3. Any previously accepted positional or optional parameter removed, renamed, or reordered -> breaking
    if len(curr_all) < len(old_all) or curr_all[:len(old_all)] != old_all:
        return True, f"parameter_removed_or_reordered:{','.join(old_all)}->{','.join(curr_all)}"

    # 4. Regular parameter converted to positional-only (`def f(x) -> def f(x, /)`) -> breaks keyword callers `f(x=1)`
    old_posonly = set(old_sig.get("posonlyArgs") or [])
    curr_posonly = set(curr_sig.get("posonlyArgs") or [])
    newly_posonly = (curr_posonly - old_posonly) & set(old_all)
    if newly_posonly:
        return True, f"parameter_became_posonly:{','.join(sorted(newly_posonly))}"

    # 5. Required keyword-only parameters added -> breaking
    old_kw_req = set(old_sig.get("kwonlyReq") or [])
    curr_kw_req = set(curr_sig.get("kwonlyReq") or [])
    if curr_kw_req - old_kw_req:
        return True, f"required_kwonly_added:{','.join(sorted(curr_kw_req - old_kw_req))}"

    # 6. Existing keyword-only parameters removed -> breaking
    old_kw_all = set(old_sig.get("kwonlyAll") or old_kw_req)
    curr_kw_all = set(curr_sig.get("kwonlyAll") or curr_kw_req)
    removed_kw = old_kw_all - curr_kw_all
    if removed_kw:
        return True, f"kwonly_parameter_removed:{','.join(sorted(removed_kw))}"

    # 7. *args or **kwargs removed -> breaking
    if (bool(old_sig.get("hasVararg")) and not bool(curr_sig.get("hasVararg"))) or (
        bool(old_sig.get("hasKwarg")) and not bool(curr_sig.get("hasKwarg"))
    ):
        return True, "vararg_or_kwarg_removed"

    # 8. For classes, public methods removed -> breaking
    if old_sig.get("kind") == "class":
        old_methods = set(old_sig.get("methods") or [])
        curr_methods = set(curr_sig.get("methods") or [])
        removed_methods = old_methods - curr_methods
        if removed_methods:
            return True, f"class_methods_removed:{','.join(sorted(removed_methods))}"

    # 9. Distinguish docstring/comment-only AST change vs executable body evolution
    old_norm = old_sig.get("normAstHash")
    curr_norm = curr_sig.get("normAstHash")
    if old_norm and curr_norm and old_norm == curr_norm and old_sig.get("sigHash") == curr_sig.get("sigHash"):
        return False, "docstring_or_comment_only"
    return False, "compatible_evolution"
