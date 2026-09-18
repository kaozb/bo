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
    -m, --model NAME    模型名（覆盖 MODEL / OPENAI_MODEL）
    -b, --base-url URL  接口地址（覆盖 OPENAI_BASE_URL）
    -k, --api-key KEY   API 密钥（覆盖 OPENAI_API_KEY）
    -y, --yes           命令执行前逐条人工确认（不加则默认直接放行）
    -r, --root DIR      限制文件类工具（read_file/write_file/search）与 run_command 的
                        cwd 只能访问 DIR 之内（默认不限制）
    -s, --max-steps N   单轮最多工具调用轮数（默认 50）
    -t, --http-timeout N  单次请求超时秒数（默认 120）
    -q                  只显示最终答复，隐藏全部工具/思考过程
    -v / -vv            显示工具结果与思考 / 完整明细
    -C, --no-color      关闭彩色输出
    -l, --log [FILE]    完整交互写入日志（默认 BO.log），屏幕上不显示明细
    -L, --no-log        撤销已记录的 -l/--log，停止写日志

参数记忆: 显式传入的连接类参数（-m / -b / -k / -r / -s / -t / -l）会加密记录到当前目录的 .bo，
          之后不传参或只传部分参数时自动复用；-y / -q / -v / -C 等交互与显示开关仅本次生效，
          不写入该文件。优先级: 命令行 > 环境变量 > .bo > 内置默认；删除 .bo 即恢复默认。

交互: /reset 清空对话，/help 帮助，exit 退出
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
    import readline  # noqa: F401  存在时启用输入历史，不存在则忽略
except Exception:
    pass

MAX_OUTPUT_CHARS = 30000             # 单个工具结果进入上下文的最大字符数
MAX_LINE_CHARS = 2000                # read_file 单行最大显示字符数
MAX_READ_BYTES = 10 * 1024 * 1024    # 文件读写大小上限，防止内存被撑爆
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 单次 HTTP 响应大小上限
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024  # search 单文件最大读取字节数
MAX_SEARCH_RESULTS = 1000            # search 单次最多返回的结果行数
MAX_EDITS = 50                       # write_file 单次 edits 数组最多条数
MAX_COMMAND_TIMEOUT = 3600           # run_command 超时上限（秒）
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024  # run_command 在内存保留的输出上限（首尾各半，再截到可见长度）
MAX_DIR_ITEMS = 1000                 # read_file 列目录时单次最多显示的项目数
MAX_REPEAT_CALLS = 3                 # 同一轮内完全相同的工具调用连续出现该次数即中止本轮
TRIM_KEEP_TURNS = 1                  # 每轮开始时保留最近几轮的完整工具调用/结果（更早的移除）
SEARCH_SKIP_DIRS = (".git", "__pycache__", "node_modules", ".venv", "venv",
                    ".tox", ".mypy_cache", ".pytest_cache")  # search 跳过的目录
AGENT_FILE = "AGENTS.md"             # 当前目录下的约定文件（不区分大小写），存在则提示模型自行读取
CONFIG_FILE = ".bo"                  # 当前目录下的参数记忆文件（加密），显式传参时写入、无参时复用
# 只有这些「连接/运行类」参数会写入 .bo；交互与显示开关（-y / -q / -v / -C）仅本次生效
CONFIG_KEYS = ("model", "base_url", "api_key", "root", "max_steps", "http_timeout", "log_path")

# 屏幕展示分级
LEVEL_QUIET, LEVEL_NORMAL, LEVEL_VERBOSE, LEVEL_DEBUG = 0, 1, 2, 3
LEVEL_NAMES = ("quiet (仅最终答复)", "normal (工具调用一行提示)",
               "verbose (含工具结果与思考)", "debug (完整明细)")


# ---------------------------------------------------------------------------
# Ctrl+C 语义：第一次中断当前一轮（生成或命令），第二次退出程序
# ---------------------------------------------------------------------------

class TurnInterrupted(BaseException):
    """第一次 Ctrl+C 抛出：只结束当前这一轮，回到提示符，不退出程序。

    继承 BaseException，避免被沿途的 except Exception 当成普通错误吞掉。
    """


# busy 表示「正处于一轮之内」，seen 表示本轮已经按过一次 Ctrl+C
_INTERRUPT = {"busy": False, "seen": False}


def _handle_sigint(signum, frame):
    if _INTERRUPT["busy"] and not _INTERRUPT["seen"]:
        _INTERRUPT["seen"] = True
        raise TurnInterrupted()     # 一轮之内第一次按下：只中断本轮
    raise KeyboardInterrupt()       # 空闲时、或本轮内第二次按下：退出程序


def install_sigint_handler():
    """接管 SIGINT；装不上（非主线程等）就沿用默认行为。"""
    try:
        signal.signal(signal.SIGINT, _handle_sigint)
    except (ValueError, OSError):
        pass


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


def _arr(desc, item_props, item_required):
    """一个「对象数组」类型的 JSON Schema 属性。"""
    return {"type": "array", "description": desc,
            "items": {"type": "object", "properties": item_props, "required": item_required}}


