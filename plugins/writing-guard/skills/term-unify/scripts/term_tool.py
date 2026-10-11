#!/usr/bin/env python3
"""term-unify 스킬 도구. 프로젝트 문서의 표기 변이를 찾고, 용어집(glossary.json)을 만들고 점검한다.

  scan ROOT [--include GLOB ...] [--json]   표기 변이 후보(영문 철자·대소문자·하이픈, 국문 띄어쓰기, 약어 정의 여부)
  init GLOSSARY                              빈 용어집을 만든다
  add GLOSSARY --group 제목 --id ID --ko 국문 --en 영문 [--meaning 뜻] [--avoid "말1, 말2"] [--level V|R]
                                             항목을 추가하고 avoid 목록으로 점검 규칙을 자동 생성한다
  pattern VARIANT [--canonical 정본] [--ignore-case]   정본과 겹치지 않는 안전한 점검 정규식을 만든다
  check GLOSSARY PATH...                     문서에서 용어집 위반을 file:line으로 보고한다 (V/E 위반이 있으면 exit 1)
  validate GLOSSARY                          정규식 오류, 중복 id, 정본 표기와 위반 규칙의 충돌을 찾는다
  render GLOSSARY [OUT.md]                   사람이 읽는 용어집 표를 만든다 (기본: GLOSSARY와 같은 폴더의 .md)
  register ROOT GLOSSARY                     ROOT/.claude/writing-guard.json에 용어집 경로를 등록해 전역 훅이 점검하게 한다
  lookup GLOSSARY WORD                       국문, 영문, 쓰지 않는 말에서 단어를 찾는다

용어집 형식과 점검 로직은 플러그인의 scripts/term_rules.py와 같다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

def _guard_dir() -> Path:
    """점검 코드(prose.py)가 있는 폴더. 플러그인 안에서는 <플러그인>/scripts다."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "prose.py").exists():
            return parent / "scripts"
    return Path.home() / ".claude" / "scripts" / "writing-guard"


GUARD_DIR = _guard_dir()
sys.path.insert(0, str(GUARD_DIR))

from prose import EXCLUDED_DIRS, extract, is_prose_name  # noqa: E402
from term_rules import CONFIG_NAME, entries, find_hits, find_project, load_rules  # noqa: E402

EN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*")
HANGUL_RUN = re.compile(r"[가-힣]+")
ABBR = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*)(?![A-Za-z0-9])")
SENTENCE_START = set(".!?:;#*-|>(\"'“")
STOPWORDS = frozenset(
    "a an the of in on at to for from by with and or but is are was were be been this that these those it its "
    "we our you your they their as not no can may will".split()
)
PARTICLES = ("에서", "으로", "에게", "까지", "부터", "보다", "처럼", "이나", "과", "와", "을", "를", "이", "가", "은", "는", "의", "에", "로", "도", "만")
LIMIT = 40
LEVELS = ("V", "R", "E")


# ---------------------------------------------------------------- 파일


def prose_files(root: Path, includes: list[str] | None = None) -> list[Path]:
    """root 아래 문서 파일. 제외 폴더(node_modules, .git 등)는 root 안쪽 경로만 보고 들어가지 않는다."""
    project = find_project(root)
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
        for name in sorted(names):
            path = Path(folder) / name
            rel = path.relative_to(root).as_posix()
            if not is_prose_name(path):
                continue
            if includes and not any(fnmatch.fnmatch(rel, pattern) for pattern in includes):
                continue
            if project and project.excludes(path):
                continue
            found.append(path)
    return found


def prose_lines(path: Path, keep_commands: bool = False) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return extract(text, path.suffix, keep_commands=keep_commands).splitlines()


def where(path: Path, root: Path, line_no: int) -> str:
    return f"{path.relative_to(root).as_posix()}:{line_no}"


# ---------------------------------------------------------------- scan


def _is_sentence_start(line: str, start: int) -> bool:
    prefix = line[:start].rstrip()
    return not prefix or prefix[-1] in SENTENCE_START


