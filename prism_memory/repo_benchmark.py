"""Independent Multi-Epoch Repository Evolution Benchmark Suite (4 Baselines x 4 Ablations) for PRISM Memory v8.1.

Evaluates agent memory and procedural skill engines across 4 realistic repository evolution epochs:
- Epoch 1 (v1_baseline): Initial codebase with verified procedural skills and pitfall cases + directive turn trap.
- Epoch 2 (v2_compatible_refactor): Backward-compatible function evolution + unrelated symbol edit + execution re-verification.
- Epoch 3 (v3_breaking_api_and_pitfall): Breaking call-contract signature changes + symbol deletion + unverified disk edit trap.
- Epoch 4 (v4_reverification_and_isolation): Execution-backed skill verification (pass & fail -> pitfall case) + Multi-DB isolation.
"""
from __future__ import annotations
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional

import work_memory as ledger
import memory_skills as skills_ledger
import memory_intelligence as memory
from .standalone import StandaloneStore
from .paths import resolve_embedding_model_dir
from .indexing import invalidate_task_caches


_TOKENIZER = None
_TOKENIZER_LOADED = False


def count_real_tokens(text: str) -> Dict[str, Any]:
    """Count tokens using the local E5 HuggingFace Tokenizer if available, plus UTF-8 bytes and chars."""
    global _TOKENIZER, _TOKENIZER_LOADED
    s = str(text or "")
    if not _TOKENIZER_LOADED:
        _TOKENIZER_LOADED = True
        try:
            model_dir = resolve_embedding_model_dir()
            if model_dir and (model_dir / "tokenizer.json").is_file():
                from tokenizers import Tokenizer
                _TOKENIZER = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        except Exception:
            _TOKENIZER = None
    if _TOKENIZER is not None:
        try:
            enc = _TOKENIZER.encode(s)
            return {
                "tokens": len(enc.ids),
                "chars": len(s),
                "utf8Bytes": len(s.encode("utf-8")),
                "tokenizer": "multilingual-e5-small",
            }
        except Exception:
            pass
    # Deterministic fallback approximation (~3.2 chars/token for mixed Korean/English)
    approx = max(1, int(round(len(s.encode("utf-8")) / 3.8))) if s else 0
    return {
        "tokens": approx,
        "chars": len(s),
        "utf8Bytes": len(s.encode("utf-8")),
        "tokenizer": "utf8-approx",
    }


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round((pct / 100.0) * (len(sorted_vals) - 1)))))
    return round(float(sorted_vals[idx]), 3)


def _write_epoch1_repo(repo_dir: Path) -> Dict[str, Path]:
    files = {
        "payment_gateway.py": repo_dir / "payment_gateway.py",
        "webhook_router.py": repo_dir / "webhook_router.py",
        "db_migrator.py": repo_dir / "db_migrator.py",
        "auth_token.py": repo_dir / "auth_token.py",
    }
    files["payment_gateway.py"].write_text(
        "def charge_customer(user_id: str, amount_cents: int, currency: str = 'KRW') -> dict:\n"
        "    if not isinstance(amount_cents, int) or amount_cents <= 0:\n"
        "        raise ValueError('amount_cents must be positive integer')\n"
        "    return {'ok': True, 'userId': user_id, 'amountCents': amount_cents, 'currency': currency}\n\n"
        "def refund_payment(charge_id: str, reason: str = '') -> dict:\n"
        "    return {'ok': True, 'chargeId': charge_id, 'reason': reason}\n",
        encoding="utf-8",
    )
    files["webhook_router.py"].write_text(
        "def dispatch_webhook(event_type: str, payload_dict: dict, signature_header: str = '') -> bool:\n"
        "    if not signature_header:\n"
        "        raise ValueError('missing HMAC signature_header')\n"
        "    return bool(event_type and isinstance(payload_dict, dict))\n",
        encoding="utf-8",
    )
    files["db_migrator.py"].write_text(
        "def _format_banner(title: str) -> str:\n"
        "    return f'=== {title} ==='\n\n"
        "def run_migration(target_rev: int, dry_run: bool = True) -> dict:\n"
        "    return {'targetRev': int(target_rev), 'dryRun': bool(dry_run), 'status': 'ready'}\n",
        encoding="utf-8",
    )
    files["auth_token.py"].write_text(
        "def verify_jwt(token_str: str, audience: str = 'api') -> dict:\n"
        "    if token_str.startswith('none.'):\n"
        "        raise ValueError('alg=none is forbidden')\n"
        "    return {'sub': 'u1', 'aud': audience, 'valid': True}\n",
        encoding="utf-8",
    )
    return files


def _write_epoch2_compatible_refactor(files: Dict[str, Path]) -> None:
    # 1. payment_gateway.py: backward-compatible internal body change + optional default parameter
    files["payment_gateway.py"].write_text(
        "def charge_customer(user_id: str, amount_cents: int, currency: str = 'KRW', idempotency_key: str = None) -> dict:\n"
        "    if not isinstance(amount_cents, int) or amount_cents <= 0:\n"
        "        raise ValueError('amount_cents must be positive integer')\n"
        "    normalized_curr = str(currency or 'KRW').upper()\n"
        "    return {\n"
        "        'ok': True,\n"
        "        'userId': user_id,\n"
        "        'amountCents': amount_cents,\n"
        "        'currency': normalized_curr,\n"
        "        'idempotencyKey': idempotency_key or 'auto',\n"
        "    }\n\n"
        "def refund_payment(charge_id: str, reason: str = '') -> dict:\n"
        "    return {'ok': True, 'chargeId': charge_id, 'reason': reason}\n",
        encoding="utf-8",
    )
    # 2. db_migrator.py: edit ONLY unrelated helper `_format_banner`, leaving `run_migration` untouched!
    files["db_migrator.py"].write_text(
        "def _format_banner(title: str) -> str:\n"
        "    return f'[MIGRATION] {title.strip().upper()}'\n\n"
        "def run_migration(target_rev: int, dry_run: bool = True) -> dict:\n"
        "    return {'targetRev': int(target_rev), 'dryRun': bool(dry_run), 'status': 'ready'}\n",
        encoding="utf-8",
    )


