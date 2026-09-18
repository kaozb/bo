# BO 项目约定（长期记忆）

## 项目概况

BO 是一个单文件、纯标准库的最小编码智能体，通过 OpenAI 兼容接口（/v1/chat/completions）驱动，
提供 4 个工具：

- `read_file`    读文件（带行号、支持 offset/limit 续读）/ 列目录
- `write_file`   一个工具两种模式：给 `content` 新建或整体覆盖；给 `old_string` + `new_string`
                 （或 `edits`）对已有文件做精确替换。**不拆成 write + edit 两个工具**（多一份定义、
                 多一轮模型决策），删除/移动文件交给 `run_command` 的 rm / mv，也不要新增
                 delete_file / move_file。
- `search`       正则搜索（整文件预筛 + 逐行匹配）
- `run_command`  shell 执行（PIPE + 采集线程，超时按进程组击杀）

文件构成：

- `bo.py`     中文版，**功能改动的唯一来源（single source of truth）**
- `bo_en.py`  英文版，与 bo.py 结构一一对应，仅注释与用户可见文案不同（命名、逻辑、顺序保持一致）
- `README.md` 文档，以中文版为准，开头有 `[English]` 锚点链接
- `tools/py36check.py`  Python 3.6 兼容扫描（禁用语法/API 清单，可独立运行）
- `tools/regress.py`    工具层回归用例 + 性能守卫（不启动模型，直接调用工具函数）
- `.bo`       运行时生成的加密参数记忆文件，不提交（已在 .gitignore）
- `agents.md` 本文件，记录跨会话约定

## 工作流程约定（重要）

1. **接到修正/修复任务时，只改 `bo.py`。** 不要在同一轮里顺手改 `bo_en.py` 或 `README.md`。
2. 只有在**准备提交（commit）之前**，才做同步：
   - 把 bo.py 的改动对应地翻译/移植到 `bo_en.py`（保持结构与行序对应，只译文案）；
   - 如果改动影响用法、参数、特性说明，再更新 `README.md`。注意 README「特性」节写明了工具个数
     （目前 4 个：read_file / write_file / search / run_command，其中 write_file 双模式），
     增删工具或改变划分时这一行必须一起改。
3. 同步后必须验证：`python3 -m py_compile bo.py bo_en.py` 通过，并抽查 `diff bo.py bo_en.py`
   确认两边只有文案差异、没有逻辑漂移。
4. 提交信息用中文 `fix:` / `feat:` 前缀，保持现有风格；提交前确认 `.bo`、`BO.log`、`__pycache__` 未被纳入。

## 编码约束

- 必须兼容 **Python 3.6+**：不用 3.7+ 专有 API（如 `subprocess.run(capture_output=/text=)`、
  `stream.reconfigure` 直接调用、海象运算符 `:=`、dataclasses、`f"{x=}"`）。
  需要 UTF-8 时按 bo.py 里的 `_force_utf8` 方式做 getattr 探测 + TextIOWrapper 兜底。
  改完必须跑 `python3 tools/py36check.py bo.py`（会扫出上面这些写法）。
- **零第三方依赖**，只用标准库；保持单文件，不拆模块（`tools/` 下的脚本是开发工具，不属于运行时）。
- 用户可见输出保持中文（bo.py）/ 英文（bo_en.py）两套，新增文案要同时留位置。
- 修改代码时尽量小步精确（write_file 的 old_string/new_string），不要整文件重写。
- 优先用 C 级原语而不是 Python 级逐元素循环：`str.find/replace/count/join`、`bytes.count`、
  `os.scandir`、`io.TextIOWrapper` + `itertools.islice`。这些既是「原生的方案」也是性能主要来源。

## 工具层现状（2025-09-18 整改完成，均已实测）

性能相关的关键设计，改动时不要回退：

- **search 整文件预筛**：先 `re.compile(pattern, flags | re.MULTILINE)` 对全文 `search`，不命中就跳过
  整个文件的 `splitlines` + 逐行匹配（730 文件 1.6s → 0.23s）。
  启用条件在 `_search_prefilter`：pattern 含 `\A` / `\Z` / `(?-m` 时不用；
  pattern 含 `$` 且正文含 `\r`（CRLF 文件）时也不用——这两类会让预筛漏报（已用反例验证）。
- **write_file 精确 replace_all 直接走 `str.replace`**（460KB/500 处：0.64s → 0.006s）；
  容错路径仍按跨度拼接，但用 join 而不是反复切片。