def _strip_particle(word: str) -> str:
    for particle in PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[: -len(particle)]
    return word


def _meaningful_case_variant(surfaces: list[str], mid_caps: Counter) -> bool:
    """대소문자만 다른 변이 가운데 용어 표기 차이로 볼 만한 것만 남긴다.

    한 단어짜리는 제목과 표 머리글 때문에 흔히 생기므로 단어 안쪽 대문자(GPT-4o, BiLSTM)가 다를 때만 남긴다.
    여러 단어짜리는 첫 글자 말고도 다르면 남기고(LLM ceiling / LLM Ceiling), 첫 글자만 다르면
    문장 중간에서 대문자로 쓴 적이 있을 때만 남긴다.
    """
    if all(" " not in s for s in surfaces):
        mixed = [s for s in surfaces if not s.isupper()]  # 전부 대문자인 표기는 제목·머리글로 본다
        return len({s[1:] for s in mixed}) > 1
    if len({s[1:] for s in surfaces}) > 1:
        return True
    return any(mid_caps[s] for s in surfaces if s[0].isupper())


def _en_clusters(corpus: list[tuple[str, str]]) -> list[dict]:
    counts: dict[str, Counter] = defaultdict(Counter)
    first_seen: dict[tuple[str, str], str] = {}
    mid_caps: Counter = Counter()  # 문장 중간에서 대문자로 시작한 표기
    for loc, line in corpus:
        tokens = [(m.group(0), m.start()) for m in EN_TOKEN.finditer(line)]
        for n in (1, 2, 3):
            for i in range(len(tokens) - n + 1):
                gram = tokens[i : i + n]
                surface = " ".join(token for token, _ in gram)
                words = [token.lower() for token, _ in gram]
                if any(len(w) == 1 for w in words) or (n > 1 and (words[0] in STOPWORDS or words[-1] in STOPWORDS)):
                    continue  # LaTeX 첨자 같은 한 글자 조각, 앞뒤에 불용어가 붙은 조각
                if n == 1 and words[0] in STOPWORDS:
                    continue
                key = re.sub(r"[-_\s]", "", surface.lower())
                if len(key) < 4:
                    continue
                counts[key][surface] += 1
                first_seen.setdefault((key, surface), loc)
                if surface[0].isupper() and not _is_sentence_start(line, gram[0][1]):
                    mid_caps[surface] += 1
    clusters = []
    for key, surfaces in counts.items():
        if len(surfaces) < 2:
            continue
        lowered = {s.lower() for s in surfaces}
        if len(lowered) == 1 and not _meaningful_case_variant(list(surfaces), mid_caps):
            continue
        variants = [
            {"form": s, "count": c, "first": first_seen[(key, s)]} for s, c in surfaces.most_common()
        ]
        clusters.append({"key": key, "total": sum(surfaces.values()), "variants": variants})
    return sorted(clusters, key=lambda c: -c["total"])[:LIMIT]


def _ko_spacing(corpus: list[tuple[str, str]]) -> list[dict]:
    spaced: Counter = Counter()
    first_seen: dict[str, str] = {}
    words: Counter = Counter()  # 붙여 쓴 형태를 찾기 위한 어절(조사를 뗀 형태 포함) 빈도
    for _, line in corpus:
        for run in HANGUL_RUN.findall(line):
            words[run] += 1
            if _strip_particle(run) != run:
                words[_strip_particle(run)] += 1
    for loc, line in corpus:
        runs = list(HANGUL_RUN.finditer(line))
        for left, right in zip(runs, runs[1:]):
            if line[left.end() : right.start()].strip():
                continue
            stem = _strip_particle(right.group(0))
            if len(left.group(0)) + len(stem) < 3:
                continue
            form = f"{left.group(0)} {stem}"
            spaced[form] += 1
            first_seen.setdefault(form, loc)
    clusters = []
    for form, count in spaced.items():
        joined = form.replace(" ", "")
        joined_count = words[joined]
        if joined_count:
            clusters.append(
                {
                    "spaced": form,
                    "spaced_count": count,
                    "joined": joined,
                    "joined_count": joined_count,
                    "first": first_seen[form],
                }
            )
    return sorted(clusters, key=lambda c: -(c["spaced_count"] + c["joined_count"]))[:LIMIT]