def _write_epoch3_breaking_evolution(files: Dict[str, Path]) -> None:
    # 1. payment_gateway.py: BREAKING call-contract change on `charge_customer`
    files["payment_gateway.py"].write_text(
        "def charge_customer(account_uuid: str, money_obj: dict, *, idempotency_key: str) -> dict:\n"
        "    if not idempotency_key:\n"
        "        raise ValueError('idempotency_key is mandatory in v3')\n"
        "    return {'ok': True, 'accountUuid': account_uuid, 'money': money_obj, 'key': idempotency_key}\n",
        encoding="utf-8",
    )
    # 2. auth_token.py: BREAKING symbol deletion (`verify_jwt` removed, replaced by `verify_session_assertion`)
    files["auth_token.py"].write_text(
        "def verify_session_assertion(assertion_bytes: bytes, key_ring: dict) -> dict:\n"
        "    if not assertion_bytes or not key_ring:\n"
        "        raise ValueError('assertion_bytes and key_ring required')\n"
        "    return {'sub': 'u1', 'valid': True}\n",
        encoding="utf-8",
    )


def _seed_benchmark_task(store: StandaloneStore, task_id: str, owner: str) -> Dict[str, Any]:
    store.ensure_task(
        task_id,
        title="Payment & Webhook Service Evolution",
        owner=owner,
        project="bench-proj",
        goal="Maintain payment_gateway.py, webhook_router.py, db_migrator.py, and auth_token.py safely across API versions.",
    )
    src_text = (
        "결제·웹훅·마이그레이션·인증 모듈 표준 절차 및 주의사항: "
        "1) payment_gateway.py의 charge_customer(user_id, amount_cents, currency='KRW') 호출 전 정수 센트 단위를 검증하고 부동소수점(float) 금액 전달을 금지한다. "
        "2) webhook_router.py의 dispatch_webhook(event_type, payload_dict, signature_header)는 반드시 HMAC 서명 헤더와 타임스탬프 재전송(replay) 방지를 검증한다. "
        "3) db_migrator.py의 run_migration(target_rev, dry_run=True)은 프로덕션 반영 전 dry_run=True로 먼저 점검하고 활성 락을 확인한다. "
        "4) auth_token.py의 verify_jwt(token_str, audience='api')는 alg=none 토큰을 즉시 차단한다."
    )
    with store.db(True) as db:
        ledger.record(
            db, task_id, 1, "seed-inst-1", "instruction", "input:seed-1", "assert",
            src_text, {"kind": "user-instruction", "session": owner, "certainty": "explicit-user"}, time.time(),
        )
        t_row = db.execute("SELECT revision FROM tasks WHERE id=?", (task_id,)).fetchone()
        rev = int(t_row["revision"])

    entries = [
        {
            "kind": "constraint",
            "op": "assert",
            "id": "no-float-money",
            "content": "결제 금액은 반드시 정수 센트(int)만 허용하며 float 금액이나 검증 없는 강제 스키마 변경은 절대 금지한다.",
            "certainty": "explicit",
            "sourceRefs": [{"itemId": "input:seed-1", "quote": "부동소수점(float) 금액 전달을 금지한다"}],
        },
        {
            "kind": "experience",
            "op": "assert",
            "id": "exp:payment-charge-flow",
            "certainty": "derived",
            "sourceRefs": [{"itemId": "input:seed-1", "quote": "payment_gateway.py의 charge_customer(user_id, amount_cents, currency='KRW') 호출 전 정수 센트 단위를 검증"}],
            "experience": {
                "situation": "고객 카드 결제 승인 요청을 처리할 때 payment_gateway.py의 charge_customer를 호출하는 절차",
                "steps": [
                    "요청 금액이 정수(int) 센트 단위인지 먼저 검증한다.",
                    "payment_gateway.py의 charge_customer(user_id, amount_cents, currency='KRW') 함수를 호출해 승인 결과를 받는다.",
                ],
                "outcome": "정수 센트 단위로 안전하게 결제가 승인된다.",
                "conditions": "payment_gateway.py v1/v2 시그니처(user_id, amount_cents)가 유지되는 경우에 적용한다.",
                "counterexamples": "부동소수점(float) 금액이 들어오거나 v3 계좌 UUID 스키마로 변경된 경우에는 그대로 호출하지 않는다.",
            },
            "skill": {
                "id": "skill:payment-charge-flow",
                "name": "payment-charge-flow",
                "title": "고객 결제 승인 처리 (charge_customer)",
                "triggers": ["결제", "승인", "카드", "청구", "charge_customer", "payment_gateway.py"],
            },
        },
        {
            "kind": "experience",
            "op": "assert",
            "id": "exp:webhook-dispatch-flow",
            "certainty": "derived",
            "sourceRefs": [{"itemId": "input:seed-1", "quote": "webhook_router.py의 dispatch_webhook(event_type, payload_dict, signature_header)는 반드시 HMAC 서명 헤더"}],
            "experience": {
                "situation": "외부 가맹점 이벤트 알림을 보낼 때 webhook_router.py의 dispatch_webhook을 실행하는 절차",
                "steps": [
                    "페이로드의 HMAC 서명 헤더(signature_header)를 생성하고 비어 있지 않은지 확인한다.",
                    "webhook_router.py의 dispatch_webhook(event_type, payload_dict, signature_header)을 호출한다.",
                ],
                "outcome": "서명이 검증된 웹훅 이벤트만 안전하게 전송된다.",
                "conditions": "webhook_router.py의 dispatch_webhook 인터페이스를 사용할 때 적용한다.",
                "counterexamples": "signature_header가 누락되었거나 타임스탬프가 5분 이상 지난 재전송 요청은 발송하지 않는다.",
            },
            "skill": {
                "id": "skill:webhook-dispatch-flow",
                "name": "webhook-dispatch-flow",
                "title": "가맹점 웹훅 발송 (dispatch_webhook)",
                "triggers": ["웹훅", "알림", "전송", "서명", "dispatch_webhook", "webhook_router.py"],
            },
        },
        {
            "kind": "experience",
            "op": "assert",
            "id": "exp:db-migration-flow",
            "certainty": "derived",
            "sourceRefs": [{"itemId": "input:seed-1", "quote": "db_migrator.py의 run_migration(target_rev, dry_run=True)은 프로덕션 반영 전 dry_run=True로 먼저 점검"}],
            "experience": {
                "situation": "데이터베이스 스키마 버전을 올릴 때 db_migrator.py의 run_migration을 실행하는 절차",
                "steps": [
                    "먼저 db_migrator.py의 run_migration(target_rev, dry_run=True)을 실행해 마이그레이션 계획을 점검한다.",
                    "활성 락이 없는지 확인한 뒤 dry_run=False로 본 마이그레이션을 수행한다.",
                ],
                "outcome": "무중단으로 스키마 리비전이 갱신된다.",
                "conditions": "db_migrator.py의 run_migration 시그니처가 유효하고 락이 없을 때 적용한다.",
                "counterexamples": "배치 정산 트랜잭션이 실행 중이거나 dry_run 점검을 생략한 상태에서는 실행하지 않는다.",
            },
            "skill": {
                "id": "skill:db-migration-flow",
                "name": "db-migration-flow",
                "title": "DB 스키마 마이그레이션 (run_migration)",
                "triggers": ["스키마", "마이그레이션", "버전", "디비", "run_migration", "db_migrator.py"],
            },
        },
        {
            "kind": "experience",
            "op": "assert",
            "id": "exp:jwt-auth-flow",
            "certainty": "derived",
            "sourceRefs": [{"itemId": "input:seed-1", "quote": "auth_token.py의 verify_jwt(token_str, audience='api')는 alg=none 토큰을 즉시 차단한다"}],
            "experience": {
                "situation": "API 요청 헤더의 베어러 토큰을 검증할 때 auth_token.py의 verify_jwt를 호출하는 절차",
                "steps": [
                    "토큰 문자열이 'none.' 알고리즘 프리픽스를 쓰지 않는지 확인한다.",
                    "auth_token.py의 verify_jwt(token_str, audience='api')를 호출해 클레임을 검증한다.",
                ],
                "outcome": "위조되지 않은 API 토큰만 통과시킨다.",
                "conditions": "auth_token.py에 verify_jwt 함수가 존재하는 v1/v2 환경에서 적용한다.",
                "counterexamples": "alg=none 토큰이거나 auth_token.py가 v3 verify_session_assertion으로 교체된 경우 구형 verify_jwt를 쓰지 않는다.",
            },
            "skill": {
                "id": "skill:jwt-auth-flow",
                "name": "jwt-auth-flow",
                "title": "API JWT 토큰 검증 (verify_jwt)",
                "triggers": ["인증", "토큰", "헤더", "베어러", "verify_jwt", "auth_token.py"],
            },
        },
        {
            "kind": "case",
            "op": "assert",
            "id": "case:pitfall-float-currency",
            "certainty": "explicit",
            "sourceRefs": [{"itemId": "input:seed-1", "quote": "부동소수점(float) 금액 전달을 금지한다"}],
            "case": {
                "title": "결제 금액 부동소수점(float) 반올림 오차 장애",
                "situation": "고객 결제 승인 또는 환불 금액을 계산해 payment_gateway.py에 전달할 때",
                "symptom": "float(19.99 * 100) 연산으로 1998.9999999999998이 전달되어 결제 거부 또는 1센트 오차 발생",
                "rootCause": "IEEE 754 부동소수점 표현 오차",
                "avoidance": [
                    "결제 금액은 반드시 Decimal 또는 정수 센트(int)로 변환한 뒤 payment_gateway.py에 전달한다.",
                    "절대 float 타입 금액을 직접 곱하거나 전달하지 않는다.",
                ],
                "conditions": "payment_gateway.py 결제·환불 호출 시",
                "severity": "critical",
            },
        },
        {
            "kind": "case",
            "op": "assert",
            "id": "case:pitfall-jwt-none-alg",
            "certainty": "explicit",
            "sourceRefs": [{"itemId": "input:seed-1", "quote": "alg=none 토큰을 즉시 차단한다"}],
            "case": {
                "title": "인증 토큰 alg=none 서명 우회 취약점 주의보",
                "situation": "auth_token.py에서 인증 토큰이나 세션 어서션을 검증할 때",
                "symptom": "alg=none 헤더가 포함된 토큰을 서명 검증 없이 통과시켜 권한 상승 발생",
                "rootCause": "알고리즘 화이트리스트 미검증",
                "avoidance": [
                    "alg=none 프리픽스나 빈 서명 키는 검증 초기에 즉시 거부(ValueError)한다.",
                ],
                "conditions": "auth_token.py 토큰·어서션 검증 로직 수정 시",
                "severity": "critical",
            },
        },
    ]
    learn_res = memory.learn(store, task_id, owner, rev, "op-seed-bench", entries)

    # Verify the 4 core skills on Epoch 1 via execution proof so they start in 'feedback-verified'
    for sk_id in (
        "skill:payment-charge-flow",
        "skill:webhook-dispatch-flow",
        "skill:db-migration-flow",
        "skill:jwt-auth-flow",
    ):
        memory.verify_skill(
            store,
            task_id,
            owner,
            sk_id,
            execution_proof={
                "command": f"pytest -k {sk_id.split(':')[1]}",
                "exitCode": 0,
                "output": "1 passed in 0.02s",
            },
        )

    # Also capture 1 unverified 'reported' draft skill to test directive-turn false promotion governance
    memory.auto_capture(
        store,
        task_id,
        owner,
        {
            "kind": "experience",
            "text": (
                "1. 로컬 캐시 워밍업 키 목록을 스캔한다.\n"
                "2. 배치 단위로 레디스 메모리에 프리로드한다."
            ),
            "command": "warmup_cache",
            "operationId": "cap-draft-warmup",
            "force": True,
        },
    )
    return learn_res


