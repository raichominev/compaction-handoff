#!/usr/bin/env python3
"""Compaction recovery for Claude Code sessions (user-level hooks).

watch       PostToolBatch. At (compaction point - handoff budget): write the cycle digest and ask
            for the compaction handoff. After the handoff, ask for a refresh each time the context
            grows one handoff budget past the last write of the marker. With COMPACTION_FOCUS=1,
            also ask for a COMPACT-FOCUS message close to the compaction point.
precompact  PreCompact. Record where auto-compaction fires. Hold a proactive auto-compaction while
            the handoff (or, with COMPACTION_FOCUS=1, the focus message) is missing, up to a
            ceiling below the hard limit. Otherwise write the digest of the work since the handoff,
            and count the user messages in it.
resume      SessionStart (matcher "compact"). Point the model at the handoff and the digests. Give a
            warning when the user wrote messages after the handoff.
simulate    Offline test: digest for the Nth compaction of a transcript vs. its summary.

In a subagent, every hook does nothing. Claude Code gives the tool hooks of a subagent an agent_id, but
it sends the PreCompact and SessionStart of a subagent's compaction with the parent's session_id and
transcript_path and without agent_id (seen in 2.1.275). precompact therefore also compares the
transcripts: an auto-compaction starts when a context reaches the compaction point, so a subagent
transcript written in the last minute that is fuller than the parent's marks the compaction as the
subagent's. precompact records it, and resume skips the SessionStart that ends it.

Timing adapts. The compaction point is the lowest recent first-PreCompact context of the model
family (it resets when the configuration changes), else the configured window. The handoff budget
is 1.2 x the 80th percentile of the project's recent handoff costs, else 100K.

Every context number here is the sum of the usage fields of the last assistant message. It runs
some thousand tokens below the preTokens value that Claude Code records for the same point (3K at
737K, 22K at 978K in measured cases). Compare only numbers of this scale with each other.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

OUT_ROOT = Path(os.environ.get("COMPACTION_DIR") or Path.home() / ".claude" / "compaction")
SETTINGS = Path.home() / ".claude" / "settings.json"
TAIL_TOKENS = 150_000
TASK_BUDGET_CHARS = 150_000
FINAL_BUDGET_CHARS = 90_000
MAX_TOOL_LINES = 400
USER_CAP = 4000
LATEST_TEXT_CAP = 4000
RESULT_CAP = 300
DEFAULT_BUDGET = 100_000
BUDGET_RANGE = (40_000, 200_000)
FOCUS_LEAD = 15_000
FOCUS_GRACE = 50_000
HARD_MARGIN = 25_000
LIVE_SECONDS = 60
RECORD_SECONDS = 900
KEY_ARGS = ("file_path", "notebook_path", "path", "command", "pattern", "url", "query",
            "skill", "description", "prompt")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
FOCUS_MARK = "COMPACT-FOCUS"
# Off unless COMPACTION_FOCUS=1: in testing it gave no gain over a plain compaction.
FOCUS_ENABLED = os.environ.get("COMPACTION_FOCUS") == "1"
SKILL = "compaction-handoff"


# ---------- files ----------

def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_jsonl(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def utc_of_mtime(path):
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------- transcript ----------

def load(path):
    entries = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                e["_line"] = n
                entries.append(e)
    return entries


def tail_lines(path, chunk):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - chunk))
        return fh.read().decode("utf-8", "replace").splitlines()


def is_boundary(e):
    return e.get("type") == "system" and e.get("subtype") == "compact_boundary"


def cycle_id(path):
    last = "start"
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"compact_boundary"' in line:
                try:
                    last = json.loads(line).get("uuid") or last
                except ValueError:
                    pass
    return last


def active_chain(entries, end_uuid=None):
    by_uuid = {e["uuid"]: e for e in entries if e.get("uuid")}
    if end_uuid is None:
        main = [e for e in entries if e.get("uuid") and not e.get("isSidechain")]
        if not main:
            return []
        end_uuid = main[-1]["uuid"]
    chain, cur = [], by_uuid.get(end_uuid)
    while cur is not None and not is_boundary(cur):
        chain.append(cur)
        cur = by_uuid.get(cur.get("parentUuid"))
    chain.reverse()
    return chain


def first_copies(entries):
    """Each entry at its first place in the file. After a compaction of the main thread, a subagent's
    compaction in the same process makes Claude Code (2.1.271 to 2.1.275) write the older history again at
    the end of the file, with the same uuids and its older compact_boundary, and parent the next message
    on that copy (anthropics/claude-code#92089)."""
    seen, out = set(), []
    for e in entries:
        u = e.get("uuid")
        if not (u and u in seen):
            seen.add(u)
            out.append(e)
    return out


def live_context(entries, end_uuid=None):
    """The chain of the last entry when it starts at the newest compaction, else the main-session
    entries after the newest compaction."""
    chain = active_chain(entries, end_uuid)
    main = [e for e in first_copies(entries) if not e.get("isSidechain")]
    last_b = max((i for i, e in enumerate(main) if is_boundary(e)), default=-1)
    if len(chain) >= 10 and (end_uuid or last_b < 0 or chain[0].get("parentUuid") == main[last_b].get("uuid")):
        return chain
    return main[last_b + 1:]


def message(e):
    return e.get("message") if isinstance(e.get("message"), dict) else {}


def blocks(e):
    content = message(e).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def ctx_tokens(e):
    usage = message(e).get("usage")
    if e.get("type") != "assistant" or not isinstance(usage, dict):
        return None
    return sum(usage.get(k) or 0 for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def last_usage(path):
    for chunk in (1_000_000, 8_000_000, 64_000_000):
        for line in reversed(tail_lines(path, chunk)):
            if '"usage"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if ctx_tokens(e):
                return ctx_tokens(e), message(e).get("model")
    return None, None


def focus_written(path, since):
    for line in tail_lines(path, 8_000_000):
        if FOCUS_MARK not in line or '"assistant"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "assistant" and (e.get("timestamp") or "") >= since and any(
                b.get("type") == "text" and FOCUS_MARK in b.get("text", "") for b in blocks(e)):
            return True
    return False


# ---------- digest ----------

def cut_tail(chain, tail_tokens):
    sizes = [(i, c) for i, c in ((i, ctx_tokens(e)) for i, e in enumerate(chain)) if c]
    if not sizes:
        return chain, None
    current = sizes[-1][1]
    start = 0
    for i, c in sizes:
        if c <= current - tail_tokens:
            start = i + 1
    return chain[start:], current


def result_text(block):
    c = block.get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return str(c or "")


def clip(text, cap):
    text = text.strip()
    if len(text) <= cap:
        return text
    half = cap // 2
    return f"{text[:half]}\n[... {len(text) - cap} chars cut ...]\n{text[-half:]}"


def tool_line(block):
    inp = block.get("input") or {}
    args = []
    for k in KEY_ARGS:
        if inp.get(k) and len(args) < 2:
            args.append(f"{k}={str(inp[k]).replace(chr(10), ' / ')[:200]}")
    return f"{block.get('name')} " + " | ".join(args)


def tool_summary(run):
    counts = {}
    for b in run:
        counts[b.get("name")] = counts.get(b.get("name"), 0) + 1
    parts = ", ".join(f"{name} x{n}" for name, n in sorted(counts.items(), key=lambda x: -x[1]))
    return f"- ({len(run)} earlier tool calls: {parts})"


def iter_items(tail):
    """(kind, hh:mm, payload) in reading order: user text, claude text, tool calls, errors."""
    for e in tail:
        t = (e.get("timestamp") or "")[11:16]
        if e.get("type") == "user":
            if e.get("isMeta") or e.get("isCompactSummary") or e.get("isVisibleInTranscriptOnly"):
                continue
            for b in blocks(e):
                if b.get("type") == "text":
                    text = REMINDER_RE.sub("", b.get("text", "")).strip()
                    if text:
                        yield "user", t, text
                elif b.get("type") == "tool_result" and b.get("is_error"):
                    yield "error", t, result_text(b)
        elif e.get("type") == "assistant":
            for b in blocks(e):
                if b.get("type") == "text" and b.get("text", "").strip():
                    yield "claude", t, b["text"].strip()
                elif b.get("type") == "tool_use":
                    yield "tool", t, b


def origin_kind(e):
    origin = e.get("origin")
    return origin.get("kind") if isinstance(origin, dict) else None


def user_prompts(tail):
    """The times of the messages that the user wrote. Notifications and command echoes have another origin."""
    return [e.get("timestamp") or "" for e in tail if e.get("type") == "user" and origin_kind(e) == "human"]


def count_file(block, edited, read):
    fp = (block.get("input") or {}).get("file_path")
    if fp:
        bucket = edited if block.get("name") in EDIT_TOOLS else read
        bucket[fp] = bucket.get(fp, 0) + 1


def render_item(kind, t, p, cap):
    if kind == "tool":
        return f"- {t} {tool_line(p)}"
    if kind == "user":
        return f"\n### USER {t}\n{clip(p, USER_CAP)}\n"
    if kind == "claude":
        return f"\n**Claude {t}:** {clip(p, cap)}\n"
    return f"  - ERROR: {clip(p, RESULT_CAP)}"


def render(tail, meta, budget):
    items = list(iter_items(tail))
    listed = set([i for i, (k, _, _) in enumerate(items) if k == "tool"][-MAX_TOOL_LINES:])
    claude_idx = [i for i, (k, _, _) in enumerate(items) if k == "claude"]
    latest = set(claude_idx[-5:])
    fixed = sum(min(len(p), USER_CAP) for k, _, p in items if k == "user")
    fixed += 170 * (len(listed) + sum(1 for k, _, _ in items if k == "error"))
    fixed += LATEST_TEXT_CAP * len(latest)
    text_cap = max(400, (budget - fixed) // max(1, len(claude_idx) - len(latest)))
    edited, read, timeline, run = {}, {}, [], []
    for i, (kind, t, p) in enumerate(items):
        if kind == "tool":
            count_file(p, edited, read)
            if i not in listed:
                run.append(p)
                continue
        if run:
            timeline.append(tool_summary(run))
            run = []
        timeline.append(render_item(kind, t, p, LATEST_TEXT_CAP if i in latest else text_cap))
    if run:
        timeline.append(tool_summary(run))
    head = [
        f"# Compaction digest ({meta['tag']}) - session {meta['session']}",
        f"Written {meta['written']} by a hook. It covers {meta['scope']} "
        f"(context {meta['ctx']:,} tokens; it starts at transcript line {meta['line']}).",
        f"Transcript: {meta['transcript']}",
        "Mechanical extract: user messages verbatim, Claude's text (older texts clipped), tool calls, errors.",
        "",
        "## Files edited",
        *[f"- {f} ({n}x)" for f, n in sorted(edited.items(), key=lambda x: -x[1])],
        "",
        "## Files read (top 40)",
        *[f"- {f} ({n}x)" for f, n in sorted(read.items(), key=lambda x: -x[1])[:40]],
        "",
        "## Timeline (oldest first)",
    ]
    return "\n".join(head + timeline) + "\n"


def session_dir(hook):
    sid = hook.get("session_id") or Path(hook.get("transcript_path", "unknown")).stem
    return OUT_ROOT / sid


def write_digest(hook, tag, since=None):
    transcript = hook["transcript_path"]
    chain = live_context(load(transcript))
    ctx = next((ctx_tokens(e) for e in reversed(chain) if ctx_tokens(e)), 0)
    if tag == "task":
        tail, scope, budget = chain, "the whole cycle since the previous compaction", TASK_BUDGET_CHARS
    elif since:
        tail = [e for e in chain if (e.get("timestamp") or "") >= since]
        scope, budget = f"the work after the handoff ({since})", FINAL_BUDGET_CHARS
    else:
        tail, _ = cut_tail(chain, TAIL_TOKENS)
        scope, budget = f"the last ~{TAIL_TOKENS // 1000}K tokens", FINAL_BUDGET_CHARS
    if not tail:
        return None, []
    meta = dict(tag=tag, session=hook.get("session_id") or Path(transcript).stem, scope=scope, ctx=ctx,
                written=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line=tail[0].get("_line"),
                transcript=transcript)
    out_dir = session_dir(hook)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"digest-{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}.md"
    path.write_text(render(tail, meta, budget), encoding="utf-8")
    return path, tail


# ---------- timing ----------

def env_int(name):
    value = os.environ.get(name)
    return int(value) if value else None


def family(model):
    return "200k" if "haiku" in (model or "") else "1m"


def window_for(model):
    return 200_000 if family(model) == "200k" else 1_000_000


def parse_tokens(value):
    if isinstance(value, (int, float)):
        return int(value)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", str(value or ""))
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).lower()
    if unit == "m":
        return int(n * 1_000_000)
    return int(n * 1000) if unit == "k" or n <= 1000 else int(n)


def configured_point(model):
    """Docs: native-1M models compact at about 967K unless a window or a percentage lowers it."""
    window = env_int("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or parse_tokens(read_json(SETTINGS).get("autoCompactWindow"))
    base = min(window or 967_000, window_for(model) - 33_000)
    pct = os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
    return int(base * min(float(pct), 100) / 100) if pct else base


def points_file():
    return OUT_ROOT / "compaction-points.json"


def observed_point(model):
    data = read_json(points_file())
    fam = family(model)
    return data.get(fam) if data.get(fam + "_config") == configured_point(model) else None


def store_point(model, ctx):
    data = read_json(points_file())
    fam, config = family(model), configured_point(model)
    history = data.get(fam + "_history", []) if data.get(fam + "_config") == config else []
    history = (history + [ctx])[-5:]
    data.update({fam: min(history), fam + "_history": history, fam + "_config": config})
    write_json(points_file(), data)


def compact_point(model):
    return env_int("COMPACTION_POINT") or observed_point(model) or configured_point(model)


def project_key(cwd):
    return re.split(r"[\\/]\.claude[\\/]worktrees[\\/]", cwd or "")[0].rstrip("\\/").lower()


def costs_file():
    return OUT_ROOT / "handoff-costs.jsonl"


def handoff_budget(cwd):
    if env_int("COMPACTION_HANDOFF_BUDGET"):
        return env_int("COMPACTION_HANDOFF_BUDGET")
    key = project_key(cwd)
    recent = [r.get("cost", 0) for r in read_jsonl(costs_file()) if r.get("project") == key][-10:]
    if len(recent) < 3:
        return DEFAULT_BUDGET
    recent.sort()
    lo, hi = BUDGET_RANGE
    return max(lo, min(hi, int(recent[int(0.8 * (len(recent) - 1))] * 1.2)))


def handoff_point(model, cwd):
    return env_int("COMPACTION_HANDOFF_AT") or compact_point(model) - handoff_budget(cwd)


def focus_point(model):
    return env_int("COMPACTION_FOCUS_AT") or compact_point(model) - FOCUS_LEAD


def block_ceiling(model):
    return env_int("COMPACTION_BLOCK_CEILING") or window_for(model) - HARD_MARGIN


# ---------- session state ----------

def load_state(out_dir, ctx):
    """A context far below the last handoff means a compaction ran: forget the old cycle for good."""
    state = read_json(out_dir / "state.json")
    if state.get("handoff_ctx") and ctx < state["handoff_ctx"] // 2:
        (out_dir / "state.json").unlink(missing_ok=True)
        return {}
    return state


def save_state(out_dir, state):
    write_json(out_dir / "state.json", state)


def marker_path(out_dir):
    return out_dir / "handoff-done.txt"


def handoff_done(out_dir, state):
    marker = marker_path(out_dir)
    return bool(state.get("handoff_asked")) and marker.exists() and utc_of_mtime(marker) >= state["handoff_asked"]


def handoff_file(out_dir):
    marker = marker_path(out_dir)
    return marker.read_text(encoding="utf-8").strip() if marker.exists() else ""


def log_cost(hook, state, ctx):
    record = {"project": project_key(hook.get("cwd")), "session": hook.get("session_id"),
              "asked_ctx": state["handoff_ctx"], "done_ctx": ctx, "cost": max(0, ctx - state["handoff_ctx"]),
              "asked": state["handoff_asked"], "done": utc_of_mtime(marker_path(session_dir(hook)))}
    costs_file().parent.mkdir(parents=True, exist_ok=True)
    with open(costs_file(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def newest(folder, pattern):
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


# ---------- messages to the model ----------

def handoff_request(ctx, model, cwd, out_dir, digest):
    return (f"[compaction-handoff] The context is about {ctx // 1000}K tokens. Auto-compaction starts near "
            f"{compact_point(model) // 1000}K tokens. The handoff budget for this project is about "
            f"{handoff_budget(cwd) // 1000}K tokens. Run the {SKILL} skill before you continue the task. "
            f"The skill input is the cycle digest {digest}. It lists the user messages of this cycle word "
            "for word, your text, the tool calls and the edited files. Write the handoff where the project "
            f"CLAUDE.md puts session documents. If CLAUDE.md gives no location, write {out_dir / 'handoff.md'}. "
            f"When the handoff is complete, write its path into {marker_path(out_dir)}. Compaction waits for "
            f"this file until the context is about {block_ceiling(model) // 1000}K tokens. Take the time that "
            "the skill needs. The recovery hook pastes the Resume section of the handoff into the context after "
            "compaction, so write that section as a continuation prompt. Then continue the task. If the "
            f"{SKILL} skill is not available, start the handoff with the heading '## 1. Resume': a continuation "
            "prompt with the blocks READ IN THIS ORDER, WHERE THINGS STAND, DO NEXT and HARD RULES. Add these "
            "sections after it: Working set, User instructions, Reasoning state, State, Decisions, Refuted "
            "approaches, Open items, Verification log.")


def refresh_request(ctx, state, out_dir):
    handoff = handoff_file(out_dir) or str(out_dir / "handoff.md")
    return (f"[compaction-handoff] Handoff refresh. The handoff {handoff} is from about "
            f"{state['done_ctx'] // 1000}K tokens. The context is now about {ctx // 1000}K tokens. "
            "Compaction did not run in this time. The handoff is thus older than the work. Update it now "
            "for the work after that point. Rewrite the Resume section. Its four blocks are READ IN THIS "
            "ORDER, WHERE THINGS STAND, DO NEXT and HARD RULES. Give the evidence for each status claim. "
            "Add each new instruction and ruling of the user word for word. Update the state and the next "
            "steps. Do not run the probes of the skill again. This work is still in your context, so you do "
            f"not need a digest. Then write the handoff path into {marker_path(out_dir)} again. Then "
            "continue the task.")


def focus_request(ctx, model, out_dir):
    handoff = handoff_file(out_dir) or "the handoff file"
    return (f"[compaction-focus] The context is about {ctx // 1000}K tokens. Auto-compaction starts near "
            f"{compact_point(model) // 1000}K tokens. Write one chat message that starts with {FOCUS_MARK}. "
            "The compaction summarizer reads it as its instructions. In the message, do these four things. "
            "1) Name the current work. 2) List the items that must stay word for word: the latest user "
            "instructions, file paths with line numbers, commands, IDs, numbers and the next step. "
            "3) List the finished work to compress to one line each. "
            f"4) Give the path of the handoff: {handoff}. Use 400 words or fewer. Then continue the task.")


def inject(text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolBatch", "additionalContext": text}},
                     ensure_ascii=False))


# ---------- subagents ----------

def in_subagent(hook):
    return bool(hook.get("agent_id")) or "subagents" in Path(hook.get("transcript_path") or "").parts


def fill(path):
    """The used share of the context window at the end of a transcript. A compaction resets it to 0."""
    for chunk in (1_000_000, 8_000_000):
        try:
            lines = tail_lines(path, chunk)
        except OSError:  # the transcript is gone
            return 0
        for line in reversed(lines):
            if '"usage"' not in line and '"compact_boundary"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if is_boundary(e):
                return 0
            if ctx_tokens(e):
                return ctx_tokens(e) / window_for(message(e).get("model"))
    return 0


def age(path):
    try:
        return time.time() - path.stat().st_mtime
    except OSError:  # the transcript is gone
        return float("inf")


def compacting_subagent(path):
    """The subagent transcript, written in the last minute, that is fuller than the main one."""
    main = Path(path)
    fills = {p: fill(p) for p in main.with_suffix("").joinpath("subagents").rglob("agent-*.jsonl")
             if age(p) < LIVE_SECONDS}
    agent = max(fills, key=fills.get, default=None)
    return agent if agent and fills[agent] > fill(main) else None


def note_subagent_compaction(path, out_dir):
    agent = compacting_subagent(path)
    if agent:
        write_json(out_dir / f"subagent-compaction-{agent.stem}.json",
                   {"transcript": str(agent), "fill": fill(agent)})
    return bool(agent)


def claim_subagent_compaction(out_dir):
    """A record counts while the subagent's context is unchanged, that is, while its compaction runs.
    Only the hook that deletes a record uses it, so one record skips one SessionStart."""
    for record in out_dir.glob("subagent-compaction-*.json"):
        try:
            age = time.time() - record.stat().st_mtime
            data = read_json(record)
            record.unlink()
        except OSError:  # another hook took the record first
            continue
        agent = Path(data.get("transcript") or "")
        if age < RECORD_SECONDS and agent.is_file() and fill(agent) == data.get("fill"):
            return True
    return False


# ---------- hook entry points ----------

def usable(hook):
    path = hook.get("transcript_path")
    return not in_subagent(hook) and path and Path(path).exists()


def watch(hook):
    if not usable(hook):
        return
    path = hook["transcript_path"]
    ctx, model = last_usage(path)
    if not ctx:
        return
    ctx += sum(len(str(c.get("tool_response", ""))) for c in hook.get("tool_calls") or []) // 3
    out_dir = session_dir(hook)
    state = load_state(out_dir, ctx)
    if not state.get("handoff_asked"):
        if ctx >= handoff_point(model, hook.get("cwd")):
            digest, _ = write_digest(hook, "task")
            state.update(handoff_asked=now_utc(), handoff_ctx=ctx, cycle=cycle_id(path),
                         budget=handoff_budget(hook.get("cwd")))
            save_state(out_dir, state)
            inject(handoff_request(ctx, model, hook.get("cwd"), out_dir, digest))
        return
    if not handoff_done(out_dir, state):
        return
    if not state.get("cost_logged"):
        log_cost(hook, state, ctx)
        state["cost_logged"] = True
        save_state(out_dir, state)
    marker_at = utc_of_mtime(marker_path(out_dir))
    if marker_at != state.get("marker_at"):  # the handoff was written or refreshed: the baseline moves
        state.update(marker_at=marker_at, done_ctx=ctx)
        save_state(out_dir, state)
    if FOCUS_ENABLED and not state.get("focus_asked") and ctx >= focus_point(model):
        state.update(focus_asked=now_utc(), focus_ctx=ctx)
        save_state(out_dir, state)
        inject(focus_request(ctx, model, out_dir))
        return
    budget = state.get("budget") or handoff_budget(hook.get("cwd"))
    if ctx >= max(state.get("done_ctx") or ctx, state.get("refresh_ctx") or 0) + budget:
        state["refresh_ctx"] = ctx
        save_state(out_dir, state)
        inject(refresh_request(ctx, state, out_dir))


def should_block(state, out_dir, path, ctx, model):
    if ctx >= block_ceiling(model) or not state.get("handoff_asked"):
        return False
    if not handoff_done(out_dir, state):
        return True
    if not FOCUS_ENABLED:
        return False
    if not state.get("focus_asked"):
        return ctx < compact_point(model) + FOCUS_GRACE
    return not focus_written(path, state["focus_asked"]) and ctx < state.get("focus_ctx", ctx) + FOCUS_GRACE


def precompact(hook):
    if not usable(hook):
        return
    path = hook["transcript_path"]
    out_dir = session_dir(hook)
    if hook.get("trigger") == "auto" and note_subagent_compaction(path, out_dir):
        return
    ctx, model = last_usage(path)
    cycle = cycle_id(path)
    state = read_json(out_dir / "state.json")
    if state.get("cycle") != cycle:
        state = {"cycle": cycle}
    if hook.get("trigger") == "auto" and ctx:
        if not state.get("first_precompact_ctx"):
            store_point(model, ctx)
            state["first_precompact_ctx"] = ctx
            save_state(out_dir, state)
        if should_block(state, out_dir, path, ctx, model):
            print(json.dumps({"decision": "block",
                              "reason": "compaction-recovery: the handoff or the COMPACT-FOCUS message is missing"}))
            return
    since = utc_of_mtime(marker_path(out_dir)) if handoff_done(out_dir, state) else None
    digest, tail = write_digest(hook, "final", since)
    if since and digest:
        prompts = user_prompts(tail)
        state.update(late_prompts=len(prompts), last_prompt_at=prompts[-1] if prompts else "")
        save_state(out_dir, state)


# The resume message pastes the handoff's Resume section (the continuation prompt). Hook output is capped
# at 10,000 characters, so the pasted part is capped well below that.
RESUME_CAP = 7000
RESUME_HEAD_RE = re.compile(r"^(#{1,3})\s*(?:\d+\.\s*)?(?:resume|continuation prompt)\b", re.I | re.M)


def resume_section(text):
    """The handoff's Resume section, up to the next heading of the same or a higher level."""
    m = RESUME_HEAD_RE.search(text)
    if not m:
        return ""
    level = len(m.group(1))
    lines = []
    for line in text[m.end():].splitlines()[1:]:
        h = re.match(r"(#{1,6})\s", line)
        if h and len(h.group(1)) <= level:
            break
        if not line.strip().startswith("```"):
            lines.append(line)
    return re.sub(r"(\n\s*-{3,}\s*)+$", "", "\n".join(lines).strip())


def clip_head(text, cap):
    return text if len(text) <= cap else text[:cap].rstrip() + "\n[... cut here: read the rest in the handoff file ...]"


def continuation_prompt(handoff):
    try:
        return clip_head(resume_section(Path(handoff).read_text(encoding="utf-8")), RESUME_CAP)
    except OSError:
        return ""


def short_time(ts):
    return f"{ts[:10]} {ts[11:16]} UTC" if len(ts) >= 16 else ts


def late_warning(state, since, final):
    return (f"Warning: the user wrote {state['late_prompts']} message(s) after the handoff ({since}). The last "
            f"one came at {short_time(state.get('last_prompt_at') or '')}. The final digest {final} holds these "
            "messages word for word. The handoff is older than them. Where the handoff disagrees with these "
            "messages, or with the final digest, the messages and the final digest win.")


def resume(hook):
    out_dir = session_dir(hook)
    if in_subagent(hook) or not out_dir.exists() or claim_subagent_compaction(out_dir):
        return
    state = read_json(out_dir / "state.json")
    current = handoff_done(out_dir, state)
    (out_dir / "state.json").unlink(missing_ok=True)  # the compaction ended the cycle
    handoff = handoff_file(out_dir)
    final, task = newest(out_dir, "digest-final-*.md"), newest(out_dir, "digest-task-*.md")
    if not (handoff or final):
        return
    prompt = continuation_prompt(handoff) if handoff and current else ""
    late = state.get("late_prompts", 0) if current and final else 0
    since = short_time(utc_of_mtime(marker_path(out_dir))) if late else ""
    lines = ["[compaction-recovery] Compaction ran. The summary above loses detail of the recent work."]
    if late:
        lines.append(late_warning(state, since, final))
    if prompt:
        lines += ["Before any other action, do the READ IN THIS ORDER list below completely, including every "
                  "project document that it names. "
                  + ("Then read the final digest. Then do the newest open request of the user. Use the DO NEXT "
                     "list only where the final digest does not replace it." if late else "Then do the DO NEXT "
                     "list."),
                  f"Continuation prompt, from the Resume section of the handoff {handoff}:", "<<<", prompt, ">>>"]
    elif handoff and current:
        lines.append(f"Before any other action, read the handoff {handoff}. Start with its Resume section and "
                     "read every document that it names.")
    elif handoff:
        lines.append(f"No handoff was written before this compaction. The last handoff, {handoff}, is from an "
                     "earlier cycle and can be out of date. Use it for background only.")
    if final:
        lines.append(f"Read {final}. It holds the last part of the work before compaction, word for word.")
    if late:
        trust = (f"For the work after {since}, trust the final digest and the files. For older work, trust the "
                 "handoff and the files. ")
    elif prompt:
        trust = "If the summary and the continuation prompt disagree, trust the prompt and the files. "
    else:
        trust = "If the summary and the files disagree, trust the files. "
    lines.append(trust
                 + (f"The cycle digest {task} holds the cycle before the handoff. Read it only for a detail "
                    "that the handoff does not hold. " if task and current else "")
                 + f"For other details, search the transcript {hook.get('transcript_path', '')}. "
                 "Do not repeat finished steps. Do not start a verification probe unless DO NEXT asks for one.")
    print("\n".join(lines))


# ---------- offline test ----------

def summary_text(e):
    return " ".join(b.get("text", "") for b in blocks(e) if b.get("type") == "text")


def simulate(transcript, k, out_path):
    entries = load(transcript)
    b = [e for e in entries if is_boundary(e)][k - 1]
    tail, ctx = cut_tail(live_context(entries, b.get("logicalParentUuid")), TAIL_TOKENS)
    meta = dict(tag="simulated", session=Path(transcript).stem, scope=f"the last ~{TAIL_TOKENS // 1000}K tokens",
                ctx=ctx or 0, written=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line=tail[0].get("_line"),
                transcript=transcript)
    digest = render(tail, meta, FINAL_BUDGET_CHARS)
    Path(out_path).write_text(digest, encoding="utf-8")
    summary = next((summary_text(e) for e in entries
                    if e.get("isCompactSummary") and e.get("parentUuid") == b.get("uuid")), "")
    s = re.sub(r"\s+", " ", summary.lower())
    files = {(p.get("input") or {}).get("file_path") for kind, _, p in iter_items(tail) if kind == "tool"} - {None}
    cm = b.get("compactMetadata") or {}
    print(f"boundary {b.get('timestamp', '')[:16]} pre={cm.get('preTokens')} post={cm.get('postTokens')} | "
          f"summary chars={len(summary):,} | digest chars={len(digest):,} | files of the tail named in the "
          f"summary: {sum(Path(f).name.lower() in s for f in files)}/{len(files)}")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "simulate":
        simulate(sys.argv[2], int(sys.argv[3]), sys.argv[4])
        return
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        hook = json.loads(raw) if raw.strip() else {}
        {"watch": watch, "precompact": precompact, "resume": resume}.get(mode, lambda h: None)(hook)
    except Exception as exc:  # a recovery hook must never break compaction or startup
        print(f"[compaction-recovery] hook error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
