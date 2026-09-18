#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查源码里是否用了 Python 3.6 不支持的特性（bo.py 需向下兼容 3.6）。

用法:
    python3 tools/py36check.py bo.py bo_en.py

退出码: 0 通过 / 1 发现问题 / 2 用法错误

注意: 本脚本自身也必须能在 Python 3.6 上运行，因此只使用 3.6 已有的 API；
新语法节点用 getattr 探测（在旧版 ast 里不存在）。
"""

import ast
import re
import sys

# 3.7+ 才有的标准库模块（含子模块）
BAD_MODULES = frozenset((
    "dataclasses", "contextvars", "importlib.resources", "importlib.metadata",
    "graphlib", "zoneinfo", "tomllib", "typing_extensions",
))
# subprocess.run 里 3.7+ 才有的关键字参数
BAD_RUN_KWARGS = frozenset(("capture_output", "text", "encoding", "errors"))
# 3.7+ / 3.9+ 才有的方法
BAD_METHODS = {"reconfigure": "3.7+", "removeprefix": "3.9+",
               "removesuffix": "3.9+", "is_relative_to": "3.9+"}
# 新版 ast 才有的节点：属性名 -> 说明
NEW_NODES = (("NamedExpr", "海象运算符 := （需要 3.8+）"),
             ("Match", "match 语句（需要 3.10+）"),
             ("TryStar", "except* （需要 3.11+）"))
F_STRING_EQ = re.compile(r"""f["'][^"'\n]*\{[^{}\n]*=\}""")
# 拆成两段拼接，避免本行文本被自己的正则命中
FUTURE_ANNOTATIONS = re.compile(r"from\s+__future__\s+import\s+.*\banno"
                                r"tations\b")


def _bad_module(name):
    """模块名（或它的父包）是否属于 3.7+ 模块。"""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        if ".".join(parts[:i]) in BAD_MODULES:
            return True
    return False


def check(path, issues):
    with open(path, "rb") as f:
        text = f.read().decode("utf-8")
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        issues.append("%s:%s: 语法解析失败（%s），请在 3.6 上人工确认" % (path, e.lineno, e.msg))
        return

    args_cls = getattr(ast, "arguments", None)  # 3.8 也有 ast.Arguments，这里取通用名
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        for attr, msg in NEW_NODES:
            cls = getattr(ast, attr, None)
            if cls is not None and isinstance(node, cls):
                issues.append("%s:%d: %s" % (path, line, msg))
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            cur = node.module or ""
            names = [(cur + "." + a.name).lstrip(".") for a in node.names]
        else:
            names = []
        for n in names:
            if _bad_module(n):
                issues.append("%s:%d: 导入了 Python 3.7+ 才有的模块: %s" % (path, line, n))
        if args_cls is not None and isinstance(node, args_cls) and getattr(node, "posonlyargs", None):
            issues.append("%s:%d: 位置-only 参数 / （需要 3.8+）" % (path, line))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            fn = node.func
            if fn.attr == "run" and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess":
                for kw in node.keywords:
                    if kw.arg in BAD_RUN_KWARGS:
                        issues.append("%s:%d: subprocess.run 用了 3.7+ 参数 %s=" % (path, line, kw.arg))
            if fn.attr in BAD_METHODS:
                issues.append("%s:%d: .%s() 需要 Python %s" % (path, line, fn.attr, BAD_METHODS[fn.attr]))

    for i, line in enumerate(text.split("\n"), 1):
        if F_STRING_EQ.search(line):
            issues.append("%s:%d: f-string 的 = 说明符（需要 3.8+）" % (path, i))
        if FUTURE_ANNOTATIONS.search(line):
            # 提示语不写成完整语句，避免本行被自己的正则命中
            issues.append("%s:%d: from __future__ 导入 annotations（需要 3.7+）" % (path, i))


def main(argv):
    paths = argv[1:]
    if not paths:
        sys.stderr.write("用法: python3 tools/py36check.py 文件...\n")
        return 2
    issues = []
    for p in paths:
        try:
            check(p, issues)
        except IOError as e:
            issues.append("%s: 无法读取: %s" % (p, e))
    if issues:
        sys.stdout.write("发现 %d 处可能不兼容 Python 3.6 的写法:\n" % len(issues))
        for s in issues:
            sys.stdout.write("  " + s + "\n")
        return 1
    sys.stdout.write("通过: %s 未发现 3.6 不兼容写法（当前解释器 %s）\n"
                     % (", ".join(paths), sys.version.split()[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
