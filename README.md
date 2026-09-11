# compaction-handoff

A Claude Code plugin that carries a long session across auto-compaction. Before compaction, the
session writes a verified handoff. After compaction, the handoff's continuation prompt goes back into
the context, so the session resumes its work instead of a lossy summary.

## The problem

Compaction replaces the conversation with a summary of about 10K tokens. The summary keeps the older
work in outline and drops much of the recent detail: file paths, numbers, rulings and the next step.
The session continues from that summary, and the work goes wrong quietly.

## The method

Three hooks and one skill:

| When | Hook | What it does |
|---|---|---|
| One handoff budget before compaction | `PostToolBatch` | Writes a digest of the cycle and asks the session to run the `compaction-handoff` skill |
| Auto-compaction starts | `PreCompact` | Holds a proactive auto-compaction until the handoff exists, up to a token ceiling. Then it writes a digest of the work after the handoff |
| After compaction | `SessionStart` (`compact`) | Pastes the handoff's continuation prompt into the context |

The timing adapts. The plugin records where auto-compaction starts for each model family, and what a
handoff costs in each project. The handoff starts at the compaction point minus that cost: late enough
to cover the work, early enough to finish.

The `compaction-handoff` skill applies the probes of
[deep-handoff](https://github.com/raichominev/session-handoff-skill) to the work since the last
compaction. It adds four of its own: the user's rulings word for word, the working set, the reasoning
state, and evidence for every status claim. deep-handoff stays the tool for the end of a session. This
plugin declares it as a dependency, so it installs with it.

## What it produces

For each session, in `~/.claude/compaction/<session-id>/`:

1. `handoff.md`, or a file in the location for session documents that the project's `CLAUDE.md`
   gives. Its first section is a continuation prompt: READ IN THIS ORDER, WHERE THINGS STAND, DO NEXT,
   HARD RULES.
2. `digest-task-*.md` — a mechanical extract of the cycle: the user messages word for word, the
   session's text, the tool calls and the edited files.
3. `digest-final-*.md` — the same extract for the work between the handoff and compaction.

`~/.claude/compaction/compaction-points.json` and `handoff-costs.jsonl` hold the learned timing.

## Install

```
/plugin marketplace add raichominev/concilium
/plugin install compaction-handoff@raicho-skills
```

Python 3.8 or later must be on `PATH` as `python3` or `python`.

The summarizer also reads a `# Compact instructions` section in `CLAUDE.md`, which a plugin cannot add.
[`docs/compact-instructions.md`](docs/compact-instructions.md) holds an optional section that fits this
plugin.

## Configuration

Environment variables override the learned values:

| Variable | Effect |
|---|---|
| `COMPACTION_POINT` | Context size, in tokens, at which auto-compaction starts |
| `COMPACTION_HANDOFF_AT` | Context size at which the handoff request comes |
| `COMPACTION_HANDOFF_BUDGET` | Tokens that a handoff needs. Default: learned, 100K until three handoffs are recorded |
| `COMPACTION_BLOCK_CEILING` | Context size above which compaction no longer waits. Default: the context window minus 25K |
| `COMPACTION_TAIL_TOKENS` | Size of the fallback digest tail. Default: 150K |
| `COMPACTION_DIR` | Where the files go. Default: `~/.claude/compaction` |
| `COMPACTION_FOCUS=1` | Also asks the session for a COMPACT-FOCUS message for the summarizer. Off by default, because in testing it gave no gain over a plain compaction |

## Limits

- A hook cannot start a turn in an interactive session. After a manual `/compact`, the session waits
  for a prompt, and a one-word prompt such as "resume" is enough. Auto-compaction during a run
  continues without one.
- The plugin never holds a manual `/compact`, and never holds a compaction at the context limit.
- Hook output is capped at 10,000 characters, so the pasted continuation prompt is capped at 7,000.

## Notes

`python scripts/compaction_recovery.py simulate <transcript.jsonl> <n> <out.md>` builds the digest for
the n-th compaction of a transcript and compares it with the summary that Claude Code wrote.

MIT licence.
