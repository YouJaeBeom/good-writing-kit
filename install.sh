#!/usr/bin/env bash
# good-writing-kit 설치 스크립트
#   글쓰기 점검 훅과 용어 통일·설명 기준 스킬(writing-guard),
#   국문 윤문(im-not-ai humanize-korean), 영문 윤문(humanizer)을 Claude Code 플러그인으로 설치한다.
#
# 사용:
#   curl -fsSL https://raw.githubusercontent.com/YouJaeBeom/good-writing-kit/main/install.sh | bash
#
# 다시 실행하면 업데이트한다. 예전에 파일을 직접 복사해 설치한 흔적(~/.claude/scripts/writing-guard 등)이
# 있으면 백업 폴더로 옮기고 settings.json에서 그 훅을 뺀다. 지우지 않고 옮기므로 되돌릴 수 있다.
set -euo pipefail

REPO="${GOOD_WRITING_KIT_REPO:-YouJaeBeom/good-writing-kit}"
MARKET="good-writing-kit"
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
BACKUP="$CLAUDE_HOME/backups/good-writing-kit-$(date +%Y%m%d-%H%M%S)"

say() { printf '[good-writing-kit] %s\n' "$*"; }
die() { printf '[good-writing-kit] 오류: %s\n' "$*" >&2; exit 1; }

command -v claude >/dev/null 2>&1 || die "Claude Code(claude 명령)가 필요합니다. 먼저 설치하고 로그인하세요."
command -v git >/dev/null 2>&1 || die "git이 필요합니다."
command -v python3 >/dev/null 2>&1 || die "python3(3.8 이상)가 필요합니다."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || die "python3 3.8 이상이 필요합니다."

# 1. 마켓플레이스 등록. 이미 등록돼 있으면 최신으로 갱신한다
if claude plugin marketplace list 2>/dev/null | grep -q "$MARKET"; then
  say "마켓플레이스 갱신: $MARKET"
  claude plugin marketplace update "$MARKET"
else
  say "마켓플레이스 등록: $REPO"
  claude plugin marketplace add "$REPO"
fi

# 2. 예전 수동 설치본 정리 (없으면 아무것도 하지 않는다). 플러그인 설치보다 먼저 해야
#    수동 설치본을 '이미 설치됨'으로 잘못 보고 건너뛰는 일이 없다
move_old() {
  local target="$1"
  [ -e "$target" ] || [ -L "$target" ] || return 0
  mkdir -p "$BACKUP"
  mv "$target" "$BACKUP/"
  say "예전 설치본을 백업으로 옮김: $target"
}
move_old "$CLAUDE_HOME/scripts/writing-guard"
move_old "$CLAUDE_HOME/skills/term-unify"
move_old "$CLAUDE_HOME/skills/clear-explain"
for name in humanize humanize-korean humanize-scan humanize-redo; do move_old "$CLAUDE_HOME/skills/$name"; done
for name in humanize-monolith humanize-diagnostician humanize-finalizer korean-ai-tell-taxonomist; do
  move_old "$CLAUDE_HOME/agents/$name.md"
done
if [ -d "$CLAUDE_HOME/skills/humanizer/.git" ] &&
  git -C "$CLAUDE_HOME/skills/humanizer" remote get-url origin 2>/dev/null | grep -q "blader/humanizer"; then
  move_old "$CLAUDE_HOME/skills/humanizer"
fi

SETTINGS="$CLAUDE_HOME/settings.json" BACKUP="$BACKUP" python3 - <<'PY'
import json, os, shutil
from pathlib import Path

settings = Path(os.environ["SETTINGS"])
if not settings.exists():
    raise SystemExit(0)
data = json.loads(settings.read_text(encoding="utf-8"))
hooks = data.get("hooks", {})
old = "scripts/writing-guard/writing_guard.py"  # 예전 수동 설치 훅의 경로
changed = False
for event in list(hooks):
    kept = [e for e in hooks[event] if not any(old in h.get("command", "") for h in e.get("hooks", []))]
    if len(kept) != len(hooks[event]):
        changed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
if changed:
    backup = Path(os.environ["BACKUP"])
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(settings, backup / "settings.json")
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("[good-writing-kit] settings.json에서 예전 writing-guard 훅을 뺐습니다 (원본은 백업 폴더).")
PY

# 3. 플러그인 설치. 같은 이름의 플러그인이 다른 마켓플레이스에서 이미 설치돼 있으면 건너뛴다
# ~/.claude/skills에 직접 넣은 스킬은 목록에 "이름@skills-dir"로 보이지만 플러그인이 아니므로 뺀다
installed_from() { claude plugin list 2>/dev/null | grep -oE "$1@[A-Za-z0-9._-]+" | grep -v "@skills-dir$" | head -n 1 || true; }
for plugin in writing-guard humanize-korean humanizer; do
  current="$(installed_from "$plugin")"
  if [ "$current" = "$plugin@$MARKET" ]; then
    say "업데이트: $current"
    claude plugin update "$current"
  elif [ -n "$current" ]; then
    say "이미 설치됨: $current (그대로 둡니다)"
  else
    say "설치: $plugin@$MARKET"
    claude plugin install "$plugin@$MARKET"
  fi
done

# 4. 약어 점검에서 뺄 이름 목록. 프로젝트·학회 이름을 공백으로 나열한다 (서버마다 따로 관리)
KNOWN="$CLAUDE_HOME/writing-guard-known-terms.txt"
if [ ! -e "$KNOWN" ]; then
  printf '# 약어 점검에서 빼는 이름. 공백이나 줄바꿈으로 구분한다. # 뒤는 주석.\n' > "$KNOWN"
  say "만듦: $KNOWN"
fi

if [ -f "$CLAUDE_HOME/CLAUDE.md" ] && grep -q "^# 글쓰기: AI 말투 제거" "$CLAUDE_HOME/CLAUDE.md"; then
  say "참고: ~/.claude/CLAUDE.md에 같은 글쓰기 규칙이 있어 플러그인의 규칙 주입은 건너뜁니다."
  say "      플러그인 규칙(업데이트가 반영되는 쪽)을 쓰려면 CLAUDE.md에서 그 절을 지우세요."
fi
[ -d "$BACKUP" ] && say "백업 폴더: $BACKUP"
say "완료. 새 Claude Code 세션부터 적용됩니다."
