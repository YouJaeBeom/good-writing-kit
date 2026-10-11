"""writing-guard 훅 테스트. 실행: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import chat_check  # noqa: E402
import prose  # noqa: E402
import term_rules  # noqa: E402
import writing_guard as guard  # noqa: E402

KO_PARAGRAPH = "이 연구는 기사 정치 성향을 판정하는 여러 에이전트의 결과를 모아 최종 판정을 내리는 방법을 제안한다. " * 8
EN_PARAGRAPH = "This study proposes a method that aggregates several agent judgments into one final label. " * 6


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(guard, "CLAUDE_HOME", home / ".claude")
    monkeypatch.setattr(guard, "STATE_DIR", home / ".claude" / "state" / "writing-guard")
    monkeypatch.setattr(guard, "OFF_SWITCH", home / ".claude" / "writing-guard.off")
    monkeypatch.setattr(guard, "REMINDER_OFF", home / ".claude" / "writing-guard-reminder.off")
    monkeypatch.setattr(guard, "CHAT_OFF", home / ".claude" / "writing-guard-chat.off")
    monkeypatch.setattr(guard, "KNOWN_TERMS", home / ".claude" / "writing-guard-known-terms.txt")
    monkeypatch.setattr(prose, "TMP_PREFIXES", ())
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return tmp_path


def run(capsys, handler, payload) -> dict | None:
    handler(payload)
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def edit(path: Path, old: str, new: str, session="s1") -> dict:
    path.write_text(new, encoding="utf-8")
    return {
        "session_id": session,
        "tool_name": "Edit",
        "cwd": str(path.parent),
        "tool_input": {"file_path": str(path), "old_string": old, "new_string": new},
        "tool_response": {"filePath": str(path)},
    }


def make_project(root: Path, rules: list[dict], config: dict | None = None) -> Path:
    glossary = root / ".claude" / "glossary.json"
    glossary.parent.mkdir(parents=True, exist_ok=True)
    data = {"updated": "2026-10-11", "intro": "", "groups": [{"title": "t", "note": "", "entries": [
        {"id": "term", "ko": "기사 정치 성향", "en": "article political leaning", "meaning": "", "avoid": "", "decided": "", "rules": rules}
    ]}]}
    glossary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if config is not None:
        (root / ".claude" / "writing-guard.json").write_text(json.dumps(config), encoding="utf-8")
    return glossary


# ---------------------------------------------------------------- prose


def test_markdown_extract_drops_code_links_and_front_matter_but_keeps_lines():
    text = "---\ntitle: x\n---\n본문 `code` 입니다 [링크](http://a.b)\n```py\nprint(1)\n```\n끝"
    out = prose.extract(text, ".md")
    assert out.count("\n") == text.count("\n")
    assert "print" not in out and "code" not in out and "http" not in out and "title" not in out
    assert "본문" in out and "링크" in out and "끝" in out


def test_tex_extract_drops_cite_math_and_comments():
    text = "결과는 \\cite{a} 좋다 $x^2$ % 주석\n\\begin{equation}\ny=1\n\\end{equation}\n끝"
    out = prose.extract(text, ".tex")
    assert "cite" not in out and "x^2" not in out and "주석" not in out and "y=1" not in out
    assert "결과는" in out and "끝" in out


def test_added_blocks_counts_only_new_words_inside_a_changed_line():
    blocks = prose.added_blocks("모델은 좋은 결과를 낸다.", "모델은 매우 좋은 결과를 낸다.")
    assert [b.text for b in blocks] == ["매우"]
    assert blocks[0].korean and blocks[0].lines == "모델은 매우 좋은 결과를 낸다."
    assert prose.measure(blocks) == (2, 0)


def test_measure_splits_korean_and_english_lines():
    blocks = prose.added_blocks("", "한국어 문장입니다.\nAn English sentence here.")
    assert prose.measure(blocks) == (len("한국어문장입니다."), 4)


def test_is_prose_path_skips_agent_files_and_dependencies(tmp_path):
    assert prose.is_prose_path(tmp_path / "README.md")
    assert prose.is_prose_path(tmp_path / "paper" / "intro.tex")
    for skipped in (".claude/x.md", "a/SKILL.md", "CLAUDE.md", "requirements.txt", "node_modules/a.md", "a.py"):
        assert not prose.is_prose_path(tmp_path / skipped), skipped


def test_large_blocks_skip_token_diff():
    old = " ".join(f"w{i}" for i in range(prose.MAX_DIFF_TOKENS + 1))
    new = old + " extra"
    assert prose._added_tokens(old.split(), new.split()) == list(range(len(new.split())))


# ---------------------------------------------------------------- term_rules


def test_find_hits_reports_word_and_context(tmp_path):
    glossary = make_project(tmp_path, [{"pattern": "기사 성향", "level": "V", "hint": "'기사 정치 성향'으로 쓴다"}])
    hits = term_rules.find_hits("우리는 기사 성향을 예측한다.", term_rules.load_rules(glossary))
    assert [(h.word, h.level) for h in hits] == [("기사 성향", "V")]
    assert "기사 성향을" in hits[0].context


def test_invalid_regex_is_reported(tmp_path):
    glossary = make_project(tmp_path, [{"pattern": "(", "level": "V", "hint": ""}])
    with pytest.raises(ValueError, match="정규식 오류"):
        term_rules.load_rules(glossary)


def test_find_project_prefers_config_and_stops_at_home(tmp_path):
    project = tmp_path / "proj"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "g.json").write_text("{}", encoding="utf-8")
    (project / ".claude").mkdir()
    (project / ".claude" / "writing-guard.json").write_text(
        json.dumps({"glossary": "docs/g.json", "exclude": ["drafts/**"], "humanize": False}), encoding="utf-8"
    )
    config = term_rules.find_project(project / "docs" / "a.md")
    assert config.glossary == project / "docs" / "g.json" and config.humanize is False
    assert config.excludes(project / "drafts" / "x.md") and not config.excludes(project / "docs" / "a.md")
    assert term_rules.find_project(tmp_path / "home" / "a.md") is None


def test_find_project_uses_default_glossary_without_config(tmp_path):
    glossary = make_project(tmp_path / "p", [])
    assert term_rules.find_project(tmp_path / "p" / "a.md").glossary == glossary


# ---------------------------------------------------------------- guard: humanize


def test_korean_paragraph_triggers_instruction_once_then_stop_blocks(tmp_path, capsys):
    doc = tmp_path / "doc.md"
    out = run(capsys, guard.on_post_edit, edit(doc, "", KO_PARAGRAPH))
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "AI 말투 점검 필요" in context and "humanize-scan" in context and "humanizer" not in context
    assert run(capsys, guard.on_post_edit, edit(doc, KO_PARAGRAPH, KO_PARAGRAPH + "\n추가 문장.")) is None
    blocked = run(capsys, guard.on_stop, {"session_id": "s1"})
    assert blocked["decision"] == "block" and "doc.md" in blocked["reason"]


def test_stop_gives_up_after_max_blocks(tmp_path, capsys):
    run(capsys, guard.on_post_edit, edit(tmp_path / "doc.md", "", EN_PARAGRAPH))
    for _ in range(guard.MAX_BLOCKS):
        assert run(capsys, guard.on_stop, {"session_id": "s1"})["decision"] == "block"
    final = run(capsys, guard.on_stop, {"session_id": "s1"})
    assert "systemMessage" in final and "humanizer" in final["systemMessage"]
    assert run(capsys, guard.on_stop, {"session_id": "s1"}) is None


def test_skill_clears_only_its_language_and_ignores_apply_edits(tmp_path, capsys):
    ko_doc, en_doc = tmp_path / "ko.md", tmp_path / "en.md"
    run(capsys, guard.on_post_edit, edit(ko_doc, "", KO_PARAGRAPH))
    run(capsys, guard.on_post_edit, edit(en_doc, "", EN_PARAGRAPH))
    run(capsys, guard.on_post_skill, {"session_id": "s1", "tool_input": {"skill": "humanize-scan"}})
    blocked = run(capsys, guard.on_stop, {"session_id": "s1"})
    assert "humanizer" in blocked["reason"] and "humanize-scan" not in blocked["reason"]
    run(capsys, guard.on_post_skill, {"session_id": "s1", "tool_input": {"skill": "humanizer"}})
    rewritten = KO_PARAGRAPH.replace("제안한다", "내놓는다")
    run(capsys, guard.on_post_edit, edit(ko_doc, KO_PARAGRAPH, rewritten))  # 교정본 반영은 집계하지 않는다
    assert run(capsys, guard.on_stop, {"session_id": "s1"}) is None


def test_write_counts_only_the_difference_from_original_file(tmp_path, capsys):
    doc = tmp_path / "doc.md"
    payload = {
        "session_id": "s1",
        "tool_name": "Write",
        "tool_input": {"file_path": str(doc), "content": KO_PARAGRAPH + "\n짧은 줄."},
        "tool_response": {"originalFile": KO_PARAGRAPH},
    }
    doc.write_text(KO_PARAGRAPH + "\n짧은 줄.", encoding="utf-8")
    assert run(capsys, guard.on_post_edit, payload) is None
    assert run(capsys, guard.on_stop, {"session_id": "s1"}) is None


def test_non_prose_and_excluded_files_are_ignored(tmp_path, capsys):
    assert run(capsys, guard.on_post_edit, edit(tmp_path / "a.py", "", EN_PARAGRAPH)) is None
    project = tmp_path / "p"
    make_project(project, [], config={"exclude": ["drafts/**"]})
    (project / "drafts").mkdir()
    assert run(capsys, guard.on_post_edit, edit(project / "drafts" / "x.md", "", EN_PARAGRAPH)) is None
    assert run(capsys, guard.on_stop, {"session_id": "s1"}) is None


def test_project_can_turn_humanize_off(tmp_path, capsys):
    project = tmp_path / "p"
    make_project(project, [], config={"humanize": False})
    assert run(capsys, guard.on_post_edit, edit(project / "a.md", "", EN_PARAGRAPH)) is None
    assert run(capsys, guard.on_stop, {"session_id": "s1"}) is None


# ---------------------------------------------------------------- guard: prompt


def test_skip_request_turns_off_counting_for_the_turn(tmp_path, capsys):
    out = run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "이 문단 원문 유지해서 넣어줘", "cwd": str(tmp_path)})
    assert guard.SKIP_NOTE in out["hookSpecificOutput"]["additionalContext"]
    assert run(capsys, guard.on_post_edit, edit(tmp_path / "doc.md", "", KO_PARAGRAPH)) is None
    assert run(capsys, guard.on_stop, {"session_id": "s1"}) is None
    run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "다음 절 써줘", "cwd": str(tmp_path)})
    assert run(capsys, guard.on_post_edit, edit(tmp_path / "doc2.md", "", KO_PARAGRAPH)) is not None


def test_prompt_reminder_mentions_project_glossary_and_can_be_turned_off(tmp_path, capsys):
    project = tmp_path / "p"
    glossary = make_project(project, [])
    out = run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "설명해줘", "cwd": str(project)})
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "설명 기준" in context and str(glossary) in context
    guard.REMINDER_OFF.write_text("", encoding="utf-8")
    assert run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "설명해줘", "cwd": str(project)}) is None


# ---------------------------------------------------------------- guard: terms


def test_term_violation_blocks_until_fixed(tmp_path, capsys):
    project = tmp_path / "p"
    make_project(project, [
        {"pattern": "기사 성향", "level": "V", "hint": "'기사 정치 성향'으로 쓴다"},
        {"pattern": "편향", "level": "R", "hint": "뜻을 확인한다"},
    ])
    doc = project / "a.md"
    out = run(capsys, guard.on_post_edit, edit(doc, "", "우리는 기사 성향과 편향을 본다."))
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "[용어 위반]" in context and "[용어 검토]" in context
    blocked = run(capsys, guard.on_stop, {"session_id": "s1"})
    assert blocked["decision"] == "block" and "기사 성향" in blocked["reason"]
    doc.write_text("우리는 기사 정치 성향과 편향을 본다.", encoding="utf-8")
    assert run(capsys, guard.on_stop, {"session_id": "s1"}) is None


# ---------------------------------------------------------------- main


def test_main_reports_errors_without_failing(capsys, monkeypatch):
    def boom(_payload):
        raise RuntimeError("bad")

    monkeypatch.setitem(guard.HANDLERS, "stop", boom)
    monkeypatch.setattr(guard, "read_payload", lambda: {})
    assert guard.main(["writing_guard.py", "stop"]) == 0
    assert "writing-guard 오류" in capsys.readouterr().out


def test_main_rejects_unknown_command_and_respects_off_switch(capsys):
    assert guard.main(["writing_guard.py", "nope"]) == 1
    guard.OFF_SWITCH.write_text("", encoding="utf-8")
    assert guard.main(["writing_guard.py", "nope"]) == 0


# ---------------------------------------------------------------- 리뷰에서 나온 회귀 테스트


def test_large_rewrite_with_repeated_lines_finishes_quickly(tmp_path, capsys):
    import time

    old = "\n".join(f"line {i} old text" if i % 2 else "" for i in range(4000))
    new = "\n".join(f"line {i} new words" if i % 2 else "" for i in range(4000))
    tex_old = "\n".join(f"cell {i} & value \\\\" if i % 2 else "\\hline" for i in range(4000))
    tex_new = "\n".join(f"cell {i} & changed \\\\" if i % 2 else "\\hline" for i in range(4000))
    for name, before, after in (("big.md", old, new), ("big.tex", tex_old, tex_new)):
        payload = {
            "session_id": "s1",
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / name), "content": after},
            "tool_response": {"originalFile": before},
        }
        started = time.monotonic()
        guard.on_post_edit(payload)
        assert time.monotonic() - started < 3, name
    capsys.readouterr()


def test_table_rule_does_not_cross_lines_or_backtrack():
    import time

    text = "Intro\n\n|---|---|\nafter BADWORD\n"
    assert prose.extract(text, ".md").count("\n") == text.count("\n")
    started = time.monotonic()
    prose.extract("text\n" + "\n" * 5000 + "tail\n", ".md")
    assert time.monotonic() - started < 0.5


def test_tex_row_break_is_not_display_math():
    out = prose.extract("a & b \\\\[2pt]\nreal prose here\n\\[ x \\]\nafter\n", ".tex")
    assert "real prose here" in out and "after" in out and " x " not in out


def test_term_hit_on_non_adjacent_changed_lines_blocks_until_fixed(tmp_path, capsys):
    project = tmp_path / "p"
    make_project(project, [{"pattern": "천장", "level": "V", "hint": "'상한'으로 쓴다"}])
    doc = project / "a.md"
    old = "첫째 줄 내용입니다 여기까지\n그대로 둘 줄 입니다 변경 없음\n마지막 줄 문장 끝입니다 여기\n"
    new = "첫째 줄 수정되었습니다 여기까지\n그대로 둘 줄 입니다 변경 없음\n천장 값을 쓴 줄 문장 끝입니다\n"
    assert "[용어 위반]" in run(capsys, guard.on_post_edit, edit(doc, old, new))["hookSpecificOutput"]["additionalContext"]
    assert run(capsys, guard.on_stop, {"session_id": "s1"})["decision"] == "block"


def test_broken_glossary_warns_but_still_counts(tmp_path, capsys):
    project = tmp_path / "p"
    make_project(project, [{"pattern": "(unclosed", "level": "V", "hint": ""}])
    context = run(capsys, guard.on_post_edit, edit(project / "a.md", "", KO_PARAGRAPH))["hookSpecificOutput"]["additionalContext"]
    assert "용어집" in context and "읽지 못해" in context and "AI 말투 점검 필요" in context
    assert run(capsys, guard.on_stop, {"session_id": "s1"})["decision"] == "block"


def test_broken_project_config_is_ignored_with_warning(tmp_path, capsys):
    project = tmp_path / "p"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "writing-guard.json").write_text("{bad json,}", encoding="utf-8")
    out = run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "원문 유지해줘", "cwd": str(project)})
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "프로젝트 설정" in context and guard.SKIP_NOTE in context


def test_corrupt_or_unknown_state_is_reset(capsys):
    guard.STATE_DIR.mkdir(parents=True, exist_ok=True)
    corrupt = guard.STATE_DIR / "s1.json"
    corrupt.write_text('{"ko_chars": 33', encoding="utf-8")
    assert run(capsys, guard.on_stop, {"session_id": "s1"}) is None
    assert not corrupt.exists()
    corrupt.write_text('{"ko_chars": 400, "future_field": 1}', encoding="utf-8")
    assert run(capsys, guard.on_stop, {"session_id": "s1"})["decision"] == "block"


def test_prune_skips_files_that_vanish(capsys):
    guard.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (guard.STATE_DIR / "gone.json").symlink_to(guard.STATE_DIR / "missing-target")
    guard.prune_stale()  # 끊어진 링크의 stat()이 FileNotFoundError를 내도 멈추지 않는다


@pytest.mark.parametrize("prompt", ["그대로 써줘", "이 문단 원문 유지", "윤문하지 마", "AI 말투 점검 생략", "don't humanize it"])
def test_skip_request_matches_explicit_requests(prompt):
    assert guard.skip_requested(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "keep the API as is",
        "이 함수는 그대로 두고 테스트만 고쳐",
        "print the log verbatim",
        "테스트 점검 생략하고 배포해",
        "약어는 사용자가 먼저 쓴 것만 그대로 쓰고, 나머지는 풀어 씁니다",
        '사용자가 "원문 유지"라고 한 턴은 세지 않는다',
        "“그대로 써줘”라고 하면 건너뛴다",
    ],
)
def test_skip_request_ignores_ordinary_and_quoted_text(prompt):
    assert not guard.skip_requested(prompt)


def test_main_stops_itself_before_hook_timeout(capsys, monkeypatch):
    import time

    monkeypatch.setattr(guard, "TIME_LIMIT_SECONDS", 1)
    monkeypatch.setitem(guard.HANDLERS, "stop", lambda _payload: time.sleep(5))
    monkeypatch.setattr(guard, "read_payload", lambda: {})
    started = time.monotonic()
    assert guard.main(["writing_guard.py", "stop"]) == 0
    assert time.monotonic() - started < 3
    assert "초 안에 끝나지 않아" in capsys.readouterr().out


# ---------------------------------------------------------------- 채팅 답변 점검


def write_transcript(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return path


def human(text: str) -> dict:
    return {"type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def test_read_transcript_returns_this_turns_text_and_human_messages(tmp_path):
    transcript = write_transcript(tmp_path / "t.jsonl", [
        human("첫 질문 RAG"),
        assistant({"type": "text", "text": "이전 답변"}),
        human("두 번째 질문"),
        assistant({"type": "thinking", "thinking": "생각"}, {"type": "text", "text": "앞부분"}),
        assistant({"type": "tool_use", "name": "Bash", "input": {}}),
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "결과"}]}},
        assistant({"type": "text", "text": "뒷부분"}),
    ])
    reply, users = chat_check.read_transcript(transcript)
    assert reply == "앞부분\n뒷부분" and users == ["첫 질문 RAG", "두 번째 질문"]


def test_abbreviation_rules():
    reply = (
        "RAG와 SDA를 비교했다. 검색 증강 생성(RAG)이 아니라 BLC(Backbone Leaning Correction)를 쓴다. "
        "API, PDF는 그대로 둔다. `SKILL.md`와 `NEWLIB` 설정, L7 줄, CLAUDE.md, ORION 폴더, WWW 학회도 본다."
    )
    issues = chat_check.find_issues(reply, ["우리 SDA 실험"], cwd="/home/x/projects/ORION")
    abbr_line = next(i for i in issues if i.startswith("풀어 쓰지 않은 약어"))
    for ok in ("RAG", "SDA", "BLC", "API", "PDF", "SKILL", "NEWLIB", "L7", "CLAUDE", "ORION"):
        assert ok not in abbr_line, ok
    assert "WWW" in abbr_line
    assert chat_check.find_issues(reply, ["우리 SDA 실험", "WWW"], cwd="/home/x/projects/ORION") == []


def test_back_references_skip_quoted_examples():
    assert chat_check.find_issues("'그거', '위 2번' 대신 이름을 쓴다.", []) == []
    issues = chat_check.find_issues("위 2번을 고쳤고 그거는 그대로 뒀다.", [])
    assert any("위 2번" in i and "그거" in i for i in issues)


def test_korean_and_english_ai_tells():
    reply = (
        "속도가 아니라 정확도가 중요하다. 양이 아니라 질이 핵심이다.\n\n"
        "파일을 읽고, 규칙을 적용하며, 결과를 쓰지만, 끝나지 않는다.\n\n"
        "It is not just a tool, but a partner — fast — and calm — sure. Great question!"
    )
    issue = next(i for i in chat_check.find_issues(reply, []) if i.startswith("AI 말투 신호"))
    for part in ("대구 2회", "쉼표 3회", "em dash(—) 3회", "Great question"):
        assert part in issue, part
    assert chat_check.find_issues("짧고 담백한 답변입니다.", []) == []


def test_stop_records_feedback_and_next_prompt_reports_it(tmp_path, capsys):
    transcript = write_transcript(tmp_path / "t.jsonl", [
        human("설명해줘"),
        assistant({"type": "text", "text": "XYZ를 고쳤고 위 2번도 처리했다."}),
    ])
    first = run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "설명해줘", "cwd": str(tmp_path)})
    assert guard.REMINDER in first["hookSpecificOutput"]["additionalContext"]
    assert run(capsys, guard.on_stop, {"session_id": "s1", "transcript_path": str(transcript), "cwd": str(tmp_path)}) is None
    second = run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "다음", "cwd": str(tmp_path)})
    context = second["hookSpecificOutput"]["additionalContext"]
    assert guard.FEEDBACK_HEAD in context and "XYZ" in context and "위 2번" in context and guard.REMINDER in context
    assert run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "또", "cwd": str(tmp_path)}) is None


def test_reminder_is_sent_on_first_turn_and_every_tenth(tmp_path, capsys):
    sent = []
    for turn in range(1, 21):
        out = run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "질문", "cwd": str(tmp_path)})
        if out:
            sent.append(turn)
    assert sent == [1, 10, 20]


def test_chat_check_can_be_turned_off_and_uses_known_terms(tmp_path, capsys):
    transcript = write_transcript(tmp_path / "t.jsonl", [human("질문"), assistant({"type": "text", "text": "QQQ 결과"})])
    guard.KNOWN_TERMS.write_text("# 주석\nQQQ\n", encoding="utf-8")
    run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "질문", "cwd": str(tmp_path)})
    run(capsys, guard.on_stop, {"session_id": "s1", "transcript_path": str(transcript)})
    assert run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "다음", "cwd": str(tmp_path)}) is None
    guard.KNOWN_TERMS.unlink()
    guard.CHAT_OFF.write_text("", encoding="utf-8")
    run(capsys, guard.on_stop, {"session_id": "s1", "transcript_path": str(transcript)})
    assert run(capsys, guard.on_prompt, {"session_id": "s1", "prompt": "또", "cwd": str(tmp_path)}) is None


def test_chat_check_failure_does_not_skip_document_check(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(guard, "read_transcript", lambda _p: (_ for _ in ()).throw(RuntimeError("bad transcript")))
    transcript = write_transcript(tmp_path / "t.jsonl", [human("질문")])
    run(capsys, guard.on_post_edit, edit(tmp_path / "doc.md", "", KO_PARAGRAPH))
    blocked = run(capsys, guard.on_stop, {"session_id": "s1", "transcript_path": str(transcript)})
    assert blocked["decision"] == "block"


def test_quoted_examples_are_not_counted_as_tells():
    reply = '"좋은 질문입니다" 같은 상투구와 "A가 아니라 B" 대구, "쓰고, 넘긴다" 같은 쉼표는 피한다.'
    assert chat_check.find_issues(reply, []) == []


def test_abbreviation_defined_inside_longer_parentheses():
    reply = "검색 증강 생성(Retrieval-Augmented Generation, RAG)은 문서를 찾는다. 이후 RAG는 근거를 쓴다."
    assert chat_check.find_issues(reply, []) == []


# ---------------------------------------------------------------- 플러그인 배포


def test_plugin_component_files_are_not_counted(tmp_path):
    plugin = tmp_path / "kit" / "plugins" / "wg"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (tmp_path / "kit" / ".claude-plugin").mkdir()
    assert not prose.is_prose_path(plugin / "rules" / "writing-rules.md")
    assert not prose.is_prose_path(plugin / "skills" / "x" / "references" / "notes.md")
    assert prose.is_prose_path(tmp_path / "kit" / "README.md")
    assert prose.is_prose_path(plugin / "README.md")


def test_session_start_injects_rules_unless_claude_md_has_them(tmp_path, capsys, monkeypatch):
    rules = tmp_path / "writing-rules.md"
    rules.write_text("# 글쓰기: AI 말투 제거\n규칙 본문", encoding="utf-8")
    monkeypatch.setattr(guard, "RULES_FILE", rules)
    monkeypatch.setattr(guard, "USER_CLAUDE_MD", guard.CLAUDE_HOME / "CLAUDE.md")
    out = run(capsys, guard.on_session_start, {})
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "규칙 본문" in out["hookSpecificOutput"]["additionalContext"]
    guard.USER_CLAUDE_MD.write_text("# 다른 규칙\n\n# 글쓰기: AI 말투 제거\n...", encoding="utf-8")
    assert run(capsys, guard.on_session_start, {}) is None