TOOLS = [
    _fn("read_file", "读取文件（带行号）或列出目录，offset 从 1 开始、未读完会给出续读 offset，"
        "二进制文件拒绝读取。write_file 的 old_string 不要带行号。",
        {"path": _p("string", "文件或目录路径（相对或绝对）"),
         "offset": _p("integer", "起始行号 / 起始项目，从 1 开始，默认 1"),
         "limit": _p("integer", "文件最多读取行数（默认 2000）/ 目录最多列出项数（默认 200）")},
        ["path"]),
    _fn("write_file", "写文件：给 content 新建或整体覆盖（新建文件必须用它，不要用 run_command 的 echo/cat "
        "重定向）；给 old_string + new_string 或 edits 对已有文件做精确替换（只改局部请用这种模式），"
        "两种模式不能混用。old_string 必须唯一出现，否则报错并列出候选位置；多处不同修改用 edits 一次提交；"
        "父目录不存在会自动创建；content 为空会被拒绝。",
        {"path": _p("string", "文件路径"),
         "content": _p("string", "整体写入的完整文本内容（与 old_string/edits 二选一，不能为空）"),
         "old_string": _p("string", "被替换的原文，需在文件中唯一（与 content 二选一；不要带行号）"),
         "new_string": _p("string", "替换后的新文本（与 content 二选一）"),
         "replace_all": _p("boolean", "为 true 时替换所有出现位置，默认 false"),
         "edits": _arr("一次提交多处修改，按顺序应用；任一失败则整单不写入（与 content 二选一）",
                       {"old_string": _p("string", "被替换的原文（不要带行号）"),
                        "new_string": _p("string", "替换后的新文本"),
                        "replace_all": _p("boolean", "替换所有出现位置，默认 false")},
                       ["old_string", "new_string"])},
        ["path"]),
    _fn("search", "用正则搜索文件或目录（区分大小写，忽略大小写用 (?i) 前缀），跳过 .git / __pycache__ / "
        "node_modules 等目录以及二进制与超大文件。比用 run_command 跑 grep 更省输出。",
        {"pattern": _p("string", "正则表达式；不是合法正则时自动按字面量搜索"),
         "path": _p("string", "要搜索的文件或目录，默认当前目录 ."),
         "glob": _p("string", "只搜索匹配该通配符的文件（如 *.py），默认全部"),
         "max_results": _p("integer", "最多返回的命中行数，默认 100，上限 1000（上下文行不计入；输出另有 30000 字符上限）"),
         "context_lines": _p("integer", "每条命中附加上下各 N 行（0-10），默认 0")},
        ["pattern"]),
    _fn("run_command", "在 shell 中执行命令，返回退出码与合并后的 stdout+stderr。"
        "命令的 stdin 是空的，不要执行 vim / top 等交互式命令；输出过大时只保留首尾并提示截断。",
        {"command": _p("string", "要执行的 shell 命令（非交互式；后台任务请自行 nohup ... &）"),
         "cwd": _p("string", "命令的工作目录，默认当前目录（须在 --root 允许范围内）"),
         "timeout": _p("integer", "超时秒数，默认 120，超时后连同派生进程一起终止")},
        ["command"]),
]


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------

def _decode_bytes(data):
    """把 bytes 解码为文本，返回 (文本, 实际编码)；尽量无损（latin-1 兜底总是成功）。"""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc), enc
        except Exception:
            continue
    return data.decode("utf-8", "replace"), "utf-8"


def _detect_encoding(data):
    """返回能无损解码 data 的候选编码；判定与 _decode_bytes 完全相同。"""
    return _decode_bytes(data)[1]


def _count_lines(data):
    """统计行数（\\n 断行，末行无换行符也算一行）；data 可为 bytes 或 str。"""
    nl = b"\n" if isinstance(data, bytes) else "\n"
    n = data.count(nl)
    return n + 1 if data and not data.endswith(nl) else n


def _decode(data):
    return _decode_bytes(data)[0]


def _split_lines(text):
    """统一的文本行切分：只按 \n 断行，去掉行尾 \r，忽略末尾空行。

    与 splitlines() 不同，这里不把 \x0b / \x0c / \x85 / \u2028 等也当行分隔，
    行号与编辑器一致，也与大文件流式读取的切分方式相同。
    """
    lines = text.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


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


