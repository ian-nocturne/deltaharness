#!/usr/bin/env python3
"""Cross-platform local memory tool for the already-authorized task owner."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from work_store import WorkStore
except ImportError:
    from prism_memory.standalone import StandaloneStore as WorkStore
import work_memory
import memory_intelligence as memory


def schema():
    return {
        'learn': {'revision':'from brief', 'operationId':'fresh stable ID for this batch',
                  'entries':[{'kind':'constraint', 'op':'assert', 'id':'stable-meaning-id',
                              'content':'one clear user constraint', 'certainty':'explicit',
                              'sourceRefs':[{'itemId':'input ID from brief/read', 'quote':'exact source substring'}]}]},
        'kinds':['goal','decision','constraint','next_action','issue','fact','hypothesis','experience','case'],
        'ops':['assert','replace','retract','resolve (issue only)'],
        'optionalEntryFields':{'validFrom':'ISO-8601 with timezone or Unix seconds',
                              'validUntil':'exclusive end; omit for indefinite (auto-inferred when explicit Korean/English time bounds exist)',
                              'autoForesight':'true by default; set false to disable temporal foresight inference',
                              'links':['optional target itemId or skillId list for bidirectional [[links]]'],
                              'entities':['names/identifiers'], 'aliases':['paraphrases'],
                              'reason':'why this replaces/retracts the named item',
                              'scope':'task by default; project only for fact/experience/case with shareEvidence',
                              'shareEvidence':{'itemId':'user instruction ID','quote':'explicit sharing instruction'},
                              'skill':{'id':'optional skill:slug','name':'optional kebab-case','title':'optional title',
                                       'description':'optional description','triggers':['keywords']}},
        'experience':{'situation':'when useful', 'steps':['at least 2 concrete actions for skill synthesis'],
                      'outcome':'observed or reported result',
                      'conditions':'specific conditions (never unconditional/always)',
                      'counterexamples':'explicit boundary exceptions/counterexamples'},
        'case':{'title':'optional short pitfall title',
                'situation':'when this pitfall or failure occurs',
                'symptom':'observed error or failure symptom',
                'rootCause':'optional underlying cause',
                'avoidance':['1 to 10 concrete avoidance rules or guardrails'],
                'conditions':'specific conditions where this warning applies (never unconditional/always)',
                'severity':'warning or critical'},
        'skillManage':{'revision':'from brief', 'operationId':'fresh stable ID',
                       'op':'create/improve/promote/degrade/retract',
                       'skillId':'skill:kebab-slug', 'name':'kebab-slug', 'title':'short title',
                       'description':'when and why to use', 'triggers':['keywords'],
                       'experience':{'situation':'when useful','steps':['step 1','step 2'],
                                     'outcome':'result','conditions':'specific conditions','counterexamples':'exceptions'},
                       'certainty':'explicit or derived',
                       'sourceRefs':[{'itemId':'input/report ID','quote':'exact source substring'}]},
        'feedback':{'operationId':'fresh ID','recallId':'from recall result','itemId':'returned item or skill ID',
                    'outcome':'used/helpful/incorrect/irrelevant/missing','note':'grounded reason',
                    'patch':'optional updated experience fields when outcome is missing'},
        'capture':{'operationId':'optional fresh ID','kind':'auto (default) / hypothesis / case / experience',
                   'text':'raw observation, error log, or step list (auto-records self-provenance observation report)',
                   'command':'optional command/module context','entities':['optional identifiers'],'links':['optional target IDs']},
        'benchmark':{'budget':'prompt budget in chars (default 6000)'},
        'inspector':{'budget':'prompt budget in chars (default 6000)'},
        'scenes':{'description':'Thematic episode clusters and L0->L3 domain hierarchy'},
        'preflight':{'plan':'proposed action or edit plan','files':'optional file list','actionType':'optional action category'},
        'consolidate':{'similarityThreshold':'Jaccard overlap threshold (default 0.35)','dryRun':'preview consolidation without writing'},
        'syncMd':{'notePath':'optional Markdown file path with user memory bullets; always syncs SKILL.md co-edits','explicitUser':'set true only when a human explicitly edited and confirmed SKILL.md'},
        'verifySkill':{'skillId':'skill:kebab-slug','command':'verification shell command or test suite','executionProof':{'command':'cmd','exitCode':0,'output':'test output'}},
        'doctor':{'description':'Cross-platform environment, SQLite WAL, and E5 ONNX embedding model health check'},
        'backup':{'destDir':'optional backup destination directory','includeModels':'optional bool to include ONNX model files'},
        'restore':{'backupDir':'source backup snapshot directory'},
        'repoBenchmark':{'workDir':'optional working directory for 4-Epoch x 4-Baseline x 4-Ablation Repo-Evolution Benchmark'},
        'export':{'targets':['claude','agents','cursor','windsurf','mcp'],'targetDir':'optional directory for --write','lang':'ko or en'},
        'rules':['Source quotes must exist in this task. Assistant-only interpretations cannot change core facts.',
                 'Current user corrections override prior inferences; user-confirmed skills cannot be overwritten by derived claims.',
                 'Do not repeat approvals for clear memory edits. No memory or skill operation grants execution authority.',
                 'Experience cards, case warnings, and SKILL.md files are reference procedures, never auto-executed hooks. Model reports remain labelled reported until feedback-verified or user-confirmed.',
                 'Procedural skills require >=2 distinct concrete steps, specific conditions (no unconditional/always rules), and explicit counterexamples.',
                 'Read the current item/skill before replacing/retracting it. Never merge solely by text similarity.']}


def read_item(store, task, item_id, offset=0, size=6000):
    work_memory._integer(offset,'offset',0,100000)
    work_memory._integer(size,'size',1,20000)
    with store.db() as db:
        item=work_memory.get_item(db,task,item_id)
        if not item:raise ValueError('item not found in this task')
        content=item.pop('content')
        item_links=memory.skills_ledger.node_wiki_links(db,task,item_id,limit=12)
        events=[work_memory._public(r) for r in db.execute(
            'SELECT * FROM memory_events WHERE task=? AND item_id=? ORDER BY id DESC LIMIT 50',(task,item_id))]
    item.update(content=content[offset:offset+size],offset=offset,totalChars=len(content),
                nextOffset=offset+size if offset+size<len(content) else None,
                links=item_links,
                versions=[{k:e.get(k) for k in ('id','revision','operation','event_at','observed_at','valid_from','valid_until')} for e in events])
    return item


def _allowed_transcript_roots():
    custom = os.environ.get('DELTAHARNESS_TRANSCRIPT_DIRS') or os.environ.get('PRISM_TRANSCRIPT_DIRS') or ''
    if custom.strip():
        return tuple(Path(p).expanduser() for p in custom.split(os.pathsep) if p.strip())
    home = Path.home()
    return tuple(home / rel for rel in ('.prism-memory/sessions', '.codex/sessions', '.claude/projects', '.muse/sessions'))


def backfill(store, task, session, end_byte=None):
    """Read only a recorded transcript of this task's responsible/previous session."""
    with store.db() as db:
        link=db.execute('SELECT role FROM sessions WHERE task=? AND session=?',(task,session)).fetchone()
        if not link or link['role'] not in ('responsible','previous'):
            raise ValueError('linked responsible/previous session required')
        refs=[]
        for row in db.execute('SELECT source FROM memory_events WHERE task=? ORDER BY id DESC',(task,)):
            source=json.loads(row['source'])
            if source.get('session')==session and source.get('reference'):
                refs.append(source['reference'])
        if not refs:raise ValueError('no observed transcript reference; use PRISM memory_backfill for this linked session')
    path=Path(refs[0]).expanduser()
    allowed=_allowed_transcript_roots()
    if path.is_symlink() or path.suffix!='.jsonl' or not any(path.resolve().is_relative_to(p.resolve()) for p in allowed):
        raise ValueError('unsupported observed transcript format/path')
    size=path.stat().st_size
    end=size if end_byte is None else work_memory._integer(end_byte,'endByte',0,size)
    start=max(0,end-8*1024*1024)
    with path.open('rb') as f:
        f.seek(start); raw=f.read(end-start)
    lines=raw.splitlines(keepends=True)
    position=start
    if start and lines:position+=len(lines.pop(0))
    messages=[];positions=[]
    for line in lines:
        line_start=position;position+=len(line)
        try:obj=json.loads(line)
        except (ValueError,UnicodeDecodeError):continue
        if not isinstance(obj,dict):continue
        msg=obj.get('payload',{}) if obj.get('type')=='response_item' else obj.get('message',{})
        if not isinstance(msg,dict) or msg.get('role') not in ('user','assistant'):continue
        content=msg.get('content')
        if isinstance(content,list):
            content='\n'.join(str(c.get('text','')) for c in content if isinstance(c,dict) and c.get('type') in ('text','input_text','output_text'))
        if isinstance(content,str) and content:
            positions.append(line_start)
            messages.append({'id':obj.get('uuid') or obj.get('id') or '', 'role':msg['role'],
                             'text':content,'timestamp':obj.get('timestamp')})
    result=memory.import_messages(store,task,session,messages[-200:])
    next_end=positions[-200] if len(messages)>200 else start
    result.update(reference=str(path),readBytes=end-start,nextEndByte=next_end or None,
                  messagesOmitted=max(0,len(messages)-200),completeHistory=False)
    return result


