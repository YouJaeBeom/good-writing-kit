"""프로젝트 용어집(glossary.json)을 읽어 표기 규칙으로 바꾸고, 글에서 규칙 위반을 찾는다.

용어집 형식은 다음과 같다.
  {"updated": "YYYY-MM-DD", "intro": "...",
   "groups": [{"title": "...", "note": "...",
               "entries": [{"id", "ko", "en", "meaning", "avoid", "decided",
                            "rules": [{"pattern": 정규식, "level": "V|R|E", "hint": "..."}]}]}]}
level V는 위반(반드시 고침), R은 문맥 검토, E는 프로젝트가 정한 표기 규칙 위반으로 V처럼 다룬다.

프로젝트 설정은 가장 가까운 상위 폴더의 .claude/writing-guard.json이다.
  {"glossary": "docs/glossary.json", "exclude": ["drafts/**"], "humanize": true}
설정 파일이 없어도 .claude/glossary.json이 있으면 그 용어집을 쓴다.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "writing-guard.json"
DEFAULT_GLOSSARY = Path(".claude") / "glossary.json"
CONTEXT_CHARS = 15


@dataclass(frozen=True)
class Rule:
    entry_id: str
    rx: re.Pattern
    level: str
    hint: str


@dataclass(frozen=True)
class Hit:
    entry_id: str
    level: str
    word: str
    hint: str
    context: str


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    glossary: Path | None
    exclude: tuple[str, ...]
    humanize: bool

    def excludes(self, path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return False
        return any(fnmatch.fnmatch(rel, pattern) for pattern in self.exclude)


def entries(data: dict) -> list[dict]:
    """groups 안의 항목과 최상위 entries 항목을 모두 돌려준다."""
    found = [entry for group in data.get("groups", []) for entry in group.get("entries", [])]
    return found + list(data.get("entries", []))


def load_rules(path: Path) -> list[Rule]:
    """용어집의 정규식 규칙을 컴파일한다. 잘못된 정규식은 ValueError로 알린다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rules: list[Rule] = []
    for entry in entries(data):
        for rule in entry.get("rules", []):
            try:
                rx = re.compile(rule["pattern"])
            except re.error as exc:
                raise ValueError(f"{path}: {entry.get('id')} 정규식 오류 {rule['pattern']!r}: {exc}") from exc
            rules.append(Rule(entry.get("id", "?"), rx, rule.get("level", "V"), rule.get("hint", "")))
    return rules


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def find_hits(text: str, rules: list[Rule]) -> list[Hit]:
    flat = collapse(text)
    hits: list[Hit] = []
    for rule in rules:
        for match in rule.rx.finditer(flat):
            start = max(0, match.start() - CONTEXT_CHARS)
            context = flat[start : match.end() + CONTEXT_CHARS].strip()
            hits.append(Hit(rule.entry_id, rule.level, match.group(0), rule.hint, context))
    return hits


def find_project(start: Path, home: Path | None = None) -> ProjectConfig | None:
    """start에서 위로 올라가며 프로젝트 설정을 찾는다. 홈 폴더 자체는 프로젝트로 보지 않는다."""
    home = (home or Path.home()).resolve()
    current = start.resolve()
    if current.is_file() or not current.exists():
        current = current.parent
    for folder in (current, *current.parents):
        if folder == home or folder == folder.parent:
            return None
        config_file = folder / ".claude" / CONFIG_NAME
        default = folder / DEFAULT_GLOSSARY
        if config_file.exists():
            config = json.loads(config_file.read_text(encoding="utf-8"))
            glossary = config.get("glossary")
            glossary_path = (folder / glossary) if glossary else (default if default.exists() else None)
            return ProjectConfig(
                root=folder,
                glossary=glossary_path,
                exclude=tuple(config.get("exclude", ())),
                humanize=bool(config.get("humanize", True)),
            )
        if default.exists():
            return ProjectConfig(root=folder, glossary=default, exclude=(), humanize=True)
    return None
