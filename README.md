# good-writing-kit

Claude Code가 쓰는 문서와 채팅 답변에서 AI 말투를 줄이고, 프로젝트 용어를 통일하고, 약어를 풀어 쓰게 하는 플러그인 묶음입니다. 아래 명령 한 줄로 설치합니다.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/YouJaeBeom/good-writing-kit/main/install.sh | bash
```

You can also install from inside Claude Code:

```
/plugin marketplace add YouJaeBeom/good-writing-kit
/plugin install writing-guard@good-writing-kit
/plugin install humanize-korean@good-writing-kit
/plugin install humanizer@good-writing-kit
```

Requirements: Claude Code (installed and logged in), git, and Python 3.8 or later. The plugins load in the next Claude Code session.

## What gets installed

| Plugin | What it does | Source |
|---|---|---|
| `writing-guard` | Hooks that check documents and chat replies, plus the `term-unify` and `clear-explain` skills | this repository |
| `humanize-korean` | Rewrites Korean text that reads as machine-written (im-not-ai) | [epoko77-ai/im-not-ai](https://github.com/epoko77-ai/im-not-ai) |
| `humanizer` | Rewrites English text that reads as machine-written | [blader/humanizer](https://github.com/blader/humanizer) |

The marketplace points to the original repositories for `humanize-korean` and `humanizer`, so their updates come from their authors. If one of them is already installed from another marketplace, `install.sh` leaves it alone.

## How writing-guard works

| When | What the hook does |
|---|---|
| A session starts | Adds the writing rules in `plugins/writing-guard/rules/writing-rules.md` to the session. Skips this if `~/.claude/CLAUDE.md` already contains them. |
| You send a message | Reminds Claude of the explanation rules on the first turn, on every 10th turn, and after a reply that had problems. Reports what the previous reply got wrong. A message such as "그대로 써줘" or "원문 유지" turns off the document check for that turn. |
| Claude edits a file | Counts new prose written to `.md`, `.tex`, `.txt`, `.rst`, `.org` and `.adoc` files. When one reply adds more than 300 Korean characters or 60 English words, Claude has to run `humanize-scan` (Korean) or `humanizer` (English) before it finishes. If the project has a glossary, changed lines are checked against it. |
| A skill runs | Marks the Korean or English text as checked. |
| Claude finishes a reply | Scans the chat reply with regular expressions for unexplained abbreviations, references such as "위 2번" that need earlier context, and Korean signals of machine-written text. The scan runs locally and uses no tokens. If a document was not checked or a glossary violation is still in the file, the hook stops Claude from finishing, at most twice. |

Files the hook does not count: anything under `.claude/`, `node_modules/` or temporary folders, `CLAUDE.md`, `SKILL.md`, `AGENTS.md`, and the `skills`, `rules`, `agents`, `commands` and `hooks` folders of a plugin.

## Skills

- `term-unify`: say "용어 통일 스킬 적용해줘" while working in a project. Claude collects spelling variants, synonyms and undefined abbreviations from the documents, asks you which form to keep, and writes `glossary.json` with a readable `glossary.md`. It fixes the text only after you confirm the number of changes, then registers the glossary so the hook checks new writing against it.
- `clear-explain`: rules for explanations. Expand abbreviations you have not used yourself, use the project's own terms, give each thing one name, and write each reply so it makes sense without the earlier conversation.

## Settings

| File | Effect |
|---|---|
| `~/.claude/writing-guard.off` | Turns off every writing-guard hook |
| `~/.claude/writing-guard-chat.off` | Turns off the chat reply check |
| `~/.claude/writing-guard-reminder.off` | Turns off the explanation reminders |
| `~/.claude/writing-guard-known-terms.txt` | Names that are not treated as abbreviations, such as project or conference names, separated by spaces |
| `<project>/.claude/writing-guard.json` | Per-project options: `{"humanize": false}`, `{"exclude": ["drafts/**"]}`, `{"glossary": "docs/glossary.json"}` |

## Update

Run the install command again, or:

```
claude plugin marketplace update good-writing-kit
claude plugin update writing-guard@good-writing-kit
```

## Uninstall

Use the names shown by `claude plugin list`:

```
claude plugin uninstall writing-guard@good-writing-kit
claude plugin uninstall humanize-korean@good-writing-kit
claude plugin uninstall humanizer@good-writing-kit
claude plugin marketplace remove good-writing-kit
```

## Moving from a manual install

If these files were copied into `~/.claude` by hand, `install.sh` moves the old copies to `~/.claude/backups/good-writing-kit-<time>/` and removes the old hook entries from `settings.json`. Other hooks stay as they are. If `~/.claude/CLAUDE.md` still holds the writing rules, the plugin does not add them a second time; delete that section from `CLAUDE.md` to use the plugin's copy, which is updated along with the plugin.

## Tests

```
python3 -m pytest -q plugins/writing-guard
```

## License

`writing-guard` is released under the MIT License. `humanize-korean` (im-not-ai, by epoko77-ai) and `humanizer` (by blader) are separate projects under their own MIT licenses. This repository only links to them.