- **`_candidates` 未命中路径用片段子串扫描**（`_probe_fragments` + `str.find`），
  不再对每一行跑 `difflib.SequenceMatcher`（30000 行：12.8s → 0.02s）。
- **_locate 容错分支按行比对**（`_locate_fuzzy`）而不是逐字符建下标映射（2MB：5.1s → 0.7s）。
- **_read_lines_window**：小文件整读并用 `_decode_bytes` 探测编码；>10MB 大文件两遍扫描
  （分块 `bytes.count(b"\n")` 数行 + `TextIOWrapper(newline="")` + `islice` 取窗口）。
- **_list_dir 用 os.scandir**（DirEntry 缓存类型/大小，5000 项：0.32s → 0.02s）。
- **行切分统一走 `_split_lines`**：只按 `\n` 断行、去掉行尾 `\r`、忽略末尾空行。
  因此 `\x0b` / `\x0c` / `\x85` / `\u2028` 不再被当行分隔（与编辑器行号一致）。

已知的行为变更（有意为之，勿当 bug 改回）：

- 行级容错定位不再把「被替换文本之后的」行尾空白与 `\r` 一起吞掉。
- 整体写入 GBK 文件时若新内容无法用原编码表示，会回退 utf-8 **并在结果里明说**（编辑模式仍报错、不写盘）。
- `write_file` 只给 `replace_all`（缺 old_string）会点名提示该参数被忽略，而不是笼统报「缺少参数」。

已删除的符号（不要重新引入）：`_looks_binary`（并入 `_read_lines_window` 的一次 open）、
`_norm_text`、`MAX_SEARCH_LINES`（改用 `MAX_OUTPUT_CHARS` 字符预算）、`import codecs`。

## bo.py 去重合并（本轮，只动结构、不动行为，已用回归 + 等价性抽查验证）

- `_decode_bytes` 改为单趟解码（utf-8 → gbk → latin-1），`_detect_encoding` 变为它的薄封装
  （原来是「先探测再解码」两遍解码）；两者判定结果完全一致。
- `_atomic_write(path, data, mode=None)` 增加权限参数：mode 为 None 时保留原权限（新文件 0644），
  否则用给定权限。`save_config` 直接复用它，不再自备一套 mkstemp/fsync/replace（.bo 仍是 0600）。
- `execute_tool` 改为查 `TOOL_FUNCS` 字典分派；该字典的键必须与 `TOOLS` 里的工具名一致。
- `parse_args` 写回 .bo 的 `updates` 由 `CONFIG_KEYS` 循环生成（键名就是 CONFIG_KEYS）。
- `Output.stream_begin/stream_end` 统一走 `_w`；`tool_read_file` 的 offset 计算提到分支之前；
  `_candidates` 未命中路径复用 `_line_of`（1 基行号）。
- 体积 1735 → 1697 行。**评估过但不值得做**（收益小于改动成本，勿为行数硬拆）：
  `_write_whole_file` / `_edit_existing_file` 里相同的 `_atomic_write` try/except 包装、
  `_read_file_bytes` 与 `_read_lines_window` 的重复大小检查、`MAX_READ_BYTES // 1048576` 的重复算式。

## 对话历史瘦身（本轮新增，只动 bo.py）

- `TRIM_KEEP_TURNS = 1`（顶部常量）+ `_trim_history(messages, keep_turns)`：**每轮开始、append 当前
  user 消息之前**调用，把「上上轮及之前」的工具调用与结果全部移除，只保留最近 1 轮的完整工具往返。
- 轮次边界 = `role == "user"` 的消息；system 消息始终保留。移除内容：`role == "tool"` 的结果消息，
  以及 assistant 消息的 `tool_calls` 字段（去掉后 content 为空则整条删除）。
  user 输入与 assistant 正文保留，所以对话主线不丢，只是不再无限膨胀。
- 丢掉纯工具往返的 assistant 消息后可能出现连续两条 user，`_trim_history` 会把它们合并（`\n\n` 连接）
  以维持角色交替；`keep_turns` 可调（=2 表示保留最近两轮）。
- 裁剪是静默的，只在 `BO.log` 里记一条 `TRIM ... N 条消息`；日志仍保留完整原始记录。
- 测试脚本在 `/tmp/test_trim.py`（6 个场景，不入库）；改动后用 `python3 /tmp/test_trim.py` 复验，
  或至少确认「单轮不裁剪 / 幂等 / 保留段 tool_call_id 与 tool_calls 成对」。

## Ctrl+C 语义（本轮新增，只动 bo.py）

