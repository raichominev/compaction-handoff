---
name: compaction-handoff
description: Write a durable handoff before Claude Code compacts the context, and restore the working state after compaction. Use it when a [compaction-handoff] or [compaction-recovery] hook message asks for it, or when the user asks to prepare for compaction. Write a COMPACT-FOCUS message only when a [compaction-focus] hook message asks for it.
---

# Compaction handoff

Compaction replaces the conversation with a summary of about 10K tokens. The summary keeps the older
work in outline and loses much of the detail of the recent work. After compaction, the handoff and the
project documents give the session most of what it needs. They do not give the current run: the work
between the handoff and compaction.

This skill writes the handoff before compaction. Hooks start the skill, hold compaction until the skill
is complete, save the current run as a digest, and paste the handoff's continuation prompt into the
context after compaction. A hook also asks for a refresh when the handoff becomes older than the work.

The skill uses the probes of the `deep-handoff` skill. It applies them only to the work since the
previous compaction (the "cycle"). `deep-handoff` stays the procedure for the end of a session.

## The hook messages

| Message | When it comes | What to do |
|---|---|---|
| `[compaction-handoff]` | At the compaction point minus the handoff budget of the project | Do the procedure |
| `[compaction-handoff] Handoff refresh` | When the context grows one handoff budget past the last handoff, and compaction did not run | Do the refresh procedure |
| `[compaction-recovery]` | Directly after compaction | Do the recovery steps |
| `[compaction-focus]` | Only when the environment variable `COMPACTION_FOCUS=1` is set. It is off by default. | Write the COMPACT-FOCUS message |

The hooks learn two numbers. The compaction point is where auto-compaction started in recent cycles.
The handoff budget comes from the token cost of the recent handoffs in the same project. Until three
handoffs are recorded, the budget is 100K tokens.

## Procedure

1. Stop the task at a safe point.
2. Make sure that no operation is half done. Examples: a database write, a migration on one database
   only, a commit through a temporary index. Finish the operation, or record its exact state and how to
   reverse it.
3. Read the cycle digest that the hook message names. It lists the user messages of the cycle word for
   word, your text, the tool calls and the edited files.
4. Read the handoff of the previous cycle of this session, if one exists.
5. Find each fact in the cycle that no durable file holds. Examples: a number, a decision, a user
   ruling, a file path, a refuted approach. This is probe f of `deep-handoff` (orphan hunt). It is the
   most important step, because compaction deletes these facts.
6. Copy each instruction and ruling of the user in this cycle into the handoff, word for word.
7. Record the working set: file paths with line numbers, commands, IDs, the branch and the worktree,
   uncommitted changes, background tasks and servers that run.
8. Record the reasoning state: the current hypotheses, the evidence for each, the next test that
   decides between them, and your confidence.
9. Recompute each number that the handoff gives (probe e). Write the command or the query that
   produces the number next to it.
10. Check each status claim that the handoff makes. Examples: "finished", "complete", "passing",
    "running", "merged", "pushed". Get the evidence now: a command output, or a file that you read
    now. Write the evidence next to the claim. If you cannot check a claim, write "unverified" next to
    it. Do not copy a status from an earlier message or an earlier handoff without a check.
11. Run the code that changed in this cycle (probe b). Smoke-test each script that you created or
    changed in this cycle. Record which pass and which fail.
12. Do the claim check (probe a) and the contradiction hunt (probe d) on the documents that changed in
    this cycle.
13. Do a data audit (probe c) only if this cycle wrote data.
14. Put each settled fact in its living document (the one-home rule). Keep each provisional fact in the
    handoff and mark it "provisional".
15. Write or update the handoff. Use the sections below.
16. Write the path of the handoff into the marker file that the hook message names. Compaction waits
    for this file.
17. Continue the task.

Do not commit or push. List the changed files in the handoff.

## Refresh procedure

A refresh request comes when the context grows one handoff budget past the last handoff, and
compaction did not run. The handoff is then older than the work. A wrong estimate of the compaction
point is the usual cause. The refresh is not a second full procedure. Do these steps only.

