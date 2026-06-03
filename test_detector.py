#!/usr/bin/env python3
"""
Offline tests for the auto-approval detector in pty-wrapper.py.

These do NOT spawn `claude`. They load the detection logic directly and feed
it the kind of (ANSI-stripped) text Claude Code paints when it shows a
permission prompt, so the decision logic can be verified without a live PTY.

Run:  python3 test_detector.py
Exit code is non-zero if any check fails.
"""
import importlib.util
import os
import re

# Load pty-wrapper.py (hyphen in the name → import by path).
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


# --- Fixtures: approximate strip_ansi() output for a permission prompt ------
# strip_ansi turns escape codes into spaces and collapses runs of spaces, so
# real captured text is single-spaced with box-drawing glyphs left intact.

def box(header, command, description, extra_pad=0):
    """Build a permission-prompt blob resembling stripped terminal output.
    `header` is the tool-call line (e.g. "Bash(find ...)" or "Grep(...)").
    `extra_pad` inflates the gap between header and prompt to simulate long
    commands / tall boxes that push the header far above the prompt."""
    pad = (" filler" * extra_pad)
    return (
        f"● {header}\n"
        f" {description}{pad}\n"
        "╭────────╮\n"
        f" {command}\n"
        f" {description}\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        " 2. Yes, and don't ask again this session\n"
        " 3. No, and tell Claude what to do differently (esc)\n"
        "╰────────╯\n"
    )


SAFE_FIND = box("Bash(find . -name '*.py' -type f)",
                "find . -name '*.py' -type f", "Find Python files")
SAFE_GREP_TOOL = box("Grep(pattern: 'TODO', path: 'src')",
                     "Grep TODO in src", "Search for TODO")
SAFE_READ = box("Read(/Users/me/project/main.py)",
                "Read main.py", "Read the file")
SAFE_RG_BASH = box("Bash(rg -n 'def ' src/)", "rg -n 'def ' src/", "ripgrep")
UNSAFE_EDIT = box("Edit(/Users/me/project/main.py)",
                  "Edit main.py", "Modify the file")
UNSAFE_RM = box("Bash(rm -rf build/)", "rm -rf build/", "Remove build dir")
UNSAFE_GIT = box("Bash(git push origin main)", "git push origin main", "Push")

RESULT = (
    " ⏺ Done. Found 12 matches across 4 files. Here is a short summary of "
    "what I found and what I plan to do next, which is several lines of "
    "ordinary assistant prose with no permission prompt anywhere in it. " * 4
)


def fresh(*chunks, now=100.0):
    d = pw.Detector()
    for c in chunks:
        d.feed(c)
    return d


# --- classify_pending_tool -------------------------------------------------
check(pw.classify_pending_tool(SAFE_FIND) is True, "classify: find → safe")
check(pw.classify_pending_tool(SAFE_RG_BASH) is True, "classify: rg → safe")
check(pw.classify_pending_tool(SAFE_GREP_TOOL) is True, "classify: Grep tool → safe")
check(pw.classify_pending_tool(SAFE_READ) is True, "classify: Read → safe")
check(pw.classify_pending_tool(UNSAFE_EDIT) is False, "classify: Edit → unsafe")
check(pw.classify_pending_tool(UNSAFE_RM) is False, "classify: rm → unsafe")
check(pw.classify_pending_tool(UNSAFE_GIT) is False, "classify: git push → unsafe")
check(pw.classify_pending_tool("just some prose, no tool") is None,
      "classify: no header → None")

# The pending tool is the LAST header: a safe Grep that earlier ran, then an
# Edit prompt now — must classify as unsafe (the bug the old window-based
# check could get wrong).
check(pw.classify_pending_tool(SAFE_GREP_TOOL + RESULT + UNSAFE_EDIT) is False,
      "classify: safe-then-unsafe uses the latest header")

