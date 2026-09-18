#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BO -- a single-file, standard-library-only minimal coding agent

As long as the machine can run Python 3.6+, this script runs without
installing any third-party library. The backend uses an OpenAI-compatible
interface (/v1/chat/completions), so it works with OpenAI, DeepSeek,
Qwen, vLLM, Ollama, LM Studio and any other compatible service.

Usage:
    export OPENAI_BASE_URL=https://api.deepseek.com/v1
    export OPENAI_API_KEY=sk-xxxx
    export MODEL=deepseek-chat
    python BO.py

Options:
    -m, --model NAME    model name (overrides MODEL / OPENAI_MODEL)
    -b, --base-url URL  endpoint URL (overrides OPENAI_BASE_URL)
    -k, --api-key KEY   API key (overrides OPENAI_API_KEY)
    -y, --yes           ask for manual confirmation before each command (without it, commands run directly)
    -r, --root DIR      restrict the file tools (read_file/write_file/search) and the
                        run_command cwd to DIR only (unrestricted by default)
    -s, --max-steps N   maximum number of tool-call rounds per turn (default 50)
    -t, --http-timeout N  timeout in seconds for a single request (default 120)
    -q                  show only the final answer, hide all tool/thinking output
    -v / -vv            show tool results and thinking / full detail
    -C, --no-color      disable colored output
    -l, --log [FILE]    write the full interaction to a log file (default BO.log), no detail on screen
    -L, --no-log        undo a recorded -l/--log and stop writing a log

Parameter memory: explicitly passed connection options (-m / -b / -k / -r / -s / -t / -l) are
                  encrypted into .bo in the current directory and reused when omitted or only
                  partially passed. Interaction and display switches (-y / -q / -v / -C) apply to
                  this run only and are not written there.
                  Precedence: command line > environment variable > .bo > built-in default. Delete .bo to reset.

Interaction: /reset clears the conversation, /help shows help, exit quits
"""

import argparse
import base64
import difflib
import fnmatch
import hashlib
import hmac
import io
import itertools
import json
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:
    import readline  # noqa: F401  enables input history when available, ignored otherwise
except Exception:
    pass

MAX_OUTPUT_CHARS = 30000             # max characters of a single tool result entering the context
MAX_LINE_CHARS = 2000                # max characters displayed per line by read_file
MAX_READ_BYTES = 10 * 1024 * 1024    # read/write size limit to avoid blowing up memory
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # max size of a single HTTP response
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024  # max bytes read per file by search
MAX_SEARCH_RESULTS = 1000            # max result lines returned by search
MAX_EDITS = 50                       # max entries in one write_file edits array
MAX_COMMAND_TIMEOUT = 3600           # upper bound for a run_command timeout (seconds)
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024  # in-memory output cap for run_command (half head, half tail, then trimmed for display)
MAX_DIR_ITEMS = 1000                 # max entries read_file lists for one directory
MAX_REPEAT_CALLS = 3                 # an identical tool call repeated this many times in a row aborts the turn
TRIM_KEEP_TURNS = 1                  # how many recent turns keep their full tool calls/results (earlier ones are removed)
SEARCH_SKIP_DIRS = (".git", "__pycache__", "node_modules", ".venv", "venv",
                    ".tox", ".mypy_cache", ".pytest_cache")  # directories search skips
AGENT_FILE = "AGENTS.md"             # convention file in the current directory (case-insensitive); if present, the model is told to read it
CONFIG_FILE = ".bo"                  # encrypted parameter memory in the current directory; written on explicit options, reused when omitted
# only these connection/run options are written to .bo; interaction and display switches (-y / -q / -v / -C) apply to this run only
CONFIG_KEYS = ("model", "base_url", "api_key", "root", "max_steps", "http_timeout", "log_path")

# Screen verbosity levels
LEVEL_QUIET, LEVEL_NORMAL, LEVEL_VERBOSE, LEVEL_DEBUG = 0, 1, 2, 3
LEVEL_NAMES = ("quiet (final answer only)", "normal (one-line tool call notice)",
               "verbose (tool results and thinking included)", "debug (full detail)")


# ---------------------------------------------------------------------------
# Ctrl+C semantics: the first press interrupts the current turn (generation or
# command), the second one quits the program
# ---------------------------------------------------------------------------

class TurnInterrupted(BaseException):
    """Raised on the first Ctrl+C: end only this turn, back to the prompt, no exit.

    Inherits BaseException so it is not swallowed as an ordinary error by the
    `except Exception` blocks along the way.
    """


# busy means "inside a turn", seen means Ctrl+C was already pressed once during this turn
_INTERRUPT = {"busy": False, "seen": False}


def _handle_sigint(signum, frame):
    if _INTERRUPT["busy"] and not _INTERRUPT["seen"]:
        _INTERRUPT["seen"] = True
        raise TurnInterrupted()     # first press within a turn: interrupt this turn only
    raise KeyboardInterrupt()       # idle, or a second press within a turn: quit the program


def install_sigint_handler():
    """Take over SIGINT; if that is impossible (non-main thread etc.), keep the default behaviour."""
    try:
        signal.signal(signal.SIGINT, _handle_sigint)
    except (ValueError, OSError):
        pass


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function calling format)
# ---------------------------------------------------------------------------

def _p(kind, desc):
    """A single JSON Schema property."""
    return {"type": kind, "description": desc}


def _fn(name, desc, props, required):
    """A tool definition of type function."""
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


def _arr(desc, item_props, item_required):
    """A JSON Schema property of type array of objects."""
    return {"type": "array", "description": desc,
            "items": {"type": "object", "properties": item_props, "required": item_required}}


TOOLS = [
    _fn("read_file", "Read a file with line numbers or list a directory; offset is 1-based and the continuation "
        "offset is reported when the read stops early; binary files are refused. Do not copy the line numbers "
        "into write_file's old_string.",
        {"path": _p("string", "File or directory path (relative or absolute)"),
         "offset": _p("integer", "Starting line number / entry, 1-based, default 1"),
         "limit": _p("integer", "Maximum lines to read (default 2000) / entries to list (default 200)")},
        ["path"]),
    _fn("write_file", "Write a file: pass content to create it or overwrite it entirely (always use this for new "
        "files instead of echo/cat redirection in run_command); pass old_string + new_string or edits for an exact "
        "replacement inside an existing file (use that mode for a local change). Both modes cannot be mixed. "
        "old_string must occur uniquely, otherwise an error lists the candidate positions; use edits to submit "
        "several different changes at once; missing parent directories are created; empty content is refused.",
        {"path": _p("string", "File path"),
         "content": _p("string", "The complete text to write (alternative to old_string/edits, cannot be empty)"),
         "old_string": _p("string", "The original text to replace; must be unique in the file (alternative to content; do not include line numbers)"),
         "new_string": _p("string", "The new text after replacement (alternative to content)"),
         "replace_all": _p("boolean", "Replace every occurrence when true, default false"),
         "edits": _arr("Submit several changes at once, applied in order; nothing is written if any of them fails (alternative to content)",
                       {"old_string": _p("string", "The original text to replace (do not include line numbers)"),
                        "new_string": _p("string", "The new text after replacement"),
                        "replace_all": _p("boolean", "Replace every occurrence, default false")},
                       ["old_string", "new_string"])},
        ["path"]),
    _fn("search", "Search a file or directory with a regular expression (case-sensitive; prefix with (?i) to ignore "
        "case), skipping .git / __pycache__ / node_modules and other such directories as well as binary and huge "
        "files. It saves a lot of output compared with grep through run_command.",
        {"pattern": _p("string", "Regular expression; an invalid one falls back to a literal search"),
         "path": _p("string", "File or directory to search, default . (current directory)"),
         "glob": _p("string", "Only search files matching this pattern (e.g. *.py), default all"),
         "max_results": _p("integer", "Maximum result lines to return, default 100, max 1000 (context lines do not count; the output also has a 30000 character cap)"),
         "context_lines": _p("integer", "Include N lines of context around each match (0-10), default 0")},
        ["pattern"]),
    _fn("run_command", "Execute a command in the shell and return the exit code plus the merged stdout+stderr. "
        "The command's stdin is empty, so do not run interactive programs such as vim / top; long output keeps "
        "the head and the tail with a truncation notice.",
        {"command": _p("string", "The shell command to execute (non-interactive; use nohup ... & for background work)"),
         "cwd": _p("string", "Working directory for the command, default the current one (must stay inside --root)"),
         "timeout": _p("integer", "Timeout in seconds, default 120; on timeout the whole process group is killed")},
        ["command"]),
]


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def _decode_bytes(data):
    """Decode bytes into text; return (text, actual encoding); lossless whenever possible (latin-1 always works)."""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc), enc
        except Exception:
            continue
    return data.decode("utf-8", "replace"), "utf-8"


def _detect_encoding(data):
    """Return the candidate encoding that decodes data losslessly; identical to _decode_bytes' decision."""
    return _decode_bytes(data)[1]