def _abbreviations(corpus: list[tuple[str, str]]) -> list[dict]:
    counts: Counter = Counter()
    first_use: dict[str, str] = {}
    defined: dict[str, str] = {}
    for loc, line in corpus:
        for match in ABBR.finditer(line):
            abbr = match.group(1)
            if len(abbr) > 10:
                continue
            counts[abbr] += 1
            first_use.setdefault(abbr, loc)
            esc = re.escape(abbr)
            if abbr not in defined and (
                re.search(rf"[^\s()][^()\n]{{1,60}}?\s*\(\s*{esc}\s*\)", line)
                or re.search(rf"{esc}\s*\(\s*[^()\n]{{3,60}}\)", line)
            ):
                defined[abbr] = loc
    return [
        {"abbr": abbr, "count": count, "first_use": first_use[abbr], "defined_at": defined.get(abbr)}
        for abbr, count in counts.most_common(LIMIT)
    ]


def scan(root: Path, includes: list[str] | None = None) -> dict:
    files = prose_files(root, includes)
    corpus = [(where(path, root, no), line) for path in files for no, line in enumerate(prose_lines(path), 1)]
    return {
        "root": str(root),
        "files": len(files),
        "english_variants": _en_clusters(corpus),
        "korean_spacing": _ko_spacing(corpus),
        "abbreviations": _abbreviations(corpus),
    }