# --- core fire decision ----------------------------------------------------
check(fresh(SAFE_FIND).poll(100.0) is True, "fire: safe find prompt")
check(fresh(SAFE_GREP_TOOL).poll(100.0) is True, "fire: safe Grep prompt")
check(fresh(UNSAFE_EDIT).poll(100.0) is False, "no-fire: Edit prompt")
check(fresh(UNSAFE_RM).poll(100.0) is False, "no-fire: rm prompt")
check(fresh(RESULT).poll(100.0) is False, "no-fire: prose with no prompt")

# --- cause #1: header far above the prompt (long command / tall box) -------
# Old code required tool+prompt within ~800-1500 chars of each other; a long
# find/grep blew that window and the prompt waited. Classifying from the whole
# buffer fixes it.
FAR = box("Bash(find . -path './node_modules' -prune -o -name '*.py' -print)",
          "find . -name '*.py'", "A very long description", extra_pad=400)
check(len(FAR) > 2000, "fixture: far-header blob really is large")
header_to_prompt = FAR.index("Do you want to proceed?") - FAR.index("Bash(")
check(header_to_prompt > 1500,
      f"fixture: header sits {header_to_prompt} chars above prompt (>1500)")
check(fresh(FAR).poll(100.0) is True,
      "fire: long find with header far above the prompt")

# --- cause #2: static prompt is re-checked on later polls ------------------
# A prompt that finished rendering and produced no further bytes must still be
# approvable on a subsequent poll (the loop polls on its select() timeout).
d = fresh(SAFE_FIND)
check(d.poll(100.0) is True and d.poll(100.20) is True,
      "fire: static prompt still fires on a later poll (no new bytes)")

# --- cause #3 + stray-1 guard: bursts and re-arming ------------------------
d = fresh(SAFE_FIND)
check(d.poll(100.0) is True, "burst: first prompt fires")
d.commit_fired(100.0)
# Within cooldown, even if the same prompt repaints, do not re-fire.
d.feed(SAFE_FIND)
check(d.poll(100.3) is False, "burst: no re-fire during cooldown (repaint)")
# Prompt gets answered → screen shows results (no prompt) → detector re-arms.
d.feed(RESULT)
check(d.poll(100.4) is False, "burst: result screen does not fire")
# A genuinely new safe prompt after the cooldown fires again.
d.feed(SAFE_GREP_TOOL)
check(d.poll(101.0) is True, "burst: a new prompt after cooldown fires")

# After firing, an answered prompt that lingers in scrollback must not produce
# a stray "1" once the cooldown lapses (the classic stray-injection bug).
d = fresh(SAFE_FIND)
d.poll(100.0)
d.commit_fired(100.0)
d.feed(" short ack line\n")          # tiny result, prompt text now gone from view
stray = any(d.poll(t) for t in (101.0, 101.5, 102.0, 120.0))
check(stray is False, "stray-1: answered prompt does not re-fire when idle")

# --- prose / quoted-text safety --------------------------------------------
# Discussing the prompt in chat (no real menu at the bottom) must not fire.
CHAT = ("You asked why it waits. The wrapper looks for 'Do you want to "
        "proceed?' followed by '1. Yes' and a Grep( or Bash(find ...) header. "
        "Here is what that looks like in the transcript, quoted inline. " * 3)
check(fresh(CHAT).poll(100.0) is False,
      "safety: prose merely mentioning the pattern does not fire")

# --- strip_ansi ------------------------------------------------------------
raw = b"\x1b[2m\x1b[38;5;241m Do you want\x1b[0m   to    proceed?\x1b[?25l"
check("Do you want to proceed?" in pw.strip_ansi(raw),
      "strip_ansi: removes CSI codes and collapses spaces")

# --- version banner --------------------------------------------------------
_digest, _mtime = pw.self_fingerprint()
check(re.fullmatch(r"[0-9a-f]{8}", _digest) is not None,
      "fingerprint: returns an 8-char hex content hash")
check(isinstance(pw.__version__, str) and pw.__version__,
      "fingerprint: __version__ is a non-empty string")

print()
if _FAILS:
    print(f"{len(_FAILS)} CHECK(S) FAILED")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