- `TurnInterrupted(BaseException)` + `_INTERRUPT = {"busy", "seen"}` + `_handle_sigint` /
  `install_sigint_handler`（放在顶部常量区下方）：**一轮之内第一次 Ctrl+C 只中断本轮，第二次
  （或空闲时按一次）退出程序**。必须继承 BaseException，否则会被 `call_llm` 里的
  `except Exception`（转成「读取流式响应失败」RuntimeError）等地方吞掉。
- `run_turn` 开头置 `busy=True, seen=False`，main 的 `finally` 复位 busy，所以计数是每轮独立的。
- 中断落点三处：`call_llm` 调用处（assistant 消息还没入历史，直接 return）；工具循环内
  `execute_tool` 处（复用 aborted / abort_note / abort_info，给未执行的 tool_calls 补结果）；
  main 的兜底 `except TurnInterrupted`（用 `_close_dangling_tool_calls` 补 assistant.tool_calls
  缺的 tool 结果，免得下一轮被接口判成格式错误）。
- `tool_run_command` 的 `proc.wait` 增加 `except TurnInterrupted: _terminate_group(proc); raise`，
  中断长命令时不留派生进程。
- 空闲（等输入）时按 Ctrl+C 仍是退出；`/help` 加了一行说明。
- 测试脚本 `/tmp/test_sigint.py`（不入库）：假 SSE 服务 + 子进程发真信号，覆盖「生成中中断 /
  二次退出 / 空闲退出 / 命令执行中中断且清理 sleep 子进程 / 中断后会话仍正常」。改动后用
  `python3 /tmp/test_sigint.py` 复验（约 30s）。

## 常用验证命令

```bash
cd /root/bo
python3 -m py_compile bo.py bo_en.py                    # 语法检查
python3 tools/py36check.py bo.py bo_en.py tools/*.py    # 3.6 兼容扫描
python3 tools/regress.py                                # 工具层回归 + 性能守卫
python3 bo.py -h                                        # 帮助/参数自检
diff bo.py bo_en.py                                     # 确认双语版仅文案差异
git status --short                                      # 提交前检查
```

## 双语同步记录（已完成，勿重做）

- **bo_en.py 已按 bo.py 逐段整体重写**（1765 行 vs 1743 行，行数差仅来自英文文案折行）：
  工具定义（4 个、write_file 双模式）、`_detect_encoding` / `_count_lines` / `_split_lines` /
  `_confirm` / `_binary_error` / `_atomic_write(mode)` / `_read_lines_window(shown)` /
  `_list_dir(offset, limit)` / `_write_whole_file` / `_edit_existing_file` / `_locate_fuzzy` /
  `_probe_fragments` / `_search_prefilter` / `_collect_process_output` / `_command_output_text` /
  `TOOL_FUNCS` / `MAX_COMMAND_OUTPUT_BYTES` / `MAX_DIR_ITEMS` / `MAX_REPEAT_CALLS` /
  `TRIM_KEEP_TURNS` / `_trim_history` / run_turn 的 trim 与重复调用护栏，全部一一对应。
  英文版已删除 `edit_file`、`_norm_text`、`import codecs`。
- README 中英两节均已改为「四个工具」+ write_file 双模式 + 历史瘦身说明。
- **等价性验证脚本 `/tmp/equiv.py`（临时，不入库）**：对两个模块跑同一批操作，比较纯函数结果、
  `_trim_history` 输出、30 组工具层用例（GBK 回退、edits 原子性、search 预筛边界、run_command
  超时与大输出首尾）、`.bo` 跨版本互读与 0600 权限、`_atomic_write` 权限语义、`-h` 与启动自检；
  只比语言无关量（状态标签 + 数字序列），不比较文案。改动工具层后建议重建脚本再跑一遍。
- 结构核对命令（应只剩 LEVEL_NAMES 一行文案差异）：
  `grep -n "^def \|^class \|^    def \|^[A-Z_][A-Z_0-9]* = " bo.py|sed 's/ *#.*//'|cut -d: -f2- > /tmp/a.sym`
  对 bo_en.py 同样处理再 `diff /tmp/a.sym /tmp/b.sym`。

## bo.py 缺陷审查与修复（本轮，只动 bo.py）

用 `/tmp/audit_bo.py`（临时，不入库）实测出 6 个缺陷，已在 bo.py 修掉：

1. `_atomic_write` 的清理改为 `except BaseException`：Ctrl+C 抛的是 BaseException，原来会跳过
   unlink，在工作目录残留 `.bo-*.tmp`（已复现）。
