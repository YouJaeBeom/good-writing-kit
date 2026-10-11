"""채팅 답변 점검. 응답이 끝날 때 방금 쓴 답변을 정규식으로만 검사한다(모델 호출 없음, 토큰 0).

찾는 것
  - 풀지 않은 약어: 사용자가 이 대화에서 쓴 적 없고, 누구나 아는 약어도 아니고, 답변 안에서 풀어 쓰지도 않은 것
  - 앞 대화 참조: "위 2번", "아까 말한", "그거"처럼 앞 맥락을 기억해야 읽히는 말
  - 국문 AI 말투: im-not-ai humanize-scan의 실측 신호 6개 중 채팅에 맞는 3개
    (C-8 "A가 아니라 B" 대구 2회+, C-11 연결어미 뒤 쉼표 3회+, I-4 문단 끝 당위 2회+)
  - 영문 AI 말투와 채팅 상투구: em dash 남발, not X but Y, "Great question" 같은 말
결과는 다음 사용자 메시지 때 훅이 짧게 알린다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

COMMON_ABBR = frozenset(
    "AI API PDF URL OK ID UI UX CSV JSON HTML CSS HTTP HTTPS SQL CPU GPU OS PC USB IT PR CLI IDE FAQ PPT "
    "EU US UK AM PM KST UTC README TODO LICENSE CHANGELOG II III IV".split()
)
CODE_BLOCK = re.compile(r"```.*?```|~~~.*?~~~", re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
LINK_TARGET = re.compile(r"\]\([^)\n]*\)|https?://\S+|(?:~|\.{1,2})?/[\w./-]+")
# 대문자 두 글자 이상(L7 같은 줄 번호는 제외). 파일 이름(CLAUDE.md)의 일부는 제외한다.
ABBR = re.compile(r"(?<![A-Za-z0-9_./\-])([A-Z]{2,}[0-9]*)(s?)(?![A-Za-z0-9_]|\.[A-Za-z])")
QUOTES = "\"'“”‘’「」`"


BACK_REFERENCE = re.compile(
    r"위\s*\d+\s*번|아까\s*(?:말한|그|본)|앞서\s*말한\s*(?:것|거|그)|(?<![가-힣])그거(?:[는를가도]|요)?(?![가-힣])"
)
ENTRY_TYPE = re.compile(r'"type"\s*:\s*"(?:user|assistant)"')
KO_CONTRAST = re.compile(r"(?:이|가|게|것이)\s*아니라|(?:인가|일까)\s*,\s*[^?.]{1,20}(?:인가|일까)")
KO_CONNECTIVE_COMMA = re.compile(r"[가-힣](?:고|며|지만|면서|아서|어서),\s")
KO_MUST_ENDING = re.compile(r"해야\s*(?:한다|합니다)\.?\s*$")
EN_CONTRAST = re.compile(r"\bnot (?:just |only |merely )?[\w-]+(?: [\w-]+){0,3}, but\b|\bisn't (?:just )?[\w-]+(?: [\w-]+){0,3}\s*[—–-]+\s*it's\b", re.I)
EM_DASH = "—"
CHAT_RESIDUE = re.compile(
    r"Great question|I hope this helps|Let me know if|Certainly!|You're absolutely right|Here's the thing|"
    r"좋은 질문|도움이 되(?:었|셨)(?:길|으면)|궁금한 점이 있으시면 언제든",
    re.I,
)

THRESHOLDS = {"contrast": 2, "connective_comma": 3, "must_ending": 2, "em_dash": 3, "en_contrast": 2}
SHOWN_MAX = 5


def strip_code(text: str) -> str:
    text = CODE_BLOCK.sub(" ", text)
    text = INLINE_CODE.sub(" ", text)
    return LINK_TARGET.sub(" ", text)


def named_tokens(text: str) -> set[str]:
    """코드 표기, 경로, 링크 안에 나온 대문자 이름. 프로젝트·폴더 이름 같은 고유명사로 보고 약어 점검에서 뺀다."""
    spans = CODE_BLOCK.findall(text) + INLINE_CODE.findall(text) + LINK_TARGET.findall(text)
    return set(re.findall(r"[A-Z]{2,}[0-9]*", " ".join(spans)))


QUOTED = re.compile(r'"[^"\n]{1,60}"|“[^”\n]{1,60}”|\'[^\'\n]{1,60}\'|‘[^’\n]{1,60}’|「[^」\n]{1,60}」')


def unquoted(pattern: re.Pattern, text: str) -> list[str]:
    """따옴표로 감싼 짧은 인용(규칙을 설명하며 예로 든 말)은 빼고 찾는다."""
    return [m.group(0) for m in pattern.finditer(QUOTED.sub(" ", text))]


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _is_prompt(entry: dict) -> bool:
    """도구 결과가 아닌 사용자 쪽 메시지(사람의 입력이나 서브에이전트 보고)인지."""
    if entry.get("type") != "user":
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return isinstance(content, str)


def read_transcript(path: Path) -> tuple[str, list[str]]:
    """(마지막 사용자 메시지 이후 Claude가 쓴 답변 글, 사람이 직접 보낸 메시지 목록)을 돌려준다."""
    human: list[str] = []
    entries: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not ENTRY_TYPE.search(line):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (entry.get("origin") or {}).get("kind") == "human":
                human.append(_block_text((entry.get("message") or {}).get("content")))
            entries.append(entry)
    reply: list[str] = []
    for entry in reversed(entries):
        if _is_prompt(entry):
            break
        if entry.get("type") == "assistant":
            text = _block_text((entry.get("message") or {}).get("content"))
            if text.strip():  # 도구 호출만 있는 항목은 빈 글이다
                reply.append(text)
    return "\n".join(reversed(reply)).strip(), human


def _abbr_issues(reply: str, user_texts: list[str], known: set[str]) -> list[str]:
    prose = strip_code(reply)
    user_words = set(re.findall(r"[A-Za-z][A-Za-z0-9]*", "\n".join(user_texts))) | named_tokens(reply) | known
    found: dict[str, None] = {}
    for match in ABBR.finditer(prose):
        abbr = match.group(1)
        if abbr in COMMON_ABBR or abbr in user_words or abbr in found:
            continue
        esc = re.escape(abbr)
        # 정의로 보는 형태: "검색 증강 생성(RAG)", "(Retrieval-Augmented Generation, RAG)", "MMR(Maximal Marginal Relevance)"
        defined = re.search(rf"\([^()\n]*(?<![A-Za-z]){esc}s?(?![A-Za-z])[^()\n]*\)", prose) or re.search(
            rf"{esc}s?\s*\([^()\n]{{2,}}\)", prose
        )
        if not defined:
            found[abbr] = None
    if not found:
        return []
    shown = ", ".join(list(found)[:SHOWN_MAX])
    return [f"풀어 쓰지 않은 약어: {shown} (사용자가 쓴 적 없는 약어는 처음 나올 때 풀어 쓴다)"]


def _style_issues(reply: str) -> list[str]:
    prose = strip_code(reply)
    issues: list[str] = []
    back = unquoted(BACK_REFERENCE, prose)
    if back:
        issues.append(f"앞 대화 참조: {', '.join(dict.fromkeys(back))} (대상의 이름과 한 줄 설명으로 다시 쓴다)")
    counts = {
        "contrast": len(unquoted(KO_CONTRAST, prose)),
        "connective_comma": len(unquoted(KO_CONNECTIVE_COMMA, prose)),
        "must_ending": sum(1 for para in re.split(r"\n\s*\n", prose) if KO_MUST_ENDING.search(para.strip())),
        "em_dash": prose.count(EM_DASH),
        "en_contrast": len(EN_CONTRAST.findall(prose)),
    }
    labels = {
        "contrast": "'A가 아니라 B' 대구 {n}회",
        "connective_comma": "연결어미(-고, -며, -지만 등) 뒤 쉼표 {n}회",
        "must_ending": "문단 끝 당위('~해야 한다') {n}회",
        "em_dash": "em dash(—) {n}회",
        "en_contrast": "'not X, but Y' 대구 {n}회",
    }
    tells = [labels[key].format(n=n) for key, n in counts.items() if n >= THRESHOLDS[key]]
    residue = list(dict.fromkeys(unquoted(CHAT_RESIDUE, prose)))
    if residue:
        tells.append(f"채팅 상투구: {', '.join(residue)}")
    if tells:
        issues.append("AI 말투 신호: " + "; ".join(tells))
    return issues


def find_issues(reply: str, user_texts: list[str], cwd: str = "") -> list[str]:
    """cwd(작업 폴더 경로)에 나오는 대문자 이름도 프로젝트 이름으로 보고 약어 점검에서 뺀다."""
    if not reply.strip():
        return []
    known = set(re.findall(r"[A-Z]{2,}[0-9]*", cwd))
    return _abbr_issues(reply, user_texts, known) + _style_issues(reply)
