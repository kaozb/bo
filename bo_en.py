#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BO —— a minimal coding agent in a single file, standard library only

As long as the device can run Python 3.6+, this script runs, with no third-party libraries to install.
The backend uses an OpenAI-compatible API (/v1/chat/completions), so it can talk to OpenAI, DeepSeek,
Tongyi, vLLM, Ollama, LM Studio and any other compatible service.

Usage:
    export OPENAI_BASE_URL=https://api.deepseek.com/v1
    export OPENAI_API_KEY=sk-xxxx
    export MODEL=deepseek-chat
    python BO.py

Arguments:
    -m, --model NAME    model name (overrides MODEL / OPENAI_MODEL)
    -b, --base-url URL  API address (overrides OPENAI_BASE_URL)
    -k, --api-key KEY   API key (overrides OPENAI_API_KEY)
    -y, --yes           require manual confirmation for each command before it runs (without it, commands run by default)
    -s, --max-steps N   max number of tool call rounds per turn (default 50)
    -t, --http-timeout N  timeout in seconds for a single request (default 120)
    -q                  show only the final answer, hide all tool/thinking output
    -v / -vv            show tool results and thinking / full detail
    -C, --no-color      disable colored output
    -d, --db FILE       session database (default .ai.db), the full interaction record is written here

Argument memory: connection-type arguments passed explicitly (-m / -b / -k / -s / -t / -d) are encrypted and recorded in .bo in the user's home directory,
          and are reused automatically later when no arguments or only some arguments are passed; interactive and display switches such as -y / -q / -v / -C take effect for this run only and are
          not written to that file. Priority: command line > environment variables > .bo > built-in defaults; deleting .bo restores the defaults.

Interaction: /reset clears and starts a new session, /s loads a past session, /help shows help, exit quits
"""

import argparse
import base64
import difflib
import fnmatch
import hashlib
import hmac
import io
import json
import os
import platform
import re
import signal
import sqlite3
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
MAX_LINE_CHARS = 2000                # max characters displayed for a single line by read_file
MAX_READ_BYTES = 3 * 1024 * 1024     # file read/write size limit; anything larger is refused outright (to keep memory from blowing up)
MAX_READ_MB = MAX_READ_BYTES // 1048576  # the limit in MB, reused by notice texts
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # max size of a single HTTP response
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024  # max bytes read per file by search
MAX_SEARCH_RESULTS = 1000            # max number of result lines returned by one search
MAX_EDITS = 50                       # max entries in the write_file edits array per call
MAX_COMMAND_TIMEOUT = 3600           # run_command timeout limit (seconds)
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024  # max output run_command keeps in memory (half at each end, then truncated to the visible length)
MAX_DIR_ITEMS = 1000                 # max items displayed at once when read_file lists a directory
MAX_REPEAT_CALLS = 3                 # number of consecutive identical tool calls in one turn that aborts the turn
TRIM_KEEP_CALLS = 1                  # keep only the most recent few tool calls (with their results) in the history, remove earlier ones
SEARCH_SKIP_DIRS = (".git", "__pycache__", "node_modules", ".venv", "venv",
                    ".tox", ".mypy_cache", ".pytest_cache")  # directories skipped by search
AGENT_FILE = "AGENTS.md"             # convention file in the current directory (case-insensitive); when present the model is told to read it
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".bo")
# Argument memory file in the user's home directory (encrypted), shared globally: written when arguments are passed explicitly, reused when none are
# Only these "connection/run-type" arguments are written to .bo; interactive and display switches (-y / -q / -v / -C) take effect for this run only
CONFIG_KEYS = ("model", "base_url", "api_key", "max_steps", "http_timeout", "db_path")
DEFAULT_DB_FILE = ".ai.db"              # default session database file name (in the startup directory); can be set and remembered with -d/--db
BO_VERSION = "1.1.0-db"                 # version identifier written into the session database

# Screen display levels
LEVEL_QUIET, LEVEL_NORMAL, LEVEL_VERBOSE, LEVEL_DEBUG = 0, 1, 2, 3
LEVEL_NAMES = ("quiet (final answer only)", "normal (one-line notice per tool call)",
               "verbose (including tool results and thinking)", "debug (full detail)")


# ---------------------------------------------------------------------------
# Ctrl+C semantics: the first press interrupts the current turn (generation or command), the second quits the program
# ---------------------------------------------------------------------------

class TurnInterrupted(BaseException):
    """Raised on the first Ctrl+C: end only the current turn, return to the prompt, do not quit.

    Inherits from BaseException so it is not swallowed as an ordinary error by except Exception along the way.
    """


# busy means "currently inside a turn", seen means Ctrl+C has already been pressed once this turn
_INTERRUPT = {"busy": False, "seen": False}


def _handle_sigint(signum, frame):
    if _INTERRUPT["busy"] and not _INTERRUPT["seen"]:
        _INTERRUPT["seen"] = True
        raise TurnInterrupted()     # first press within a turn: interrupt only this turn
    raise KeyboardInterrupt()       # while idle, or second press within the turn: quit the program


def install_sigint_handler():
    """Take over SIGINT; if that fails (non-main thread, etc.) keep the default behavior."""
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
    """A JSON Schema property of type "array of objects"."""
    return {"type": "array", "description": desc,
            "items": {"type": "object", "properties": item_props, "required": item_required}}


TOOLS = [
    _fn("read_file", "read a file (with line numbers) or list a directory; offset starts at 1 and an offset for continued reading is given when the file is not fully read, "
        "binary files are refused. Do not include line numbers in write_file's old_string.",
        {"path": _p("string", "file or directory path (relative or absolute)"),
         "offset": _p("integer", "starting line number / starting item, starting at 1, default 1"),
         "limit": _p("integer", "max lines to read for a file (default 2000) / max items to list for a directory (default 200)")},
        ["path"]),
    _fn("write_file", "write a file, two modes [never mix them]:\n"
        "(1) create/overwrite whole file: give only path + content (do not give old_string/new_string/edits).\n"
        "(2) change only part: give only path + old_string + new_string (or edits to submit several spots at once), "
        "and in that case [do not give content].\n"
        "Counter-example (will fail): giving both content and old_string. Correct approach: to change part, give only old_string+new_string.\n"
        "old_string must appear verbatim in the file and be unique (trailing spaces/indentation must match, do not include line numbers), "
        "otherwise an error is reported and candidate lines are listed; submit several different changes at once with edits; a missing parent directory is created automatically; empty content is refused.",
        {"path": _p("string", "file path"),
         "content": _p("string", "the complete text to write as a whole (choose one of this or old_string/edits, cannot be empty)"),
         "old_string": _p("string", "the original text being replaced, must be unique in the file (choose one of this or content; do not include line numbers)"),
         "new_string": _p("string", "the new text after replacement (choose one of this or content)"),
         "replace_all": _p("boolean", "when true, replace every occurrence, default false"),
         "edits": _arr("submit several changes at once, applied in order; if any fails the whole batch is not written (choose one of this or content)",
                       {"old_string": _p("string", "the original text being replaced (do not include line numbers)"),
                        "new_string": _p("string", "the new text after replacement"),
                        "replace_all": _p("boolean", "replace every occurrence, default false")},
                       ["old_string", "new_string"])},
        ["path"]),
    _fn("search", "search files or directories with a regular expression (case-sensitive; use the (?i) prefix to ignore case), skipping .git / __pycache__ / "
        "node_modules and similar directories as well as binary and oversized files. Uses less output than running grep via run_command.",
        {"pattern": _p("string", "regular expression; when it is not a valid regex, it is searched literally"),
         "path": _p("string", "file or directory to search, default current directory ."),
         "glob": _p("string", "search only files matching this wildcard (e.g. *.py), default all"),
         "max_results": _p("integer", "max number of matching lines to return, default 100, limit 1000 (context lines are not counted; the output has a separate 30000 character limit)"),
         "context_lines": _p("integer", "add N lines of context above and below each match (0-10), default 0")},
        ["pattern"]),
    _fn("run_command", "run a command in the shell, returning the exit code and the merged stdout+stderr."
        "The command's stdin is empty; do not run interactive commands such as vim / top; when the output is too large only the beginning and end are kept and truncation is reported.",
        {"command": _p("string", "the shell command to run (non-interactive; for background tasks use nohup ... & yourself)"),
         "cwd": _p("string", "working directory of the command, default current directory"),
         "timeout": _p("integer", "timeout in seconds, default 120; on timeout it is terminated together with any spawned processes")},
        ["command"]),
]


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def _decode_bytes(data):
    """Decode bytes into text, returning (text, actual encoding); as lossless as possible (latin-1 as a fallback always succeeds)."""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc), enc
        except Exception:
            continue
    return data.decode("utf-8", "replace"), "utf-8"


def _count_lines(data):
    """Count lines (\\n breaks lines, a final line without a newline still counts); data may be bytes or str."""
    nl = b"\n" if isinstance(data, bytes) else "\n"
    n = data.count(nl)
    return n + 1 if data and not data.endswith(nl) else n


def _split_lines(text):
    """Unified text line splitting: break lines only on \n, drop a trailing \r, ignore trailing empty lines.

    Unlike splitlines(), \x0b / \x0c / \x85 / \u2028 etc. are not treated as line separators here,
    so line numbers match the editor and match how streaming reads of large files split lines.
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
    note = note or "\n... [truncated, the original text has %d characters]"
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
    """Convert a tool argument to int; fall back to the default when it is invalid or out of range."""
    try:
        n = int(value)
    except Exception:
        return default
    if minimum is not None and n < minimum:
        return default
    return n


