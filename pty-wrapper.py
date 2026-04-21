#!/usr/bin/env python3.13
"""
PTY wrapper for Claude Code CLI.
Runs `claude` interactively but auto-approves read-only tool prompts.

Usage:
  python3.13 ~/.claude/pty-wrapper.py [any claude args...]
  e.g.  python3.13 ~/.claude/pty-wrapper.py
        python3.13 ~/.claude/pty-wrapper.py --model sonnet
"""
import fcntl
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

# Strip ANSI/VT escape sequences (CSI, OSC, two-byte, etc.)
ANSI_RE = re.compile(
    rb'\x1b(?:'
    rb'\[[0-?]*[ -/]*[@-~]'       # CSI sequences
    rb'|\][^\x07]*\x07'           # OSC sequences (terminated by BEL)
    rb'|\][^\x1b]*\x1b\\'        # OSC sequences (terminated by ST)
    rb'|[^[\]].?'                 # two-byte sequences
    rb')'
)

# --- Auto-approve patterns ---
# Non-Bash read-only tools
TOOL_RE = re.compile(
    r'(Read file|Read\s*\(|Glob\s*\(|Grep\s*\(|Glob\b|Grep\b|Read\b'
    r'|ToolSearch\s*\(|ToolSearch\b'
    r'|TaskGet\s*\(|TaskGet\b|TaskList\s*\(|TaskList\b)',
    re.IGNORECASE,
)
# Bash read-only commands. Git intentionally excluded: even read-only git
# subcommands are not in the enterprise auto-approve ruleset.
SAFE_BASH_CMDS = (
    r'ls|find|head|wc|cat|grep|rg|tail|file|stat|du|df|pwd|echo|which'
    r'|whoami|env|printenv|uname|hostname|date|id'
)
# Must match the FIRST command inside Bash(...). Matching after a pipe would
# greenlight pipelines like `Bash(git log ... | head -5)` because `head` is
# safe — even though the command starts with an unapproved `git`.
BASH_CMD_RE = re.compile(
    rf'Bash\s*\(\s*({SAFE_BASH_CMDS})\b',
    re.IGNORECASE,
)
# The approval prompt — require the numbered bullet form Claude Code
# actually renders ("1. Yes"), not just any "Yes". Bounded distance keeps
# it from spanning unrelated output.
PROMPT_RE = re.compile(r'Do you want to proceed\?.{0,400}?\b1\.\s*Yes\b', re.DOTALL)
# Only trust signals inside the trailing slice of visible output — the
# live prompt is always at the bottom of the screen. Without this, prose
# or quoted code higher up (e.g. this script's own patterns discussed in
# chat) can spuriously match.
TAIL_WINDOW = 800


MULTI_SPACE_RE = re.compile(r' {2,}')

def strip_ansi(data: bytes) -> str:
    # Replace ANSI sequences with a space (cursor moves act as spaces)
    text = ANSI_RE.sub(b' ', data).decode('utf-8', errors='ignore')
    # Collapse multiple spaces into one
    return MULTI_SPACE_RE.sub(' ', text)


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

    text_buf = ''       # rolling window of STRIPPED text (not raw bytes)
    cooldown = 0.0      # timestamp: don't auto-approve again until after this time
    BUF_MAX = 16000     # characters of visible text — plenty for tool name + prompt

    try:
        while True:
            try:
                rfds, _, _ = select.select([master_fd, sys.stdin.fileno()], [], [], 0.05)
            except (ValueError, select.error):
                break

            # Output from claude → our terminal
            if master_fd in rfds:
                try:
                    data = os.read(master_fd, 16384)
                except OSError:
                    break
                os.write(sys.stdout.fileno(), data)

                # Buffer stripped text so ANSI bloat doesn't eat our window
                text_buf = (text_buf + strip_ansi(data))[-BUF_MAX:]
                now = time.monotonic()

                tail = text_buf[-TAIL_WINDOW:]
                if now > cooldown and PROMPT_RE.search(tail):
                    is_safe_tool = bool(TOOL_RE.search(tail))
                    is_safe_bash = bool(BASH_CMD_RE.search(tail))
                    if is_safe_tool or is_safe_bash:
                        # Set cooldown immediately: even if re-verify aborts,
                        # we don't want to re-fire on the same stale buffer.
                        cooldown = now + 3.0
                        # Let Claude settle, then drain any new output that
                        # arrived during the pause and re-verify the prompt
                        # is STILL on screen. Without this, auto-dismissed
                        # prompts cause us to inject "1" into the idle
                        # chat input line after the prompt is gone.
                        time.sleep(0.1)
                        try:
                            while True:
                                r2, _, _ = select.select([master_fd], [], [], 0)
                                if master_fd not in r2:
                                    break
                                more = os.read(master_fd, 16384)
                                if not more:
                                    break
                                os.write(sys.stdout.fileno(), more)
                                text_buf = (text_buf + strip_ansi(more))[-BUF_MAX:]
                        except OSError:
                            pass
                        tail = text_buf[-TAIL_WINDOW:]
                        if PROMPT_RE.search(tail):
                            os.write(master_fd, b'1\r')
                            text_buf = ''

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

            # Check if claude exited
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                break

    finally:
        termios.tcsetattr(sys.stdin, termios.TCSAFLUSH, old_attrs)


if __name__ == '__main__':
    main()