def _confirm(prompt):
    """向用户询问 yes/no；非 y/yes 一律视为拒绝（EOF/EOT 也算拒绝）。

    Ctrl+C 不在这里吞掉：交给 _handle_sigint —— 本轮第一次只中断本轮，第二次退出程序。
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        ans = input().strip().lower()
    except EOFError:
        ans = "n"
    return ans in ("y", "yes")


def _truncate_middle(text, limit=MAX_OUTPUT_CHARS):
    """命令输出截断：保留首尾两端（报错通常在尾部），中间省略。"""
    if not text or len(text) <= limit:
        return text
    head = max(1, limit // 3)
    tail = max(0, limit - head - 64)
    note = "\n... [中间省略 %d 字符] ...\n" % (len(text) - head - tail)
    return text[:head] + note + (text[-tail:] if tail else "")


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


def _binary_error(shown):
    """二进制文件的统一提示。"""
    return ("错误: %s 疑似二进制文件（含 NUL 字节），已跳过文本读取。"
            "可用 run_command 的 file / xxd / hexdump 查看。" % shown)


def _atomic_write(path, data, mode=None):
    """原子写入：先写同目录临时文件并 fsync，再 os.replace 覆盖。

    mode 为 None 时保留原权限（新文件 0644），否则一律使用给定权限（如 .bo 的 0600）。
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
    except BaseException:  # 含 Ctrl+C（BaseException），中断也不能留下临时文件
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _read_lines_window(path, shown, offset, limit):
    """按行窗口读取文件，返回 (行列表, 总行数, 错误)；offset 为 0 基。

    小文件一次读入并用 _decode_bytes 探测编码（GBK 等也能正确读出）；
    超过 MAX_READ_BYTES 的大文件改为两遍流式扫描：先分块统计 \n 得到总行数，
    再用 TextIOWrapper 只取目标行，因此不会把整份内容读进内存。
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
            if b"\x00" in f.read(8192):  # 二进制嗅探与统计行数共用这一次 open
                return None, 0, _binary_error(shown)
            f.seek(0)
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                total += chunk.count(b"\n")
                last = chunk[-1:]
    except Exception as e:
        return None, 0, "错误: 读取失败: %s" % e
    if last and last != b"\n":
        total += 1  # 末行没有换行符

    out = []
    if offset < total:
        try:
            with open(path, "rb") as f:
                wrap = io.TextIOWrapper(f, encoding="utf-8", errors="replace", newline="")
                for ln in itertools.islice(wrap, offset, offset + limit):
                    out.append(ln.rstrip("\n").rstrip("\r"))
        except Exception as e:
            return None, 0, "错误: 读取失败: %s" % e
    return out, total, None


def _list_dir(real, shown, offset=0, limit=200):
    """列出目录内容（read_file 传入目录时使用）；只列一层，支持 offset/limit 翻项。"""
    try:
        with os.scandir(real) as it:
            entries = sorted(it, key=lambda e: e.name)
    except Exception as e:
        return "错误: 无法读取目录: %s" % e

    dirs, files, links, others = [], [], [], []
    for e in entries:
        try:
            if e.is_symlink():
                links.append("%s -> %s" % (e.name, os.readlink(e.path)))
            elif e.is_dir():
                dirs.append(e.name + "/")
            elif e.is_file():
                files.append("%s  (%d 字节)" % (e.name, e.stat().st_size))
            else:
                others.append(e.name)
        except Exception:
            others.append(e.name)

    items = dirs + links + files + others
    if not items:
        return "目录 %s 为空。" % shown
    window = items[offset:offset + limit]
    if not window:
        return "目录 %s 共 %d 项，offset=%d 已超出末尾。" % (shown, len(items), offset + 1)
    note = ""
    if offset + len(window) < len(items):
        note = "\n... [还有 %d 项，续看 offset=%d]" % (
            len(items) - offset - len(window), offset + len(window) + 1)
    return "目录 %s 共 %d 项（目录 %d / 文件 %d），显示第 %d-%d 项:\n%s%s" % (
        shown, len(items), len(dirs), len(files),
        offset + 1, offset + len(window), "\n".join(window), note)


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
    try:
        _atomic_write(path, data, 0o600)
    except Exception as e:
        return "写入失败: %s" % e
    return None


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def tool_read_file(args, opts):
    shown = args.get("path")
    path, err = _resolve(shown, opts)
    if err:
        return err
    offset = max(_int(args.get("offset"), 1, minimum=1) - 1, 0)  # 对外 1 基，内部 0 基
    if os.path.isdir(path):
        return _list_dir(path, shown, offset,
                         min(_int(args.get("limit"), 200, minimum=1), MAX_DIR_ITEMS))
    if not os.path.exists(path):
        return "错误: 文件不存在: %s" % shown
    if not os.path.isfile(path):
        return "错误: 不是普通文件（设备文件等）: %s" % shown

    limit = _int(args.get("limit"), 2000, minimum=1)
    lines, total, err = _read_lines_window(path, shown, offset, limit)
    if err:
        return err
    if total == 0:
        return "文件 %s 是空文件。" % shown
    if offset >= total:
        return "文件 %s 共 %d 行，offset=%d 已超出文件末尾。" % (shown, total, offset + 1)

    # 逐行拼装并累计字符数：即使 limit 很大，单次输出也不会超出上下文预算
    shown_lines, used, cut = [], 0, False
    for i, ln in enumerate(lines):
        text = _truncate(ln, MAX_LINE_CHARS, note="... [本行已截断]")
        cost = len(text) + 8  # 行号与换行的大致开销
        if shown_lines and used + cost > MAX_OUTPUT_CHARS:
            cut = True
            break
        shown_lines.append("%6d\t%s" % (offset + i + 1, text))
        used += cost
    end = offset + len(shown_lines)
    header = "文件 %s 共 %d 行，显示第 %d-%d 行:" % (shown, total, offset + 1, end)
    if end >= total:
        footer = "\n[已到文件末尾]"
    elif cut:
        footer = "\n[输出已达 %d 字符上限，本次显示到第 %d 行；续读 offset=%d]" % (
            MAX_OUTPUT_CHARS, end, end + 1)
    else:
        footer = "\n[还有 %d 行未显示，续读 offset=%d]" % (total - end, end + 1)
    return header + "\n" + "\n".join(shown_lines) + footer


def tool_write_file(args, opts):
    """写文件：按参数自动选择「整体写入」或「精确替换」两种模式。"""
    shown = args.get("path")
    path, err = _resolve(shown, opts)
    if err:
        return err

    has_content = args.get("content") is not None
    has_edit = (args.get("edits") is not None or args.get("old_string") is not None
                or args.get("new_string") is not None)
    if has_content and has_edit:
        return ("错误: content 与 old_string/new_string/edits 不能同时使用。"
                "整体写入请只给 content；局部替换请只给 old_string + new_string 或 edits。")
    if has_edit:
        return _edit_existing_file(path, shown, args)
    if not has_content:
        hint = ""
        if args.get("replace_all") is not None:
            hint = "（replace_all 只在配合 old_string/new_string/edits 时有效，本次未生效）"
        return ("错误: 缺少参数。整体写入需 content；局部替换需 old_string + new_string，"
                "或用 edits 一次提交多处修改。" + hint)
    result = _write_whole_file(path, shown, args)
    if args.get("replace_all") is not None and result.startswith("已"):
        result += ("\n提示: replace_all 只在局部替换（old_string/new_string 或 edits）时有效，"
                   "本次整体写入已忽略该参数。")
    return result


def _write_whole_file(path, shown, args):
    """模式一：整体写入（新建或覆盖）。"""
    content = args.get("content")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    if content == "":
        return ("错误: content 为空，会清空文件，已拒绝写入。新建空文件请用 run_command 的 touch；"
                "确实要清空已有文件请显式执行 `: > 文件`。")
    if os.path.isdir(path):
        return "错误: %s 是目录，不能写入文件" % shown

    existed = os.path.isfile(path)
    enc, old_lines, old_bytes = "utf-8", 0, 0
    if existed:
        if os.path.getsize(path) > MAX_READ_BYTES:
            return "错误: 目标文件过大（> %d MB），拒绝整体覆盖；请改用 old_string/new_string 局部修改" % (
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
        enc_note = "（原编码 %s 无法表示新内容，已改用 utf-8 保存）" % enc
        enc, data = "utf-8", content.encode("utf-8")
    if len(data) > MAX_READ_BYTES:
        return "错误: 写入内容过大（%.1f MB，上限 %d MB）" % (
            len(data) / 1048576.0, MAX_READ_BYTES // 1048576)

    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent)
        except Exception as e:
            return "错误: 无法创建目录 %s: %s" % (parent, e)
    try:
        _atomic_write(path, data)
    except Exception as e:
        return "错误: 写入失败: %s" % e
    new_lines, new_bytes = _count_lines(content), len(data)
    if not existed:
        return "已新建 %s（%d 行，%d 字节，编码 %s）%s" % (
            shown, new_lines, new_bytes, enc, enc_note)
    result = "已覆盖 %s（原 %d 行/%d 字节 → 现 %d 行/%d 字节，编码 %s）%s" % (
        shown, old_lines, old_bytes, new_lines, new_bytes, enc, enc_note)
    if old_bytes and new_bytes < old_bytes * 0.5 and new_lines < old_lines:
        result += "\n注意: 新内容比原文件小了 %.0f%%，若只想改局部请改用 old_string/new_string。" % (
            (1 - new_bytes / float(old_bytes)) * 100)
    return result


def _line_of(text, idx):
    """下标 -> 1 基行号。"""
    return text.count("\n", 0, idx) + 1


def _line_text(text, idx):
    """下标所在行的原文。"""
    a = text.rfind("\n", 0, idx) + 1
    b = text.find("\n", idx)
    return text[a:b if b >= 0 else len(text)]


def _locate_fuzzy(text, old):
    """忽略行尾空白与 CRLF 差异定位 old，返回原文跨度列表。

    按行比对：单行 old 在每行内做子串查找；多行 old 要求首行以 first 结尾、
    中间行逐行相等、末行以 last 开头，与「忽略行尾空白/CRLF」的语义一致。
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
    """定位 old 在 text 中的出现，返回 (跨度列表, 说明)。

    先精确匹配；一次都没命中时，再忽略行尾空白 / CRLF 差异匹配一遍。
    """
    if not old:
        return [], ""
    spans, start = [], 0
    while True:
        i = text.find(old, start)
        if i < 0:
            break
        spans.append((i, i + len(old)))
        start = i + 1  # 允许重叠命中，避免漏判「出现多次」（如 aaa 中找 aa）
    if spans:
        return spans, ""
    spans = _locate_fuzzy(text, old)
    return spans, "，已忽略行尾空白/CRLF 差异" if spans else ""


