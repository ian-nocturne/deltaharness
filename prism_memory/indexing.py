"""True O(postings) inverted index, active row snapshot cache, and O(N*k) incremental link graph."""
from collections import Counter
import math
import re
import time

import work_memory as ledger

_WORD_RE = re.compile(r"[^\W_]+")
_SNAKE_IDENT_RE = re.compile(r"[a-z0-9가-힣]+(?:_[a-z0-9가-힣]+)+")
_HANGUL_CHAR_RE = re.compile(r"[가-힣]")
_EMPTY_COUNTER = Counter()

_TASK_ROW_SNAPSHOT_CACHE = {}
_MAX_ROW_SNAPSHOT_CACHE = 32
_LINK_SYNC_STATE_CACHE = {}


class SparseCountsProxy:
    """Zero-allocation sparse proxy for per-document query-term Counters.
    Supports `counts[i]`, `iter(counts)`, `len(counts)`, and `.max_matched` in O(|candidates|) instead of O(N).
    """
    __slots__ = ("_map", "_n", "max_matched", "candidate_indices")

    def __init__(self, counts_map: dict, total_docs: int, candidate_indices: set):
        self._map = counts_map
        self._n = total_docs
        self.candidate_indices = candidate_indices
        self.max_matched = max((len(c) for c in counts_map.values()), default=0)

    def __getitem__(self, idx: int):
        return self._map.get(idx, _EMPTY_COUNTER)

    def __len__(self):
        return self._n

    def __iter__(self):
        # Iterates only over non-empty candidate Counters when used in generator expressions like `max((len(c) for c in counts))`
        return iter(self._map.values())


def db_identity_key(db) -> str:
    try:
        db_list = db.execute("PRAGMA database_list").fetchall()
        if db_list and len(db_list[0]) > 2 and db_list[0][2]:
            return str(db_list[0][2])
    except Exception:
        pass
    return str(id(db))


def invalidate_task_caches(task: str = None, inverted_cache: dict = None):
    """Invalidate row snapshot and link sync state caches for a task (or all tasks if None)."""
    if task is None:
        _TASK_ROW_SNAPSHOT_CACHE.clear()
        _LINK_SYNC_STATE_CACHE.clear()
        if inverted_cache is not None:
            inverted_cache.clear()
        return
    for k in [k for k in _TASK_ROW_SNAPSHOT_CACHE if isinstance(k, tuple) and len(k) > 1 and k[1] == task]:
        _TASK_ROW_SNAPSHOT_CACHE.pop(k, None)
    for k in [k for k in _LINK_SYNC_STATE_CACHE if isinstance(k, tuple) and len(k) > 1 and k[1] == task]:
        _LINK_SYNC_STATE_CACHE.pop(k, None)
    if inverted_cache is not None:
        inverted_cache.pop(task, None)
        for k in [k for k in inverted_cache if isinstance(k, tuple) and len(k) > 1 and k[1] == task]:
            inverted_cache.pop(k, None)


def _fast_task_event_sig(db, task: str):
    """O(log N) top-of-index lookup using idx_memory_events_task_observed instead of scanning all task rows."""
    top_ev = db.execute(
        "SELECT id, observed_at FROM memory_events WHERE task=? ORDER BY observed_at DESC, id DESC LIMIT 1",
        (task,),
    ).fetchone()
    if top_ev is not None:
        return int(top_ev[0] or 0), round(float(top_ev[1] or 0.0), 4)
    stat = db.execute(
        "SELECT COALESCE(max(event_id), 0), COALESCE(max(observed_at), 0.0) FROM memory_items WHERE task=? AND status='active'",
        (task,),
    ).fetchone()
    return int(stat[0] or 0), round(float(stat[1] or 0.0), 4)


