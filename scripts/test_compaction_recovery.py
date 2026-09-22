#!/usr/bin/env python3
"""Regression tests for the compaction hooks.

Run: python scripts/test_compaction_recovery.py

Each test starts the script the way Claude Code starts it: one process for each hook call, with the
hook payload on stdin. The transcripts are written here, so the tests need no session data.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).with_name("compaction_recovery.py")
CWD = "C:\\proj\\demo"
POINT = dict(COMPACTION_POINT=723761, COMPACTION_HANDOFF_BUDGET=100000)
HANDOFF = ("# Handoff\n\n## 1. Resume\n\nREAD IN THIS ORDER\n1. this file\n\nWHERE THINGS STAND\n"
           "state-sentinel\n\nDO NEXT\n1. do-the-thing\n\nHARD RULES\n- ask first\n\n"
           "## 2. Working set\nworking-set-sentinel\n")
# Event fields as Claude Code 2.1.275 sends them. The tool hooks of a subagent also carry agent_id and
# agent_type. The PreCompact and SessionStart of a subagent's compaction carry the same fields as those
# of the main session: the parent's session_id and transcript_path, and no agent_id.
PRE_COMPACT = {"trigger": "auto", "custom_instructions": None}
SESSION_START = {"source": "compact", "model": "claude-opus-5", "session_title": "demo"}
SUBAGENT = {"agent_id": "a9f73d99ab517e119", "agent_type": "general-purpose"}


def stamp(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def reply(ctx, offset=0):
    return {"type": "assistant", "timestamp": stamp(offset),
            "message": {"role": "assistant", "model": "claude-opus-5",
                        "content": [{"type": "text", "text": "work"}],
                        "usage": {"input_tokens": 1, "cache_read_input_tokens": ctx - 1,
                                  "cache_creation_input_tokens": 0}}}


class Hooks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "out"
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        self.count = 0

    # ---------- the transcript ----------

    def append(self, entry):
        self.count += 1
        entry.setdefault("uuid", f"u{self.count}")
        entry.setdefault("parentUuid", f"u{self.count - 1}" if self.count > 1 else None)
        entry.setdefault("isSidechain", False)
        entry.setdefault("timestamp", stamp())
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def assistant(self, ctx, offset=0):
        self.append(reply(ctx, offset))

    def prompt(self, text, kind="human", offset=5):
        self.append({"type": "user", "origin": {"kind": kind}, "timestamp": stamp(offset),
                     "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    # ---------- the hook ----------

    def hook(self, mode, event="PostToolBatch", extra=None, **env):
        payload = {"session_id": "s1", "transcript_path": str(self.transcript), "cwd": CWD,
                   "hook_event_name": event, "tool_calls": []}
        payload.update(extra or {})
        environment = dict(os.environ, COMPACTION_DIR=str(self.out), PYTHONIOENCODING="utf-8")
        environment.pop("COMPACTION_FOCUS", None)
        environment.update({k: str(v) for k, v in env.items()})
        done = subprocess.run([sys.executable, str(SCRIPT), mode], input=json.dumps(payload).encode(),
                              capture_output=True, env=environment)
        self.assertEqual(done.stderr.decode("utf-8", "replace"), "", "the hook wrote to stderr")
        return done.stdout.decode("utf-8", "replace").strip()

    def write_marker(self, ahead=2):
        """The model writes the handoff and then the marker. The marker gives the time of the handoff."""
        handoff = self.root / "handoff.md"
        handoff.write_text(HANDOFF, encoding="utf-8")
        marker = self.out / "s1" / "handoff-done.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(handoff), encoding="utf-8")
        os.utime(marker, (time.time() + ahead,) * 2)

    def state(self):
        return json.loads((self.out / "s1" / "state.json").read_text(encoding="utf-8"))

    def handoff_written(self, asked_ctx=636442, done_ctx=676985):
        """The handoff request, then the handoff, then the first call that sees the marker."""
        self.assistant(asked_ctx)
        self.assertIn("[compaction-handoff]", self.hook("watch", **POINT))
        self.write_marker()
        self.assistant(done_ctx)
        self.assertEqual(self.hook("watch", **POINT), "")


class Refresh(Hooks):
    """The handoff is refreshed when the context grows one budget past the marker."""

    def test_an_early_handoff_is_refreshed_again_and_again(self):
        # These numbers replay a real session. The hook expected compaction near 724K, because a stale
        # environment override lowered the point. The session ran to 956K before compaction.
        self.handoff_written()
        self.assertEqual(self.state()["done_ctx"], 676985)
        self.assistant(776000)
        self.assertEqual(self.hook("watch", **POINT), "")  # one budget is not yet complete
        self.assistant(777000)
        out = self.hook("watch", **POINT)
        self.assertIn("[compaction-handoff] Handoff refresh.", out)
        self.assertIn("handoff.md", out)
        self.assertIn("handoff-done.txt", out)
        self.assistant(800000)
        self.assertEqual(self.hook("watch", **POINT), "")  # the request comes once for each budget
        self.write_marker(ahead=6)                        # the model refreshed the handoff
        self.assistant(801000)
        self.assertEqual(self.hook("watch", **POINT), "")
        self.assertEqual(self.state()["done_ctx"], 801000)
        self.assistant(901000)
        self.assertIn("Handoff refresh.", self.hook("watch", **POINT))

    def test_a_handoff_at_the_right_time_gets_no_refresh(self):
        env = dict(COMPACTION_POINT=967000, COMPACTION_HANDOFF_BUDGET=100000)
        self.assistant(867500)
        self.assertIn("[compaction-handoff]", self.hook("watch", **env))
        self.write_marker()
        self.assistant(900000)
        self.assertEqual(self.hook("watch", **env), "")
        self.assistant(966000)
        self.assertEqual(self.hook("watch", **env), "")  # compaction comes before the next budget

    def test_an_ignored_request_comes_again_one_budget_later(self):
        self.handoff_written()
        self.assistant(777000)
        self.assertIn("Handoff refresh.", self.hook("watch", **POINT))
        self.assistant(876000)
        self.assertEqual(self.hook("watch", **POINT), "")
        self.assistant(877000)
        self.assertIn("Handoff refresh.", self.hook("watch", **POINT))

    def test_compaction_ends_the_cycle(self):
        self.handoff_written()
        self.assistant(120000)  # compaction ran: the context is small again
        self.assertEqual(self.hook("watch", **POINT), "")
        self.assertFalse((self.out / "s1" / "state.json").exists())
        self.assistant(640000)
        out = self.hook("watch", **POINT)
        self.assertIn("[compaction-handoff]", out)
        self.assertNotIn("refresh", out.lower())  # the new cycle asks for a handoff, not for a refresh

    def test_compaction_does_not_wait_for_a_refresh(self):
        self.handoff_written()
        self.assistant(777000)
        self.assertIn("Handoff refresh.", self.hook("watch", **POINT))
        self.assertNotIn("block", self.hook("precompact", "PreCompact", {"trigger": "auto"}, **POINT))


class LateMessages(Hooks):
    """The recovery message warns when the user wrote messages after the handoff."""

    def prepare(self, prompts=(), notifications=0):
        self.handoff_written()
        for text in prompts:
            self.prompt(text)
        for n in range(notifications):
            self.prompt(f"<task-notification>{n}</task-notification>", kind="task-notification")
        self.assistant(700000, offset=9)
        self.hook("precompact", "PreCompact", {"trigger": "auto"}, **POINT)
        return self.hook("resume", "SessionStart", {"source": "compact"}, **POINT)

    def test_the_warning_names_the_messages_and_the_digest(self):
        out = self.prepare(prompts=("forget that, do the new thing now",), notifications=2)
        self.assertIn("Warning: the user wrote 1 message(s) after the handoff", out)  # notifications do not count
        self.assertIn("digest-final-", out)
        self.assertIn("the messages and the final digest win", out)
        self.assertIn("trust the final digest and the files", out)
        self.assertNotIn("trust the prompt and the files", out)
        self.assertIn("state-sentinel", out)  # the continuation prompt is still pasted
        self.assertLess(out.index("Warning:"), out.index("<<<"))  # the warning comes before the prompt
        self.assertLess(len(out), 10000)  # the limit for hook output

    def test_no_warning_when_no_message_came(self):
        out = self.prepare()
        self.assertNotIn("Warning:", out)
        self.assertIn("If the summary and the continuation prompt disagree, trust the prompt and the files.", out)
        self.assertIn("state-sentinel", out)
        self.assertNotIn("working-set-sentinel", out)  # only the Resume section is pasted


class Baseline(Hooks):
    """The behaviour that the two new steps must not break."""

    def test_the_handoff_is_asked_once(self):
        self.assistant(636442)
        self.assertIn("[compaction-handoff]", self.hook("watch", **POINT))
        self.assistant(650000)
        self.assertEqual(self.hook("watch", **POINT), "")

    def test_auto_compaction_waits_for_the_handoff(self):
        self.assistant(636442)
        self.hook("watch", **POINT)
        self.assistant(700000)
        self.assertIn('"block"', self.hook("precompact", "PreCompact", {"trigger": "auto"}, **POINT))
        self.assertEqual(self.hook("precompact", "PreCompact", {"trigger": "manual"}, **POINT), "")
        self.write_marker()
        self.assertNotIn("block", self.hook("precompact", "PreCompact", {"trigger": "auto"}, **POINT))

    def test_a_handoff_of_an_earlier_cycle_is_not_pasted(self):
        self.write_marker()
        self.assistant(300000, offset=9)
        out = self.hook("resume", "SessionStart", {"source": "compact"}, **POINT)
        self.assertIn("earlier cycle", out)
        self.assertNotIn("state-sentinel", out)

    def test_a_subagent_call_is_ignored(self):
        self.assistant(636442)
        self.assertEqual(self.hook("watch", extra={"agent_id": "a1"}, **POINT), "")


class CycleDigest(Hooks):
    """The cycle digest holds the work after the newest compaction."""

    def boundary(self, offset):
        self.append({"type": "system", "subtype": "compact_boundary", "parentUuid": None, "timestamp": stamp(offset)})

    def work(self, ruling, offset):
        self.prompt(ruling, offset=offset)
        for n in range(10):
            self.assistant(100000 + n, offset=offset + n + 1)

    def digest(self):
        out = self.hook("watch", **POINT)
        self.assertIn("[compaction-handoff]", out)
        return next((self.out / "s1").glob("digest-task-*")).read_text(encoding="utf-8")

    def test_a_queued_message_after_a_manual_compact_does_not_move_the_cycle(self):
        # Claude Code 2.1.271 to 2.1.275: a queued message that arrives after a manual /compact gets a parent
        # from before that compaction, and the older chain is written again at the end of the transcript.
        self.boundary(-900)
        self.work("old-ruling", -890)
        older = self.transcript.read_text(encoding="utf-8").splitlines()
        self.boundary(-700)
        self.append({"type": "user", "isCompactSummary": True, "timestamp": stamp(-699),
                     "message": {"role": "user", "content": "summary"}})
        self.work("current-ruling", -690)
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write("\n".join(older) + "\n")
        self.append({"type": "user", "origin": {"kind": "task-notification"}, "timestamp": stamp(-100),
                     "parentUuid": json.loads(older[-1])["uuid"],
                     "message": {"role": "user", "content": [{"type": "text", "text": "agent done"}]}})
        self.assistant(636442, offset=-90)
        digest = self.digest()
        self.assertIn("current-ruling", digest)
        self.assertNotIn("old-ruling", digest)

    def test_an_abandoned_branch_stays_out_of_the_cycle_digest(self):
        self.boundary(-900)
        self.work("kept-ruling", -890)
        fork = f"u{self.count}"
        self.prompt("abandoned-ruling", offset=-800)
        self.assistant(200000, offset=-799)
        self.append(dict(reply(636442, -700), parentUuid=fork))  # the user went back to an earlier message
        digest = self.digest()
        self.assertIn("kept-ruling", digest)
        self.assertNotIn("abandoned-ruling", digest)


class Subagents(Hooks):
    """A subagent that compacts gets nothing from the hooks, and the parent's files stay as they were."""

    def subagent(self, entry, name="agent-a9f73d99ab517e119"):
        """Claude Code keeps a subagent transcript in <session>/subagents/."""
        path = self.transcript.with_suffix("") / "subagents" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(entry, isSidechain=True)) + "\n")
        return path

    def left(self, pattern):
        return list((self.out / "s1").glob(pattern))

    def test_a_subagent_compaction_gets_no_continuation_prompt(self):
        # 2026-09-22: a background subagent ran to the compaction point, and the recovery hook pasted
        # the parent's DO NEXT list into it.
        self.handoff_written()
        state = self.state()
        self.subagent(reply(956895))
        self.assertEqual(self.hook("precompact", "PreCompact", PRE_COMPACT, **POINT), "")
        self.assertEqual(self.hook("resume", "SessionStart", SESSION_START, **POINT), "")
        self.assertEqual(self.state(), state)  # the parent's cycle goes on
        self.assertEqual(self.left("digest-final-*"), [])
        self.assertEqual(self.left("subagent-compaction-*"), [])  # the SessionStart used the record
        self.assertFalse((self.out / "compaction-points.json").exists())  # no false compaction point
        # The subagent works on with a small context. The parent's own compaction works as before.
        self.subagent({"type": "system", "subtype": "compact_boundary", "timestamp": stamp()})
        self.subagent(reply(72670))
        self.assistant(700000, offset=9)
        self.hook("precompact", "PreCompact", PRE_COMPACT, **POINT)
        self.assertIn("state-sentinel", self.hook("resume", "SessionStart", SESSION_START, **POINT))

    def test_a_subagent_compaction_is_not_held_for_the_parents_handoff(self):
        self.assistant(636442)
        self.assertIn("[compaction-handoff]", self.hook("watch", **POINT))  # the parent owes a handoff
        self.subagent(reply(956895))
        self.assertEqual(self.hook("precompact", "PreCompact", PRE_COMPACT, **POINT), "")
        self.assertNotIn("first_precompact_ctx", self.state())

    def test_a_main_compaction_beside_a_live_subagent_works_as_before(self):
        self.handoff_written()
        self.subagent(reply(400000))
        self.assistant(700000, offset=9)
        self.assertEqual(self.hook("precompact", "PreCompact", PRE_COMPACT, **POINT), "")
        self.assertEqual(len(self.left("digest-final-*")), 1)
        self.assertIn("state-sentinel", self.hook("resume", "SessionStart", SESSION_START, **POINT))

    def test_a_manual_compaction_is_the_main_sessions(self):
        self.handoff_written()
        self.subagent(reply(900000))  # fuller than the main session, but only the main session takes /compact
        self.assistant(700000, offset=9)
        self.hook("precompact", "PreCompact", dict(PRE_COMPACT, trigger="manual"), **POINT)
        self.assertEqual(len(self.left("digest-final-*")), 1)
        self.assertIn("state-sentinel", self.hook("resume", "SessionStart", SESSION_START, **POINT))

    def test_a_record_counts_only_while_the_subagent_compacts(self):
        self.handoff_written()
        self.subagent(reply(956895))
        self.hook("precompact", "PreCompact", PRE_COMPACT, **POINT)
        self.assertEqual(len(self.left("subagent-compaction-*")), 1)
        self.subagent(reply(958000))  # no compaction followed: the subagent works on
        self.hook("precompact", "PreCompact", dict(PRE_COMPACT, trigger="manual"), **POINT)
        self.assertIn("state-sentinel", self.hook("resume", "SessionStart", SESSION_START, **POINT))
        self.assertEqual(self.left("subagent-compaction-*"), [])

    def test_a_payload_that_names_a_subagent_is_ignored(self):
        self.handoff_written()
        state = self.state()
        self.assertEqual(self.hook("watch", extra=SUBAGENT, **POINT), "")
        self.assertEqual(self.hook("precompact", "PreCompact", dict(PRE_COMPACT, **SUBAGENT), **POINT), "")
        self.assertEqual(self.hook("resume", "SessionStart", dict(SESSION_START, **SUBAGENT), **POINT), "")
        inside = {"transcript_path": str(self.subagent(reply(956895)))}
        self.assertEqual(self.hook("precompact", "PreCompact", dict(PRE_COMPACT, **inside), **POINT), "")
        self.assertEqual(self.hook("resume", "SessionStart", dict(SESSION_START, **inside), **POINT), "")
        self.assertEqual(self.state(), state)
        self.assertEqual(self.left("digest-final-*"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