def _probe_fragments(old, limit=3):
    """从 old 中取出用于近似定位的片段：优先整行，其次较长的词。"""
    frags = [s for s in (ln.strip() for ln in old.split("\n")) if len(s) >= 3]
    if not frags:
        frags = [t for t in re.split(r"[^0-9A-Za-z_]+", old) if len(t) >= 3]
    if len(frags) > limit:  # 取首行与末行，兼顾跨多行的 old
        frags = [frags[0], frags[-1]]
    return frags[:limit]


def _candidates(text, old, spans, limit=5):
    """生成定位报告：已命中的位置，或与 old 最接近的行。"""
    if spans:
        out = []
        for a, _ in spans[:limit]:
            out.append("  第 %d 行: %s" % (
                _line_of(text, a), _truncate(_line_text(text, a).strip(), 160)))
        if len(spans) > limit:
            out.append("  ... 等共 %d 处" % len(spans))
        return "候选位置:\n" + "\n".join(out)

    # 未命中：用 old 里的片段做子串扫描（str.find 是 C 级实现，比逐行打分快几个数量级）
    lines = text.split("\n")
    hits, seen = [], set()
    for needle in _probe_fragments(old):
        start = 0
        while len(hits) < limit:
            i = text.find(needle, start)
            if i < 0:
                break
            ln = _line_of(text, i)  # 1 基行号
            if ln not in seen:
                seen.add(ln)
                hits.append(ln)
            start = i + 1
        if len(hits) >= limit:
            break
    if not hits:
        return ("未找到相近内容（文件共 %d 行）。请用 read_file 确认原文"
                "（注意空格、缩进，且不要把行号一起复制）。" % len(lines))
    return "最接近的行:\n" + "\n".join(
        "  第 %d 行: %s" % (i, _truncate(lines[i - 1].strip(), 160)) for i in hits)