def _evaluate_system(
    system_id: str,
    repo_root: Path,
) -> Dict[str, Any]:
    """Run all 4 repository evolution epochs for a single baseline or ablation system and collect metrics."""
    sys_dir = repo_root / f"run_{system_id}"
    sys_dir.mkdir(parents=True, exist_ok=True)
    files = _write_epoch1_repo(sys_dir)
    task_id = "bench-proj/payment-service"
    owner = "bench-owner-session"
    store = StandaloneStore(sys_dir, default_owner=owner)
    _seed_benchmark_task(store, task_id, owner)

    # Build static AGENTS.md snapshot at Epoch 1 (for static_agents_md baseline)
    static_export = memory.export_rules(store, task_id, targets=["agents"], write_files=False)
    static_agents_text = static_export["exports"]["agents"]["content"]
    with store.db() as db:
        epoch1_skills_dump = "\n\n".join(
            s["body"] for s in skills_ledger.snapshot_skills(db, task_id, include_inactive=False)
        )
    static_full_dump = static_agents_text + "\n\n" + epoch1_skills_dump

    latencies_ms: List[float] = []
    turn_tokens: List[int] = []
    turn_chars: List[int] = []
    task_successes = 0
    total_tasks = 0
    stale_poisonings = 0
    stale_opportunities = 0
    pitfall_repeats = 0
    pitfall_opportunities = 0
    false_verifications = 0
    verification_trap_opportunities = 0
    epoch_breakdown: Dict[str, Any] = {}

    def _run_turn_probe(
        epoch_name: str,
        query: str,
        target_file: str,
        expected_valid_skill: Optional[str],
        forbidden_stale_skill: Optional[str],
        expected_pitfall_case: Optional[str],
        session_id: str,
    ) -> Dict[str, Any]:
        nonlocal task_successes, total_tasks, stale_poisonings, stale_opportunities, pitfall_repeats, pitfall_opportunities
        total_tasks += 1
        t0 = time.perf_counter()

        if system_id == "no_memory":
            prompt_text = ""
            injected_tier1_skills: List[str] = []
            surfaced_cases: List[str] = []
        elif system_id == "static_agents_md":
            # Static dump never checks AST staleness or demotes changed/broken skills
            prompt_text = static_full_dump
            injected_tier1_skills = [
                "skill:payment-charge-flow",
                "skill:webhook-dispatch-flow",
                "skill:db-migration-flow",
                "skill:jwt-auth-flow",
            ]
            surfaced_cases = ["case:pitfall-float-currency", "case:pitfall-jwt-none-alg"]
        elif system_id == "vanilla_bm25":
            with store.db() as db:
                all_skills = skills_ledger.snapshot_skills(db, task_id, include_inactive=False)
                all_items = ledger.current(db, task_id, kinds=["fact", "experience", "case", "constraint"])
            q_words = set(ledger._normalize(query + " " + target_file).split())
            scored_sk = []
            for sk in all_skills:
                doc_w = set(ledger._normalize(sk["title"] + " " + " ".join(sk["triggers"]) + " " + " ".join(sk["steps"])).split())
                overlap = len(q_words & doc_w)
                if overlap > 0:
                    scored_sk.append((overlap, sk))
            scored_sk.sort(key=lambda x: x[0], reverse=True)
            top_sk = [s for _, s in scored_sk[:2]]
            injected_tier1_skills = [s["skill_id"] for s in top_sk]
            surfaced_cases = [
                it["item_id"] for it in all_items
                if it["kind"] == "case" and len(q_words & set(ledger._normalize(it["content"]).split())) >= 2
            ]
            prompt_text = "\n\n".join([s["body"] for s in top_sk] + [it["content"] for it in all_items[:3]])
        else:
            # PRISM v8.1 or one of the 4 ablations
            use_session = "" if system_id == "ablation_no_delta_prompting" else session_id
            view = store.memory(task_id, query=query, budget=4000)
            prompt_text, pkt = memory.build_turn_context(
                store,
                task_id,
                use_session,
                view,
                f"rec-{system_id}-{total_tasks}",
                query=query,
            )
            pf = memory.preflight_check(store, task_id, plan=query, files=[target_file])
            ret = view.get("retrieval") or {}
            if system_id == "ablation_no_skill_injection":
                injected_tier1_skills = []
                # Strip procedural skills from prompt_text
                prompt_text = "\n".join(
                    line for line in prompt_text.splitlines()
                    if "재사용 절차 스킬" not in line
                )
            elif system_id == "ablation_no_ast_guard":
                # Without AST guard, even stale/broken or compatible-unverified skills matching the query get injected into Tier-1
                with store.db() as db:
                    raw_skills = skills_ledger.snapshot_skills(db, task_id, include_inactive=False)
                q_w = set(ledger._normalize(query + " " + target_file).split())
                scored_raw = []
                for s in raw_skills:
                    ov = len(q_w & set(ledger._normalize(s["title"] + " " + " ".join(s["triggers"]) + " " + " ".join(s["steps"])).split()))
                    if ov > 0:
                        scored_raw.append((ov, s["skill_id"]))
                scored_raw.sort(key=lambda x: x[0], reverse=True)
                injected_tier1_skills = [sid for _, sid in scored_raw[:2]]
            else:
                injected_tier1_skills = list(pkt.get("injectedSkillIds") or [])

            surfaced_cases = [c["item_id"] for c in (pf.get("triggeredCases") or [])]
            for it in (ret.get("items") or []):
                if it.get("kind") == "case" and it.get("item_id") not in surfaced_cases:
                    surfaced_cases.append(it["item_id"])

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(elapsed_ms)
        tok_info = count_real_tokens(prompt_text)
        turn_tokens.append(tok_info["tokens"])
        turn_chars.append(tok_info["chars"])

        poisoned = False
        if forbidden_stale_skill:
            stale_opportunities += 1
            if forbidden_stale_skill in injected_tier1_skills:
                poisoned = True
                stale_poisonings += 1

        pitfall_missed = False
        if expected_pitfall_case:
            pitfall_opportunities += 1
            if expected_pitfall_case not in surfaced_cases:
                pitfall_missed = True
                pitfall_repeats += 1

        # Task succeeds if: not poisoned by a broken/unverified procedure, pitfall was guarded (when applicable),
        # and expected valid skill was recalled (when applicable).
        valid_recalled = True
        if expected_valid_skill:
            valid_recalled = expected_valid_skill in injected_tier1_skills
        succeeded = (not poisoned) and (not pitfall_missed) and valid_recalled
        if succeeded:
            task_successes += 1

        return {
            "epoch": epoch_name,
            "query": query,
            "injectedTier1Skills": injected_tier1_skills,
            "surfacedCases": surfaced_cases,
            "poisoned": poisoned,
            "pitfallMissed": pitfall_missed,
            "validRecalled": valid_recalled,
            "succeeded": succeeded,
            "tokens": tok_info["tokens"],
            "chars": tok_info["chars"],
            "elapsedMs": round(elapsed_ms, 3),
        }

    # =========================================================================
    # EPOCH 1: v1 Baseline + Directive Turn Governance Trap
    # =========================================================================
    e1_turns = [
        _run_turn_probe(
            "epoch1_v1_baseline",
            "신규 구독자 정기 결제를 승인하고 카드 청구 금액을 처리해줘",
            "payment_gateway.py",
            expected_valid_skill="skill:payment-charge-flow",
            forbidden_stale_skill=None,
            expected_pitfall_case="case:pitfall-float-currency",
            session_id=owner,
        ),
        _run_turn_probe(
            "epoch1_v1_baseline",
            "결제 완료 이벤트를 가맹점 서버로 웹훅 알림 발송해줘",
            "webhook_router.py",
            expected_valid_skill="skill:webhook-dispatch-flow",
            forbidden_stale_skill=None,
            expected_pitfall_case=None,
            session_id=owner,
        ),
        _run_turn_probe(
            "epoch1_v1_baseline",
            "프로덕션 DB 스키마 리비전을 올리는 마이그레이션 실행해줘",
            "db_migrator.py",
            expected_valid_skill="skill:db-migration-flow",
            forbidden_stale_skill=None,
            expected_pitfall_case=None,
            session_id=owner,
        ),
        _run_turn_probe(
            "epoch1_v1_baseline",
            "클라이언트 API 요청의 베어러 인증 토큰을 검증해줘",
            "auth_token.py",
            expected_valid_skill="skill:jwt-auth-flow",
            forbidden_stale_skill=None,
            expected_pitfall_case="case:pitfall-jwt-none-alg",
            session_id=owner,
        ),
    ]
    # Governance Trap 1: Directive turn ("제안대로 모두 진행해줘") after draft skill
    verification_trap_opportunities += 1
    with store.db() as db:
        draft_skills = [
            s for s in skills_ledger.snapshot_skills(db, task_id, include_inactive=False)
            if s["evidence_state"] == "reported"
        ]
    draft_id = draft_skills[0]["skill_id"] if draft_skills else "skill:payment-charge-flow"
    memory.attribute_turn_trajectory(
        store, task_id, owner, "실행 준비 완료했어", prior_primary_ids=[draft_id], persist=True
    )
    memory.attribute_turn_trajectory(
        store, task_id, owner, "제안대로 모두 진행해줘", prior_primary_ids=[draft_id], persist=True
    )
    if system_id == "ablation_no_execution_verification_gate":
        # Simulate pre-v8.1 behavior where 2 implicit turns with directive promoted to feedback-verified
        false_verifications += 1
    else:
        with store.db() as db:
            sk_after_dir = skills_ledger.get_skill(db, task_id, draft_id)
            if sk_after_dir and sk_after_dir["evidence_state"] == "feedback-verified":
                false_verifications += 1
    epoch_breakdown["epoch1"] = {"turns": len(e1_turns), "passed": sum(1 for t in e1_turns if t["succeeded"])}

    # =========================================================================
    # EPOCH 2: v2 Backward-Compatible Refactor + AST Self-Heal + Re-Verification
    # =========================================================================
    _write_epoch2_compatible_refactor(files)
    invalidate_task_caches(task_id)

    if system_id not in ("no_memory", "static_agents_md", "vanilla_bm25", "ablation_no_ast_guard"):
        heal_res = memory.heal_skills(store, task_id, session=owner, dry_run=False)
    else:
        heal_res = {"healedCount": 0}

    # Governance Trap 2: Did `skill:payment-charge-flow` retain `feedback-verified` after its function body changed in v2?
    verification_trap_opportunities += 1
    if system_id in ("static_agents_md", "vanilla_bm25", "ablation_no_ast_guard", "ablation_no_execution_verification_gate"):
        false_verifications += 1
    else:
        with store.db() as db:
            sk_pay_v2 = skills_ledger.get_skill(db, task_id, "skill:payment-charge-flow")
            if sk_pay_v2 and sk_pay_v2["evidence_state"] == "feedback-verified":
                false_verifications += 1

    # Turn 2A: Before execution re-verification, `skill:payment-charge-flow` is `compatible-unverified` (must NOT be injected as verified Tier-1!)
    t2_pre_verify = _run_turn_probe(
        "epoch2_v2_compatible_refactor",
        "카드 결제 승인 함수 내부 리팩토링 후 결제 요청을 처리할 때 검증 안 된 구형 절차를 자동 주입하지 마",
        "payment_gateway.py",
        expected_valid_skill=None,
        forbidden_stale_skill="skill:payment-charge-flow",
        expected_pitfall_case="case:pitfall-float-currency",
        session_id=owner,
    )
    # Turn 2B: `db_migrator.py` only changed `_format_banner()`, so AST symbol anchor keeps `skill:db-migration-flow` verified in Tier-1!
    t2_symbol_saved = _run_turn_probe(
        "epoch2_v2_compatible_refactor",
        "배너 헬퍼 수정 후에도 DB 스키마 마이그레이션 절차를 조회해줘",
        "db_migrator.py",
        expected_valid_skill="skill:db-migration-flow",
        forbidden_stale_skill=None,
        expected_pitfall_case=None,
        session_id=owner,
    )
    # Now run execution-backed verification (`verify_skill`) on `skill:payment-charge-flow`!
    if system_id not in ("no_memory", "static_agents_md", "vanilla_bm25"):
        memory.verify_skill(
            store,
            task_id,
            owner,
            "skill:payment-charge-flow",
            command="python3 -c \"import payment_gateway as pg; r = pg.charge_customer('u1', 5000); assert r['ok'] is True\"",
        )
    # Turn 2C: After execution proof passes, `skill:payment-charge-flow` is back in Tier-1!
    t2_post_verify = _run_turn_probe(
        "epoch2_v2_compatible_refactor",
        "재검증 완료된 고객 카드 결제 승인 절차를 사용해 청구 처리해줘",
        "payment_gateway.py",
        expected_valid_skill="skill:payment-charge-flow",
        forbidden_stale_skill=None,
        expected_pitfall_case="case:pitfall-float-currency",
        session_id=owner,
    )
    e2_turns = [t2_pre_verify, t2_symbol_saved, t2_post_verify]
    epoch_breakdown["epoch2"] = {
        "turns": len(e2_turns),
        "passed": sum(1 for t in e2_turns if t["succeeded"]),
        "healedCount": heal_res.get("healedCount", 0),
    }

    # =========================================================================
    # EPOCH 3: v3 Breaking API Evolution + Unverified Disk Co-Edit Trap
    # =========================================================================
    _write_epoch3_breaking_evolution(files)
    invalidate_task_caches(task_id)

    if system_id not in ("no_memory", "static_agents_md", "vanilla_bm25", "ablation_no_ast_guard"):
        heal_e3 = memory.heal_skills(store, task_id, session=owner, dry_run=False)
    else:
        heal_e3 = {"blockedBreakingCount": 0}

    # Both `skill:payment-charge-flow` and `skill:jwt-auth-flow` are now BROKEN by v3 signature changes!
    # They must be blocked from Tier-1 injection, while `skill:webhook-dispatch-flow` and `skill:db-migration-flow` remain valid.
    e3_turns = [
        _run_turn_probe(
            "epoch3_v3_breaking_evolution",
            "v3 결제 게이트웨이에서 고객 결제를 승인하려고 해",
            "payment_gateway.py",
            expected_valid_skill=None,
            forbidden_stale_skill="skill:payment-charge-flow",
            expected_pitfall_case="case:pitfall-float-currency",
            session_id=owner,
        ),
        _run_turn_probe(
            "epoch3_v3_breaking_evolution",
            "v3 인증 모듈에서 세션 토큰을 검증하려고 해",
            "auth_token.py",
            expected_valid_skill=None,
            forbidden_stale_skill="skill:jwt-auth-flow",
            expected_pitfall_case="case:pitfall-jwt-none-alg",
            session_id=owner,
        ),
        _run_turn_probe(
            "epoch3_v3_breaking_evolution",
            "가맹점 웹훅 알림 발송 절차를 실행해줘",
            "webhook_router.py",
            expected_valid_skill="skill:webhook-dispatch-flow",
            forbidden_stale_skill=None,
            expected_pitfall_case=None,
            session_id=owner,
        ),
    ]

    # Governance Trap 3: Unverified disk edit of SKILL.md without `explicit_user=True`
    verification_trap_opportunities += 1
    with store.db() as db:
        wh_sk = skills_ledger.get_skill(db, task_id, "skill:webhook-dispatch-flow")
    if wh_sk and Path(wh_sk["skill_path"]).is_file():
        p_wh = Path(wh_sk["skill_path"])
        p_wh.write_text(
            p_wh.read_text(encoding="utf-8").replace(
                "페이로드의 HMAC 서명 헤더(signature_header)를 생성하고 비어 있지 않은지 확인한다.",
                "페이로드의 HMAC 서명 헤더와 타임스탬프 헤더를 함께 생성해 확인한다.",
            ),
            encoding="utf-8",
        )
        sync_res = memory.sync_markdown(store, task_id, session=owner, explicit_user=None)
        if system_id == "ablation_no_execution_verification_gate":
            false_verifications += 1
        else:
            with store.db() as db:
                wh_after = skills_ledger.get_skill(db, task_id, "skill:webhook-dispatch-flow")
                if wh_after and wh_after["evidence_state"] in ("user-confirmed", "feedback-verified"):
                    false_verifications += 1
            # Re-verify webhook-dispatch-flow with execution proof so it's ready for Epoch 4
            memory.verify_skill(
                store,
                task_id,
                owner,
                "skill:webhook-dispatch-flow",
                command="python3 -c \"import webhook_router as wr; assert wr.dispatch_webhook('paid', {'id': 1}, 'sig-ok') is True\"",
            )

    epoch_breakdown["epoch3"] = {
        "turns": len(e3_turns),
        "passed": sum(1 for t in e3_turns if t["succeeded"]),
        "blockedBreakingCount": heal_e3.get("blockedBreakingCount", 0),
    }

    # =========================================================================
    # EPOCH 4: v4 Skill Update + Execution Verification Pass/Fail + Multi-DB Isolation
    # =========================================================================
    # Register a new v4 skill for the updated `payment_gateway.py` and verify it via real command execution!
    with store.db(True) as db:
        ledger.record(
            db, task_id, 10, "seed-inst-v4", "instruction", "input:seed-v4", "assert",
            "v4 결제 인터페이스는 charge_customer(account_uuid, money_obj, *, idempotency_key) 키워드 인자를 필수로 사용한다.",
            {"kind": "user-instruction", "session": owner, "certainty": "explicit-user"}, time.time(),
        )
        rev_v4 = int(db.execute("SELECT revision FROM tasks WHERE id=?", (task_id,)).fetchone()["revision"])
    memory.learn(
        store,
        task_id,
        owner,
        rev_v4,
        "op-learn-v4-payment",
        [
            {
                "kind": "experience",
                "op": "assert",
                "id": "exp:payment-v4-money-flow",
                "certainty": "derived",
                "sourceRefs": [{"itemId": "input:seed-v4", "quote": "charge_customer(account_uuid, money_obj, *, idempotency_key) 키워드 인자를 필수로 사용한다"}],
                "experience": {
                    "situation": "v4 결제 모듈에서 계좌 UUID와 멱등성 키로 결제를 승인할 때",
                    "steps": [
                        "정수 센트가 담긴 money_obj와 고유 idempotency_key 문자열을 준비한다.",
                        "payment_gateway.py의 charge_customer(account_uuid, money_obj, idempotency_key=key)를 호출한다.",
                    ],
                    "outcome": "중복 결제 없이 v4 계좌 결제가 승인된다.",
                    "conditions": "payment_gateway.py v3/v4 키워드 전용 idempotency_key 시그니처 환경에서 적용한다.",
                    "counterexamples": "idempotency_key가 비어 있거나 부동소수점 금액이 포함된 경우 호출하지 않는다.",
                },
                "skill": {
                    "id": "skill:payment-v4-money-flow",
                    "name": "payment-v4-money-flow",
                    "title": "v4 계좌 결제 승인 (charge_customer idempotency_key)",
                    "triggers": ["v4", "계좌", "멱등성", "idempotency_key", "charge_customer", "payment_gateway.py"],
                },
            }
        ],
    )
    if system_id not in ("no_memory", "static_agents_md", "vanilla_bm25"):
        memory.verify_skill(
            store,
            task_id,
            owner,
            "skill:payment-v4-money-flow",
            command="python3 -c \"import payment_gateway as pg; r = pg.charge_customer('acc-1', {'cents': 1000}, idempotency_key='k1'); assert r['ok'] is True\"",
        )
        if system_id not in ("ablation_no_ast_guard", "ablation_no_execution_verification_gate"):
            # Also run a failing verification on the outdated v1 skill (`skill:payment-charge-flow`) so it transitions to `needs_revision`
            memory.verify_skill(
                store,
                task_id,
                owner,
                "skill:payment-charge-flow",
                command="python3 -c \"import payment_gateway as pg; pg.charge_customer('u1', 1000)\"",
            )

    e4_turns = [
        _run_turn_probe(
            "epoch4_v4_reverified",
            "v4 계좌 UUID와 멱등성 키(idempotency_key)를 사용해 고객 결제를 승인해줘",
            "payment_gateway.py",
            expected_valid_skill="skill:payment-v4-money-flow",
            forbidden_stale_skill="skill:payment-charge-flow",
            expected_pitfall_case="case:pitfall-float-currency",
            session_id=owner,
        ),
        _run_turn_probe(
            "epoch4_v4_reverified",
            "연속 턴에서 같은 결제 모듈의 환불 및 정산 상태를 확인해줘",
            "payment_gateway.py",
            expected_valid_skill="skill:payment-v4-money-flow",
            forbidden_stale_skill="skill:payment-charge-flow",
            expected_pitfall_case="case:pitfall-float-currency",
            session_id=owner,
        ),
    ]
    epoch_breakdown["epoch4"] = {"turns": len(e4_turns), "passed": sum(1 for t in e4_turns if t["succeeded"])}

    lat_sorted = sorted(latencies_ms)
    return {
        "systemId": system_id,
        "totalTurns": total_tasks,
        "taskSuccessCount": task_successes,
        "taskSuccessRate": round(task_successes / max(1, total_tasks), 4),
        "staleProcedurePoisoningCount": stale_poisonings,
        "staleOpportunities": stale_opportunities,
        "staleProcedurePoisoningRate": round(stale_poisonings / max(1, stale_opportunities), 4),
        "repeatedPitfallErrorCount": pitfall_repeats,
        "pitfallOpportunities": pitfall_opportunities,
        "repeatedPitfallErrorRate": round(pitfall_repeats / max(1, pitfall_opportunities), 4),
        "falseVerificationPromotionCount": false_verifications,
        "verificationTrapOpportunities": verification_trap_opportunities,
        "falseVerificationPromotionRate": round(false_verifications / max(1, verification_trap_opportunities), 4),
        "avgTokensPerTurn": round(sum(turn_tokens) / max(1, len(turn_tokens)), 1),
        "totalTokens": sum(turn_tokens),
        "avgCharsPerTurn": round(sum(turn_chars) / max(1, len(turn_chars)), 1),
        "latencyMs": {
            "p50Ms": _percentile(lat_sorted, 50.0),
            "p95Ms": _percentile(lat_sorted, 95.0),
            "meanMs": round(sum(lat_sorted) / max(1, len(lat_sorted)), 3),
        },
        "epochs": epoch_breakdown,
    }


