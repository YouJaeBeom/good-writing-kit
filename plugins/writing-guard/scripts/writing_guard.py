#!/usr/bin/env python3
"""good-writing-kit의 글쓰기 점검 훅. 플러그인 hooks/hooks.json에서 서브커맨드로 호출한다.

  session-start  SessionStart: 플러그인의 rules/writing-rules.md(글쓰기 규칙)를 세션 맥락에 넣는다.
                 ~/.claude/CLAUDE.md에 같은 규칙이 이미 있으면 겹치지 않게 건너뛴다.

  prompt      UserPromptSubmit: 설명 기준(약어, 도메인 용어, 맥락 복원, 채팅 AI 말투)을 상기시킨다.
              토큰을 아끼려고 세션 첫 턴, 10턴마다, 직전 답변에서 문제가 나온 다음 턴에만 보낸다.
              직전 답변 점검 결과가 있으면 함께 알린다. "그대로 써줘", "원문 유지" 같은 요청이 있으면
              그 턴의 문서 AI 말투 점검을 끈다.
  post-edit   PostToolUse(Edit|Write|MultiEdit): 문서 파일(.md .tex .txt 등)에 새로 쓴 국문 글자 수와
              영문 단어 수를 한 번의 응답 동안 누적하고, 프로젝트 용어집이 있으면 바뀐 줄의 표기 위반을 알린다.
              점검할 것이 없으면 응답이 끝날 때(Stop) 누적분을 비운다.
  post-skill  PostToolUse(Skill): humanizer(영문), humanize-scan/humanize-korean(국문)이 실행되면
              그 언어의 누적분을 점검한 것으로 기록한다.
  stop        Stop: 방금 쓴 채팅 답변을 정규식으로 점검해(chat_check.py, 토큰 0) 결과를 다음 턴에 넘기고,
              기준(국문 300자, 영문 60단어)을 넘긴 글이 점검 없이 남았거나 용어 위반이 고쳐지지
              않았으면 응답 종료를 막는다. 두 번까지만 막고 세 번째는 사용자에게 알리고 통과시킨다.

끄는 법
  ~/.claude/writing-guard.off            훅 전체를 끈다.
  ~/.claude/writing-guard-reminder.off   설명 기준 상기만 끈다.
  ~/.claude/writing-guard-chat.off       채팅 답변 점검만 끈다.
  ~/.claude/writing-guard-known-terms.txt  약어로 보지 않을 이름(프로젝트·학회 이름 등)을 공백으로 나열한다.
  <프로젝트>/.claude/writing-guard.json  {"humanize": false} 또는 {"exclude": ["drafts/**"]}로 프로젝트 단위 조정.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_check import find_issues, read_transcript  # noqa: E402
from prose import added_blocks, extract, is_prose_path, measure  # noqa: E402
from term_rules import collapse, find_hits, find_project, load_rules  # noqa: E402

CLAUDE_HOME = Path.home() / ".claude"
RULES_FILE = Path(__file__).resolve().parent.parent / "rules" / "writing-rules.md"
USER_CLAUDE_MD = CLAUDE_HOME / "CLAUDE.md"
STATE_DIR = CLAUDE_HOME / "state" / "writing-guard"
OFF_SWITCH = CLAUDE_HOME / "writing-guard.off"
REMINDER_OFF = CLAUDE_HOME / "writing-guard-reminder.off"
CHAT_OFF = CLAUDE_HOME / "writing-guard-chat.off"
KNOWN_TERMS = CLAUDE_HOME / "writing-guard-known-terms.txt"

KO_CHAR_LIMIT = 300  # ~/.claude/CLAUDE.md: 한국어 한 문단
EN_WORD_LIMIT = 60  # 같은 문서의 영어 기준
MAX_BLOCKS = 2
REMIND_EVERY = 10  # 설명 기준 상기를 몇 턴마다 다시 보낼지
TIME_LIMIT_SECONDS = 15  # settings.json의 훅 timeout(20초)보다 먼저 스스로 멈춘다
STALE_SECONDS = 3 * 24 * 3600
SHOWN_MAX = 8

EN_SKILLS = frozenset({"humanizer"})
KO_SKILLS = frozenset({"humanize-scan", "humanize-korean", "humanize", "humanize-redo"})

# 글을 손대지 말라는 명시적 요청만 잡는다. "그대로 둬", "그대로 쓰고", "as is"처럼 흔한 말은 넣지 않는다.
# 따옴표로 감싼 인용("원문 유지"라고 하면…)은 skip_requested()에서 뺀다.
SKIP_REQUEST = re.compile(
    r"그대로\s*(?:써\s*(?:줘|주세요|주라|달라)|옮겨\s*(?:줘|주세요))|원문\s*(?:유지|그대로)|윤문\s*(?:하지|말|없이|생략|빼)"
    r"|(?:AI\s*말투|문체)\s*점검\s*(?:생략|하지\s*마|빼)|humaniz\w*\s*(?:하지|말|생략|빼|skip)"
    r"|\b(?:don'?t|do\s+not|no|skip)\s+humaniz",
    re.I,
)

REMINDER = (
    "[설명 기준] 약어는 사용자가 이 대화에서 먼저 쓴 것만 그대로 쓰고, 나머지 약어와 내부 라벨(버전명, 단계 번호, "
    "임시로 붙인 이름)은 처음 나올 때 풀어 쓴다. 용어는 프로젝트 문서와 사용자가 쓰는 도메인 용어를 그대로 쓰고 "
    "새 이름을 만들지 않는다. 앞 대화를 잊은 사람도 이 답변만으로 이해하도록, 앞에서 나온 대상은 '그거', "
    "'위 2번' 대신 이름과 한 줄 설명으로 다시 가리킨다. 채팅 답변에서도 AI 말투('A가 아니라 B' 대구 반복, "
    "-고·-며·-지만 뒤 쉼표, em dash 남발, 상투구)를 피한다. 자세한 기준은 clear-explain 스킬."
)
FEEDBACK_HEAD = "[지난 답변 점검] 직전 채팅 답변에서 다음이 보였다. 이번 답변부터 고친다:"
SKIP_NOTE = "이번 요청은 원문 유지 요청으로 보고 문서 파일의 AI 말투 점검을 건너뛴다."
GLOSSARY_NOTE = (
    "이 프로젝트 용어집: {path}. 답변에서도 이 표기를 쓰고, 용어집에 없는 새 용어는 임의로 정하지 말고 사용자에게 묻는다."
)
SKILL_GUIDE = {
    "ko": "국문은 im-not-ai의 humanize-scan(손볼 곳이 많다고 나오면 humanize-korean)",
    "en": "영문은 humanizer",
}
INSTRUCTION = (
    "[AI 말투 점검 필요] 이번 응답에서 문서 파일({files})에 새로 쓴 글이 기준(국문 {ko_limit}자, 영문 {en_limit}단어)을 "
    "넘었다(국문 {ko}자, 영문 {en}단어). 응답을 끝내기 전에 {skills} 스킬로 점검하고 교정본을 해당 파일에 Edit로 반영하라. "
    "프로젝트 문체 규칙(프로젝트 CLAUDE.md, check_style.py 등)이 스킬 결과보다 우선하고, 사실·수치·인용·고유명사·"
    "LaTeX 명령·코드는 바꾸지 않는다. 점검 없이 끝내면 Stop 훅이 막는다."
)
BLOCK_HUMANIZE = (
    "문서 파일({files})에 국문 {ko}자, 영문 {en}단어를 새로 썼지만 AI 말투 점검이 실행되지 않았다. "
    "{skills} 스킬로 점검하고 교정본을 파일에 반영하라. 사용자가 원문 유지나 점검 생략을 명시적으로 요청한 경우에만 "
    "그 사실을 밝히고 종료하라."
)
BLOCK_TERMS = (
    "방금 고친 줄에 프로젝트 용어집({glossary})과 다른 표기가 남아 있다: {spots}. 확정 표기로 고친 뒤 종료하라. "
    "확정 표기가 문맥에 맞지 않으면 바꾸지 말고 사용자에게 알린다."
)


@dataclass(frozen=True)
class State:
    """세션별 상태. 점검할 것이 없으면 응답이 끝날 때 비우므로 사실상 한 응답 동안의 누적이다.

    ko_chars, en_words: 마지막 점검 뒤 새로 쓴 분량. files: 그 글이 들어간 파일.
    checked_files: 이번 턴에 점검을 마친 파일. 교정본을 반영하는 Edit가 다시 집계되지 않게 한다.
    term_open: 아직 고쳐지지 않은 용어 위반 (파일, 문맥).
    """

    ko_chars: int = 0
    en_words: int = 0
    blocks: int = 0
    notified: bool = False
    skip_turn: bool = False
    files: tuple[str, ...] = ()
    checked_files: tuple[str, ...] = ()
    term_open: tuple[tuple[str, str], ...] = ()
    glossaries: tuple[str, ...] = ()

    def pending_languages(self) -> tuple[str, ...]:
        langs = []
        if self.ko_chars >= KO_CHAR_LIMIT:
            langs.append("ko")
        if self.en_words >= EN_WORD_LIMIT:
            langs.append("en")
        return tuple(langs)

    def file_names(self) -> str:
        return ", ".join(Path(file).name for file in self.files)


def read_payload() -> dict:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def context_output(event: str, lines: list[str]) -> None:
    if lines:
        emit({"hookSpecificOutput": {"hookEventName": event, "additionalContext": "\n".join(lines)}})


def session_key(payload: dict) -> str:
    return re.sub(r"[^\w.-]", "_", str(payload.get("session_id") or "nosession"))


@contextmanager
def locked_state(payload: dict):
    """세션 상태 파일을 잠근 채 (경로, 현재 상태)를 넘긴다. 병렬 Edit 훅이 서로 덮어쓰지 않게 한다."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = session_key(payload)
    path = STATE_DIR / f"{key}.json"
    with open(STATE_DIR / f"{key}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path, load_state(path)


def load_state(path: Path) -> State | None:
    """상태를 읽는다. 시간 초과로 쓰다 만 파일처럼 읽을 수 없으면 지우고 새로 시작한다."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(State)}
        data = {k: v for k, v in data.items() if k in known}
        return State(
            **{
                **data,
                "files": tuple(data.get("files", ())),
                "checked_files": tuple(data.get("checked_files", ())),
                "term_open": tuple(tuple(item) for item in data.get("term_open", ())),
                "glossaries": tuple(data.get("glossaries", ())),
            }
        )
    except (ValueError, TypeError, AttributeError) as exc:
        print(f"writing-guard: 손상된 상태 파일을 지움 {path}: {exc!r}", file=sys.stderr)
        path.unlink(missing_ok=True)
        return None


def save_state(path: Path, state: State) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def drop_state(path: Path) -> None:
    path.unlink(missing_ok=True)  # 잠금 파일은 다른 훅이 기다리고 있을 수 있어 남기고, prune_stale이 지운다


def prune_stale() -> None:
    if not STATE_DIR.exists():
        return
    cutoff = time.time() - STALE_SECONDS
    for old in STATE_DIR.iterdir():
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        except OSError:
            continue  # 다른 세션이 방금 지운 파일


def skip_requested(prompt: str) -> bool:
    """원문 유지 요청이 있는지. 따옴표로 감싼 인용은 요청으로 보지 않는다."""
    quotes = "\"'“”‘’「」`"
    for match in SKIP_REQUEST.finditer(prompt):
        before = prompt[match.start() - 1] if match.start() else ""
        after = prompt[match.end() : match.end() + 4]
        if (before and before in quotes) or any(q in after for q in quotes):
            continue
        return True
    return False


def chat_path(state_path: Path) -> Path:
    return state_path.with_suffix(".chat.json")


def load_chat(path: Path) -> dict:
    """채팅 점검 상태(턴 수, 다음 턴에 알릴 결과). 문서 상태와 달리 세션 내내 유지한다."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except ValueError:
        path.unlink(missing_ok=True)
        return {}


def save_chat(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".chattmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def known_terms() -> str:
    if not KNOWN_TERMS.exists():
        return ""
    lines = KNOWN_TERMS.read_text(encoding="utf-8", errors="replace").splitlines()
    return " ".join(line.split("#", 1)[0] for line in lines)


def skills_for(langs: tuple[str, ...]) -> str:
    return ", ".join(SKILL_GUIDE[lang] for lang in langs)


def safe_project(start: Path, warnings: list[str]):
    """프로젝트 설정을 찾는다. 설정 파일이 깨졌으면 경고만 남기고 프로젝트가 없는 것으로 본다."""
    try:
        return find_project(start)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        warnings.append(f"[writing-guard] 프로젝트 설정(.claude/writing-guard.json)을 읽지 못해 무시한다: {exc}")
        return None


def safe_rules(glossary: Path, warnings: list[str]) -> list:
    try:
        return load_rules(glossary)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        warnings.append(f"[writing-guard] 용어집 {glossary}을 읽지 못해 용어 점검을 건너뛴다: {exc}")
        return []


def is_violation(level: str) -> bool:
    return level in ("V", "E")


# ---------------------------------------------------------------- session-start


def on_session_start(_payload: dict) -> None:
    if not RULES_FILE.exists():
        return
    rules = RULES_FILE.read_text(encoding="utf-8")
    first_heading = next((line for line in rules.splitlines() if line.startswith("# ")), "")
    if first_heading and USER_CLAUDE_MD.exists() and first_heading in USER_CLAUDE_MD.read_text(encoding="utf-8"):
        return  # 사용자가 CLAUDE.md에 같은 규칙을 이미 두었다
    context_output("SessionStart", [rules])


# ---------------------------------------------------------------- prompt


def on_prompt(payload: dict) -> None:
    prune_stale()
    skip = skip_requested(str(payload.get("prompt", "")))
    with locked_state(payload) as (path, state):
        if state is not None or skip:
            save_state(path, replace(state or State(), skip_turn=skip, checked_files=()))
        chat = load_chat(chat_path(path))
        turns = int(chat.get("turns", 0)) + 1
        feedback = [str(item) for item in chat.get("feedback", [])]
        save_chat(chat_path(path), {"turns": turns, "feedback": []})
    lines: list[str] = []
    remind = not REMINDER_OFF.exists() and (turns == 1 or turns % REMIND_EVERY == 0 or bool(feedback))
    if remind:
        lines.append(REMINDER)
        project = safe_project(Path(payload.get("cwd") or "."), lines)
        if project and project.glossary and project.glossary.exists():
            lines.append(GLOSSARY_NOTE.format(path=project.glossary))
    if feedback:
        lines += [FEEDBACK_HEAD, *(f"  - {item}" for item in feedback)]
    if skip:
        lines.append(SKIP_NOTE)
    context_output("UserPromptSubmit", lines)


# ---------------------------------------------------------------- post-edit


def before_after(tool_name: str, tool_input: dict, tool_response: dict) -> tuple[str, str]:
    if tool_name == "Edit":
        return tool_input.get("old_string", ""), tool_input.get("new_string", "")
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits", [])
        return (
            "\n".join(edit.get("old_string", "") for edit in edits),
            "\n".join(edit.get("new_string", "") for edit in edits),
        )
    original = tool_response.get("originalFile") if isinstance(tool_response, dict) else None
    return (original if isinstance(original, str) else ""), tool_input.get("content", "")


def target_path(payload: dict) -> Path | None:
    tool_input = payload.get("tool_input", {})
    response = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
    raw = tool_input.get("file_path") or response.get("filePath")
    if not raw:
        return None
    path = Path(raw)
    return (path if path.is_absolute() else Path(payload.get("cwd") or ".") / path).resolve()


def term_lines(hits, path: Path, glossary: Path) -> list[str]:
    lines: list[str] = []
    found = [hit for hit in hits if is_violation(hit.level)]
    review = [hit for hit in hits if hit.level == "R"]
    if found:
        lines.append(f"[용어 위반] {path.name}의 방금 고친 줄에 확정 표기와 다른 말 {len(found)}건. 바로 고쳐라:")
        lines += [f"  - '{hit.word}' → {hit.hint}   (…{hit.context}…)" for hit in found[:SHOWN_MAX]]
    if review:
        lines.append(f"[용어 검토] {len(review)}건. 문맥을 보고 판단하라:")
        lines += [f"  - '{hit.word}' → {hit.hint}" for hit in review[:SHOWN_MAX]]
    if found or review:
        lines.append(f"용어집: {glossary}. 용어집에 없는 새 용어는 임의로 정하지 말고 사용자에게 묻는다.")
    return lines


def on_post_edit(payload: dict) -> None:
    path = target_path(payload)
    if path is None or not is_prose_path(path):
        return
    warnings: list[str] = []
    project = safe_project(path, warnings)
    if project and project.excludes(path):
        return
    old, new = before_after(
        payload.get("tool_name", ""), payload.get("tool_input") or {}, payload.get("tool_response") or {}
    )
    old, new = old or "", new or ""
    blocks = added_blocks(extract(old, path.suffix), extract(new, path.suffix))
    glossary = project.glossary if project and project.glossary and project.glossary.exists() else None
    hits = []
    if glossary:
        rules = safe_rules(glossary, warnings)
        term_blocks = blocks
        if path.suffix.lower() == ".tex":  # 용어 규칙에는 LaTeX 명령이 들어갈 수 있어 명령을 남긴 텍스트로 본다
            term_blocks = added_blocks(
                extract(old, path.suffix, keep_commands=True), extract(new, path.suffix, keep_commands=True)
            )
        # 줄마다 따로 찾아야 문맥이 파일에 실제로 있는 문자열이 되고, Stop에서 다시 찾을 수 있다
        hits = [hit for block in term_blocks for hit in find_hits(block.lines, rules)]
    if not blocks and not hits:
        context_output("PostToolUse", warnings)
        return

    with locked_state(payload) as (state_path, prev):
        state = prev or State()
        if state.skip_turn:
            context_output("PostToolUse", warnings)
            return
        counted = (project is None or project.humanize) and str(path) not in state.checked_files
        ko, en = measure(blocks) if counted else (0, 0)
        opened = tuple((str(path), hit.context) for hit in hits if is_violation(hit.level))
        state = replace(
            state,
            ko_chars=state.ko_chars + ko,
            en_words=state.en_words + en,
            files=tuple(dict.fromkeys((*state.files, str(path)))) if (ko or en) else state.files,
            term_open=tuple(dict.fromkeys((*state.term_open, *opened))),
            glossaries=tuple(dict.fromkeys((*state.glossaries, str(glossary)))) if opened else state.glossaries,
        )
        lines = warnings + (term_lines(hits, path, glossary) if glossary else [])
        langs = state.pending_languages()
        if langs and not state.notified:
            lines.append(
                INSTRUCTION.format(
                    files=state.file_names(),
                    ko_limit=KO_CHAR_LIMIT,
                    en_limit=EN_WORD_LIMIT,
                    ko=state.ko_chars,
                    en=state.en_words,
                    skills=skills_for(langs),
                )
            )
            state = replace(state, notified=True)
        if state != State():
            save_state(state_path, state)
    context_output("PostToolUse", lines)


# ---------------------------------------------------------------- post-skill


def on_post_skill(payload: dict) -> None:
    skill = str(payload.get("tool_input", {}).get("skill", "")).split(":")[-1]
    if skill not in EN_SKILLS | KO_SKILLS:
        return
    with locked_state(payload) as (path, state):
        if state is None:
            return
        if skill in EN_SKILLS:
            state = replace(state, en_words=0)
        else:
            state = replace(state, ko_chars=0)
        checked = tuple(dict.fromkeys((*state.checked_files, *state.files)))
        files = () if not state.pending_languages() else state.files
        save_state(path, replace(state, files=files, checked_files=checked, notified=False))


# ---------------------------------------------------------------- stop


def still_open(term_open: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """고쳐지지 않고 파일에 그대로 남은 (파일, 문맥)만 돌려준다."""
    cache: dict[str, str] = {}
    remaining = []
    for file, context in term_open:
        if file not in cache:
            target = Path(file)
            text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            cache[file] = collapse(extract(text, target.suffix, keep_commands=True))
        if context in cache[file]:
            remaining.append((file, context))
    return tuple(remaining)


def record_chat_feedback(payload: dict) -> None:
    """방금 쓴 채팅 답변을 점검해 다음 턴에 알릴 결과를 저장한다. 응답 종료는 막지 않는다."""
    if CHAT_OFF.exists():
        return
    transcript = payload.get("transcript_path")
    reply, user_texts = "", []
    if transcript and Path(transcript).exists():
        reply, user_texts = read_transcript(Path(transcript))
    if not reply and isinstance(payload.get("last_assistant_message"), str):
        reply = payload["last_assistant_message"]
    issues = find_issues(reply, [*user_texts, known_terms()], cwd=str(payload.get("cwd") or ""))
    with locked_state(payload) as (path, _state):
        chat = load_chat(chat_path(path))
        save_chat(chat_path(path), {**chat, "feedback": issues})


def on_stop(payload: dict) -> None:
    try:
        record_chat_feedback(payload)
    except Exception as exc:  # 채팅 점검 실패가 문서 점검까지 막지 않게 한다. 원인은 로그로 남긴다
        traceback.print_exc(file=sys.stderr)
        print(f"writing-guard: 채팅 답변 점검 실패: {exc!r}", file=sys.stderr)
    with locked_state(payload) as (path, state):
        if state is None:
            return
        if state.skip_turn:
            drop_state(path)
            return
        open_terms = still_open(state.term_open)
        langs = state.pending_languages()
        if not langs and not open_terms:
            drop_state(path)
            return
        files = state.file_names()
        if state.blocks >= MAX_BLOCKS:
            drop_state(path)
            pending = []
            if langs:
                pending.append(f"AI 말투 점검 미실행({skills_for(langs)})")
            if open_terms:
                pending.append(f"용어 위반 {len(open_terms)}건 미수정")
            emit(
                {
                    "systemMessage": f"writing-guard: {', '.join(pending)} 상태로 종료합니다. "
                    f"{MAX_BLOCKS}회 막았지만 해결되지 않았습니다 (대상: {files or '용어 위반 파일'})."
                }
            )
            return
        save_state(path, replace(state, blocks=state.blocks + 1, term_open=open_terms))
    reasons = []
    if open_terms:
        spots = " / ".join(f"{Path(file).name}: …{ctx}…" for file, ctx in open_terms[:SHOWN_MAX])
        reasons.append(BLOCK_TERMS.format(glossary=", ".join(state.glossaries), spots=spots))
    if langs:
        reasons.append(
            BLOCK_HUMANIZE.format(files=files, ko=state.ko_chars, en=state.en_words, skills=skills_for(langs))
        )
    emit({"decision": "block", "reason": " ".join(reasons)})


HANDLERS = {"session-start": on_session_start, "prompt": on_prompt, "post-edit": on_post_edit, "post-skill": on_post_skill, "stop": on_stop}


def main(argv: list[str]) -> int:
    if OFF_SWITCH.exists():
        return 0
    command = argv[1] if len(argv) > 1 else ""
    handler = HANDLERS.get(command)
    if handler is None:
        print(f"usage: {Path(argv[0]).name} {{{'|'.join(HANDLERS)}}}", file=sys.stderr)
        return 1
    def give_up(_signum, _frame):
        raise TimeoutError(f"{TIME_LIMIT_SECONDS}초 안에 끝나지 않아 이번 점검을 건너뜀")

    signal.signal(signal.SIGALRM, give_up)
    signal.alarm(TIME_LIMIT_SECONDS)
    try:
        handler(read_payload())
    except Exception as exc:  # 훅 오류로 작업을 멈추지 않되, 숨기지 않고 사용자에게 보인다
        traceback.print_exc(file=sys.stderr)
        emit({"systemMessage": f"writing-guard 오류 ({command}): {exc!r}"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
