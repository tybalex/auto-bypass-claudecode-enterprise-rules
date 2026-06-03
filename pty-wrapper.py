#!/usr/bin/env python3.13
"""
PTY wrapper for Claude Code CLI.
Runs `claude` interactively but auto-approves read-only tool prompts.

Usage:
  python3.13 ~/.claude/pty-wrapper.py [any claude args...]
  e.g.  python3.13 ~/.claude/pty-wrapper.py
        python3.13 ~/.claude/pty-wrapper.py --model sonnet

Detection design (see test_detector.py for the spec it must satisfy):
  * The PROMPT must be at the BOTTOM of the screen (matched in a short tail) —
    this keeps prose/quoted text higher up from triggering us.
  * The pending TOOL is identified by the LAST tool-call header anywhere in the
    buffer, not just the tail. Long `find`/`grep` commands push their
    `● Bash(...)` / `Grep(...)` header far above the prompt; classifying from
    the whole buffer (not a 1500-char window) is what makes those reliable.
  * Detection re-runs on the select() timeout, not only when new bytes arrive,
    so a prompt that finishes rendering and then sits STATIC still gets caught.
  * An `armed` latch + short cooldown together prevent re-firing a stray "1"
    into the chat input after a prompt has already been answered.
"""
import fcntl
import hashlib
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import tty

# Human-readable version. Bump when you change detection behavior. The content
# hash in the startup banner is the authoritative identifier; this is just a
# friendly label.
__version__ = "2026-06-02"

# Strip ANSI/VT escape sequences (CSI, OSC, two-byte, etc.)
ANSI_RE = re.compile(
    rb'\x1b(?:'
    rb'\[[0-?]*[ -/]*[@-~]'       # CSI sequences
    rb'|\][^\x07]*\x07'           # OSC sequences (terminated by BEL)
    rb'|\][^\x1b]*\x1b\\'        # OSC sequences (terminated by ST)
    rb'|[^[\]].?'                 # two-byte sequences
    rb')'
)
MULTI_SPACE_RE = re.compile(r' {2,}')

# --- Tool classification ---------------------------------------------------
# Read-only tools we auto-approve.
SAFE_TOOLS = ('Read', 'Glob', 'Grep', 'ToolSearch', 'TaskGet', 'TaskList')
# Other tools we recognize purely so a pending unsafe tool is classified as
# unsafe (and therefore NOT auto-approved) instead of falling back to an older,
# already-answered safe header still sitting in the buffer.
OTHER_TOOLS = (
    'Bash', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Task', 'Agent',
    'WebFetch', 'WebSearch', 'TaskCreate', 'TaskUpdate', 'TaskStop',
    'KillShell', 'BashOutput', 'SlashCommand', 'ExitPlanMode', 'EnterPlanMode',
)
# A tool-call header as Claude Code renders it: `Name(`. Requiring the paren
# keeps us from matching the tool *names* when they appear in ordinary prose.
KNOWN_TOOL_RE = re.compile(
    r'\b(' + '|'.join(SAFE_TOOLS + OTHER_TOOLS) + r')\s*\(',
)
# Bash read-only commands, matched against the start of the command inside
# `Bash(...)`. Git is intentionally excluded: even read-only git subcommands
# are not in the enterprise auto-approve ruleset. To auto-approve read-only
# git too, add `|git\s+(status|log|diff|show|branch|remote|tag)` here.
SAFE_BASH_CMDS = (
    r'ls|find|head|wc|cat|grep|rg|tail|file|stat|du|df|pwd|echo|which'
    r'|whoami|env|printenv|uname|hostname|date|id'
)
SAFE_BASH_START = re.compile(rf'\s*({SAFE_BASH_CMDS})\b', re.IGNORECASE)