def _diff_block(old_block, new_block, context=1, limit=24):
    """生成一小段 unified diff（去掉文件头，必要时截断）。"""
    a, b = old_block.splitlines(), new_block.splitlines()
    lines = [ln for ln in difflib.unified_diff(a, b, lineterm="", n=context)
             if not ln.startswith("--- ") and not ln.startswith("+++ ")]
    if len(lines) > limit:
        lines = lines[:limit] + ["... [diff 已截断]"]
    return "\n".join(lines)


def _apply_edit(text, edit):
    """应用一条修改，返回 (新文本, 替换处数, diff, 说明, 错误)。"""
    old, new = edit.get("old_string"), edit.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return text, 0, "", "", "错误: 每条修改都需要字符串型的 old_string 与 new_string"
    if not old:
        return text, 0, "", "", "错误: old_string 不能为空"
    if old == new:
        return text, 0, "", "", "错误: old_string 与 new_string 相同，无需修改"

    replace_all = bool(edit.get("replace_all"))
    spans, note = _locate(text, old)
    if not spans:
        return text, 0, "", "", "错误: 未找到 old_string。\n" + _candidates(text, old, [])
    if len(spans) > 1 and not replace_all:
        return text, 0, "", "", (
            "错误: old_string 在文件中出现 %d 次，不唯一。可补充上下文使其唯一，"
            "或设 replace_all=true 连同上下文一起全部替换。\n%s" % (
                len(spans), _candidates(text, old, spans)))

    if replace_all:
        # 重叠的匹配一起替换会相互破坏，此处只保留互不重叠的位置
        use, last_end = [], -1
        for a, b in spans:
            if a >= last_end:
                use.append((a, b))
                last_end = b
    else:
        use = spans[:1]
    if replace_all and not note:
        out = text.replace(old, new)  # 精确匹配时交给 str.replace（C 级，一次扫描）
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
        diff += "\n（共替换 %d 处，diff 仅示第一处）" % len(use)
    return out, len(use), diff, note, ""


def _edit_existing_file(path, shown, args):
    """模式二：对已有文件做精确字符串替换（old_string/new_string 或 edits）。"""
    edits = args.get("edits")
    if edits is None:
        if args.get("old_string") is None or args.get("new_string") is None:
            return "错误: 需要 path / old_string / new_string 三个参数，或用 edits 提交多处修改"
        edits = [{"old_string": args.get("old_string"),
                  "new_string": args.get("new_string"),
                  "replace_all": args.get("replace_all")}]
    if not isinstance(edits, list) or not edits:
        return "错误: edits 必须是至少含一条修改的数组"
    if len(edits) > MAX_EDITS:
        return "错误: edits 一次最多 %d 条，请分批提交" % MAX_EDITS

    if not os.path.isfile(path):
        return "错误: 文件不存在或不是普通文件: %s（新建文件请传 content 参数）" % shown
    raw, err = _read_file_bytes(path)
    if err:
        return err
    text, enc = _decode_bytes(raw)

    diffs, total, tolerant = [], 0, False
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            return "错误: edits 第 %d 项不是对象" % i
        text, n, diff, note, err = _apply_edit(text, edit)
        if err:
            head = "错误: edits 第 %d 项失败: " % i if len(edits) > 1 else ""
            return head + err + ("\n（本次未做任何写入）" if len(edits) > 1 else "")
        total += n
        tolerant = tolerant or bool(note)
        if diff:
            diffs.append(diff)

    try:
        data = text.encode(enc)
    except Exception as e:
        return "错误: 新内容无法用原编码(%s)保存: %s" % (enc, e)
    try:
        _atomic_write(path, data)
    except Exception as e:
        return "错误: 写入失败: %s" % e

    head = "已修改 %s（共替换 %d 处%s）" % (
        shown, total, "，已忽略行尾空白/CRLF 差异" if tolerant else "")
    return head + ("\n" + "\n".join(diffs) if diffs else "")


