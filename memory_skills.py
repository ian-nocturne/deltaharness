"""Procedural skill synthesis, improvement, verification and scoped reuse loop.

Skills are distilled from grounded experience cards and user corrections. They
are stored in SQLite (memory_skills / memory_skill_events) and materialized as
ordinary UTF-8 SKILL.md packages under <root>/memory_skills/<task>/<name>/.
Skills are reference procedures, never auto-executed shell hooks or authority.
"""
import ast
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path

import work_memory as ledger
import memory_vectors as vectors

SKILL_VERSION = 5
SKILL_OPS = {'create', 'improve', 'consolidate', 'promote', 'degrade', 'retract', 'coedit', 'heal', 'verify_execution', 'verify_failed'}
SKILL_STATUSES = {'active', 'needs_revision', 'retracted'}
EVIDENCE_STATES = {'reported', 'trial-used', 'feedback-verified', 'user-confirmed', 'needs_revision', 'retracted'}
STALE_INACTIVE_DAYS = 30.0
CONSOLIDATION_SEMANTIC_THRESHOLD = 0.88
CONSOLIDATION_LEXICAL_THRESHOLD = 0.62

_PLACEHOLDERS = {
    '', 'tbd', 'todo', 'n/a', 'na', 'none', 'null', 'nil', '...', 'placeholder',
    'test', 'example', '없음', '미정', '추후', '예시', '테스트', '해당 없음', '해당없음',
}
_OVERGENERALIZED_CONDITIONS = {
    'always', 'all', 'all cases', 'every case', 'every task', 'all tasks',
    'any', 'any case', 'any situation', 'any task', 'unconditional', 'unconditionally',
    'universal', 'universally', 'everywhere', 'no condition', 'no conditions',
    '항상', '무조건', '모든 경우', '모든 작업', '모든 상황', '언제나', '제한 없음',
    '제한없음', '조건 없음', '조건없음', '예외 없음', '예외없음', '전부',
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _sha256_text(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def is_skill_primary_eligible(skill):
    """Return True if a skill is eligible for Stage-1 primary prompt injection."""
    if not isinstance(skill, dict):
        return False
    if skill.get('stale'):
        return False
    staleness = skill.get('staleness') or {}
    if staleness.get('stale'):
        return False
    v_status = skill.get('verificationStatus') or staleness.get('verificationStatus') or 'verified'
    if v_status == 'compatible-unverified':
        return False
    if int((skill.get('stats') or {}).get('incorrect') or 0) > 0:
        return False
    return True


_GIT_BRANCH_CACHE = {}


def detect_git_branch(root=None):
    """Fast zero-subprocess Git branch detector (Beads style branch-aware memory scope) with 10s in-memory cache."""
    key = str(root or '')
    now = time.monotonic()
    cached = _GIT_BRANCH_CACHE.get(key)
    if cached is not None and (now - cached[1]) < 10.0:
        return cached[0]
    start = Path(root) if root else Path.cwd()
    branch = 'main'
    try:
        curr = start.resolve()
        for candidate in [curr] + list(curr.parents):
            git_entry = candidate / '.git'
            if git_entry.is_dir():
                head_file = git_entry / 'HEAD'
                if head_file.is_file():
                    raw = head_file.read_text(encoding='utf-8', errors='ignore').strip()
                    if raw.startswith('ref: refs/heads/'):
                        branch = raw[len('ref: refs/heads/'):].strip()
                    else:
                        branch = raw[:12] if raw else 'detached'
                    break
            elif git_entry.is_file():
                # Git worktree .git file pointer
                raw_ptr = git_entry.read_text(encoding='utf-8', errors='ignore').strip()
                if raw_ptr.startswith('gitdir:'):
                    gitdir = Path(raw_ptr[len('gitdir:'):].strip())
                    if not gitdir.is_absolute():
                        gitdir = (candidate / gitdir).resolve()
                    head_file = gitdir / 'HEAD'
                    if head_file.is_file():
                        raw = head_file.read_text(encoding='utf-8', errors='ignore').strip()
                        if raw.startswith('ref: refs/heads/'):
                            branch = raw[len('ref: refs/heads/'):].strip()
                        else:
                            branch = raw[:12] if raw else 'detached'
                        break
    except OSError:
        pass
    if len(_GIT_BRANCH_CACHE) > 64:
        _GIT_BRANCH_CACHE.clear()
    _GIT_BRANCH_CACHE[key] = (branch, now)
    return branch


_FILE_HASH_AST_CACHE = {}
_FILE_SIGNATURE_CACHE = {}


from prism_memory import code_anchors as _code_anchors
from prism_memory import indexing as _indexing_core
from prism_memory import evaluation as _eval_core


def _cached_file_sha(file_path):
    """Return SHA256 of file_path using prism_memory.code_anchors cache."""
    return _code_anchors.cached_file_sha(file_path)


def _node_call_signature(node):
    """Extract call-contract signature metadata from a Python AST node."""
    return _code_anchors._node_call_signature(node)


def extract_file_symbol_signatures(file_path):
    """Extract call-contract signatures for all functions/classes in file_path to distinguish compatible vs breaking changes."""
    return _code_anchors.extract_file_symbol_signatures(file_path)


def is_breaking_symbol_change(old_sig, curr_sig):
    """Determine if a symbol evolution breaks existing call contracts (returns (is_breaking, reason))."""
    return _code_anchors.is_breaking_symbol_change(old_sig, curr_sig)


def extract_file_symbol_hashes(file_path):
    """Extract per-symbol (function/class) SHA256 hashes from a source file via AST (Python) or block regex."""
    return _code_anchors.extract_file_symbol_hashes(file_path)


def compute_actr_activation(
    event_at=0.0,
    observed_at=0.0,
    used_count=0,
    helpful_count=0,
    incorrect_count=0,
    now=None,
    decay=0.5,
    *,
    created_at=None,
    updated_at=None,
    used=None,
    helpful=None,
    incorrect=None,
):
    """Power-law base-level activation B_i = ln(sum(t_k^-d)) mapped to [0.0, 1.0]."""
    if created_at is not None and not event_at:
        event_at = created_at
    if updated_at is not None and not observed_at:
        observed_at = updated_at
    if used is not None and not used_count:
        used_count = used
    if helpful is not None and not helpful_count:
        helpful_count = helpful
    if incorrect is not None and not incorrect_count:
        incorrect_count = incorrect
    now = time.time() if now is None else float(now)
    base_ts = max(float(event_at or 0.0), float(observed_at or 0.0))
    if base_ts <= 0:
        base_ts = now
    age_hours = max(0.25, (now - base_ts) / 3600.0)
    total_rehearsals = 1 + max(0, int(used_count or 0)) + 2 * max(0, int(helpful_count or 0))
    energy = 0.0
    for k in range(total_rehearsals):
        effective_age = max(0.25, age_hours / (1.0 + 0.65 * k))
        energy += math.pow(effective_age, -decay)
    b_val = math.log(max(1e-6, energy))
    if int(incorrect_count or 0) > 0:
        b_val -= 0.8 * int(incorrect_count)
    norm_score = 1.0 / (1.0 + math.exp(-0.9 * (b_val + 0.5)))
    return round(norm_score, 4)


def _extract_referenced_files(card, root=None, explicit_files=None):
    """Extract existing regular file paths and referenced AST symbol hashes (file.py::Symbol)."""
    candidates = []
    explicit_symbols_by_raw = {}
    if isinstance(explicit_files, list):
        for x in explicit_files:
            if isinstance(x, str):
                if '::' in x:
                    f_part, sym_part = x.split('::', 1)
                    candidates.append(f_part.strip())
                    if sym_part.strip():
                        explicit_symbols_by_raw.setdefault(f_part.strip(), set()).add(sym_part.strip())
                elif '#' in x and not x.endswith('#'):
                    f_part, sym_part = x.split('#', 1)
                    candidates.append(f_part.strip())
                    if sym_part.strip() and not sym_part.strip().startswith('L'):
                        explicit_symbols_by_raw.setdefault(f_part.strip(), set()).add(sym_part.strip())
                else:
                    candidates.append(x.strip())
            elif isinstance(x, dict) and x.get('path'):
                candidates.append(str(x['path']).strip())
                if isinstance(x.get('symbols'), dict):
                    explicit_symbols_by_raw.setdefault(str(x['path']).strip(), set()).update(x['symbols'].keys())
                elif isinstance(x.get('symbols'), list):
                    explicit_symbols_by_raw.setdefault(str(x['path']).strip(), set()).update(str(s) for s in x['symbols'])
    blobs = [
        str(card.get('situation', '')),
        str(card.get('conditions', '')),
        str(card.get('counterexamples', '')),
        str(card.get('outcome', '')),
    ] + [str(s) for s in card.get('steps', [])]
    text_blob = '\n'.join(blobs)
    pos_text_blob = '\n'.join([
        str(card.get('situation', '')),
        str(card.get('conditions', '')),
        str(card.get('outcome', '')),
    ] + [str(s) for s in card.get('steps', [])])
    for match in re.finditer(r'(/(?:Users|var|private|tmp|opt|etc)/[^\s`\'"<>():;,]+|(?:[A-Za-z0-9_.-]+/)*(?:Makefile|makefile|GNUmakefile)|[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:py|json|md|sh|js|jsx|ts|tsx|mjs|cjs|go|rs|sql|tf|ya?ml))(?:::([A-Za-z_][A-Za-z0-9_.-]*))?', text_blob):
        token = match.group(1).strip('.,:;!?)')
        candidates.append(token)
        if match.group(2):
            explicit_symbols_by_raw.setdefault(token, set()).add(match.group(2).strip())
    recorded = []
    seen = set()
    root_path = Path(root) if root else None
    text_identifiers = set(re.findall(r'\b([A-Za-z_][A-Za-z0-9_.-]{1,})\b', pos_text_blob))
    for raw in candidates:
        p = Path(raw)
        if not p.is_absolute():
            if root_path and (root_path / p).exists():
                p = root_path / p
            else:
                continue
        if p.name in ('SKILL.md', 'provenance.json'):
            continue
        try:
            if p.is_symlink() or not p.is_file():
                continue
            resolved = str(p.resolve())
            if resolved in seen:
                continue
            stat = p.stat()
            if stat.st_size > 5 * 1024 * 1024:
                continue
            seen.add(resolved)
            all_sym_hashes = extract_file_symbol_hashes(p)
            explicit_syms = (
                set(explicit_symbols_by_raw.get(raw, set()))
                | set(explicit_symbols_by_raw.get(resolved, set()))
                | set(explicit_symbols_by_raw.get(p.name, set()))
            )
            wanted_syms = set(explicit_syms)
            if all_sym_hashes and not explicit_syms:
                for sym_name in all_sym_hashes:
                    if sym_name in text_identifiers:
                        wanted_syms.add(sym_name)
            tracked_symbols = {
                sym: all_sym_hashes[sym]
                for sym in sorted(wanted_syms)
                if sym in all_sym_hashes
            }
            sig_info = extract_file_symbol_signatures(p)
            all_sigs = sig_info.get('symbols') or {}
            tracked_sigs = {
                sym: all_sigs[sym]
                for sym in sorted(tracked_symbols)
                if sym in all_sigs
            }
            entry_obj = {
                'path': resolved,
                'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                'size': stat.st_size,
                'language': sig_info.get('__language__', 'unknown'),
                'resolution': sig_info.get('__resolution__', 'unsupported'),
            }
            if tracked_symbols:
                entry_obj['symbols'] = tracked_symbols
            if tracked_sigs:
                entry_obj['symbolSignatures'] = tracked_sigs
            elif all_sigs:
                # Store top-level file signatures when no specific symbol was named so file-level changes can still be contract-checked
                entry_obj['fileSignatures'] = {k: all_sigs[k] for k in sorted(all_sigs)[:25]}
            recorded.append(entry_obj)
            if len(recorded) >= 10:
                break
        except OSError:
            continue
    return recorded


def evaluate_staleness(skill_row, now=None):
    """Check symbol-anchored AST changes on disk, strict call-contract compatibility, and power-law dynamic staleness horizon."""
    now = time.time() if now is None else float(now)
    source = skill_row.get('source') if isinstance(skill_row.get('source'), dict) else {}
    ref_files = source.get('referencedFiles') if isinstance(source.get('referencedFiles'), list) else []
    changed_files = []
    changed_symbols = []
    symbol_anchored_saved = []
    breaking_changes = []
    compatible_changes = []
    unanchored_changes = []
    body_evolved = False
    for entry in ref_files:
        if not isinstance(entry, dict) or not entry.get('path') or not entry.get('sha256'):
            continue
        p = Path(entry['path'])
        try:
            if p.is_symlink() or not p.is_file():
                changed_files.append(entry['path'])
                breaking_changes.append(f"file_missing:{p.name}")
                continue
            curr_sha = _cached_file_sha(p)
            if curr_sha != entry['sha256']:
                sig_info = extract_file_symbol_signatures(p)
                if not sig_info.get('__syntax_ok__', True):
                    changed_files.append(entry['path'])
                    breaking_changes.append(f"syntax_error:{p.name}")
                    continue
                curr_sigs = sig_info.get('symbols') or {}
                tracked_syms = entry.get('symbols') if isinstance(entry.get('symbols'), dict) else {}
                tracked_sigs = entry.get('symbolSignatures') if isinstance(entry.get('symbolSignatures'), dict) else {}
                file_sigs = entry.get('fileSignatures') if isinstance(entry.get('fileSignatures'), dict) else {}
                if tracked_syms:
                    curr_syms = extract_file_symbol_hashes(p)
                    sym_diffs = [
                        sym for sym, old_sym_sha in tracked_syms.items()
                        if curr_syms.get(sym) != old_sym_sha
                    ]
                    if not sym_diffs:
                        # File changed outside the tracked symbols! Symbol anchor protects skill from false staleness.
                        symbol_anchored_saved.append(entry['path'])
                        continue
                    for sym in sym_diffs:
                        changed_symbols.append(f"{p.name}::{sym}")
                        if sym not in curr_syms or (curr_sigs and sym not in curr_sigs):
                            breaking_changes.append(f"symbol_deleted:{p.name}::{sym}")
                        elif sym in tracked_sigs and sym in curr_sigs:
                            is_brk, brk_reason = is_breaking_symbol_change(tracked_sigs[sym], curr_sigs[sym])
                            if is_brk:
                                breaking_changes.append(f"{brk_reason}:{p.name}::{sym}")
                            else:
                                compatible_changes.append(f"symbol_compatible:{p.name}::{sym}")
                                if brk_reason != 'docstring_or_comment_only':
                                    body_evolved = True
                        else:
                            # Missing prior signature baseline: cannot prove call-contract compatibility automatically
                            unanchored_changes.append(f"missing_signature_baseline:{p.name}::{sym}")
                            breaking_changes.append(f"missing_signature_baseline:{p.name}::{sym}")
                elif file_sigs:
                    for sym, old_s in file_sigs.items():
                        if sym not in curr_sigs:
                            breaking_changes.append(f"symbol_deleted:{p.name}::{sym}")
                        else:
                            is_brk, brk_reason = is_breaking_symbol_change(old_s, curr_sigs[sym])
                            if is_brk:
                                breaking_changes.append(f"{brk_reason}:{p.name}::{sym}")
                            elif brk_reason != 'docstring_or_comment_only':
                                body_evolved = True
                    if not breaking_changes:
                        compatible_changes.append(f"file_signatures_compatible:{p.name}")
                elif p.suffix.lower() not in ('.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.sh', '.go', '.rs', '.sql', '.tf') and p.name not in ('Makefile', 'makefile', 'GNUmakefile'):
                    compatible_changes.append(f"doc_or_config_compatible:{p.name}")
                else:
                    # Code file changed without any prior symbol or signature baseline -> refuse blind auto-heal
                    unanchored_changes.append(f"unanchored_code_file_changed:{p.name}")
                    breaking_changes.append(f"unanchored_code_file_changed:{p.name}")
                changed_files.append(entry['path'])
        except OSError:
            changed_files.append(entry['path'])
            breaking_changes.append(f"file_unreadable:{p.name}")
    last_fb_at = 0.0
    if isinstance(source.get('lastFeedback'), dict):
        last_fb_at = float(source['lastFeedback'].get('at') or 0.0)
    last_touch = max(float(skill_row.get('observed_at') or 0.0), float(skill_row.get('event_at') or 0.0), last_fb_at)
    inactive_days = max(0.0, (now - last_touch) / 86400.0) if last_touch > 0 else 0.0
    used_cnt = int(skill_row.get('used_count') or (skill_row.get('stats') or {}).get('used') or 0)
    helpful_cnt = int(skill_row.get('helpful_count') or (skill_row.get('stats') or {}).get('helpful') or 0)
    incorrect_cnt = int(skill_row.get('incorrect_count') or (skill_row.get('stats') or {}).get('incorrect') or 0)
    effective_stale_days = min(180.0, STALE_INACTIVE_DAYS + 15.0 * (used_cnt + 2 * helpful_cnt))
    actr_score = compute_actr_activation(
        skill_row.get('event_at'), skill_row.get('observed_at'),
        used_count=used_cnt, helpful_count=helpful_cnt, incorrect_count=incorrect_cnt, now=now,
    )
    reasons = []
    for ch in changed_files:
        reasons.append('referenced_file_changed:' + Path(ch).name)
    for cs in changed_symbols[:4]:
        reasons.append('referenced_symbol_changed:' + cs)
    for bc in breaking_changes[:4]:
        reasons.append('breaking_change:' + bc)
    if inactive_days >= effective_stale_days:
        reasons.append('inactive_30d')
    self_healable = bool(changed_files and not breaking_changes and not unanchored_changes and inactive_days < effective_stale_days)
    if breaking_changes:
        ver_status = 'broken'
    elif unanchored_changes:
        ver_status = 'unanchored'
    elif body_evolved or source.get('verificationStatus') == 'compatible-unverified':
        ver_status = 'compatible-unverified'
    else:
        ver_status = source.get('verificationStatus') or 'verified'
    return {
        'stale': bool(reasons),
        'reasons': reasons,
        'referencedFileCount': len(ref_files),
        'changedFiles': changed_files,
        'changedSymbols': changed_symbols,
        'symbolAnchoredSaved': symbol_anchored_saved,
        'breakingChanges': breaking_changes,
        'compatibleChanges': compatible_changes,
        'unanchoredChanges': unanchored_changes,
        'selfHealable': self_healable,
        'verificationStatus': ver_status,
        'selfHealedCount': int(source.get('selfHealedCount') or 0),
        'inactiveDays': round(inactive_days, 2),
        'effectiveStaleDays': round(effective_stale_days, 1),
        'actrActivation': actr_score,
    }


def heal_compatible_skills(db, root=None, task=None, skill_id=None, dry_run=False, now=None):
    """AST Signature-Aware Self-Healing Re-Binding: automatically rebind referenced file/symbol hashes when changes are backward-compatible, while refusing to mask breaking or unanchored changes."""
    ensure_schema(db)
    now = time.time() if now is None else float(now)
    params = []
    where = ["status='active'"]
    if task:
        where.append('task=?')
        params.append(task)
    if skill_id:
        where.append('skill_id=?')
        params.append(skill_id)
    rows = [
        _public_skill(r)
        for r in db.execute(
            f"SELECT * FROM memory_skills WHERE {' AND '.join(where)} ORDER BY event_id DESC",
            params,
        ).fetchall()
    ]
    healed = []
    blocked = []
    staged_materialize = []
    for sk in rows:
        st = evaluate_staleness(sk, now=now)
        if not st.get('stale'):
            continue
        if st.get('breakingChanges') or st.get('unanchoredChanges') or not st.get('selfHealable'):
            blocked.append({
                'skillId': sk['skill_id'],
                'task': sk['task'],
                'breakingChanges': st.get('breakingChanges', []),
                'unanchoredChanges': st.get('unanchoredChanges', []),
                'reasons': st.get('reasons', []),
            })
            continue
        # Compatible evolution: re-bind file sha256, symbol AST hashes, and call-contract signatures
        old_source = dict(sk.get('source') or {})
        old_refs = old_source.get('referencedFiles') if isinstance(old_source.get('referencedFiles'), list) else []
        new_refs = []
        rebound_details = []
        for entry in old_refs:
            if not isinstance(entry, dict) or not entry.get('path'):
                continue
            p = Path(entry['path'])
            if p.is_symlink() or not p.is_file():
                new_refs.append(entry)
                continue
            raw_bytes = p.read_bytes()
            new_sha = hashlib.sha256(raw_bytes).hexdigest()
            all_sym_hashes = extract_file_symbol_hashes(p)
            sig_info = extract_file_symbol_signatures(p)
            all_sigs = sig_info.get('symbols') or {}
            old_syms = entry.get('symbols') if isinstance(entry.get('symbols'), dict) else {}
            if not old_syms and all_sym_hashes:
                pos_blob = '\n'.join([sk.get('title', ''), sk.get('conditions', ''), sk.get('outcome', '')] + list(sk.get('steps') or []))
                mentioned = set(re.findall(r'\b([A-Za-z_][A-Za-z0-9_]{2,})\b', pos_blob))
                old_syms = {sym: all_sym_hashes[sym] for sym in sorted(mentioned) if sym in all_sym_hashes}
            updated_syms = {
                sym: all_sym_hashes[sym]
                for sym in sorted(old_syms)
                if sym in all_sym_hashes
            }
            updated_sigs = {
                sym: all_sigs[sym]
                for sym in sorted(updated_syms)
                if sym in all_sigs
            }
            new_entry = {
                'path': str(p.resolve()),
                'sha256': new_sha,
                'size': len(raw_bytes),
            }
            if updated_syms:
                new_entry['symbols'] = updated_syms
            if updated_sigs:
                new_entry['symbolSignatures'] = updated_sigs
            elif all_sigs:
                new_entry['fileSignatures'] = {k: all_sigs[k] for k in sorted(all_sigs)[:25]}
            if new_sha != entry.get('sha256'):
                rebound_details.append({
                    'file': p.name,
                    'oldSha256': entry.get('sha256', '')[:12],
                    'newSha256': new_sha[:12],
                    'anchoredSymbols': sorted(updated_syms.keys()),
                })
            new_refs.append(new_entry)
        heal_count = int(old_source.get('selfHealedCount') or 0) + 1
        ver_status = st.get('verificationStatus', 'compatible-unverified')
        target_ev_state = sk['evidence_state']
        if rebound_details and ver_status == 'compatible-unverified' and target_ev_state == 'feedback-verified':
            target_ev_state = 'trial-used'
        new_source = {
            **old_source,
            'referencedFiles': new_refs,
            'evidenceState': target_ev_state,
            'selfHealedCount': heal_count,
            'lastSelfHealedAt': now,
            'lastSelfHealedDetails': rebound_details,
            'verificationStatus': ver_status,
            'verificationMode': 'ast-self-healed',
            'priorVerifiedClearedByCodeChange': bool(target_ev_state != sk['evidence_state']),
        }
        card = {
            'situation': (old_source.get('experience') or {}).get('situation') or sk['title'],
            'steps': list(sk['steps']),
            'outcome': sk['outcome'],
            'conditions': sk['conditions'],
            'counterexamples': sk['counterexamples'],
        }
        new_body = render_skill_markdown(
            name=sk['name'],
            skill_id=sk['skill_id'],
            experience_id=sk['experience_id'],
            task=sk['task'],
            scope=sk['scope'],
            version=sk['version'],
            status=sk['status'],
            evidence_state=target_ev_state,
            title=sk['title'],
            description=sk['description'],
            triggers=sk['triggers'],
            card=card,
            source=new_source,
            stats=sk['stats'],
        )
        new_file_sha = _sha256_text(new_body)
        if not dry_run:
            row_payload = {
                'task': sk['task'],
                'revision': sk['revision'],
                'event_key': 'skill-heal:' + _digest([sk['task'], sk['skill_id'], new_file_sha, heal_count, int(now * 1000)]),
                'skill_id': sk['skill_id'],
                'name': sk['name'],
                'experience_id': sk['experience_id'],
                'version': sk['version'],
                'operation': 'heal',
                'status': sk['status'],
                'evidence_state': target_ev_state,
                'scope': sk['scope'],
                'title': sk['title'],
                'description': sk['description'],
                'triggers': sk['triggers'],
                'steps': sk['steps'],
                'outcome': sk['outcome'],
                'conditions': sk['conditions'],
                'counterexamples': sk['counterexamples'],
                'body': new_body,
                'skill_path': sk['skill_path'],
                'file_sha256': new_file_sha,
                'source': new_source,
                'prior_version': sk['version'],
                'prior_status': sk['status'],
                'prior_evidence_state': sk['evidence_state'],
                'helpful_count': sk['helpful_count'],
                'used_count': sk['used_count'],
                'incorrect_count': sk['incorrect_count'],
                'missing_count': sk['missing_count'],
                'valid_from': sk['valid_from'],
                'valid_until': sk['valid_until'],
                'event_at': sk['event_at'],
                'observed_at': now,
            }
            _write_skill_event_and_row(db, row_payload)
            if target_ev_state != sk['evidence_state']:
                exp_item = ledger.get_item(db, sk['task'], sk['experience_id'])
                if exp_item and exp_item['status'] == 'active':
                    exp_source = {
                        k: v for k, v in exp_item['source'].items()
                        if k not in ('contentSha256', 'contentChars', 'observedAt')
                    }
                    exp_source.update({
                        'evidenceState': target_ev_state,
                        'verificationStatus': ver_status,
                    })
                    ledger.record(
                        db, sk['task'], sk['revision'],
                        'exp-heal:' + _digest([sk['task'], sk['experience_id'], new_file_sha, heal_count]),
                        'experience', sk['experience_id'], 'replace', exp_item['content'], exp_source,
                        event_at=now, valid_from=exp_item.get('valid_from'), valid_until=exp_item.get('valid_until'),
                    )
            if root is not None:
                events = skill_events(db, sk['task'], sk['skill_id'], limit=30)
                staged_materialize.append((_public_skill(row_payload), events))
        healed.append({
            'skillId': sk['skill_id'],
            'task': sk['task'],
            'compatibleChanges': st.get('compatibleChanges', []),
            'verificationStatus': ver_status,
            'evidenceState': target_ev_state,
            'priorEvidenceState': sk['evidence_state'],
            'reboundFiles': rebound_details,
            'selfHealedCount': heal_count,
        })
    for skill_row, events in staged_materialize:
        materialize_skill(root, skill_row, events)
    return {
        'ok': True,
        'taskId': task,
        'dryRun': bool(dry_run),
        'healedCount': len(healed),
        'healedSkills': healed,
        'blockedBreakingCount': len(blocked),
        'blockedBreakingSkills': blocked,
    }


_ENSURED_DB_TS = {}


def ensure_schema(db):
    now_m = time.monotonic()
    db_id = id(db)
    if now_m - _ENSURED_DB_TS.get(db_id, -10.0) < 1.5:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_skills'").fetchone():
            return
    for sql in (
        '''CREATE TABLE IF NOT EXISTS memory_skills (
           task TEXT NOT NULL,
           skill_id TEXT NOT NULL,
           name TEXT NOT NULL,
           experience_id TEXT NOT NULL,
           revision INTEGER NOT NULL,
           version INTEGER NOT NULL,
           event_id INTEGER NOT NULL,
           status TEXT NOT NULL,
           evidence_state TEXT NOT NULL,
           scope TEXT NOT NULL DEFAULT 'task',
           title TEXT NOT NULL,
           description TEXT NOT NULL,
           triggers TEXT NOT NULL,
           steps TEXT NOT NULL,
           outcome TEXT NOT NULL,
           conditions TEXT NOT NULL,
           counterexamples TEXT NOT NULL,
           body TEXT NOT NULL,
           skill_path TEXT NOT NULL,
           file_sha256 TEXT NOT NULL,
           source TEXT NOT NULL,
           helpful_count INTEGER NOT NULL DEFAULT 0,
           used_count INTEGER NOT NULL DEFAULT 0,
           incorrect_count INTEGER NOT NULL DEFAULT 0,
           missing_count INTEGER NOT NULL DEFAULT 0,
           valid_from REAL,
           valid_until REAL,
           event_at REAL NOT NULL,
           observed_at REAL NOT NULL,
           PRIMARY KEY(task, skill_id))''',
        '''CREATE TABLE IF NOT EXISTS memory_skill_events (
           id INTEGER PRIMARY KEY AUTOINCREMENT,
           task TEXT NOT NULL,
           revision INTEGER NOT NULL,
           event_key TEXT NOT NULL,
           skill_id TEXT NOT NULL,
           name TEXT NOT NULL,
           experience_id TEXT NOT NULL,
           version INTEGER NOT NULL,
           operation TEXT NOT NULL,
           status TEXT NOT NULL,
           evidence_state TEXT NOT NULL,
           scope TEXT NOT NULL,
           title TEXT NOT NULL,
           description TEXT NOT NULL,
           triggers TEXT NOT NULL,
           steps TEXT NOT NULL,
           outcome TEXT NOT NULL,
           conditions TEXT NOT NULL,
           counterexamples TEXT NOT NULL,
           body TEXT NOT NULL,
           skill_path TEXT NOT NULL,
           file_sha256 TEXT NOT NULL,
           source TEXT NOT NULL,
           prior_version INTEGER NOT NULL DEFAULT 0,
           prior_status TEXT NOT NULL DEFAULT '',
           prior_evidence_state TEXT NOT NULL DEFAULT '',
           helpful_count INTEGER NOT NULL DEFAULT 0,
           used_count INTEGER NOT NULL DEFAULT 0,
           incorrect_count INTEGER NOT NULL DEFAULT 0,
           missing_count INTEGER NOT NULL DEFAULT 0,
           valid_from REAL,
           valid_until REAL,
           event_at REAL NOT NULL,
           observed_at REAL NOT NULL,
           UNIQUE(task, event_key))''',
        '''CREATE INDEX IF NOT EXISTS memory_skills_task_status
           ON memory_skills(task, status, event_id DESC)''',
        '''CREATE INDEX IF NOT EXISTS memory_skill_events_lookup
           ON memory_skill_events(task, skill_id, id DESC)''',
        '''CREATE TRIGGER IF NOT EXISTS memory_skill_events_no_update
           BEFORE UPDATE ON memory_skill_events
           BEGIN SELECT RAISE(ABORT, 'memory_skill_events is append-only'); END''',
        '''CREATE TRIGGER IF NOT EXISTS memory_skill_events_no_delete
           BEFORE DELETE ON memory_skill_events
           BEGIN SELECT RAISE(ABORT, 'memory_skill_events is append-only'); END''',
        '''CREATE TABLE IF NOT EXISTS memory_links (
           task TEXT NOT NULL,
           source_id TEXT NOT NULL,
           target_id TEXT NOT NULL,
           relation TEXT NOT NULL,
           weight REAL NOT NULL,
           evidence TEXT NOT NULL,
           updated_at REAL NOT NULL,
           PRIMARY KEY(task, source_id, target_id, relation))''',
        '''CREATE INDEX IF NOT EXISTS memory_links_source
           ON memory_links(task, source_id, weight DESC)''',
        '''CREATE INDEX IF NOT EXISTS memory_links_target
           ON memory_links(task, target_id, weight DESC)''',
        '''CREATE TABLE IF NOT EXISTS memory_session_state (
           task TEXT NOT NULL,
           session TEXT NOT NULL,
           core_digest TEXT NOT NULL,
           delivered_ids TEXT NOT NULL,
           turn_count INTEGER NOT NULL DEFAULT 0,
           updated_at REAL NOT NULL,
           last_primary_ids TEXT NOT NULL DEFAULT '[]',
           last_turn_query TEXT NOT NULL DEFAULT '',
           PRIMARY KEY(task, session))''',
    ):
        db.execute(sql)
    for alter_sql in (
        "ALTER TABLE memory_session_state ADD COLUMN last_primary_ids TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE memory_session_state ADD COLUMN last_turn_query TEXT NOT NULL DEFAULT ''",
    ):
        try:
            db.execute(alter_sql)
        except Exception:
            pass
    if len(_ENSURED_DB_TS) > 64:
        _ENSURED_DB_TS.clear()
    _ENSURED_DB_TS[db_id] = now_m


def _clean_token(value):
    return re.sub(r'\s+', ' ', ledger._normalize(str(value or ''))).strip(' .,:;!?-_/\'"()[]{}')


def _is_placeholder(value):
    cleaned = _clean_token(value)
    return not cleaned or cleaned in _PLACEHOLDERS or len(cleaned) < 2


def _is_overgeneralized(conditions):
    cleaned = _clean_token(conditions)
    if cleaned in _OVERGENERALIZED_CONDITIONS:
        return True
    for pattern in (
        r'^(?:always|unconditionally|in all cases|for every task|for all tasks)\b',
        r'^(?:항상|무조건|모든 경우에|모든 작업에서|언제나 예외 없이)\b',
    ):
        if re.search(pattern, cleaned):
            return True
    return False


def extract_wiki_links(text_blob):
    """Extract explicit [[target_id]], [[rel:target_id]], or [[target_id|label]] links from text."""
    if not isinstance(text_blob, str) or '[[' not in text_blob:
        return []
    found = []
    seen = set()
    for match in re.finditer(r'\[\[([^\[\]|#]+?)(?:\|[^\[\]]*)?\]\]', text_blob):
        target = match.group(1).strip()
        target = re.sub(
            r'^(?:depends_on|blocks|mitigates|verifies|implements|supersedes|consolidated_into):',
            '',
            target,
            flags=re.IGNORECASE,
        ).strip()
        if target and len(target) <= 240 and target not in seen:
            seen.add(target)
            found.append(target)
    return found[:24]


def validate_case_card(card):
    """Validate a pitfall/failure case card."""
    if not isinstance(card, dict):
        raise ValueError('case card must be an object')
    allowed = {'situation', 'symptom', 'rootCause', 'avoidance', 'conditions', 'severity', 'title', 'contrastiveDiff'}
    if set(card) - allowed:
        raise ValueError('invalid case card fields')
    required = {'situation', 'symptom', 'avoidance', 'conditions'}
    if not required.issubset(card):
        raise ValueError('case needs situation, symptom, avoidance, conditions')
    situation = ledger._text(card.get('situation'), 'situation', 2000, empty=False)
    symptom = ledger._text(card.get('symptom'), 'symptom', 2000, empty=False)
    root_cause = ledger._text(card.get('rootCause', ''), 'rootCause', 2000, empty=True)
    conditions = ledger._text(card.get('conditions'), 'conditions', 2000, empty=False)
    severity = str(card.get('severity') or 'warning').strip().lower()
    if severity not in ('warning', 'critical'):
        raise ValueError('case severity must be warning or critical')
    if _is_placeholder(situation) or len(situation.strip()) < 3:
        raise ValueError('case situation must be specific and non-placeholder')
    if _is_placeholder(symptom) or len(symptom.strip()) < 3:
        raise ValueError('case symptom must be specific and non-placeholder')
    if _is_placeholder(conditions) or len(conditions.strip()) < 3:
        raise ValueError('case conditions must be specific and non-placeholder')
    if _is_overgeneralized(conditions):
        raise ValueError('case conditions cannot be unconditional or over-generalized')
    raw_avoidance = card.get('avoidance')
    if isinstance(raw_avoidance, str):
        raw_avoidance = [raw_avoidance]
    if not isinstance(raw_avoidance, list) or not 1 <= len(raw_avoidance) <= 10:
        raise ValueError('case avoidance must be 1 to 10 concrete rules')
    avoidance = []
    for item in raw_avoidance:
        rule = ledger._text(item, 'avoidance', 600, empty=False)
        if _is_placeholder(rule) or len(rule.strip()) < 3:
            raise ValueError('each case avoidance rule must be concrete and non-placeholder')
        avoidance.append(rule)
    result = {
        'situation': situation,
        'symptom': symptom,
        'rootCause': root_cause,
        'avoidance': avoidance,
        'conditions': conditions,
        'severity': severity,
    }
    if card.get('title'):
        result['title'] = ledger._text(card.get('title'), 'title', 240, empty=False)
    if isinstance(card.get('contrastiveDiff'), dict):
        result['contrastiveDiff'] = card['contrastiveDiff']
    return result


def format_case_content(card):
    """Render canonical text for a pitfall/failure case memory item."""
    lines = [
        f"[함정·실패 사례] {card['situation']}",
        f"증상/실패: {card['symptom']}",
    ]
    if card.get('rootCause'):
        lines.append(f"원인: {card['rootCause']}")
    lines.append(f"회피 수칙: {' / '.join(card['avoidance'])}")
    lines.append(f"발생 조건: {card['conditions']}")
    return '\n'.join(lines)


def validate_substance(card, strict_counterexamples=True):
    """Validate procedural substance and reject over-generalized or trivial cards."""
    if not isinstance(card, dict):
        raise ValueError('skill procedure card must be an object')
    required = {'situation', 'steps', 'outcome', 'conditions', 'counterexamples'}
    if set(card) - (required | {'title', 'description', 'triggers', 'name'}):
        raise ValueError('invalid skill procedure fields')
    if not required.issubset(card):
        raise ValueError('experience needs situation, steps, outcome, conditions, counterexamples')
    situation = ledger._text(card.get('situation'), 'situation', 2000, empty=False)
    outcome = ledger._text(card.get('outcome'), 'outcome', 2000, empty=False)
    conditions = ledger._text(card.get('conditions'), 'conditions', 2000, empty=False)
    counterexamples = ledger._text(card.get('counterexamples', ''), 'counterexamples', 2000,
                                   empty=not strict_counterexamples)
    if _is_placeholder(situation) or len(situation.strip()) < 3:
        raise ValueError('experience situation must be specific and non-placeholder')
    if _is_placeholder(outcome) or len(outcome.strip()) < 3:
        raise ValueError('experience outcome must be specific and non-placeholder')
    if _is_placeholder(conditions) or len(conditions.strip()) < 3:
        raise ValueError('experience conditions must be specific and non-placeholder')
    if _is_overgeneralized(conditions):
        raise ValueError('experience conditions cannot be unconditional or over-generalized')
    raw_steps = card.get('steps')
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 20:
        raise ValueError('experience needs bounded steps')
    steps = []
    seen_steps = set()
    for raw_step in raw_steps:
        step = ledger._text(raw_step, 'step', 600, empty=False)
        if _is_placeholder(step) or len(step.strip()) < 3:
            raise ValueError('each procedural step must be concrete and non-placeholder')
        norm = _clean_token(step)
        seen_steps.add(norm)
        steps.append(step)
    if strict_counterexamples:
        if len(steps) < 2 or len(seen_steps) < 2:
            raise ValueError('procedural skill requires at least 2 distinct concrete steps')
        if _is_placeholder(counterexamples) or len(counterexamples.strip()) < 3:
            raise ValueError('procedural skill requires explicit counterexamples or boundary exceptions')
    elif counterexamples and _is_placeholder(counterexamples):
        raise ValueError('counterexamples cannot be a placeholder token')
    return {
        'situation': situation,
        'steps': steps,
        'outcome': outcome,
        'conditions': conditions,
        'counterexamples': counterexamples,
    }


def can_auto_synthesize(card):
    """Validate basic card rules; return True only when full skill substance is met."""
    validated = validate_substance(card, strict_counterexamples=False)
    distinct = {_clean_token(s) for s in validated['steps']}
    if len(validated['steps']) < 2 or len(distinct) < 2:
        return False
    if _is_placeholder(validated['counterexamples']) or len(validated['counterexamples'].strip()) < 3:
        return False
    return True


def skill_name_for(item_id, explicit_name=None, title=''):
    raw = str(explicit_name or '').strip()
    if not raw:
        raw = str(item_id or '').strip()
        for prefix in ('skill:', 'experience:'):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
    slug = re.sub(r'[^a-z0-9]+', '-', raw.lower()).strip('-')
    slug = re.sub(r'-{2,}', '-', slug)
    if not slug or len(slug) < 2:
        slug = 'skill-' + hashlib.sha256((str(item_id) + ':' + str(title)).encode('utf-8')).hexdigest()[:16]
    if len(slug) > 64:
        suffix = hashlib.sha256(slug.encode('utf-8')).hexdigest()[:8]
        slug = slug[:55].rstrip('-') + '-' + suffix
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
        raise ValueError('invalid skill name')
    return slug


def skill_id_for(item_id, explicit_id=None, explicit_name=None):
    if explicit_id:
        sid = ledger._identifier(explicit_id, 'skillId')
        return sid if sid.startswith('skill:') else 'skill:' + sid
    raw = str(item_id or '').strip()
    if raw.startswith('skill:'):
        return ledger._identifier(raw, 'skillId')
    if raw.startswith('experience:'):
        return ledger._identifier('skill:' + raw[len('experience:'):], 'skillId')
    name = skill_name_for(raw, explicit_name=explicit_name)
    return ledger._identifier('skill:' + name, 'skillId')


def _task_slug(task):
    cleaned = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(task)).strip('_')
    digest = hashlib.sha256(str(task).encode('utf-8')).hexdigest()[:12]
    return (cleaned[:48] + '-' if cleaned else '') + digest


def _triggers_for(card, source, extra_triggers=None):
    candidates = []
    if isinstance(extra_triggers, list):
        for item in extra_triggers[:12]:
            candidates.append(ledger._text(item, 'trigger', 160, empty=False))
    for key in ('entities', 'aliases'):
        for item in source.get(key, [])[:12]:
            candidates.append(ledger._text(item, key, 160, empty=False))
    for field in (card['situation'], card['conditions']):
        phrase = field.split('\n', 1)[0].strip()
        if phrase:
            candidates.append(phrase[:160])
    deduped = []
    seen = set()
    for item in candidates:
        norm = _clean_token(item)
        if norm and norm not in seen:
            seen.add(norm)
            deduped.append(item)
    return deduped[:16]


def render_skill_markdown(name, skill_id, experience_id, task, scope, version, status,
                          evidence_state, title, description, triggers, card, source, stats):
    yaml_desc = description.replace('\n', ' ').replace('"', "'").strip()
    lines = [
        '---',
        f'name: {name}',
        f'description: "{yaml_desc}"',
        '---',
        '',
        f'# {title}',
        '',
        '> PRISM 절차 기억 스킬이에요. 자동 실행 훅이나 권한 승인이 아닌 참고 절차이며, 현재 사용자 지시·원본 근거·실행 통제가 항상 우선해요.',
        '',
        f'- 스킬 ID: `{skill_id}`',
        f'- 원본 경험 ID: `{experience_id}`',
        f'- 업무: `{task}` · 범위: `{scope}`',
        f'- 버전: `v{version}` · 상태: `{status}` · 근거 수준: `{evidence_state}`',
        f"- 활용 집계: 사용 {stats.get('used', 0)}회 · 도움 {stats.get('helpful', 0)}회 · 오류 {stats.get('incorrect', 0)}회 · 보완요청 {stats.get('missing', 0)}회",
        '',
        '## 언제 사용하나요 (Trigger & Situation)',
        '',
        card['situation'],
    ]
    if triggers:
        lines += ['', '주요 트리거 키워드: ' + ', '.join(f'`{t}`' for t in triggers)]
    lines += [
        '',
        '## 적용 조건 (Conditions)',
        '',
        card['conditions'],
        '',
        '## 적용 예외 및 반례 (Counterexamples — 이럴 때는 쓰지 마세요)',
        '',
        card['counterexamples'],
        '',
        '## 실행 절차 (Step-by-Step Procedure)',
        '',
    ]
    for idx, step in enumerate(card['steps'], 1):
        lines.append(f'{idx}. {step}')
    lines += [
        '',
        '## 기대 결과 및 검증 기준 (Expected Outcome)',
        '',
        card['outcome'],
        '',
        '## 원본 근거 (Provenance)',
        '',
        '- 상세 이력 및 원문 인용: `references/provenance.json`',
    ]
    for ref in source.get('sourceRefs', []):
        lines.append(
            f"- 원문 `{ref.get('itemId')}` (사건 `{ref.get('eventId')}`, 종류 `{ref.get('kind')}`, SHA256 `{ref.get('contentSha256')}`)"
        )
    return '\n'.join(lines) + '\n'


def _assert_no_symlink(path, stop_at=None):
    current = Path(path)
    stop = Path(stop_at) if stop_at else None
    for candidate in [current] + list(current.parents):
        if candidate.is_symlink():
            raise ValueError(f'symlink path rejected: {candidate}')
        if stop and candidate == stop:
            break


def _atomic_write(path, content):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix='.prism-skill-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def skill_paths(root, task, name):
    base = Path(root) / 'memory_skills'
    task_dir = base / _task_slug(task)
    skill_dir = task_dir / name
    ref_dir = skill_dir / 'references'
    return base, task_dir, skill_dir, ref_dir, skill_dir / 'SKILL.md', ref_dir / 'provenance.json'


def materialize_skill(root, row, events=()):
    """Write SKILL.md and references/provenance.json safely without symlinks."""
    root = Path(root)
    _assert_no_symlink(root, stop_at=root)
    base, task_dir, skill_dir, ref_dir, md_path, prov_path = skill_paths(root, row['task'], row['name'])
    for folder in (base, task_dir, skill_dir, ref_dir):
        if folder.is_symlink():
            raise ValueError(f'skill folder cannot be a symlink: {folder}')
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    for target in (md_path, prov_path):
        if target.is_symlink():
            raise ValueError(f'skill target cannot be a symlink: {target}')
    _atomic_write(md_path, row['body'])
    provenance = {
        'schemaVersion': SKILL_VERSION,
        'task': row['task'],
        'skillId': row['skill_id'],
        'name': row['name'],
        'experienceId': row['experience_id'],
        'revision': row['revision'],
        'version': row['version'],
        'status': row['status'],
        'evidenceState': row['evidence_state'],
        'scope': row['scope'],
        'title': row['title'],
        'description': row['description'],
        'triggers': row['triggers'],
        'steps': row['steps'],
        'outcome': row['outcome'],
        'conditions': row['conditions'],
        'counterexamples': row['counterexamples'],
        'fileSha256': row['file_sha256'],
        'validFrom': row['valid_from'],
        'validUntil': row['valid_until'],
        'eventAt': row['event_at'],
        'observedAt': row['observed_at'],
        'stats': {
            'used': row['used_count'],
            'helpful': row['helpful_count'],
            'incorrect': row['incorrect_count'],
            'missing': row['missing_count'],
        },
        'source': row['source'],
        'history': [
            {
                'eventId': e['id'],
                'version': e['version'],
                'revision': e['revision'],
                'operation': e['operation'],
                'status': e['status'],
                'evidenceState': e['evidence_state'],
                'priorVersion': e['prior_version'],
                'priorStatus': e['prior_status'],
                'priorEvidenceState': e['prior_evidence_state'],
                'fileSha256': e['file_sha256'],
                'eventAt': e['event_at'],
                'observedAt': e['observed_at'],
            }
            for e in events[:30]
        ],
    }
    _atomic_write(prov_path, json.dumps(provenance, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
    return str(md_path)


def _public_skill(row):
    if row is None:
        return None
    item = dict(row)
    for key in ('triggers', 'steps', 'source'):
        if key in item and isinstance(item[key], str):
            item[key] = json.loads(item[key])
    item['stats'] = {
        'used': item.get('used_count', 0),
        'helpful': item.get('helpful_count', 0),
        'incorrect': item.get('incorrect_count', 0),
        'missing': item.get('missing_count', 0),
    }
    item['skillId'] = item.get('skill_id')
    item['evidenceState'] = item.get('evidence_state')
    item['useCount'] = item.get('used_count', 0)
    item['helpfulCount'] = item.get('helpful_count', 0)
    staleness = evaluate_staleness(item)
    item['staleness'] = staleness
    item['stale'] = staleness['stale']
    return item


def get_skill(db, task, skill_or_exp_id):
    ledger._identifier(task, 'task')
    ledger._identifier(skill_or_exp_id, 'skillId')
    row = db.execute(
        'SELECT * FROM memory_skills WHERE task=? AND (skill_id=? OR experience_id=? OR name=?) ORDER BY event_id DESC LIMIT 1',
        (task, skill_or_exp_id, skill_or_exp_id, skill_or_exp_id),
    ).fetchone()
    return _public_skill(row)


def skill_events(db, task, skill_id=None, limit=50):
    ledger._identifier(task, 'task')
    limit = ledger._integer(limit, 'limit', 1, 500)
    if skill_id:
        ledger._identifier(skill_id, 'skillId')
        rows = db.execute(
            'SELECT * FROM memory_skill_events WHERE task=? AND skill_id=? ORDER BY id DESC LIMIT ?',
            (task, skill_id, limit),
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT * FROM memory_skill_events WHERE task=? ORDER BY id DESC LIMIT ?',
            (task, limit),
        ).fetchall()
    return [_public_skill(r) for r in rows]


def _write_skill_event_and_row(db, row):
    cur = db.execute(
        '''INSERT INTO memory_skill_events(
           task,revision,event_key,skill_id,name,experience_id,version,operation,
           status,evidence_state,scope,title,description,triggers,steps,outcome,
           conditions,counterexamples,body,skill_path,file_sha256,source,
           prior_version,prior_status,prior_evidence_state,
           helpful_count,used_count,incorrect_count,missing_count,
           valid_from,valid_until,event_at,observed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            row['task'], row['revision'], row['event_key'], row['skill_id'], row['name'],
            row['experience_id'], row['version'], row['operation'], row['status'],
            row['evidence_state'], row['scope'], row['title'], row['description'],
            _json(row['triggers']), _json(row['steps']), row['outcome'], row['conditions'],
            row['counterexamples'], row['body'], row['skill_path'], row['file_sha256'],
            _json(row['source']), row['prior_version'], row['prior_status'],
            row['prior_evidence_state'], row['helpful_count'], row['used_count'],
            row['incorrect_count'], row['missing_count'], row['valid_from'],
            row['valid_until'], row['event_at'], row['observed_at'],
        ),
    )
    event_id = cur.lastrowid
    row['event_id'] = event_id
    db.execute(
        '''INSERT INTO memory_skills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(task,skill_id) DO UPDATE SET
           name=excluded.name,experience_id=excluded.experience_id,revision=excluded.revision,
           version=excluded.version,event_id=excluded.event_id,status=excluded.status,
           evidence_state=excluded.evidence_state,scope=excluded.scope,title=excluded.title,
           description=excluded.description,triggers=excluded.triggers,steps=excluded.steps,
           outcome=excluded.outcome,conditions=excluded.conditions,
           counterexamples=excluded.counterexamples,body=excluded.body,
           skill_path=excluded.skill_path,file_sha256=excluded.file_sha256,
           source=excluded.source,helpful_count=excluded.helpful_count,
           used_count=excluded.used_count,incorrect_count=excluded.incorrect_count,
           missing_count=excluded.missing_count,valid_from=excluded.valid_from,
           valid_until=excluded.valid_until,event_at=excluded.event_at,
           observed_at=excluded.observed_at''',
        (
            row['task'], row['skill_id'], row['name'], row['experience_id'],
            row['revision'], row['version'], event_id, row['status'],
            row['evidence_state'], row['scope'], row['title'], row['description'],
            _json(row['triggers']), _json(row['steps']), row['outcome'], row['conditions'],
            row['counterexamples'], row['body'], row['skill_path'], row['file_sha256'],
            _json(row['source']), row['helpful_count'], row['used_count'],
            row['incorrect_count'], row['missing_count'], row['valid_from'],
            row['valid_until'], row['event_at'], row['observed_at'],
        ),
    )
    return event_id


def _find_similar_skill(db, task, validated, triggers, exclude_skill_id=None):
    """Find an existing active skill in the same task with high semantic/lexical/step similarity."""
    rows = [
        _public_skill(r)
        for r in db.execute(
            "SELECT * FROM memory_skills WHERE task=? AND status='active' ORDER BY event_id DESC",
            (task,),
        ).fetchall()
    ]
    candidates = [r for r in rows if r['skill_id'] != exclude_skill_id]
    if not candidates:
        return None, None
    new_full = '\n'.join([validated['situation'], ' / '.join(validated['steps']), ' '.join(triggers)])
    new_steps_str = ' / '.join(validated['steps'])
    t_new = set(_tokens(new_full))
    s_new = set(_tokens(new_steps_str))

    best = None
    best_scores = None
    best_combined = 0.0
    for cand in candidates:
        cand_full = '\n'.join([cand['title'], cand['description'], ' / '.join(cand['steps']), ' '.join(cand['triggers'])])
        cand_steps_str = ' / '.join(cand['steps'])
        t_cand = set(_tokens(cand_full))
        s_cand = set(_tokens(cand_steps_str))
        lex_sim = len(t_new & t_cand) / max(1, len(t_new | t_cand))
        step_sim = len(s_new & s_cand) / max(1, len(s_new | s_cand))
        sem_sim = 0.0
        if lex_sim >= 0.15 or step_sim >= 0.15:
            try:
                emb = vectors.embed([
                    f"{validated['situation']}\n{new_steps_str}",
                    f"{cand['title']}\n{cand_steps_str}",
                ])
                sem_sim = float(vectors.similarity(emb[0], emb[1]))
            except Exception:
                sem_sim = 0.0
        matched = (
            (sem_sim >= CONSOLIDATION_SEMANTIC_THRESHOLD and (lex_sim >= 0.25 or step_sim >= 0.25))
            or lex_sim >= CONSOLIDATION_LEXICAL_THRESHOLD
            or step_sim >= 0.65
        )
        if matched:
            combined = max(sem_sim, lex_sim, step_sim)
            if combined > best_combined:
                best_combined = combined
                best = cand
                best_scores = {
                    'semantic': round(sem_sim, 4),
                    'lexical': round(lex_sim, 4),
                    'step': round(step_sim, 4),
                }
    return best, best_scores


def stage_skill_from_experience(db, root, task, revision, event_key, exp_item_id, op,
                                card, source, valid_from=None, valid_until=None,
                                event_at=None, skill_spec=None):
    """Validate and record a skill change in DB, returning a row to materialize."""
    ledger._identifier(task, 'task')
    ledger._identifier(event_key, 'event_key')
    ledger._identifier(exp_item_id, 'experienceId')
    skill_spec = skill_spec or {}
    if not isinstance(skill_spec, dict):
        raise ValueError('skill specification must be an object')
    if set(skill_spec) - {'id', 'name', 'title', 'description', 'triggers'}:
        raise ValueError('invalid skill specification fields')
    skill_id = skill_id_for(exp_item_id, skill_spec.get('id'), skill_spec.get('name'))
    existing = get_skill(db, task, skill_id)
    explicit_user = source.get('certainty') == 'explicit-user' or source.get('evidenceState') == 'user-confirmed'
    now = time.time()
    event_at = now if event_at is None else ledger._timestamp(event_at, 'event_at')

    if op in ('retract', 'resolve'):
        if not existing:
            return None
        if existing['evidence_state'] == 'user-confirmed' and not explicit_user:
            raise ValueError('user-confirmed skill cannot be retracted without explicit user instruction')
        if existing['status'] == 'retracted':
            return {'skill': existing, 'materialize': False, 'replayed': True}
        version = existing['version'] + 1
        status = 'retracted'
        evidence_state = 'retracted'
        validated = {
            'situation': existing['description'],
            'steps': existing['steps'],
            'outcome': existing['outcome'],
            'conditions': existing['conditions'],
            'counterexamples': existing['counterexamples'],
        }
        name = existing['name']
        title = existing['title']
        description = existing['description']
        triggers = existing['triggers']
        stats = existing['stats']
        skill_op = 'retract'
        new_source = {
            **existing['source'],
            **source,
            'skillOperation': 'retract',
            'supersedesSkillVersion': existing['version'],
            'userConfirmed': existing['evidence_state'] == 'user-confirmed',
        }
    else:
        validated = validate_substance(card, strict_counterexamples=True)
        triggers = _triggers_for(validated, source, skill_spec.get('triggers'))
        consolidated_scores = None
        consolidated_from = []
        if not existing and not skill_spec.get('id') and not skill_spec.get('name'):
            similar, sim_scores = _find_similar_skill(db, task, validated, triggers, exclude_skill_id=skill_id)
            if similar:
                if similar['evidence_state'] == 'user-confirmed' and not explicit_user:
                    return {
                        'skill': similar,
                        'materialize': False,
                        'replayed': True,
                        'consolidatedInto': similar['skill_id'],
                        'consolidationScores': sim_scores,
                        'skippedReason': 'user_confirmed_target',
                    }
                existing = similar
                skill_id = similar['skill_id']
                consolidated_scores = sim_scores
                merged_steps = list(similar['steps'])
                seen_norm = {_clean_token(s) for s in merged_steps}
                for st in validated['steps']:
                    norm_st = _clean_token(st)
                    if norm_st not in seen_norm and len(merged_steps) < 20:
                        seen_norm.add(norm_st)
                        merged_steps.append(st)
                validated['steps'] = merged_steps
                if _clean_token(validated['counterexamples']) not in _clean_token(similar['counterexamples']):
                    validated['counterexamples'] = f"{similar['counterexamples']} / {validated['counterexamples']}"[:2000]
                else:
                    validated['counterexamples'] = similar['counterexamples']
                triggers = _triggers_for(validated, source, list(similar['triggers']) + list(triggers))
                consolidated_from = sorted(set(
                    list(similar['source'].get('consolidatedFrom') or []) + [similar['experience_id'], exp_item_id]
                ))
                exp_item_id = similar['experience_id']

        if existing and existing['evidence_state'] == 'user-confirmed' and not explicit_user:
            raise ValueError('user-confirmed skill cannot be overwritten by derived claims')
        name = existing['name'] if existing else skill_name_for(
            exp_item_id, skill_spec.get('name'), skill_spec.get('title') or validated['situation']
        )
        title = ledger._text(
            skill_spec.get('title') or (existing['title'] if existing and not skill_spec.get('title') else validated['situation']),
            'title', 240, empty=False,
        )
        if existing and not skill_spec.get('title') and not consolidated_scores and existing['title'] == existing['source'].get('experience', {}).get('situation'):
            title = ledger._text(validated['situation'][:240], 'title', 240, empty=False)
        description = ledger._text(
            skill_spec.get('description') or f"{validated['situation']} (조건: {validated['conditions'][:240]})",
            'description', 1000, empty=False,
        )
        if explicit_user:
            evidence_state = 'user-confirmed'
        elif existing and existing['evidence_state'] == 'user-confirmed':
            evidence_state = 'user-confirmed'
        elif existing and existing['evidence_state'] in ('feedback-verified', 'trial-used') and op == 'assert':
            evidence_state = existing['evidence_state']
        else:
            evidence_state = source.get('evidenceState') or 'reported'
        status = 'active'
        stats = existing['stats'] if existing else {'used': 0, 'helpful': 0, 'incorrect': 0, 'missing': 0}

        if existing and existing['status'] == 'active':
            same_substance = (
                existing['steps'] == validated['steps']
                and existing['outcome'] == validated['outcome']
                and existing['conditions'] == validated['conditions']
                and existing['counterexamples'] == validated['counterexamples']
                and existing['description'] == description
                and existing['scope'] == source.get('scope', existing['scope'])
                and existing['evidence_state'] == evidence_state
                and existing['valid_from'] == valid_from
                and existing['valid_until'] == valid_until
                and not consolidated_scores
            )
            if same_substance:
                return {'skill': existing, 'materialize': False, 'replayed': True}

        version = (existing['version'] + 1) if existing else 1
        if consolidated_scores:
            skill_op = 'consolidate'
        else:
            skill_op = 'improve' if existing else 'create'
        created_by = (
            existing['source'].get('createdBySession') or existing['source'].get('session', '')
            if existing else source.get('session', '')
        )
        merged_refs = list(source.get('sourceRefs', []))
        if existing and consolidated_scores:
            seen_ref_keys = {(r.get('itemId'), r.get('eventId')) for r in merged_refs}
            for r in existing['source'].get('sourceRefs', []):
                key = (r.get('itemId'), r.get('eventId'))
                if key not in seen_ref_keys:
                    seen_ref_keys.add(key)
                    merged_refs.append(r)
        ref_files = _extract_referenced_files(validated, root=root, explicit_files=source.get('referencedFiles'))
        if not ref_files and existing:
            ref_files = list(existing['source'].get('referencedFiles') or [])
        new_source = {
            **(existing['source'] if existing else {}),
            **source,
            'sourceRefs': merged_refs,
            'experience': validated,
            'evidenceState': evidence_state,
            'skillOperation': skill_op,
            'userConfirmed': evidence_state == 'user-confirmed',
            'supersedesSkillVersion': existing['version'] if existing else 0,
            'createdBySession': created_by,
            'referencedFiles': ref_files,
            'gitBranch': source.get('gitBranch') or (existing['source'].get('gitBranch') if existing else None) or detect_git_branch(root),
        }
        if consolidated_from:
            new_source['consolidatedFrom'] = consolidated_from
        if consolidated_scores:
            new_source['consolidationScores'] = consolidated_scores

    _, _, _, _, md_path, _ = skill_paths(root, task, name)
    body = render_skill_markdown(
        name=name,
        skill_id=skill_id,
        experience_id=exp_item_id,
        task=task,
        scope=new_source.get('scope', existing['scope'] if existing else 'task'),
        version=version,
        status=status,
        evidence_state=evidence_state,
        title=title,
        description=description,
        triggers=triggers,
        card=validated,
        source=new_source,
        stats=stats,
    )
    row = {
        'task': task,
        'revision': revision,
        'event_key': event_key,
        'skill_id': skill_id,
        'name': name,
        'experience_id': exp_item_id,
        'version': version,
        'operation': skill_op,
        'status': status,
        'evidence_state': evidence_state,
        'scope': new_source.get('scope', existing['scope'] if existing else 'task'),
        'title': title,
        'description': description,
        'triggers': triggers,
        'steps': validated['steps'],
        'outcome': validated['outcome'],
        'conditions': validated['conditions'],
        'counterexamples': validated['counterexamples'],
        'body': body,
        'skill_path': str(md_path),
        'file_sha256': _sha256_text(body),
        'source': new_source,
        'prior_version': existing['version'] if existing else 0,
        'prior_status': existing['status'] if existing else '',
        'prior_evidence_state': existing['evidence_state'] if existing else '',
        'helpful_count': stats['helpful'],
        'used_count': stats['used'],
        'incorrect_count': stats['incorrect'],
        'missing_count': stats['missing'],
        'valid_from': valid_from,
        'valid_until': valid_until,
        'event_at': event_at,
        'observed_at': now,
    }
    _write_skill_event_and_row(db, row)
    res = {'skill': _public_skill(row), 'materialize': True, 'replayed': False}
    if op not in ('retract', 'resolve') and consolidated_scores:
        res['consolidatedInto'] = skill_id
        res['consolidatedFrom'] = new_source.get('consolidatedFrom', [])
        res['consolidationScores'] = consolidated_scores
    return res


def apply_feedback(db, root, task, revision, session, operation, recall_id, item_id, outcome, note='', patch=None):
    """Update a procedural skill based on grounded recall feedback."""
    skill = get_skill(db, task, item_id) if item_id else None
    if not skill and patch and isinstance(patch, dict) and patch.get('skillId'):
        skill = get_skill(db, task, patch['skillId'])
    if not skill:
        return None
    if skill['status'] == 'retracted':
        return {'skillId': skill['skill_id'], 'status': 'retracted', 'version': skill['version'], 'versionChanged': False}

    now = time.time()
    used_count = skill['used_count'] + (1 if outcome == 'used' else 0)
    helpful_count = skill['helpful_count'] + (1 if outcome == 'helpful' else 0)
    incorrect_count = skill['incorrect_count'] + (1 if outcome == 'incorrect' else 0)
    missing_count = skill['missing_count'] + (1 if outcome == 'missing' else 0)
    stats = {'used': used_count, 'helpful': helpful_count, 'incorrect': incorrect_count, 'missing': missing_count}

    card = {
        'situation': skill['source'].get('experience', {}).get('situation') or skill['title'],
        'steps': list(skill['steps']),
        'outcome': skill['outcome'],
        'conditions': skill['conditions'],
        'counterexamples': skill['counterexamples'],
    }
    status = skill['status']
    evidence_state = skill['evidence_state']
    version_changed = False
    skill_op = None
    prior_recalls = set(skill['source'].get('verifiedRecallIds') or [])

    is_compatible_unverified = (
        skill['source'].get('verificationStatus') == 'compatible-unverified'
        or skill.get('staleness', {}).get('verificationStatus') == 'compatible-unverified'
    )
    if outcome in ('used', 'helpful'):
        if recall_id:
            prior_recalls.add(recall_id)
        positive_total = used_count + helpful_count
        creator_session = str(skill['source'].get('createdBySession') or skill['source'].get('session') or '')
        cross_session = bool(session and creator_session and session != creator_session)
        if status == 'active' and evidence_state in ('reported', 'trial-used'):
            if is_compatible_unverified and outcome != 'helpful':
                if evidence_state == 'reported':
                    evidence_state = 'trial-used'
                    version_changed = True
                    skill_op = 'promote_trial'
            elif positive_total >= 2 or cross_session or len(prior_recalls) >= 2 or (is_compatible_unverified and outcome == 'helpful'):
                evidence_state = 'feedback-verified'
                version_changed = True
                skill_op = 'promote'
            elif evidence_state == 'reported':
                evidence_state = 'trial-used'
                version_changed = True
                skill_op = 'promote_trial'
    elif outcome == 'incorrect':
        if skill['evidence_state'] == 'user-confirmed' and not (isinstance(patch, dict) and patch.get('userConfirmed')):
            raise ValueError('user-confirmed skill requires explicit user correction to override or degrade')
        if note and note.strip() and note.strip() not in card['counterexamples']:
            addition = f"주의·실패 사례({operation}): {note.strip()}"
            card['counterexamples'] = (card['counterexamples'] + ' / ' + addition).strip(' /')[:2000]
        if status != 'needs_revision' or evidence_state != 'needs_revision' or (note and note.strip()):
            status = 'needs_revision'
            evidence_state = 'needs_revision'
            version_changed = True
            skill_op = 'degrade'
    elif outcome == 'missing':
        if isinstance(patch, dict) and set(patch) & {'situation', 'steps', 'outcome', 'conditions', 'counterexamples'}:
            if skill['evidence_state'] == 'user-confirmed' and not patch.get('userConfirmed'):
                raise ValueError('user-confirmed skill cannot be modified without explicit user confirmation')
            merged = {**card, **{k: patch[k] for k in ('situation', 'steps', 'outcome', 'conditions', 'counterexamples') if k in patch}}
            card = validate_substance(merged, strict_counterexamples=True)
            if status == 'needs_revision':
                status = 'active'
                evidence_state = 'user-confirmed' if skill['evidence_state'] == 'user-confirmed' else 'feedback-verified'
            version_changed = True
            skill_op = 'improve'
        elif note and note.strip() and note.strip() not in card['counterexamples']:
            if skill['evidence_state'] == 'user-confirmed':
                raise ValueError('user-confirmed skill cannot be modified without explicit user confirmation')
            addition = f"보완 조건({operation}): {note.strip()}"
            card['counterexamples'] = (card['counterexamples'] + ' / ' + addition).strip(' /')[:2000]
            version_changed = True
            skill_op = 'improve'

    ref_files = _extract_referenced_files(card, root=root, explicit_files=skill['source'].get('referencedFiles'))
    if not ref_files:
        ref_files = list(skill['source'].get('referencedFiles') or [])
    version = skill['version'] + (1 if version_changed else 0)
    code_anchor_digest = _digest([
        (r.get('path'), r.get('sha256'), r.get('symbols'))
        for r in (ref_files or [])
        if isinstance(r, dict)
    ])
    ver_status = skill['source'].get('verificationStatus') or 'verified'
    ver_evidence = skill['source'].get('verificationEvidence')
    if outcome == 'helpful' and status == 'active' and evidence_state in ('feedback-verified', 'user-confirmed'):
        ver_status = 'verified'
        ver_evidence = {
            'verifiedAt': now,
            'verifiedBySession': session,
            'skillVersion': version,
            'codeAnchorDigest': code_anchor_digest,
            'mode': 'explicit-helpful-feedback',
            'recallId': recall_id,
            'operation': operation,
        }
    elif evidence_state == 'feedback-verified' and not ver_evidence and ver_status != 'compatible-unverified':
        ver_evidence = {
            'verifiedAt': now,
            'verifiedBySession': session,
            'skillVersion': version,
            'codeAnchorDigest': code_anchor_digest,
            'mode': 'multi-feedback-promotion',
            'recallId': recall_id,
            'operation': operation,
        }
    elif outcome == 'incorrect':
        ver_status = 'broken'

    new_source = {
        **skill['source'],
        'experience': card,
        'evidenceState': evidence_state,
        'verificationStatus': ver_status,
        'verifiedRecallIds': sorted(prior_recalls)[:20],
        'referencedFiles': ref_files,
        'lastFeedback': {
            'operation': operation,
            'recallId': recall_id,
            'outcome': outcome,
            'note': note,
            'session': session,
            'at': now,
        },
    }
    if ver_evidence:
        new_source['verificationEvidence'] = ver_evidence
    body = render_skill_markdown(
        name=skill['name'],
        skill_id=skill['skill_id'],
        experience_id=skill['experience_id'],
        task=task,
        scope=skill['scope'],
        version=version,
        status=status,
        evidence_state=evidence_state,
        title=skill['title'],
        description=skill['description'],
        triggers=skill['triggers'],
        card=card,
        source=new_source,
        stats=stats,
    )
    file_sha256 = _sha256_text(body)

    if version_changed:
        row = {
            'task': task,
            'revision': revision,
            'event_key': 'skill-fb:' + _digest([operation, skill['skill_id']]),
            'skill_id': skill['skill_id'],
            'name': skill['name'],
            'experience_id': skill['experience_id'],
            'version': version,
            'operation': skill_op,
            'status': status,
            'evidence_state': evidence_state,
            'scope': skill['scope'],
            'title': skill['title'],
            'description': skill['description'],
            'triggers': skill['triggers'],
            'steps': card['steps'],
            'outcome': card['outcome'],
            'conditions': card['conditions'],
            'counterexamples': card['counterexamples'],
            'body': body,
            'skill_path': skill['skill_path'],
            'file_sha256': file_sha256,
            'source': new_source,
            'prior_version': skill['version'],
            'prior_status': skill['status'],
            'prior_evidence_state': skill['evidence_state'],
            'helpful_count': helpful_count,
            'used_count': used_count,
            'incorrect_count': incorrect_count,
            'missing_count': missing_count,
            'valid_from': skill['valid_from'],
            'valid_until': skill['valid_until'],
            'event_at': now,
            'observed_at': now,
        }
        _write_skill_event_and_row(db, row)
        exp_item = ledger.get_item(db, task, skill['experience_id'])
        if exp_item and exp_item['status'] == 'active':
            exp_source = {
                k: v for k, v in exp_item['source'].items()
                if k not in ('contentSha256', 'contentChars', 'observedAt')
            }
            exp_source.update({
                'experience': card,
                'evidenceState': evidence_state,
                'skillVersion': version,
                'skillStatus': status,
            })
            exp_content = '\n'.join([
                card['situation'],
                ' / '.join(card['steps']),
                card['outcome'],
                '조건: ' + card['conditions'],
                '예외: ' + card['counterexamples'],
            ])
            ledger.record(
                db, task, revision, 'exp-fb:' + _digest([operation, skill['experience_id']]),
                'experience', skill['experience_id'], 'replace', exp_content, exp_source,
                event_at=now, valid_from=exp_item.get('valid_from'), valid_until=exp_item.get('valid_until'),
            )
        updated = _public_skill(row)
    else:
        db.execute(
            '''UPDATE memory_skills
               SET helpful_count=?,used_count=?,incorrect_count=?,missing_count=?,
                   body=?,file_sha256=?,source=?,observed_at=?
               WHERE task=? AND skill_id=?''',
            (
                helpful_count, used_count, incorrect_count, missing_count,
                body, file_sha256, _json(new_source), now, task, skill['skill_id'],
            ),
        )
        updated = get_skill(db, task, skill['skill_id'])

    events = skill_events(db, task, skill['skill_id'], limit=30)
    materialize_skill(root, updated, events)
    synthesized_case_id = None
    if outcome == 'incorrect' and note and note.strip():
        synthesized_case_id = 'case:pitfall-' + skill['name']
        avoidance_rules = [note.strip()]
        if card.get('counterexamples') and card['counterexamples'] not in avoidance_rules:
            avoidance_rules.append(card['counterexamples'][:600])
        case_card = {
            'title': f"{skill['title']} 실패·함정 주의보",
            'situation': card['situation'],
            'symptom': note.strip()[:2000],
            'rootCause': f"절차 스킬({skill['skill_id']}) 실행 중 오류 피드백 접수",
            'avoidance': avoidance_rules[:10],
            'conditions': card['conditions'],
            'severity': 'warning',
        }
        case_content = format_case_content(case_card)
        old_case = ledger.get_item(db, task, synthesized_case_id)
        case_op = 'replace' if old_case and old_case['status'] == 'active' else 'assert'
        case_source = {
            'kind': 'feedback-pitfall-synthesis',
            'session': session,
            'operation': operation,
            'recallId': recall_id,
            'certainty': 'derived',
            'evidenceState': 'feedback-verified',
            'scope': skill['scope'],
            'linkedSkillId': skill['skill_id'],
            'linkedExperienceId': skill['experience_id'],
            'links': [skill['skill_id'], skill['experience_id']],
            'referencedFiles': ref_files,
            'entities': list(skill['source'].get('entities', [])),
            'aliases': list(skill['source'].get('aliases', [])),
            'sourceRefs': list(skill['source'].get('sourceRefs', [])),
            'case': case_card,
        }
        ledger.record(
            db, task, revision, 'case-fb:' + _digest([operation, synthesized_case_id]),
            'case', synthesized_case_id, case_op, case_content, case_source, event_at=now,
        )
    sync_task_links(db, task)
    res = {
        'skillId': updated['skill_id'],
        'name': updated['name'],
        'version': updated['version'],
        'status': updated['status'],
        'evidenceState': updated['evidence_state'],
        'versionChanged': version_changed,
        'operation': skill_op,
        'skillPath': updated['skill_path'],
        'fileSha256': updated['file_sha256'],
        'stats': updated['stats'],
        'stale': updated['stale'],
        'staleness': updated['staleness'],
    }
    if synthesized_case_id:
        res['synthesizedCaseId'] = synthesized_case_id
    return res


def sync_task_links(db, task, force=False):
    """Build deterministic bidirectional [[links]] across task memory items, cases, and skills with O(1) skip check and O(N*k) inverted bucket construction."""
    ledger._identifier(task, 'task')
    ensure_schema(db)
    if not force:
        needed, cached_res, state_sig = _indexing_core.check_link_sync_needed(db, task)
        if not needed and cached_res is not None:
            return cached_res
    else:
        _, _, state_sig = _indexing_core.check_link_sync_needed(db, task)
    now = time.time()
    items = ledger.current(
        db, task,
        kinds=['decision', 'constraint', 'fact', 'experience', 'case', 'issue', 'hypothesis'],
    )
    skills = [
        _public_skill(r)
        for r in db.execute(
            "SELECT * FROM memory_skills WHERE task=? AND status='active' ORDER BY event_id DESC",
            (task,),
        ).fetchall()
    ]
    nodes = {}
    for item in items:
        nid = item['item_id']
        src = item.get('source') or {}
        files = {
            Path(f['path']).name.lower()
            for f in (src.get('referencedFiles') or [])
            if isinstance(f, dict) and f.get('path')
        }
        for raw_f in (src.get('files') or []):
            if isinstance(raw_f, str) and raw_f.strip():
                files.add(Path(raw_f.strip()).name.lower())
        for m in re.finditer(r'\b([A-Za-z0-9_.-]+\.(?:py|json|md|sh|js|ts))\b', item.get('content', '')):
            fn = m.group(1).lower()
            if fn not in ('skill.md', 'provenance.json'):
                files.add(fn)
        entities = {
            _clean_token(lbl)
            for k in ('entities', 'aliases')
            for lbl in (src.get(k) or [])
            if _clean_token(lbl) and len(_clean_token(lbl)) >= 2
        }
        source_refs = {
            r.get('itemId')
            for r in (src.get('sourceRefs') or [])
            if isinstance(r, dict) and r.get('itemId')
        }
        explicit_targets = set(extract_wiki_links(item.get('content', '')))
        for lnk in (src.get('links') or []):
            if isinstance(lnk, str) and lnk.strip():
                explicit_targets.add(lnk.strip().strip('[]'))
        if src.get('skillId'):
            explicit_targets.add(src['skillId'])
        if src.get('linkedSkillId'):
            explicit_targets.add(src['linkedSkillId'])
        if src.get('linkedExperienceId'):
            explicit_targets.add(src['linkedExperienceId'])
        supersedes_targets = set()
        for sup_id in (src.get('supersedesItemIds') or []):
            if isinstance(sup_id, str) and sup_id.strip():
                supersedes_targets.add(sup_id.strip())
        if isinstance(src.get('supersededBy'), str) and src['supersededBy'].strip():
            supersedes_targets.add(src['supersededBy'].strip())
        typed_relations = []
        raw_rels = src.get('relations')
        if isinstance(raw_rels, list):
            for rel_entry in raw_rels:
                if isinstance(rel_entry, dict) and rel_entry.get('target') and rel_entry.get('relation'):
                    typed_relations.append((str(rel_entry['target']).strip(), str(rel_entry['relation']).strip()))
        elif isinstance(raw_rels, dict):
            for rel_name, tgt_list in raw_rels.items():
                if isinstance(tgt_list, list):
                    for t_val in tgt_list:
                        if isinstance(t_val, str) and t_val.strip():
                            typed_relations.append((t_val.strip(), str(rel_name).strip()))
        if isinstance(src.get('consolidatedInto'), str) and src['consolidatedInto'].strip():
            typed_relations.append((src['consolidatedInto'].strip(), 'consolidated_into'))
        for cf_id in (src.get('consolidatedFrom') or []):
            if isinstance(cf_id, str) and cf_id.strip():
                typed_relations.append((cf_id.strip(), 'consolidated_into'))
        content_str = item.get('content', '')
        for m_typed in re.finditer(r'\[\[([^\[\]|#]+?)\|(depends_on|blocks|mitigates|verifies|implements|supersedes|consolidated_into)\]\]', content_str, re.IGNORECASE):
            typed_relations.append((m_typed.group(1).strip(), m_typed.group(2).strip().lower()))
        for m_prefix in re.finditer(r'\[\[(depends_on|blocks|mitigates|verifies|implements|supersedes|consolidated_into):([^\[\]|#]+?)(?:\|[^\[\]]*)?\]\]', content_str, re.IGNORECASE):
            typed_relations.append((m_prefix.group(2).strip(), m_prefix.group(1).strip().lower()))
        for m_ctx in re.finditer(r'(선행|의존|depends\s+on|차단|blocks|방지|완화|mitigates|검증|verifies|구현|implements)[^\n\[\]]{0,24}\[\[(?:(?:depends_on|blocks|mitigates|verifies|implements|supersedes|consolidated_into):)?([^\[\]|#]+?)\]\]', content_str, re.IGNORECASE):
            kw = m_ctx.group(1).lower()
            tgt = m_ctx.group(2).strip()
            rel_kind = (
                'depends_on' if ('선행' in kw or '의존' in kw or 'depends' in kw)
                else 'blocks' if ('차단' in kw or 'block' in kw)
                else 'mitigates' if ('방지' in kw or '완화' in kw or 'mitigate' in kw)
                else 'verifies' if ('검증' in kw or 'verif' in kw)
                else 'implements'
            )
            typed_relations.append((tgt, rel_kind))
        box_evo = src.get('boxEvolution') if isinstance(src.get('boxEvolution'), dict) else {}
        for kw in (box_evo.get('contextKeywords') or []):
            ck = _clean_token(kw)
            if ck and len(ck) >= 2:
                entities.add(ck)
        nodes[nid] = {
            'id': nid,
            'kind': item['kind'],
            'files': files,
            'entities': entities,
            'sourceRefs': source_refs,
            'explicit': explicit_targets,
            'supersedes': supersedes_targets,
            'typedRelations': typed_relations,
        }
    for sk in skills:
        nid = sk['skill_id']
        src = sk.get('source') or {}
        files = {
            Path(f['path']).name.lower()
            for f in (src.get('referencedFiles') or [])
            if isinstance(f, dict) and f.get('path')
        }
        text_blob = '\n'.join([sk['title'], sk['conditions'], sk['counterexamples']] + list(sk['steps']))
        for m in re.finditer(r'\b([A-Za-z0-9_.-]+\.(?:py|json|md|sh|js|ts))\b', text_blob):
            fn = m.group(1).lower()
            if fn not in ('skill.md', 'provenance.json'):
                files.add(fn)
        entities = {
            _clean_token(lbl)
            for lbl in (list(sk.get('triggers') or []) + list(src.get('entities') or []) + list(src.get('aliases') or []))
            if _clean_token(lbl) and len(_clean_token(lbl)) >= 2
        }
        source_refs = {
            r.get('itemId')
            for r in (src.get('sourceRefs') or [])
            if isinstance(r, dict) and r.get('itemId')
        }
        explicit_targets = set(extract_wiki_links(text_blob))
        if sk.get('experience_id'):
            explicit_targets.add(sk['experience_id'])
        typed_relations = []
        for c_id in (src.get('consolidatedFrom') or []):
            if isinstance(c_id, str) and c_id:
                explicit_targets.add(c_id)
                typed_relations.append((c_id, 'consolidated_into'))
        nodes[nid] = {
            'id': nid,
            'kind': 'skill',
            'files': files,
            'entities': entities,
            'sourceRefs': source_refs,
            'explicit': explicit_targets,
            'supersedes': set(),
            'typedRelations': typed_relations,
        }

    edges = {}

    def add_pair(a, b, relation, weight, evidence_val):
        if not a or not b or a == b:
            return
        for u, v in ((a, b), (b, a)):
            key = (u, v, relation)
            if key not in edges or edges[key][0] < weight:
                edges[key] = (weight, evidence_val)

    node_ids = sorted(nodes)
    for nid in node_ids:
        node = nodes[nid]
        for target in sorted(node['explicit']):
            if target != nid and (target in nodes or target.startswith(('skill:', 'experience:', 'case:', 'fact:', 'decision:', 'constraint:', 'issue:', 'field:', 'hypothesis:'))):
                rel = 'pitfall_of' if (node['kind'] == 'case' or target.startswith('case:')) else (
                    'skill_experience' if ({node['kind'], nodes.get(target, {}).get('kind')} == {'skill', 'experience'}) else 'explicit_link'
                )
                w = 0.95 if rel in ('pitfall_of', 'skill_experience') else 1.0
                add_pair(nid, target, rel, w, f'[[{target}]]')
        for sup_target in sorted(node.get('supersedes') or ()):
            if sup_target != nid:
                add_pair(nid, sup_target, 'supersedes', 0.90, f'supersedes:{sup_target}')
        for rel_target, rel_kind in (node.get('typedRelations') or ()):
            if rel_target != nid and rel_kind in ('depends_on', 'blocks', 'mitigates', 'verifies', 'implements', 'consolidated_into', 'supersedes'):
                add_pair(nid, rel_target, rel_kind, 0.92, f'{rel_kind}:{rel_target}')

    _indexing_core.build_shared_edges_inverted(nodes, node_ids, add_pair)

    db.execute('DELETE FROM memory_links WHERE task=?', (task,))
    if edges:
        db.executemany(
            'INSERT OR REPLACE INTO memory_links VALUES(?,?,?,?,?,?,?)',
            [
                (task, u, v, relation, float(weight), str(evidence_val)[:500], now)
                for (u, v, relation), (weight, evidence_val) in sorted(edges.items())
            ],
        )
    _invalidate_task_link_cache(task)
    res_obj = {'taskId': task, 'directedLinks': len(edges), 'undirectedPairs': len({tuple(sorted((u, v))) for u, v, _ in edges})}
    _indexing_core.record_link_sync_state(db, task, state_sig, res_obj)
    return res_obj


_TASK_LINK_CACHE = {}
_MAX_TASK_LINK_CACHE = 32


def _db_identity_key(db):
    return _indexing_core.db_identity_key(db)


def _invalidate_task_link_cache(task=None):
    _indexing_core.invalidate_task_caches(task)
    if task is None:
        _TASK_LINK_CACHE.clear()
        return
    _TASK_LINK_CACHE.pop(task, None)
    for k in [k for k in _TASK_LINK_CACHE if isinstance(k, tuple) and len(k) > 1 and k[1] == task]:
        _TASK_LINK_CACHE.pop(k, None)


def _load_task_link_graph(db, task):
    """Load and cache adjacency list, out-degrees, and sorted [[wiki_links]] per node for a task in a single SQL query."""
    now_m = time.monotonic()
    fast_cached = _TASK_LINK_CACHE.get(task)
    if fast_cached is not None and fast_cached.get('_db_id') == id(db) and (now_m - fast_cached.get('_ts', -10.0) < 0.8):
        return fast_cached
    ensure_schema(db)
    stat = db.execute(
        'SELECT count(*), COALESCE(max(updated_at), 0.0) FROM memory_links WHERE task=?',
        (task,),
    ).fetchone()
    cnt = int(stat[0] or 0)
    max_upd = round(float(stat[1] or 0.0), 4)
    if cnt == 0:
        empty_graph = {'adj': {}, 'out_deg': {}, 'wiki_links': {}, '_db_id': id(db), '_ts': now_m}
        _TASK_LINK_CACHE[task] = empty_graph
        return empty_graph
    ckey = (_db_identity_key(db), task, cnt, max_upd)
    cached = _TASK_LINK_CACHE.get(ckey)
    if cached is not None:
        cached['_db_id'] = id(db)
        cached['_ts'] = now_m
        _TASK_LINK_CACHE[task] = cached
        return cached

    edge_rows = db.execute(
        'SELECT source_id, target_id, relation, weight FROM memory_links WHERE task=? ORDER BY weight DESC, target_id ASC',
        (task,),
    ).fetchall()
    adj = {}
    out_neighbors = {}
    max_w_by_src = {}
    for r in edge_rows:
        u = r['source_id']
        v = r['target_id']
        rel = r['relation']
        w = float(r['weight'])
        adj.setdefault(u, []).append((v, rel, w))
        out_neighbors.setdefault(u, set()).add(v)
        src_map = max_w_by_src.setdefault(u, {})
        if w > src_map.get(v, -1.0):
            src_map[v] = w

    out_deg = {u: max(1, len(nbrs)) for u, nbrs in out_neighbors.items()}
    wiki_links = {}
    for u, tgt_map in max_w_by_src.items():
        ordered = sorted(tgt_map.items(), key=lambda kv: (-kv[1], kv[0]))
        wiki_links[u] = [f"[[{t_id}]]" for t_id, _ in ordered]

    graph = {'adj': adj, 'out_deg': out_deg, 'wiki_links': wiki_links, '_db_id': id(db), '_ts': now_m}
    if len(_TASK_LINK_CACHE) >= _MAX_TASK_LINK_CACHE:
        _TASK_LINK_CACHE.pop(next(iter(_TASK_LINK_CACHE)), None)
    _TASK_LINK_CACHE[ckey] = graph
    _TASK_LINK_CACHE[task] = graph
    return graph


_PPR_REL_MULTIPLIER = {
    'pitfall_of': 1.25,
    'mitigates': 1.22,
    'skill_experience': 1.20,
    'explicit_link': 1.20,
    'depends_on': 1.18,
    'blocks': 1.18,
    'implements': 1.16,
    'verifies': 1.15,
    'supersedes': 1.15,
    'consolidated_into': 1.15,
    'shared_source': 0.95,
    'shared_file': 0.82,
    'shared_entity': 0.78,
}


def ppr_propagate(db, task, seed_scores, max_hops=2, alpha=0.55):
    """2-Hop Hub-Penalized Personalized PageRank over in-memory cached memory_links."""
    if not seed_scores:
        return {}, {}
    graph = _load_task_link_graph(db, task)
    adj = graph['adj']
    if not adj:
        return {}, {}
    out_deg = graph['out_deg']

    def _edge_factor(u_id, rel, w):
        d = out_deg.get(u_id, 1)
        if rel in ('pitfall_of', 'skill_experience', 'explicit_link', 'supersedes'):
            hub_pen = 1.0 / math.pow(d, 0.22)
        else:
            hub_pen = 1.0 / math.sqrt(d)
        return w * _PPR_REL_MULTIPLIER.get(rel, 0.85) * hub_pen

    hop1_scores = {}
    provenance = {}
    ordered_seeds = sorted(
        ((sid, float(sc)) for sid, sc in seed_scores.items() if float(sc) > 0),
        key=lambda kv: -kv[1],
    )[:8]

    for seed_id, seed_sc in ordered_seeds:
        base_sc = max(seed_sc, 0.016)
        for tgt_id, rel, w in adj.get(seed_id, []):
            if tgt_id == seed_id:
                continue
            s1 = round(base_sc * alpha * _edge_factor(seed_id, rel, w), 8)
            if s1 > hop1_scores.get(tgt_id, 0.0):
                hop1_scores[tgt_id] = s1
                provenance[tgt_id] = {
                    'fromId': seed_id,
                    'from': seed_id,
                    'relation': rel,
                    'weight': round(w, 4),
                    'hop': 1,
                    'path': [seed_id, tgt_id],
                    'pprScore': round(s1, 6),
                }

    hop2_scores = {}
    if max_hops >= 2 and hop1_scores:
        ordered_hop1 = sorted(hop1_scores.items(), key=lambda kv: -kv[1])[:10]
        for mid_id, mid_sc in ordered_hop1:
            prov1 = provenance[mid_id]
            origin_id = prov1['fromId']
            for tgt2_id, rel2, w2 in adj.get(mid_id, []):
                if tgt2_id in (mid_id, origin_id) or tgt2_id in seed_scores:
                    continue
                s2 = round(mid_sc * (alpha * 0.68) * _edge_factor(mid_id, rel2, w2), 8)
                if s2 > hop1_scores.get(tgt2_id, 0.0) and s2 > hop2_scores.get(tgt2_id, 0.0):
                    hop2_scores[tgt2_id] = s2
                    provenance[tgt2_id] = {
                        'fromId': origin_id,
                        'from': origin_id,
                        'viaId': mid_id,
                        'relation': f"{prov1['relation']}->{rel2}",
                        'weight': round(prov1['weight'] * w2, 4),
                        'hop': 2,
                        'path': [origin_id, mid_id, tgt2_id],
                        'pprScore': round(s2, 6),
                    }

    combined = dict(hop2_scores)
    for k, v in hop1_scores.items():
        if v >= combined.get(k, 0.0):
            combined[k] = v
    return combined, provenance


def node_wiki_links(db, task, node_id, limit=8):
    """Return formatted [[target_id]] links for a given item/skill/case node from in-memory link graph cache."""
    graph = _load_task_link_graph(db, task)
    return list((graph['wiki_links'].get(node_id) or [])[:limit])


def get_links(db, task, node_id=None, limit=100):
    """Query bidirectional memory links for a task or a specific node."""
    ledger._identifier(task, 'task')
    limit = ledger._integer(limit, 'limit', 1, 500)
    ensure_schema(db)
    if node_id:
        ledger._identifier(node_id, 'nodeId')
        rows = db.execute(
            '''SELECT source_id, target_id, relation, weight, evidence, updated_at
               FROM memory_links WHERE task=? AND source_id=?
               ORDER BY weight DESC, target_id ASC LIMIT ?''',
            (task, node_id, limit),
        ).fetchall()
    else:
        rows = db.execute(
            '''SELECT source_id, target_id, relation, weight, evidence, updated_at
               FROM memory_links WHERE task=?
               ORDER BY weight DESC, source_id ASC, target_id ASC LIMIT ?''',
            (task, limit),
        ).fetchall()
    return [
        {
            'sourceId': r['source_id'],
            'targetId': r['target_id'],
            'wikiLink': f"[[{r['target_id']}]]",
            'relation': r['relation'],
            'weight': r['weight'],
            'evidence': r['evidence'],
            'updatedAt': r['updated_at'],
        }
        for r in rows
    ]


_ARCHETYPE_PATH_RE = re.compile(r'/(?:Users|home|var|private|tmp|opt|etc)/[^\s`\'"<>():;,]+')
_ARCHETYPE_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
_ARCHETYPE_UUID_RE = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE)
_ARCHETYPE_HEX_RE = re.compile(r'\b[0-9a-f]{24,64}\b', re.IGNORECASE)
_ARCHETYPE_SECRET_RE = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._-]+)\b')


def sanitize_archetype_text(text):
    """AFTER (arXiv:2606.23127) Privacy-Safe Archetype Sanitizer: strip absolute paths, emails, session UUIDs, raw hashes, and tokens while preserving file basenames and AST symbols."""
    if not isinstance(text, str) or not text:
        return ''

    def _replace_path(m):
        raw = m.group(0).rstrip('.,:;!?)')
        if '::' in raw:
            p_part, sym_part = raw.split('::', 1)
            return f"<project>/{Path(p_part).name}::{sym_part}"
        return f"<project>/{Path(raw).name}"

    out = _ARCHETYPE_SECRET_RE.sub('<redacted-credential>', text)
    out = _ARCHETYPE_EMAIL_RE.sub('<redacted-email>', out)
    out = _ARCHETYPE_PATH_RE.sub(_replace_path, out)
    out = _ARCHETYPE_UUID_RE.sub('<session-id>', out)
    out = _ARCHETYPE_HEX_RE.sub('<hash>', out)
    return out


def retrieve_federated_archetypes(db, current_task, query='', limit=2):
    """AFTER (arXiv:2606.23127) & sylex-memory Cross-Project Federated Archetype Transfer: recall verified procedural skills and cases across projects in WorkStore with privacy sanitization."""
    ensure_schema(db)
    cur_proj_row = db.execute('SELECT project FROM tasks WHERE id=?', (current_task,)).fetchone()
    cur_project = cur_proj_row['project'] if cur_proj_row else ''

    # Gather verified active skills from other projects (or other tasks when project differs)
    skill_rows = [
        _public_skill(r)
        for r in db.execute(
            '''SELECT s.* FROM memory_skills s
               LEFT JOIN tasks t ON t.id = s.task
               WHERE s.task <> ?
                 AND (? = '' OR COALESCE(t.project, '') <> ?)
                 AND s.status = 'active'
                 AND s.evidence_state IN ('feedback-verified', 'user-confirmed')
                 AND s.incorrect_count = 0
               ORDER BY s.helpful_count DESC, s.used_count DESC, s.event_at DESC
               LIMIT 50''',
            (current_task, cur_project, cur_project),
        ).fetchall()
    ]
    case_rows = [
        ledger._public(r)
        for r in db.execute(
            '''SELECT m.* FROM memory_items m
               LEFT JOIN tasks t ON t.id = m.task
               WHERE m.task <> ?
                 AND (? = '' OR COALESCE(t.project, '') <> ?)
                 AND m.kind = 'case'
                 AND m.status = 'active'
               ORDER BY m.event_at DESC
               LIMIT 50''',
            (current_task, cur_project, cur_project),
        ).fetchall()
    ]

    qwords = set(_tokens(query)[:128]) if query else set()
    normalized = ledger._normalize(query).strip() if query else ''

    sanitized_skills = []
    seen_names = set()
    for sk in skill_rows:
        if sk.get('stale') or sk['name'] in seen_names:
            continue
        seen_names.add(sk['name'])
        blob = '\n'.join([sk['title'], sk['description'], ' '.join(sk['triggers']), ' / '.join(sk['steps']), sk['conditions']])
        counts_i, _ = _token_counts(blob, qwords)
        overlap = sum(counts_i.values())
        if normalized and overlap == 0:
            norm_blob = ledger._normalize(blob)
            overlap = sum(1 for w in qwords if len(w) >= 2 and w in norm_blob)
        if normalized and overlap == 0:
            continue
        san_steps = [sanitize_archetype_text(s) for s in sk['steps']]
        san_cond = sanitize_archetype_text(sk['conditions'])
        san_ce = sanitize_archetype_text(sk['counterexamples'])
        san_out = sanitize_archetype_text(sk['outcome'])
        san_title = sanitize_archetype_text(sk['title'])
        san_desc = sanitize_archetype_text(sk['description'])
        cp = dict(sk)
        cp.update({
            'title': san_title,
            'description': san_desc,
            'steps': san_steps,
            'conditions': san_cond,
            'counterexamples': san_ce,
            'outcome': san_out,
            'sharedFromTask': sk['task'],
            'sharedMode': 'federated-archetype',
            'federatedScore': round(0.85 * (0.02 + 0.005 * min(10, overlap)), 6),
            'source': {
                **{k: v for k, v in (sk.get('source') or {}).items() if k not in ('sourceRefs', 'session', 'createdBySession')},
                'federatedArchetype': True,
                'sanitized': True,
                'originTask': sk['task'],
            },
        })
        sanitized_skills.append(cp)

    sanitized_cases = []
    seen_case_ids = set()
    for cr in case_rows:
        src = cr.get('source') or {}
        ev = src.get('evidenceState') or ('user-confirmed' if src.get('certainty') == 'explicit-user' else 'reported')
        if ev not in ('feedback-verified', 'user-confirmed') or cr['item_id'] in seen_case_ids:
            continue
        seen_case_ids.add(cr['item_id'])
        blob = cr.get('content') or ''
        counts_i, _ = _token_counts(blob, qwords)
        overlap = sum(counts_i.values())
        if normalized and overlap == 0:
            norm_blob = ledger._normalize(blob)
            overlap = sum(1 for w in qwords if len(w) >= 2 and w in norm_blob)
        if normalized and overlap == 0:
            continue
        raw_card = dict(src.get('case') or {})
        san_card = {
            'title': sanitize_archetype_text(raw_card.get('title', cr['item_id'])),
            'situation': sanitize_archetype_text(raw_card.get('situation', '')),
            'symptom': sanitize_archetype_text(raw_card.get('symptom', '')),
            'rootCause': sanitize_archetype_text(raw_card.get('rootCause', '')),
            'avoidance': [sanitize_archetype_text(a) for a in (raw_card.get('avoidance') or [])],
            'conditions': sanitize_archetype_text(raw_card.get('conditions', '')),
            'severity': raw_card.get('severity', 'warning'),
        }
        cp_case = dict(cr)
        cp_case['content'] = sanitize_archetype_text(blob)
        cp_case['sharedFromTask'] = cr['task']
        cp_case['sharedMode'] = 'federated-archetype'
        cp_case['federatedScore'] = round(0.85 * (0.02 + 0.005 * min(10, overlap)), 6)
        cp_case['source'] = {
            'kind': 'federated-archetype',
            'certainty': 'derived',
            'evidenceState': ev,
            'scope': 'project',
            'case': san_card,
            'federatedArchetype': True,
            'sanitized': True,
            'originTask': cr['task'],
        }
        sanitized_cases.append(cp_case)

    sanitized_skills.sort(key=lambda x: (-x.get('federatedScore', 0.0), -x.get('event_at', 0.0)))
    sanitized_cases.sort(key=lambda x: (-x.get('federatedScore', 0.0), -x.get('event_at', 0.0)))
    return {
        'skills': sanitized_skills[:limit],
        'cases': sanitized_cases[:limit],
    }


def snapshot_skills(db, task, at=None, known_at=None, before=None, after=None,
                    project_only=False, verified_candidates_only=False,
                    include_verified_candidates=True, shared=False,
                    include_inactive=False, limit=200, federated=False):
    """Bitemporal snapshot of task skills."""
    ledger._identifier(task, 'task')
    limit = ledger._integer(limit, 'limit', 0, 1000)
    now = time.time()
    effective_at = now if at is None else ledger._timestamp(at, 'at')

    if known_at is None and at is None and before is None and after is None:
        rows = [
            _public_skill(r)
            for r in db.execute(
                'SELECT * FROM memory_skills WHERE task=? ORDER BY event_id DESC',
                (task,),
            ).fetchall()
        ]
    else:
        params = [task]
        where = ['task=?']
        if known_at is not None:
            where.append('observed_at<=?')
            params.append(ledger._timestamp(known_at, 'known_at'))
        rows_raw = db.execute(
            f"SELECT * FROM memory_skill_events WHERE {' AND '.join(where)} ORDER BY id DESC",
            params,
        ).fetchall()
        latest_by_skill = {}
        for r in rows_raw:
            sid = r['skill_id']
            if sid not in latest_by_skill:
                latest_by_skill[sid] = _public_skill(r)
        rows = list(latest_by_skill.values())

    filtered = []
    for row in rows:
        if not include_inactive and row['status'] != 'active':
            continue
        is_verified_candidate = (
            row['status'] == 'active'
            and row['evidence_state'] in ('feedback-verified', 'user-confirmed')
            and (row.get('source') or {}).get('verificationStatus') != 'compatible-unverified'
            and int(row.get('incorrect_count') or 0) == 0
        )
        if verified_candidates_only:
            if not is_verified_candidate:
                continue
            row['sharedMode'] = 'project-verified-candidate'
        elif project_only:
            if row['scope'] == 'project':
                row['sharedMode'] = 'project-explicit'
            elif include_verified_candidates and is_verified_candidate:
                row['sharedMode'] = 'project-verified-candidate'
            else:
                continue
        if row['valid_from'] is not None and row['valid_from'] > effective_at:
            continue
        if row['valid_until'] is not None and row['valid_until'] <= effective_at:
            continue
        if before is not None and row['event_at'] >= ledger._timestamp(before, 'before'):
            continue
        if after is not None and row['event_at'] <= ledger._timestamp(after, 'after'):
            continue
        row['links'] = node_wiki_links(db, row['task'], row['skill_id'])
        filtered.append(row)

    if shared and not project_only and not verified_candidates_only:
        local_names = {r['name'] for r in filtered}
        project = db.execute('SELECT project FROM tasks WHERE id=?', (task,)).fetchone()
        if project:
            others = db.execute(
                'SELECT id FROM tasks WHERE project=? AND id<>? ORDER BY id LIMIT 100',
                (project['project'], task),
            ).fetchall()
            for other in others:
                shared_rows = snapshot_skills(
                    db, other['id'], at=at, known_at=known_at, before=before, after=after,
                    project_only=True, include_verified_candidates=include_verified_candidates,
                    shared=False, include_inactive=False, limit=200,
                )
                for r in shared_rows:
                    if r['name'] in local_names:
                        continue
                    r['sharedFromTask'] = other['id']
                    filtered.append(r)

    if federated and not project_only and not verified_candidates_only:
        local_names = {r['name'] for r in filtered}
        fed = retrieve_federated_archetypes(db, task, query='', limit=10)
        for r in fed['skills']:
            if r['name'] not in local_names:
                local_names.add(r['name'])
                filtered.append(r)

    filtered.sort(key=lambda r: (-r['event_at'], -r.get('event_id', r.get('id', 0))))
    return filtered[:limit]


_SKILL_WORD_RE = re.compile(r'[^\W_]+')
_SKILL_HANGUL_RE = re.compile(r'[가-힣]')
_SKILL_TOKEN_CACHE = {}
_MAX_SKILL_TOKEN_CACHE = 2048
_SKILL_VECTOR_CACHE = {}
_MAX_SKILL_VECTOR_CACHE = 1024


def _cached_skill_tokens(content):
    cached = _SKILL_TOKEN_CACHE.get(content)
    if cached is not None:
        return cached
    norm_text = ledger._normalize(content)
    counts = Counter()
    length = 0
    for match in _SKILL_WORD_RE.finditer(norm_text):
        word = match.group(0)
        length += 1
        counts[word] += 1
        if _SKILL_HANGUL_RE.search(word):
            for i in range(len(word) - 1):
                length += 1
                counts[word[i:i + 2]] += 1
    res = (dict(counts), length, norm_text)
    if len(_SKILL_TOKEN_CACHE) >= _MAX_SKILL_TOKEN_CACHE:
        _SKILL_TOKEN_CACHE.pop(next(iter(_SKILL_TOKEN_CACHE)), None)
    _SKILL_TOKEN_CACHE[content] = res
    return res


def _tokens(content):
    words = _SKILL_WORD_RE.findall(ledger._normalize(content))
    return words + [word[i:i + 2] for word in words if _SKILL_HANGUL_RE.search(word)
                    for i in range(len(word) - 1)]


def _token_counts(content, wanted):
    cached = _cached_skill_tokens(content)
    full_counts, length = cached[0], cached[1]
    if not wanted:
        return Counter(), length
    return Counter({w: full_counts[w] for w in wanted if w in full_counts}), length


def _embed_skills_cached(rows, searchable):
    """Persistent in-memory skill vector cache keyed by (skill_id, version, file_sha256, model)."""
    s_vectors = [None] * len(rows)
    missing_indices = []
    missing_texts = []
    missing_keys = []
    for i, (row, text_i) in enumerate(zip(rows, searchable)):
        vkey = (row.get('skill_id'), row.get('version'), row.get('file_sha256'), vectors.MODEL)
        cached_vec = _SKILL_VECTOR_CACHE.get(vkey)
        if cached_vec is not None:
            s_vectors[i] = cached_vec
        else:
            missing_indices.append(i)
            missing_texts.append(text_i)
            missing_keys.append(vkey)
    if missing_texts:
        encoded = []
        for k in range(0, len(missing_texts), 32):
            encoded.extend(vectors.embed(missing_texts[k:k + 32]))
        for idx, vkey, vec in zip(missing_indices, missing_keys, encoded):
            if len(_SKILL_VECTOR_CACHE) >= _MAX_SKILL_VECTOR_CACHE:
                _SKILL_VECTOR_CACHE.pop(next(iter(_SKILL_VECTOR_CACHE)), None)
            _SKILL_VECTOR_CACHE[vkey] = vec
            s_vectors[idx] = vec
    return s_vectors


def retrieve_skills(db, task, query='', budget=2400, limit=4, root=None,
                    at=None, known_at=None, before=None, after=None, shared=False,
                    include_project_verified=True, semantic_config=None,
                    seed_node_scores=None, primary_limit=2, index_limit=4, federated=False):
    """Scoped hybrid retrieval over active procedural skills with 2-hop associative expansion & progressive index."""
    ledger._identifier(task, 'task')
    query = ledger._text(query, 'query', 2000)
    budget = ledger._integer(budget, 'budget', 0, 50000)
    limit = ledger._integer(limit, 'limit', 0, 50)
    rows = snapshot_skills(
        db, task, at=at, known_at=known_at, before=before, after=after,
        project_only=False, include_verified_candidates=include_project_verified,
        shared=bool(shared), include_inactive=False, limit=500, federated=bool(federated),
    )

    if not rows or limit == 0 or budget == 0:
        return {'items': [], 'primaryItems': [], 'indexItems': [], 'candidateCount': len(rows), 'omitted': len(rows)}

    normalized = ledger._normalize(query).strip()
    qwords = set(_tokens(query)[:256])
    terms = list(dict.fromkeys(re.findall(r'[^\W_]+', normalized)))[:64]
    pairs = sorted({t[i:i + 2] for t in terms if re.search('[가-힣]', t) for i in range(len(t) - 1)})[:128]

    searchable = [
        '\n'.join([
            r['title'], r['description'], ' '.join(r['triggers']),
            ' / '.join(r['steps']), r['outcome'],
            '조건: ' + r['conditions'], '예외: ' + r['counterexamples'],
        ])
        for r in rows
    ]
    cached_entries = [_cached_skill_tokens(text) for text in searchable]
    counts = [
        Counter({w: full_c[w] for w in qwords if w in full_c}) if qwords else Counter()
        for full_c, _, _ in cached_entries
    ]
    lengths = [length for _, length, _ in cached_entries]
    norm_texts = [nt for _, _, nt in cached_entries]
    average = sum(lengths) / max(1, len(rows))
    frequencies = {term: sum(term in c for c in counts) for term in qwords}

    lexical, trigger_match = {}, {}
    for i, (row, norm_i, counts_i) in enumerate(zip(rows, norm_texts, counts)):
        full_c = cached_entries[i][0]
        base = 0
        if normalized and normalized in norm_i:
            base += 16
        for t in terms:
            if t in full_c or t in norm_i:
                base += 8 if len(t) > 1 else 2
        for p in pairs:
            if p in full_c or p in norm_i:
                base += 3
        bm25 = 0.0
        length = lengths[i]
        for term in qwords:
            tf = counts_i.get(term, 0)
            if tf:
                idf = math.log(1 + (len(rows) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                bm25 += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / max(1, average)))
        if base or bm25:
            lexical[i] = bm25 + base / 20.0
        labels = list(row['triggers']) + row['source'].get('entities', []) + row['source'].get('aliases', [])
        if labels and normalized:
            overlap = sum(
                bool(ledger._normalize(label).strip()) and ledger._normalize(label) in normalized
                for label in labels
            )
            if overlap:
                trigger_match[i] = overlap

    semantic = {}
    config = semantic_config or {'semantic': False, 'threshold': 0.78}
    if normalized and config.get('semantic') and rows:
        try:
            qvector = vectors.embed([query], query=True)[0]
            s_vectors = _embed_skills_cached(rows, searchable)
            sim_scores = vectors.batch_similarity(qvector, s_vectors)
            thresh = config.get('threshold', 0.78)
            for i, score in enumerate(sim_scores):
                if score >= thresh:
                    semantic[i] = score
        except Exception:
            pass

    ranked = {}
    signals = {}
    for name, scores in (('bm25', lexical), ('trigger', trigger_match), ('semantic', semantic)):
        for rank, i in enumerate(sorted(scores, key=lambda idx: (-scores[idx], -rows[idx]['event_at'])), 1):
            ranked[i] = ranked.get(i, 0.0) + 1.0 / (60 + rank)
            signals.setdefault(i, {})[name] = round(scores[i], 6)

    # 2-hop hub-penalized PPR associative expansion from seed memory items or seed skills
    associative_from = {}
    seed_map = dict(seed_node_scores or {})
    for idx, sc in sorted(ranked.items(), key=lambda kv: -kv[1])[:3]:
        if sc > 0:
            seed_map[rows[idx]['skill_id']] = max(seed_map.get(rows[idx]['skill_id'], 0.0), sc)
    if seed_map:
        id_to_idx = {}
        for idx, r in enumerate(rows):
            if r['task'] == task:
                id_to_idx[r['skill_id']] = idx
                id_to_idx[r['experience_id']] = idx
        ppr_scores, ppr_prov = ppr_propagate(db, task, seed_map, max_hops=2, alpha=0.55)
        for node_id, raw_ppr in ppr_scores.items():
            target_idx = id_to_idx.get(node_id)
            if target_idx is None:
                continue
            prov = ppr_prov.get(node_id, {})
            if rows[target_idx]['skill_id'] == prov.get('fromId'):
                continue
            boost = round(raw_ppr * 0.45, 8)
            prev_assoc = signals.get(target_idx, {}).get('associative', 0.0)
            if boost > prev_assoc:
                ranked[target_idx] = ranked.get(target_idx, 0.0) + boost - prev_assoc
                signals.setdefault(target_idx, {})['associative'] = boost
                signals[target_idx]['pprHop'] = prov.get('hop', 1)
                associative_from[target_idx] = prov

    evidence_boost = {
        'user-confirmed': 0.008,
        'feedback-verified': 0.004,
        'trial-used': 0.002,
        'reported': 0.0,
    }
    for i in list(ranked):
        ranked[i] += evidence_boost.get(rows[i]['evidence_state'], 0.0)
        actr_val = float((rows[i].get('staleness') or {}).get('actrActivation') or 0.5)
        ranked[i] += round(0.003 * actr_val, 8)
        signals.setdefault(i, {})['actrActivation'] = actr_val
        if rows[i].get('stale'):
            ranked[i] -= 0.005
            signals.setdefault(i, {})['staleDecay'] = -0.005
        if rows[i].get('sharedMode') == 'project-verified-candidate':
            ranked[i] -= 0.001
        elif rows[i].get('sharedMode') == 'federated-archetype':
            ranked[i] = round(ranked[i] * 0.85, 8)
            signals.setdefault(i, {})['federatedWeight'] = 0.85

    if not normalized:
        ranked = {
            i: (
                evidence_boost.get(rows[i]['evidence_state'], 0.0)
                + 0.003 * float((rows[i].get('staleness') or {}).get('actrActivation') or 0.5)
                - (0.005 if rows[i].get('stale') else 0.0)
            )
            for i in range(len(rows))
        }

    ordered_indices = sorted(
        ranked,
        key=lambda idx: (-ranked[idx], -rows[idx]['event_at'], -rows[idx].get('event_id', 0)),
    )
    items = []
    selected_indices = set()
    remaining = budget
    for i in ordered_indices:
        if len(items) >= limit or remaining <= 0:
            break
        row = rows[i]
        badges = []
        if row.get('sharedFromTask'):
            if row.get('sharedMode') == 'federated-archetype':
                mode_label = '교차 프로젝트 아키타입'
            elif row.get('sharedMode') == 'project-verified-candidate':
                mode_label = '프로젝트 검증 후보'
            else:
                mode_label = '프로젝트 공유'
            badges.append(f"{mode_label}:{row['sharedFromTask']}")
        if row.get('stale'):
            badges.append('재확인 권장:' + ','.join(row['staleness']['reasons']))
        badge_str = (' · ' + ' · '.join(badges)) if badges else ''
        summary = (
            f"[{row['skill_id']} v{row['version']} · {row['evidence_state']}{badge_str}] {row['title']}\n"
            f"조건: {row['conditions']}\n"
            f"절차: {' -> '.join(row['steps'])}\n"
            f"결과: {row['outcome']}\n"
            f"예외(반례): {row['counterexamples']}\n"
            f"문서: {row['skill_path']}"
        )
        excerpt = summary[:min(1200, max(240, remaining))]
        remaining -= len(excerpt)
        selected_indices.add(i)
        entry = {
            'task': row['task'],
            'skillId': row['skill_id'],
            'name': row['name'],
            'experienceId': row['experience_id'],
            'version': row['version'],
            'status': row['status'],
            'evidenceState': row['evidence_state'],
            'scope': row['scope'],
            'title': row['title'],
            'description': row['description'],
            'triggers': row['triggers'],
            'steps': row['steps'],
            'outcome': row['outcome'],
            'conditions': row['conditions'],
            'counterexamples': row['counterexamples'],
            'skillPath': row['skill_path'],
            'fileSha256': row['file_sha256'],
            'stats': row['stats'],
            'stale': row['stale'],
            'staleness': row['staleness'],
            'verificationStatus': row['staleness'].get('verificationStatus') or row['source'].get('verificationStatus'),
            'gitBranch': row['source'].get('gitBranch', 'main'),
            'links': row.get('links', []),
            'excerpt': excerpt,
            'score': round(ranked[i], 8),
            'signals': signals.get(i, {}),
            'event_at': row['event_at'],
            'event_id': row.get('event_id', row.get('id', 0)),
            'sharedFromTask': row.get('sharedFromTask'),
            'sharedMode': row.get('sharedMode'),
            'consolidatedFrom': row['source'].get('consolidatedFrom', []),
            'sourceRefs': [
                {k: r.get(k) for k in ('itemId', 'eventId', 'kind', 'contentSha256')}
                for r in row['source'].get('sourceRefs', [])
            ],
        }
        if i in associative_from:
            entry['associativeFrom'] = associative_from[i]
        items.append(entry)

    primary_items = items[:primary_limit]
    index_items = []
    for i in ordered_indices:
        if len(index_items) >= index_limit:
            break
        row = rows[i]
        if any(p['skillId'] == row['skill_id'] for p in primary_items):
            continue
        index_items.append({
            'task': row['task'],
            'skillId': row['skill_id'],
            'name': row['name'],
            'version': row['version'],
            'evidenceState': row['evidence_state'],
            'title': row['title'][:120],
            'stale': row['stale'],
            'links': row.get('links', [])[:4],
            'score': round(ranked[i], 8),
            'disclosureStage': 'index',
            'readCommand': f"skill --id {row['skill_id']}",
        })

    return {
        'items': items,
        'primaryItems': primary_items,
        'indexItems': index_items,
        'candidateCount': len(rows),
        'omitted': max(0, len(rows) - len(items)),
    }


def sync_task_skills(store, task, session):
    """Synthesize skills for existing eligible active experiences that lack a skill record."""
    ledger._identifier(task, 'task')
    ledger._identifier(session, 'session')
    staged = []
    with store.db(True) as db:
        current = store.row(db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
        if not current or current['owner'] != session:
            raise ValueError('only the current responsible session can sync skills')
        store._check_input_lease(db, task)
        experiences = ledger.current(db, task, kinds=['experience'])
        for exp in experiences:
            card = exp['source'].get('experience')
            if not card or not can_auto_synthesize(card):
                continue
            existing = get_skill(db, task, exp['item_id'])
            if existing and existing['status'] == 'active' and existing['steps'] == card['steps']:
                events = skill_events(db, task, existing['skill_id'], limit=30)
                materialize_skill(store.root, existing, events)
                continue
            res = stage_skill_from_experience(
                db, store.root, task, current['revision'],
                'skill-sync:' + _digest([task, exp['item_id'], exp['event_id']]),
                exp['item_id'], 'assert', card, exp['source'],
                valid_from=exp.get('valid_from'), valid_until=exp.get('valid_until'),
                event_at=exp.get('event_at'),
            )
            if res and res.get('materialize'):
                events = skill_events(db, task, res['skill']['skill_id'], limit=30)
                staged.append((res['skill'], events))
        heal_res = heal_compatible_skills(db, root=store.root, task=task, dry_run=False)
        sync_task_links(db, task)
    synthesized = []
    for skill_row, events in staged:
        materialize_skill(store.root, skill_row, events)
        synthesized.append({
            'skillId': skill_row['skill_id'],
            'name': skill_row['name'],
            'experienceId': skill_row['experience_id'],
            'version': skill_row['version'],
            'status': skill_row['status'],
            'evidenceState': skill_row['evidence_state'],
            'skillPath': skill_row['skill_path'],
            'fileSha256': skill_row['file_sha256'],
        })
    return {
        'ok': True,
        'taskId': task,
        'synthesized': synthesized,
        'healedCount': heal_res['healedCount'],
        'healedSkills': heal_res['healedSkills'],
        'blockedBreakingSkills': heal_res['blockedBreakingSkills'],
    }


def manage_skill(store, task, session, revision, operation, spec, reference_validator):
    """Explicitly create, improve, promote, degrade, or retract a procedural skill."""
    ledger._identifier(task, 'task')
    ledger._identifier(session, 'session')
    ledger._identifier(operation, 'operation')
    if not isinstance(spec, dict):
        raise ValueError('skill operation spec must be an object')
    allowed = {
        'op', 'skillId', 'name', 'experienceId', 'title', 'description', 'triggers',
        'experience', 'sourceRefs', 'certainty', 'scope', 'shareEvidence',
        'entities', 'aliases', 'reason', 'validFrom', 'validUntil', 'timestamp',
        'referencedFiles', 'links',
    }
    if set(spec) - allowed:
        raise ValueError('invalid skill management fields')
    op = spec.get('op', 'create')
    if op not in ('create', 'improve', 'promote', 'degrade', 'retract'):
        raise ValueError('skill op must be create, improve, promote, degrade, or retract')

    payload = _digest(['skill_manage', session, revision, spec])
    staged = None
    with store.db(True) as db:
        current = store.row(db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
        if not current or current['owner'] != session:
            raise ValueError('only the current responsible session can manage skills')
        prior = db.execute(
            'SELECT * FROM memory_batches WHERE task=? AND operation=?',
            (task, operation),
        ).fetchone()
        if prior:
            if prior['digest'] != payload:
                raise ValueError('operation changed; use a fresh operationId')
            return json.loads(prior['result'])
        if current['revision'] != revision:
            raise ValueError('memory revision changed; refresh before writing')
        store._check_input_lease(db, task)

        refs = reference_validator(db, task, spec.get('sourceRefs'))
        explicit = spec.get('certainty') == 'explicit' and any(r['kind'] == 'instruction' for r in refs)
        scope = spec.get('scope', 'task')
        if scope not in ('task', 'project'):
            raise ValueError('scope must be task or project')
        source = {
            'kind': 'skill-manage',
            'session': session,
            'operation': operation,
            'sourceRefs': refs,
            'certainty': 'explicit-user' if explicit else 'derived',
            'scope': scope,
            'reason': ledger._text(spec.get('reason', ''), 'reason', 1000),
        }
        if 'referencedFiles' in spec:
            source['referencedFiles'] = spec['referencedFiles']
        if 'links' in spec and isinstance(spec['links'], list):
            source['links'] = [ledger._text(x, 'link', 240, empty=False) for x in spec['links'][:20]]
        if scope == 'project':
            share = reference_validator(db, task, [spec.get('shareEvidence')])
            if share[0]['kind'] != 'instruction':
                raise ValueError('project sharing requires user evidence')
            source['shareEvidence'] = share[0]
        for label_key in ('entities', 'aliases'):
            values = spec.get(label_key, [])
            if not isinstance(values, list) or len(values) > 20:
                raise ValueError('bounded labels required')
            source[label_key] = [ledger._text(v, label_key, 120, empty=False) for v in values]

        from memory_intelligence import timestamp
        start = timestamp(spec.get('validFrom'), 'validFrom')
        end = timestamp(spec.get('validUntil'), 'validUntil')
        now = time.time()

        target_ref = spec.get('skillId') or spec.get('experienceId') or spec.get('name')
        existing = get_skill(db, task, target_ref) if target_ref else None

        if op == 'retract':
            if not existing:
                raise ValueError('skill to retract not found')
            exp_id = existing['experience_id']
            exp_item = ledger.get_item(db, task, exp_id)
            if exp_item and exp_item['status'] == 'active':
                ledger.record(
                    db, task, revision + 1, 'skill-exp-retract:' + _digest([operation, exp_id]),
                    'experience', exp_id, 'retract', '', source, event_at=now,
                )
            staged_res = stage_skill_from_experience(
                db, store.root, task, revision + 1,
                'skill-manage:' + _digest([operation, existing['skill_id']]),
                exp_id, 'retract', None, source, valid_from=start, valid_until=end, event_at=now,
                skill_spec={'id': existing['skill_id'], 'name': existing['name']},
            )
            events = skill_events(db, task, existing['skill_id'], limit=30)
            staged = (staged_res['skill'], events)
        elif op in ('promote', 'degrade'):
            if not existing:
                raise ValueError('skill to update not found')
            if existing['evidence_state'] == 'user-confirmed' and not explicit and op == 'degrade':
                raise ValueError('user-confirmed skill cannot be degraded without explicit user source')
            card = {
                'situation': existing['source'].get('experience', {}).get('situation') or existing['title'],
                'steps': list(existing['steps']),
                'outcome': existing['outcome'],
                'conditions': existing['conditions'],
                'counterexamples': existing['counterexamples'],
            }
            if op == 'degrade' and source['reason'] and source['reason'] not in card['counterexamples']:
                card['counterexamples'] = (card['counterexamples'] + ' / 주의: ' + source['reason']).strip(' /')[:2000]
            new_status = 'active' if op == 'promote' else 'needs_revision'
            new_evidence = ('user-confirmed' if explicit else 'feedback-verified') if op == 'promote' else 'needs_revision'
            version = existing['version'] + 1
            new_source = {
                **existing['source'],
                **source,
                'experience': card,
                'evidenceState': new_evidence,
                'skillOperation': op,
                'userConfirmed': new_evidence == 'user-confirmed',
                'supersedesSkillVersion': existing['version'],
            }
            body = render_skill_markdown(
                name=existing['name'],
                skill_id=existing['skill_id'],
                experience_id=existing['experience_id'],
                task=task,
                scope=existing['scope'],
                version=version,
                status=new_status,
                evidence_state=new_evidence,
                title=existing['title'],
                description=existing['description'],
                triggers=existing['triggers'],
                card=card,
                source=new_source,
                stats=existing['stats'],
            )
            row = {
                'task': task,
                'revision': revision + 1,
                'event_key': 'skill-manage:' + _digest([operation, existing['skill_id']]),
                'skill_id': existing['skill_id'],
                'name': existing['name'],
                'experience_id': existing['experience_id'],
                'version': version,
                'operation': op,
                'status': new_status,
                'evidence_state': new_evidence,
                'scope': existing['scope'],
                'title': existing['title'],
                'description': existing['description'],
                'triggers': existing['triggers'],
                'steps': card['steps'],
                'outcome': card['outcome'],
                'conditions': card['conditions'],
                'counterexamples': card['counterexamples'],
                'body': body,
                'skill_path': existing['skill_path'],
                'file_sha256': _sha256_text(body),
                'source': new_source,
                'prior_version': existing['version'],
                'prior_status': existing['status'],
                'prior_evidence_state': existing['evidence_state'],
                'helpful_count': existing['helpful_count'],
                'used_count': existing['used_count'],
                'incorrect_count': existing['incorrect_count'] + (1 if op == 'degrade' else 0),
                'missing_count': existing['missing_count'],
                'valid_from': start if start is not None else existing['valid_from'],
                'valid_until': end if end is not None else existing['valid_until'],
                'event_at': now,
                'observed_at': now,
            }
            _write_skill_event_and_row(db, row)
            exp_item = ledger.get_item(db, task, existing['experience_id'])
            if exp_item and exp_item['status'] == 'active':
                exp_source = {
                    k: v for k, v in exp_item['source'].items()
                    if k not in ('contentSha256', 'contentChars', 'observedAt')
                }
                exp_source.update({
                    'experience': card,
                    'evidenceState': new_evidence,
                    'skillVersion': version,
                    'skillStatus': new_status,
                })
                exp_content = '\n'.join([
                    card['situation'],
                    ' / '.join(card['steps']),
                    card['outcome'],
                    '조건: ' + card['conditions'],
                    '예외: ' + card['counterexamples'],
                ])
                ledger.record(
                    db, task, revision + 1, 'skill-exp-manage:' + _digest([operation, existing['experience_id']]),
                    'experience', existing['experience_id'], 'replace', exp_content, exp_source,
                    event_at=now, valid_from=exp_item.get('valid_from'), valid_until=exp_item.get('valid_until'),
                )
            events = skill_events(db, task, existing['skill_id'], limit=30)
            staged = (_public_skill(row), events)
        else:
            card = validate_substance(spec.get('experience'), strict_counterexamples=True)
            exp_id = (
                existing['experience_id'] if existing
                else spec.get('experienceId')
                or ('experience:' + skill_name_for(spec.get('skillId') or spec.get('name') or card['situation']))
            )
            ledger._identifier(exp_id, 'experienceId')
            source['experience'] = card
            source['evidenceState'] = 'user-confirmed' if explicit else 'reported'
            exp_content = '\n'.join([
                card['situation'],
                ' / '.join(card['steps']),
                card['outcome'],
                '조건: ' + card['conditions'],
                '예외: ' + card['counterexamples'],
            ])
            old_exp = ledger.get_item(db, task, exp_id)
            exp_op = 'replace' if old_exp and old_exp['status'] == 'active' else 'assert'
            ledger.record(
                db, task, revision + 1, 'skill-exp:' + _digest([operation, exp_id]),
                'experience', exp_id, exp_op, exp_content, source,
                event_at=now, valid_from=start, valid_until=end,
            )
            staged_res = stage_skill_from_experience(
                db, store.root, task, revision + 1,
                'skill-manage:' + _digest([operation, exp_id]),
                exp_id, exp_op, card, source,
                valid_from=start, valid_until=end, event_at=now,
                skill_spec={
                    k: spec[k]
                    for k in ('skillId', 'name', 'title', 'description', 'triggers')
                    if k in spec
                } if not ('skillId' in spec) else {
                    'id': spec['skillId'],
                    **{k: spec[k] for k in ('name', 'title', 'description', 'triggers') if k in spec},
                },
            )
            events = skill_events(db, task, staged_res['skill']['skill_id'], limit=30)
            staged = (staged_res['skill'], events)

        sync_task_links(db, task)
        skill_row, events = staged
        db.execute(
            'UPDATE tasks SET revision=revision+1,updated=? WHERE id=?',
            (now, task),
        )
        result = {
            'ok': True,
            'taskId': task,
            'revision': revision + 1,
            'operationId': operation,
            'skill': {
                'skillId': skill_row['skill_id'],
                'name': skill_row['name'],
                'experienceId': skill_row['experience_id'],
                'version': skill_row['version'],
                'status': skill_row['status'],
                'evidenceState': skill_row['evidence_state'],
                'scope': skill_row['scope'],
                'skillPath': skill_row['skill_path'],
                'fileSha256': skill_row['file_sha256'],
                'stats': skill_row['stats'],
                'stale': skill_row['stale'],
                'staleness': skill_row['staleness'],
            },
        }
        db.execute(
            'INSERT INTO memory_batches VALUES(?,?,?,?)',
            (task, operation, payload, _json(result)),
        )
    if staged:
        materialize_skill(store.root, staged[0], staged[1])
    return result


def read_skill(store, task, skill_or_exp_id):
    """Read full skill record, SKILL.md content, and version history."""
    with store.db() as db:
        skill = get_skill(db, task, skill_or_exp_id)
        if not skill:
            raise ValueError('skill not found in this task')
        skill['links'] = node_wiki_links(db, task, skill['skill_id'])
        events = skill_events(db, task, skill['skill_id'], limit=50)
    path = Path(skill['skill_path'])
    disk_exists = path.exists() and not path.is_symlink()
    disk_sha256 = _sha256_text(path.read_text(encoding='utf-8')) if disk_exists else ''
    return {
        **skill,
        'skillPath': skill['skill_path'],
        'markdownPath': skill['skill_path'],
        'evidenceState': skill['evidence_state'],
        'diskExists': disk_exists,
        'diskVerified': disk_exists and disk_sha256 == skill['file_sha256'],
        'versions': [
            {
                'eventId': e['id'],
                'version': e['version'],
                'revision': e['revision'],
                'operation': e['operation'],
                'status': e['status'],
                'evidenceState': e['evidence_state'],
                'priorVersion': e['prior_version'],
                'priorStatus': e['prior_status'],
                'priorEvidenceState': e['prior_evidence_state'],
                'fileSha256': e['file_sha256'],
                'eventAt': e['event_at'],
                'observedAt': e['observed_at'],
            }
            for e in events
        ],
        'events': events,
    }


def parse_skill_markdown(md_text, fallback_card=None):
    """Parse human-edited SKILL.md back into a structured skill card (Basic Memory style co-editing)."""
    fallback = dict(fallback_card or {})
    title = fallback.get('title', '')
    m_title = re.search(r'(?m)^#\s+(.+?)\s*$', md_text)
    if m_title:
        title = m_title.group(1).strip()

    def _extract_section(header_prefix):
        pat = rf'(?ms)^##\s+{re.escape(header_prefix)}[^\n]*\n(.*?)(?=^##\s+|\Z)'
        m = re.search(pat, md_text)
        if not m:
            return ''
        lines = []
        for ln in m.group(1).splitlines():
            s = ln.strip()
            if s.startswith('주요 트리거 키워드:'):
                continue
            lines.append(ln.rstrip())
        return '\n'.join(lines).strip()

    situation = _extract_section('언제 사용하나요') or fallback.get('situation', '')
    conditions = _extract_section('적용 조건') or fallback.get('conditions', '')
    counterexamples = _extract_section('적용 예외 및 반례') or fallback.get('counterexamples', '')
    outcome = _extract_section('기대 결과 및 검증 기준') or fallback.get('outcome', '')
    steps_raw = _extract_section('실행 절차')
    steps = []
    if steps_raw:
        for ln in steps_raw.splitlines():
            s = ln.strip()
            m_step = re.match(r'^(?:\d+[.)]|[-*])\s+(.+)$', s)
            if m_step:
                steps.append(m_step.group(1).strip())
            elif s:
                steps.append(s)
    if not steps:
        steps = list(fallback.get('steps') or [])

    triggers = None
    m_trig = re.search(r'주요 트리거 키워드:\s*(.+)', md_text)
    if m_trig:
        found_trigs = re.findall(r'`([^`]+)`', m_trig.group(1))
        if found_trigs:
            triggers = [t.strip() for t in found_trigs if t.strip()]

    validated = validate_substance(
        {
            'situation': situation,
            'steps': steps,
            'outcome': outcome,
            'conditions': conditions,
            'counterexamples': counterexamples,
        },
        strict_counterexamples=True,
    )
    return {
        'title': title,
        'triggers': triggers,
        'card': validated,
    }


def sync_skill_markdowns(store, task, session=None, explicit_user=None):
    """Bi-directionally sync human-edited SKILL.md files on disk back into SQLite memory_skills & memory_items.
    Promotes evidenceState to 'user-confirmed' ONLY when explicit_user=True is explicitly supplied.
    """
    ledger._identifier(task, 'task')
    now = time.time()
    staged = []
    with store.db(True) as db:
        current = store.row(db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
        if not current:
            raise ValueError('업무를 찾지 못했어요.')
        active_session = session or current['owner']
        if current['owner'] != active_session:
            raise ValueError('only the current responsible session can sync markdown')
        store._check_input_lease(db, task)
        is_explicit_user_coedit = bool(explicit_user is True)
        skills_rows = [
            _public_skill(r)
            for r in db.execute("SELECT * FROM memory_skills WHERE task=? AND status='active' ORDER BY event_id DESC", (task,)).fetchall()
        ]
        rev = current['revision']
        for sk in skills_rows:
            p = Path(sk['skill_path'])
            if not p.exists() or p.is_symlink() or not p.is_file():
                continue
            disk_text = p.read_text(encoding='utf-8', errors='ignore')
            disk_sha = _sha256_text(disk_text)
            if disk_sha == sk['file_sha256']:
                continue
            fallback_card = {
                'title': sk['title'],
                'situation': (sk['source'].get('experience') or {}).get('situation') or sk['title'],
                'steps': sk['steps'],
                'outcome': sk['outcome'],
                'conditions': sk['conditions'],
                'counterexamples': sk['counterexamples'],
            }
            parsed = parse_skill_markdown(disk_text, fallback_card=fallback_card)
            new_card = parsed['card']
            same_content = (
                new_card['steps'] == sk['steps']
                and new_card['conditions'] == sk['conditions']
                and new_card['counterexamples'] == sk['counterexamples']
                and new_card['outcome'] == sk['outcome']
                and parsed['title'] == sk['title']
            )
            if same_content:
                continue
            rev += 1
            new_version = sk['version'] + 1
            new_triggers = parsed['triggers'] or _triggers_for(new_card, sk['source'], sk['triggers'])
            new_title = ledger._text(parsed['title'] or sk['title'], 'title', 240, empty=False)
            new_desc = ledger._text(f"{new_card['situation']} (조건: {new_card['conditions'][:240]})", 'description', 1000, empty=False)
            ref_files = _extract_referenced_files(new_card, root=store.root, explicit_files=sk['source'].get('referencedFiles'))
            if is_explicit_user_coedit:
                target_ev_state = 'user-confirmed'
                target_ver_status = 'verified'
            else:
                prev_ev = sk.get('evidence_state') or 'reported'
                target_ev_state = 'trial-used' if prev_ev in ('feedback-verified', 'user-confirmed') else prev_ev
                target_ver_status = 'compatible-unverified'
            new_source = {
                **sk['source'],
                'experience': new_card,
                'evidenceState': target_ev_state,
                'userConfirmed': target_ev_state == 'user-confirmed',
                'verificationStatus': target_ver_status,
                'verificationMode': 'markdown-coedit' if is_explicit_user_coedit else 'unverified-disk-sync',
                'skillOperation': 'coedit',
                'lastSyncSource': 'markdown-coedit',
                'coeditedDiskSha256': disk_sha,
                'coeditedBySession': active_session,
                'supersedesSkillVersion': sk['version'],
                'referencedFiles': ref_files or sk['source'].get('referencedFiles', []),
                'gitBranch': detect_git_branch(store.root),
            }
            body = render_skill_markdown(
                name=sk['name'],
                skill_id=sk['skill_id'],
                experience_id=sk['experience_id'],
                task=task,
                scope=sk['scope'],
                version=new_version,
                status='active',
                evidence_state=target_ev_state,
                title=new_title,
                description=new_desc,
                triggers=new_triggers,
                card=new_card,
                source=new_source,
                stats=sk['stats'],
            )
            row = {
                'task': task,
                'revision': rev,
                'event_key': 'skill-coedit:' + _digest([task, sk['skill_id'], disk_sha, rev]),
                'skill_id': sk['skill_id'],
                'name': sk['name'],
                'experience_id': sk['experience_id'],
                'version': new_version,
                'operation': 'coedit',
                'status': 'active',
                'evidence_state': target_ev_state,
                'scope': sk['scope'],
                'title': new_title,
                'description': new_desc,
                'triggers': new_triggers,
                'steps': new_card['steps'],
                'outcome': new_card['outcome'],
                'conditions': new_card['conditions'],
                'counterexamples': new_card['counterexamples'],
                'body': body,
                'skill_path': sk['skill_path'],
                'file_sha256': _sha256_text(body),
                'source': new_source,
                'prior_version': sk['version'],
                'prior_status': sk['status'],
                'prior_evidence_state': sk['evidence_state'],
                'helpful_count': sk['helpful_count'],
                'used_count': sk['used_count'],
                'incorrect_count': sk['incorrect_count'],
                'missing_count': sk['missing_count'],
                'valid_from': sk['valid_from'],
                'valid_until': sk['valid_until'],
                'event_at': now,
                'observed_at': now,
            }
            _write_skill_event_and_row(db, row)
            exp_item = ledger.get_item(db, task, sk['experience_id'])
            if exp_item and exp_item['status'] == 'active':
                exp_source = {
                    k: v for k, v in exp_item['source'].items()
                    if k not in ('contentSha256', 'contentChars', 'observedAt')
                }
                exp_source.update({
                    'experience': new_card,
                    'evidenceState': target_ev_state,
                    'verificationStatus': target_ver_status,
                    'certainty': 'explicit-user' if is_explicit_user_coedit else exp_source.get('certainty', 'derived'),
                    'lastSyncSource': 'markdown-coedit',
                    'skillVersion': new_version,
                })
                exp_content = '\n'.join([
                    new_card['situation'],
                    ' / '.join(new_card['steps']),
                    new_card['outcome'],
                    '조건: ' + new_card['conditions'],
                    '예외: ' + new_card['counterexamples'],
                ])
                ledger.record(
                    db, task, rev, 'exp-coedit:' + _digest([task, sk['experience_id'], disk_sha, rev]),
                    'experience', sk['experience_id'], 'replace', exp_content, exp_source,
                    event_at=now, valid_from=exp_item.get('valid_from'), valid_until=exp_item.get('valid_until'),
                )
            events = skill_events(db, task, sk['skill_id'], limit=30)
            staged.append((_public_skill(row), events))
        if staged:
            sync_task_links(db, task)
            db.execute('UPDATE tasks SET revision=?,updated=? WHERE id=?', (rev, now, task))
    synced = []
    for skill_row, events in staged:
        materialize_skill(store.root, skill_row, events)
        synced.append({
            'skillId': skill_row['skill_id'],
            'name': skill_row['name'],
            'version': skill_row['version'],
            'evidenceState': skill_row['evidence_state'],
            'verificationStatus': skill_row['source'].get('verificationStatus', 'verified'),
            'skillPath': skill_row['skill_path'],
            'fileSha256': skill_row['file_sha256'],
        })
    return {'ok': True, 'taskId': task, 'synced': synced, 'syncedSkills': synced, 'syncedCount': len(synced)}


def _detect_git_head_state(root):
    """Return (head_sha, dirty_worktree) for the git repository containing root, or ('', False) if not a git repo."""
    if not root:
        return '', False
    try:
        proc_sha = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if proc_sha.returncode != 0:
            return '', False
        head_sha = (proc_sha.stdout or '').strip()
        proc_st = subprocess.run(
            ['git', 'status', '--porcelain', '-uno'],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        dirty = bool((proc_st.stdout or '').strip()) if proc_st.returncode == 0 else False
        return head_sha, dirty
    except (OSError, subprocess.SubprocessError):
        return '', False


def _parse_test_runner_count(cmd_str, output_text, explicit_test_count=None):
    """Parse executed test count from test runner output and detect zero-test false positives (`Ran 0 tests`, `0 passed`)."""
    out = str(output_text or '')
    if explicit_test_count is not None:
        cnt = int(explicit_test_count)
        return cnt, (cnt <= 0)
    # Explicit zero-test patterns across unittest, pytest, jest, vitest, go test
    if re.search(r'\b(?:Ran\s+0\s+tests?|0\s+passed|collected\s+0\s+items?|no\s+tests\s+ran)\b', out, re.IGNORECASE):
        return 0, True
    counts = []
    m_unit = re.search(r'\bRan\s+(\d+)\s+tests?\b', out, re.IGNORECASE)
    if m_unit:
        counts.append(int(m_unit.group(1)))
    m_pass = re.search(r'\b(\d+)\s+passed\b', out, re.IGNORECASE)
    if m_pass:
        counts.append(int(m_pass.group(1)))
    m_col = re.search(r'\bcollected\s+(\d+)\s+items?\b', out, re.IGNORECASE)
    if m_col:
        counts.append(int(m_col.group(1)))
    go_passes = re.findall(r'^---\s+PASS:', out, re.MULTILINE)
    if go_passes:
        counts.append(len(go_passes))
    if counts:
        best = max(counts)
        return best, (best <= 0)
    # For inline assertion scripts (`python3 -c "assert ..."`), count assert statements or default to 1 command check
    assert_cnt = len(re.findall(r'\bassert\b', str(cmd_str or '')))
    return max(1, assert_cnt), False


def verify_skill_execution(store, task, session, skill_id, command=None, execution_proof=None, operation_id=None, timeout=15.0):
    """Execution-Backed Skill Verification: verify a procedural skill by running a bounded check command or recording an execution proof, binding verificationEvidence (exitCode, command, outputSha256, codeAnchorDigest, headSha, testCount) to the skill."""
    ledger._identifier(task, 'task')
    session = ledger._text(session, 'session', 160, empty=False)
    skill_ref = ledger._text(skill_id, 'skillId', 240, empty=False)
    if not skill_ref.startswith('skill:'):
        skill_ref = 'skill:' + _slug_skill_name(skill_ref)
    op_id = ledger._text(operation_id, 'operationId', 160) if operation_id else 'verify:' + _digest([task, session, skill_ref, command, execution_proof, time.time()])

    if command is None and not isinstance(execution_proof, dict):
        raise ValueError('command 또는 executionProof가 필요해요.')

    # Pre-check owner and skill existence before running any subprocess
    with store.db(False) as db:
        ensure_schema(db)
        existing_op = db.execute(
            'SELECT * FROM memory_batches WHERE task=? AND operation=?',
            (task, op_id),
        ).fetchone()
        if existing_op:
            raw_res = existing_op['result'] if 'result' in existing_op.keys() else existing_op['response']
            return json.loads(raw_res)
        current = store.row(db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
        if not current:
            raise ValueError('업무를 찾지 못했어요.')
        if current['owner'] != session:
            raise ValueError('only the current responsible session can verify skills')
        store._check_input_lease(db, task)
        target_skill = get_skill(db, task, skill_ref)
        if not target_skill:
            raise ValueError(f'스킬을 찾지 못했어요: {skill_ref}')

    explicit_test_count = None
    # Execute command outside SQLite lock if command was given
    if command is not None:
        if isinstance(command, (list, tuple)):
            cmd_args = [str(x) for x in command]
            cmd_str = ' '.join(cmd_args)
            use_shell = False
        else:
            cmd_str = str(command).strip()
            if not cmd_str:
                raise ValueError('비어 있지 않은 검증 명령(command)이 필요해요.')
            cmd_args = cmd_str
            use_shell = True
        try:
            proc = subprocess.run(
                cmd_args,
                shell=use_shell,
                cwd=str(store.root),
                capture_output=True,
                text=True,
                timeout=min(max(float(timeout or 15.0), 1.0), 30.0),
            )
            exit_code = int(proc.returncode)
            combined_out = ((proc.stdout or '') + '\n' + (proc.stderr or '')).strip()
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            combined_out = f"TimeoutExpired after {timeout}s: {exc}"
        except OSError as exc:
            exit_code = 127
            combined_out = f"OSError: {exc}"
        output_sha256 = _sha256_text(combined_out)
        mode = 'subprocess-execution'
    else:
        cmd_str = str(execution_proof.get('command') or 'execution-proof').strip()
        if 'exitCode' not in execution_proof:
            raise ValueError('executionProof에는 정수형 exitCode가 필요해요.')
        exit_code = int(execution_proof['exitCode'])
        combined_out = str(
            execution_proof.get('output')
            or ((str(execution_proof.get('stdout') or '') + '\n' + str(execution_proof.get('stderr') or '')).strip())
        ).strip()
        if 'testCount' in execution_proof and execution_proof['testCount'] is not None:
            explicit_test_count = int(execution_proof['testCount'])
        if exit_code == 0 and not combined_out and not execution_proof.get('outputSha256') and explicit_test_count is None:
            raise ValueError('executionProof로 성공(exitCode=0)을 기록할 때는 실행 출력(output) 또는 testCount가 필요해요.')
        output_sha256 = str(execution_proof.get('outputSha256') or _sha256_text(combined_out))
        mode = 'execution-proof'

    test_count, zero_tests_rejected = _parse_test_runner_count(cmd_str, combined_out, explicit_test_count=explicit_test_count)
    if exit_code == 0 and zero_tests_rejected:
        exit_code = 1
        combined_out = (combined_out + '\n[ZeroTestsRejected] 실행된 테스트 수가 0건(Ran 0 tests / 0 passed)이어서 검증 승격을 거부했어요.').strip()
        output_sha256 = _sha256_text(combined_out)

    head_sha, dirty_worktree = _detect_git_head_state(store.root)
    if isinstance(execution_proof, dict) and execution_proof.get('headSha'):
        head_sha = str(execution_proof['headSha']).strip()

    now = time.time()
    synthesized_case_id = None
    with store.db(True) as db:
        ensure_schema(db)
        existing_op = db.execute(
            'SELECT * FROM memory_batches WHERE task=? AND operation=?',
            (task, op_id),
        ).fetchone()
        if existing_op:
            raw_res = existing_op['result'] if 'result' in existing_op.keys() else existing_op['response']
            return json.loads(raw_res)
        current = store.row(db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
        if not current:
            raise ValueError('업무를 찾지 못했어요.')
        if current['owner'] != session:
            raise ValueError('only the current responsible session can verify skills')
        store._check_input_lease(db, task)
        skill = get_skill(db, task, skill_ref)
        if not skill:
            raise ValueError(f'스킬을 찾지 못했어요: {skill_ref}')

        card = {
            'situation': skill['source'].get('experience', {}).get('situation') or skill['title'],
            'steps': list(skill['steps']),
            'outcome': skill['outcome'],
            'conditions': skill['conditions'],
            'counterexamples': skill['counterexamples'],
        }
        ref_files = _extract_referenced_files(card, root=store.root, explicit_files=skill['source'].get('referencedFiles'))
        if not ref_files:
            ref_files = list(skill['source'].get('referencedFiles') or [])
        code_anchor_digest = _digest([
            (r.get('path'), r.get('sha256'), r.get('symbols'))
            for r in (ref_files or [])
            if isinstance(r, dict)
        ])
        version = int(skill['version']) + 1
        rev = int(current['revision']) + 1
        verification_evidence = {
            'verifiedAt': now,
            'verifiedBySession': session,
            'skillVersion': version,
            'codeAnchorDigest': code_anchor_digest,
            'headSha': head_sha,
            'dirtyWorktree': dirty_worktree,
            'testCount': test_count if exit_code == 0 else 0,
            'zeroTestsRejected': zero_tests_rejected,
            'command': cmd_str[:500],
            'exitCode': exit_code,
            'outputSha256': output_sha256,
            'outputExcerpt': combined_out[:600],
            'mode': mode,
            'operation': op_id,
        }
        helpful_count = int(skill['helpful_count'] or 0)
        used_count = int(skill['used_count'] or 0)
        incorrect_count = int(skill['incorrect_count'] or 0)
        missing_count = int(skill['missing_count'] or 0)

        if exit_code == 0:
            status = 'active'
            evidence_state = 'user-confirmed' if skill['evidence_state'] == 'user-confirmed' else 'feedback-verified'
            ver_status = 'verified'
            ver_mode = mode
            skill_op = 'verify_execution'
            helpful_count += 1
            used_count += 1
        else:
            if skill['evidence_state'] == 'user-confirmed':
                raise ValueError('user-confirmed skill requires explicit user correction to override or degrade')
            status = 'needs_revision'
            evidence_state = 'needs_revision'
            ver_status = 'broken'
            ver_mode = 'zero-tests-rejected' if zero_tests_rejected else 'execution-failed'
            skill_op = 'verify_failed'
            incorrect_count += 1
            fail_note = f"실행 검증 실패(exitCode={exit_code}, cmd={cmd_str[:120]}): {combined_out[:240]}"
            if fail_note not in card['counterexamples']:
                card['counterexamples'] = (card['counterexamples'] + ' / ' + fail_note).strip(' /')[:2000]

        stats = {'used': used_count, 'helpful': helpful_count, 'incorrect': incorrect_count, 'missing': missing_count}
        new_source = {
            **skill['source'],
            'experience': card,
            'evidenceState': evidence_state,
            'verificationStatus': ver_status,
            'verificationMode': ver_mode,
            'verificationEvidence': verification_evidence,
            'referencedFiles': ref_files,
            'gitBranch': detect_git_branch(store.root),
        }
        body = render_skill_markdown(
            name=skill['name'],
            skill_id=skill['skill_id'],
            experience_id=skill['experience_id'],
            task=task,
            scope=skill['scope'],
            version=version,
            status=status,
            evidence_state=evidence_state,
            title=skill['title'],
            description=skill['description'],
            triggers=skill['triggers'],
            card=card,
            source=new_source,
            stats=stats,
        )
        file_sha256 = _sha256_text(body)
        row = {
            'task': task,
            'revision': rev,
            'event_key': 'skill-verify:' + _digest([op_id, skill['skill_id'], version]),
            'skill_id': skill['skill_id'],
            'name': skill['name'],
            'experience_id': skill['experience_id'],
            'version': version,
            'operation': skill_op,
            'status': status,
            'evidence_state': evidence_state,
            'scope': skill['scope'],
            'title': skill['title'],
            'description': skill['description'],
            'triggers': skill['triggers'],
            'steps': card['steps'],
            'outcome': card['outcome'],
            'conditions': card['conditions'],
            'counterexamples': card['counterexamples'],
            'body': body,
            'skill_path': skill['skill_path'],
            'file_sha256': file_sha256,
            'source': new_source,
            'prior_version': skill['version'],
            'prior_status': skill['status'],
            'prior_evidence_state': skill['evidence_state'],
            'helpful_count': helpful_count,
            'used_count': used_count,
            'incorrect_count': incorrect_count,
            'missing_count': missing_count,
            'valid_from': skill['valid_from'],
            'valid_until': skill['valid_until'],
            'event_at': now,
            'observed_at': now,
        }
        _write_skill_event_and_row(db, row)
        exp_item = ledger.get_item(db, task, skill['experience_id'])
        if exp_item and exp_item['status'] == 'active':
            exp_source = {
                k: v for k, v in exp_item['source'].items()
                if k not in ('contentSha256', 'contentChars', 'observedAt')
            }
            exp_source.update({
                'experience': card,
                'evidenceState': evidence_state,
                'verificationStatus': ver_status,
                'verificationEvidence': verification_evidence,
                'skillVersion': version,
                'skillStatus': status,
            })
            exp_content = '\n'.join([
                card['situation'],
                ' / '.join(card['steps']),
                card['outcome'],
                '조건: ' + card['conditions'],
                '예외: ' + card['counterexamples'],
            ])
            ledger.record(
                db, task, rev, 'exp-verify:' + _digest([op_id, skill['experience_id'], version]),
                'experience', skill['experience_id'], 'replace', exp_content, exp_source,
                event_at=now, valid_from=exp_item.get('valid_from'), valid_until=exp_item.get('valid_until'),
            )
        if exit_code != 0:
            synthesized_case_id = 'case:pitfall-' + skill['name']
            case_card = {
                'title': f"{skill['title']} 실행 검증 실패 주의보",
                'situation': card['situation'],
                'symptom': f"exitCode={exit_code} ({cmd_str[:160]}): {combined_out[:600]}",
                'rootCause': f"절차 스킬({skill['skill_id']}) 실행 검증 실패",
                'avoidance': [card['counterexamples'][:600]],
                'conditions': card['conditions'],
                'severity': 'warning',
            }
            case_content = format_case_content(case_card)
            old_case = ledger.get_item(db, task, synthesized_case_id)
            case_op = 'replace' if old_case and old_case['status'] == 'active' else 'assert'
            case_source = {
                'kind': 'execution-verify-pitfall',
                'session': session,
                'operation': op_id,
                'certainty': 'derived',
                'evidenceState': 'feedback-verified',
                'scope': skill['scope'],
                'linkedSkillId': skill['skill_id'],
                'case': case_card,
            }
            ledger.record(
                db, task, rev, 'case-verify:' + _digest([op_id, synthesized_case_id]),
                'case', synthesized_case_id, case_op, case_content, case_source,
                event_at=now, valid_from=now,
            )
        sync_task_links(db, task)
        db.execute('UPDATE tasks SET revision=?,updated=? WHERE id=?', (rev, now, task))
        updated = _public_skill(row)
        events = skill_events(db, task, skill['skill_id'], limit=30)
        res_payload = {
            'ok': exit_code == 0,
            'taskId': task,
            'skillId': skill['skill_id'],
            'version': version,
            'status': status,
            'evidenceState': evidence_state,
            'verificationStatus': ver_status,
            'verificationMode': ver_mode,
            'verificationEvidence': verification_evidence,
            'skill': updated,
            'synthesizedCaseId': synthesized_case_id,
        }
        db.execute(
            'INSERT OR REPLACE INTO memory_batches VALUES (?,?,?,?)',
            (task, op_id, _digest([task, op_id, skill['skill_id'], version]), _json(res_payload)),
        )
    materialize_skill(store.root, updated, events)
    return res_payload


def skill_metrics(db, task):
    """Compute lightweight recall & procedural skill adoption/effectiveness metrics with bounded [0.0, 1.0] rates."""
    ledger._identifier(task, 'task')
    ensure_schema(db)
    recalls_rows = db.execute(
        'SELECT id, item_refs, stats FROM memory_recalls WHERE task=? ORDER BY created DESC',
        (task,),
    ).fetchall()
    turn_rows = db.execute(
        'SELECT id, request_id, raw_text FROM memory_turn_prompts WHERE task=?',
        (task,),
    ).fetchall()
    skills_rows = [
        _public_skill(r)
        for r in db.execute('SELECT * FROM memory_skills WHERE task=? ORDER BY event_id DESC', (task,)).fetchall()
    ]
    case_count = db.execute(
        "SELECT count(*) FROM memory_items WHERE task=? AND kind='case' AND status='active'",
        (task,),
    ).fetchone()[0]
    link_count = db.execute(
        'SELECT count(*) FROM memory_links WHERE task=?',
        (task,),
    ).fetchone()[0]
    links_by_relation = dict(
        db.execute('SELECT relation, count(*) FROM memory_links WHERE task=? GROUP BY relation', (task,)).fetchall()
    )
    impressions_by_id = Counter()
    elapsed_ms_list = []
    recall_delivered_chars = {}
    for r in recalls_rows:
        try:
            refs = json.loads(r['item_refs']) if isinstance(r['item_refs'], str) else []
            seen_in_recall = set()
            for ref in refs:
                if isinstance(ref, dict):
                    for k in ('id', 'skillId', 'experienceId'):
                        if ref.get(k):
                            seen_in_recall.add(ref[k])
            for sid in seen_in_recall:
                impressions_by_id[sid] += 1
        except Exception:
            pass
        try:
            stats_obj = json.loads(r['stats']) if isinstance(r['stats'], str) else {}
            if isinstance(stats_obj.get('elapsedMs'), (int, float)):
                elapsed_ms_list.append(float(stats_obj['elapsedMs']))
            recall_delivered_chars[r['id']] = int(stats_obj.get('deliveredChars') or 0) + int(stats_obj.get('deliveredSkills') or 0) * 320
        except Exception:
            pass

    prompt_chars = [
        len(r['raw_text'] or '') + recall_delivered_chars.get(r['id'], 240)
        for r in turn_rows
    ]
    evidence_breakdown = Counter(s['evidence_state'] for s in skills_rows)
    status_breakdown = Counter(s['status'] for s in skills_rows)
    stale_count = sum(1 for s in skills_rows if s['status'] == 'active' and s.get('stale'))
    consolidated_count = sum(1 for s in skills_rows if s['source'].get('consolidatedFrom'))

    per_skill = []
    total_positive_touches = 0
    total_used = 0
    total_helpful = 0
    total_incorrect = 0
    total_missing = 0
    total_impressions = 0
    for s in skills_rows:
        used = int(s.get('used_count') or 0)
        helpful = int(s.get('helpful_count') or 0)
        incorrect = int(s.get('incorrect_count') or 0)
        missing = int(s.get('missing_count') or 0)
        raw_imp = max(
            impressions_by_id.get(s['skill_id'], 0),
            impressions_by_id.get(s['experience_id'], 0),
        )
        rates = _eval_core.compute_skill_rates(used, helpful, incorrect, missing, raw_imp)
        imp = rates['impressions']
        total_positive_touches += rates['positiveTouches']
        total_used += used
        total_helpful += helpful
        total_incorrect += incorrect
        total_missing += missing
        total_impressions += imp
        per_skill.append({
            'skillId': s['skill_id'],
            'name': s['name'],
            'version': s['version'],
            'status': s['status'],
            'evidenceState': s['evidence_state'],
            'verificationStatus': s['staleness'].get('verificationStatus', 'verified'),
            'impressions': imp,
            'usedCount': used,
            'helpfulCount': helpful,
            'incorrectCount': incorrect,
            'missingCount': missing,
            'adoptionRate': rates['adoptionRate'],
            'helpfulRate': rates['helpfulRate'],
            'errorRate': rates['errorRate'],
            'reuseIntensity': rates.get('reuseIntensity', 0.0),
            'stale': s['stale'],
            'staleReasons': s['staleness']['reasons'],
            'actrActivation': s['staleness'].get('actrActivation', 0.5),
            'consolidatedFrom': s['source'].get('consolidatedFrom', []),
            'links': node_wiki_links(db, task, s['skill_id']),
        })

    overall_adoption = round(min(1.0, total_positive_touches / total_impressions), 4) if total_impressions > 0 else None
    overall_helpful = round(min(1.0, total_helpful / max(1, total_positive_touches)), 4) if total_positive_touches > 0 else None
    overall_error = round(min(1.0, total_incorrect / total_impressions), 4) if total_impressions > 0 else None
    reuse_intensity = round((total_used + total_helpful) / max(1, total_impressions), 4) if (total_used + total_helpful) > 0 else 0.0

    return {
        'recallCount': len(recalls_rows),
        'turnPromptCount': len(turn_rows),
        'avgPromptChars': round(sum(prompt_chars) / len(prompt_chars), 1) if prompt_chars else 0.0,
        'maxPromptChars': max(prompt_chars) if prompt_chars else 0,
        'avgRetrievalElapsedMs': round(sum(elapsed_ms_list) / len(elapsed_ms_list), 2) if elapsed_ms_list else 0.0,
        'skillsByStatus': dict(status_breakdown),
        'skillsByEvidenceState': dict(evidence_breakdown),
        'staleSkillCount': stale_count,
        'consolidatedSkillCount': consolidated_count,
        'caseCount': case_count,
        'linkCount': link_count,
        'linksByRelation': links_by_relation,
        'totalSkillImpressions': total_impressions,
        'overallAdoptionRate': overall_adoption,
        'overallHelpfulRate': overall_helpful,
        'overallErrorRate': overall_error,
        'reuseIntensity': reuse_intensity,
        'perSkill': per_skill,
    }