def _count_lines(data):
    """Count lines (split on \\n, a last line without newline still counts); data may be bytes or str."""
    nl = b"\n" if isinstance(data, bytes) else "\n"
    n = data.count(nl)
    return n + 1 if data and not data.endswith(nl) else n


def _decode(data):
    return _decode_bytes(data)[0]


def _split_lines(text):
    """The single text line splitter: breaks on \\n only, drops a trailing \\r, ignores a final empty line.

    Unlike splitlines() it does not treat \\x0b / \\x0c / \\x85 / \\u2028 as line separators, so line
    numbers match the editor and the streaming reader used for huge files.
    """
    lines = text.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def _truncate(text, limit=MAX_OUTPUT_CHARS, note=None):
    """Truncate text; if note contains %d it is replaced with the original length."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    note = note or "\n... [truncated, original was %d characters]"
    try:
        return text[:limit] + note % len(text)
    except TypeError:
        return text[:limit] + note


def _indent(text, prefix="    ", limit=MAX_OUTPUT_CHARS):
    text = _truncate(text or "", limit, note="\n... [display omitted]")
    return "\n".join(prefix + ln for ln in text.splitlines())


def _brief(raw, n=120):
    s = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


def _first_line(text):
    return text.strip().splitlines()[0][:200] if text and text.strip() else "(empty)"


def _int(value, default, minimum=None):
    """Convert a tool argument to int; fall back to the default when invalid or out of range."""
    try:
        n = int(value)
    except Exception:
        return default
    if minimum is not None and n < minimum:
        return default
    return n


def _is_error(result):
    """Decide whether a tool result counts as a failure (at normal level only errors are shown)."""
    head = (result or "").lstrip()
    if head.startswith(("Error", "Command timed out", "User declined")):
        return True
    if head.startswith("Exit code:"):
        try:
            return int(head.split(":", 1)[1].strip().splitlines()[0]) != 0
        except Exception:
            return False
    return False


def _confirm(prompt):
    """Ask the user yes/no; anything other than y/yes counts as declined (EOF/EOT included).

    Ctrl+C is not swallowed here: it goes to _handle_sigint -- the first press in a turn
    interrupts that turn only, the second one quits the program.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        ans = input().strip().lower()
    except EOFError:
        ans = "n"
    return ans in ("y", "yes")


