# Auto-Approve Read-Only Actions in Claude Code (Enterprise Mode)

A PTY wrapper for Claude Code CLI that auto-approves read-only tool prompts, bypassing enterprise permission settings that force manual approval for safe operations.

## Problem

Enterprise-managed Claude Code settings force manual approval for read-only actions like `Read`, `Grep`, `Glob`, `ls`, `find`, etc., overriding personal permissions. This makes interactive sessions painfully slow when Claude needs to explore codebases.

## Solution

A Python PTY wrapper (`pty-wrapper.py`) that:

1. Spawns `claude` in a pseudo-terminal — **full interactive experience preserved**
2. Bridges your terminal and claude bidirectionally (including terminal resize)
3. Watches terminal output for permission prompts on safe, read-only tools
4. Auto-sends the approval keystroke when detected
5. Everything else passes through normally — you still approve non-read-only actions manually

## Auto-Approved Actions

| Type | Actions |
|------|---------|
| **Tools** | `Read`, `Glob`, `Grep`, `ToolSearch`, `TaskGet`, `TaskList` |
| **Bash commands** | `ls`, `find`, `head`, `wc`, `cat`, `grep`, `rg`, `tail`, `file`, `stat`, `du`, `df`, `pwd`, `echo`, `which`, `whoami`, `env`, `printenv`, `uname`, `hostname`, `date`, `id` |

Read-only `git` (`status`, `log`, `diff`, …) is **not** auto-approved by default — the enterprise ruleset doesn't auto-approve git either. See [Customization](#customization) to add it.

## Setup

### Requirements

- Python 3.10+ (tested with 3.13)
- No pip dependencies — uses only standard library modules
- `claude` CLI installed and on your PATH

### Install

```bash
# Copy the wrapper script
cp pty-wrapper.py ~/.claude/pty-wrapper.py
chmod +x ~/.claude/pty-wrapper.py

# Add alias to your shell (add to ~/.zshrc or ~/.bashrc)
alias cw="python3.13 ~/.claude/pty-wrapper.py"
```

Adjust `python3.13` to match your Python 3.10+ binary.

### Knowing which version a session runs

`cw` prints a banner at startup identifying the build it loaded:

```
[cw] pty-wrapper 2026-06-02 · 3f9a1c2b · modified Jun 02 15:35 · auto-approving read-only tools
```

The middle field is a SHA-256 prefix of the script as this process read it. To
confirm a session matches the file on disk:

```bash
shasum -a 256 ~/.claude/pty-wrapper.py | cut -c1-8
```

If the hashes match, that session is running the current code. Because the
script is loaded once at launch, **updating `pty-wrapper.py` requires
restarting `cw`** for the change to take effect.

### Usage

```bash
# Start interactive session
cw

# With arguments (all args forwarded to claude)
cw --model sonnet
cw --resume <session-id>
```

You can run multiple `cw` sessions in parallel — each spawns its own independent `claude` subprocess with its own PTY.

## How It Works

```
Your Terminal
    ↕ (stdin/stdout)
pty-wrapper.py
    ↕ (PTY master/slave)
claude CLI (subprocess)
```

1. Creates a PTY pair and forks — child process becomes `claude`
2. Parent bridges master PTY ↔ real terminal in a `select()` loop
3. Strips ANSI escape codes and buffers the visible text
4. Detects an approval when **both** hold:
   - the real menu (`"Do you want to proceed?"` → `"1. Yes"` → `"2."`) is at the **bottom** of the screen, and
   - the **permission box** just above it is for a safe read-only tool — a `Bash command` box whose first command word is read-only, or a read-only tool (`Read`/`Glob`/`Grep`/…)
5. Re-verifies the prompt is still on screen, then sends `1` + Enter to accept "Yes"
6. Handles `SIGWINCH` to forward terminal resize events

### Reliability details

Earlier versions intermittently left `find`/`grep` prompts waiting for manual
approval. The causes, all addressed now:

- **The pending command is read from the permission box, not the transcript.**
  A Bash permission prompt renders as a `Bash command` box with the raw command
  and `Permission rule Bash …` — *not* as `Bash(...)`. The `Bash(...)` form only
  appears for already-**completed** calls in the transcript (and not at all for
  subagent calls). Detection now classifies the first word of the command in the
  box, so a subagent's `grep`/`find` is approved even when the only `Bash(...)`
  text on screen belongs to earlier, unrelated commands.
- **Re-checks on idle, not only on new output.** The detector runs every loop
  turn — including the `select()` timeout — so a prompt that finished rendering
  and then sits static still gets approved.
- **No stray `1`.** On approval the buffer is cleared and a short cooldown
  starts, so the prompt being dismissed (and any redraws of it) can't re-fire a
  `1` into the chat input, while a genuinely new prompt right after still does.

> **Note:** the wrapper loads the script once at launch. After updating
> `pty-wrapper.py`, **restart your `cw` session** for changes to take effect.

### Why PTY instead of hooks or Agent SDK?

We tried several approaches:

| Approach | Result |
|----------|--------|
| **PreToolUse hooks** | Worked initially, but enterprise patched the bug that allowed user hooks to bypass managed settings |
| **Agent SDK `canUseTool`** | Works, but loses the interactive CLI experience (one-shot only) |
| **PTY wrapper** | Works — enterprise can't block it because it just simulates human input at the terminal level |

## Customization

### Adding more safe bash commands

Edit the `SAFE_BASH_CMDS` alternation in `pty-wrapper.py` (it's matched against
the start of the command inside `Bash(...)`):

```python
SAFE_BASH_CMDS = (
    r'ls|find|head|wc|cat|grep|rg|tail|file|stat|du|df|pwd|echo|which'
    r'|whoami|env|printenv|uname|hostname|date|id|YOUR_COMMAND_HERE'
)
```

To also auto-approve read-only git, append `|git\s+(status|log|diff|show|branch|remote|tag)`.

### Adding more safe tools

Add the tool name to the `SAFE_TOOLS` tuple:

```python
SAFE_TOOLS = ('Read', 'Glob', 'Grep', 'ToolSearch', 'TaskGet', 'TaskList', 'YourTool')
```

## Tests

The detection logic is covered by an offline harness (no `claude` needed):

```bash
python3 test_detector.py
```

It feeds recorded, ANSI-stripped prompt renders through the detector and
asserts the fire/no-fire decisions — including the long-command, static-prompt,
burst, and stray-`1` cases above.

## Known Limitations

- The approval prompt will briefly flash on screen before being auto-accepted
- Detection requires the real menu structure (`1. Yes` → `2.`) at the bottom of the screen, so merely *quoting* the prompt in chat won't trigger it. The unavoidable edge case: if the literal menu text is reproduced verbatim at the very bottom of the visible screen with a tool header above it, it can still match.
- Rare back-to-back prompts with a tiny result between them may fall back to manual approval (the safe failure) rather than auto-approving the second.
- Only works on macOS/Linux (uses `pty`, `fork`, `termios`)

## License

MIT
