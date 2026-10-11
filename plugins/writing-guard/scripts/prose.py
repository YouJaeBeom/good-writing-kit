"""문서 파일에서 사람이 읽는 산문만 뽑고, 수정 전후를 비교해 새로 쓴 분량을 잰다.

writing_guard.py(훅)와 skills/term-unify/scripts/term_tool.py가 함께 쓴다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath

PROSE_EXT = frozenset({".md", ".markdown", ".mdx", ".txt", ".rst", ".tex", ".org", ".adoc"})
# 사람이 읽는 글이 아니라 에이전트 지시문이거나 도구 설정인 파일
NON_PROSE_NAMES = frozenset(
    {"claude.md", "agents.md", "skill.md", "memory.md", "cmakelists.txt", "robots.txt", "license", "license.md", "license.txt"}
)
NON_PROSE_NAME = re.compile(r"^(requirements.*|constraints.*|.*\.lock)\.txt$", re.I)
EXCLUDED_DIRS = frozenset(
    {".claude", "node_modules", ".git", "_workspace", ".venv", "venv", "site-packages", "dist", "build", "vendor", "__pycache__", ".next"}
)
TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/")
# 플러그인 안의 이 폴더들은 에이전트 지시문(스킬, 규칙, 에이전트 정의)이라 사람이 읽는 글로 세지 않는다
PLUGIN_COMPONENT_DIRS = frozenset({"skills", "agents", "commands", "rules", "hooks"})

HANGUL = re.compile(r"[가-힣]")
WORD = re.compile(r"[A-Za-z]{2,}")
KOREAN_RATIO = 0.3
MAX_DIFF_TOKENS = 5000  # 이보다 큰 덩어리는 토큰 비교를 생략하고 새 덩어리를 모두 추가분으로 본다

# LaTeX
TEX_COMMENT = re.compile(r"(?<!\\)%.*$", re.M)
TEX_DROP_CMD = re.compile(
    r"\\(?:cite\w*|ref|autoref|cref|Cref|eqref|label|begin|end|input|include|includegraphics|url|href|bibliography\w*)"
    r"\*?(?:\[[^\]]*\])*\{[^}]*\}"
)
TEX_MATH_ENV = re.compile(
    r"\\begin\{(equation|align|gather|multline|lstlisting|verbatim|minted|tabular)\*?\}.*?\\end\{\1\*?\}", re.S
)
TEX_DISPLAY_MATH = re.compile(r"(?<!\\)\\\[.*?\\\]|\$\$.*?\$\$", re.S)  # \\[2pt] 줄바꿈은 수식 시작이 아니다
TEX_INLINE_MATH = re.compile(r"\$[^$\n]*\$|\\\(.*?\\\)")
TEX_CMD = re.compile(r"\\[A-Za-z]+\*?")
TEX_PUNCT = re.compile(r"[{}~&\\]")

# Markdown과 일반 텍스트
MD_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
MD_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.S | re.M)
MD_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
MD_INLINE_CODE = re.compile(r"`[^`\n]*`")
MD_LINK_TARGET = re.compile(r"\]\([^)\n]*\)")
MD_URL = re.compile(r"https?://\S+")
MD_TAG = re.compile(r"</?[A-Za-z][^>\n]*>")
MD_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t:|-]+\|[ \t:|-]*$", re.M)  # 줄을 넘지 않게 \s 대신 공백·탭만


@dataclass(frozen=True)
class Block:
    """새로 쓴 글 한 덩어리.

    text는 새로 들어간 말만, lines는 그 말이 들어간 줄 전체다. korean은 그 줄이 국문 위주인지 여부다.
    분량은 text로 재고, 용어 점검은 파일에서 다시 찾을 수 있도록 lines로 한다.
    """

    text: str
    korean: bool
    lines: str


def is_prose_name(path: Path) -> bool:
    """확장자와 파일 이름만 보고 문서 파일인지 판단한다."""
    if path.suffix.lower() not in PROSE_EXT:
        return False
    return not (path.name.lower() in NON_PROSE_NAMES or NON_PROSE_NAME.match(path.name))


def is_plugin_component(path: Path) -> bool:
    """가장 가까운 플러그인 루트(.claude-plugin 폴더가 있는 곳) 기준으로 skills/, rules/ 등에 있는 파일인지."""
    for parent in path.parents:
        if (parent / ".claude-plugin").is_dir():
            parts = path.relative_to(parent).parts
            return len(parts) > 1 and parts[0] in PLUGIN_COMPONENT_DIRS
    return False


def is_prose_path(path: Path) -> bool:
    """사람이 읽는 문서 파일인지 판단한다. 에이전트 설정, 의존성 폴더, 임시 폴더, 플러그인 구성 파일은 뺀다."""
    posix = path.as_posix()
    if not is_prose_name(path):
        return False
    if posix.startswith(TMP_PREFIXES):
        return False
    if any(part in EXCLUDED_DIRS for part in PurePosixPath(posix).parts):
        return False
    return not is_plugin_component(path)


def _blank_keep_lines(match: re.Match) -> str:
    return "\n" * match.group(0).count("\n")


def extract(text: str, suffix: str, keep_commands: bool = False) -> str:
    """산문만 남긴다. 지운 구간은 줄바꿈을 유지해 줄 번호가 원문과 맞게 한다.

    keep_commands=True이면 LaTeX의 주석과 수식만 지우고 명령(\\ref, \\cite 등)은 남긴다.
    용어집 규칙에는 'Figure~\\ref'처럼 명령을 포함한 것이 있어 용어 점검은 이 모드로 한다.
    """
    if suffix.lower() == ".tex":
        out = TEX_COMMENT.sub("", text)
        out = TEX_MATH_ENV.sub(_blank_keep_lines, out)
        out = TEX_DISPLAY_MATH.sub(_blank_keep_lines, out)
        out = TEX_INLINE_MATH.sub(" ", out)
        if keep_commands:
            return out
        out = TEX_DROP_CMD.sub(" ", out)
        out = TEX_CMD.sub(" ", out)
        return TEX_PUNCT.sub(" ", out)
    out = MD_FRONT_MATTER.sub(_blank_keep_lines, text)
    out = MD_FENCE.sub(_blank_keep_lines, out)
    out = MD_HTML_COMMENT.sub(_blank_keep_lines, out)
    out = MD_INLINE_CODE.sub(" ", out)
    out = MD_LINK_TARGET.sub("]", out)
    out = MD_URL.sub(" ", out)
    out = MD_TAG.sub(" ", out)
    return MD_TABLE_RULE.sub("", out)


def is_korean(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(compact) and len(HANGUL.findall(compact)) / len(compact) >= KOREAN_RATIO


def _added_tokens(old_tokens: list[str], new_tokens: list[str]) -> list[int]:
    """new_tokens 가운데 old_tokens에 없던 토큰의 위치를 돌려준다."""
    if len(old_tokens) > MAX_DIFF_TOKENS or len(new_tokens) > MAX_DIFF_TOKENS:
        return list(range(len(new_tokens)))
    added: list[int] = []
    matcher = SequenceMatcher(None, old_tokens, new_tokens, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(range(j1, j2))
    return added


def added_blocks(old: str, new: str) -> list[Block]:
    """수정 전 산문(old)과 수정 후 산문(new)을 비교해, 새로 들어간 말을 줄마다 하나의 Block으로 돌려준다.

    줄 단위로 바뀐 구간을 찾고, 바뀐 구간 안에서는 단어 단위로 비교해 고친 단어만 센다.
    국문과 영문 판정도 줄마다 따로 한다.
    """
    old_lines, new_lines = old.splitlines(), new.splitlines()
    blocks: list[Block] = []
    # autojunk=True: 빈 줄이나 \\hline처럼 자주 반복되는 줄을 색인하지 않아 큰 파일에서도 빠르다.
    # 그 때문에 덩어리가 넓게 잡혀도 아래 단어 비교가 이미 있던 단어를 걸러낸다.
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=True)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("equal", "delete"):
            continue
        lines = new_lines[j1:j2]
        positions = [(row, token) for row, line in enumerate(lines) for token in line.split()]
        if tag == "insert":
            added = range(len(positions))
        else:
            old_tokens = " ".join(old_lines[i1:i2]).split()
            added = _added_tokens(old_tokens, [token for _, token in positions])
        by_row: dict[int, list[str]] = {}
        for index in added:
            row, token = positions[index]
            by_row.setdefault(row, []).append(token)
        for row, tokens in sorted(by_row.items()):
            blocks.append(Block(text=" ".join(tokens), korean=is_korean(lines[row]), lines=lines[row]))
    return blocks


def measure(blocks: list[Block]) -> tuple[int, int]:
    """(국문 글자 수, 영문 단어 수). 국문은 공백을 뺀 글자 수, 영문은 두 글자 이상 단어 수다."""
    ko = en = 0
    for block in blocks:
        if block.korean:
            ko += len(re.sub(r"\s+", "", block.text))
        else:
            en += sum(1 for token in block.text.split() if WORD.search(token))
    return ko, en
