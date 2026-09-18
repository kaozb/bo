#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BO —— 单文件、纯标准库的最小编码智能体

只要设备能跑 Python 3.6+，本脚本就能运行，无需安装任何第三方库。
后端使用 OpenAI 兼容接口（/v1/chat/completions），可对接 OpenAI、DeepSeek、
通义、vLLM、Ollama、LM Studio 等任何兼容服务。

用法:
    export OPENAI_BASE_URL=https://api.deepseek.com/v1
    export OPENAI_API_KEY=sk-xxxx
    export MODEL=deepseek-chat
    python BO.py

参数:
    -m, --model / --base-url / --api-key   覆盖对应环境变量
    --yes               命令执行前逐条人工确认（不加则默认直接放行）
    --root DIR          限制 read_file/edit_file 只能访问 DIR 之内（默认不限制）
    --max-steps N       单轮最多工具调用轮数（默认 50）
    --http-timeout N    单次请求超时秒数（默认 120）
    -q                  只显示最终答复，隐藏全部工具/思考过程
    -v / -vv            显示工具结果与思考 / 完整明细
    --no-color          关闭彩色输出
    -l, --log [FILE]    完整交互写入日志（默认 BO.log），屏幕上不显示明细
    --no-log            撤销已记录的 -l/--log，停止写日志

参数记忆: 显式传入的连接类参数（-m / --base-url / --api-key / --root / --max-steps /
          --http-timeout / -l）会加密记录到当前目录的 .bo，之后不传参或只传部分参数时自动复用；
          --yes / -q / -v / --no-color 等交互与显示开关仅本次生效，不写入该文件。
          优先级: 命令行 > 环境变量 > .bo > 内置默认；删除 .bo 即恢复默认。