# The approval prompt — require the real numbered MENU Claude Code renders:
# "Do you want to proceed?" then "1. Yes" then a "2." option. Demanding the
# 1→2 structure (not just a stray "1. Yes") keeps prose that merely quotes the
# prompt — e.g. discussing this script inside a session — from triggering us.
# Bounded gaps keep the match from spanning unrelated output.
PROMPT_RE = re.compile(
    r'Do you want to proceed\?.{0,400}?\b1\.\s*Yes\b.{0,400}?\b2\.',
    re.DOTALL,
)

# Tuning.
TAIL_WINDOW = 900      # the live prompt is always at the bottom of the screen
BUF_MAX = 16000        # how far back we look for the pending tool's header
COOLDOWN = 0.8         # seconds after a fire before we may fire again
SETTLE = 0.08          # let the prompt finish rendering before we re-verify


def strip_ansi(data: bytes) -> str:
    # Replace ANSI sequences with a space (cursor moves act as spaces)
    text = ANSI_RE.sub(b' ', data).decode('utf-8', errors='ignore')
    # Collapse multiple spaces into one
    return MULTI_SPACE_RE.sub(' ', text)


def classify_pending_tool(text: str):
    """Return True if the most recent tool-call header in `text` is a safe
    read-only tool, False if it is some other (unsafe) tool, or None if no
    recognized tool header is present.

    The pending tool is always the LAST recognized `Name(` header before the
    prompt — Claude renders the tool header immediately above its permission
    box, so the bottom-most header is the one being asked about.
    """
    last = None
    for m in KNOWN_TOOL_RE.finditer(text):
        last = m
    if last is None:
        return None
    name = last.group(1)
    if name == 'Bash':
        return bool(SAFE_BASH_START.match(text[last.end():]))
    return name in SAFE_TOOLS


class Detector:
    """Decides, from a rolling buffer of stripped terminal text, whether the
    auto-approval keystroke should be sent. Pure w.r.t. I/O so it can be
    exercised by test_detector.py without a real PTY."""

    def __init__(self):
        self.text_buf = ''
        self.armed = True          # may we fire for the current screen state?
        self.cooldown_until = 0.0  # monotonic timestamp

    def feed(self, stripped: str) -> None:
        self.text_buf = (self.text_buf + stripped)[-BUF_MAX:]

    def poll(self, now: float) -> bool:
        """Return True if the approval keystroke should be sent now. Also
        re-arms (and discards stale scrollback) whenever the screen has no
        live prompt, so a previously-answered prompt can't re-trigger us."""
        tail = self.text_buf[-TAIL_WINDOW:]
        if not PROMPT_RE.search(tail):
            # No live prompt on screen: ready for the next one, and drop old
            # scrollback so an answered prompt lingering above can't re-match.
            self.armed = True
            if len(self.text_buf) > TAIL_WINDOW:
                self.text_buf = tail
            return False
        if not self.armed or now < self.cooldown_until:
            return False
        return classify_pending_tool(self.text_buf) is True

    def commit_fired(self, now: float) -> None:
        """Record that we just sent the keystroke: disarm until the prompt
        clears, start the cooldown, and drop everything through the answered
        prompt so its text can't re-match."""
        self.armed = False
        self.cooldown_until = now + COOLDOWN
        tail = self.text_buf[-TAIL_WINDOW:]
        last = None
        for m in PROMPT_RE.finditer(tail):
            last = m
        if last is not None:
            abs_end = len(self.text_buf) - len(tail) + last.end()
            self.text_buf = self.text_buf[abs_end:]


def self_fingerprint() -> tuple[str, str]:
    """Return (short content hash, human mtime) of THIS running script, read
    from disk at call time — i.e. the exact bytes this process loaded. Two
    sessions with the same hash are provably running identical code."""
    try:
        with open(__file__, 'rb') as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()[:8]
    except OSError:
        digest = '????????'
    try:
        mtime = time.strftime('%b %d %H:%M', time.localtime(os.stat(__file__).st_mtime))
    except OSError:
        mtime = '?'
    return digest, mtime