1. Stop the task at a safe point. Make sure that no operation is half done (step 2 above).
2. Rewrite the Resume section. Keep its four blocks: READ IN THIS ORDER, WHERE THINGS STAND, DO NEXT
   and HARD RULES.
3. Give the evidence for each status claim in that section (step 10 above).
4. Add each new instruction and ruling of the user word for word, in the User instructions section.
5. Update the State, Open items and Next steps sections.
6. Do not run the probes again. The work since the last handoff is still in your context.
7. Write the path of the handoff into the marker file again. The hook reads the time of that file.
8. Continue the task.

## Where the handoff goes

Use the location for session documents that the project CLAUDE.md gives. If it gives no location, use
the path in the hook message. Keep one handoff file for each session and topic, and update the same
file at each compaction. A new file for each compaction makes the next session read several stale
copies.

## Handoff sections

1. **Resume: the continuation prompt.** After compaction, the recovery hook pastes this section into the
   context. It must work alone, for a session that remembers nothing. Use the heading `## 1. Resume`
   and keep the section under 6,000 characters. Write these four blocks in this order:
   - **READ IN THIS ORDER**: a numbered list. Item 1 is this handoff file. Then give each project
     document that the next step needs, with the reason. The last item is the final digest that the
     recovery hook names.
   - **WHERE THINGS STAND**: the state in a few lines. Give the evidence for each status claim
     (step 10).
   - **DO NEXT**: numbered, concrete actions. Give file paths with line numbers, commands, and the
     exact failure or ruling that each action answers.
   - **HARD RULES**: the constraints that a new session would otherwise learn again at a high cost.
2. **Working set** (step 7).
3. **User instructions and rulings**, word for word (step 6).
4. **Reasoning state** (step 8).
5. **State by topic**: done, in progress, not started.
6. **Decisions**, each with its reason.
7. **Refuted approaches**: do not try them again.
8. **Open items**: each with its evidence, where to find the evidence, and who decides.
9. **Next steps**: the full ranked queue. DO NEXT in the Resume section holds its first items.
10. **Cautions**.
11. **Verification log**: each probe that ran and what it found. At the next compaction handoff, use a
    different probe. After compaction, do not run a probe as part of the task unless DO NEXT asks
    for one.

## COMPACT-FOCUS message

This step is off by default. Do it only when a `[compaction-focus]` hook message asks for it. In
testing it gave no gain over a plain compaction.

When the user's CLAUDE.md holds the `# Compact instructions` section from this plugin's
`docs/compact-instructions.md`, the summarizer obeys the most recent COMPACT-FOCUS message. Write the
message as one chat message that starts with `COMPACT-FOCUS`.

1. Name the current work in one sentence.
2. List the items that must stay word for word: the latest user instructions, file paths with line
   numbers, commands, IDs, numbers, error text and the next step.
3. List the finished work to compress to one line each. Include the handoff procedure, because the
   handoff file holds it.
4. Give the paths of the handoff and of the digests.
5. Use 400 words or fewer. Give priorities. Do not copy the content of the handoff.

## Recovery after compaction

1. The recovery hook pastes the continuation prompt (the Resume section of the handoff) into the
   context. Do its READ IN THIS ORDER list completely, including every project document that it names.
2. Read the final digest that the hook message names. It holds the work after the handoff, word for
   word.
3. If the recovery message warns that the user wrote messages after the handoff, those messages and
   the final digest win. Do the newest open request of the user. Use the DO NEXT list only where the
   final digest does not replace it.
4. If there is no such warning, do the DO NEXT list. If the summary and the continuation prompt
   disagree, trust the prompt and the files.
5. For a detail that these files do not hold, read the cycle digest or search the transcript.
6. Do not repeat finished steps. Do not start a verification probe unless DO NEXT asks for one.

After a manual `/compact`, the session waits for a prompt. A one-word prompt such as "resume" is
enough.

## Proportion

Time is acceptable, because compaction waits for the handoff. Tokens have a limit. Compaction runs
without the handoff when the context reaches the ceiling in the hook message, about 25K tokens below
the hard limit. When the context is less than 50K tokens below that ceiling, stop the probes. Write the
handoff file and the marker file first.