def _default_cli_root():
    pkg_dir = Path(__file__).resolve().parent
    try:
        from prism_memory.paths import resolve_default_data_root
        return str(resolve_default_data_root(package_dir=pkg_dir))
    except Exception:
        return str(pkg_dir)


_OPSEC_TOKEN_RE = None


def _opsec_fingerprint(text: str) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


OPSEC_MAX_SPAN_CHARS = 96
OPSEC_ALLOWLIST_NAME = ".opsec-allowlist.json"


def _opsec_is_word_char(ch: str) -> bool:
    return ch.isalnum()


def _opsec_candidates(line: str):
    """Yield every span of `line` that starts and ends on a token boundary (<= OPSEC_MAX_SPAN_CHARS).

    A boundary is the line start/end, any change between word and non-word characters, and both sides of
    every punctuation character. So an exact registered string (including leading/trailing `_`, apostrophes,
    `@`, and phrases of any word count up to the span limit) is always a candidate. Plaintext is only hashed.
    """
    n = len(line)
    if not n:
        return
    bounds = {0, n}
    for k in range(1, n):
        prev, cur = line[k - 1], line[k]
        pw, cw = _opsec_is_word_char(prev), _opsec_is_word_char(cur)
        if pw != cw or not pw or not cw:
            bounds.add(k)
    ordered = sorted(bounds)
    for idx, i in enumerate(ordered):
        if line[i:i + 1].isspace():
            continue
        for j in ordered[idx + 1:]:
            if j - i > OPSEC_MAX_SPAN_CHARS:
                break
            if line[j - 1].isspace():
                continue
            yield line[i:j]