def _is_error(result):
    """Decide whether a tool result counts as a failure (at normal level only errors are reported)."""
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
    """Ask the user yes/no; anything other than y/yes counts as a decline (EOF/EOT also counts as a decline).

    Ctrl+C is not swallowed here: it is handed to _handle_sigint —— the first press in a turn interrupts only the turn, the second quits the program.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        ans = input().strip().lower()
    except EOFError:
        ans = "n"
    return ans in ("y", "yes")


def _truncate_middle(text, limit=MAX_OUTPUT_CHARS):
    """Command output truncation: keep both ends (errors are usually at the tail) and omit the middle."""
    if not text or len(text) <= limit:
        return text
    head = max(1, limit // 3)
    tail = max(0, limit - head - 64)
    note = "\n... [middle omitted %d characters] ...\n" % (len(text) - head - tail)
    return text[:head] + note + (text[-tail:] if tail else "")


# ---------------------------------------------------------------------------
# File access (path normalization, size limits and atomic writes)
# ---------------------------------------------------------------------------

def _resolve(path, opts):
    """Normalize a path to its real path, returning (real path, error message). opts is kept to keep the call signature uniform."""
    if not path:
        return None, "Error: missing path argument"
    return os.path.realpath(path), None


def _read_file_bytes(path):
    """Read an entire file with a size limit. Returns (bytes, error message)."""
    if not os.path.exists(path):
        return None, "Error: file does not exist: %s" % path
    if not os.path.isfile(path):
        return None, "Error: not a regular file (directory/device file, etc.): %s" % path
    size = os.path.getsize(path)
    if size > MAX_READ_BYTES:
        return None, "Error: file too large (%.1f MB, limit %d MB), please use run_command with head/tail/sed" % (
            size / 1048576.0, MAX_READ_MB)
    try:
        with open(path, "rb") as f:
            return f.read(), None
    except Exception as e:
        return None, "Error: read failed: %s" % e


def _binary_error(shown):
    """Unified notice for binary files."""
    return ("Error: %s looks like a binary file (contains NUL bytes), text reading was skipped."
            "You can inspect it with file / xxd / hexdump via run_command." % shown)


def _atomic_write(path, data, mode=None):
    """Atomic write: write a temporary file in the same directory and fsync it, then overwrite with os.replace.

    When mode is None the original permissions are kept (0644 for a new file), otherwise the given permissions are used unconditionally (e.g. 0600 for .bo).
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
    except BaseException:  # includes Ctrl+C (BaseException); an interrupt must not leave a temporary file behind
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _read_lines_window(path, shown, offset, limit):
    """Read a file by line window, returning (line list, total lines, error); offset is 0-based.

    The whole file is read and _decode_bytes detects the encoding (utf-8 → gbk → latin-1, so GBK and the like are read correctly too).
    Files above MAX_READ_BYTES are refused outright by _read_file_bytes and no longer go through streaming scanning,
    so the "small files fine but large files garbled due to hard-coded utf-8" double-path inconsistency does not exist.
    """
    raw, err = _read_file_bytes(path)
    if err:
        return None, 0, err
    if b"\x00" in raw[:8192]:
        return None, 0, _binary_error(shown)
    lines = _split_lines(_decode_bytes(raw)[0])
    return lines[offset:offset + limit], len(lines), None


def _list_dir(real, shown, offset=0, limit=200):
    """List directory contents (used when read_file is given a directory); lists one level only, supports offset/limit paging."""
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
        return "Directory %s has %d items in total, offset=%d is past the end." % (shown, len(items), offset + 1)
    note = ""
    if offset + len(window) < len(items):
        note = "\n... [%d more items, continue with offset=%d]" % (
            len(items) - offset - len(window), offset + len(window) + 1)
    return "Directory %s has %d items in total (directories %d / files %d), showing items %d-%d:\n%s%s" % (
        shown, len(items), len(dirs), len(files),
        offset + 1, offset + len(window), "\n".join(window), note)


def find_agent_file():
    """Find AGENTS.md case-insensitively in the current directory, returning the actual file name or None."""
    want = AGENT_FILE.lower()
    try:
        for name in sorted(os.listdir(".")):
            if name.lower() == want and os.path.isfile(name):
                return name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# .bo argument memory (simple encryption: directory path derived keystream XOR + HMAC-SHA256 check)
# ---------------------------------------------------------------------------

_CFG_MAGIC = b"BOCFG1"
_CFG_MAX_BYTES = 1 << 20             # .bo size limit 1MB


def _cfg_keys(path):
    """Derive (keystream key, mac key) from the real path of the directory containing .bo; the file is thereby bound to the directory."""
    base = os.path.dirname(os.path.realpath(path)).encode("utf-8")
    return (hashlib.sha256(b"bo-config-stream-v1|" + base).digest(),
            hashlib.sha256(b"bo-config-mac-v1|" + base).digest())


def _cfg_keystream(key, n):
    """Derive an n-byte keystream from the key (SHA-256 counter mode)."""
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
    """Decrypt; returns None if any step (base64/magic/HMAC/encoding) fails (treated as no valid config)."""
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
    """Read and decrypt .bo. Returns (dict, status), status ∈ 'missing' / 'ok' / 'invalid'."""
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
    """Encrypted write of .bo (0600, atomic replace). Returns an error message, or None on success."""
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
        return "File %s has %d lines in total, offset=%d is past the end of the file." % (shown, total, offset + 1)

    # Assemble line by line and accumulate the character count: even with a large limit a single output stays within the context budget
    shown_lines, used, cut = [], 0, False
    for i, ln in enumerate(lines):
        text = _truncate(ln, MAX_LINE_CHARS, note="... [this line was truncated]")
        cost = len(text) + 8  # rough overhead of the line number and the newline
        if shown_lines and used + cost > MAX_OUTPUT_CHARS:
            cut = True
            break
        shown_lines.append("%6d\t%s" % (offset + i + 1, text))
        used += cost
    end = offset + len(shown_lines)
    header = "File %s has %d lines in total, showing lines %d-%d:" % (shown, total, offset + 1, end)
    if end >= total:
        footer = "\n[end of file reached]"
    elif cut:
        footer = "\n[output reached the %d character limit, showing up to line %d this time; continue with offset=%d]" % (
            MAX_OUTPUT_CHARS, end, end + 1)
    else:
        footer = "\n[%d lines not shown yet, continue with offset=%d]" % (total - end, end + 1)
    return header + "\n" + "\n".join(shown_lines) + footer


def _pick_write_mode(args):
    """Decide the write mode from the arguments, returning (execution function, extra hint); when the argument combination is invalid the function is None and the hint is the error message."""
    has_content = args.get("content") is not None
    has_edit = (args.get("edits") is not None or args.get("old_string") is not None
                or args.get("new_string") is not None)
    if has_content and has_edit:
        # Model slip-up: it stuffed the whole context into content and also gave old_string/new_string.
        # As long as new_string / edits is present the intent is a partial replace, so ignore content and just do it.
        if args.get("new_string") is not None or args.get("edits") is not None:
            return _edit_existing_file, ("\nHint: detected that content was given at the same time; treated it as a \"partial replace\""
                                          " and ignored content. If you really want to overwrite the whole file, pass only content.")
        return None, ("Error: content and old_string/new_string/edits cannot be used together."
                      "To write the whole file give only content; for a partial replace give only old_string + new_string or edits."
                      "(If you wanted a partial replace, please add new_string.)")
    if has_edit:
        return _edit_existing_file, ""
    if not has_content:
        hint = ""
        if args.get("replace_all") is not None:
            hint = "(replace_all only works together with old_string/new_string/edits, it had no effect this time)"
        return None, ("Error: missing arguments. Writing the whole file needs content; a partial replace needs old_string + new_string, "
                      "or use edits to submit several changes at once." + hint)
    if args.get("replace_all") is not None:
        return _write_whole_file, ("\nHint: replace_all only works for a partial replace (old_string/new_string or edits)"
                                    ", so this whole-file write ignored the argument.")
    return _write_whole_file, ""


def tool_write_file(args, opts):
    """Write a file: automatically choose between "write the whole file" and "exact replace" based on the arguments."""
    shown = args.get("path")
    path, err = _resolve(shown, opts)
    if err:
        return err
    func, hint = _pick_write_mode(args)
    if func is None:
        return hint  # invalid argument combination, the hint is the error message
    result = func(path, shown, args)
    if hint and result.startswith("OK:"):  # only append the hint on success
        result += hint
    return result


