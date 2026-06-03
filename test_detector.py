#!/usr/bin/env python3
"""
Offline tests for the auto-approval detector in pty-wrapper.py.

These do NOT spawn `claude`. They load the detection logic directly and feed
it the kind of (ANSI-stripped) text Claude Code paints when it shows a
permission prompt, so the decision logic can be verified without a live PTY.

Fixtures mirror the REAL permission-prompt format (see the screenshot that
motivated the fix): a Bash command renders as a "Bash command" box with the
raw command and "Permission rule Bash …" — NOT as "Bash(...)". The transcript
"● Bash(...)" form is only for already-completed calls.

Run:  python3 test_detector.py
"""
import importlib.util
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "pty_wrapper", os.path.join(_HERE, "pty-wrapper.py")
)
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)

_FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        _FAILS.append(msg)


# --- Fixtures: approximate strip_ansi() output ------------------------------
MENU = (" ❯ 1. Yes\n"
        "   2. Yes, and don't ask again in this project\n"
        "   3. No, and tell Claude what to do differently (esc)\n")


def bash_box(command, description="Run shell command"):
    """A pending Bash permission prompt, as Claude actually renders it."""
    return (" Bash command\n"
            f"   {command}\n"
            f"   {description}\n"
            " Permission rule Bash requires confirmation for this command.\n"
            " Do you want to proceed?\n" + MENU)


def tool_box(header, description):
    """A pending read-only tool prompt (tool-call header + menu)."""
    return (f" ● {header}\n"
            f"   {description}\n"
            " Do you want to proceed?\n" + MENU)


# The exact command from the screenshot that wasn't being auto-approved.
GREP = bash_box(
    'grep -r "metric\\|telemetry\\|analytics\\|thumbsUp\\|feedback\\|review" '
    '/Users/yingbeit/workspace/nvcoworkmvp/src/core --include="*.ts" -l | head -20'
)
FIND = bash_box('find . -name "*.py" -type f')
RG = bash_box('rg -n "def " src/')
RM = bash_box('rm -rf build/')
GIT = bash_box('git push origin main')

READ_TOOL = tool_box('Read(/Users/me/project/main.py)', 'Read the file')
GREP_TOOL = tool_box('Grep(pattern: "TODO", path: "src")', 'Search files')
EDIT_TOOL = tool_box('Edit(/Users/me/project/main.py)', 'Modify the file')

# Completed bash calls render with the "Bash(...)" header in the transcript —
# this is what sits ABOVE a subagent's pending box (the screenshot scenario).
TRANSCRIPT = (
    " ● Bash(git branch --show-current)\n   yingbei/agent-quality\n"
    " ● Bash(git log --oneline -5)\n   abfd1b32 improve agent preset config UI\n"
    "   54314d52 Merge branch 'feature/x' into 'main'\n"
)
RESULT = (" ⏺ Done. Found 12 matches across 4 files; here is a multi-line "
          "summary of ordinary assistant prose with no prompt in it. " * 6)


def fresh(*chunks):
    d = pw.Detector()
    for c in chunks:
        d.feed(c)
    return d


# --- classify_pending_tool --------------------------------------------------
check(pw.classify_pending_tool(GREP) is True, "classify: grep box → safe")
check(pw.classify_pending_tool(FIND) is True, "classify: find box → safe")
check(pw.classify_pending_tool(RG) is True, "classify: rg box → safe")
check(pw.classify_pending_tool(RM) is False, "classify: rm box → unsafe")
check(pw.classify_pending_tool(GIT) is False, "classify: git box → unsafe (git excluded)")
check(pw.classify_pending_tool(READ_TOOL) is True, "classify: Read tool → safe")
check(pw.classify_pending_tool(GREP_TOOL) is True, "classify: Grep tool → safe")
check(pw.classify_pending_tool(EDIT_TOOL) is False, "classify: Edit tool → unsafe")
check(pw.classify_pending_tool("ordinary prose, no prompt") is None,
      "classify: no tool → None")

# THE regression: a subagent grep whose box sits below completed git headers.
# Must classify the grep box, not be fooled by the "Bash(git ...)" headers.
check(pw.classify_pending_tool(TRANSCRIPT + GREP) is True,
      "classify: pending grep box wins over completed Bash(git ...) headers")

# A long command must still classify by its first word (region is wide enough).
LONG = bash_box('grep -rn "' + ("x" * 1500) + '" .')
check(len(LONG) > 1600 and pw.classify_pending_tool(LONG) is True,
      "classify: long grep command still safe")

# Known limitation (documented): only the FIRST word is checked, so a
# redirecting echo is still approved. Asserting it so the behavior is explicit.
check(pw.classify_pending_tool(bash_box('echo pwned > ~/.zshrc')) is True,
      "classify: echo-with-redirect approved (first-word-only limitation)")

# --- fire decision ----------------------------------------------------------
check(fresh(GREP).poll(100.0) is True, "fire: grep prompt")
check(fresh(TRANSCRIPT + GREP).poll(100.0) is True, "fire: subagent grep prompt")
check(fresh(READ_TOOL).poll(100.0) is True, "fire: Read prompt")
check(fresh(RM).poll(100.0) is False, "no-fire: rm prompt")
check(fresh(EDIT_TOOL).poll(100.0) is False, "no-fire: Edit prompt")
check(fresh(RESULT).poll(100.0) is False, "no-fire: prose, no prompt")

# --- static prompt re-checked on a later poll (no new bytes) ---------------
d = fresh(GREP)
check(d.poll(100.0) is True and d.poll(100.2) is True,
      "fire: static prompt still fires on a later poll")

# --- burst + stray-1 guard --------------------------------------------------
d = fresh(GREP)
check(d.poll(100.0) is True, "burst: first prompt fires")
d.commit_fired(100.0)
d.feed(GREP)                       # repaint of the same prompt during cooldown
check(d.poll(100.3) is False, "burst: no re-fire during cooldown")
d.feed(RESULT)                     # answered → results on screen → re-arm
check(d.poll(100.4) is False, "burst: result screen does not fire")
d.feed(FIND)                       # a genuinely new prompt after cooldown
check(d.poll(101.0) is True, "burst: new prompt after cooldown fires")

# Idle after answering must not inject a stray "1".
d = fresh(GREP)
d.poll(100.0)
d.commit_fired(100.0)
d.feed(" short ack\n")
check(not any(d.poll(t) for t in (101.0, 101.5, 120.0)),
      "stray-1: answered prompt stays quiet when idle")

# --- prose safety -----------------------------------------------------------
CHAT = ("Why does it wait? It looks for 'Do you want to proceed?' then a "
        "grep or find command. Quoting that inline should not trigger it. " * 4)
check(fresh(CHAT).poll(100.0) is False, "safety: quoted prose does not fire")

# --- strip_ansi + fingerprint ----------------------------------------------
raw = b"\x1b[2m\x1b[38;5;241m Do you want\x1b[0m   to    proceed?\x1b[?25l"
check("Do you want to proceed?" in pw.strip_ansi(raw),
      "strip_ansi: removes CSI codes and collapses spaces")
_digest, _ = pw.self_fingerprint()
check(re.fullmatch(r"[0-9a-f]{8}", _digest) is not None,
      "fingerprint: 8-char hex content hash")
check(isinstance(pw.__version__, str) and pw.__version__,
      "fingerprint: __version__ is a non-empty string")

print()
if _FAILS:
    print(f"{len(_FAILS)} CHECK(S) FAILED")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