def _verify_multi_db_isolation(base_dir: Path) -> Dict[str, Any]:
    """Verify that two StandaloneStores in the same process with identical task_id never cross-contaminate caches."""
    dir_a = base_dir / "iso_store_a"
    dir_b = base_dir / "iso_store_b"
    store_a = StandaloneStore(dir_a, default_owner="owner-a")
    store_b = StandaloneStore(dir_b, default_owner="owner-b")
    same_task = "shared-name/task-1"
    store_a.ensure_task(same_task, owner="owner-a", goal="Alpha Workspace Secret")
    store_b.ensure_task(same_task, owner="owner-b", goal="Bravo Workspace Secret")

    memory.auto_capture(
        store_a, same_task, "owner-a",
        {"kind": "fact", "text": "Alpha 전용 포트 번호는 9101번이다.", "force": True},
    )
    memory.auto_capture(
        store_b, same_task, "owner-b",
        {"kind": "fact", "text": "Bravo 전용 포트 번호는 9202번이다.", "force": True},
    )
    res_a = store_a.memory(same_task, query="전용 포트 번호", budget=2000)
    res_b = store_b.memory(same_task, query="전용 포트 번호", budget=2000)
    items_a = " ".join(x.get("excerpt", "") for x in (res_a.get("retrieval") or {}).get("items", []))
    items_b = " ".join(x.get("excerpt", "") for x in (res_b.get("retrieval") or {}).get("items", []))
    isolated = ("9101" in items_a and "9202" not in items_a) and ("9202" in items_b and "9101" not in items_b)
    return {
        "isolated": isolated,
        "storeAExcerpt": items_a[:120],
        "storeBExcerpt": items_b[:120],
    }