def _iter_search_files(root_path, glob_pat):
    """列出待搜索的文件；跳过常见噪声目录与不匹配 glob 的文件。"""
    if os.path.isfile(root_path):
        return [root_path]
    rx = None
    if glob_pat and glob_pat != "*":
        rx = re.compile(fnmatch.translate(glob_pat))  # 预编译，避免逐文件重复翻译
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
    """生成用于「整文件预筛」的正则；不可靠时返回 None（退回逐行匹配）。

    MULTILINE 只会放宽 ^ 与 $，所以「逐行有命中」的文本整文件搜索必然也命中，除非：
      - pattern 含 \\A / \\Z / (?-m：这些锚点在整文件与逐行下的含义不同；
      - pattern 含 $ 且正文含 \\r（CRLF 文件）：$ 的落点随切分方式改变。
    后一种情况由调用方按正文判断，此处只排除前一类 pattern。
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
        return "错误: 缺少 pattern 参数（要搜索的正则表达式）"
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
        return "错误: 路径不存在: %s" % shown

    glob_pat = args.get("glob") or "*"
    limit = min(_int(args.get("max_results"), 100, minimum=1), MAX_SEARCH_RESULTS)
    context = min(_int(args.get("context_lines"), 0, minimum=0), 10)
    is_dir = os.path.isdir(target)
    files = _iter_search_files(target, glob_pat)
    if not files:
        return "在 %s 下没有匹配 glob=%s 的文件" % (shown, glob_pat)

    pre = _search_prefilter(rx.pattern, rx.flags)  # 整文件预筛；None 表示不适用
    crlf_anchored = "$" in rx.pattern            # 含 $ 时 CRLF 正文不能用预筛
    hits, scanned, skipped = [], 0, 0
    matched, used, full = 0, 0, False            # used: 已累计的输出字符数
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
        if b"\x00" in raw[:8192]:  # 二进制文件跳过
            continue
        body = _decode_bytes(raw)[0]
        scanned += 1
        # 整文件预筛：不命中就跳过 splitlines 与逐行正则，避免大目录下白扫
        if pre is not None and not (crlf_anchored and "\r" in body):
            if not pre.search(body):
                continue
        lines = _split_lines(body)
        label = os.path.relpath(full_path, target) if is_dir else shown
        emitted = -1  # 已输出到的行号（0 基），避免相邻命中的上下文行重复
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
                    label, sep, k + 1, sep, _truncate(lines[k], MAX_LINE_CHARS, note="... [本行已截断]"))
                if used + len(line) + 1 > MAX_OUTPUT_CHARS:
                    full = True
                    break
                hits.append(line)
                used += len(line) + 1
            emitted = max(emitted, hi - 1)

    if not matched:
        return "在 %s 下搜索 %s 无命中（已扫描 %d 个文件）%s" % (
            shown, pattern, scanned,
            "（pattern 不是合法正则，已按字面量搜索）" if literal else "")
    tail = ""
    if matched >= limit:
        tail += "\n[命中数已达上限 %d，可缩小 path/glob 或调大 max_results]" % limit
    if full:
        tail += "\n[输出已达 %d 字符上限，可调小 context_lines 或缩小搜索范围]" % MAX_OUTPUT_CHARS
    if skipped:
        tail += "\n[跳过 %d 个超过 %d MB 的大文件]" % (skipped, MAX_SEARCH_FILE_BYTES // 1048576)
    if literal:
        tail += "\n[pattern 不是合法正则，已按字面量搜索]"
    return "搜索 %s（在 %s 下，已扫 %d 个文件，命中 %d 处，输出 %d 行）:\n%s%s" % (
        pattern, shown, scanned, matched, len(hits), _truncate("\n".join(hits)), tail)


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


def _collect_process_output(stream, box, half):
    """读取线程：边读边丢弃中间部分，只保留首尾，避免超长输出撑爆内存。"""
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
    """把采集到的输出还原为文本，返回 (文本, 说明)。"""
    head, tail = box.get("head", b""), box.get("tail", b"")
    total = box.get("total", 0)
    if total > MAX_COMMAND_OUTPUT_BYTES:
        omitted = max(0, total - len(head) - len(tail))
        note = "输出共 %.1f KB，中间约 %d 字节已省略（要完整内容请重定向到文件后再 read_file）" % (
            total / 1024.0, omitted)
        return (head.decode("utf-8", "replace")
                + "\n... [中间省略约 %d 字节] ...\n" % omitted
                + tail.decode("utf-8", "replace")), note
    return _decode_bytes(head + tail)[0], ""


def tool_run_command(args, opts):
    command = args.get("command")
    if not command:
        return "错误: 缺少 command 参数"
    timeout = min(_int(args.get("timeout"), 120, minimum=1), MAX_COMMAND_TIMEOUT)

    cwd = opts["cwd"]
    if args.get("cwd"):
        cwd, err = _resolve(args.get("cwd"), opts)
        if err:
            return err
        if not os.path.isdir(cwd):
            return "错误: cwd 不是目录: %s" % args.get("cwd")
    elif opts.get("root"):
        # 默认 cwd（启动目录）同样要受 --root 约束，否则「只能访问 DIR 之内」形同虚设
        cwd, err = _resolve(cwd, opts)
        if err:
            return ("错误: 默认工作目录 %s 越出允许范围（--root %s）。"
                    "请先 cd 到该目录内再启动，或在参数里显式传 cwd。" % (opts["cwd"], opts["root"]))

    if opts["confirm"] and not _confirm("\n[待执行命令]%s %s\n确认执行? [y/N] " % (
            " (cwd=%s)" % cwd if cwd != opts["cwd"] else "", command)):
        return "用户拒绝执行该命令。"

    popen_kwargs = {"shell": True, "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT, "cwd": cwd}
    if hasattr(os, "killpg"):
        # 让子进程成为独立进程组组长，超时时可整组击杀
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except Exception as e:
        return "错误: 无法启动命令: %s" % e

    # 输出交给后台线程边读边限额保留：命令吐再多也不会把内存吃光
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
        _terminate_group(proc)      # Ctrl+C（中断本轮或退出）时都不留派生进程
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
        body = "(无输出)"
    if timed_out:
        return "命令超时（> %ds）已被终止（含派生进程）。已产生的输出:\n%s%s" % (
            timeout, header, body)
    return "退出码: %d\n%s输出:\n%s" % (proc.returncode, header, body)


TOOL_FUNCS = {"read_file": tool_read_file, "write_file": tool_write_file,
              "search": tool_search, "run_command": tool_run_command}


def execute_tool(name, args, opts):
    func = TOOL_FUNCS.get(name)
    if func is None:
        return "错误: 未知工具 %s" % name
    return func(args, opts)


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
        self._w(self._c("\n[错误] " + text, self.RED) + "\n")


def tools_hint():
    """由 TOOLS 自动生成系统提示里的工具清单，新增工具时不必改两处。"""
    lines = []
    for t in TOOLS:
        f = t["function"]
        lines.append("- %s: %s" % (f["name"], f["description"].split("。")[0]))
    return "\n".join(lines)


def build_system_prompt(opts):
    prompt = (
        "你是 BO，运行在用户本机的编码智能体。\n"
        "工作目录: %s\n"
        "系统: %s\n"
        "\n"
        "可用工具:\n%s\n"
        "\n"
        "做法: 先 search 定位，再 read_file 看清片段，然后 write_file（整体写入给 content、"
        "局部修改给 old_string + new_string），最后用 run_command 验证（跑测试/编译/执行脚本）；"
        "不要凭空猜测。\n"
        "新建文件一律用 write_file 的 content 模式，不要用 run_command 的 echo/cat 重定向拼文件；"
        "删除或移动文件用 run_command 的 rm / mv，比较危险的操作先向用户确认。\n"
        "\n"
        "输出: 当前是纯文本终端，不渲染 Markdown。不要用加粗、标题、代码围栏等语法。回答用中文，简洁直接。\n"
    ) % (os.getcwd(), platform.platform(), tools_hint())

    # 存在 AGENTS.md 时只需告知约定；文件内容一律不注入提示词，由模型自行 read_file
    agent = find_agent_file()
    if agent:
        prompt += (
            "\n约定: 当前目录下的 %s 是本项目的长期约定/记忆文件。开工前先 read_file 看它，\n"
            "按其中约定工作；产生需要跨会话保留的信息时，用 write_file 更新它。\n"
        ) % agent
    return prompt


# ---------------------------------------------------------------------------
# 对话主循环
# ---------------------------------------------------------------------------

def _trim_history(messages, keep_turns=TRIM_KEEP_TURNS):
    """移除较早轮次里的工具调用与工具结果，只保留最近 keep_turns 轮的完整工具往返。

    轮次以 role == "user" 的消息为界（system 消息始终保留）。被移除的是「上上轮及之前」的：
      - role == "tool" 的结果消息；
      - assistant 消息的 tool_calls 字段（去掉后 content 为空则整条删除）。
    user 的输入与 assistant 的正文都保留，因此对话主线还在，只有工具往返被清掉。
    返回被移除的消息条数。
    """
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(starts) <= keep_turns:
        return 0
    cut = starts[len(starts) - keep_turns]  # 需要完整保留的第一条 user 的下标
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
                continue  # 只剩工具调用、没有正文的助手消息整体丢弃
        kept.append(m)
    if len(kept) == len(messages):
        return 0
    # 丢掉纯工具往返的助手消息后可能出现连续两条 user，合并它们以保持角色交替
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
    """给缺结果的那批 tool_calls 补一条结果，保持消息历史合法。

    中断/异常可能落在工具循环之外，此时 assistant 的 tool_calls 已入历史却没人应答，
    下一次请求会被接口判为格式错误；这里统一补齐。返回补了几条。
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
    # 本轮内第一次 Ctrl+C 只中断本轮（见 _handle_sigint）；busy 由 main 在结束时复位
    _INTERRUPT["busy"], _INTERRUPT["seen"] = True, False
    # 每轮开始前先瘦身：只保留最近 keep_turns 轮的工具往返，更早的调用与结果全部移除
    dropped = _trim_history(messages)
    if dropped:
        out.log("TRIM", "已移除较早轮次的工具调用与结果: %d 条消息" % dropped)
    messages.append({"role": "user", "content": user_text})
    repeat = {"key": None, "n": 0}  # 连续相同工具调用的护栏

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
            interrupted = False
        except TurnInterrupted:
            interrupted = True
        finally:
            if cur_kind["v"] is not None:
                out.stream_end()
        if interrupted:
            # 生成中途被打断：assistant 消息还没入历史，直接结束本轮即可
            out.info("\n[已中断本次生成，本轮结束；再按一次 Ctrl+C 退出程序]")
            return

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

        # content 统一成字符串：非流式回退时接口可能给出 null，部分服务端会判为格式错误
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
                abort_note = "错误: 本轮已因重复调用中止，本次调用未执行。"
                abort_info = "检测到连续 %d 次完全相同的调用，已停止本轮" % MAX_REPEAT_CALLS
                result = ("错误: 完全相同的 %s 调用已连续出现 %d 次，已中止本轮，本次调用未执行。"
                          "请换一种思路，或直接说明结论。" % (name, repeat["n"]))
            else:
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        args = {}
                except ValueError as e:
                    result = "错误: 无法解析工具参数 JSON: %s" % e
                else:
                    try:
                        result = execute_tool(name, args, opts)
                    except TurnInterrupted:
                        # 工具执行到一半被打断：结果不可信，也不继续跑后续调用
                        result = ("错误: 用户按 Ctrl+C 中断了本轮，本次调用未正常结束，"
                                  "请勿继续调用工具，等待用户下一步指示。")
                        aborted = True
                        abort_note = "错误: 本轮已被 Ctrl+C 中断，本次调用未执行。"
                        abort_info = "已中断本轮（Ctrl+C），再按一次 Ctrl+C 退出程序"

            out.log("TOOL_RESULT " + (name or "?"), result)
            out.tool_result(result, _is_error(result))
            messages.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})

        if aborted:
            out.info("\n[%s]" % abort_info)
            return

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
               "显式传入的连接类参数（-m/--model、-b/--base-url、-k/--api-key、-r/--root、"
               "-s/--max-steps、-t/--http-timeout、-l/--log）会加密记录到当前目录的 .bo，"
               "之后不传参或只传部分参数时自动复用；删除 .bo 即恢复默认。"
               "优先级: 命令行 > 环境变量 > .bo > 内置默认。")
    parser.add_argument("-m", "--model", default=S,
                        help="模型名（默认取环境变量 MODEL / OPENAI_MODEL，其次取 .bo）")
    parser.add_argument("-b", "--base-url", default=S,
                        help="OpenAI 兼容接口地址（默认取 OPENAI_BASE_URL，其次取 .bo）")
    parser.add_argument("-k", "--api-key", default=S,
                        help="API 密钥（默认取 OPENAI_API_KEY，其次取 .bo）")
    parser.add_argument("-y", "--yes", action="store_true", dest="confirm",
                        help="开启命令执行前的逐条人工确认（不加则命令默认直接放行）")
    parser.add_argument("-r", "--root", default=S, metavar="DIR",
                        help="限制文件类工具（read_file/write_file/search）与 run_command 的 cwd 只能访问该目录之内（默认不限制）")
    parser.add_argument("-s", "--max-steps", type=int, default=S, help="单轮最多工具调用轮数")
    parser.add_argument("-t", "--http-timeout", type=int, default=S, help="单次 HTTP 请求超时秒数")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="只显示最终答复，隐藏全部工具/思考过程")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="提高显示级别：-v 显示工具结果与思考，-vv 显示完整明细")
    parser.add_argument("-C", "--no-color", action="store_true",
                        help="关闭彩色输出（默认仅在终端下着色）")
    parser.add_argument("-l", "--log", nargs="?", const="BO.log", default=S, dest="log_path", metavar="FILE",
                        help="把完整交互记录写入日志文件（默认 BO.log），屏幕上不显示明细")
    parser.add_argument("-L", "--no-log", action="store_const", const=None, default=S, dest="log_path",
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

    # 只把本次显式传入的连接类参数写回 .bo，保留文件中其它键（键名同 CONFIG_KEYS）
    resolved = {"model": model, "base_url": base_url, "api_key": api_key, "root": root,
                "max_steps": max_steps, "http_timeout": http_timeout, "log_path": log_path}
    updates = {k: resolved[k] for k in CONFIG_KEYS if k in a_vars}

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
    install_sigint_handler()

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
                                 "  exit     退出\n"
                                 "  Ctrl+C   第一次只中断当前一轮（生成/命令），再按一次退出\n")
                continue

            try:
                run_turn(user, messages, opts, out)
            except KeyboardInterrupt:   # 本轮内连按两次 Ctrl+C
                sys.stdout.write("\n再见。\n")
                break
            except TurnInterrupted:     # 兜底：中断落在生成/工具循环之外
                _close_dangling_tool_calls(
                    messages, "错误: 本轮已被 Ctrl+C 中断，本次调用未执行。")
                out.info("\n[已中断本轮；再按一次 Ctrl+C 退出程序]")
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
        # 兜底：在错误/中断的收尾处理中再按一次 Ctrl+C，避免直接抛 traceback
        sys.stdout.write("\n再见。\n")
    finally:
        out.log("SESSION END", "编号: %d\n耗时: %.1fs" % (out.session_id, time.time() - started))
        out.close()


if __name__ == "__main__":
    main()