交互: /reset 清空对话，/help 帮助，exit 退出
"""

import argparse
import base64
import hashlib
import hmac
import io
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
    import readline  # noqa: F401  存在时启用输入历史，不存在则忽略
except Exception:
    pass

MAX_OUTPUT_CHARS = 30000             # 单个工具结果进入上下文的最大字符数
MAX_LINE_CHARS = 2000                # read_file 单行最大显示字符数
MAX_READ_BYTES = 10 * 1024 * 1024    # 文件读写大小上限，防止内存被撑爆
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 单次 HTTP 响应大小上限
AGENT_FILE = "AGENTS.md"             # 当前目录下的约定文件（不区分大小写），存在则提示模型自行读取
CONFIG_FILE = ".bo"                  # 当前目录下的参数记忆文件（加密），显式传参时写入、无参时复用
# 只有这些「连接/运行类」参数会写入 .bo；交互与显示开关（--yes / -q / -v / --no-color）仅本次生效
CONFIG_KEYS = ("model", "base_url", "api_key", "root", "max_steps", "http_timeout", "log_path")

# 屏幕展示分级
LEVEL_QUIET, LEVEL_NORMAL, LEVEL_VERBOSE, LEVEL_DEBUG = 0, 1, 2, 3
LEVEL_NAMES = ("quiet (仅最终答复)", "normal (工具调用一行提示)",
               "verbose (含工具结果与思考)", "debug (完整明细)")


# ---------------------------------------------------------------------------
# 工具定义（OpenAI function calling 格式）
# ---------------------------------------------------------------------------

def _p(kind, desc):
    """一个 JSON Schema 属性。"""
    return {"type": kind, "description": desc}


def _fn(name, desc, props, required):
    """一个 function 类型的工具定义。"""
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    _fn("read_file", "读取文件文本内容，返回带行号的结果。可用 offset/limit 读取片段。",
        {"path": _p("string", "文件路径（相对或绝对）"),
         "offset": _p("integer", "起始行号，从 0 开始，默认 0"),
         "limit": _p("integer", "最多读取行数，默认 2000")},
        ["path"]),
    _fn("edit_file", "对文件做精确字符串替换。old_string 必须在文件中唯一出现，否则报错。用于修改已有文件。",
        {"path": _p("string", "文件路径"),
         "old_string": _p("string", "被替换的原文，需在文件中唯一"),
         "new_string": _p("string", "替换后的新文本")},
        ["path", "old_string", "new_string"]),
    _fn("run_command", "在 shell 中执行命令，返回退出码与合并后的 stdout+stderr。",
        {"command": _p("string", "要执行的 shell 命令"),
         "timeout": _p("integer", "超时秒数，默认 120")},
        ["command"]),
]


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------

def _decode_bytes(data):
    """把 bytes 解码为文本，返回 (文本, 实际编码)；尽量无损。"""
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
    """截断文本；note 中若含 %d 会被替换为原文长度。"""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    note = note or "\n... [已截断，原文共 %d 字符]"
    try:
        return text[:limit] + note % len(text)
    except TypeError:
        return text[:limit] + note


def _indent(text, prefix="    ", limit=MAX_OUTPUT_CHARS):
    text = _truncate(text or "", limit, note="\n... [显示已省略]")
    return "\n".join(prefix + ln for ln in text.splitlines())


def _brief(raw, n=120):
    s = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


def _first_line(text):
    return text.strip().splitlines()[0][:200] if text and text.strip() else "(空)"


def _int(value, default, minimum=None):
    """把工具参数转成 int；非法或越界时回落到默认值。"""
    try:
        n = int(value)
    except Exception:
        return default
    if minimum is not None and n < minimum:
        return default
    return n


def _is_error(result):
    """判断工具结果是否算失败（normal 级别下只提示错误）。"""
    head = (result or "").lstrip()
    if head.startswith(("错误", "命令超时", "用户拒绝")):
        return True
    if head.startswith("退出码:"):
        try:
            return int(head.split(":", 1)[1].strip().splitlines()[0]) != 0
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# 文件访问（含路径限制与原子写入）
# ---------------------------------------------------------------------------

def _resolve(path, opts):
    """校验路径，返回 (真实路径, 错误信息)。受 --root 限制时越界即报错。"""
    if not path:
        return None, "错误: 缺少 path 参数"
    real = os.path.realpath(path)
    root = opts.get("root")
    if root and real != root and not real.startswith(root + os.sep):
        return None, "错误: 路径越出允许范围（--root %s）: %s" % (root, path)
    return real, None


def _read_file_bytes(path):
    """读取整个文件，带大小上限。返回 (bytes, 错误信息)。"""
    if not os.path.exists(path):
        return None, "错误: 文件不存在: %s" % path
    if not os.path.isfile(path):
        return None, "错误: 不是普通文件（目录/设备文件等）: %s" % path
    size = os.path.getsize(path)
    if size > MAX_READ_BYTES:
        return None, "错误: 文件过大（%.1f MB，上限 %d MB），请用 run_command 配合 head/tail/sed 处理" % (
            size / 1048576.0, MAX_READ_BYTES // 1048576)
    try:
        with open(path, "rb") as f:
            return f.read(), None
    except Exception as e:
        return None, "错误: 读取失败: %s" % e


def _atomic_write(path, data):
    """原子写入：先写同目录临时文件并 fsync，保留原权限，再 os.replace 覆盖。"""
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
    """在当前目录不区分大小写地查找 AGENTS.md，返回实际文件名或 None。"""
    want = AGENT_FILE.lower()
    try:
        for name in sorted(os.listdir(".")):
            if name.lower() == want and os.path.isfile(name):
                return name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# .bo 参数记忆（简单加密：目录路径派生密钥流 XOR + HMAC-SHA256 校验）
# ---------------------------------------------------------------------------

_CFG_MAGIC = b"BOCFG1"
_CFG_MAX_BYTES = 1 << 20             # .bo 大小上限 1MB


def _cfg_keys(path):
    """由 .bo 所在目录的真实路径派生 (密钥流密钥, 校验密钥)；文件因此与目录绑定。"""
    base = os.path.dirname(os.path.realpath(path)).encode("utf-8")
    return (hashlib.sha256(b"bo-config-stream-v1|" + base).digest(),
            hashlib.sha256(b"bo-config-mac-v1|" + base).digest())


def _cfg_keystream(key, n):
    """用密钥派生 n 字节密钥流（SHA-256 计数器模式）。"""
    out, i = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(key + i.to_bytes(4, "big")).digest()
        i += 1
    return bytes(out[:n])


def _cfg_xor(data, key):
    return bytes(b ^ k for b, k in zip(bytearray(data), _cfg_keystream(key, len(data))))


def _encrypt_config(raw_bytes, stream_key, mac_key):
    """明文 -> base64(魔数 + HMAC-SHA256 + XOR 密文)。"""
    body = _cfg_xor(raw_bytes, stream_key)
    mac = hmac.new(mac_key, _CFG_MAGIC + body, hashlib.sha256).digest()
    return base64.b64encode(_CFG_MAGIC + mac + body) + b"\n"


def _decrypt_config(raw, stream_key, mac_key):
    """解密；base64/魔数/HMAC/编码任一步失败返回 None（视为无有效配置）。"""
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
    """读取并解密 .bo。返回 (dict, status)，status ∈ 'missing' / 'ok' / 'invalid'。"""
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
    """加密写入 .bo（0600、原子替换）。返回错误信息，成功返回 None。"""
    stream_key, mac_key = _cfg_keys(path)
    try:
        data = _encrypt_config(
            json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            stream_key, mac_key)
    except Exception as e:
        return "序列化失败: %s" % e
    target = os.path.realpath(path)
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".",
                                   prefix=".bo-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception as e:
        return "写入失败: %s" % e
    return None


# ---------------------------------------------------------------------------
# 工具实现
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
        return "文件 %s 是空文件。" % args.get("path")
    if offset >= total:
        return "文件 %s 共 %d 行，offset=%d 已超出文件末尾。" % (args.get("path"), total, offset)
    chunk = lines[offset:offset + limit]
    header = "文件 %s 共 %d 行，显示第 %d-%d 行:" % (
        args.get("path"), total, offset + 1, min(offset + limit, total))
    body = "\n".join(
        "%6d\t%s" % (offset + i + 1, _truncate(ln, MAX_LINE_CHARS, note="... [本行已截断]"))
        for i, ln in enumerate(chunk))
    return header + "\n" + body


def tool_edit_file(args, opts):
    old, new = args.get("old_string"), args.get("new_string")
    if old is None or new is None:
        return "错误: 需要 path / old_string / new_string 三个参数"
    if old == new:
        return "错误: old_string 与 new_string 相同，无需修改"
    path, err = _resolve(args.get("path"), opts)
    if err:
        return err
    raw, err = _read_file_bytes(path)
    if err:
        return err
    text, enc = _decode_bytes(raw)

    count = text.count(old)
    if count == 0:
        return "错误: 未找到 old_string。请确认原文完全一致（含空格与缩进）"
    if count > 1:
        return "错误: old_string 在文件中出现 %d 次，不唯一。请补充上下文使其唯一。" % count

    try:
        data = text.replace(old, new, 1).encode(enc)
    except Exception as e:
        return "错误: 新内容无法用原编码(%s)保存: %s" % (enc, e)
    try:
        _atomic_write(path, data)
    except Exception as e:
        return "错误: 写入失败: %s" % e
    return "已修改 %s（替换 1 处）" % args.get("path")


def _terminate_group(proc):
    """杀掉整个进程组，清理命令派生的孙进程；不支持进程组时退回单进程。"""
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
        return "错误: 缺少 command 参数"
    timeout = _int(args.get("timeout"), 120, minimum=1)

    if opts["confirm"]:
        sys.stdout.write("\n[待执行命令] %s\n确认执行? [y/N] " % command)
        sys.stdout.flush()
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans not in ("y", "yes"):
            return "用户拒绝执行该命令。"

    popen_kwargs = {"shell": True, "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT, "cwd": opts["cwd"]}
    if hasattr(os, "killpg"):
        # 让子进程成为独立进程组组长，超时时可整组击杀
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except Exception as e:
        return "错误: 无法启动命令: %s" % e

    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_group(proc)
        out, _ = proc.communicate()
        return "命令超时（> %ds）已被终止（含派生进程）。已产生的输出:\n%s" % (
            timeout, _truncate(_decode(out)))

    text = _decode(out)
    return "退出码: %d\n输出:\n%s" % (proc.returncode, _truncate(text) if text.strip() else "(无输出)")


def execute_tool(name, args, opts):
    if name == "read_file":
        return tool_read_file(args, opts)
    if name == "edit_file":
        return tool_edit_file(args, opts)
    if name == "run_command":
        return tool_run_command(args, opts)
    return "错误: 未知工具 %s" % name


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------

def _merge_tool_call_delta(acc, delta):
    """把流式返回的一块 tool_calls 分片合并进累积结果。

    分片可能只带 index/id/function.name/function.arguments 的一部分，需按 index 归并。
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
    """流式请求 /chat/completions，返回聚合后的 message 字典。

    on_delta(kind, text)：kind 为 "content" 或 "reasoning"，text 为本次新增文本。
    若服务端不支持流式（未返回 data: 行），自动回退到一次性 JSON 解析。
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
        raise RuntimeError("HTTP %s 错误: %s" % (e.code, _truncate(_decode(e.read()), 2000)))
    except urllib.error.URLError as e:
        raise RuntimeError("网络错误: %s" % e.reason)

    try:
        for raw in resp:
            total += len(raw)
            if total > MAX_RESPONSE_BYTES:
                raise RuntimeError("响应过大（> %d MB），已拒绝" % (MAX_RESPONSE_BYTES // 1048576))
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
                raise RuntimeError("接口返回错误: %s" % chunk["error"])
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
        raise RuntimeError("读取流式响应失败: %s" % e)
    finally:
        resp.close()

    if not saw_sse:
        # 服务端未按 SSE 返回（有的兼容实现忽略 stream=true），回退为整体解析
        return _parse_full_response("".join(raw_lines))

    msg = {"role": "assistant", "content": "".join(content_parts)}
    reasoning = "".join(reasoning_parts)
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _parse_full_response(text):
    """非流式回退：解析完整 JSON 响应，取出 message。"""
    try:
        obj = json.loads(text)
    except Exception as e:
        raise RuntimeError("响应不是合法 JSON: %s" % e)
    if isinstance(obj, dict) and obj.get("error"):
        raise RuntimeError("接口返回错误: %s" % obj["error"])
    try:
        return obj["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("响应结构异常: %s" % _truncate(json.dumps(obj, ensure_ascii=False), 2000))


# ---------------------------------------------------------------------------
# 屏幕展示与日志
# ---------------------------------------------------------------------------

SESSION_MARK = "] SESSION BEGIN "


def _count_sessions(path):
    """统计日志里已有的会话数，用于给本次会话编号。"""
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
    """屏幕分级展示 + 完整日志落盘。"""

    RESET, DIM, RED, CYAN = "\033[0m", "\033[2m", "\033[31m", "\033[36m"

    def __init__(self, level, color, log_path):
        self.level, self.color, self.log_path = level, color, log_path
        self._log = None
        self._stream_open = None
        self.session_id = 0
        if log_path:
            self.session_id = _count_sessions(log_path)
            try:
                # 日志可能含命令输出乃至密钥，按 0600 创建，仅本人可读
                fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                self._log = os.fdopen(fd, "a", encoding="utf-8")
            except Exception as e:
                sys.stderr.write("无法打开日志文件 %s: %s\n" % (log_path, e))
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

    # 日志与显示级别无关，始终记录完整内容；每条记录用 END 标记收尾，便于在长内容中定位
    def log(self, kind, text):
        if self._log is None:
            return
        self._log.write("\n===== [%s] %s =====\n%s\n===== END %s =====\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), kind, text if text else "(空)", kind))
        self._log.flush()

    def assistant(self, text, is_final):
        if text and (self.level > LEVEL_QUIET or is_final):
            self._w("\n" + text + "\n")

    # --- 流式增量输出 ---
    def stream_begin(self, kind):
        """一段流式内容开始前调用，负责换行与着色前缀。"""
        if kind == "reasoning":
            if self.level >= LEVEL_VERBOSE:
                self._stream_open = self.DIM
            else:
                self._stream_open = None
        else:
            # 正文：quiet 级别下不实时打印，留给最终答复统一显示
            self._stream_open = None if self.level == LEVEL_QUIET else ""
        if self._stream_open is not None:
            sys.stdout.write("\n")
            if self._stream_open:
                sys.stdout.write(self._stream_open)
            sys.stdout.flush()

    def stream_delta(self, text):
        if self._stream_open is None:
            return
        sys.stdout.write(text)
        sys.stdout.flush()

    def stream_end(self):
        if self._stream_open is None:
            return
        if self._stream_open:
            sys.stdout.write(self.RESET)
        sys.stdout.write("\n")
        sys.stdout.flush()
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
        self._w(self._c("\n[错误] " + text, self.RED) + "\n")


def build_system_prompt(opts):
    prompt = (
        "你是 BO，运行在用户本机的编码智能体。\n"
        "工作目录: %s\n"
        "系统: %s\n"
        "\n"
        "工具: read_file(读文件,带行号) / edit_file(old_string 唯一命中后精确替换) / run_command(执行 shell 命令)。\n"
        "需要看内容先 read_file，改文件用 edit_file，可用 run_command 验证；不要凭空猜测。\n"
        "\n"
        "输出: 当前是纯文本终端，不渲染 Markdown。不要用加粗、标题、代码围栏等语法。回答用中文，简洁直接。\n"
    ) % (os.getcwd(), platform.platform())

    # 存在 AGENTS.md 时只需告知约定；文件内容一律不注入提示词，由模型自行 read_file
    agent = find_agent_file()
    if agent:
        prompt += (
            "\n约定: 当前目录下的 %s 是本项目的长期约定/记忆文件。开工前先 read_file 看它，\n"
            "按其中约定工作；产生需要跨会话保留的信息时，用 edit_file 更新它。\n"
        ) % agent
    return prompt


# ---------------------------------------------------------------------------
# 对话主循环
# ---------------------------------------------------------------------------

def run_turn(user_text, messages, opts, out):
    out.log("USER", user_text)
    messages.append({"role": "user", "content": user_text})

    for _ in range(opts["max_steps"]):
        # 流式回调：边收边打印；正文始终实时显示，思考仅在 verbose 级别显示
        stream_gap = {"content": False}
        cur_kind = {"v": None}

        def on_delta(kind, text):
            if kind == "reasoning" and out.level < LEVEL_VERBOSE:
                return
            if kind == "content" and stream_gap["content"] and cur_kind["v"] == "reasoning":
                out.stream_end()  # 正文在思考之后出现时另起一行
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
        finally:
            if cur_kind["v"] is not None:
                out.stream_end()

        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        tool_calls = msg.get("tool_calls") or []

        # 内容已在流式阶段实时打印，这里只补写日志（quiet 级别下思考被跳过，正文最终答复见下）
        if reasoning:
            out.log("THINKING", reasoning.strip())
        if content:
            out.log("ASSISTANT", content)
        # content 为空但 quiet 级别下未打印过任何正文时，兜底显示
        if content and out.level == LEVEL_QUIET and not tool_calls:
            out.assistant(content, True)

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
                result = "错误: 无法解析工具参数 JSON: %s" % e
            else:
                result = execute_tool(name, args, opts)

            out.log("TOOL_RESULT " + (name or "?"), result)
            out.tool_result(result, _is_error(result))
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})

    out.info("\n[已达单轮工具调用上限 %d，停止本轮]" % opts["max_steps"])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def parse_args():
    env = os.environ.get
    S = argparse.SUPPRESS  # 用 SUPPRESS 区分「用户是否显式传了该 option」
    parser = argparse.ArgumentParser(
        description="BO —— 单文件、纯标准库的最小编码智能体",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="注意: --yes 的含义是「命令执行前需人工确认」。不加 --yes 时命令默认直接放行。\n"
               "显式传入的连接类参数（--model / --base-url / --api-key / --root / --max-steps / "
               "--http-timeout / -l）会加密记录到当前目录的 .bo，之后不传参或只传部分参数时自动复用；"
               "删除 .bo 即恢复默认。优先级: 命令行 > 环境变量 > .bo > 内置默认。")
    parser.add_argument("-m", "--model", default=S,
                        help="模型名（默认取环境变量 MODEL / OPENAI_MODEL，其次取 .bo）")
    parser.add_argument("--base-url", default=S,
                        help="OpenAI 兼容接口地址（默认取 OPENAI_BASE_URL，其次取 .bo）")
    parser.add_argument("--api-key", default=S,
                        help="API 密钥（默认取 OPENAI_API_KEY，其次取 .bo）")
    parser.add_argument("--yes", action="store_true", dest="confirm",
                        help="开启命令执行前的逐条人工确认（不加则命令默认直接放行）")
    parser.add_argument("--root", default=S, metavar="DIR",
                        help="限制 read_file/edit_file 只能访问该目录之内（默认不限制）")
    parser.add_argument("--max-steps", type=int, default=S, help="单轮最多工具调用轮数")
    parser.add_argument("--http-timeout", type=int, default=S, help="单次 HTTP 请求超时秒数")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="只显示最终答复，隐藏全部工具/思考过程")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="提高显示级别：-v 显示工具结果与思考，-vv 显示完整明细")
    parser.add_argument("--no-color", action="store_true",
                        help="关闭彩色输出（默认仅在终端下着色）")
    parser.add_argument("-l", "--log", nargs="?", const="BO.log", default=S, dest="log_path", metavar="FILE",
                        help="把完整交互记录写入日志文件（默认 BO.log），屏幕上不显示明细")
    parser.add_argument("--no-log", action="store_const", const=None, default=S, dest="log_path",
                        help="撤销已记录的 -l/--log，停止写日志")
    a = parser.parse_args()
    a_vars = vars(a)

    cfg_path = os.path.join(os.getcwd(), CONFIG_FILE)
    cfg, cfg_status = load_config(cfg_path)
    used_cfg = []  # 记录本次实际从 .bo 取值的键，用于启动提示

    def pick(name, env_value, default):
        """解析单个参数：显式 CLI > 环境变量 > .bo > 内置默认。"""
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
    # 空字符串环境变量按「未设置」处理，否则会覆盖掉默认接口地址
    base_url = pick("base_url", env("OPENAI_BASE_URL") or None, "https://api.openai.com/v1")
    api_key = pick("api_key", env("OPENAI_API_KEY") or None, "")
    root = pick("root", None, None)
    root = os.path.realpath(root) if root else None
    max_steps = pick("max_steps", None, 50)
    http_timeout = pick("http_timeout", None, 120)

    # 日志：-l/--log 设置，--no-log 显式清空（清空需与「未传参」区分，故单独处理）
    if "log_path" in a_vars:
        log_path = a_vars["log_path"]
    elif "log_path" in cfg:
        used_cfg.append("log_path")
        log_path = cfg["log_path"]
    else:
        log_path = None

    # 交互与显示开关仅本次生效，不写入 .bo
    confirm = bool(a.confirm)
    level = LEVEL_QUIET if a.quiet else min(LEVEL_NORMAL + a.verbose, LEVEL_DEBUG)
    try:
        color = (not a.no_color) and env("NO_COLOR") is None and sys.stdout.isatty()
    except Exception:
        color = False

    # 只把本次显式传入的连接类参数写回 .bo，保留文件中其它键
    updates = {}
    if "model" in a_vars:
        updates["model"] = model
    if "base_url" in a_vars:
        updates["base_url"] = base_url
    if "api_key" in a_vars:
        updates["api_key"] = api_key
    if "root" in a_vars:
        updates["root"] = root
    if "max_steps" in a_vars:
        updates["max_steps"] = max_steps
    if "http_timeout" in a_vars:
        updates["http_timeout"] = http_timeout
    if "log_path" in a_vars:
        updates["log_path"] = log_path

    config_saved, config_error = False, None
    if updates:
        merged = dict(cfg)
        merged.update(updates)
        if merged != cfg:  # 无变化则不重写，避免无谓地改动文件
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
    """让标准输入输出使用 UTF-8，避免在 C/POSIX locale 下中文报 UnicodeEncodeError。

    Python 3.7+ 有 reconfigure；3.6 没有，只能自己包一层 TextIOWrapper。
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

    opts = parse_args()
    out = Output(opts["level"], opts["color"], opts["log_path"])
    system_prompt = build_system_prompt(opts)

    sys.stdout.write(
        "BO —— 最小编码智能体 (Python %s, %s)\n"
        "模型: %s\n接口: %s\n命令确认: %s\n显示级别: %s\n"
        % (platform.python_version(), platform.system(), opts["model"], opts["base_url"],
           "开启 (--yes)" if opts["confirm"] else "关闭 (默认放行)", LEVEL_NAMES[opts["level"]]))
    if opts["root"]:
        sys.stdout.write("文件访问限制: %s\n" % opts["root"])
    if out.log_path:
        sys.stdout.write("完整日志: %s\n" % out.log_path)
    if opts["config_status"] == "invalid":
        sys.stderr.write("警告: %s 存在但无法解密/解析（或不属于本目录），已忽略\n" % CONFIG_FILE)
    if opts["config_used"]:
        sys.stdout.write("参数记忆: 已从 %s 读取\n" % CONFIG_FILE)
    if opts["config_saved"]:
        sys.stdout.write("参数记忆: 已写入 %s\n" % CONFIG_FILE)
    elif opts["config_error"]:
        sys.stderr.write("警告: 写入 %s 失败: %s\n" % (CONFIG_FILE, opts["config_error"]))
    sys.stdout.write("输入 /help 查看帮助，exit 退出。\n")

    out.log("SESSION BEGIN", "编号: %d\nmodel=%s\nbase_url=%s\ncwd=%s\nlevel=%s" % (
        out.session_id, opts["model"], opts["base_url"], opts["cwd"], opts["level"]))
    out.log("SYSTEM", system_prompt)
    messages = [{"role": "system", "content": system_prompt}]
    started = time.time()

    try:
        while True:
            try:
                user = input("\n你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\n再见。\n")
                break
            if not user:
                continue
            if user in ("exit", "quit", "/exit", "/quit"):
                sys.stdout.write("再见。\n")
                break
            if user == "/reset":
                messages = [{"role": "system", "content": system_prompt}]
                out.log("RESET", "对话历史已清空")
                sys.stdout.write("已清空对话历史。\n")
                continue
            if user == "/help":
                sys.stdout.write("可用交互命令:\n"
                                 "  /reset   清空对话历史\n"
                                 "  /help    显示本帮助\n"
                                 "  exit     退出\n")
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
        out.log("SESSION END", "编号: %d\n耗时: %.1fs" % (out.session_id, time.time() - started))
        out.close()


if __name__ == "__main__":
    main()