def scan_markdown(report: dict) -> str:
    lines = [f"# 표기 변이 후보 ({report['root']}, 문서 {report['files']}개)", ""]
    lines += ["## 영문 표기 변이 (철자·대소문자·하이픈·띄어쓰기만 다른 표기)", ""]
    for cluster in report["english_variants"]:
        lines.append("- " + " / ".join(f"`{v['form']}` {v['count']}회 ({v['first']})" for v in cluster["variants"]))
    if not report["english_variants"]:
        lines.append("- 없음")
    lines += ["", "## 국문 띄어쓰기 변이", ""]
    for c in report["korean_spacing"]:
        lines.append(f"- `{c['spaced']}` {c['spaced_count']}회 ({c['first']}) / `{c['joined']}` {c['joined_count']}회")
    if not report["korean_spacing"]:
        lines.append("- 없음")
    lines += ["", "## 약어 (정의 위치가 없으면 풀어 쓴 형태를 한 번도 밝히지 않은 것)", ""]
    lines += ["| 약어 | 횟수 | 첫 사용 | 정의 위치 |", "|---|---|---|---|"]
    for a in report["abbreviations"]:
        lines.append(f"| {a['abbr']} | {a['count']} | {a['first_use']} | {a['defined_at'] or '없음'} |")
    lines += [
        "",
        "이 도구는 철자가 같은 계열의 변이만 찾는다. 뜻이 같은 다른 단어(동의어, 국문과 영문 쌍, 한 단어의 여러 뜻)는 "
        "문서를 직접 읽고 찾는다.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 용어집 편집


def load_glossary(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_glossary(path: Path, data: dict) -> None:
    data = {**data, "updated": dt.date.today().isoformat()}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def init(path: Path) -> str:
    if path.exists():
        return f"이미 있음: {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_glossary(path, {"intro": "", "groups": []})
    return f"만듦: {path}"


def make_pattern(variant: str, canonical: str = "", ignore_case: bool = False) -> str:
    """variant를 잡되 canonical(정본 표기)은 잡지 않는 정규식을 만든다."""
    body = r"\s+".join(re.escape(part) for part in variant.split())
    if re.match(r"[A-Za-z0-9]", variant):
        body = r"(?<![A-Za-z0-9-])" + body
    if re.search(r"[A-Za-z0-9]$", variant):
        body += r"(?![A-Za-z0-9-])"
    for form in [c.strip() for c in canonical.split(",") if c.strip()]:
        if variant in form and variant != form:
            before, after = form.split(variant, 1)
            body = (f"(?<!{re.escape(before)})" if before else "") + body + (f"(?!{re.escape(after)})" if after else "")
    pattern = ("(?i)" if ignore_case else "") + body
    rx = re.compile(pattern)
    if not rx.search(variant):
        raise ValueError(f"패턴 {pattern!r}이 '{variant}'를 잡지 못한다")
    for form in [c.strip() for c in canonical.split(",") if c.strip()]:
        if rx.search(form):
            raise ValueError(f"패턴 {pattern!r}이 정본 표기 '{form}'까지 잡는다. 직접 정규식을 쓴다")
    return pattern


def add_entry(path: Path, args: argparse.Namespace) -> str:
    data = load_glossary(path)
    if any(entry.get("id") == args.id for entry in entries(data)):
        raise ValueError(f"이미 있는 id: {args.id}")
    canonical = ", ".join(x for x in (args.ko, args.en) if x)
    avoid = [a.strip() for a in (args.avoid or "").split(",") if a.strip()]
    rules = [
        {
            "pattern": make_pattern(word, canonical, args.ignore_case),
            "level": args.level,
            "hint": f"'{args.ko or args.en}'(으)로 쓴다" if args.level == "V" else f"문맥을 보고 '{args.ko or args.en}'인지 확인한다",
        }
        for word in avoid
    ]
    entry = {
        "id": args.id,
        "ko": args.ko or "",
        "en": args.en or "",
        "meaning": args.meaning or "",
        "avoid": ", ".join(avoid),
        "decided": dt.date.today().isoformat(),
        "rules": rules,
    }
    groups = list(data.get("groups", []))
    for index, group in enumerate(groups):
        if group.get("title") == args.group:
            groups[index] = {**group, "entries": [*group.get("entries", []), entry]}
            break
    else:
        groups.append({"title": args.group, "note": "", "entries": [entry]})
    save_glossary(path, {**data, "groups": groups})
    return f"추가: {args.id} (규칙 {len(rules)}개)"


# ---------------------------------------------------------------- 점검


def check(glossary: Path, targets: list[Path]) -> tuple[list[str], bool]:
    rules = load_rules(glossary)
    report: list[str] = []
    has_violation = False
    for target in targets:
        root = target if target.is_dir() else target.parent
        files = prose_files(target) if target.is_dir() else [target]
        for path in files:
            for no, line in enumerate(prose_lines(path, keep_commands=True), 1):
                for hit in find_hits(line, rules):
                    has_violation = has_violation or hit.level in ("V", "E")
                    report.append(f"{where(path, root, no)} [{hit.level}] '{hit.word}' → {hit.hint}   (…{hit.context}…)")
    return report, has_violation


def validate(path: Path) -> list[str]:
    data = load_glossary(path)
    problems: list[str] = []
    seen: set[str] = set()
    for entry in entries(data):
        missing = [k for k in ("id", "ko", "en", "meaning", "avoid", "decided", "rules") if k not in entry]
        if missing:
            problems.append(f"{entry.get('id', '?')}: 빠진 필드 {missing}")
        if "id" in entry and entry["id"] in seen:
            problems.append(f"중복 id: {entry['id']}")
        seen.add(entry.get("id"))
        for rule in entry.get("rules", []):
            if rule.get("level") not in LEVELS:
                problems.append(f"{entry.get('id')}: level은 V, R, E 중 하나여야 한다")
    try:
        rules = load_rules(path)
    except ValueError as exc:
        return [*problems, str(exc)]
    for entry in entries(data):
        for form in re.split(r",\s*(?![^()]*\))", f"{entry.get('ko', '')}, {entry.get('en', '')}"):
            for rule in rules:
                if form.strip() and rule.level == "V" and rule.rx.search(form):
                    problems.append(f"정본 표기 '{form.strip()}'({entry.get('id', '?')})가 {rule.entry_id}의 위반 규칙에 걸린다")
    return problems


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render(path: Path) -> str:
    data = load_glossary(path)
    lines = [
        "# 용어집",
        "",
        f"갱신: {data.get('updated', '')}. 정본은 `{path.name}`이고 이 파일은 "
        f"term-unify 스킬의 `term_tool.py render {path.name}`로 만든다. 직접 고치지 않는다.",
        "",
    ]
    if data.get("intro"):
        lines += [data["intro"], ""]
    for group in data.get("groups", []):
        lines += [f"## {group['title']}", ""]
        if group.get("note"):
            lines += [group["note"], ""]
        lines += ["| 국문 | 영문 | 뜻과 쓰는 법 | 쓰지 않는 말 | 결정일 |", "|---|---|---|---|---|"]
        for entry in group.get("entries", []):
            cells = [entry.get(k, "") for k in ("ko", "en", "meaning", "avoid", "decided")]
            lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
        lines.append("")
    return "\n".join(lines)


def register(root: Path, glossary: Path) -> str:
    config_path = root / ".claude" / CONFIG_NAME
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    rel = glossary.resolve().relative_to(root.resolve()).as_posix()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({**config, "glossary": rel}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"등록: {config_path} → {rel}"


def lookup(path: Path, word: str) -> list[dict]:
    word = word.lower()
    return [e for e in entries(load_glossary(path)) if any(word in str(e.get(k, "")).lower() for k in ("id", "ko", "en", "avoid"))]


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("scan")
    p.add_argument("root", type=Path)
    p.add_argument("--include", nargs="*")
    p.add_argument("--json", action="store_true")
    sub.add_parser("init").add_argument("glossary", type=Path)
    p = sub.add_parser("add")
    p.add_argument("glossary", type=Path)
    p.add_argument("--group", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--ko", default="")
    p.add_argument("--en", default="")
    p.add_argument("--meaning", default="")
    p.add_argument("--avoid", default="")
    p.add_argument("--level", choices=("V", "R"), default="V")
    p.add_argument("--ignore-case", action="store_true")
    p = sub.add_parser("pattern")
    p.add_argument("variant")
    p.add_argument("--canonical", default="")
    p.add_argument("--ignore-case", action="store_true")
    p = sub.add_parser("check")
    p.add_argument("glossary", type=Path)
    p.add_argument("paths", type=Path, nargs="+")
    sub.add_parser("validate").add_argument("glossary", type=Path)
    p = sub.add_parser("render")
    p.add_argument("glossary", type=Path)
    p.add_argument("out", type=Path, nargs="?")
    p = sub.add_parser("register")
    p.add_argument("root", type=Path)
    p.add_argument("glossary", type=Path)
    p = sub.add_parser("lookup")
    p.add_argument("glossary", type=Path)
    p.add_argument("word")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            report = scan(args.root.resolve(), args.include)
            print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else scan_markdown(report))
            if report["files"] == 0:
                print(f"경고: {args.root} 아래에서 문서 파일(.md .tex .txt 등)을 찾지 못했다.", file=sys.stderr)
        elif args.command == "init":
            print(init(args.glossary))
        elif args.command == "add":
            print(add_entry(args.glossary, args))
        elif args.command == "pattern":
            print(make_pattern(args.variant, args.canonical, args.ignore_case))
        elif args.command == "check":
            report, has_violation = check(args.glossary, args.paths)
            print("\n".join(report) if report else "위반 없음")
            return 1 if has_violation else 0
        elif args.command == "validate":
            problems = validate(args.glossary)
            print("\n".join(problems) if problems else "용어집 OK")
            return 1 if problems else 0
        elif args.command == "render":
            out = args.out or args.glossary.with_suffix(".md")
            out.write_text(render(args.glossary) + "\n", encoding="utf-8")
            print(f"만듦: {out}")
        elif args.command == "register":
            print(register(args.root, args.glossary))
        elif args.command == "lookup":
            hits = lookup(args.glossary, args.word)
            for e in hits:
                print(f"{e['ko']} <-> {e['en']}   (쓰지 않는 말: {e['avoid'] or '-'})\n    {e['meaning']}")
            return 0 if hits else 1
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
