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
# Non-Bash tools: match tool name
TOOL_RE = re.compile(r'(Read file|Read\(|Glob\(|Grep\()', re.IGNORECASE)
# Bash commands: match raw command text + "Permission rule Bash"
BASH_CMD_RE = re.compile(r'(?:^|\s)(ls|find|head|wc|cat|grep|tail|file|stat|du|df|pwd|echo|which|whoami|env|printenv)\s', re.IGNORECASE | re.MULTILINE)
BASH_PERM_RE = re.compile(r'Permission rule Bash')
# The approval prompt — require the Yes/No options to avoid false positives
# when the script's own code is displayed on screen
PROMPT_RE = re.compile(r'Do you want to proceed\?.*?1\..*?Yes', re.DOTALL)


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

    buf = b''
    cooldown = 0.0  # timestamp: don't auto-approve again until after this time

    try:
        while True:
            try:
                rfds, _, _ = select.select([master_fd, sys.stdin.fileno()], [], [], 0.05)
            except (ValueError, select.error):
                break

            # Output from claude → our terminal
            if master_fd in rfds:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                os.write(sys.stdout.fileno(), data)

                buf = (buf + data)[-4000:]  # keep a rolling window
                now = time.monotonic()

                if now > cooldown:
                    text = strip_ansi(buf)
                    if PROMPT_RE.search(text):
                        is_safe_tool = bool(TOOL_RE.search(text))
                        is_safe_bash = bool(BASH_CMD_RE.search(text) and BASH_PERM_RE.search(text))
                        if is_safe_tool or is_safe_bash:
                            time.sleep(0.1)
                            os.write(master_fd, b'1')
                            time.sleep(0.05)
                            os.write(master_fd, b'\r')
                            buf = b''
                            cooldown = now + 1.0

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