def _truncate_middle(text, limit=MAX_OUTPUT_CHARS):
    """Truncate command output keeping head and tail (errors are usually at the end)."""
    if not text or len(text) <= limit:
        return text
    head = max(1, limit // 3)
    tail = max(0, limit - head - 64)
    note = "\n... [%d characters omitted in the middle] ...\n" % (len(text) - head - tail)
    return text[:head] + note + (text[-tail:] if tail else "")


# ---------------------------------------------------------------------------
# File access (with path restriction and atomic writes)
# ---------------------------------------------------------------------------

def _resolve(path, opts):
    """Validate a path and return (real path, error message). With --root, out-of-range paths are rejected."""
    if not path:
        return None, "Error: missing path argument"
    real = os.path.realpath(path)
    root = opts.get("root")
    if root and real != root and not real.startswith(root + os.sep):
        return None, "Error: path outside the allowed range (--root %s): %s" % (root, path)
    return real, None


def _read_file_bytes(path):
    """Read a whole file with a size limit. Return (bytes, error message)."""
    if not os.path.exists(path):
        return None, "Error: file does not exist: %s" % path
    if not os.path.isfile(path):
        return None, "Error: not a regular file (directory/device file, etc.): %s" % path
    size = os.path.getsize(path)
    if size > MAX_READ_BYTES:
        return None, "Error: file too large (%.1f MB, limit %d MB); use run_command with head/tail/sed instead" % (
            size / 1048576.0, MAX_READ_BYTES // 1048576)
    try:
        with open(path, "rb") as f:
            return f.read(), None
    except Exception as e:
        return None, "Error: read failed: %s" % e


def _binary_error(shown):
    """The standard notice for a binary file."""
    return ("Error: %s looks like a binary file (contains a NUL byte), text reading was skipped. "
            "Use file / xxd / hexdump through run_command to inspect it." % shown)


def _atomic_write(path, data, mode=None):
    """Atomic write: write a temp file in the same directory, fsync, then os.replace.

    With mode=None the original permissions are kept (0644 for a new file), otherwise the given
    permissions are used unconditionally (e.g. 0600 for .bo).
    """
    target = os.path.realpath(path)
    if mode is None:
        try:
            mode = os.stat(target).st_mode & 0o777
        except Exception:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".", prefix=".bo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:  # includes Ctrl+C (BaseException); no temp file may be left behind
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _read_lines_window(path, shown, offset, limit):
    """Read a window of lines, returning (lines, total lines, error); offset is 0-based.

    Small files are read in one go and decoded with _decode_bytes (so GBK and similar work too);
    files above MAX_READ_BYTES are scanned twice instead: first the \\n count is accumulated in
    chunks to get the total, then a TextIOWrapper pulls just the target lines, so the whole content
    never sits in memory.
    """
    if os.path.getsize(path) <= MAX_READ_BYTES:
        raw, err = _read_file_bytes(path)
        if err:
            return None, 0, err
        if b"\x00" in raw[:8192]:
            return None, 0, _binary_error(shown)
        lines = _split_lines(_decode_bytes(raw)[0])
        return lines[offset:offset + limit], len(lines), None

    total, last = 0, b""
    try:
        with open(path, "rb") as f:
            if b"\x00" in f.read(8192):  # binary sniffing shares this single open with the line counting
                return None, 0, _binary_error(shown)
            f.seek(0)
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                total += chunk.count(b"\n")
                last = chunk[-1:]
    except Exception as e:
        return None, 0, "Error: read failed: %s" % e
    if last and last != b"\n":
        total += 1  # the last line has no newline

    out = []
    if offset < total:
        try:
            with open(path, "rb") as f:
                wrap = io.TextIOWrapper(f, encoding="utf-8", errors="replace", newline="")
                for ln in itertools.islice(wrap, offset, offset + limit):
                    out.append(ln.rstrip("\n").rstrip("\r"))
        except Exception as e:
            return None, 0, "Error: read failed: %s" % e
    return out, total, None


def _list_dir(real, shown, offset=0, limit=200):
    """List a directory (used when read_file is given a directory); one level only, with offset/limit paging."""
    try:
        with os.scandir(real) as it:
            entries = sorted(it, key=lambda e: e.name)
    except Exception as e:
        return "Error: cannot read directory: %s" % e

    dirs, files, links, others = [], [], [], []
    for e in entries:
        try:
            if e.is_symlink():
                links.append("%s -> %s" % (e.name, os.readlink(e.path)))
            elif e.is_dir():
                dirs.append(e.name + "/")
            elif e.is_file():
                files.append("%s  (%d bytes)" % (e.name, e.stat().st_size))
            else:
                others.append(e.name)
        except Exception:
            others.append(e.name)

    items = dirs + links + files + others
    if not items:
        return "Directory %s is empty." % shown
    window = items[offset:offset + limit]
    if not window:
        return "Directory %s has %d entries; offset=%d is past the end." % (shown, len(items), offset + 1)
    note = ""
    if offset + len(window) < len(items):
        note = "\n... [%d more entries, continue with offset=%d]" % (
            len(items) - offset - len(window), offset + len(window) + 1)
    return "Directory %s has %d entries (%d dirs / %d files), showing %d-%d:\n%s%s" % (
        shown, len(items), len(dirs), len(files),
        offset + 1, offset + len(window), "\n".join(window), note)


def find_agent_file():
    """Find AGENTS.md in the current directory case-insensitively; return the actual file name or None."""
    want = AGENT_FILE.lower()
    try:
        for name in sorted(os.listdir(".")):
            if name.lower() == want and os.path.isfile(name):
                return name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# .bo parameter memory (simple encryption: directory-path-derived keystream XOR + HMAC-SHA256)
# ---------------------------------------------------------------------------

_CFG_MAGIC = b"BOCFG1"
_CFG_MAX_BYTES = 1 << 20             # .bo size limit, 1MB


def _cfg_keys(path):
    """Derive (stream key, MAC key) from the real path of the directory holding .bo, binding the file to it."""
    base = os.path.dirname(os.path.realpath(path)).encode("utf-8")
    return (hashlib.sha256(b"bo-config-stream-v1|" + base).digest(),
            hashlib.sha256(b"bo-config-mac-v1|" + base).digest())


def _cfg_keystream(key, n):
    """Derive n bytes of keystream from the key (SHA-256 counter mode)."""
    out, i = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(key + i.to_bytes(4, "big")).digest()
        i += 1
    return bytes(out[:n])


def _cfg_xor(data, key):
    return bytes(b ^ k for b, k in zip(bytearray(data), _cfg_keystream(key, len(data))))


def _encrypt_config(raw_bytes, stream_key, mac_key):
    """Plaintext -> base64(magic + HMAC-SHA256 + XOR ciphertext)."""
    body = _cfg_xor(raw_bytes, stream_key)
    mac = hmac.new(mac_key, _CFG_MAGIC + body, hashlib.sha256).digest()
    return base64.b64encode(_CFG_MAGIC + mac + body) + b"\n"


def _decrypt_config(raw, stream_key, mac_key):
    """Decrypt; return None if any step (base64/magic/HMAC/decoding) fails (treated as no valid config)."""
    try:
        blob = base64.b64decode(raw.strip(), validate=True)
    except Exception:
        return None
    head = len(_CFG_MAGIC)
    if len(blob) < head + 32 or not blob.startswith(_CFG_MAGIC):
        return None
    body, mac = blob[head + 32:], blob[head:head + 32]
    expect = hmac.new(mac_key, _CFG_MAGIC + body, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expect):
        return None
    try:
        return _cfg_xor(body, stream_key).decode("utf-8")
    except Exception:
        return None


def load_config(path):
    """Read and decrypt .bo. Return (dict, status) where status is 'missing' / 'ok' / 'invalid'."""
    if not os.path.isfile(path):
        return {}, "missing"
    try:
        with open(path, "rb") as f:
            raw = f.read(_CFG_MAX_BYTES + 1)
    except Exception:
        return {}, "invalid"
    if len(raw) > _CFG_MAX_BYTES:
        return {}, "invalid"
    stream_key, mac_key = _cfg_keys(path)
    text = _decrypt_config(raw, stream_key, mac_key)
    if text is None:
        return {}, "invalid"
    try:
        obj = json.loads(text)
    except Exception:
        return {}, "invalid"
    if not isinstance(obj, dict):
        return {}, "invalid"
    return {k: v for k, v in obj.items() if k in CONFIG_KEYS}, "ok"


def save_config(cfg, path):
    """Encrypt and write .bo (0600, atomic replace). Return an error message, or None on success."""
    stream_key, mac_key = _cfg_keys(path)
    try:
        data = _encrypt_config(
            json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            stream_key, mac_key)
    except Exception as e:
        return "serialization failed: %s" % e
    try:
        _atomic_write(path, data, 0o600)
    except Exception as e:
        return "write failed: %s" % e
    return None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_read_file(args, opts):
    shown = args.get("path")
    path, err = _resolve(shown, opts)
    if err:
        return err
    offset = max(_int(args.get("offset"), 1, minimum=1) - 1, 0)  # 1-based externally, 0-based internally
    if os.path.isdir(path):
        return _list_dir(path, shown, offset,
                         min(_int(args.get("limit"), 200, minimum=1), MAX_DIR_ITEMS))
    if not os.path.exists(path):
        return "Error: file does not exist: %s" % shown
    if not os.path.isfile(path):
        return "Error: not a regular file (device file, etc.): %s" % shown

    limit = _int(args.get("limit"), 2000, minimum=1)
    lines, total, err = _read_lines_window(path, shown, offset, limit)
    if err:
        return err
    if total == 0:
        return "File %s is empty." % shown
    if offset >= total:
        return "File %s has %d lines; offset=%d is past the end of the file." % (shown, total, offset + 1)

    # assemble line by line while counting characters: even a huge limit stays inside the context budget
    shown_lines, used, cut = [], 0, False
    for i, ln in enumerate(lines):
        text = _truncate(ln, MAX_LINE_CHARS, note="... [this line was truncated]")
        cost = len(text) + 8  # rough cost of the line number and the newline
        if shown_lines and used + cost > MAX_OUTPUT_CHARS:
            cut = True
            break
        shown_lines.append("%6d\t%s" % (offset + i + 1, text))
        used += cost
    end = offset + len(shown_lines)
    header = "File %s has %d lines, showing lines %d-%d:" % (shown, total, offset + 1, end)
    if end >= total:
        footer = "\n[end of file]"
    elif cut:
        footer = "\n[output reached the %d character limit, shown up to line %d; continue with offset=%d]" % (
            MAX_OUTPUT_CHARS, end, end + 1)
    else:
        footer = "\n[%d more lines, continue with offset=%d]" % (total - end, end + 1)
    return header + "\n" + "\n".join(shown_lines) + footer


def tool_write_file(args, opts):
    """Write a file: pick the whole-file or the exact-replacement mode from the arguments."""
    shown = args.get("path")
    path, err = _resolve(shown, opts)
    if err:
        return err

    has_content = args.get("content") is not None
    has_edit = (args.get("edits") is not None or args.get("old_string") is not None
                or args.get("new_string") is not None)
    if has_content and has_edit:
        return ("Error: content cannot be combined with old_string/new_string/edits. "
                "For a whole-file write pass content only; for a local replacement pass old_string + new_string "
                "or edits.")
    if has_edit:
        return _edit_existing_file(path, shown, args)
    if not has_content:
        hint = ""
        if args.get("replace_all") is not None:
            hint = " (replace_all only works together with old_string/new_string/edits and had no effect here)"
        return ("Error: missing arguments. A whole-file write needs content; a local replacement needs "
                "old_string + new_string, or edits to submit several changes at once." + hint)
    result = _write_whole_file(path, shown, args)
    if args.get("replace_all") is not None and result.startswith(("Created", "Overwrote")):
        result += ("\nNote: replace_all only applies to a local replacement (old_string/new_string or edits); "
                   "this whole-file write ignored it.")
    return result


def _write_whole_file(path, shown, args):
    """Mode one: write the whole file (create or overwrite)."""
    content = args.get("content")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    if content == "":
        return ("Error: content is empty and would clear the file, write refused. Use touch through run_command "
                "for a new empty file; to really empty an existing file run `: > file` explicitly.")
    if os.path.isdir(path):
        return "Error: %s is a directory, cannot write a file there" % shown

    existed = os.path.isfile(path)
    enc, old_lines, old_bytes = "utf-8", 0, 0
    if existed:
        if os.path.getsize(path) > MAX_READ_BYTES:
            return "Error: target file too large (> %d MB), refusing to overwrite it entirely; use old_string/new_string for a local change" % (
                MAX_READ_BYTES // 1048576)
        raw, err = _read_file_bytes(path)
        if err:
            return err
        old_bytes = len(raw)
        old_lines = _count_lines(raw)
        enc = _detect_encoding(raw)
    enc_note = ""
    try:
        data = content.encode(enc)
    except Exception:
        enc_note = " (the original encoding %s cannot represent the new content, saved as utf-8)" % enc
        enc, data = "utf-8", content.encode("utf-8")
    if len(data) > MAX_READ_BYTES:
        return "Error: content too large (%.1f MB, limit %d MB)" % (
            len(data) / 1048576.0, MAX_READ_BYTES // 1048576)

    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent)
        except Exception as e:
            return "Error: cannot create directory %s: %s" % (parent, e)
    try:
        _atomic_write(path, data)
    except Exception as e:
        return "Error: write failed: %s" % e
    new_lines, new_bytes = _count_lines(content), len(data)
    if not existed:
        return "Created %s (%d lines, %d bytes, encoding %s)%s" % (
            shown, new_lines, new_bytes, enc, enc_note)
    result = "Overwrote %s (%d lines/%d bytes -> %d lines/%d bytes, encoding %s)%s" % (
        shown, old_lines, old_bytes, new_lines, new_bytes, enc, enc_note)
    if old_bytes and new_bytes < old_bytes * 0.5 and new_lines < old_lines:
        result += "\nNote: the new content is %.0f%% smaller than the original; if you only wanted a local change, use old_string/new_string instead." % (
            (1 - new_bytes / float(old_bytes)) * 100)
    return result


def _line_of(text, idx):
    """Character offset -> 1-based line number."""
    return text.count("\n", 0, idx) + 1


def _line_text(text, idx):
    """The source line containing the given offset."""
    a = text.rfind("\n", 0, idx) + 1
    b = text.find("\n", idx)
    return text[a:b if b >= 0 else len(text)]


def _locate_fuzzy(text, old):
    """Locate old ignoring trailing-whitespace and CRLF differences; return the spans in the original text.

    Compared line by line: a single-line old is searched as a substring inside each line; a multi-line
    old requires the first line to end with first, the middle lines to match exactly and the last line
    to start with last, matching the "ignore trailing whitespace/CRLF" semantics.
    """
    t_lines = text.split("\n")
    o_lines = old.split("\n")
    t_trim = [ln.rstrip() for ln in t_lines]
    o_trim = [ln.rstrip() for ln in o_lines]
    if not any(o_trim):
        return []
    starts, acc = [], 0
    for ln in t_lines:
        starts.append(acc)
        acc += len(ln) + 1
    if len(o_trim) == 1:
        needle = o_trim[0]
        if not needle:
            return []
        spans = []
        for i, ln in enumerate(t_trim):
            k = ln.find(needle)
            while k >= 0:
                spans.append((starts[i] + k, starts[i] + k + len(needle)))
                k = ln.find(needle, k + 1)
        return spans
    first, last = o_trim[0], o_trim[-1]
    mid, n = o_trim[1:-1], len(o_trim)
    spans = []
    for i in range(len(t_trim) - n + 1):
        if not t_trim[i].endswith(first):
            continue
        if mid and t_trim[i + 1:i + n - 1] != mid:
            continue
        if not t_trim[i + n - 1].startswith(last):
            continue
        spans.append((starts[i] + len(t_trim[i]) - len(first),
                      starts[i + n - 1] + len(last)))
    return spans


def _locate(text, old):
    """Locate old in text, returning (spans, note).

    An exact match is tried first; when that finds nothing, the search is repeated ignoring
    trailing-whitespace / CRLF differences.
    """
    if not old:
        return [], ""
    spans, start = [], 0
    while True:
        i = text.find(old, start)
        if i < 0:
            break
        spans.append((i, i + len(old)))
        start = i + 1  # overlapping matches are allowed, so "appears more than once" is never missed (aa inside aaa)
    if spans:
        return spans, ""
    spans = _locate_fuzzy(text, old)
    return spans, ", trailing-whitespace/CRLF differences ignored" if spans else ""


def _probe_fragments(old, limit=3):
    """Pick fragments of old used for approximate location: whole lines first, then longer words."""
    frags = [s for s in (ln.strip() for ln in old.split("\n")) if len(s) >= 3]
    if not frags:
        frags = [t for t in re.split(r"[^0-9A-Za-z_]+", old) if len(t) >= 3]
    if len(frags) > limit:  # keep the first and the last line for a multi-line old
        frags = [frags[0], frags[-1]]
    return frags[:limit]


def _candidates(text, old, spans, limit=5):
    """Build a location report: the positions already matched, or the closest lines."""
    if spans:
        out = []
        for a, _ in spans[:limit]:
            out.append("  line %d: %s" % (
                _line_of(text, a), _truncate(_line_text(text, a).strip(), 160)))
        if len(spans) > limit:
            out.append("  ... %d occurrences in total" % len(spans))
        return "Candidate positions:\n" + "\n".join(out)

    # not found: scan with fragments of old through str.find (C level, orders of magnitude faster than scoring every line)
    lines = text.split("\n")
    hits, seen = [], set()
    for needle in _probe_fragments(old):
        start = 0
        while len(hits) < limit:
            i = text.find(needle, start)
            if i < 0:
                break
            ln = _line_of(text, i)  # 1-based line number
            if ln not in seen:
                seen.add(ln)
                hits.append(ln)
            start = i + 1
        if len(hits) >= limit:
            break
    if not hits:
        return ("No similar content found (%d lines in the file). Use read_file to check the original text "
                "(watch the spaces and indentation, and do not copy the line numbers)." % len(lines))
    return "Closest lines:\n" + "\n".join(
        "  line %d: %s" % (i, _truncate(lines[i - 1].strip(), 160)) for i in hits)


def _diff_block(old_block, new_block, context=1, limit=24):
    """Build a small unified diff (file headers dropped, truncated when needed)."""
    a, b = old_block.splitlines(), new_block.splitlines()
    lines = [ln for ln in difflib.unified_diff(a, b, lineterm="", n=context)
             if not ln.startswith("--- ") and not ln.startswith("+++ ")]
    if len(lines) > limit:
        lines = lines[:limit] + ["... [diff truncated]"]
    return "\n".join(lines)


def _apply_edit(text, edit):
    """Apply one change; returns (new text, replacements, diff, note, error)."""
    old, new = edit.get("old_string"), edit.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return text, 0, "", "", "Error: every change needs a string old_string and new_string"
    if not old:
        return text, 0, "", "", "Error: old_string cannot be empty"
    if old == new:
        return text, 0, "", "", "Error: old_string and new_string are identical, nothing to change"

    replace_all = bool(edit.get("replace_all"))
    spans, note = _locate(text, old)
    if not spans:
        return text, 0, "", "", "Error: old_string not found.\n" + _candidates(text, old, [])
    if len(spans) > 1 and not replace_all:
        return text, 0, "", "", (
            "Error: old_string occurs %d times in the file, not unique. Add more context to make it unique, "
            "or set replace_all=true to replace every occurrence together with its context.\n%s" % (
                len(spans), _candidates(text, old, spans)))

    if replace_all:
        # replacing overlapping matches together would corrupt them, so only non-overlapping positions are kept
        use, last_end = [], -1
        for a, b in spans:
            if a >= last_end:
                use.append((a, b))
                last_end = b
    else:
        use = spans[:1]
    if replace_all and not note:
        out = text.replace(old, new)  # an exact match goes to str.replace (C level, one pass)
    else:
        pieces, cur = [], 0
        for a, b in use:
            pieces.append(text[cur:a])
            pieces.append(new)
            cur = b
        pieces.append(text[cur:])
        out = "".join(pieces)
    diff = _diff_block(text[use[0][0]:use[0][1]], new)
    if len(use) > 1:
        diff += "\n(%d occurrences replaced, the diff shows the first one only)" % len(use)
    return out, len(use), diff, note, ""


def _edit_existing_file(path, shown, args):
    """Mode two: exact string replacement inside an existing file (old_string/new_string or edits)."""
    edits = args.get("edits")
    if edits is None:
        if args.get("old_string") is None or args.get("new_string") is None:
            return "Error: path / old_string / new_string are required, or use edits to submit several changes"
        edits = [{"old_string": args.get("old_string"),
                  "new_string": args.get("new_string"),
                  "replace_all": args.get("replace_all")}]
    if not isinstance(edits, list) or not edits:
        return "Error: edits must be an array with at least one change"
    if len(edits) > MAX_EDITS:
        return "Error: at most %d edits at a time, please submit them in batches" % MAX_EDITS

    if not os.path.isfile(path):
        return "Error: file does not exist or is not a regular file: %s (pass content to create it)" % shown
    raw, err = _read_file_bytes(path)
    if err:
        return err
    text, enc = _decode_bytes(raw)

    diffs, total, tolerant = [], 0, False
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            return "Error: edits item %d is not an object" % i
        text, n, diff, note, err = _apply_edit(text, edit)
        if err:
            head = "Error: edits item %d failed: " % i if len(edits) > 1 else ""
            return head + err + ("\n(nothing was written)" if len(edits) > 1 else "")
        total += n
        tolerant = tolerant or bool(note)
        if diff:
            diffs.append(diff)

    try:
        data = text.encode(enc)
    except Exception as e:
        return "Error: the new content cannot be saved with the original encoding (%s): %s" % (enc, e)
    try:
        _atomic_write(path, data)
    except Exception as e:
        return "Error: write failed: %s" % e

    head = "Modified %s (%d replacement(s)%s)" % (
        shown, total, ", trailing-whitespace/CRLF differences ignored" if tolerant else "")
    return head + ("\n" + "\n".join(diffs) if diffs else "")


def _iter_search_files(root_path, glob_pat):
    """List the files to search, skipping noisy directories and non-matching globs."""
    if os.path.isfile(root_path):
        return [root_path]
    rx = None
    if glob_pat and glob_pat != "*":
        rx = re.compile(fnmatch.translate(glob_pat))  # precompiled, so it is not translated once per file
    files = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = sorted(d for d in dirnames if d not in SEARCH_SKIP_DIRS)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if rx is not None and not (rx.match(name)
                                       or rx.match(os.path.relpath(full, root_path))):
                continue
            files.append(full)
    return files


def _search_prefilter(pattern, flags):
    """Build the regex used for the whole-file prefilter; return None when unreliable (fall back to per-line).

    MULTILINE only relaxes ^ and $, so whenever a line matches, the whole-file search matches too, except:
      - the pattern contains \\A / \\Z / (?-m: those anchors mean different things per file and per line;
      - the pattern contains $ and the body contains \\r (a CRLF file): where $ lands depends on the split.
    The second case is left to the caller, which inspects the body; here only the first kind is excluded.
    """
    if "\\A" in pattern or "\\Z" in pattern or "(?-m" in pattern:
        return None
    try:
        return re.compile(pattern, flags | re.MULTILINE)
    except re.error:
        return None


def tool_search(args, opts):
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return "Error: missing pattern argument (the regular expression to search for)"
    literal = False
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
        literal = True

    shown = args.get("path") or "."
    target, err = _resolve(shown, opts)
    if err:
        return err
    if not os.path.exists(target):
        return "Error: path does not exist: %s" % shown

    glob_pat = args.get("glob") or "*"
    limit = min(_int(args.get("max_results"), 100, minimum=1), MAX_SEARCH_RESULTS)
    context = min(_int(args.get("context_lines"), 0, minimum=0), 10)
    is_dir = os.path.isdir(target)
    files = _iter_search_files(target, glob_pat)
    if not files:
        return "No files matching glob=%s under %s" % (shown, glob_pat)

    pre = _search_prefilter(rx.pattern, rx.flags)  # whole-file prefilter; None means it does not apply
    crlf_anchored = "$" in rx.pattern            # with $ in the pattern, a CRLF body cannot use the prefilter
    hits, scanned, skipped = [], 0, 0
    matched, used, full = 0, 0, False            # used: characters already accumulated for the output
    for full_path in files:
        if matched >= limit or full:
            break
        try:
            if os.path.getsize(full_path) > MAX_SEARCH_FILE_BYTES:
                skipped += 1
                continue
            with open(full_path, "rb") as f:
                raw = f.read()
        except Exception:
            continue
        if b"\x00" in raw[:8192]:  # skip binary files
            continue
        body = _decode_bytes(raw)[0]
        scanned += 1
        # whole-file prefilter: a miss skips splitlines and the per-line regex, avoiding wasted scans in big trees
        if pre is not None and not (crlf_anchored and "\r" in body):
            if not pre.search(body):
                continue
        lines = _split_lines(body)
        label = os.path.relpath(full_path, target) if is_dir else shown
        emitted = -1  # 0-based line already emitted, so context lines of nearby matches are not repeated
        for i, ln in enumerate(lines):
            if matched >= limit or full:
                break
            if not rx.search(ln):
                continue
            matched += 1
            lo = max(0, i - context)
            hi = min(len(lines), i + context + 1)
            for k in range(max(lo, emitted + 1), hi):
                sep = ":" if k == i else "-"
                line = "%s%s%d%s%s" % (
                    label, sep, k + 1, sep, _truncate(lines[k], MAX_LINE_CHARS, note="... [this line was truncated]"))
                if used + len(line) + 1 > MAX_OUTPUT_CHARS:
                    full = True
                    break
                hits.append(line)
                used += len(line) + 1
            emitted = max(emitted, hi - 1)

    if not matched:
        return "No match for %s under %s (%d files scanned)%s" % (
            shown, pattern, scanned,
            " (the pattern is not a valid regex, searched literally)" if literal else "")
    tail = ""
    if matched >= limit:
        tail += "\n[reached the limit of %d matches; narrow path/glob or raise max_results]" % limit
    if full:
        tail += "\n[output reached the %d character limit; lower context_lines or narrow the search]" % MAX_OUTPUT_CHARS
    if skipped:
        tail += "\n[skipped %d files larger than %d MB]" % (skipped, MAX_SEARCH_FILE_BYTES // 1048576)
    if literal:
        tail += "\n[the pattern is not a valid regex, searched literally]"
    return "Search %s (under %s, %d files scanned, %d matches, %d lines of output):\n%s%s" % (
        pattern, shown, scanned, matched, len(hits), _truncate("\n".join(hits)), tail)


def _terminate_group(proc):
    """Kill the whole process group to clean up grandchildren spawned by the command; fall back to the single process."""
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _collect_process_output(stream, box, half):
    """Reader thread: drop the middle while reading, keeping head and tail, so huge output cannot blow up memory."""
    head, tail, total, head_len = [], b"", 0, 0
    while True:
        try:
            chunk = stream.read(65536)
        except Exception:
            break
        if not chunk:
            break
        total += len(chunk)
        if head_len < half:
            take = chunk[:half - head_len]
            head.append(take)
            head_len += len(take)
            chunk = chunk[len(take):]
        if chunk:
            tail = (tail + chunk)[-half:]
    box["head"], box["tail"], box["total"] = b"".join(head), tail, total


def _command_output_text(box):
    """Turn the collected output back into text; return (text, note)."""
    head, tail = box.get("head", b""), box.get("tail", b"")
    total = box.get("total", 0)
    if total > MAX_COMMAND_OUTPUT_BYTES:
        omitted = max(0, total - len(head) - len(tail))
        note = "the output was %.1f KB, about %d bytes in the middle were omitted (redirect to a file and read it back for the full content)" % (
            total / 1024.0, omitted)
        return (head.decode("utf-8", "replace")
                + "\n... [about %d bytes omitted in the middle] ...\n" % omitted
                + tail.decode("utf-8", "replace")), note
    return _decode_bytes(head + tail)[0], ""


def tool_run_command(args, opts):
    command = args.get("command")
    if not command:
        return "Error: missing command argument"
    timeout = min(_int(args.get("timeout"), 120, minimum=1), MAX_COMMAND_TIMEOUT)

    cwd = opts["cwd"]
    if args.get("cwd"):
        cwd, err = _resolve(args.get("cwd"), opts)
        if err:
            return err
        if not os.path.isdir(cwd):
            return "Error: cwd is not a directory: %s" % args.get("cwd")
    elif opts.get("root"):
        # the default cwd (startup directory) must respect --root too, otherwise "inside DIR only" means nothing
        cwd, err = _resolve(cwd, opts)
        if err:
            return ("Error: the default working directory %s is outside the allowed range (--root %s). "
                    "cd into that directory before starting, or pass cwd explicitly." % (opts["cwd"], opts["root"]))

    if opts["confirm"] and not _confirm("\n[Command to run]%s %s\nExecute? [y/N] " % (
            " (cwd=%s)" % cwd if cwd != opts["cwd"] else "", command)):
        return "User declined to run the command."

    popen_kwargs = {"shell": True, "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT, "cwd": cwd}
    if hasattr(os, "killpg"):
        # make the child its own process group leader so the whole group can be killed on timeout
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except Exception as e:
        return "Error: cannot start the command: %s" % e

    # output goes to a background thread that keeps only a bounded head/tail: however much the command prints stays bounded
    box = {}
    reader = threading.Thread(
        target=_collect_process_output,
        args=(proc.stdout, box, max(1, MAX_COMMAND_OUTPUT_BYTES // 2)))
    reader.daemon = True
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except (TurnInterrupted, KeyboardInterrupt):
        _terminate_group(proc)      # on any Ctrl+C (interrupt or quit) leave no spawned process behind
        raise
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(proc)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
    reader.join(10)
    try:
        proc.stdout.close()
    except Exception:
        pass

    text, note = _command_output_text(box)
    header = (note + "\n") if note else ""
    body = _truncate_middle(text)
    if not body.strip():
        body = "(no output)"
    if timed_out:
        return "Command timed out (> %ds) and was terminated (including spawned processes). Output produced so far:\n%s%s" % (
            timeout, header, body)
    return "Exit code: %d\n%sOutput:\n%s" % (proc.returncode, header, body)


TOOL_FUNCS = {"read_file": tool_read_file, "write_file": tool_write_file,
              "search": tool_search, "run_command": tool_run_command}


def execute_tool(name, args, opts):
    func = TOOL_FUNCS.get(name)
    if func is None:
        return "Error: unknown tool %s" % name
    return func(args, opts)


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

def _merge_tool_call_delta(acc, delta):
    """Merge one streamed tool_calls fragment into the accumulated list.

    A fragment may carry only part of index/id/function.name/function.arguments,
    so pieces are merged by their index.
    """
    for piece in delta or []:
        idx = piece.get("index")
        if idx is None:
            idx = len(acc)
        while len(acc) <= idx:
            acc.append({"id": None, "type": "function",
                        "function": {"name": "", "arguments": ""}})
        slot = acc[idx]
        if piece.get("id"):
            slot["id"] = piece["id"]
        if piece.get("type"):
            slot["type"] = piece["type"]
        fn = piece.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]


def call_llm(messages, opts, on_delta=None):
    """Stream /chat/completions and return the aggregated message dict.

    on_delta(kind, text): kind is "content" or "reasoning", text is the new piece.
    If the server ignores stream=true (no data: lines), falls back to parsing
    the full JSON response at once.
    """
    payload = {"model": opts["model"], "messages": messages,
               "tools": TOOLS, "tool_choice": "auto", "stream": True}
    url = opts["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    if opts["api_key"]:
        req.add_header("Authorization", "Bearer " + opts["api_key"])

    content_parts, reasoning_parts, tool_calls = [], [], []
    raw_lines, total = [], 0
    saw_sse = False

    try:
        resp = urllib.request.urlopen(req, timeout=opts["http_timeout"])
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s error: %s" % (e.code, _truncate(_decode(e.read()), 2000)))
    except urllib.error.URLError as e:
        raise RuntimeError("Network error: %s" % e.reason)

    try:
        for raw in resp:
            total += len(raw)
            if total > MAX_RESPONSE_BYTES:
                raise RuntimeError("Response too large (> %d MB), rejected" % (MAX_RESPONSE_BYTES // 1048576))
            line = _decode(raw).strip()
            if not line:
                continue
            if not line.startswith("data:"):
                if not saw_sse:
                    raw_lines.append(_decode(raw))
                continue
            saw_sse = True
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if isinstance(chunk, dict) and chunk.get("error"):
                raise RuntimeError("API returned an error: %s" % chunk["error"])
            try:
                delta = chunk["choices"][0].get("delta") or {}
            except (KeyError, IndexError, TypeError):
                continue
            reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
            if reasoning:
                reasoning_parts.append(reasoning)
                if on_delta:
                    on_delta("reasoning", reasoning)
            text = delta.get("content") or ""
            if text:
                content_parts.append(text)
                if on_delta:
                    on_delta("content", text)
            if delta.get("tool_calls"):
                _merge_tool_call_delta(tool_calls, delta["tool_calls"])
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError("Failed to read streaming response: %s" % e)
    finally:
        resp.close()

    if not saw_sse:
        # Server did not return SSE (some compatible backends ignore stream=true),
        # fall back to parsing the whole body as JSON.
        return _parse_full_response("".join(raw_lines))

    msg = {"role": "assistant", "content": "".join(content_parts)}
    reasoning = "".join(reasoning_parts)
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _parse_full_response(text):
    """Non-streaming fallback: parse a complete JSON response and return message."""
    try:
        obj = json.loads(text)
    except Exception as e:
        raise RuntimeError("Response is not valid JSON: %s" % e)
    if isinstance(obj, dict) and obj.get("error"):
        raise RuntimeError("API returned an error: %s" % obj["error"])
    try:
        return obj["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Unexpected response structure: %s" % _truncate(json.dumps(obj, ensure_ascii=False), 2000))


# ---------------------------------------------------------------------------
# Screen output and logging
# ---------------------------------------------------------------------------

SESSION_MARK = "] SESSION BEGIN "


def _count_sessions(path):
    """Count existing sessions in the log so this session can be numbered."""
    mark = SESSION_MARK.encode("utf-8")
    tail_len = len(mark) - 1
    count, tail = 0, b""
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                data = tail + chunk
                count += data.count(mark)
                tail = data[-tail_len:]
    except Exception:
        return 1
    return count + 1


class Output(object):
    """Tiered screen output + full log to disk."""

    RESET, DIM, RED, CYAN = "\033[0m", "\033[2m", "\033[31m", "\033[36m"

    def __init__(self, level, color, log_path):
        self.level, self.color, self.log_path = level, color, log_path
        self._log = None
        self._stream_open = None
        self.session_id = 0
        if log_path:
            self.session_id = _count_sessions(log_path)
            try:
                # the log may contain command output or even secrets; create it 0600, readable by the owner only
                fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                self._log = os.fdopen(fd, "a", encoding="utf-8")
            except Exception as e:
                sys.stderr.write("Cannot open log file %s: %s\n" % (log_path, e))
                self.log_path = None

    def _c(self, text, code):
        return text if not self.color else code + text + self.RESET

    def _w(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def close(self):
        if self._log is not None:
            try:
                self._log.close()
            except Exception:
                pass
            self._log = None

    # logging is independent of the display level and always records the full content;
    # each record ends with an END marker so it can be located inside long content
    def log(self, kind, text):
        if self._log is None:
            return
        self._log.write("\n===== [%s] %s =====\n%s\n===== END %s =====\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), kind, text if text else "(empty)", kind))
        self._log.flush()

    def assistant(self, text, is_final):
        if text and (self.level > LEVEL_QUIET or is_final):
            self._w("\n" + text + "\n")

    # --- streaming incremental output ---
    def stream_begin(self, kind):
        """Called before a streamed segment starts; handles the newline and color prefix."""
        if kind == "reasoning":
            if self.level >= LEVEL_VERBOSE:
                self._stream_open = self.DIM
            else:
                self._stream_open = None
        else:
            # content: at quiet level do not print live, leave it to the final answer
            self._stream_open = None if self.level == LEVEL_QUIET else ""
        if self._stream_open is not None:
            self._w("\n" + self._stream_open)

    def stream_delta(self, text):
        if self._stream_open is None:
            return
        sys.stdout.write(text)
        sys.stdout.flush()

    def stream_end(self):
        if self._stream_open is None:
            return
        self._w((self.RESET if self._stream_open else "") + "\n")
        self._stream_open = None

    def reasoning(self, text):
        if text and self.level >= LEVEL_VERBOSE:
            limit = MAX_OUTPUT_CHARS if self.level >= LEVEL_DEBUG else 800
            self._w(self._c(_indent(text, prefix="  ", limit=limit), self.DIM) + "\n")

    def tool_call(self, name, raw_args):
        if self.level >= LEVEL_NORMAL:
            self._w(self._c("  · %s(%s)\n" % (name, _brief(raw_args)), self.DIM))

    def tool_result(self, result, is_error):
        if self.level >= LEVEL_VERBOSE:
            limit = MAX_OUTPUT_CHARS if self.level >= LEVEL_DEBUG else 3000
            self._w(self._c(_indent(result, limit=limit), self.CYAN) + "\n")
        elif self.level >= LEVEL_NORMAL and is_error:
            self._w(self._c("    ! " + _first_line(result), self.RED) + "\n")

    def info(self, text):
        self._w(text + "\n")

    def error(self, text):
        self._w(self._c("\n[error] " + text, self.RED) + "\n")


def tools_hint():
    """Build the tool list for the system prompt from TOOLS, so a new tool needs one edit only."""
    lines = []
    for t in TOOLS:
        f = t["function"]
        lines.append("- %s: %s" % (f["name"], f["description"].split(". ")[0]))
    return "\n".join(lines)


def build_system_prompt(opts):
    prompt = (
        "You are BO, a coding agent running on the user's own machine.\n"
        "Working directory: %s\n"
        "System: %s\n"
        "\n"
        "Available tools:\n%s\n"
        "\n"
        "Approach: locate with search, read the relevant part with read_file, then use write_file "
        "(content for a whole-file write, old_string + new_string for a local change), and finally verify with "
        "run_command (tests/build/scripts); never guess.\n"
        "Always create new files with write_file's content mode, never by echo/cat redirection in run_command; "
        "delete or move files with run_command's rm / mv, and confirm with the user before anything risky.\n"
        "\n"
        "Output: this is a plain text terminal, Markdown is not rendered. Do not use bold, headings, code fences or "
        "similar syntax. Answer in English, concisely and directly.\n"
    ) % (os.getcwd(), platform.platform(), tools_hint())

    # when AGENTS.md exists just mention the convention; its content is never injected into the prompt,
    # the model reads it itself with read_file
    agent = find_agent_file()
    if agent:
        prompt += (
            "\nConvention: %s in the current directory is this project's long-term convention/memory file. "
            "read_file it before starting work and follow what it says; when information needs to be kept "
            "across sessions, update it with write_file.\n"
        ) % agent
    return prompt


# ---------------------------------------------------------------------------
# Main conversation loop
# ---------------------------------------------------------------------------

def _trim_history(messages, keep_turns=TRIM_KEEP_TURNS):
    """Drop tool calls and tool results of earlier turns, keeping the last keep_turns turns complete.

    A turn boundary is a message with role == "user" (system messages are always kept). What is
    removed belongs to the turn before last and earlier:
      - messages with role == "tool";
      - the tool_calls field of assistant messages (the message goes away when its content is empty).
    User input and assistant prose stay, so the conversation thread survives and only the tool
    round trips are dropped. Returns the number of messages removed.
    """
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(starts) <= keep_turns:
        return 0
    cut = starts[len(starts) - keep_turns]  # index of the first user message to keep complete
    kept = []
    for i, m in enumerate(messages):
        if i >= cut:
            kept.append(m)
            continue
        role = m.get("role")
        if role == "tool":
            continue
        if role == "assistant" and m.get("tool_calls"):
            m = {k: v for k, v in m.items() if k != "tool_calls"}
            if not (m.get("content") or "").strip():
                continue  # an assistant message with tool calls only and no prose is dropped entirely
        kept.append(m)
    if len(kept) == len(messages):
        return 0
    # dropping the pure tool round trips can leave two user messages in a row; merge them to keep the roles alternating
    merged = []
    for m in kept:
        if merged and merged[-1].get("role") == "user" and m.get("role") == "user":
            merged[-1] = {"role": "user",
                          "content": (merged[-1].get("content") or "") + "\n\n" + (m.get("content") or "")}
            continue
        merged.append(m)
    dropped = len(messages) - len(merged)
    messages[:] = merged
    return dropped


def _close_dangling_tool_calls(messages, note):
    """Append a result for the batch of tool_calls that has none, keeping the history valid.

    An interrupt/exception outside the tool loop leaves an assistant tool_calls message in the
    history with nobody answering it; the next request would be rejected as malformed, so the
    missing results are filled in here. Returns how many were added.
    """
    answered = set(m.get("tool_call_id") for m in messages if m.get("role") == "tool")
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending = [tc for tc in m["tool_calls"] if tc.get("id") not in answered]
            for tc in pending:
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": note})
            return len(pending)
    return 0


def run_turn(user_text, messages, opts, out):
    out.log("USER", user_text)
    # the first Ctrl+C within a turn interrupts this turn only (see _handle_sigint); main resets busy at the end
    _INTERRUPT["busy"], _INTERRUPT["seen"] = True, False
    # trim before every turn: only the last keep_turns turns keep their tool round trips, earlier ones are removed
    dropped = _trim_history(messages)
    if dropped:
        out.log("TRIM", "removed tool calls and results of earlier turns: %d messages" % dropped)
    messages.append({"role": "user", "content": user_text})
    repeat = {"key": None, "n": 0}  # guard against repeated identical tool calls

    for _ in range(opts["max_steps"]):
        # streaming callback: print as pieces arrive; content always live, reasoning only at verbose
        stream_gap = {"content": False}
        cur_kind = {"v": None}

        def on_delta(kind, text):
            if kind == "reasoning" and out.level < LEVEL_VERBOSE:
                return
            if kind == "content" and stream_gap["content"] and cur_kind["v"] == "reasoning":
                out.stream_end()  # content after reasoning starts on its own line
                cur_kind["v"] = None
            if cur_kind["v"] != kind:
                if cur_kind["v"] is not None:
                    out.stream_end()
                out.stream_begin(kind)
                cur_kind["v"] = kind
            out.stream_delta(text)
            if kind == "content":
                stream_gap["content"] = True

        try:
            msg = call_llm(messages, opts, on_delta)
            interrupted = False
        except TurnInterrupted:
            interrupted = True
        finally:
            if cur_kind["v"] is not None:
                out.stream_end()
        if interrupted:
            # interrupted mid-generation: the assistant message never entered the history, so just end the turn
            out.info("\n[generation interrupted, this turn ends here; press Ctrl+C again to quit]")
            return

        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        tool_calls = msg.get("tool_calls") or []

        # content was already printed live, so only the log is written here (at quiet level reasoning is skipped;
        # the final answer is shown below)
        if reasoning:
            out.log("THINKING", reasoning.strip())
        if content:
            out.log("ASSISTANT", content)
        # fallback at quiet level, where live content was suppressed and nothing has been printed yet
        if content and out.level == LEVEL_QUIET and not tool_calls:
            out.assistant(content, True)

        # content is always a string: the non-streaming fallback may give null, which some servers reject
        assistant_msg = {"role": "assistant", "content": msg.get("content") or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)
        if not tool_calls:
            return

        aborted = False
        abort_note = abort_info = ""
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name, raw_args = fn.get("name"), fn.get("arguments") or "{}"
            out.log("TOOL_CALL " + (name or "?"), raw_args)
            out.tool_call(name, raw_args)
            if aborted:
                result = abort_note
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
                continue

            key = "%s|%s" % (name, raw_args)
            if key == repeat["key"]:
                repeat["n"] += 1
            else:
                repeat["key"], repeat["n"] = key, 1
            if repeat["n"] >= MAX_REPEAT_CALLS:
                aborted = True
                abort_note = "Error: this turn was aborted after repeated identical calls, this call was not executed."
                abort_info = "%d identical calls in a row detected, this turn stops here" % MAX_REPEAT_CALLS
                result = ("Error: the identical %s call was repeated %d times in a row; this turn was aborted and "
                          "this call was not executed. Try a different approach or state your conclusion."
                          % (name, repeat["n"]))
            else:
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        args = {}
                except ValueError as e:
                    result = "Error: cannot parse the tool argument JSON: %s" % e
                else:
                    try:
                        result = execute_tool(name, args, opts)
                    except TurnInterrupted:
                        # interrupted halfway through a tool: the result cannot be trusted, and the
                        # remaining calls are not run either
                        result = ("Error: the user pressed Ctrl+C during this turn, so this call did not finish "
                                  "normally; do not call more tools and wait for the user's next instruction.")
                        aborted = True
                        abort_note = "Error: this turn was interrupted by Ctrl+C, this call was not executed."
                        abort_info = "this turn was interrupted (Ctrl+C); press Ctrl+C again to quit"

            out.log("TOOL_RESULT " + (name or "?"), result)
            out.tool_result(result, _is_error(result))
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})

        if aborted:
            out.info("\n[%s]" % abort_info)
            return

    out.info("\n[Tool call limit of %d reached for this turn, stopping]" % opts["max_steps"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    env = os.environ.get
    S = argparse.SUPPRESS  # use SUPPRESS to tell whether the user explicitly passed an option
    parser = argparse.ArgumentParser(
        description="BO -- a single-file, standard-library-only minimal coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Note: --yes means \"ask for manual confirmation before each command\". Without --yes commands run directly.\n"
               "Explicitly passed connection options (-m/--model, -b/--base-url, -k/--api-key, -r/--root, "
               "-s/--max-steps, -t/--http-timeout, -l/--log) are encrypted into .bo in the current directory "
               "and reused later when omitted or only partially passed; delete .bo to reset. "
               "Precedence: command line > environment variable > .bo > built-in default.")
    parser.add_argument("-m", "--model", default=S,
                        help="model name (defaults to MODEL / OPENAI_MODEL, then .bo)")
    parser.add_argument("-b", "--base-url", default=S,
                        help="OpenAI-compatible endpoint URL (defaults to OPENAI_BASE_URL, then .bo)")
    parser.add_argument("-k", "--api-key", default=S,
                        help="API key (defaults to OPENAI_API_KEY, then .bo)")
    parser.add_argument("-y", "--yes", action="store_true", dest="confirm",
                        help="ask for manual confirmation before each command (without it commands run directly)")
    parser.add_argument("-r", "--root", default=S, metavar="DIR",
                        help="restrict the file tools (read_file/write_file/search) and the run_command cwd to this directory only (unrestricted by default)")
    parser.add_argument("-s", "--max-steps", type=int, default=S, help="maximum number of tool-call rounds per turn")
    parser.add_argument("-t", "--http-timeout", type=int, default=S, help="timeout in seconds for a single HTTP request")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="show only the final answer, hide all tool/thinking output")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="raise the display level: -v shows tool results and thinking, -vv shows full detail")
    parser.add_argument("-C", "--no-color", action="store_true",
                        help="disable colored output (colors are used only on a terminal by default)")
    parser.add_argument("-l", "--log", nargs="?", const="BO.log", default=S, dest="log_path", metavar="FILE",
                        help="write the full interaction to a log file (default BO.log), no detail on screen")
    parser.add_argument("-L", "--no-log", action="store_const", const=None, default=S, dest="log_path",
                        help="undo a recorded -l/--log and stop writing a log")
    a = parser.parse_args()
    a_vars = vars(a)

    cfg_path = os.path.join(os.getcwd(), CONFIG_FILE)
    cfg, cfg_status = load_config(cfg_path)
    used_cfg = []  # keys actually taken from .bo this run, for the startup notice

    def pick(name, env_value, default):
        """Resolve one option: explicit CLI > environment variable > .bo > built-in default."""
        v = getattr(a, name, None)
        if v is not None:
            return v
        if env_value is not None:
            return env_value
        if name in cfg:
            used_cfg.append(name)
            return cfg[name]
        return default

    model = pick("model", env("MODEL") or env("OPENAI_MODEL"), "gpt-4o-mini")
    # an empty environment variable counts as unset, otherwise it would blank the default endpoint
    base_url = pick("base_url", env("OPENAI_BASE_URL") or None, "https://api.openai.com/v1")
    api_key = pick("api_key", env("OPENAI_API_KEY") or None, "")
    root = pick("root", None, None)
    root = os.path.realpath(root) if root else None
    max_steps = pick("max_steps", None, 50)
    http_timeout = pick("http_timeout", None, 120)

    # log: -l/--log sets it, --no-log clears it explicitly (clearing must be told apart from "not passed")
    if "log_path" in a_vars:
        log_path = a_vars["log_path"]
    elif "log_path" in cfg:
        used_cfg.append("log_path")
        log_path = cfg["log_path"]
    else:
        log_path = None

    # interaction and display switches apply to this run only and are not written to .bo
    confirm = bool(a.confirm)
    level = LEVEL_QUIET if a.quiet else min(LEVEL_NORMAL + a.verbose, LEVEL_DEBUG)
    try:
        color = (not a.no_color) and env("NO_COLOR") is None and sys.stdout.isatty()
    except Exception:
        color = False

    # write back only the connection options explicitly passed this run, preserving other keys (names come from CONFIG_KEYS)
    resolved = {"model": model, "base_url": base_url, "api_key": api_key, "root": root,
                "max_steps": max_steps, "http_timeout": http_timeout, "log_path": log_path}
    updates = {k: resolved[k] for k in CONFIG_KEYS if k in a_vars}

    config_saved, config_error = False, None
    if updates:
        merged = dict(cfg)
        merged.update(updates)
        if merged != cfg:  # skip rewriting when nothing changed, to avoid needless file churn
            config_error = save_config(merged, cfg_path)
            config_saved = config_error is None

    return {
        "model": model, "base_url": base_url, "api_key": api_key,
        "confirm": confirm, "root": root,
        "max_steps": max_steps, "http_timeout": http_timeout, "cwd": os.getcwd(),
        "level": level, "color": color, "log_path": log_path,
        "config_status": cfg_status, "config_saved": config_saved, "config_error": config_error,
        "config_used": bool(used_cfg),
    }


def setup_stdio():
    """Force UTF-8 on stdin/stdout/stderr so non-ASCII output never fails.

    Python 3.7+ has reconfigure; 3.6 does not, so wrap the raw buffer instead.
    """
    for name in ("stdout", "stderr", "stdin"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
                continue
            except Exception:
                pass
        buf = getattr(stream, "buffer", None)
        if buf is None:
            continue
        try:
            setattr(sys, name, io.TextIOWrapper(buf, encoding="utf-8", errors="replace"))
        except Exception:
            pass


def main():
    setup_stdio()
    install_sigint_handler()

    opts = parse_args()
    out = Output(opts["level"], opts["color"], opts["log_path"])
    system_prompt = build_system_prompt(opts)

    sys.stdout.write(
        "BO -- minimal coding agent (Python %s, %s)\n"
        "Model: %s\nEndpoint: %s\nCommand confirmation: %s\nDisplay level: %s\n"
        % (platform.python_version(), platform.system(), opts["model"], opts["base_url"],
           "on (--yes)" if opts["confirm"] else "off (run directly)", LEVEL_NAMES[opts["level"]]))
    if opts["root"]:
        sys.stdout.write("File access restriction: %s\n" % opts["root"])
    if out.log_path:
        sys.stdout.write("Full log: %s\n" % out.log_path)
    if opts["config_status"] == "invalid":
        sys.stderr.write("Warning: %s exists but cannot be decrypted/parsed (or belongs to another directory); ignored\n" % CONFIG_FILE)
    if opts["config_used"]:
        sys.stdout.write("Parameter memory: loaded from %s\n" % CONFIG_FILE)
    if opts["config_saved"]:
        sys.stdout.write("Parameter memory: written to %s\n" % CONFIG_FILE)
    elif opts["config_error"]:
        sys.stderr.write("Warning: failed to write %s: %s\n" % (CONFIG_FILE, opts["config_error"]))
    sys.stdout.write("Type /help for help, exit to quit.\n")

    out.log("SESSION BEGIN", "number: %d\nmodel=%s\nbase_url=%s\ncwd=%s\nlevel=%s" % (
        out.session_id, opts["model"], opts["base_url"], opts["cwd"], opts["level"]))
    out.log("SYSTEM", system_prompt)
    messages = [{"role": "system", "content": system_prompt}]
    started = time.time()

    try:
        while True:
            try:
                user = input("\nyou > ").strip()
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\nGoodbye.\n")
                break
            if not user:
                continue
            if user in ("exit", "quit", "/exit", "/quit"):
                sys.stdout.write("Goodbye.\n")
                break
            if user == "/reset":
                messages = [{"role": "system", "content": system_prompt}]
                out.log("RESET", "conversation history cleared")
                sys.stdout.write("Conversation history cleared.\n")
                continue
            if user == "/help":
                sys.stdout.write("Available commands:\n"
                                 "  /reset   clear the conversation history\n"
                                 "  /help    show this help\n"
                                 "  exit     quit\n"
                                 "  Ctrl+C   first press interrupts the current turn (generation/command), "
                                 "a second one quits\n")
                continue

            try:
                run_turn(user, messages, opts, out)
            except KeyboardInterrupt:   # two Ctrl+C presses within one turn
                sys.stdout.write("\nGoodbye.\n")
                break
            except TurnInterrupted:     # fallback: the interrupt landed outside generation/the tool loop
                _close_dangling_tool_calls(
                    messages, "Error: this turn was interrupted by Ctrl+C, this call was not executed.")
                out.info("\n[this turn was interrupted; press Ctrl+C again to quit]")
            except RuntimeError as e:
                out.log("ERROR", str(e))
                out.error(str(e))
            except Exception as e:
                msg = "%s: %s" % (type(e).__name__, e)
                out.log("ERROR", msg)
                out.error(msg)
            finally:
                _INTERRUPT["busy"] = False
    except KeyboardInterrupt:
        # fallback: Ctrl+C pressed again while handling an error/interrupt, avoid a raw traceback
        sys.stdout.write("\nGoodbye.\n")
    finally:
        out.log("SESSION END", "number: %d\nduration: %.1fs" % (out.session_id, time.time() - started))
        out.close()


if __name__ == "__main__":
    main()