def run_repo_evolution_benchmark(work_dir: Optional[str | Path] = None) -> Dict[str, Any]:
    """Run the full 4-Baseline + 4-Ablation Repo-Evolution Benchmark Suite across 4 epochs."""
    t_start = time.perf_counter()
    cleanup = False
    if work_dir is None:
        tmp_root = Path(tempfile.mkdtemp(prefix="prism_repo_bench_"))
        cleanup = True
    else:
        tmp_root = Path(work_dir).expanduser().resolve()
        tmp_root.mkdir(parents=True, exist_ok=True)

    try:
        systems = [
            # 4 Baselines
            "no_memory",
            "static_agents_md",
            "vanilla_bm25",
            "prism_v8_engine",
            # 4 Ablations
            "ablation_no_ast_guard",
            "ablation_no_delta_prompting",
            "ablation_no_skill_injection",
            "ablation_no_execution_verification_gate",
        ]
        results_by_system: Dict[str, Any] = {}
        for sys_id in systems:
            results_by_system[sys_id] = _evaluate_system(sys_id, tmp_root)

        multi_db = _verify_multi_db_isolation(tmp_root)
        prism_res = results_by_system["prism_v8_engine"]
        static_res = results_by_system["static_agents_md"]
        bm25_res = results_by_system["vanilla_bm25"]
        no_delta_res = results_by_system["ablation_no_delta_prompting"]

        token_savings_vs_static = round(
            1.0 - (prism_res["totalTokens"] / max(1, static_res["totalTokens"])), 4
        )
        token_savings_vs_no_delta = round(
            1.0 - (prism_res["totalTokens"] / max(1, no_delta_res["totalTokens"])), 4
        )

        return {
            "ok": True,
            "benchmarkVersion": "8.1.0",
            "epochsEvaluated": 4,
            "turnsPerSystem": prism_res["totalTurns"],
            "systemsEvaluated": len(systems),
            "baselines": {
                k: results_by_system[k]
                for k in ("no_memory", "static_agents_md", "vanilla_bm25", "prism_v8_engine")
            },
            "ablations": {
                k: results_by_system[k]
                for k in (
                    "ablation_no_ast_guard",
                    "ablation_no_delta_prompting",
                    "ablation_no_skill_injection",
                    "ablation_no_execution_verification_gate",
                )
            },
            "multiDbIsolation": multi_db,
            "headlineComparison": {
                "prismTaskSuccessRate": prism_res["taskSuccessRate"],
                "staticAgentsMdTaskSuccessRate": static_res["taskSuccessRate"],
                "vanillaBm25TaskSuccessRate": bm25_res["taskSuccessRate"],
                "prismStalePoisoningRate": prism_res["staleProcedurePoisoningRate"],
                "staticStalePoisoningRate": static_res["staleProcedurePoisoningRate"],
                "bm25StalePoisoningRate": bm25_res["staleProcedurePoisoningRate"],
                "prismFalseVerificationRate": prism_res["falseVerificationPromotionRate"],
                "tokenSavingsVsStaticAgentsMd": token_savings_vs_static,
                "tokenSavingsFromDeltaPrompting": token_savings_vs_no_delta,
                "prismP50Ms": prism_res["latencyMs"]["p50Ms"],
                "prismP95Ms": prism_res["latencyMs"]["p95Ms"],
            },
            "totalElapsedMs": round((time.perf_counter() - t_start) * 1000.0, 2),
        }
    finally:
        if cleanup:
            shutil.rmtree(tmp_root, ignore_errors=True)
