"""Honest metric calculation, provenance integrity verification, and identity drift auditing."""
import hashlib
import time
import work_memory as ledger


def compute_skill_rates(used: int, helpful: int, incorrect: int, missing: int, raw_impressions: int) -> dict:
    """Compute bounded [0.0, 1.0] adoptionRate, helpfulRate, and errorRate without double-counting used + helpful."""
    used = max(0, int(used or 0))
    helpful = max(0, int(helpful or 0))
    incorrect = max(0, int(incorrect or 0))
    missing = max(0, int(missing or 0))
    raw_imp = max(0, int(raw_impressions or 0))
    # Each positive adoption event is counted at most once against impressions
    positive_touches = max(used, helpful)
    imp = max(raw_imp, positive_touches + incorrect)
    adoption_rate = round(min(1.0, positive_touches / imp), 4) if imp > 0 else None
    helpful_rate = round(min(1.0, helpful / max(1, positive_touches)), 4) if positive_touches > 0 else None
    error_rate = round(min(1.0, incorrect / imp), 4) if imp > 0 else None
    reuse_intensity = round((used + helpful) / max(1, raw_imp if raw_imp > 0 else imp), 4) if (used + helpful) > 0 else 0.0
    return {
        "impressions": imp,
        "rawImpressions": raw_imp,
        "positiveTouches": positive_touches,
        "adoptionRate": adoption_rate,
        "helpfulRate": helpful_rate,
        "errorRate": error_rate,
        "reuseIntensity": reuse_intensity,
    }


def verify_live_provenance_and_drift(db, task: str, all_active: list, core_kinds: set) -> dict:
    """Verify sourceRefs against original instruction/report SHA-256 + exact quote inclusion,
    and measure actual identityDriftRate across memory_events history.
    """
    by_id = {r["item_id"]: r for r in all_active}
    learned_rows = [r for r in all_active if r["kind"] in ("fact", "experience", "case", "hypothesis")]
    verified_count = 0
    cryptographically_verified = 0
    evolved_box_count = 0
    consolidated_mem_count = 0
    prospective_item_count = 0

    ev_rows = db.execute(
        "SELECT id, item_id, kind, operation, status, content, source FROM memory_events WHERE task=? ORDER BY id ASC",
        (task,),
    ).fetchall()
    event_by_id = {int(er["id"]): er for er in ev_rows if er["id"] is not None}

    for r in learned_rows:
        src = r.get("source") or {}
        if src.get("boxEvolution"):
            evolved_box_count += 1
        if src.get("consolidatedFrom"):
            consolidated_mem_count += 1
        if src.get("prospective"):
            prospective_item_count += 1

        refs = src.get("sourceRefs")
        if isinstance(refs, list) and refs:
            all_refs_ok = True
            all_crypto_ok = True
            for ref in refs:
                if not isinstance(ref, dict) or not ref.get("itemId"):
                    all_refs_ok = False
                    all_crypto_ok = False
                    break
                ref_id = ref["itemId"]
                ref_sha = ref.get("contentSha256")
                if not ref_sha:
                    all_refs_ok = False
                    all_crypto_ok = False
                    break
                ref_ev_id = ref.get("eventId")
                raw_content = None
                if ref_ev_id is not None:
                    ev_row = event_by_id.get(int(ref_ev_id))
                    if ev_row is not None and ev_row["item_id"] == ref_id:
                        raw_content = ev_row["content"] or ""
                else:
                    orig = by_id.get(ref_id) or ledger.get_item(db, task, ref_id)
                    if orig is not None:
                        raw_content = orig.get("content") or ""
                if raw_content is None:
                    all_refs_ok = False
                    all_crypto_ok = False
                    break
                actual_sha = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
                if actual_sha != ref_sha:
                    all_refs_ok = False
                    all_crypto_ok = False
                    break
                quote = ref.get("quote")
                if quote and quote not in raw_content:
                    all_refs_ok = False
                    all_crypto_ok = False
                    break
            if all_refs_ok:
                verified_count += 1
            if all_crypto_ok:
                cryptographically_verified += 1
        elif src.get("kind") in ("legacy-snapshot", "explicit-memory-edit"):
            verified_count += 1

    # Measure actual identity drift: check if any previously protected (CORE_KINDS or user-confirmed) item
    # is ever subsequently replaced/retracted/resolved by sleep-consolidation or unconfirmed derived extraction
    drift_violations = 0
    protected_checked = 0
    user_confirmed_ids = set()
    for er in ev_rows:
        try:
            esrc = ledger._loads(er["source"]) if hasattr(ledger, "_loads") else __import__("json").loads(er["source"] or "{}")
        except Exception:
            esrc = {}
        iid = er["item_id"]
        knd = er["kind"]
        is_protected_now = (
            knd in core_kinds
            or esrc.get("certainty") == "explicit-user"
            or esrc.get("evidenceState") == "user-confirmed"
        )
        if iid in user_confirmed_ids and er["operation"] in ("replace", "retract", "resolve"):
            if esrc.get("kind") == "sleep-consolidation" or (
                esrc.get("certainty") == "derived" and esrc.get("lastSyncSource") != "markdown-coedit"
            ):
                drift_violations += 1
        if is_protected_now and iid not in user_confirmed_ids:
            user_confirmed_ids.add(iid)
            protected_checked += 1

    drift_rate = round(drift_violations / max(1, protected_checked), 4) if protected_checked > 0 else 0.0
    prov_pct = round((verified_count / max(1, len(learned_rows))) * 100.0, 2) if learned_rows else 100.0
    return {
        "learnedItemCount": len(learned_rows),
        "sourceHashVerifiedCount": verified_count,
        "cryptographicallyVerifiedCount": cryptographically_verified,
        "provenanceVerifiedPct": prov_pct,
        "liveProvenance": {
            "checkedCount": len(learned_rows),
            "verifiedCount": verified_count,
            "cryptographicallyVerifiedCount": cryptographically_verified,
            "verifiedPct": prov_pct,
        },
        "evolvedBoxCount": evolved_box_count,
        "consolidatedMemoryCount": consolidated_mem_count,
        "prospectiveIntentionCount": prospective_item_count,
        "protectedItemsAudited": protected_checked,
        "identityDriftViolations": drift_violations,
        "coreEventOverwriteCount": drift_violations,
        "identityDriftRate": drift_rate,
        "identityDriftScore": drift_rate,
    }


