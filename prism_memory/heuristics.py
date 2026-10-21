"""Negation-aware trajectory heuristics, contrastive case synthesis, prospective cues, and query decomposition."""
from pathlib import Path
import re

import work_memory as ledger

_WORD_RE = re.compile(r"[^\W_]+")

# 1. Negated-Error / Privative Positive Phrases ("오류 없이", "에러 없이", "문제 없이", "no error", "without errors")
# Stripped FIRST before checking negative patterns so "오류 없이 통과했어" never misfires as negative.
_PRIVATIVE_POSITIVE_RE = re.compile(
    r"(?i)(?:(?:오류|에러|문제|버그|충돌|실패|예외|이상|차질)[이가은는도]?\s*(?:전혀\s*|하나도\s*)?(?:없이|없고|없음|없어|없네|없습니다|안\s*나고|발생하지\s*않고)"
    r"|\b(?:no|without\s+any|without|zero)\s+(?:errors?|bugs?|issues?|failures?|exceptions?|problems?|crashes?|regressions?)\b)",
)

# 2. Deferred / Unexecuted Phrases ("응 아직 실행하지 않았어", "아직 테스트 전이야", "not yet tested")
_DEFERRED_EXECUTION_RE = re.compile(
    r"(?i)(?:(?:아직|전혀|일단)\s*[^\n,.]{0,28}?(?:실행하지\s*않|적용하지\s*않|테스트하지\s*않|확인하지\s*않|안\s*해\s*봤|안\s*돌려|못\s*해\s*봤|전이야|전입니다|모르겠)"
    r"|\b(?:not\s+yet\s+(?:tested|run|executed|applied|verified)|haven'?t\s+(?:tested|run|tried|applied)\s+yet|will\s+test\s+later)\b)",
)

# 3. Negated-Positive / Adverse Contrast Phrases ("Not good", "좋아 보이지만 안 돼", "통과 못 했어", "작동 안 해", "잘 작동하지 않아")
_NEGATED_POSITIVE_RE = re.compile(
    r"(?i)(?:(?:작동|동작|통과|적용|실행|해결|빌드|배포|연결|복구)(?:하지\s*않[아았음네습]?|[이가은는을를도]?\s*(?:잘\s*)?(?:안\s*돼|안\s*됨|안\s*되|안\s*해|안\s*하|못\s*했|못\s*함|않[아았음]|실패))"
    r"|(?:좋아|됐|성공|완료|통과)[^\n,.]{0,16}?(?:보이지만|줄\s*알았는데|같지만|했는데도?)\s*[^\n,.]{0,20}?(?:안\s*돼|안\s*됨|오류|에러|실패|문제|아직)"
    r"|\b(?:not\s+(?:good|great|ok|okay|working|passing|fixed|resolved|done|successful)|doesn'?t\s+work|did\s+not\s+(?:work|pass)|still\s+(?:failing|broken))\b)",
)

# 4. Direct Negative / Failure Phrases
_TRAJECTORY_NEGATIVE_RE = re.compile(
    r"(?i)(?:아니[요야]?|틀렸|오류|에러|실패|버그|잘못|안\s*돼|안\s*됨|안됩니다|충돌|다시\s*(?:해|확인|점검)|되돌려|복구해"
    r"|\b(?:wrong|error|failed|broken|incorrect|revert|rollback|traceback|exception|bug)\b)",
)

# 5. Pure Action Directives ("제안대로 모두 진행해줘", "진행해줘", "proceed") — signals trial adoption, NOT outcome verification
_ACTION_DIRECTIVE_RE = re.compile(
    r"(?i)(?:^(?:진행|제안대로|그렇게|모두\s*진행|다\s*진행|이어서|다음)"
    r"|\b(?:proceed|continue|next)\b"
    r"|(?:이어서\s*진행|다음\s*단계|제안대로|모두\s*진행))",
)

# 6. Direct Positive / Outcome Confirmation Phrases ("잘 작동해", "오류 없이 통과했어", "성공", "완료")
_TRAJECTORY_POSITIVE_RE = re.compile(
    r"(?i)(?:^(?:응|네|예|좋아|좋아요|좋습니다|확인|완료|성공|됐어|잘\s*됨|통과|적용|고마워|감사|오케이|ㅇㅋ)"
    r"|\b(?:ok|okay|yes|good|great|works|passed|done|thanks|thank\s+you|lgtm)\b"
    r"|(?:잘\s*작동|통과했|적용됐|성공적|문제없|<POSITIVE_NO_ERROR>))",
)


def analyze_trajectory_sentiment(query_text: str) -> dict:
    """Negation-aware, clause-level trajectory sentiment classifier.
    Correctly handles privative positives ('오류 없이 통과했어', 'no error'),
    deferred execution ('응 아직 실행하지 않았어'), negated positives ('Not good', '좋아 보이지만 안 돼'),
    and separates action directives ('제안대로 모두 진행해줘') from outcome confirmations.
    """
    q_text = str(query_text or "").strip()
    if not q_text:
        return {
            "sentiment": "neutral",
            "hasPositive": False,
            "hasDirective": False,
            "hasNegative": False,
            "isDeferred": False,
            "reason": "empty_query",
        }

    # Step 1: Check explicit deferred / not-yet-executed clauses first
    if _DEFERRED_EXECUTION_RE.search(q_text):
        return {
            "sentiment": "deferred",
            "hasPositive": False,
            "hasDirective": False,
            "hasNegative": False,
            "isDeferred": True,
            "reason": "deferred_unexecuted",
        }

    # Step 2: Replace privative positive clauses ("오류 없이", "no error") with a positive token before negative matching
    masked_text = _PRIVATIVE_POSITIVE_RE.sub(" <POSITIVE_NO_ERROR> ", q_text)
    had_privative_positive = "<POSITIVE_NO_ERROR>" in masked_text

    # Step 3: Check negated-positive ("Not good", "통과 못 했어", "좋아 보이지만 안 돼") and direct negative on masked text
    has_negated_pos = bool(_NEGATED_POSITIVE_RE.search(masked_text))
    has_direct_neg = bool(_TRAJECTORY_NEGATIVE_RE.search(masked_text))
    if has_negated_pos or has_direct_neg:
        return {
            "sentiment": "negative",
            "hasPositive": False,
            "hasDirective": False,
            "hasNegative": True,
            "isDeferred": False,
            "reason": "negated_positive" if has_negated_pos else "explicit_negative",
        }

    # Step 4: Check outcome-positive confirmation vs pure action directive on masked text
    has_pos = bool(had_privative_positive or _TRAJECTORY_POSITIVE_RE.search(masked_text))
    has_dir = bool(_ACTION_DIRECTIVE_RE.search(masked_text))
    return {
        "sentiment": "positive" if has_pos else ("directive" if has_dir else "neutral"),
        "hasPositive": has_pos,
        "hasDirective": has_dir,
        "hasNegative": False,
        "isDeferred": False,
        "reason": (
            "privative_positive" if had_privative_positive
            else ("explicit_positive" if has_pos else ("action_directive" if has_dir else "neutral"))
        ),
    }