2. `tool_run_command` 的 `proc.wait` 同时捕获 `KeyboardInterrupt` 再 `_terminate_group`：原来只有
   TurnInterrupted 会清理进程组，本轮第二次 Ctrl+C（退出）会把 `sleep 30` 这类派生进程留成孤儿（已复现）。
3. `_confirm` 不再把 Ctrl+C 当「拒绝」：只捕 EOFError，KeyboardInterrupt 交给 `_handle_sigint`
   决定（第一次中断本轮、第二次退出）。原来在命令确认提示符处按第二次 Ctrl+C 会被吞成答案 "n"。
4. `tool_run_command` 的**默认 cwd**（启动目录）也要过 `--root` 校验：原来只校验显式传入的 cwd，
   `-r DIR` 但从 DIR 之外启动时，不带 cwd 的命令可在任意目录跑，与 `-r` 的帮助文案不符。现在返回
   「默认工作目录 ... 越出允许范围」并提示先 cd 或用显式 cwd（`-r` 与启动目录相同时无感）。
5. `run_turn` 里 `assistant.content` 统一写成 `msg.get("content") or ""`：非流式回退
   （`_parse_full_response`）可能给出 `content: null`，直接回传会被部分服务端判为格式错误。
6. `main` 的 while 循环外补一层 `except KeyboardInterrupt`：在错误/中断的收尾处理里再按一次 Ctrl+C，
   原来会带 traceback 退出（except 处理块内抛出的异常不会被同一个 try 的其它 except 接住）。

验证：`regress.py` + `/tmp/test_sigint.py` + `/tmp/test_trim.py` 全过，`/tmp/audit_bo.py` 六项全 ok，
`py_compile` + `py36check.py` 通过；`/tmp/t_main_kb.py` 专门验证第 6 条（mock 掉 run_turn 与 Output.error）。

评估过但有意不改（影响小 / 只是统计口径）：

- search 输出预算耗尽时 `matched` 计数会大于实际输出的行数；
- 目标是单个文件时 search 忽略 glob；
- `.bo` 里 `max_steps` / `http_timeout` 被手工改成非数字类型时不做校验（只有本机可写，风险低）。

## 双语同步与本轮提交（已完成）

bo.py 的两批改动（Ctrl+C 语义 + 上面的 6 处修复）已按结构逐处移植到 bo_en.py：

- 常量区下方：`TurnInterrupted(BaseException)` / `_INTERRUPT` / `_handle_sigint` / `install_sigint_handler`；
- `_confirm` 只捕 EOFError；`_atomic_write` 的 `except BaseException`；
- `tool_run_command` 的 `elif opts.get("root")` 默认 cwd 校验 + `proc.wait` 的
  `except (TurnInterrupted, KeyboardInterrupt): _terminate_group; raise`；
- `_close_dangling_tool_calls`（放在 `_trim_history` 之后、`run_turn` 之前）；
- `run_turn`：`_INTERRUPT["busy"]/["seen"]` 置位、`call_llm` 的 `interrupted` 分支、
  `abort_note`/`abort_info`、`execute_tool` 的 TurnInterrupted 分支、`content or ""`、`out.info("\n[%s]" % abort_info)`；
- `main`：`install_sigint_handler()`、`/help` 的 Ctrl+C 行、`except KeyboardInterrupt` / `except TurnInterrupted`、
  `finally: _INTERRUPT["busy"] = False`、while 外层兜底 `except KeyboardInterrupt`。

README 中英两节均已补：特性里加 Ctrl+C 一行、`-r` 行注明「默认工作目录同样受限」、交互命令表加 Ctrl+C 行。

验证记录（本轮实测全过）：

- `py_compile` / `tools/py36check.py` / `tools/regress.py`；符号表 diff 只剩 `LEVEL_NAMES` 一行（预期）；
- `/tmp/equiv.py` 全部等价；
- `/tmp/test_sigint.py`（中文版全过）+ `/tmp/test_sigint_en.py`（英文版，由前者替换 BO 路径、
  提示符 `you >`、四处文案断言、`import bo_en as bo` 生成）全过；
- `/tmp/test_trim.py` 6 场景通过。

## 待办

- 无（下次动 bo.py 后仍按「先只改 bo.py、提交前再同步 bo_en.py / README」的老流程走）。

## 环境

- 开发机：Linux armv7l，Python 3.11.2（但代码需向下兼容 3.6；本机没有 3.6，只能用
  `tools/py36check.py` 做 API 引入版本核对 + 语法扫描，无法真机验证）
- 运行参数记忆优先顺序：命令行 > 环境变量 > `.bo` > 内置默认