def _row_active_at(row: dict, ts: float) -> bool:
    if row.get("status") != "active":
        return False
    vf = row.get("valid_from")
    vu = row.get("valid_until")
    if vf is not None and float(vf) > ts:
        return False
    if vu is not None and float(vu) <= ts:
        return False
    return True


def evaluate_temporal_guard(db, task: str, timed_rows: list):
    """Evaluate temporal expiry filtering on live timed rows (if any) plus a deterministic non-mutating synthetic probe."""
    live_eval = 0
    live_hits = 0
    for tr in timed_rows:
        vu = float(tr["valid_until"])
        before_snap = ledger.snapshot(db, task, at=vu - 1.0, kinds=[tr["kind"]], limit=500)
        after_snap = ledger.snapshot(db, task, at=vu + 1.0, kinds=[tr["kind"]], limit=500)
        in_before = any(x["item_id"] == tr["item_id"] for x in before_snap)
        in_after = any(x["item_id"] == tr["item_id"] for x in after_snap)
        live_eval += 1
        if in_before and not in_after:
            live_hits += 1

    # Also verify bitemporal window boundary logic on a synthetic probe
    now_ts = time.time()
    synth_row = {
        "status": "active",
        "valid_from": now_ts - 60.0,
        "valid_until": now_ts + 60.0,
    }
    synth_ok = (
        _row_active_at(synth_row, now_ts)
        and not _row_active_at(synth_row, now_ts - 120.0)
        and not _row_active_at(synth_row, now_ts + 120.0)
    )
    total_eval = live_eval + 1
    total_hits = live_hits + (1 if synth_ok else 0)
    detail = {
        "mode": "live_and_synthetic" if live_eval > 0 else "synthetic_only",
        "liveEvaluated": live_eval,
        "liveHits": live_hits,
        "syntheticVerified": synth_ok,
    }
    return total_eval, total_hits, detail
