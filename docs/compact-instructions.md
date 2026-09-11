# Compact instructions (optional)

The compaction summarizer reads a `# Compact instructions` section in `CLAUDE.md`. A plugin cannot add
one. The section below fits this plugin. In `~/.claude/CLAUDE.md` it applies to every project; in a
project's `CLAUDE.md` it applies to that project only.

```markdown
# Compact instructions

1. Look for chat messages that start with COMPACT-FOCUS. If you find one, obey the most recent one. Where it conflicts with items 2 to 5, it has priority.
2. Give most of the summary to the most recent task work. Compress the compaction-handoff procedure to one line, because the handoff file holds it.
3. Keep these items word for word: the latest user instructions and rulings, file paths with line numbers, commands, IDs, numbers, error text and the next step.
4. Compress finished work to one line for each item.
5. Give the paths of the handoff file and of the digest files, if the conversation names them.
```