def _write_whole_file(path, shown, args):
    """Mode one: write the whole file (create or overwrite)."""
    content = args.get("content")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    if content == "":
        return ("Error: content is empty, which would clear the file, so the write was refused. To create an empty file use run_command's touch;"
                " to really clear an existing file run `: > file` explicitly.")
    if os.path.isdir(path):
        return "Error: %s is a directory, cannot write a file" % shown

    existed = os.path.isfile(path)
    enc, old_lines, old_bytes = "utf-8", 0, 0
    if existed:
        if os.path.getsize(path) > MAX_READ_BYTES:
            return "Error: target file too large (> %d MB), refusing to overwrite the whole file; use old_string/new_string for a partial change instead" % (
                MAX_READ_BYTES // 1048576)
        raw, err = _read_file_bytes(path)
        if err:
            return err
        old_bytes = len(raw)
        old_lines = _count_lines(raw)
        enc = _decode_bytes(raw)[1]
    enc_note = ""
    try:
        data = content.encode(enc)
    except Exception:
        enc_note = "(the original encoding %s cannot represent the new content, saved as utf-8 instead)" % enc
        enc, data = "utf-8", content.encode("utf-8")
    if len(data) > MAX_READ_BYTES:
        return "Error: content to write too large (%.1f MB, limit %d MB)" % (
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
        return "OK: created %s (%d lines, %d bytes, encoding %s)%s" % (
            shown, new_lines, new_bytes, enc, enc_note)
    result = "OK: overwrote %s (was %d lines/%d bytes → now %d lines/%d bytes, encoding %s)%s" % (
        shown, old_lines, old_bytes, new_lines, new_bytes, enc, enc_note)
    if old_bytes and new_bytes < old_bytes * 0.5 and new_lines < old_lines:
        result += "\nNote: the new content is %.0f%% smaller than the original file; if you only meant to change part of it use old_string/new_string." % (
            (1 - new_bytes / float(old_bytes)) * 100)
    return result


def _line_text(text, idx):
    """The original text of the line containing the index."""
    a = text.rfind("\n", 0, idx) + 1
    b = text.find("\n", idx)
    return text[a:b if b >= 0 else len(text)]


def _locate_fuzzy(text, old):
    """Locate old ignoring trailing whitespace and CRLF differences, returning the list of spans in the original text.

    Compares line by line: a single-line old is found as a substring within each line; a multi-line old requires the first line to end with first,
    the middle lines to be equal line by line, and the last line to start with last, consistent with the "ignore trailing whitespace/CRLF" semantics.
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
    """Locate occurrences of old in text, returning (span list, note).

    Exact matching first; if nothing is matched at all, match again ignoring trailing whitespace / CRLF differences.
    """
    if not old:
        return [], ""
    spans, start = [], 0
    while True:
        i = text.find(old, start)
        if i < 0:
            break
        spans.append((i, i + len(old)))
        start = i + 1  # allow overlapping matches, to avoid missing "multiple occurrences" (e.g. finding aa in aaa)
    if spans:
        return spans, ""
    spans = _locate_fuzzy(text, old)
    return spans, ", ignored trailing whitespace/CRLF differences" if spans else ""


def _locate_tolerant(text, old):
    """Fallback degradation: when the difference between old_string and some line of the file is only in "how much/where the whitespace, tab, CRLF" is,
    locate it by "whole line equal after removing all whitespace", returning that line's span in the original text (span list, note).

    Typical slip-ups: `foo; }` written as `foo;}`, extra/missing trailing spaces, misaligned indentation, etc.
    Only handles a single-line old (a multi-line old is left to _locate_fuzzy); if too short after normalization it gives up, to avoid false matches.
    """
    o_lines = [ln for ln in old.split("\n") if ln.strip()]
    if len(o_lines) != 1:
        return [], ""
    key = re.sub(r"\s+", "", o_lines[0])
    if len(key) < 4:  # too short after normalization (e.g. "}", "fi") is easy to hit by mistake, give up the fallback
        return [], ""
    spans, acc = [], 0
    for ln in text.split("\n"):
        if re.sub(r"\s+", "", ln) == key:
            spans.append((acc, acc + len(ln)))
        acc += len(ln) + 1
    return spans, (", matched as a whole line (whitespace differences ignored)" if spans else "")


def _probe_fragments(old, limit=3):
    """Take fragments from old for approximate locating: whole lines first, then longer words.

    The first non-empty line is included in the probe no matter how short it is (even just "}", "fi"), otherwise when old_string merely
    missed a space/semicolon, the only line that could locate it would be filtered out entirely for being too short, producing the notice
    "no similar content found".
    """
    frags = [s for s in (ln.strip() for ln in old.split("\n")) if len(s) >= 3]
    for s in (ln.strip() for ln in old.split("\n")):
        if s and s not in frags:  # fallback: include the first non-empty line too, to cover very short lines
            frags.insert(0, s)
            break
    if not frags:
        frags = [t for t in re.split(r"[^0-9A-Za-z_]+", old) if len(t) >= 3]
    if len(frags) > limit:  # take the first and last lines, to cover an old spanning several lines
        frags = [frags[0], frags[-1]]
    return frags[:limit]


def _mark(line):
    """Mark invisible characters in a line: a trailing space is shown as ␣, a Tab as ⇥."""
    return line.rstrip(" \t").replace("\t", "⇥") + (
        " " + "␣" * (len(line) - len(line.rstrip(" "))))


def _candidates(text, old, spans, limit=5):
    """Generate a locating report: the positions already matched, or the lines closest to old (verbatim, easy to copy directly)."""
    if spans:
        out = []
        for a, _ in spans[:limit]:
            out.append("  line %d: %s" % (
                text.count("\n", 0, a) + 1, _mark(_line_text(text, a))))
        if len(spans) > limit:
            out.append("  ... %d positions in total" % len(spans))
        return "Candidate positions:\n" + "\n".join(out)

    # No match: scan for substrings using fragments from old (str.find is a C-level implementation, orders of magnitude faster than scoring line by line)
    lines = text.split("\n")
    hits, seen = [], set()
    for needle in _probe_fragments(old):
        start = 0
        while len(hits) < limit:
            i = text.find(needle, start)
            if i < 0:
                break
            ln = text.count("\n", 0, i) + 1  # 1-based line number
            if ln not in seen:
                seen.add(ln)
                hits.append(ln)
            start = i + 1
        if len(hits) >= limit:
            break
    if not hits:
        return ("No similar content found (%d lines in the file). Please use read_file to confirm the original text"
                "(watch out for spaces and indentation, and do not copy the line numbers along)." % len(lines))
    return ("old_string not found. The closest lines (verbatim, can be copied directly as old_string):\n" +
            "\n".join("  line %d: %s" % (i, _mark(lines[i - 1])) for i in hits) +
            "\nHint: the lines above are given verbatim, with trailing spaces marked as ␣ and Tabs as ⇥;"
            " please include these whitespace characters verbatim when copying.")


def _diff_block(old_block, new_block, context=1, limit=24):
    """Generate a small unified diff (dropping the file headers and truncating if necessary)."""
    a, b = old_block.splitlines(), new_block.splitlines()
    lines = [ln for ln in difflib.unified_diff(a, b, lineterm="", n=context)
             if not ln.startswith("--- ") and not ln.startswith("+++ ")]
    if len(lines) > limit:
        lines = lines[:limit] + ["... [diff truncated]"]
    return "\n".join(lines)


def _apply_edit(text, edit):
    """Apply one edit, returning (new text, number of replacements, diff, note, error)."""
    old, new = edit.get("old_string"), edit.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return text, 0, "", "", "Error: each edit needs a string old_string and new_string"
    if not old:
        return text, 0, "", "", "Error: old_string cannot be empty"
    if old == new:
        return text, 0, "", "", "Error: old_string and new_string are the same, no change needed"

    replace_all = bool(edit.get("replace_all"))
    spans, note = _locate(text, old)
    if not spans:
        # When neither exact nor trailing-whitespace-ignoring matching hits, do one "whole line probe" degradation:
        # if old_string merely missed/extended trailing whitespace or a semicolon, replace using that whole line's original text from the file.
        cand, cnote = _locate_tolerant(text, old)
        if cand:
            spans, note = cand, cnote
        else:
            return text, 0, "", "", "Error: old_string not found.\n" + _candidates(text, old, [])
    if len(spans) > 1 and not replace_all:
        return text, 0, "", "", (
            "Error: old_string appears %d times in the file, it is not unique. Add more context to make it unique, "
            "or set replace_all=true to replace all of them together with the context.\n%s" % (
                len(spans), _candidates(text, old, spans)))

    if replace_all:
        # Replacing overlapping matches together would corrupt each other, so only keep non-overlapping positions here
        use, last_end = [], -1
        for a, b in spans:
            if a >= last_end:
                use.append((a, b))
                last_end = b
    else:
        use = spans[:1]
    if replace_all and not note:
        out = text.replace(old, new)  # on an exact match hand it to str.replace (C-level, a single scan)
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
        diff += "\n(%d replacements in total, the diff shows only the first)" % len(use)
    return out, len(use), diff, note, ""


def _edit_existing_file(path, shown, args):
    """Mode two: exact string replacement on an existing file (old_string/new_string or edits)."""
    edits = args.get("edits")
    if edits is None:
        if args.get("old_string") is None or args.get("new_string") is None:
            return "Error: needs the three arguments path / old_string / new_string, or use edits to submit several changes"
        edits = [{"old_string": args.get("old_string"),
                  "new_string": args.get("new_string"),
                  "replace_all": args.get("replace_all")}]
    if not isinstance(edits, list) or not edits:
        return "Error: edits must be an array with at least one edit"
    if len(edits) > MAX_EDITS:
        return "Error: edits accepts at most %d entries at a time, please submit in batches" % MAX_EDITS

    if not os.path.isfile(path):
        return "Error: file does not exist or is not a regular file: %s (to create a file pass the content argument)" % shown
    raw, err = _read_file_bytes(path)
    if err:
        return err
    text, enc = _decode_bytes(raw)

    diffs, total, tolerant = [], 0, False
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            return "Error: edits entry %d is not an object" % i
        text, n, diff, note, err = _apply_edit(text, edit)
        if err:
            head = "Error: edits entry %d failed: " % i if len(edits) > 1 else ""
            return head + err + ("\n(nothing was written this time)" if len(edits) > 1 else "")
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

    head = "OK: modified %s (%d replacements in total%s)" % (
        shown, total, (", matched as a whole line (whitespace differences ignored)" if tolerant else ""))
    return head + ("\n" + "\n".join(diffs) if diffs else "")


def _iter_search_files(root_path, glob_pat):
    """List the files to be searched; skip common noise directories and files not matching glob."""
    if os.path.isfile(root_path):
        return [root_path]
    rx = None
    if glob_pat and glob_pat != "*":
        rx = re.compile(fnmatch.translate(glob_pat))  # precompiled, to avoid translating again per file
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
    """Build a regex for the "whole-file prefilter"; returns None when unreliable (falls back to per-line matching).

    MULTILINE only relaxes ^ and $, so text where "every line has a hit" is bound to also hit in a whole-file search, unless:
      - pattern contains \\A / \\Z / (?-m: the meaning of these anchors differs between whole-file and per-line;
      - pattern contains $ and the body contains \\r (CRLF file): where $ lands changes with the splitting method.
    The latter case is judged by the caller from the body; here only the former class of patterns is excluded.
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
        return "no files matching glob=%s under %s" % (shown, glob_pat)

    pre = _search_prefilter(rx.pattern, rx.flags)  # whole-file prefilter; None means not applicable
    crlf_anchored = "$" in rx.pattern            # with $ present a CRLF body cannot use the prefilter
    hits, scanned, skipped = [], 0, 0
    matched, used, full = 0, 0, False            # used: accumulated output character count
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
        # Whole-file prefilter: skip splitlines and per-line regex when there is no hit, to avoid wasted scanning in large directories
        if pre is not None and not (crlf_anchored and "\r" in body):
            if not pre.search(body):
                continue
        lines = _split_lines(body)
        label = os.path.relpath(full_path, target) if is_dir else shown
        emitted = -1  # line number already output (0-based), to avoid repeating context lines of adjacent matches
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
        return "search %s under %s had no hits (scanned %d files)%s" % (
            shown, pattern, scanned,
            "(pattern is not a valid regex, searched literally)" if literal else "")
    tail = ""
    if matched >= limit:
        tail += "\n[hit count reached the limit %d, narrow path/glob or raise max_results]" % limit
    if full:
        tail += "\n[output reached the %d character limit, lower context_lines or narrow the search scope]" % MAX_OUTPUT_CHARS
    if skipped:
        tail += "\n[skipped %d large files over %d MB]" % (skipped, MAX_SEARCH_FILE_BYTES // 1048576)
    if literal:
        tail += "\n[pattern is not a valid regex, searched literally]"
    return "search %s (under %s, scanned %d files, %d hits, output %d lines):\n%s%s" % (
        pattern, shown, scanned, matched, len(hits), _truncate("\n".join(hits)), tail)


def _terminate_group(proc):
    """Kill the whole process group, cleaning up grandchild processes spawned by the command; fall back to a single process when process groups are unsupported."""
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
    """Reader thread: discard the middle while reading, keeping only the beginning and end, so that extremely long output does not blow up memory."""
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
    """Turn the collected output back into text, returning (text, note)."""
    head, tail = box.get("head", b""), box.get("tail", b"")
    total = box.get("total", 0)
    if total > MAX_COMMAND_OUTPUT_BYTES:
        omitted = max(0, total - len(head) - len(tail))
        note = "output is %.1f KB in total, about %d bytes in the middle were omitted (for the complete content redirect to a file and read_file it)" % (
            total / 1024.0, omitted)
        return (head.decode("utf-8", "replace")
                + "\n... [middle omitted about %d bytes] ...\n" % omitted
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

    if opts["confirm"] and not _confirm("\n[command to run]%s %s\nconfirm execution? [y/N] " % (
            " (cwd=%s)" % cwd if cwd != opts["cwd"] else "", command)):
        return "User declined to run the command."

    popen_kwargs = {"shell": True, "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT, "cwd": cwd}
    if hasattr(os, "killpg"):
        # Make the child process its own process group leader, so it can be killed as a group on timeout
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except Exception as e:
        return "Error: cannot start the command: %s" % e

    # The output goes to a background thread that keeps a bounded amount while reading: no matter how much the command spews, memory will not be exhausted
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
        _terminate_group(proc)      # on Ctrl+C (interrupting this turn or quitting) leave no spawned processes behind
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
        return "Command timed out (> %ds) and was terminated (together with spawned processes). Output produced so far:\n%s%s" % (
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
    """Merge a streamed tool_calls fragment into the accumulated result.

    A fragment may carry only part of index/id/function.name/function.arguments, and they must be merged by index.
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


def fetch_models(opts):
    """Request /models and return the model ids the endpoint supports (in the endpoint's own order)."""
    url = opts["base_url"].rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    if opts["api_key"]:
        req.add_header("Authorization", "Bearer " + opts["api_key"])
    try:
        resp = urllib.request.urlopen(req, timeout=opts["http_timeout"])
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s error: %s" % (e.code, _truncate(_decode_bytes(e.read())[0], 2000)))
    except urllib.error.URLError as e:
        raise RuntimeError("network error: %s" % e.reason)
    try:
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except Exception as e:
        raise RuntimeError("failed to read the response: %s" % e)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("response too large (> %d MB), refused" % (MAX_RESPONSE_BYTES // 1048576))
    try:
        obj = json.loads(_decode_bytes(raw)[0])
        items = obj["data"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("response is not a model list: %s" % _truncate(_decode_bytes(raw)[0], 2000))
    ids = [i["id"] for i in items if isinstance(i, dict) and i.get("id")]
    if not ids:
        raise RuntimeError("the endpoint returned no models")
    return ids


def call_llm(messages, opts, on_delta=None):
    """Stream a request to /chat/completions, returning the aggregated message dict.

    on_delta(kind, text): kind is "content" or "reasoning", text is the text added this time.
    If the server does not support streaming (no data: lines returned), automatically fall back to one-shot JSON parsing.
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
    _t0 = time.time()

    content_parts, reasoning_parts, tool_calls = [], [], []
    raw_lines, total = [], 0
    saw_sse = False
    usage = None

    try:
        resp = urllib.request.urlopen(req, timeout=opts["http_timeout"])
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s error: %s" % (e.code, _truncate(_decode_bytes(e.read())[0], 2000)))
    except urllib.error.URLError as e:
        raise RuntimeError("network error: %s" % e.reason)
    latency_ms = int((time.time() - _t0) * 1000)

    try:
        for raw in resp:
            total += len(raw)
            if total > MAX_RESPONSE_BYTES:
                raise RuntimeError("response too large (> %d MB), refused" % (MAX_RESPONSE_BYTES // 1048576))
            line = _decode_bytes(raw)[0].strip()
            if not line:
                continue
            if not line.startswith("data:"):
                if not saw_sse:
                    raw_lines.append(_decode_bytes(raw)[0])
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
            usage = chunk.get("usage") or usage
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
        raise RuntimeError("failed to read the streaming response: %s" % e)
    finally:
        resp.close()

    if not saw_sse:
        # The server did not return SSE (some compatible implementations ignore stream=true), fall back to whole-response parsing
        return _parse_full_response("".join(raw_lines))

    msg = {"role": "assistant", "content": "".join(content_parts)}
    reasoning = "".join(reasoning_parts)
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
    msg["_latency_ms"] = latency_ms
    msg["_usage"] = usage or {}
    return msg


def _parse_full_response(text):
    """Non-streaming fallback: parse the complete JSON response and take out the message."""
    try:
        obj = json.loads(text)
    except Exception as e:
        raise RuntimeError("response is not valid JSON: %s" % e)
    if isinstance(obj, dict) and obj.get("error"):
        raise RuntimeError("API returned an error: %s" % obj["error"])
    try:
        msg = obj["choices"][0]["message"]
        msg["_usage"] = obj.get("usage") or {}
        msg["_latency_ms"] = None
        return msg
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("abnormal response structure: %s" % _truncate(json.dumps(obj, ensure_ascii=False), 2000))


# ---------------------------------------------------------------------------
# Session database (SQLite)
# ---------------------------------------------------------------------------

def db_open(path):
    """Open (or create) .ai.db, create the tables and mark sessions left unfinished by a previous crash as crashed.

    Returns a connection object. The session database may contain command output and even keys, so it is created with 0600.
    """
    new = not os.path.exists(path)
    try:
        conn = sqlite3.connect(path)
    except Exception as e:
        sys.stderr.write("cannot open session database %s: %s\n" % (path, e))
        return None
    try:
        if new:
            os.chmod(path, 0o600)
    except OSError:
        pass
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS sessions("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_no INTEGER,"
        " started_at REAL, ended_at REAL, status TEXT,"
        " model TEXT, base_url TEXT, cwd TEXT, host TEXT, pid INTEGER, bo_version TEXT,"
        " prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,"
        " title TEXT);"
        "CREATE TABLE IF NOT EXISTS events("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id INTEGER, seq INTEGER, ts REAL, step INTEGER, kind TEXT,"
        " role TEXT, content TEXT,"
        " tool_name TEXT, tool_call_id TEXT, tool_args TEXT, is_error INTEGER DEFAULT 0,"
        " latency_ms INTEGER, tokens_prompt INTEGER, tokens_completion INTEGER);"
        "CREATE INDEX IF NOT EXISTS idx_events_sess ON events(session_id, seq);"
        "CREATE INDEX IF NOT EXISTS idx_events_kind ON events(session_id, kind);")
    conn.execute("UPDATE sessions SET status='crashed', ended_at=COALESCE(ended_at, ?) "
                 "WHERE status='running'", (time.time(),))
    conn.commit()
    return conn


def db_new_session(conn, opts):
    """Insert a running session, returning (session_id, session_no)."""
    cur = conn.execute(
        "INSERT INTO sessions(session_no, started_at, ended_at, status, model, base_url,"
        " cwd, host, pid, bo_version) VALUES(NULL, ?, 0, 'running', ?, ?, ?, ?, ?, ?)",
        (time.time(), opts["model"], opts["base_url"], opts["cwd"],
         platform.node(), os.getpid(), BO_VERSION))
    conn.commit()
    sid = cur.lastrowid
    conn.execute("UPDATE sessions SET session_no=? WHERE id=?", (sid, sid))
    conn.commit()
    return sid, sid


def db_close_session(conn, sid, status):
    """Finish a session: write the end time and the final status (closed / crashed)."""
    if conn is None or sid is None:
        return
    conn.execute("UPDATE sessions SET ended_at=?, status=? WHERE id=?",
                 (time.time(), status, sid))
    conn.commit()


def db_add_event(conn, session_id, step, kind, content="", role=None,
                 tool_name=None, tool_call_id=None, tool_args=None, is_error=0,
                 latency_ms=None, tokens_prompt=None, tokens_completion=None):
    """Append an event to the given session, with an auto-incrementing seq."""
    if conn is None:
        return
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE session_id=?",
                       (session_id,)).fetchone()
    conn.execute(
        "INSERT INTO events(session_id, seq, ts, step, kind, role, content,"
        " tool_name, tool_call_id, tool_args, is_error, latency_ms,"
        " tokens_prompt, tokens_completion)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, (row[0] or 0) + 1, time.time(), step, kind, role, content,
         tool_name, tool_call_id, tool_args, 1 if is_error else 0, latency_ms,
         tokens_prompt, tokens_completion))
    conn.commit()
    if kind == "user" and content:
        conn.execute("UPDATE sessions SET title=? WHERE id=? AND title IS NULL",
                     (_first_line(content)[:60], session_id))
        conn.commit()


def db_bump_tokens(conn, session_id, usage, latency_ms):
    """Add this request's token usage to the session, and record latency on the most recent assistant event."""
    if conn is None or session_id is None:
        return
    usage = usage or {}
    conn.execute(
        "UPDATE sessions SET prompt_tokens=prompt_tokens+?, completion_tokens=completion_tokens+? "
        "WHERE id=?",
        (_int(usage.get("prompt_tokens"), 0), _int(usage.get("completion_tokens"), 0), session_id))
    if latency_ms is not None:
        row = conn.execute("SELECT id FROM events WHERE session_id=? AND kind='assistant' "
                           "ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
        if row:
            conn.execute("UPDATE events SET latency_ms=? WHERE id=?", (latency_ms, row[0]))
    conn.commit()


def db_list_sessions(conn, limit=10):
    """List the most recent sessions, for /s to choose from. Returns a list of rows."""
    return conn.execute(
        "SELECT id, session_no, title, started_at, status FROM sessions "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def db_load_session(conn, sid, system_prompt=None):
    """Restore a session into a messages list by seq (including the system prompt).

    Only the first system is taken as the system prompt: the beginning of every loaded session is piled with its own historical
    system/session_begin (both /s and /reset keep appending), and restoring all of them would inflate the history with a pile of
    duplicate system messages. thinking is not stored in the database and not restored; tool_call is restored into the assistant's
    tool_calls, tool_result is restored as a role=tool message, keeping the message history legal.

    Multiple tool_calls in the same step belong to the same assistant message (the model calling in parallel at once),
    so they are merged here by step into one assistant message with several tool_calls, making the restored history shape
    consistent with a live conversation —— otherwise, after loading with /s, each tool_call would occupy its own assistant message and be treated as
    several batches by _trim_history, trimming away part of the calls from the same step.

    Old version databases did not store tool_call_id (all NULL), and restoring directly would emit tool_calls with id null,
    rejected by the API as HTTP 400 "tool_calls.id and tool_calls.type are required". Here the missing
    ids are synthesized and paired up: if an id exists it is reused, if missing a placeholder id is generated (hist_ prefix), and tool_result is backfilled
    by id first, then by "earliest unmatched" order, guaranteeing every tool_call has a result and they correspond one to one;
    orphan tool_results with no matching tool_call are dropped outright, to avoid an illegal message order.
    """
    messages = []
    pending = []          # tool_call ids already restored but without a result yet (FIFO, paired in order of appearance)
    group_step = None     # the step currently being aggregated
    group_msg = None      # the assistant message of the current step (including tool_calls)
    rows = conn.execute(
        "SELECT step, kind, content, tool_call_id, tool_name, tool_args FROM events "
        "WHERE session_id=? AND kind IN "
        "('session_begin','system','user','assistant','tool_call','tool_result','reset','error') "
        "ORDER BY seq, id", (sid,)).fetchall()
    for step, kind, content, tid, name, args in rows:
        # Finishing when leaving the aggregation region of a step (meeting another step), so later tool_calls can start their own message
        stepped = kind in ("assistant", "tool_call")
        if stepped and step != group_step:
            group_step, group_msg = step, None
        if kind == "session_begin":
            continue
        elif kind == "system":
            if system_prompt is None:
                system_prompt = content
        elif kind == "user":
            messages.append({"role": "user", "content": content or ""})
        elif kind == "assistant":
            group_msg = {"role": "assistant", "content": content or "",
                         "tool_calls": []}
            messages.append(group_msg)
        elif kind == "tool_call":
            call_id = tid or ("hist_%d_%d" % (sid, len(messages)))
            pending.append(call_id)
            if group_msg is not None:
                # subsequent calls of the same step are merged into the same assistant message
                group_msg["tool_calls"].append(
                    {"id": call_id, "type": "function",
                     "function": {"name": name or "", "arguments": args or "{}"}})
            else:
                group_msg = {"role": "assistant", "content": "",
                             "tool_calls": [{"id": call_id, "type": "function",
                                             "function": {"name": name or "",
                                                          "arguments": args or "{}"}}]}
                messages.append(group_msg)
        elif kind == "tool_result":
            if tid and tid in pending:
                pending.remove(tid)
                call_id = tid
            elif pending:
                call_id = pending.pop(0)   # id missing/not matching: pair in order with the earliest unmatched call
            else:
                continue                   # no corresponding tool_call, drop it to avoid an illegal message order
            messages.append({"role": "tool", "tool_call_id": call_id, "content": content or ""})
    # assistant messages with only content and no tool calls: drop the empty tool_calls list (keeping it would emit
    # tool_calls: [], which some APIs treat as an illegal field)
    for m in messages:
        if not m.get("tool_calls"):
            m.pop("tool_calls", None)
    # Older versions did not validate arguments when persisting, so the database may hold a tool_call with illegal/truncated
    # arguments (e.g. a truncated output of just "{"). Restoring such a call means HTTP 400 for every request —— strip it at
    # load time so old sessions remain usable (compare: on the persisting side, _parse_tool_calls stops new broken records
    # from being created).
    bad_ids = set()
    for m in messages:
        for tc in m.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            if not fn.get("name") or _tool_args_error(fn.get("arguments")):
                bad_ids.add(tc.get("id"))
    if bad_ids:
        _drop_tool_calls_by_id(messages, bad_ids)
    if system_prompt is not None:
        messages.insert(0, {"role": "system", "content": system_prompt})
    return messages




def choose_session(conn):
    """Interactively list recent sessions and let the user choose. Returns the selected session_id or None."""
    rows = db_list_sessions(conn, 10)
    if not rows:
        sys.stdout.write("no past sessions yet.\n")
        return None
    for sid, no, title, started, status in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(started or 0))
        label = (title or "(untitled)")
        sys.stdout.write("  [%d] %s  %s  (%s)\n" % (sid, label[:40], ts, status))
    try:
        choice = input("which session to load? enter a number (Enter to cancel) > ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\ncancelled.\n")
        return None
    if not choice:
        return None
    try:
        sid = int(choice)
    except ValueError:
        sys.stdout.write("not a valid number.\n")
        return None
    if not any(r[0] == sid for r in rows):
        sys.stdout.write("no such session.\n")
        return None
    return sid


def choose_model(opts):
    """List the models the endpoint supports and let the user pick one by number. Returns the chosen model name or None."""
    ids = fetch_models(opts)
    for i, mid in enumerate(ids):
        sys.stdout.write("  [%d] %s\n" % (i + 1, mid))
    try:
        choice = input("which model? enter a number (Enter to cancel) > ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.stdout.write("\ncancelled.\n")
        return None
    if not choice:
        return None
    try:
        idx = int(choice)
    except ValueError:
        sys.stdout.write("not a valid number.\n")
        return None
    if idx < 1 or idx > len(ids):
        sys.stdout.write("no such model.\n")
        return None
    return ids[idx - 1]


# ---------------------------------------------------------------------------
# Screen display (the log is now written into .ai.db)
# ---------------------------------------------------------------------------

class Output(object):
    """Graded screen display. The complete record is written uniformly into the session database; no text log is written any more."""

    RESET, DIM, RED, CYAN = "\033[0m", "\033[2m", "\033[31m", "\033[36m"

    def __init__(self, level, color, conn, session_id):
        self.level, self.color = level, color
        self.conn, self.session_id = conn, session_id
        self._stream_open = None
        self.step = 0

    def _c(self, text, code):
        return text if not self.color else code + text + self.RESET

    def _w(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()

    # Parameter table for logging regular events: kind -> (db kind, fixed step or None=use current step, role)
    _LOG_MAP = {
        "SESSION BEGIN": ("session_begin", 0, None),
        "SYSTEM": ("system", 0, "system"),
        "USER": ("user", 0, "user"),
        "ASSISTANT": ("assistant", None, "assistant"),
        "TRIM": ("trim", None, None),
        "BAD_TOOL_CALL": ("bad_tool_call", None, None),
        "RESET": ("reset", 0, None),
        "ERROR": ("error", None, None),
        "SESSION END": ("session_end", 0, None),
    }

    # Logging is independent of the display level; the full content is always recorded
    def log(self, kind, text, step=None, tool_call_id=None):
        """Log by event type: the counterpart of the old text-log entries. tool_call_id is for pairing tool_call/tool_result."""
        if self.conn is None or kind == "THINKING":
            return  # no session database / thinking is not stored
        st = self.step if step is None else step
        if kind.startswith("TOOL_CALL "):
            db_add_event(self.conn, self.session_id, st, "tool_call", "",
                         tool_name=kind[len("TOOL_CALL "):], tool_args=text,
                         tool_call_id=tool_call_id)
        elif kind.startswith("TOOL_RESULT "):
            db_add_event(self.conn, self.session_id, st, "tool_result", text, role="tool",
                         tool_name=kind[len("TOOL_RESULT "):], tool_call_id=tool_call_id,
                         is_error=1 if _is_error(text) else 0)
        else:
            entry = self._LOG_MAP.get(kind)
            if entry is None:
                return
            db_kind, fixed_step, role = entry
            db_add_event(self.conn, self.session_id,
                         st if fixed_step is None else fixed_step, db_kind, text, role=role)

    def assistant(self, text, is_final):
        if text and (self.level > LEVEL_QUIET or is_final):
            self._w("\n" + text + "\n")

    # --- Streaming incremental output ---
    def stream_begin(self, kind):
        """Called before a piece of streamed content begins; handles the newline and the colored prefix."""
        if kind == "reasoning":
            if self.level >= LEVEL_VERBOSE:
                self._stream_open = self.DIM
            else:
                self._stream_open = None
        else:
            # content: not printed live at quiet level, left for the final answer to display at once
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
            self._w(self._c("  · %s(%s)" % (name or "?", _brief(raw_args or "{}")), self.DIM) + "\n")

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
    """Build the tool list in the system prompt automatically from TOOLS, so adding a tool does not require changing two places."""
    lines = []
    for t in TOOLS:
        f = t["function"]
        lines.append("- %s: %s" % (f["name"], f["description"].split("。")[0]))
    return "\n".join(lines)


def build_system_prompt(opts):
    prompt = (
        "You are BO, a coding agent running on the user's own machine.\n"
        "working directory: %s\n"
        "system: %s\n"
        "\n"
        "available tools:\n%s\n"
        "\n"
        "approach: locate with search first, then read_file to see the fragment clearly, then write_file (give content to write the whole file, "
        "give old_string + new_string for a partial change), and finally verify with run_command (run tests/compile/execute scripts); "
        "do not guess out of thin air.\n"
        "always create new files with write_file's content mode; do not assemble files with echo/cat redirection via run_command; "
        "delete or move files with rm / mv via run_command, and confirm dangerous operations with the user first.\n"
        "\n"
        "output: this is a plain-text terminal that does not render Markdown. Do not use syntax such as bold, headings or code fences. Answer concisely and directly.\n"
    ) % (os.getcwd(), platform.platform(), tools_hint())

    # When AGENTS.md exists just announce the convention; its content is never injected into the prompt, the model reads it itself with read_file
    agent = find_agent_file()
    if agent:
        prompt += (
            "\nconvention: %s in the current directory is this project's long-term convention/memory file. Before starting work read it with read_file, \n"
            "and work according to what it says; when information worth keeping across sessions comes up, update it with write_file.\n"
        ) % agent
    return prompt


# ---------------------------------------------------------------------------
# Main conversation loop
# ---------------------------------------------------------------------------

def _trim_history(messages, keep_calls=TRIM_KEEP_CALLS):
    """Remove earlier tool calls and results, keeping only the most recent keep_calls batches of calls.

    One batch = an assistant message with tool_calls + the corresponding role=tool results after it.
    What is removed is the tool result messages of earlier batches, and the tool_calls field of assistant messages
    (the whole message is deleted if its content is empty after removal); system and all user/assistant content are kept,
    so the main thread of the conversation stays complete and only old tool round-trips are cleared. Thinking (reasoning) never enters messages,
    so it is naturally neither restored nor trimmed. Returns the number of messages removed.
    """
    batches = []          # (assistant index, list of result indexes)
    cur = None
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant":
            cur = (i, [])
            batches.append(cur)
        elif role == "tool" and cur is not None:
            cur[1].append(i)
    calls = [b for b in batches if messages[b[0]].get("tool_calls")]
    if len(calls) <= keep_calls:
        return 0

    drop = set()
    for ai, results in calls[:len(calls) - keep_calls]:
        drop.update(results)
        m = {k: v for k, v in messages[ai].items() if k != "tool_calls"}
        if (m.get("content") or "").strip():
            messages[ai] = m      # has content, only the tool_calls is stripped
        else:
            drop.add(ai)          # an assistant message with tool calls only and no content is dropped entirely
    if not drop:
        return 0
    merged = []
    for i, m in enumerate(messages):
        if i in drop:
            continue
        if merged and merged[-1].get("role") == "user" and m.get("role") == "user":
            merged[-1] = {"role": "user",
                          "content": (merged[-1].get("content") or "") + "\n\n" + (m.get("content") or "")}
            continue
        merged.append(m)
    dropped = len(messages) - len(merged)
    messages[:] = merged
    return dropped


def _tool_args_error(raw):
    """Check one tool_call's arguments; return None if usable, otherwise a description of the error.

    The API does not validate the content of arguments, and the model occasionally emits a truncated `{`, or a whole XML blob
    (mistaking the tool-call examples in the prompt for its own output). Such a call cannot be executed locally, and once it is
    stored in the database / put into the history, every subsequent request is judged HTTP 400 input_invalid by the API —— so it
    must be rejected before anything is persisted.
    """
    if not isinstance(raw, str) or not raw.strip():
        return "arguments empty"
    try:
        obj = json.loads(raw)
    except ValueError as e:
        return str(e)
    if not isinstance(obj, dict):
        return "arguments is not a JSON object"
    return None


def _parse_tool_calls(tool_calls):
    """Split the tool_calls returned by the model into (usable, unusable).

    Usable is a list of (call, parsed arguments), unusable is a list of calls. A missing tool name/arguments also counts as
    unusable —— such a call likewise cannot be restored into legal tool_calls from the database, so it belongs to the same class
    of "storing it will cause a 400" calls.
    """
    ok, bad = [], []
    for tc in tool_calls or []:
        fn = tc.get("function") if isinstance(tc, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        raw = fn.get("arguments") if isinstance(fn, dict) else None
        if not name:
            bad.append(tc)
            continue
        why = _tool_args_error(raw)
        if why:
            bad.append(tc)
            continue
        ok.append((tc, json.loads(raw)))
    return ok, bad


def _drop_tool_calls_by_id(messages, ids):
    """Strip the tool_calls with the given ids (and their results) in place.

    Fallback: guarantees that neither the tool_calls sent to the API nor those written to the database contain illegal arguments.
    Normally _parse_tool_calls already rejects broken calls before persisting; this is used to clean up old records left in the history.
    The granularity is a single call: if an assistant message still has other calls, only the broken ones are deleted, and the whole
    message is removed only when all that remains is broken calls (and there is no content); the accompanying role=tool results are
    cleared as well, to avoid leaving orphan results with no corresponding call.
    """
    want = set(i for i in ids if i)
    if not want:
        return
    out = []
    for m in messages:
        role = m.get("role")
        if role == "tool" and m.get("tool_call_id") in want:
            continue                              # the result of the broken call is removed as well
        if role == "assistant" and m.get("tool_calls"):
            keep = [tc for tc in m["tool_calls"] if tc.get("id") not in want]
            if len(keep) != len(m["tool_calls"]):
                # must be modified in place (not replaced by a dict copy): the caller may still hold a reference to this message
                if keep:
                    m["tool_calls"] = keep
                elif (m.get("content") or "").strip():
                    m.pop("tool_calls", None)     # has content, only the tool_calls is stripped
                else:
                    continue                      # broken calls only and no content, drop the whole message
        out.append(m)
    messages[:] = out


def _close_dangling_tool_calls(messages, note):
    """Add a result to the batch of tool_calls missing one, keeping the message history legal.

    An interrupt/exception may land outside the tool loop, in which case the assistant's tool_calls are already in the history with nobody answering,
    and the next request would be judged a format error by the API; this fills them in uniformly. Returns how many were added.
    """
    answered = set(m.get("tool_call_id") for m in messages if m.get("role") == "tool")
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending = [tc for tc in m["tool_calls"] if tc.get("id") not in answered]
            for tc in pending:
                messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": note})
            return len(pending)
    return 0


def _close_tool_calls(messages):
    """Add a result in place for every tool_calls missing one, keeping the message history legal. Returns how many were added.

    Dangling results are inserted right after the corresponding assistant message (rather than appended to the end as a whole),
    to avoid scrambling the message order. Later batches in the trimmed history are unaffected.
    """
    answered = set(m.get("tool_call_id") for m in messages if m.get("role") == "tool")
    fixed = 0
    out = []
    for m in messages:
        out.append(m)
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        for tc in m["tool_calls"]:
            if tc.get("id") not in answered:
                out.append({"role": "tool", "tool_call_id": tc.get("id"),
                            "content": "Error: this tool call was interrupted or did not finish executing."})
                fixed += 1
    if fixed:
        messages[:] = out
    return fixed


def load_history(conn, sid, system_prompt):
    """Restore a session from the database and slim it down: like a normal conversation, keep only the most recent tool call.

    Loading with /s and the trimming inside run_turn go through the same _trim_history, guaranteeing that after reloading the history the
    shape sent to the model matches a live conversation; thinking is never stored, so it is not restored either.
    """
    messages = db_load_session(conn, sid, system_prompt)
    dropped = _trim_history(messages)
    if dropped:
        sys.stdout.write("history load trimmed earlier tool calls: removed %d messages.\n" % dropped)
    fixed = _close_tool_calls(messages)
    if fixed:
        sys.stdout.write("history load filled in %d unfinished tool results.\n" % fixed)
    return messages


def run_turn(user_text, messages, opts, out):
    out.log("USER", user_text)
    # The first Ctrl+C within the turn interrupts only the turn (see _handle_sigint); busy is reset by main at the end
    _INTERRUPT["busy"], _INTERRUPT["seen"] = True, False
    # Slim down before each turn starts: keep only the most recent batch of tool calls, remove all earlier calls and results
    dropped = _trim_history(messages)
    if dropped:
        out.log("TRIM", "removed tool calls and results from earlier rounds: %d messages" % dropped)
    messages.append({"role": "user", "content": user_text})
    repeat = {"key": None, "n": 0}  # guard rail against consecutive identical tool calls

    for _ in range(opts["max_steps"]):
        out.step += 1
        # Streaming callbacks: print while receiving; content is always shown live, thinking only at verbose level
        stream_gap = {"content": False}
        cur_kind = {"v": None}

        def on_delta(kind, text):
            if kind == "reasoning" and out.level < LEVEL_VERBOSE:
                return
            if kind == "content" and stream_gap["content"] and cur_kind["v"] == "reasoning":
                out.stream_end()  # start a new line when content appears after thinking
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
            # Interrupted mid-generation: the assistant message has not entered the history yet, just end this turn
            out.info("\n[this generation was interrupted, the turn ends here; press Ctrl+C again to quit the program]")
            return

        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        tool_calls = msg.get("tool_calls") or []

        # The content was already printed live during streaming, so only the log is written here (thinking is skipped at quiet level, the final answer is below)
        if reasoning:
            out.log("THINKING", reasoning.strip())
        if content:
            out.log("ASSISTANT", content)
        db_bump_tokens(out.conn, out.session_id, msg.get("_usage"), msg.get("_latency_ms"))
        # content is empty but nothing was printed at quiet level, show it as a fallback
        if content and out.level == LEVEL_QUIET and not tool_calls:
            out.assistant(content, True)

        # normalize content to a string: the API may return null in the non-streaming fallback, and some servers treat that as a format error
        assistant_msg = {"role": "assistant", "content": msg.get("content") or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)
        if not tool_calls:
            return

        # Parse the arguments and discard calls that fail to parse before persisting or going into the history —— this guarantees
        # that neither the database nor messages ever hold tool_calls with illegal arguments (the direct source of API HTTP 400)
        parsed, bad = _parse_tool_calls(tool_calls)
        if bad:
            # A broken call cannot be executed locally: strip it from the assistant message (not persisted, not put into history), keeping only the usable ones
            bad_ids = set(tc.get("id") for tc in bad)
            keep = [tc for tc in assistant_msg["tool_calls"]
                    if tc.get("id") not in bad_ids]
            if keep:
                assistant_msg["tool_calls"] = keep
            else:
                del assistant_msg["tool_calls"]
                if not (assistant_msg.get("content") or "").strip():
                    messages.pop()          # no content and no calls kept, the whole message is unnecessary
            for tc in bad:
                fn = tc.get("function") or {}
                reason = _tool_args_error(fn.get("arguments"))
                out.tool_call(fn.get("name"), fn.get("arguments"))
                out.tool_result("Error: cannot parse the tool argument JSON: %s" % reason, True)
                out.log("BAD_TOOL_CALL",
                        "%s arguments are not valid JSON, the call was discarded (not persisted, not put into history): %s"
                        % (fn.get("name") or "?", _brief(fn.get("arguments"), 200)),
                        tool_call_id=tc.get("id"))
            out.info("  ! %d tool call(s) had unparseable arguments and were discarded (the conversation continues)" % len(bad))
        if not parsed:
            # the whole batch of calls is unusable: end the turn here (the history holds no broken calls, so the next request is definitely legal)
            return

        aborted = False
        abort_note = abort_info = ""
        for tc, args in parsed:
            name = (tc.get("function") or {}).get("name")
            raw_args = (tc.get("function") or {}).get("arguments")
            # arguments are confirmed legal, only now persist/put into history: whatever is stored can always be replayed
            out.log("TOOL_CALL " + (name or "?"), raw_args, tool_call_id=tc.get("id"))
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
                abort_note = "Error: this turn was aborted due to repeated calls, this call did not execute."
                abort_info = "detected %d consecutive identical calls, this turn was stopped" % MAX_REPEAT_CALLS
                result = ("Error: the identical %s call has appeared %d times in a row, this turn was aborted and this call did not execute."
                          "Please try a different approach, or state your conclusion directly." % (name, repeat["n"]))
            else:
                try:
                    result = execute_tool(name, args, opts)
                except TurnInterrupted:
                    # interrupted halfway through tool execution: the result is untrustworthy, and later calls are not run either
                    result = ("Error: the user interrupted this turn with Ctrl+C, this call did not finish properly;"
                              " do not keep calling tools, wait for the user's next instruction.")
                    aborted = True
                    abort_note = "Error: this turn was interrupted by Ctrl+C, this call did not execute."
                    abort_info = "this turn was interrupted (Ctrl+C); press Ctrl+C again to quit the program"

            out.log("TOOL_RESULT " + (name or "?"), result, tool_call_id=tc.get("id"))
            out.tool_result(result, _is_error(result))
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})

        if aborted:
            out.info("\n[%s]" % abort_info)
            # When the turn aborts there may still be incomplete tool_calls; fill in the results to avoid dangling history
            _close_tool_calls(messages)
            return

    out.info("\n[tool call limit %d for a single turn reached, stopping this turn]" % opts["max_steps"])
    _close_tool_calls(messages)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    env = os.environ.get
    S = argparse.SUPPRESS  # use SUPPRESS to tell whether "the user passed this option explicitly"
    parser = argparse.ArgumentParser(
        description="BO -- a minimal coding agent in a single file, standard library only",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Note: --yes means \"manual confirmation is required before a command runs\". Without --yes commands run by default.\n"
               "Connection-type arguments passed explicitly (-m/--model, -b/--base-url, -k/--api-key, "
               "-s/--max-steps, -t/--http-timeout, -d/--db) are encrypted and recorded in .bo in the user's home directory, "
               "and reused automatically afterwards when no arguments or only some are passed; deleting .bo restores the defaults."
               "Priority: command line > environment variables > .bo > built-in defaults.")
    parser.add_argument("-m", "--model", default=S,
                        help="model name (defaults to the MODEL / OPENAI_MODEL environment variables, then .bo)")
    parser.add_argument("-b", "--base-url", default=S,
                        help="OpenAI-compatible API address (defaults to OPENAI_BASE_URL, then .bo)")
    parser.add_argument("-k", "--api-key", default=S,
                        help="API key (defaults to OPENAI_API_KEY, then .bo)")
    parser.add_argument("-y", "--yes", action="store_true", dest="confirm",
                        help="enable manual confirmation for each command before it runs (without it commands run by default)")
    parser.add_argument("-s", "--max-steps", type=int, default=S, help="max number of tool call rounds per turn")
    parser.add_argument("-t", "--http-timeout", type=int, default=S, help="timeout in seconds for a single HTTP request")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="show only the final answer, hide all tool/thinking output")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="raise the display level: -v shows tool results and thinking, -vv shows full detail")
    parser.add_argument("-C", "--no-color", action="store_true",
                        help="disable colored output (by default color is used only on a terminal)")
    parser.add_argument("-d", "--db", default=S, dest="db_path", metavar="FILE",
                        help="session database file (default .ai.db), the full interaction record is written here")
    parser.add_argument("-l", "--list-models", action="store_true", dest="list_models",
                        help="list the models the endpoint supports, pick one by number, write it to .bo and exit")
    a = parser.parse_args()
    a_vars = vars(a)

    cfg_path = CONFIG_FILE
    cfg, cfg_status = load_config(cfg_path)
    used_cfg = []  # keys actually taken from .bo this time, used for the startup notice

    def pick(name, env_value, default):
        """Resolve a single argument: explicit CLI > environment variables > .bo > built-in default."""
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
    # An empty-string environment variable is treated as "not set", otherwise it would override the default API address
    base_url = pick("base_url", env("OPENAI_BASE_URL") or None, "https://api.openai.com/v1")
    api_key = pick("api_key", env("OPENAI_API_KEY") or None, "")
    max_steps = pick("max_steps", None, 50)
    http_timeout = pick("http_timeout", None, 120)
    db_path = pick("db_path", env("BO_DB") or None, DEFAULT_DB_FILE)
    db_path = os.path.expanduser(db_path)

    # Interactive and display switches take effect for this run only and are not written to .bo
    confirm = bool(a.confirm)
    level = LEVEL_QUIET if a.quiet else min(LEVEL_NORMAL + a.verbose, LEVEL_DEBUG)
    try:
        color = (not a.no_color) and env("NO_COLOR") is None and sys.stdout.isatty()
    except Exception:
        color = False

    # Only connection-type arguments passed explicitly this time are written back to .bo, keeping the other keys in the file (key names as in CONFIG_KEYS)
    resolved = {"model": model, "base_url": base_url, "api_key": api_key,
                "max_steps": max_steps, "http_timeout": http_timeout, "db_path": db_path}
    updates = {k: resolved[k] for k in CONFIG_KEYS if k in a_vars}

    config_saved, config_error = False, None
    if updates:
        merged = dict(cfg)
        merged.update(updates)
        if merged != cfg:  # do not rewrite when nothing changed, to avoid pointless file modification
            config_error = save_config(merged, cfg_path)
            config_saved = config_error is None

    # -l only does "list models -> pick a number -> save", equivalent to passing -m once, then exits
    if a.list_models:
        try:
            chosen = choose_model({"base_url": base_url, "api_key": api_key,
                                   "http_timeout": http_timeout})
        except RuntimeError as e:
            sys.stderr.write("failed to fetch the model list: %s\n" % e)
            sys.exit(1)
        except Exception as e:
            sys.stderr.write("failed to fetch the model list: %s: %s\n" % (type(e).__name__, e))
            sys.exit(1)
        if chosen is None:
            sys.stdout.write("model unchanged.\n")
            sys.exit(0)
        merged = dict(cfg)      # start from the connection options just saved this run, so they are not overwritten
        merged.update(updates)
        merged["model"] = chosen
        err = save_config(merged, cfg_path)
        if err:
            sys.stderr.write("warning: failed to write %s: %s\n" % (cfg_path, err))
            sys.exit(1)
        sys.stdout.write("model switched to %s, written to %s.\n" % (chosen, CONFIG_FILE))
        sys.exit(0)

    return {
        "model": model, "base_url": base_url, "api_key": api_key,
        "confirm": confirm,
        "max_steps": max_steps, "http_timeout": http_timeout, "cwd": os.getcwd(),
        "level": level, "color": color, "db_path": db_path,
        "config_status": cfg_status, "config_saved": config_saved, "config_error": config_error,
        "config_used": bool(used_cfg),
    }


def setup_stdio():
    """Make stdin/stdout/stderr use UTF-8, so that Chinese does not raise UnicodeEncodeError under a C/POSIX locale.

    Python 3.7+ has reconfigure; 3.6 does not, so it can only wrap a TextIOWrapper itself.
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


def _new_session(opts, out):
    """Close the old session and start a new one (shared by process startup and /reset)."""
    db_close_session(out.conn, out.session_id, "closed")
    sid, _no = db_new_session(out.conn, opts)
    out.session_id = sid
    out.step = 0


def main():
    setup_stdio()
    install_sigint_handler()

    opts = parse_args()
    conn = db_open(opts["db_path"])
    if conn is None:
        sys.stderr.write("cannot use the session database, exiting.\n")
        sys.exit(1)
    out = Output(opts["level"], opts["color"], conn, None)
    _new_session(opts, out)
    system_prompt = build_system_prompt(opts)

    sys.stdout.write(
        "BO -- minimal coding agent (Python %s, %s)\n"
        "model: %s\nAPI: %s\ncommand confirmation: %s\ndisplay level: %s\nsession: #%d\n"
        % (platform.python_version(), platform.system(), opts["model"], opts["base_url"],
           "on (--yes)" if opts["confirm"] else "off (run by default)",
           LEVEL_NAMES[opts["level"]], out.session_id))
    sys.stdout.write("session database: %s\n" % opts["db_path"])
    if opts["config_status"] == "invalid":
        sys.stderr.write("warning: %s exists but cannot be decrypted/parsed (or was not generated on this machine), ignored\n" % CONFIG_FILE)
    if opts["config_used"]:
        sys.stdout.write("argument memory: read from %s\n" % CONFIG_FILE)
    if opts["config_saved"]:
        sys.stdout.write("argument memory: written to %s\n" % CONFIG_FILE)
    elif opts["config_error"]:
        sys.stderr.write("warning: writing %s failed: %s\n" % (CONFIG_FILE, opts["config_error"]))
    sys.stdout.write("type /help for help, exit to quit.\n")

    out.log("SESSION BEGIN", "no: %d\nmodel=%s\nbase_url=%s\ncwd=%s\nlevel=%s" % (
        out.session_id, opts["model"], opts["base_url"], opts["cwd"], opts["level"]))
    out.log("SYSTEM", system_prompt)
    messages = [{"role": "system", "content": system_prompt}]
    started = time.time()

    try:
        while True:
            try:
                user = input("\nyou > ").strip()
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\nbye.\n")
                break
            if not user:
                continue
            if user in ("exit", "quit", "/exit", "/quit"):
                sys.stdout.write("bye.\n")
                break
            if user == "/reset":
                _new_session(opts, out)
                messages = [{"role": "system", "content": system_prompt}]
                out.log("RESET", "conversation history cleared, a new session was started")
                sys.stdout.write("cleared the conversation history, started new session #%d.\n" % out.session_id)
                continue
            if user == "/s":
                try:
                    sid = choose_session(out.conn)
                except Exception as e:      # do not crash the whole program when input is interrupted/errors out
                    out.log("ERROR", "/s session selection failed: %s" % e)
                    sys.stdout.write("session selection failed: %s\n" % e)
                    continue
                if sid is not None:
                    loaded = load_history(out.conn, sid, system_prompt)
                    if loaded:
                        # First finish the current session and the empty shell just created, then switch back to continue the loaded session
                        prev = out.session_id
                        _new_session(opts, out)
                        db_close_session(out.conn, out.session_id, "closed")
                        out.session_id = sid
                        out.step = 0
                        db_close_session(out.conn, sid, "running")
                        if prev != sid:
                            out.log("RESET", "loaded session #%d (%d messages), current session #%d was finished"
                                    % (sid, len(loaded), prev))
                        messages[:] = loaded
                        out.log("RESET", "loaded session #%d (%d messages)" % (sid, len(loaded)))
                        sys.stdout.write("loaded session #%d, continue the conversation.\n" % sid)
                    else:
                        sys.stdout.write("that session has nothing to load.\n")
                continue
            if user == "/help":
                sys.stdout.write("available interaction commands:\n"
                                 "  /reset   clear the conversation history, start a new session\n"
                                 "  /s       load and continue a past session\n"
                                 "  /help    show this help\n"
                                 "  exit     quit\n"
                                 "  Ctrl+C   the first press only interrupts the current turn (generation/command), press again to quit\n")
                continue

            try:
                run_turn(user, messages, opts, out)
            except KeyboardInterrupt:   # Ctrl+C pressed twice within the turn
                sys.stdout.write("\nbye.\n")
                break
            except TurnInterrupted:     # fallback: the interrupt landed outside the generation/tool loop
                _close_dangling_tool_calls(
                    messages, "Error: this turn was interrupted by Ctrl+C, this call did not execute.")
                out.info("\n[this turn was interrupted; press Ctrl+C again to quit the program]")
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
        # fallback: pressing Ctrl+C again during the error/interrupt cleanup, to avoid throwing a raw traceback
        sys.stdout.write("\nbye.\n")
    finally:
        out.log("SESSION END", "no: %d\nelapsed: %.1fs" % (out.session_id, time.time() - started))
        db_close_session(out.conn, out.session_id, "closed")
        try:
            out.conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()