def get_cached_task_rows(db, task: str, recall_kinds, snapshot_limit: int = 5000):
    """Fetch active recall rows for a task with O(1) snapshot caching keyed by (db_key, task, max_event_id, max_observed_at)."""
    db_key = db_identity_key(db)
    max_ev, max_obs = _fast_task_event_sig(db, task)
    ckey = (db_key, task, max_ev, max_obs)
    cached = _TASK_ROW_SNAPSHOT_CACHE.get(ckey)
    now = time.time()
    if cached is not None:
        raw_items, raw_total, has_temporal = cached
        if not has_temporal:
            return raw_items, raw_total
        # Filter temporal bounds in memory in microseconds
        filtered = [
            r for r in raw_items
            if (r.get("valid_from") is None or r["valid_from"] <= now)
            and (r.get("valid_until") is None or r["valid_until"] > now)
        ]
        return filtered, len(filtered)

    # Fetch without current-time valid_from/valid_until SQL filter if we want to inspect temporal bounds,
    # or use ledger.snapshot with higher limit (25,000 instead of 5,000).
    own = ledger.snapshot(
        db, task, None, None, kinds=recall_kinds, limit=snapshot_limit,
        before=None, after=None, with_count=True,
    )
    result = [
        r for r in own["items"]
        if not (r["kind"] in ("experience", "case") and r["source"].get("evidenceState") == "needs_revision")
    ]
    total = max(0, own["count"] - (len(own["items"]) - len(result)))
    has_temporal = any(
        r.get("valid_until") is not None
        or (r.get("valid_from") is not None and float(r.get("valid_from") or 0.0) > float(r.get("event_at") or 0.0) + 0.001)
        for r in result
    )
    # Check if there are any future-scheduled active items in DB that could become valid later
    if not has_temporal:
        fut = db.execute(
            "SELECT 1 FROM memory_items WHERE task=? AND status='active' "
            "AND (valid_until IS NOT NULL OR (valid_from IS NOT NULL AND valid_from > event_at + 0.001)) LIMIT 1",
            (task,),
        ).fetchone()
        has_temporal = bool(fut)

    if not has_temporal:
        if len(_TASK_ROW_SNAPSHOT_CACHE) >= _MAX_ROW_SNAPSHOT_CACHE:
            _TASK_ROW_SNAPSHOT_CACHE.pop(next(iter(_TASK_ROW_SNAPSHOT_CACHE)), None)
        _TASK_ROW_SNAPSHOT_CACHE[ckey] = (result, total, False)
    return result, total


def _row_sig_tuple(r: dict):
    src = r.get("source") or {}
    ents = tuple(src.get("entities") or ())
    aliases = tuple(src.get("aliases") or ())
    box_kws = tuple((src.get("boxEvolution") or {}).get("contextKeywords") or ())
    prosp_st = (src.get("prospective") or {}).get("status") if isinstance(src.get("prospective"), dict) else None
    return (
        r.get("item_id", ""),
        r.get("event_id", 0),
        hash(r.get("content", "")),
        ents,
        aliases,
        box_kws,
        prosp_st,
    )