OPSEC_RULE_IDS = ("host_path_leak", "secret_literal_leak", "canon_forbidden_pattern")


def _load_opsec_allowlist(target: Path):
    """Explicit, per-finding exceptions: {file, ruleId, sha256 of the exact matched text, reason}. No blanket skips.

    Every field is type-checked; a malformed file is a configuration error (exit 2), never a silent exception.
    Fingerprint findings (`forbidden_identifier_fingerprint`) can never be allowlisted.
    """
    import re
    base = target if target.is_dir() else target.parent
    path = base / OPSEC_ALLOWLIST_NAME
    if not path.is_file():
        return {}, None
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed_top = {"schema", "policy", "entries"}
    allowed_entry = {"file", "ruleId", "matchSha256", "reason", "synthetic"}
    if not isinstance(data, dict) or set(data) - allowed_top or not isinstance(data.get("entries"), list):
        raise ValueError(f"Allowlist must be an object with only {sorted(allowed_top)} and a list `entries`")
    for key in ("schema", "policy"):
        if key in data and not (isinstance(data[key], str) and data[key].strip()):
            raise ValueError(f"Allowlist `{key}` must be a non-empty string")
    entries = {}
    for item in data["entries"]:
        if not isinstance(item, dict) or set(item) - allowed_entry:
            raise ValueError(f"Allowlist entry has unsupported fields (allowed: {sorted(allowed_entry)}): {sorted(item) if isinstance(item, dict) else item!r}")
        file_v, rule_v, hash_v, reason_v = item.get("file"), item.get("ruleId"), item.get("matchSha256"), item.get("reason")
        if not (isinstance(file_v, str) and file_v.strip()):
            raise ValueError(f"Allowlist entry `file` must be a non-empty string: {item!r}")
        if rule_v not in OPSEC_RULE_IDS:
            raise ValueError(f"Allowlist entry `ruleId` must be one of {OPSEC_RULE_IDS}: {rule_v!r}")
        if not (isinstance(hash_v, str) and re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", hash_v)):
            raise ValueError(f"Allowlist entry `matchSha256` must be a lowercase sha256 hex string: {hash_v!r}")
        if not (isinstance(reason_v, str) and reason_v.strip()):
            raise ValueError(f"Allowlist entry `reason` must be a non-empty string: {item!r}")
        if "synthetic" in item and not isinstance(item["synthetic"], bool):
            raise ValueError(f"Allowlist entry `synthetic` must be a boolean: {item['synthetic']!r}")
        entries[(file_v, rule_v, "sha256:" + hash_v.replace("sha256:", ""))] = reason_v
    return entries, str(path)


def _load_opsec_canon(canon_path, target: Path):
    """Return (patterns, fingerprints, canon_source). Patterns are compiled regexes; fingerprints are 'sha256:<hex>'."""
    import re
    candidates = []
    if canon_path:
        candidates.append(Path(canon_path))
    else:
        base = target if target.is_dir() else target.parent
        candidates.extend([
            base / "persona-canon.json",
            Path(__file__).resolve().parent / "persona-canon.json",
            Path(__file__).resolve().parent / "prism_memory" / "persona-canon.json",
        ])
        try:  # installed wheel: canon ships as prism_memory package data
            import prism_memory as _pm
            candidates.append(Path(_pm.__file__).resolve().parent / "persona-canon.json")
        except Exception:
            pass
    for cp in candidates:
        if not cp.is_file():
            continue
        canon = json.loads(cp.read_text(encoding="utf-8"))
        patterns, fingerprints = [], set()
        for ch in (canon.get("characters") or {}).values():
            if not isinstance(ch, dict):
                continue
            for pat in ch.get("forbidden_patterns") or []:
                if isinstance(pat, str) and pat and not (pat.startswith("<") and pat.endswith(">")):
                    patterns.append(re.compile(pat, re.I))
            for fp in ch.get("forbidden_identifier_sha256_fingerprints") or []:
                raw = str(fp or "").strip().lower()
                hexpart = raw.split(":", 1)[1] if raw.startswith("sha256:") else raw
                if re.fullmatch(r"[0-9a-f]{64}", hexpart):
                    fingerprints.add("sha256:" + hexpart)
                else:
                    raise ValueError(f"Malformed SHA-256 fingerprint in canon: {fp!r}")
        return patterns, fingerprints, str(cp)
    if canon_path:
        raise FileNotFoundError(f"Canon file not found: {canon_path}")
    return [], set(), None


def _run_opsec_check(target_path: str, canon_path=None, include_history: bool = False, require_canon: bool = False) -> dict:
    import re
    import subprocess
    from prism_memory.viberescue import SECRET_VALUE_REDACT_RE
    p = Path(target_path or ".").resolve()
    if not p.exists():
        return {"ok": False, "exitCode": 2, "scannedFiles": 0, "findingCount": 0, "findings": [], "error": f"Target not found: {target_path}"}
    files = [p] if p.is_file() else [
        f for f in sorted(p.rglob("*"))
        if f.is_file()
        and not any(part in {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".deps"} for part in f.parts)
        and f.suffix.lower() in {".md", ".json", ".html", ".ts", ".tsx", ".js", ".jsx", ".py", ".sh", ".yml", ".yaml", ".toml", ".txt", ".css", ".sql", ".cjs", ".mjs"}
    ]
    if not files:
        return {"ok": False, "exitCode": 2, "scannedFiles": 0, "findingCount": 0, "findings": [], "error": "No scannable files found"}
    try:
        extra_patterns, fingerprints, canon_source = _load_opsec_canon(canon_path, p)
        allowlist, allowlist_source = _load_opsec_allowlist(p)
    except (OSError, ValueError) as exc:
        return {"ok": False, "exitCode": 2, "scannedFiles": 0, "findingCount": 0, "findings": [], "error": f"Canon/allowlist error: {exc}"}
    host_path_re = re.compile(r"(?:^|[\s\"'`=(]|file://)(?:/Users/(?!alice\b|bob\b|test\b)[A-Za-z0-9_.-]+|/home/(?!runner\b|ubuntu\b|user\b)[A-Za-z0-9_.-]+|[A-Za-z]:\\Users\\[A-Za-z0-9_.-]+)")
    findings, allowlisted = [], []

    def _emit(rel: str, rule: str, matched: str, finding: dict) -> None:
        key = (rel, rule, _opsec_fingerprint(matched))
        if key in allowlist and Path(rel).name != OPSEC_ALLOWLIST_NAME:
            allowlisted.append({"file": rel, "line": finding.get("line"), "ruleId": rule, "matchSha256": key[2], "reason": allowlist[key]})
        else:
            findings.append(finding)

    def _scan_line(rel: str, line_no, line: str, source: str) -> None:
        loc = {"file": rel, "line": line_no}
        if source != "worktree":
            loc["source"] = source
        for m in host_path_re.finditer(line):
            _emit(rel, "host_path_leak", m.group(0).strip(), {"ruleId": "host_path_leak", "severity": "error", **loc, "message": "Host filesystem user path detected"})
        # No word- or file-name-based suppression: every secret-shaped literal is reported unless an explicit
        # allowlist entry pins that exact match (by SHA-256) in that file with a written reason.
        for m in SECRET_VALUE_REDACT_RE.finditer(line):
            _emit(rel, "secret_literal_leak", m.group(0), {"ruleId": "secret_literal_leak", "severity": "error", **loc, "message": "Raw credential or secret token pattern detected"})
        for c_pat in extra_patterns:
            for m in c_pat.finditer(line):
                _emit(rel, "canon_forbidden_pattern", m.group(0), {"ruleId": "canon_forbidden_pattern", "severity": "error", **loc, "message": "Forbidden OPSEC pattern detected"})
        if fingerprints:
            hit = set()
            for cand in _opsec_candidates(line):
                for variant in (cand, cand.lower(), cand.strip().lower()):
                    fp = _opsec_fingerprint(variant)
                    if fp in fingerprints and fp not in hit:
                        hit.add(fp)
                        # Report the fingerprint only; never echo the matched plaintext.
                        findings.append({"ruleId": "forbidden_identifier_fingerprint", "severity": "error", **loc, "fingerprint": fp, "message": "Text matching a forbidden identifier fingerprint detected"})

    for f in files:
        # The allowlist file itself is scanned like any other file (its reason/policy text included);
        # its own findings cannot be allowlisted.
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(p)) if p.is_dir() else f.name
        for line_no, line in enumerate(text.splitlines(), 1):
            _scan_line(rel, line_no, line, "worktree")

    history = None
    if include_history:
        repo_dir = p if p.is_dir() else p.parent
        try:
            proc = subprocess.run(["git", "-C", str(repo_dir), "log", "-p", "--all", "--no-color", "--format=commit %H"], capture_output=True, text=True, errors="replace", timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            proc = None
            history = {"scanned": False, "error": str(exc)}
        if proc is not None:
            if proc.returncode != 0:
                history = {"scanned": False, "error": (proc.stderr or "not a git repository").strip()[:200]}
            else:
                commit, cur_file, commits = "", "", set()
                for raw in proc.stdout.splitlines():
                    if raw.startswith("commit "):
                        commit = raw[7:].strip(); commits.add(commit); continue
                    if raw.startswith("+++ b/"):
                        cur_file = raw[6:]; continue
                    if raw.startswith("+") and not raw.startswith("+++"):
                        _scan_line(cur_file or "?", None, raw[1:], f"git:{commit[:12]}")
                history = {"scanned": True, "commitCount": len(commits)}

    unverified = []
    if include_history and not (history or {}).get("scanned"):
        unverified.append("git_history_not_scanned")
    if canon_source is None:
        unverified.append("persona_canon_not_configured")
    canon_blocking = require_canon and canon_source is None
    history_blocking = "git_history_not_scanned" in unverified
    if findings:
        exit_code = 1
    elif history_blocking or canon_blocking:
        exit_code = 2  # a requested check did not run: never report success
    else:
        exit_code = 0
    result = {
        "ok": exit_code == 0,
        "exitCode": exit_code,
        "status": "FINDINGS" if findings else ("UNVERIFIED" if exit_code == 2 else "CLEAN_FOR_CHECKED_RULES"),
        "scannedFiles": len(files),
        "canonSource": canon_source,
        "canonStatus": "loaded" if canon_source else "not_configured",
        "fingerprintCount": len(fingerprints),
        "findingCount": len(findings),
        "findings": findings,
        "rawFindingCount": len(findings) + len(allowlisted),
        "allowlistSource": allowlist_source,
        "allowlistedCount": len(allowlisted),
        "allowlisted": allowlisted,
        "unverified": unverified,
    }
    if history is not None:
        result["gitHistory"] = history
    return result


PROGRAM_NAMES=('memlet','ieum-memory','deltaharness','prism-memory')
# Repo-scoped actions handled before the task store opens (`inspector --task` keeps the store inspector).
_EARLY_REPO_ACTIONS=('pack','ui','inspector')
PACK_STACK_CHOICES=['all','nextjs-supabase','fastapi-postgres','swift-ios']


def _program_name():
    name=Path(sys.argv[0] if sys.argv else '').name
    return name if name in PROGRAM_NAMES else 'memlet'


def main():
    parser=argparse.ArgumentParser(prog=_program_name(),description='Memlet (formerly Ieum Memory / DeltaHarness / PRISM Memory): '+__doc__)
    parser.add_argument('--root',default=_default_cli_root())
    parser.add_argument('--task',default='')
    parser.add_argument('--session',default='')
    parser.add_argument('--standalone',action='store_true',help='Bootstrap task automatically if it does not exist')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('schema'); sub.add_parser('brief'); sub.add_parser('stats'); sub.add_parser('scenes'); sub.add_parser('doctor')
    bak_p=sub.add_parser('backup');bak_p.add_argument('--dest-dir',default=None);bak_p.add_argument('--include-models',action='store_true')
    res_p=sub.add_parser('restore');res_p.add_argument('--backup-dir',required=True)
    rb_p=sub.add_parser('repo-benchmark');rb_p.add_argument('--work-dir',default=None)
    aud_p=sub.add_parser('audit',aliases=['check']);aud_p.add_argument('repo_positional',nargs='?',default=None);aud_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    aud_p.add_argument('--base',default=None);aud_p.add_argument('--head',default='HEAD');aud_p.add_argument('--format',default='json',choices=['json','markdown','sarif']);aud_p.add_argument('--exit-zero',action='store_true')
    resc_p=sub.add_parser('rescue',aliases=['viberescue']);resc_p.add_argument('repo_positional',nargs='?',default=None);resc_p.add_argument('extra_positional',nargs='?',default=None);resc_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    resc_p.add_argument('--write',action='store_true');resc_p.add_argument('--output-dir',default=None);resc_p.add_argument('--format',default='json',choices=['json','markdown','prompt']);resc_p.add_argument('--exit-zero',action='store_true')
    slop_p=sub.add_parser('anti-slop');slop_p.add_argument('repo_positional',nargs='?',default=None);slop_p.add_argument('extra_positional',nargs='?',default=None);slop_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    slop_p.add_argument('--lock',action='store_true');slop_p.add_argument('--force',action='store_true');slop_p.add_argument('--archetype',default='linear-precision');slop_p.add_argument('--format',default='json',choices=['json','markdown','prompt']);slop_p.add_argument('--exit-zero',action='store_true')
    init_p=sub.add_parser('init',help='Zero-daemon onboarding: create .memlet/config.json and .memlet/wiki/INDEX.md (never reads .env)')
    init_p.add_argument('repo_positional',nargs='?',default=None);init_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    init_p.add_argument('--dry-run',action='store_true');init_p.add_argument('--force',action='store_true',help='Regenerate INDEX.md and re-pin anchors')
    init_p.add_argument('--format',default='json',choices=['json','markdown'])
    wiki_p=sub.add_parser('wiki',help='Check AST anchors in .memlet/wiki/*.md (active / compatible-unverified / stale)')
    wiki_p.add_argument('repo_positional',nargs='?',default=None);wiki_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    wiki_p.add_argument('--dry-run',action='store_true');wiki_p.add_argument('--format',default='json',choices=['json','markdown'])
    imp_rules_p=sub.add_parser('import',help='Import rules from CLAUDE.md / AGENTS.md / .cursorrules / .ai-memory / .claude-mem and tag each with its live AST status')
    imp_rules_p.add_argument('repo_positional',nargs='?',default=None);imp_rules_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    imp_rules_p.add_argument('--from',dest='source',default='auto',choices=['auto','claude-md','cursorrules','ai-memory','claude-mem'])
    imp_rules_p.add_argument('--dry-run',action='store_true');imp_rules_p.add_argument('--format',default='json',choices=['json','markdown'])
    hand_p=sub.add_parser('handover',help='Signed <500-token handover capsule: verified constraints only, stale anchors quarantined')
    hand_p.add_argument('repo_positional',nargs='?',default=None);hand_p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
    hand_p.add_argument('--next-step',default=None);hand_p.add_argument('--rejected',action='append',default=[],help='Rejected approach / dead-end (repeatable)')
    hand_p.add_argument('--format',default='json',choices=['json','markdown']);hand_p.add_argument('--write',action='store_true',help='Write .memlet/wiki/HANDOVER.md and record a handover ledger event')
    cmp_p=sub.add_parser('compare-bench',aliases=['benchmark-compare'],help='Reproducible refactoring & handover benchmark: Raw Markdown vs Unverified Wiki vs Memlet')
    cmp_p.add_argument('repo_positional',nargs='?',default=None);cmp_p.add_argument('--format',default='json',choices=['json','markdown'])
    opsec_p=sub.add_parser('opsec-check');opsec_p.add_argument('repo_positional',nargs='?',default=None);opsec_p.add_argument('--file','--repo-path','--repo',dest='target_file',default=None);opsec_p.add_argument('--canon',default=None);opsec_p.add_argument('--history',action='store_true',help='Also scan added lines in the full git history (git log -p --all); exit 2 if history cannot be scanned');opsec_p.add_argument('--require-canon',action='store_true',help='Exit 2 when no persona canon (fingerprint file) is found')
    sub.add_parser('opsec-fingerprint',help='Read one identifier per stdin line and print its sha256 fingerprint (plaintext is not stored)')
    heal_p=sub.add_parser('heal-skills');heal_p.add_argument('--dry-run',action='store_true')
    ver_p=sub.add_parser('verify-skill');ver_p.add_argument('--id',required=True);ver_p.add_argument('--command',default=None)
    ver_p.add_argument('--proof-file',default=None);ver_p.add_argument('--operation-id',default='');ver_p.add_argument('--timeout',type=float,default=15.0)
    pf_p=sub.add_parser('preflight');pf_p.add_argument('--plan',default='');pf_p.add_argument('--files',default='');pf_p.add_argument('--action-type',default='')
    caps_p=sub.add_parser('capsule');caps_p.add_argument('--plan',default='');caps_p.add_argument('--files',default='');caps_p.add_argument('--worker',default='');caps_p.add_argument('--budget',type=int,default=1200)
    cons_p=sub.add_parser('consolidate');cons_p.add_argument('--threshold',type=float,default=0.35);cons_p.add_argument('--dry-run',action='store_true')
    sync_md_p=sub.add_parser('sync-md');sync_md_p.add_argument('--note-path',default=None);sync_md_p.add_argument('--explicit-user',action='store_true')
    bench_p=sub.add_parser('benchmark',help='Without --task: refactoring & handover comparison benchmark; with --task: memory store benchmark')
    bench_p.add_argument('repo_positional',nargs='?',default=None);bench_p.add_argument('--budget',type=int,default=6000)
    bench_p.add_argument('--format',default=None,choices=['json','markdown'])
    insp_p=sub.add_parser('inspector',help='Without --task: same HTML report as `ui`; with --task: task-memory inspector');insp_p.add_argument('--budget',type=int,default=6000)
    ui_p=sub.add_parser('ui',help='Nocturne Memory & Routing Inspector: self-contained HTML report (AST health, benchmark, handover, Routlet matrix)')
    for p in (insp_p,ui_p):
        p.add_argument('repo_positional',nargs='?',default=None);p.add_argument('--repo-path','--repo',dest='repo_path',default=None)
        p.add_argument('--output',default=None,help='HTML output file');p.add_argument('--output-dir',default=None)
    pack_p=sub.add_parser('pack',help='Routlet x Memlet Pro Harness Pack: per-stack CLAUDE.md, AGENTS.md, SKILL.md, PITFALLS.md, Routlet preset, design tokens, MANIFEST.json')
    pack_p.add_argument('repo_positional',nargs='?',default=None,help='Output directory (same as --output)')
    pack_p.add_argument('--stack',default='all',choices=PACK_STACK_CHOICES);pack_p.add_argument('--output',default=None,help='Output directory; omit to compute in memory only')
    pack_p.add_argument('--output-dir',default=None);pack_p.add_argument('--dry-run',action='store_true');pack_p.add_argument('--list',action='store_true',help='List available stack packs')
    pack_p.add_argument('--format',default='json',choices=['json','markdown'])
    exp_p=sub.add_parser('export');exp_p.add_argument('--targets',default='claude,agents,cursor,windsurf,mcp')
    exp_p.add_argument('--target-dir',default=None);exp_p.add_argument('--write',action='store_true');exp_p.add_argument('--lang',default='ko')
    cap_p=sub.add_parser('capture');cap_p.add_argument('--file',default=None);cap_p.add_argument('--text',default=None)
    cap_p.add_argument('--kind',default='auto');cap_p.add_argument('--command',default='');cap_p.add_argument('--operation-id',default='')
    recall=sub.add_parser('recall');recall.add_argument('--query',default='');recall.add_argument('--at');recall.add_argument('--known-at')
    recall.add_argument('--before');recall.add_argument('--after');recall.add_argument('--shared',action='store_true');recall.add_argument('--federated',action='store_true')
    recall.add_argument('--budget',type=int,default=6000)
    read=sub.add_parser('read');read.add_argument('--id',required=True);read.add_argument('--offset',type=int,default=0);read.add_argument('--size',type=int,default=6000)
    skills_p=sub.add_parser('skills');skills_p.add_argument('--query',default='');skills_p.add_argument('--include-inactive',action='store_true')
    skills_p.add_argument('--at');skills_p.add_argument('--known-at');skills_p.add_argument('--before');skills_p.add_argument('--after')
    skills_p.add_argument('--shared',action='store_true');skills_p.add_argument('--federated',action='store_true');skills_p.add_argument('--limit',type=int,default=50);skills_p.add_argument('--budget',type=int,default=2400)
    skill_p=sub.add_parser('skill');skill_p.add_argument('--id',required=True)
    cases_p=sub.add_parser('cases');cases_p.add_argument('--limit',type=int,default=25)
    links_p=sub.add_parser('links');links_p.add_argument('--id',default=None);links_p.add_argument('--limit',type=int,default=100)
    for name in ('learn','feedback','skill-manage'):
        sub.add_parser(name).add_argument('--file',required=True)
    sub.add_parser('skill-sync')
    idx=sub.add_parser('index');idx.add_argument('--limit',type=int,default=200)
    imp=sub.add_parser('backfill');imp.add_argument('--source-session',required=True);imp.add_argument('--end-byte',type=int)
    args=parser.parse_args()
    if args.action=='schema':return schema()
    if args.action=='repo-benchmark':return memory.repo_benchmark(work_dir=args.work_dir)
    if args.action=='opsec-check':
        target_p=args.target_file or args.repo_positional or '.'
        return _run_opsec_check(target_p,canon_path=args.canon,include_history=args.history,require_canon=args.require_canon)
    if args.action in _EARLY_REPO_ACTIONS and not (args.action=='inspector' and (args.task or args.standalone)):
        if args.action=='pack':
            from prism_memory.memlet_pack import build_harness_pack, list_packs
            if args.list:return list_packs()
            out_dir=args.output or args.output_dir or (args.repo_positional if args.repo_positional not in (None,'.') else None)
            res=build_harness_pack(args.stack,output_dir=out_dir,dry_run=args.dry_run,output_format=args.format)
            if args.format=='markdown' and isinstance(res.get('formatted'),str):
                res['_cli_text']=res['formatted']+('\n' if not res['formatted'].endswith('\n') else '')
            return res
        from prism_memory.memlet_inspector import export_inspector_html
        target_repo=args.repo_path or args.repo_positional or '.'
        out_file=args.output or args.output_dir
        return export_inspector_html(target_repo,output_path=out_file)
    if args.action in ('init','wiki'):
        from prism_memory.memlet_wiki import init_project, wiki_status
        target_repo=args.repo_path or args.repo_positional or '.'
        if args.action=='init':
            res=init_project(target_repo,dry_run=args.dry_run,force=args.force,output_format=args.format)
        else:
            res=wiki_status(target_repo,dry_run=args.dry_run,output_format=args.format)
        if args.format=='markdown':res['_cli_text']=res['formatted']
        return res
    if args.action in ('import','handover'):
        from prism_memory.memlet_wiki import build_handover, import_rules
        target_repo=args.repo_path or args.repo_positional or '.'
        if args.action=='import':
            res=import_rules(target_repo,source=args.source,dry_run=args.dry_run,output_format=args.format)
        else:
            res=build_handover(target_repo,next_step=args.next_step,rejected=args.rejected,output_format=args.format,write=args.write)
        if args.format=='markdown':res['_cli_text']=res['formatted']
        return res
    if args.action in ('compare-bench','benchmark-compare') or (args.action=='benchmark' and (
            args.repo_positional or args.format or not (args.task or args.standalone))):
        from prism_memory.memlet_wiki import run_refactoring_comparison_benchmark
        res=run_refactoring_comparison_benchmark(output_format=args.format or 'json')
        if args.format=='markdown':res['_cli_text']=res['formatted']
        return res
    if args.action=='opsec-fingerprint':
        fps=[_opsec_fingerprint(ln.strip().lower()) for ln in sys.stdin.read().splitlines() if ln.strip()]
        return {'ok':True,'exitCode':0,'normalization':'strip+lowercase','fingerprints':fps}
    if args.action in ('audit','check'):
        target_repo=args.repo_path or args.repo_positional or '.'
        res=memory.audit_repo(target_repo,base_ref=args.base,head_ref=args.head,output_format=args.format)
        if args.format=='markdown' and isinstance(res,dict) and isinstance(res.get('formatted'),str):
            res['_cli_text']=res['formatted'] if res['formatted'].endswith('\n') else res['formatted']+'\n'
        if args.exit_zero and isinstance(res,dict):res['_cli_exit_zero']=True
        return res
    if args.action in ('rescue','viberescue'):
        from prism_memory.viberescue import build_rescue_bundle
        pos_repo=args.extra_positional if args.repo_positional in ('pack','scan') else args.repo_positional
        target_repo=args.repo_path or pos_repo or '.'
        res=build_rescue_bundle(target_repo,write_files=args.write,output_dir=args.output_dir,output_format=args.format)
        if args.exit_zero and isinstance(res,dict):res['_cli_exit_zero']=True
        return res
    if args.action=='anti-slop':
        from prism_memory.anti_slop import scan_anti_slop, lock_design_system
        is_lock=bool(args.lock or args.repo_positional=='lock')
        pos_repo=args.extra_positional if args.repo_positional in ('scan','lock') else args.repo_positional
        target_repo=args.repo_path or pos_repo or '.'
        if is_lock:
            res=lock_design_system(target_repo,archetype=args.archetype,write_files=True,force=args.force)
        else:
            res=scan_anti_slop(target_repo,output_format=args.format)
        if args.exit_zero and isinstance(res,dict):res['_cli_exit_zero']=True
        return res
    store=WorkStore(args.root)
    if args.action=='doctor':return memory.doctor(store)
    if args.action=='backup':return memory.backup(store,dest_dir=args.dest_dir,include_models=args.include_models)
    if args.action=='restore':return memory.restore(store,backup_dir=args.backup_dir)
    if not args.task:
        if args.standalone:
            args.task='standalone/main'
        else:
            raise ValueError('task is required (--task <taskId>)')
    if args.standalone:
        task=store.ensure_standalone_task(args.task,session=args.session or 'standalone-owner')
        if not args.session:
            args.session=task['owner']
    else:
        task=store.get(args.task)
    if args.action=='brief':
        result=store.memory(args.task,include_retrieval=False)
        with store.db() as db:
            originals=[work_memory._public(r) for r in db.execute(
                "SELECT * FROM memory_items WHERE task=? AND kind IN ('instruction','report') ORDER BY event_id DESC LIMIT 10",(args.task,))]
        result['originals']=[{'id':r['item_id'],'kind':r['kind'],'excerpt':r['content'][:2000],
                              'totalChars':len(r['content']),'source':r['source']} for r in originals]
        result.update(owner=task['owner'],project=task['project'],note='Use read for exact source quotes beyond the excerpt.')
        return result
    if args.action=='recall':
        result=store.memory(args.task,args.query,args.budget,at=args.at,known_at=args.known_at,
                            before=args.before,after=args.after,shared=args.shared,federated=args.federated)
        result['recallId']=memory.record_recall(store,args.task,args.session,'cli:'+str(time.time_ns()),args.query,result['retrieval'])
        return result
    if args.action=='read':return read_item(store,args.task,args.id,args.offset,args.size)
    if args.action=='skills':
        return store.skills(args.task,args.query,include_inactive=args.include_inactive,
                            at=args.at,known_at=args.known_at,before=args.before,after=args.after,
                            shared=args.shared,federated=args.federated,limit=args.limit,budget=args.budget)
    if args.action=='skill':
        return memory.read_skill(store,args.task,args.id)
    if args.action=='cases':
        return memory.cases(store,args.task,limit=args.limit)
    if args.action=='links':
        return memory.links(store,args.task,node_id=args.id,limit=args.limit)
    if args.action=='stats':
        return memory.stats(store,args.task)
    if args.action=='scenes':
        return memory.scenes(store,args.task)
    if args.action=='preflight':
        return memory.preflight_check(store,args.task,plan=args.plan,files=args.files,action_type=args.action_type)
    if args.action=='capsule':
        return memory.build_subagent_capsule(store,args.task,plan=args.plan,files=args.files,worker=args.worker,budget=args.budget)
    if args.action=='heal-skills' and args.dry_run:
        return memory.heal_skills(store,args.task,session=args.session,dry_run=True)
    if args.action=='consolidate' and args.dry_run:
        return memory.consolidate_memories(store,args.task,session=args.session,similarity_threshold=args.threshold,dry_run=True)
    if args.action=='benchmark':
        return memory.benchmark(store,args.task,budget=args.budget)
    if args.action=='inspector':
        return memory.inspector(store,args.task,budget=args.budget)
    if args.action=='export' and not args.write:
        return memory.export_rules(store,args.task,targets=args.targets,target_dir=args.target_dir,write_files=False,lang=args.lang)
    if task['owner']!=args.session:raise ValueError('current responsible session ID is required')
    runtime=os.environ.get('CODEX_THREAD_ID')
    if runtime and not os.environ.get('ANTIGRAVITY_AGENT') and args.session!='codex:'+runtime:
        raise ValueError('CLI actor does not match the current native Codex session')
    if os.environ.get('PRISM_PARENT_SESSION_ID'):
        raise ValueError('background worker memory writes must be returned to the responsible session')
    if args.action=='heal-skills':
        return memory.heal_skills(store,args.task,session=args.session,dry_run=False)
    if args.action=='verify-skill':
        proof=None
        if args.proof_file:
            raw=sys.stdin.read(200001) if args.proof_file=='-' else Path(args.proof_file).read_text(encoding='utf-8')
            proof=json.loads(raw)
        return memory.verify_skill(
            store,args.task,args.session,args.id,
            command=args.command,execution_proof=proof,
            operation_id=args.operation_id or None,timeout=args.timeout,
        )
    if args.action=='consolidate':
        return memory.consolidate_memories(store,args.task,session=args.session,similarity_threshold=args.threshold,dry_run=False)
    if args.action=='sync-md':
        return memory.sync_markdown(store,args.task,session=args.session,note_path=args.note_path,explicit_user=True if args.explicit_user else None)
    if args.action=='export':
        return memory.export_rules(store,args.task,targets=args.targets,target_dir=args.target_dir,write_files=True,lang=args.lang)
    if args.action=='capture':
        if args.file:
            raw=sys.stdin.read(200001) if args.file=='-' else Path(args.file).read_text(encoding='utf-8')
            if len(raw)>200000:raise ValueError('memory request exceeds limit')
            payload=json.loads(raw)
        elif args.text:
            payload={'text':args.text,'kind':args.kind,'command':args.command}
            if args.operation_id:payload['operationId']=args.operation_id
        else:
            raise ValueError('capture requires --text or --file')
        return memory.auto_capture(store,args.task,args.session,payload)
    if args.action=='index':return memory.index_task(store,args.task,args.limit)
    if args.action=='backfill':return backfill(store,args.task,args.source_session,args.end_byte)
    if args.action=='skill-sync':return memory.sync_skills(store,args.task,args.session)
    if args.action in ('learn','feedback','skill-manage'):
        raw=sys.stdin.read(200001) if args.file=='-' else Path(args.file).read_text(encoding='utf-8')
        if len(raw)>200000:raise ValueError('memory request exceeds limit')
        value=json.loads(raw)
        if args.action=='learn':return memory.learn(store,args.task,args.session,value['revision'],value['operationId'],value['entries'])
        if args.action=='skill-manage':
            spec=value.get('spec') if isinstance(value.get('spec'),dict) else {
                k:v for k,v in value.items() if k not in ('revision','operationId')
            }
            return memory.manage_skill(store,args.task,args.session,value['revision'],value['operationId'],spec)
        return memory.feedback(store,args.task,args.session,value['operationId'],value['recallId'],
                               value.get('itemId',''),value['outcome'],value.get('note',''),patch=value.get('patch'))


def cli():
    try:
        result = main()
        exit_zero = result.pop('_cli_exit_zero', False) if isinstance(result, dict) else False
        text = result.pop('_cli_text', None) if isinstance(result, dict) else None
        if text is not None:
            sys.stdout.write(text)
        else:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        if not exit_zero and isinstance(result, dict) and 'exitCode' in result:
            return int(result['exitCode'])
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'type': type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__=='__main__':
    sys.exit(cli())


