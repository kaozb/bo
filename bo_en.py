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
    --model / --base-url / --api-key   override the corresponding environment variable
    --yes               ask for manual confirmation before each command (without it, commands run directly)
    --root DIR          restrict read_file/edit_file to DIR only (unrestricted by default)
    --max-steps N       maximum number of tool-call rounds per turn (default 50)
    --http-timeout N    timeout in seconds for a single request (default 120)
    -q                  show only the final answer, hide all tool/thinking output
    -v / -vv            show tool results and thinking / full detail
    --no-color          disable colored output
    -l, --log [FILE]    write the full interaction to a log file (default BO.log), no detail on screen

Interaction: /reset clears the conversation, /help shows help, exit quits
"""

import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
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
AGENT_FILE = "AGENTS.md"             # convention file in the current directory (case-insensitive); if present, the model is told to read it

# Screen verbosity levels
LEVEL_QUIET, LEVEL_NORMAL, LEVEL_VERBOSE, LEVEL_DEBUG = 0, 1, 2, 3
LEVEL_NAMES = ("quiet (final answer only)", "normal (one-line tool call notice)",
               "verbose (tool results and thinking included)", "debug (full detail)")


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


TOOLS = [
    _fn("read_file", "Read the text content of a file and return it with line numbers. Use offset/limit to read a fragment.",
        {"path": _p("string", "File path (relative or absolute)"),
         "offset": _p("integer", "Starting line number, 0-based, default 0"),
         "limit": _p("integer", "Maximum number of lines to read, default 2000")},
        ["path"]),
    _fn("edit_file", "Perform an exact string replacement in a file. old_string must occur exactly once in the file, otherwise an error is returned. Use this to modify an existing file.",
        {"path": _p("string", "File path"),
         "old_string": _p("string", "The original text to replace; must be unique in the file"),
         "new_string": _p("string", "The new text after replacement")},
        ["path", "old_string", "new_string"]),
    _fn("run_command", "Execute a command in the shell and return the exit code plus the merged stdout+stderr.",
        {"command": _p("string", "The shell command to execute"),
         "timeout": _p("integer", "Timeout in seconds, default 120")},
        ["command"]),
]


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def _decode_bytes(data):
    """Decode bytes into text; return (text, actual encoding); lossless when possible."""
    if not data:
        return "", "utf-8"
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc), enc
        except Exception:
            continue
    return data.decode("utf-8", "replace"), "utf-8"


def _decode(data):
    return _decode_bytes(data)[0]


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


def _atomic_write(path, data):
    """Atomic write: write a temp file in the same directory, fsync, keep the original permissions, then os.replace."""
    target = os.path.realpath(path)
    mode = os.stat(target).st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".", prefix=".bo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


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
# Tool implementations
# ---------------------------------------------------------------------------

def tool_read_file(args, opts):
    path, err = _resolve(args.get("path"), opts)
    if err:
        return err
    raw, err = _read_file_bytes(path)
    if err:
        return err

    offset = _int(args.get("offset"), 0, minimum=0)
    limit = _int(args.get("limit"), 2000, minimum=1)
    lines = _decode(raw).splitlines()
    total = len(lines)
    if total == 0:
        return "File %s is empty." % args.get("path")
    if offset >= total:
        return "File %s has %d lines; offset=%d is past the end of the file." % (args.get("path"), total, offset)
    chunk = lines[offset:offset + limit]
    header = "File %s has %d lines, showing lines %d-%d:" % (
        args.get("path"), total, offset + 1, min(offset + limit, total))
    body = "\n".join(
        "%6d\t%s" % (offset + i + 1, _truncate(ln, MAX_LINE_CHARS, note="... [this line was truncated]"))
        for i, ln in enumerate(chunk))
    return header + "\n" + body


def tool_edit_file(args, opts):
    old, new = args.get("old_string"), args.get("new_string")
    if old is None or new is None:
        return "Error: path / old_string / new_string are all required"
    if old == new:
        return "Error: old_string and new_string are identical, nothing to change"
    path, err = _resolve(args.get("path"), opts)
    if err:
        return err
    raw, err = _read_file_bytes(path)
    if err:
        return err
    text, enc = _decode_bytes(raw)

    count = text.count(old)
    if count == 0:
        return "Error: old_string not found. Make sure the original text matches exactly (including spaces and indentation)"
    if count > 1:
        return "Error: old_string occurs %d times in the file, not unique. Add more context to make it unique." % count

    try:
        data = text.replace(old, new, 1).encode(enc)
    except Exception as e:
        return "Error: the new content cannot be saved with the original encoding (%s): %s" % (enc, e)
    try:
        _atomic_write(path, data)
    except Exception as e:
        return "Error: write failed: %s" % e
    return "Modified %s (1 replacement)" % args.get("path")


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


def tool_run_command(args, opts):
    command = args.get("command")
    if not command:
        return "Error: missing command argument"
    timeout = _int(args.get("timeout"), 120, minimum=1)

    if opts["confirm"]:
        sys.stdout.write("\n[Command to run] %s\nExecute? [y/N] " % command)
        sys.stdout.flush()
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes"):
            return "User declined to run the command."

    popen_kwargs = {"shell": True, "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT, "cwd": opts["cwd"]}
    if hasattr(os, "killpg"):
        # make the child its own process group leader so the whole group can be killed on timeout
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except Exception as e:
        return "Error: cannot start the command: %s" % e

    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_group(proc)
        out, _ = proc.communicate()
        return "Command timed out (> %ds) and was terminated (including spawned processes). Output produced so far:\n%s" % (
            timeout, _truncate(_decode(out)))

    text = _decode(out)
    return "Exit code: %d\nOutput:\n%s" % (proc.returncode, _truncate(text) if text.strip() else "(no output)")


def execute_tool(name, args, opts):
    if name == "read_file":
        return tool_read_file(args, opts)
    if name == "edit_file":
        return tool_edit_file(args, opts)
    if name == "run_command":
        return tool_run_command(args, opts)
    return "Error: unknown tool %s" % name


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

def call_llm(messages, opts):
    payload = {"model": opts["model"], "messages": messages,
               "tools": TOOLS, "tool_choice": "auto"}
    url = opts["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    if opts["api_key"]:
        req.add_header("Authorization", "Bearer " + opts["api_key"])

    try:
        resp = urllib.request.urlopen(req, timeout=opts["http_timeout"])
        try:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
        finally:
            resp.close()
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s error: %s" % (e.code, _truncate(_decode(e.read()), 2000)))
    except urllib.error.URLError as e:
        raise RuntimeError("Network error: %s" % e.reason)

    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Response too large (> %d MB), rejected" % (MAX_RESPONSE_BYTES // 1048576))
    try:
        obj = json.loads(_decode(body))
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


def build_system_prompt(opts):
    prompt = (
        "You are BO, a coding agent running on the user's own machine.\n"
        "Working directory: %s\n"
        "System: %s\n"
        "\n"
        "Tools: read_file (read a file, with line numbers) / edit_file (precise replacement after old_string matches exactly once) / run_command (execute a shell command).\n"
        "To see content use read_file first, to change a file use edit_file, and use run_command to verify; do not guess.\n"
        "\n"
        "Output: this is a plain text terminal, Markdown is not rendered. Do not use bold, headings, code fences or similar syntax. Answer in English, concisely and directly.\n"
    ) % (os.getcwd(), platform.platform())

    # when AGENTS.md exists just mention the convention; its content is never injected into the prompt,
    # the model reads it itself with read_file
    agent = find_agent_file()
    if agent:
        prompt += (
            "\nConvention: %s in the current directory is this project's long-term convention/memory file. "
            "read_file it before starting work and follow what it says; when information needs to be kept "
            "across sessions, update it with edit_file.\n"
        ) % agent
    return prompt


# ---------------------------------------------------------------------------
# Main conversation loop
# ---------------------------------------------------------------------------

def run_turn(user_text, messages, opts, out):
    out.log("USER", user_text)
    messages.append({"role": "user", "content": user_text})

    for _ in range(opts["max_steps"]):
        msg = call_llm(messages, opts)
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        tool_calls = msg.get("tool_calls") or []

        if reasoning:
            out.log("THINKING", reasoning.strip())
            out.reasoning(reasoning.strip())
        if content:
            out.log("ASSISTANT", content)
            out.assistant(content, not tool_calls)

        assistant_msg = {"role": "assistant", "content": msg.get("content")}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)
        if not tool_calls:
            return

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name, raw_args = fn.get("name"), fn.get("arguments") or "{}"
            out.log("TOOL_CALL " + (name or "?"), raw_args)
            out.tool_call(name, raw_args)
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(args, dict):
                    args = {}
            except ValueError as e:
                result = "Error: cannot parse the tool argument JSON: %s" % e
            else:
                result = execute_tool(name, args, opts)

            out.log("TOOL_RESULT " + (name or "?"), result)
            out.tool_result(result, _is_error(result))
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})

    out.info("\n[Tool call limit of %d reached for this turn, stopping]" % opts["max_steps"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description="BO -- a single-file, standard-library-only minimal coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Note: --yes means \"ask for manual confirmation before each command\". Without --yes commands run directly.")
    parser.add_argument("--model", default=env("MODEL") or env("OPENAI_MODEL") or "gpt-4o-mini",
                        help="model name (defaults to the MODEL / OPENAI_MODEL environment variable)")
    parser.add_argument("--base-url", default=env("OPENAI_BASE_URL") or "https://api.openai.com/v1",
                        help="OpenAI-compatible endpoint URL (defaults to OPENAI_BASE_URL)")
    parser.add_argument("--api-key", default=env("OPENAI_API_KEY") or "",
                        help="API key (defaults to OPENAI_API_KEY)")
    parser.add_argument("--yes", action="store_true",
                        help="ask for manual confirmation before each command (without it commands run directly)")
    parser.add_argument("--root", default=None, metavar="DIR",
                        help="restrict read_file/edit_file to this directory only (unrestricted by default)")
    parser.add_argument("--max-steps", type=int, default=50, help="maximum number of tool-call rounds per turn")
    parser.add_argument("--http-timeout", type=int, default=120, help="timeout in seconds for a single HTTP request")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="show only the final answer, hide all tool/thinking output")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="raise the display level: -v shows tool results and thinking, -vv shows full detail")
    parser.add_argument("--no-color", action="store_true",
                        help="disable colored output (colors are used only on a terminal by default)")
    parser.add_argument("-l", "--log", nargs="?", const="BO.log", default=None,
                        help="write the full interaction to a log file (default BO.log), no detail on screen")
    a = parser.parse_args()

    try:
        color = (not a.no_color) and env("NO_COLOR") is None and sys.stdout.isatty()
    except Exception:
        color = False

    return {
        "model": a.model, "base_url": a.base_url, "api_key": a.api_key,
        "confirm": a.yes, "root": os.path.realpath(a.root) if a.root else None,
        "max_steps": a.max_steps, "http_timeout": a.http_timeout, "cwd": os.getcwd(),
        "level": LEVEL_QUIET if a.quiet else min(LEVEL_NORMAL + a.verbose, LEVEL_DEBUG),
        "color": color, "log_path": a.log,
    }


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

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
                                 "  exit     quit\n")
                continue

            try:
                run_turn(user, messages, opts, out)
            except RuntimeError as e:
                out.log("ERROR", str(e))
                out.error(str(e))
            except Exception as e:
                msg = "%s: %s" % (type(e).__name__, e)
                out.log("ERROR", msg)
                out.error(msg)
    finally:
        out.log("SESSION END", "number: %d\nduration: %.1fs" % (out.session_id, time.time() - started))
        out.close()


if __name__ == "__main__":
    main()