def build_or_get_lexical_index(rows, task: str, cached_doc_tokens_fn, cache_dict: dict, max_cache: int = 32, db_key: str = ""):
    """Build or retrieve an in-memory Inverted Posting Index with token, 3-gram substring, label, and prospective postings."""
    if not isinstance(rows, list) or not rows:
        return {
            "entries": [],
            "avg_len": 1.0,
            "doc_freq": {},
            "postings": {},
            "substr_postings": {},
            "label_docs": [],
            "label_postings": {},
            "prospective_indices": [],
            "item_index_by_id": {},
            "norm_texts": [],
            "revision": 0,
        }
    task_name = task or rows[0].get("task", "")
    n_r = len(rows)
    mid_r = rows[n_r // 2]
    sample_evs = [int(r.get("event_id") or 0) for r in rows[:12]]
    sample_evs.append(int(mid_r.get("event_id") or 0))
    sample_evs.append(int(rows[-1].get("event_id") or 0))
    max_ev = max(sample_evs) if sample_evs else 0
    sig = (
        db_key,
        task_name,
        n_r,
        max_ev,
        tuple(_row_sig_tuple(r) for r in rows[:12]),
        _row_sig_tuple(mid_r),
        _row_sig_tuple(rows[-1]),
    )
    full_row_sig = tuple(_row_sig_tuple(r) for r in rows)
    cached = cache_dict.get(sig)
    if cached is not None and cached.get("_full_row_sig") == full_row_sig:
        cached["_rows_ref"] = rows
        if task_name:
            cache_dict[(db_key, task_name)] = cached
            cache_dict[task_name] = cached
        return cached
    max_ev = max((int(r.get("event_id") or 0) for r in rows), default=0)
    entries = []
    norm_texts = []
    doc_freq = Counter()
    postings = {}
    substr_postings = {}
    label_docs = []
    label_postings = {}
    prospective_indices = []
    item_index_by_id = {}
    total_len = 0

    for idx, r in enumerate(rows):
        item_index_by_id.setdefault(r.get("item_id", ""), []).append(idx)
        full_c, length, norm_t = cached_doc_tokens_fn(r["content"])
        total_len += length
        norm_texts.append(norm_t)
        src_i = r.get("source") or {}
        box_kws = (src_i.get("boxEvolution") or {}).get("contextKeywords") or []
        raw_labels = list(src_i.get("entities", [])) + list(src_i.get("aliases", [])) + list(box_kws)
        norm_labels = [
            ledger._normalize(lbl).strip()
            for lbl in raw_labels
            if len(ledger._normalize(lbl).strip()) >= 2
        ]
        if norm_labels:
            label_docs.append((idx, norm_labels))
            seen_l_toks = set()
            for lbl in norm_labels:
                label_postings.setdefault(lbl, []).append(idx)
                for l_tok in _WORD_RE.findall(lbl):
                    if len(l_tok) >= 2 and l_tok not in seen_l_toks:
                        seen_l_toks.add(l_tok)
                        label_postings.setdefault(l_tok, []).append(idx)
        prosp = src_i.get("prospective")
        if isinstance(prosp, dict) and prosp.get("status") != "fulfilled":
            prospective_indices.append(idx)
        entries.append((full_c, length, norm_t, norm_labels))
        seen_sub = set()
        for tok, tf in full_c.items():
            doc_freq[tok] += 1
            postings.setdefault(tok, []).append((idx, tf))
            # Index 3-char substrings for tokens >= 4 chars so partial/compound matches remain O(postings)
            if len(tok) >= 4:
                for si in range(min(6, len(tok) - 2)):
                    sub3 = tok[si:si + 3]
                    if sub3 not in seen_sub:
                        seen_sub.add(sub3)
                        substr_postings.setdefault(sub3, []).append(idx)

    idx_obj = {
        "entries": entries,
        "norm_texts": norm_texts,
        "avg_len": total_len / max(1, len(rows)),
        "doc_freq": dict(doc_freq),
        "postings": postings,
        "substr_postings": substr_postings,
        "label_docs": label_docs,
        "label_postings": label_postings,
        "prospective_indices": prospective_indices,
        "item_index_by_id": item_index_by_id,
        "revision": max_ev,
        "_rows_ref": rows,
        "_full_row_sig": full_row_sig,
    }
    if len(cache_dict) >= max_cache:
        cache_dict.pop(next(iter(cache_dict)), None)
    cache_dict[sig] = idx_obj
    if task_name:
        cache_dict[(db_key, task_name)] = idx_obj
        cache_dict[task_name] = idx_obj
    return idx_obj


def score_lexical_postings(rows, lex_index, normalized, terms, pairs, qwords, fast_lexical_fn, max_candidates: int = 120, approximate: bool = False):
    """Exact O(postings) lexical, BM25, and entity scorer.
    Accumulates exact BM25 across posting lists so high-TF short documents are never dropped behind low-TF documents.
    """
    n_rows = len(rows)
    doc_entries = lex_index["entries"]
    norm_texts = lex_index.get("norm_texts") or [nt for _, _, nt, _ in doc_entries]
    average = max(1.0, float(lex_index["avg_len"] or 1.0))
    doc_freq_map = lex_index["doc_freq"]
    postings = lex_index["postings"]
    substr_postings = lex_index.get("substr_postings") or {}
    label_docs = lex_index.get("label_docs") or []
    label_postings = lex_index.get("label_postings") or {}

    if normalized:
        snake_toks = _SNAKE_IDENT_RE.findall(normalized)
        if snake_toks:
            qwords = set(qwords) | set(snake_toks)

    frequencies = {term: doc_freq_map.get(term, 0) for term in qwords}
    idf_by_term = {}
    for term in qwords:
        df_t = frequencies.get(term, 0)
        idf_by_term[term] = math.log(1.0 + (n_rows - df_t + 0.5) / (df_t + 0.5))

    counts_map = {}
    doc_bm25_exact = {}
    candidate_indices = set()

    # 1. Gather exact token & Hangul bigram postings for qwords and accumulate exact BM25 per document
    ordered_qwords = sorted(qwords, key=lambda w: frequencies.get(w, 0))
    for i_q, term in enumerate(ordered_qwords):
        plist = postings.get(term)
        if not plist:
            continue
        idf_val = idf_by_term[term]
        only_existing = bool(approximate and n_rows >= 64 and i_q > 0 and len(candidate_indices) >= 1 and len(plist) > n_rows * 0.35)
        if only_existing and len(candidate_indices) <= 64:
            for doc_idx in list(counts_map.keys()):
                tf = doc_entries[doc_idx][0].get(term, 0)
                if tf:
                    counts_map[doc_idx][term] = tf
                    denom_len = 1.2 * (0.25 + 0.75 * doc_entries[doc_idx][1] / average)
                    doc_bm25_exact[doc_idx] = doc_bm25_exact.get(doc_idx, 0.0) + (idf_val * tf * 2.2 / (tf + denom_len))
            continue
        for doc_idx, tf in plist:
            if only_existing and doc_idx not in counts_map:
                continue
            candidate_indices.add(doc_idx)
            c_doc = counts_map.get(doc_idx)
            if c_doc is None:
                c_doc = Counter()
                counts_map[doc_idx] = c_doc
            c_doc[term] = tf
            denom_len = 1.2 * (0.25 + 0.75 * doc_entries[doc_idx][1] / average)
            doc_bm25_exact[doc_idx] = doc_bm25_exact.get(doc_idx, 0.0) + (idf_val * tf * 2.2 / (tf + denom_len))

    # 2. Gather postings for query terms and Hangul pairs
    if not approximate or len(candidate_indices) < max_candidates:
        for t in terms:
            plist = postings.get(t)
            if plist:
                if not approximate or n_rows < 64 or len(plist) <= n_rows * 0.35 or not candidate_indices:
                    for doc_idx, _ in plist:
                        candidate_indices.add(doc_idx)
            elif len(t) >= 3 and not candidate_indices:
                slist = substr_postings.get(t[:3])
                if slist and (not approximate or n_rows < 64 or len(slist) <= n_rows * 0.35):
                    candidate_indices.update(slist)

        for p in pairs:
            plist = postings.get(p)
            if plist and (not approximate or n_rows < 64 or len(plist) <= n_rows * 0.35 or not candidate_indices):
                for doc_idx, _ in plist:
                    candidate_indices.add(doc_idx)

    # 3. Evaluate entity/alias overlap via label_postings at scale (or label_docs for small tasks)
    entity = {}
    if normalized and label_docs:
        if len(label_docs) <= 32 or not label_postings:
            for doc_idx, norm_labels in label_docs:
                overlap = sum(lbl in normalized for lbl in norm_labels)
                if overlap:
                    entity[doc_idx] = overlap
                    candidate_indices.add(doc_idx)
        else:
            cand_label_docs = set()
            for tok in set(terms) | qwords:
                l_hits = label_postings.get(tok)
                if l_hits and (not approximate or len(l_hits) <= max(120, n_rows // 4)):
                    cand_label_docs.update(l_hits)
            for doc_idx in cand_label_docs:
                norm_labels = doc_entries[doc_idx][3]
                overlap = sum(lbl in normalized for lbl in norm_labels)
                if overlap:
                    entity[doc_idx] = overlap
                    candidate_indices.add(doc_idx)

    # 4. Fallback for tiny 2-char substring queries with 0 token postings
    if normalized and not candidate_indices and n_rows <= 600:
        for idx, nt in enumerate(norm_texts):
            if normalized in nt or any(t in nt for t in terms):
                candidate_indices.add(idx)

    # 5. Select candidates: in exact mode (approximate=False), score all candidate_indices so phrase bonuses are never dropped
    if approximate and len(candidate_indices) > max_candidates:
        ranked_cands = sorted(
            candidate_indices,
            key=lambda idx: (
                -(100.0 if idx in entity else 0.0) - doc_bm25_exact.get(idx, 0.0),
                -len(counts_map.get(idx, ())),
                idx,
            ),
        )[:max_candidates]
        eval_indices = sorted(set(ranked_cands) | set(entity.keys()))
    else:
        eval_indices = sorted(candidate_indices)

    # 6. Score eval_indices using precomputed exact BM25 + fast_lexical phrase/term bonus
    lexical = {}
    anchors = {}
    for i in eval_indices:
        full_c, _, norm_i, _ = doc_entries[i]
        base, anchor = fast_lexical_fn(full_c, norm_i, normalized, terms, pairs)
        bm25 = doc_bm25_exact.get(i, 0.0)
        if base or bm25:
            lexical[i] = bm25 + base / 20.0
            anchors[i] = anchor

    counts_proxy = SparseCountsProxy(counts_map, n_rows, set(eval_indices))
    return frequencies, counts_proxy, norm_texts, lexical, anchors, entity


def score_decor_facets_postings(query_facets, lex_index, ranked, signals, coverage_by_idx):
    """Score multi-intent query facets in O(postings) instead of scanning all N rows."""
    if len(query_facets) < 2:
        return
    postings = lex_index["postings"]
    substr_postings = lex_index.get("substr_postings") or {}
    doc_entries = lex_index["entries"]
    for facet in query_facets:
        f_terms = [t for t in (facet.get("terms") or []) if len(t) >= 2]
        if not f_terms:
            continue
        f_candidates = set()
        for t in f_terms:
            for doc_idx, _ in postings.get(t, ()):
                f_candidates.add(doc_idx)
            if len(t) >= 3:
                f_candidates.update(substr_postings.get(t[:3], ()))
        for i in f_candidates:
            full_c, _, norm_i, _ = doc_entries[i]
            matched_ft = [t for t in f_terms if t in full_c or t in norm_i]
            if matched_ft:
                f_cov = len(matched_ft) / len(f_terms)
                if f_cov >= 0.50:
                    f_boost = round(0.026 * f_cov, 6)
                    ranked[i] = ranked.get(i, 0.0) + f_boost
                    signals.setdefault(i, {})[f"facet{facet['facetId']}Coverage"] = round(f_cov, 4)
                    coverage_by_idx[i] = max(coverage_by_idx.get(i, 0.0), round(f_cov * 0.85, 6))


def check_link_sync_needed(db, task: str, ensure_schema_fn=None):
    """Check whether memory_items or memory_skills changed since the last sync_task_links run.
    If `ensure_schema_fn` is passed, calls `ensure_schema_fn(db)` and returns a bool (`need_sync`).
    Otherwise returns `(need_sync, cached_result, sig)` for `sync_task_links`.
    """
    if ensure_schema_fn is not None:
        ensure_schema_fn(db)
    db_key = db_identity_key(db)
    ckey = (db_key, task)
    cached = _LINK_SYNC_STATE_CACHE.get(ckey)

    # Fast O(log N) top-of-index check when we already have a cached link state
    ev_id, ev_obs = _fast_task_event_sig(db, task)
    sk_top = db.execute(
        "SELECT id, observed_at FROM memory_skill_events WHERE task=? ORDER BY observed_at DESC, id DESC LIMIT 1",
        (task,),
    ).fetchone()
    sk_id = int(sk_top[0] or 0) if sk_top is not None else 0
    sk_obs = round(float(sk_top[1] or 0.0), 3) if sk_top is not None else 0.0
    has_any_link = db.execute("SELECT 1 FROM memory_links WHERE task=? LIMIT 1", (task,)).fetchone() is not None
    sig = (ev_id, round(ev_obs, 3), sk_id, sk_obs)

    expected_links = int((cached[1] or {}).get("directedLinks") or 0) if cached is not None else -1
    if cached is not None and cached[0] == sig and (has_any_link or expected_links == 0 or (ev_id == 0 and sk_id == 0)):
        if ensure_schema_fn is not None:
            return False
        return False, cached[1], sig
    if ensure_schema_fn is not None:
        return True
    return True, None, sig


def record_link_sync_state(db, task: str, sig, result_dict: dict):
    db_key = db_identity_key(db)
    _LINK_SYNC_STATE_CACHE[(db_key, task)] = (sig, result_dict)


def build_shared_edges_inverted(nodes: dict, node_ids: list, add_pair_fn, max_bucket_fanout: int = 80):
    """Build shared_file, shared_entity, and shared_source edges in O(N * k) via inverted token buckets
    instead of O(N^2) all-pairs comparison.
    """
    if len(node_ids) <= 60:
        # For small tasks (N <= 60), exact pairwise is <= 1,770 iterations and preserves exact evidence lists
        for i, a_id in enumerate(node_ids):
            a = nodes[a_id]
            for b_id in node_ids[i + 1:]:
                b = nodes[b_id]
                shared_files = sorted(a["files"] & b["files"])
                if shared_files:
                    add_pair_fn(a_id, b_id, "shared_file", 0.85, ",".join(shared_files[:5]))
                shared_entities = sorted(a["entities"] & b["entities"])
                if shared_entities:
                    add_pair_fn(a_id, b_id, "shared_entity", 0.75, ",".join(shared_entities[:5]))
                shared_sources = sorted(a["sourceRefs"] & b["sourceRefs"])
                if shared_sources:
                    add_pair_fn(a_id, b_id, "shared_source", 0.80, ",".join(shared_sources[:5]))
        return

    # For large tasks (N > 60), use inverted buckets to avoid O(N^2) explosion
    for attr_key, relation, weight in (
        ("files", "shared_file", 0.85),
        ("entities", "shared_entity", 0.75),
        ("sourceRefs", "shared_source", 0.80),
    ):
        buckets = {}
        for nid in node_ids:
            for tok in nodes[nid].get(attr_key) or ():
                buckets.setdefault(tok, []).append(nid)
        pair_tokens = {}
        for tok, members in buckets.items():
            if len(members) < 2:
                continue
            if len(members) <= max_bucket_fanout:
                for idx_a, a_id in enumerate(members):
                    for b_id in members[idx_a + 1:]:
                        pkey = (a_id, b_id) if a_id < b_id else (b_id, a_id)
                        tlist = pair_tokens.setdefault(pkey, [])
                        if len(tlist) < 5:
                            tlist.append(tok)
            else:
                # Bounded O(|members|) local window (k=2) so large buckets remain sparse and fast
                for idx_a, a_id in enumerate(members):
                    for b_id in members[idx_a + 1 : idx_a + 3]:
                        pkey = (a_id, b_id) if a_id < b_id else (b_id, a_id)
                        tlist = pair_tokens.setdefault(pkey, [])
                        if len(tlist) < 5:
                            tlist.append(tok)
        for (a_id, b_id), tlist in pair_tokens.items():
            add_pair_fn(a_id, b_id, relation, weight, ",".join(sorted(tlist)[:5]))