def print_banner() -> None:
    """One dim line identifying the wrapper version, shown before claude
    starts so you always know which build a session is running."""
    digest, mtime = self_fingerprint()
    line = (f"[cw] pty-wrapper {__version__} · {digest} · modified {mtime}"
            f" · auto-approving read-only tools")
    # \x1b[2m = dim; \r\n is safe whether or not the tty is in raw mode yet.
    sys.stdout.write(f"\x1b[2m{line}\x1b[0m\r\n")
    sys.stdout.flush()


def get_terminal_size() -> tuple[int, int]:
    """Get real terminal size via ioctl on stdin."""
    try:
        result = fcntl.ioctl(0, termios.TIOCGWINSZ, b'\x00' * 8)
        rows, cols = struct.unpack('HHHH', result)[:2]
        if rows > 0 and cols > 0:
            return rows, cols
    except Exception:
        pass
    return 24, 80


def set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        winsize = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except Exception:
        pass


def main() -> None:
    print_banner()
    rows, cols = get_terminal_size()
    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd, rows, cols)
    set_winsize(slave_fd, rows, cols)

    pid = os.fork()
    if pid == 0:
        # Child: become claude
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        for fd in (0, 1, 2):
            os.dup2(slave_fd, fd)
        if slave_fd > 2:
            os.close(slave_fd)
        os.execvp('claude', ['claude'] + sys.argv[1:])
        sys.exit(1)

    os.close(slave_fd)

    # Forward SIGWINCH (terminal resize) to child PTY
    def handle_winch(_sig, _frame):
        r, c = get_terminal_size()
        set_winsize(master_fd, r, c)
        try:
            os.kill(pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGWINCH, handle_winch)

    # Save + set raw mode so all keystrokes pass through unmodified
    old_attrs = termios.tcgetattr(sys.stdin)
    tty.setraw(sys.stdin.fileno())

    det = Detector()

    def drain_pending() -> None:
        """Read whatever claude has buffered right now, mirror it, and feed
        the detector — without blocking."""
        while True:
            try:
                r2, _, _ = select.select([master_fd], [], [], 0)
            except (ValueError, select.error):
                return
            if master_fd not in r2:
                return
            try:
                more = os.read(master_fd, 16384)
            except OSError:
                return
            if not more:
                return
            os.write(sys.stdout.fileno(), more)
            det.feed(strip_ansi(more))

    def maybe_approve() -> None:
        """If a safe prompt is showing, settle, re-verify it's STILL there,
        then send the approval keystroke."""
        if not det.poll(time.monotonic()):
            return
        # Let Claude finish painting, then re-read and re-check. Without this
        # we can inject "1" into a prompt that was already dismissed.
        time.sleep(SETTLE)
        drain_pending()
        now = time.monotonic()
        if det.poll(now):
            os.write(master_fd, b'1\r')
            det.commit_fired(now)

    try:
        while True:
            try:
                rfds, _, _ = select.select(
                    [master_fd, sys.stdin.fileno()], [], [], 0.05
                )
            except (ValueError, select.error):
                break

            # Output from claude → our terminal
            if master_fd in rfds:
                try:
                    data = os.read(master_fd, 16384)
                except OSError:
                    break
                os.write(sys.stdout.fileno(), data)
                det.feed(strip_ansi(data))

            # Input from user → claude
            if sys.stdin.fileno() in rfds:
                try:
                    data = os.read(sys.stdin.fileno(), 1024)
                except OSError:
                    break
                try:
                    os.write(master_fd, data)
                except OSError:
                    break

            # Re-check on every loop turn (including the 0.05s timeout) so a
            # prompt that already finished rendering still gets approved.
            maybe_approve()

            # Check if claude exited
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                break

    finally:
        termios.tcsetattr(sys.stdin, termios.TCSAFLUSH, old_attrs)


if __name__ == '__main__':
    main()
