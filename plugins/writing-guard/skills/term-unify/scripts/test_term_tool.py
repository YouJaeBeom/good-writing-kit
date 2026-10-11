"""term_tool 테스트. 실행: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q test_term_tool.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import prose  # noqa: E402
import term_tool  # noqa: E402


@pytest.fixture(autouse=True)
def allow_tmp(monkeypatch):
    monkeypatch.setattr(prose, "TMP_PREFIXES", ())


@pytest.fixture
def project(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text(
        "We use cross-reference checks. The Agent Majority rule applies.\n"
        "A cross reference table and the agent majority vote.\n"
        "정치 성향을 분석한다. 정치성향 분포를 본다.\n"
        "Large Language Model (LLM) 을 쓴다. RAG 를 쓴다.\n"
        "```\ncode_block_should_be_ignored cross_reference\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.md").write_text("cross-ref", encoding="utf-8")
    return tmp_path


def test_scan_finds_spelling_spacing_and_abbreviation_issues(project):
    report = term_tool.scan(project)
    assert report["files"] == 1
    forms = [{v["form"] for v in c["variants"]} for c in report["english_variants"]]
    assert {"cross-reference", "cross reference"} in forms
    assert {"Agent Majority", "agent majority"} in forms
    assert not any("cross_reference" in f for group in forms for f in group)
    assert [(c["spaced"], c["joined"]) for c in report["korean_spacing"]] == [("정치 성향", "정치성향")]
    abbrs = {a["abbr"]: a for a in report["abbreviations"]}
    assert abbrs["LLM"]["defined_at"] == "docs/a.md:4" and abbrs["RAG"]["defined_at"] is None
    assert "정치 성향" in term_tool.scan_markdown(report)


def test_sentence_initial_capital_is_not_reported(tmp_path):
    (tmp_path / "a.md").write_text("Results are good.\nThe results are good.\n", encoding="utf-8")
    forms = [{v["form"] for v in c["variants"]} for c in term_tool.scan(tmp_path)["english_variants"]]
    assert {"Results", "results"} not in forms


def test_make_pattern_avoids_canonical_overlap():
    pattern = term_tool.make_pattern("성향", canonical="정치 성향")
    import re

    assert re.search(pattern, "기사 성향") and not re.search(pattern, "정치 성향")
    ascii_pattern = term_tool.make_pattern("cross-ref", canonical="cross-reference")
    assert re.search(ascii_pattern, "a cross-ref here") and not re.search(ascii_pattern, "cross-reference")
    with pytest.raises(ValueError):
        term_tool.make_pattern("성향", canonical="성향")


def test_add_check_validate_render_register_lookup(project, capsys):
    glossary = project / ".claude" / "glossary.json"
    assert term_tool.main(["init", str(glossary)]) == 0
    assert term_tool.main([
        "add", str(glossary), "--group", "분석 대상", "--id", "leaning", "--ko", "정치 성향",
        "--en", "political leaning", "--avoid", "정치성향, 성향 편향", "--meaning", "기사의 입장",
    ]) == 0
    data = json.loads(glossary.read_text(encoding="utf-8"))
    entry = data["groups"][0]["entries"][0]
    assert entry["avoid"] == "정치성향, 성향 편향" and len(entry["rules"]) == 2
    assert term_tool.main(["validate", str(glossary)]) == 0
    capsys.readouterr()
    assert term_tool.main(["check", str(glossary), str(project / "docs")]) == 1
    assert "a.md:3 [V] '정치성향'" in capsys.readouterr().out
    assert term_tool.main(["render", str(glossary)]) == 0
    assert "| 정치 성향 | political leaning |" in glossary.with_suffix(".md").read_text(encoding="utf-8")
    assert term_tool.main(["register", str(project), str(glossary)]) == 0
    config = json.loads((project / ".claude" / "writing-guard.json").read_text(encoding="utf-8"))
    assert config == {"glossary": ".claude/glossary.json"}
    assert term_tool.main(["lookup", str(glossary), "political"]) == 0
    assert term_tool.main(["add", str(glossary), "--group", "x", "--id", "leaning"]) == 2


def test_validate_reports_canonical_conflict(tmp_path):
    glossary = tmp_path / "g.json"
    glossary.write_text(json.dumps({"groups": [{"title": "t", "entries": [
        {"id": "a", "ko": "정치 성향", "en": "", "meaning": "", "avoid": "", "decided": "",
         "rules": [{"pattern": "성향", "level": "V", "hint": ""}]}
    ]}]}, ensure_ascii=False), encoding="utf-8")
    assert any("정본 표기" in p for p in term_tool.validate(glossary))


def test_scan_works_under_a_folder_named_build_and_skips_inner_dependencies(tmp_path):
    root = tmp_path / "build" / "paper"
    (root / "node_modules").mkdir(parents=True)
    (root / "a.md").write_text("cross-ref and cross-reference\n", encoding="utf-8")
    (root / "node_modules" / "b.md").write_text("ignored", encoding="utf-8")
    assert [p.name for p in term_tool.prose_files(root)] == ["a.md"]


def test_validate_and_add_survive_entries_without_id(tmp_path):
    glossary = tmp_path / "g.json"
    glossary.write_text(json.dumps({"groups": [{"title": "t", "entries": [
        {"ko": "가", "en": "", "rules": []}, {"ko": "나", "en": "", "rules": []}
    ]}]}, ensure_ascii=False), encoding="utf-8")
    assert any("빠진 필드" in p for p in term_tool.validate(glossary))
    assert term_tool.main(["add", str(glossary), "--group", "t", "--id", "new", "--ko", "다"]) == 0
