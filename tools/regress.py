#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BO 工具层回归：直接调用工具函数，不访问模型接口。

用法:
    cd <项目根目录> && python3 tools/regress.py

退出码: 0 全部通过 / 1 有失败。脚本自身需兼容 Python 3.6。
"""

import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bo  # noqa: E402

OPTS = {"root": None, "cwd": os.getcwd(), "confirm": False}
FAILS = []


def check(name, cond, extra=""):
    if cond:
        sys.stdout.write("  ok   %s\n" % name)
    else:
        sys.stdout.write("  FAIL %s  %s\n" % (name, extra))
        FAILS.append(name)


def w(path, data, enc=None):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    if not isinstance(data, bytes):
        data = data.encode(enc or "utf-8")
    with open(path, "wb") as f:
        f.write(data)
    return path


# --------------------------------------------------------------------------
# read_file
# --------------------------------------------------------------------------

def test_read_file(d):
    sys.stdout.write("read_file\n")
    empty = w(os.path.join(d, "empty.txt"), b"")
    check("空文件", "是空文件" in bo.tool_read_file({"path": empty}, OPTS))

    nonl = w(os.path.join(d, "nonl.txt"), b"a\nb\nc")
    out = bo.tool_read_file({"path": nonl}, OPTS)
    check("无尾换行计 3 行", "共 3 行" in out, out[:60])

    crlf = w(os.path.join(d, "crlf.txt"), b"a\r\nb\r\nc\r\n")
    out = bo.tool_read_file({"path": crlf}, OPTS)
    check("CRLF 行数", "共 3 行" in out, out[:60])
    check("CRLF 行尾无 \\r", "\r" not in out, repr(out[:80]))

    vt = w(os.path.join(d, "vt.txt"), b"a\x0bb\x0cc\n")
    check("\\x0b/\\x0c 不当行分隔", "共 1 行" in bo.tool_read_file({"path": vt}, OPTS))

    gbk = w(os.path.join(d, "gbk.txt"), "中文内容\n第二行\n".encode("gbk"))
    out = bo.tool_read_file({"path": gbk}, OPTS)
    check("GBK 中文解码", "中文内容" in out and "第二行" in out, repr(out[:60]))

    binf = w(os.path.join(d, "bin.dat"), b"\x00\x01\x02abc\n")
    check("二进制拒绝读取", "疑似二进制文件" in bo.tool_read_file({"path": binf}, OPTS))

    check("offset 越界提示", "已超出文件末尾" in bo.tool_read_file({"path": nonl, "offset": 99}, OPTS))
    check("文件不存在", "文件不存在" in bo.tool_read_file({"path": os.path.join(d, "nope.txt")}, OPTS))

    sub = os.path.join(d, "dir", "sub")
    os.makedirs(sub)
    w(os.path.join(d, "dir", "f1.txt"), b"x")
    os.symlink("f1.txt", os.path.join(d, "dir", "link1"))
    out = bo.tool_read_file({"path": os.path.join(d, "dir")}, OPTS)
    check("目录列举", "共 3 项" in out and "f1.txt" in out and "link1 ->" in out, out[:100])

    big = os.path.join(d, "big.txt")
    with open(big, "w") as f:
        for i in range(400000):
            f.write("line %07d abcdefghijklmnop\n" % i)
    out = bo.tool_read_file({"path": big, "offset": 399998, "limit": 5}, OPTS)
    check("大文件(>3MB)拒绝读取", "文件过大" in out and "上限 3 MB" in out, out[:100])


# --------------------------------------------------------------------------
# write_file
# --------------------------------------------------------------------------

def test_write_file(d):
    sys.stdout.write("write_file\n")
    f = os.path.join(d, "w", "a.py")
    out = bo.tool_write_file({"path": f, "content": "import os\n\ndef f():\n    return 1\n"}, OPTS)
    check("新建含父目录", "已新建" in out and os.path.isfile(f), out)

    out = bo.tool_write_file({"path": f, "content": "import os\nprint(1)\n"}, OPTS)
    check("整体覆盖", "已覆盖" in out and open(f).read().endswith("print(1)\n"), out)

    out = bo.tool_write_file({"path": f, "old_string": "print(1)", "new_string": "print(2)"}, OPTS)
    check("精确替换", "共替换 1 处" in out and "print(2)" in open(f).read(), out)

    out = bo.tool_write_file({"path": f, "old_string": "print(2)", "new_string": "X"}, OPTS)
    check("唯一命中时报日期", "共替换 1 处" in out, out)
    out = bo.tool_write_file({"path": f, "old_string": "o", "new_string": "0"}, OPTS)
    check("不唯一报错并给候选", "不唯一" in out and "候选位置" in out, out)

    out = bo.tool_write_file({"path": f, "old_string": "o", "new_string": "0", "replace_all": True}, OPTS)
    check("replace_all", "共替换" in out and "不唯一" not in out, out)

    w(f, "def f(a):   \n    return a  \n")
    out = bo.tool_write_file({"path": f, "old_string": "def f(a):\n    return a", "new_string": "def g(a):\n    return a"}, OPTS)
    check("容错替换(行尾空白)", "已忽略行尾空白" in out and "def g(a):" in open(f).read(), out)

    w(f, "alpha\nbeta\ngamma\n")
    out = bo.tool_write_file({"path": f, "edits": [
        {"old_string": "alpha", "new_string": "A"},
        {"old_string": "gamma", "new_string": "G"}]}, OPTS)
    body = open(f).read()
    check("edits 两条", "共替换 2 处" in out and body == "A\nbeta\nG\n", out)

    before = open(f).read()
    out = bo.tool_write_file({"path": f, "edits": [
        {"old_string": "beta", "new_string": "B"},
        {"old_string": "不存在的内容", "new_string": "y"}]}, OPTS)
    check("edits 失败整单不写入", "本次未做任何写入" in out and open(f).read() == before, out)

    check("空 content 拒绝", "content 为空" in bo.tool_write_file({"path": f, "content": ""}, OPTS))
    out = bo.tool_write_file({"path": f, "content": "z", "old_string": "a", "new_string": "b"}, OPTS)
    check("content 与替换互斥", "不能同时使用" in out, out)
    out = bo.tool_write_file({"path": f, "replace_all": True}, OPTS)
    check("只给 replace_all 时点名提示", "replace_all" in out and "未生效" in out, out)
    out = bo.tool_write_file({"path": os.path.join(d, "nope.py"), "old_string": "a", "new_string": "b"}, OPTS)
    check("替换不存在的文件", "文件不存在" in out, out)

    gbk = os.path.join(d, "w", "g.txt")
    w(gbk, "第一行 GBK 内容\n".encode("gbk"))
    out = bo.tool_write_file({"path": gbk, "content": "第一行\n第二行 emoji \U0001f642\n"}, OPTS)
    check("编码回退有提示", "已改用 utf-8" in out, out)
    w(gbk, "第一行 GBK 内容\n".encode("gbk"))
    out = bo.tool_write_file({"path": gbk, "old_string": "第一行", "new_string": "第一行\U0001f642"}, OPTS)
    check("编辑模式编码不符时报错", "无法用原编码" in out, out)
    check("出错后文件未被破坏", "第一行 GBK 内容" in open(gbk, "rb").read().decode("gbk"))


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def ref_matched(root, pattern, glob="*"):
    """参照实现：逐行匹配，不做整文件预筛。"""
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    n = 0
    for p in bo._iter_search_files(root, glob):
        try:
            if os.path.getsize(p) > bo.MAX_SEARCH_FILE_BYTES:
                continue
            with open(p, "rb") as fh:
                raw = fh.read()
        except Exception:
            continue
        if b"\x00" in raw[:8192]:
            continue
        for ln in bo._split_lines(bo._decode_bytes(raw)[0]):
            if rx.search(ln):
                n += 1
    return n


def test_search(d):
    sys.stdout.write("search\n")
    s = os.path.join(d, "s")
    w(os.path.join(s, "a.py"), b"import os\nx = 1\nqq a\n")
    w(os.path.join(s, "b.py"), b"alpha\ndef foo\naa\Ztail\n")
    w(os.path.join(s, "crlf.txt"), b"z\r\nx\r\nimport os\r\n")
    w(os.path.join(s, "gbk.txt"), "中文\nimport os\n".encode("gbk"))
    w(os.path.join(s, "sub", "c.py"), b"import sys\n")
    w(os.path.join(s, "bin.dat"), b"\x00\x01abc")

    pats = [r"^import", r"\Aa", r"a\Z", r"z$", r"(?-m:^a)", r"$", r"^", r"\bfoo\b",
            r"(?s)x.y", r"import os", r"aa"]
    bad = []
    for pat in pats:
        for glob in ("*", "*.py"):
            got = bo.tool_search({"pattern": pat, "path": s, "glob": glob}, OPTS)
            m = re.search(r"命中 (\d+) 处", got)
            n = int(m.group(1)) if m else 0
            if n != ref_matched(s, pat, glob):
                bad.append("%s/%s" % (pat, glob))
    check("预筛与逐行结果一致（边界 pattern）", not bad, "不一致: %s" % bad)

    out = bo.tool_search({"pattern": "import os", "path": s, "glob": "*.py"}, OPTS)
    check("glob 过滤", "命中 1 处" in out, out[:80])
    out = bo.tool_search({"pattern": "import os", "path": s}, OPTS)
    check("无 glob 时含 GBK 文件", "命中 3 处" in out, out[:80])
    out = bo.tool_search({"pattern": "import(", "path": s}, OPTS)
    check("非法正则按字面量", "不是合法正则" in out, out[:120])
    out = bo.tool_search({"pattern": "\x00", "path": s}, OPTS)
    check("二进制文件跳过", "命中 0 处" in out or "无命中" in out, out[:80])
    out = bo.tool_search({"pattern": "a", "path": s, "max_results": 1000, "context_lines": 3}, OPTS)
    check("输出不超过字符预算", len(out) <= bo.MAX_OUTPUT_CHARS + 200, "长度 %d" % len(out))


# --------------------------------------------------------------------------
# 性能守卫（阈值留了 ~20 倍余量，避免慢机器误报）
# --------------------------------------------------------------------------

def test_perf(d):
    sys.stdout.write("性能守卫\n")
    body = "".join("line %06d with filler text for scoring\n" % i for i in range(20000))

    t0 = time.time()
    bo._candidates(body, "zzz_not_here_at_all", [])
    dt = time.time() - t0
    check("候选项报告 20000 行 < 1.0s", dt < 1.0, "实际 %.3fs" % dt)

    big = ("alpha beta gamma delta " * 40 + "\n") * 500
    t0 = time.time()
    r = bo._apply_edit(big, {"old_string": "\n", "new_string": "\n# tag ", "replace_all": True})
    dt = time.time() - t0
    check("replace_all 500 处 < 0.5s", dt < 0.5 and r[1] == 500, "实际 %.3fs / %d 处" % (dt, r[1]))

    fuzzy = ("def f_%d(a, b):   \r\n    return a + b   \r\n" % 0) * (1024 * 1024 // 40)
    t0 = time.time()
    spans = bo._locate(fuzzy, "def f_0(a, b):\n    return a + b")[0]
    dt = time.time() - t0
    check("容错定位 1MB < 3.0s", dt < 3.0 and spans, "实际 %.3fs" % dt)

    files = os.path.join(d, "many")
    os.makedirs(files)
    for i in range(200):
        w(os.path.join(files, "f%03d.py" % i), b"# nothing here\n" * 200)
    t0 = time.time()
    bo.tool_search({"pattern": "zzz_never_matches", "path": files}, OPTS)
    dt = time.time() - t0
    check("search 200 文件 < 2.0s", dt < 2.0, "实际 %.3fs" % dt)


def main():
    d = tempfile.mkdtemp(prefix="bo-regress-")
    sys.stdout.write("工作目录: %s\n" % d)
    try:
        test_read_file(d)
        test_write_file(d)
        test_search(d)
        test_perf(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if FAILS:
        sys.stdout.write("\n失败 %d 项: %s\n" % (len(FAILS), "; ".join(FAILS)))
        return 1
    sys.stdout.write("\n全部通过\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
