本文是指导你怎么干活的指导手册
## 文档维护规则

1. 本文1-80行禁止修改
2. 80行后的内容与实际不符时，更新本文以反映实际
4. 本文超过500行必须主动压缩至500行以内
5. 一旦读到本文，请记住需要遵守本文的要求

## 核心原则

1. 先理解再动手：通读任务+关联代码 → 追踪端到端数据流 → 理解问题全貌
2. 最小必要实现，不写非必要代码
3. 干活前阅读 记得遵守 每次任务必须先询问模式 的要求

## 决策阶梯

写任何代码前，从第一级开始，停在第一个满足条件的台阶：

1. 需要存在吗？ → 否：跳过
2. 代码库已有？ → 复用，不重写
3. 标准库能做？ → 用它
4. 平台原生特性覆盖？ → 用它
5. 已安装依赖能解决？ → 用它
6. 一行代码能搞定？ → 就一行
7. 走到这里：写最少能工作的代码

阶梯覆盖设计、编码、评审三阶段。选方案前必须读完任务和涉及代码，追踪完整执行路径。方案上可以懒，阅读上不可以懒。

## 设计

少写代码的方式是更早说不。

- **质疑需求**：阶梯第一级在设计阶段最有力。"需要这功能吗？"——已有方案能覆盖，新功能整个跳过。
- **用问题回应**：复杂需求先给最简方案，反问"X 做了；Y 已覆盖。真的要完整 X？要就说。"不等答案，给默认。
- **不建"将来可能用"的架构**：无第二个实现 → 不建接口；无第二个产品 → 不建工厂；未变过的值 → 不加配置。
- **数据结构选对，代码自然短**：DB 约束替代应用层校验；jsonb 替代一对多关联表；窗口函数替代应用层聚合——DB 能做的，代码不重复。
- **仅一处调用的抽象是冗余，删除。**

## 编码

- 不引入无明确需求的抽象。
- 不引入新依赖，除非现有方案无法满足。
- 不要无调用方的样板代码。
- 删优于加。简单优于聪明。文件越少越好。
- 最短能工作的 diff 即赢家——前提是已理解问题。在最错处做最小改动不是懒，是第二个 bug。
- 两个标准库方案等长时，选正确处理边界条件的。懒 = 少写代码，≠ 用更脆弱的算法。
- 修 bug = 治根本，不治症状。检查所有调用方，在共享函数修一次——一处 guard 比每处各加 diff 更短。只修点名路径让其他调用方继续破损。
- 非平凡逻辑必须附带一个可运行检查：最小能证明逻辑出错的断言或小测试。不要测试框架、不要 fixtures。一行平凡代码不需要。
- 刻意简化标记：省略边界条件（全局锁、O(n²)、朴素启发式）时，用 `ponytail:` 注释标注上限和升级路径。

## 评审

评审只盯一个问题：**哪些代码可以不写。**

- 范围：只查过度工程和复杂度。正确性 bug、安全漏洞、性能交常规评审。
- 输出：每行一个发现，格式：**位置 → 标签 → 砍什么 → 替代方案**。
- 无可砍时说：**`Lean already. Ship.`**

五个标签，按可砍代码量从大到小：

| 标签 | 命中场景 | 替代方案 |
|------|---------|---------|
| `delete:` | 死代码、未用灵活性、投机功能 | 无 |
| `stdlib:` | 手写代码替代了标准库已有功能 | 标准库函数名 |
| `native:` | 库或代码做了平台原生的事 | 平台特性名 |
| `yagni:` | 单一实现抽象、无人设配置、单调用方层 | 内联，等第二个出现 |
| `shrink:` | 同逻辑可更短 | 给出更短写法 |
评审终点不是"代码好"，是"diff 变短了"。

## 安全红线

以下永不在砍代码讨论范围，设计/编码/评审三阶段均不可触碰：

| 红线 | 说明 |
|------|------|
| 信任边界输入验证 | 路径穿越、SQL 注入、XSS——永为必经检查点 |
| 防数据丢失的错误处理 | 涉及数据写入/删除时，错误处理不可省略 |
| 安全性 | 身份验证、授权、加密——不可妥协 |
| 可访问性 | 原生元素自带无障碍，自定义组件需额外保障 |
| 真实硬件校准 | 平台非理想环境：时钟漂移、传感器误差等 |

## 用户习惯
//本节单独记录用户的调试、测试、环境情况、开发习惯（当侦测到上述内容在本节增删改）
- 调试/迭代期间只动 `bo.py`，不顺手改 bo_en.py；只在提交时才把改动同步到 bo_en.py（详见「工作流程约定」）。




# BO 项目约定（长期记忆）

## 项目概况

BO 是单文件、纯标准库的最小编码智能体，走 OpenAI 兼容接口（/v1/chat/completions），4 个工具：

- `read_file`   读文件（带行号、offset/limit 续读）/ 列目录
- `write_file`  双模式：`content` 新建或整体覆盖；`old_string`+`new_string`（或 `edits`）精确替换。
                **不拆成 write + edit 两个工具**，删除/移动交给 `run_command` 的 rm/mv，不加 delete_file/move_file
- `search`      正则搜索（整文件预筛 + 逐行匹配）
- `run_command` shell 执行（PIPE + 采集线程，超时按进程组击杀）

完整交互写入 **SQLite 会话库**（默认 `.ai.db`，`-d/--db` 或环境变量 `BO_DB` 指定；sqlite3 是标准库，
不破坏零依赖）。原 `-l/--log` / `-L/--no-log` 文件日志已删除。交互命令：`/reset` 清空并开新会话、
`/s` 载入历史会话、`/help`、`exit`；启动打印 `会话: #N` 与 `会话库: 路径`。

文件构成：

- `bo.py`     中文版，**功能改动唯一来源（single source of truth）**；当前约 2183 行，已加可执行位
- `bo_en.py`  英文版，与 bo.py 结构一一对应（同 2145 行、同符号表），仅注释与用户可见文案不同（**已同步至最新**）
- `README.md` 以中文版为准，开头有 `[English]` 锚点（**已同步至最新**）
- `tools/py36check.py` Python 3.6 兼容扫描；`tools/regress.py` 工具层回归 + 性能守卫（2 项断言过时，见待办；
  大文件上限已改为 3MB，相关断言已同步为「大文件拒绝读取」）
- `.bo`    运行时生成的加密参数记忆，不提交（已在 .gitignore）；记忆键现为 db_path（原 log_path）
- `.ai.db` 会话库（含全部对话记录），**尚未加入 .gitignore，不应提交**
- `agents.md` 本文件

## 会话数据库（.ai.db，勿回退为文件日志）

- `db_open`：sqlite3 两表——`sessions`（session_no/started_at/ended_at/status running|closed|crashed/
  model/base_url/title/prompt_tokens/completion_tokens）与 `events`（session_id, seq, ts, step, kind,
  role, content, tool_call_id…，按 (session_id, seq) 索引）。打开时把残留 running 会话标 `crashed`；
  库打不开直接 `sys.exit(1)`。`BO_VERSION`（"1.1.0-db"）写入 sessions。
- 事件 kinds：`session_begin/system/user/assistant/tool_call/tool_result/trim/reset/error/session_end`，
  全部经 `Output.log` 入库；`db_bump_tokens` 累计 usage 并把耗时写回最近一条 assistant 事件。
  思考（reasoning）从不入库，因此也不会被还原。
- title = 首条 user 消息首行（≤60 字符），仅在 title 为空时写。
- `/reset` → `_new_session`：旧会话标 closed、开新会话。`/s` → `choose_session` 列最近 10 条供选择 →
  `db_load_session` 还原 → `load_history` 走同一 `_trim_history` 并用 `_close_tool_calls` 补齐悬空
  tool 结果；切换时收尾当前会话与空壳会话，再续写被载入的 sid。

## 工作流程约定（重要）

1. **接到修正/修复任务只改 `bo.py`**，不要顺手改 `bo_en.py` 或 `README.md`。
2. **只在提交前同步**：把 bo.py 的改动对应移植到 bo_en.py（保持结构与行序）；影响用法/参数/特性才更新
   README——注意 README「特性」写明了工具个数（4 个，write_file 双模式），增删工具必须一起改。
3. 同步后必须验证：`python3 -m py_compile bo.py bo_en.py` 通过，并 `diff bo.py bo_en.py` 抽查只有文案差异。
4. 提交信息用中文 `fix:`/`feat:` 前缀；提交前确认 `.bo`、`BO.log`、`__pycache__` 未被纳入。

## 编码约束

- 兼容 **Python 3.6+**：不用 3.7+ 专有 API（`subprocess.run(capture_output=/text=)`、`stream.reconfigure`、
  `:=`、dataclasses、`f"{x=}"`）；UTF-8 按 bo.py 的 `_force_utf8`（getattr 探测 + TextIOWrapper 兜底）。
  改完跑 `python3 tools/py36check.py bo.py`。
- **零第三方依赖**，只用标准库；保持单文件不拆模块（`tools/` 是开发工具，非运行时）。
- 用户可见输出保持中文（bo.py）/ 英文（bo_en.py）两套，新增文案同时留位置。
- 修改代码小步精确（old_string/new_string），不要整文件重写。
- 优先 C 级原语而非 Python 逐元素循环：`str.find/replace/count/join`、`bytes.count`、`os.scandir`、
  `io.TextIOWrapper` + `itertools.islice`。

## 工具层关键设计（改动时不要回退）

- **search 整文件预筛**：先 `re.compile(pattern, flags|re.MULTILINE)` 对全文 search，不命中跳过
  `splitlines`+逐行匹配（730 文件 1.6s → 0.23s）。禁用条件在 `_search_prefilter`：pattern 含
  `\A`/`\Z`/`(?-m` 不用；含 `$` 且正文含 `\r`（CRLF）不用——已用反例验证。
- **write_file 精确 replace_all 走 `str.replace`**（460KB/500 处 0.64s → 0.006s）；容错路径按跨度
  拼接，但用 join 不反复切片。
- **`_candidates` 未命中路径用片段子串扫描**（`_probe_fragments`+`str.find`），不对每行跑
  `difflib.SequenceMatcher`（30000 行 12.8s → 0.02s）。
- **_locate 容错分支按行比对**（`_locate_fuzzy`）而非逐字符建下标映射（2MB 5.1s → 0.7s）。
- **_locate_tolerant 终级兜底**：精确与 `_locate_fuzzy` 都未命中时，单行 old_string 与文件某行仅差
  「空白多少/tab/CRLF」→ 删除全部空白后整行相等定位（归一化后 <4 字符放弃，防误伤），提示
  「已按整行匹配（忽略了空白差异）」。
- **_read_lines_window**：整读 + `_decode_bytes` 探测编码（utf-8→gbk→latin-1）。**不再有 >MAX_READ_BYTES
  的两遍流式扫描**：超过上限的文件由 `_read_file_bytes` 直接拒绝（见下条），因此不存在「小文件正常、
  大文件因硬编码 utf-8 而乱码」的双路径不一致。
- **`MAX_READ_BYTES = 3MB`**（原 10MB，用户 2026-09-20 要求）：read_file 读取与 write_file 整体写入
  的上限；超限一律拒绝并提示用 `run_command` 配合 head/tail/sed。`MAX_READ_MB` 供提示文案复用。
- **_list_dir 用 `os.scandir`**（5000 项 0.32s → 0.02s）。
- **行切分统一走 `_split_lines`**：只按 `\n` 断行、去行尾 `\r`、忽略末尾空行；因此 `\x0b`/`\x0c`/`\x85`/
  `\u2028` 不再当行分隔（与编辑器行号一致）。

有意为之的行为变更（勿当 bug 改回）：

- 行级容错定位不再吞掉「被替换文本之后」的行尾空白与 `\r`。
- 整体写入 GBK 文件时新内容无法用原编码表示则回退 utf-8 **并明说**（编辑模式仍报错、不写盘）。
- `write_file` 只给 `replace_all`（缺 old_string）会点名提示该参数被忽略。
- `write_file` 同时给 content 与 new_string/edits：按**局部替换**执行并忽略 content（成功时附提示），
  不再直接报「不能同时使用」；只给 content+old_string（无 new_string/edits）仍报错。
- 编辑定位新增 `_locate_tolerant` 整行匹配路径，成功提示文案为「已按整行匹配（忽略了空白差异）」，
  旧文案「已忽略行尾空白」不再出现（regress 一条断言因此过时）。

已删除的符号（不要重新引入）：`_looks_binary`（并入 `_read_lines_window` 一次 open）、`_norm_text`、
`MAX_SEARCH_LINES`（改用 `MAX_OUTPUT_CHARS` 字符预算）、`import codecs`、`-l/--log` 与 `-L/--no-log`
及 `log_path`（由 `-d/--db` 与 db_path 取代）、`TRIM_KEEP_TURNS`（由 TRIM_KEEP_CALLS 取代）、
BO.log 文件日志、`import itertools`（随大文件流式扫描一并移除）。

## `--root` 已整项移除（勿重新引入）

用户 2026-09-20 要求删掉 `-r/--root`：README/`-h`/`parse_args`/`CONFIG_KEYS` 全部去掉该参数，
`_resolve` 不再做越界校验（只保留空 path 检查与 `realpath` 规范化），`tool_run_command` 里
「默认 cwd 越出允许范围」的分支随之删除，启动也不再打印「文件访问限制」。`.bo` 里遗留的 `root` 键
被 `load_config` 按 `CONFIG_KEYS` 过滤掉、不再读取。`run_command` 本就是 shell 执行、无法用 cwd 约束，
移除后文件类工具与命令都无路径限制（这是有意为之，不是回退）。

## 历史瘦身（只动 bo.py）

- `TRIM_KEEP_CALLS = 1` + `_trim_history(messages, keep_calls)`（**已从按「轮」改为按「次」**，
  勿改回 TRIM_KEEP_TURNS）：批次 = 一条带 tool_calls 的 assistant 消息 + 其后对应的 role=tool 结果；
  只保留最近 keep_calls 个批次，更早批次的 tool 结果被移除、assistant 摘掉 tool_calls 字段
  （去掉后 content 为空则整条删）。system 与全部 user/assistant 正文保留，对话主线完整。
- 调用点两处：`run_turn` 开头（append 当前 user 消息前）与 `load_history`（/s 载入后走同一裁剪，
  保证重载后发给模型的历史形状与实时对话一致）。
- **`db_load_session` 按 `step` 合并 tool_call**（2026-09-20 修）：同一 step 内的多条 tool_call 属于
  同一条 assistant 消息（模型一次并行调用），还原时并入一条带多个 tool_calls 的 assistant。
  修复前每个 tool_call 各占一条 assistant，`/s` 后被 `_trim_history` 当成多个批次、同一步的调用被裁掉
  一部分，且形状与实时对话不一致（注释曾谎称一致）。纯正文、无工具调用的 assistant 会去掉空
  `tool_calls` 列表，避免发出 `tool_calls: []` 被判非法。
- 丢掉纯工具往返后可能连续两条 user，`_trim_history` 仍会合并（`\n\n`）维持角色交替。
- 裁剪静默，仅在会话库记一条 `trim` 事件。

## Ctrl+C 语义（只动 bo.py）

- `TurnInterrupted(BaseException)` + `_INTERRUPT = {"busy","seen"}` + `_handle_sigint` /
  `install_sigint_handler`（顶部常量区下方）：**一轮内第一次 Ctrl+C 只中断本轮，第二次（或空闲时一次）
  退出程序**。必须继承 BaseException，否则被 `call_llm` 里的 `except Exception` 吞掉。
- `run_turn` 开头置 `busy=True, seen=False`，main 的 `finally` 复位，计数每轮独立。
- 中断落点三处：`call_llm` 调用处（assistant 消息未入历史，直接 return）；工具循环 `execute_tool` 处
  （复用 aborted/abort_note/abort_info，给未执行 tool_calls 补结果）；main 兜底 `except TurnInterrupted`
  （用 `_close_dangling_tool_calls` 补 tool 结果，免得下一轮被接口判格式错误）。
- 补悬空结果有两个函数，行为不同勿合并：`_close_dangling_tool_calls` 只补最近一批、追加到末尾
  （中断兜底用）；`_close_tool_calls` 给每批就地补、插在对应 assistant 之后（/s 载入历史用）。
- `tool_run_command` 的 `proc.wait` 加 `except (TurnInterrupted, KeyboardInterrupt): _terminate_group(proc); raise`，
  中断长命令不留派生进程。空闲时按 Ctrl+C 仍退出；`/help` 有一行说明。
- main 里 `run_turn` 调用处单独捕 `KeyboardInterrupt`（本轮内第二次 Ctrl+C → 打印再见并退出）。
- 测试脚本 `/tmp/test_sigint.py`（不入库，约 30s）：假 SSE 服务 + 子进程发真信号；/tmp 脚本可能已被
  清理，需要时重建。

## 缺陷审查与修复（已修 6 处，只动 bo.py，用 /tmp/audit_bo.py 实测）

1. `_atomic_write` 清理改 `except BaseException`：Ctrl+C 是 BaseException，原来跳过 unlink 残留 `.bo-*.tmp`。
2. `tool_run_command` 的 `proc.wait` 同时捕 `KeyboardInterrupt` 再 `_terminate_group`：原来第二次 Ctrl+C
   （退出）会把 `sleep 30` 留成孤儿。
3. `_confirm` 只捕 EOFError，KeyboardInterrupt 交 `_handle_sigint`：原来第二次 Ctrl+C 在确认符被吞成 "n"。
4. `tool_run_command` 的**默认 cwd**（启动目录）也要过 `--root` 校验：原来只校验显式 cwd，`-r DIR` 但从
   DIR 之外启动时不带 cwd 的命令可在任意目录跑。现在报「默认工作目录 ... 越出允许范围」并提示先 cd。
5. `run_turn` 里 `assistant.content` 统一 `msg.get("content") or ""`：非流式回退可能给 `content: null`。
6. `main` while 循环外补一层 `except KeyboardInterrupt`：收尾处理里再按 Ctrl+C 原来带 traceback 退出。

有意不改（影响小）：search 输出预算耗尽时 `matched` 计数大于实际输出行数；目标是单文件时忽略 glob；
`.bo` 里 `max_steps`/`http_timeout` 被手改成非数字不校验。

## 结构合并与双语同步（均已完成，勿重做）

- `_decode_bytes` 单趟解码（utf-8 → gbk → latin-1），`_detect_encoding` 变为薄封装，判定一致。
- `_atomic_write(path, data, mode=None)` 增加权限参数（None 保留原权限，新文件 0644）；`save_config` 直接复用。
- `execute_tool` 查 `TOOL_FUNCS` 字典分派（键必须与 `TOOLS` 工具名一致）；`parse_args` 写回 `.bo` 的
  `updates` 由 `CONFIG_KEYS` 循环生成。
- `Output.stream_begin/stream_end` 走 `_w`；`tool_read_file` 的 offset 提前到分支前；`_candidates`
  未命中路径复用 `_line_of`。
- 上一批（瘦身/提速/Ctrl+C 修复）体积 1735 → 1697 行；此后新增会话库等功能，bo.py 现约 2183 行。
  评估过但不值得做（勿为行数硬拆）：`_write_whole_file`/`_edit_existing_file` 里
  相同的 `_atomic_write` try/except、`_read_file_bytes` 与 `_read_lines_window` 的重复大小检查、
  `MAX_READ_BYTES // 1048576` 重复算式。
- 上一批的整改/去重/瘦身/Ctrl+C 六处修复已逐处移植到 bo_en.py，README 中英两节均已更新。
- **2026-09-20 完成全量同步**：bo_en.py 由 bo.py 逐行生成（按行号做「中文行→英文行」映射，
  结构与行序不变），覆盖会话库 -d/.ai.db、/s、TRIM_KEEP_CALLS、write_file 混用语义、
  _locate_tolerant，以及本轮的去 --root、3MB 上限、db_load_session 按 step 合并；
  README 中英两节同时更新（删 -r/--root 与 -l/--log，加 -d/--db、/s、3MB 上限、会话库）。
  bo_en 因英文语序补了 4 处跨行拼接缺失的空格，并把成功文案前缀统一为 `OK:`（对应中文 `已`，
  供 `tool_write_file` 的 `result.startswith("OK:")` 判断是否追加提示）。
- 等价性脚本 `/tmp/equiv.py`（临时，不入库）：比较纯函数结果、`_trim_history` 输出、30 组工具层用例、
  `.bo` 跨版本互读与 0600、`_atomic_write` 权限、`-h` 与启动自检；只比语言无关量。改工具层后建议重建再跑。
- 符号表 diff 命令（应只剩 LEVEL_NAMES 一行差异）：
  `grep -n "^def \|^class \|^    def \|^[A-Z_][A-Z_0-9]* = " bo.py|sed 's/ *#.*//'|cut -d: -f2- > /tmp/a.sym`
  bo_en.py 同样处理，再 `diff /tmp/a.sym /tmp/b.sym`。

## Python 3.6 真机实测（Docker，已完成）

```bash
docker run --rm --network host -v /root/bo:/root/bo -w /root/bo python:3.6-slim \
  sh -c 'printf "退出\n" | python bo.py -C -b <url> -k <key> -m <model> -r /root/bo -v'
```

- 用**相同路径映射**，使 `-r /root/bo` 与 `.bo` 里记的 root 容器内外一致；`--network host` 为直连宿主假接口。
- 结论（2025-09-19，中/英文版）：启动自检、UTF-8 输出、四大工具全正常；`.bo` 跨版本双向可读
  （3.11 用 scrypt，3.6 走 pbkdf2_hmac 兼容分支），权限保持 0600。
- 以上针对会话库加入**之前**的版本；本批改动未复测（sqlite3 是标准库，3.6 理论可用，但未实测）。
- 注意容器里跑会**改写 `.bo`**（`-b/-k/-m/-r` 被记忆）：先 `cp .bo /tmp/bo.conf.bak`，验证后还原并删探针目录。
- 假接口脚本 `/tmp/fake_llm.py`（不入库）：标准库 http.server + SSE，没有真实 key 时走通全流程。

## 常用验证命令

```bash
cd /root/bo
python3 -m py_compile bo.py bo_en.py                    # 语法检查
python3 tools/py36check.py bo.py bo_en.py tools/*.py    # 3.6 兼容扫描
python3 tools/regress.py                                # 工具层回归 + 性能守卫
python3 bo.py -h                                        # 帮助/参数自检
diff bo.py bo_en.py                                     # 确认仅文案差异
git status --short                                      # 提交前检查
```

当前状态：py_compile / py36check 均过（编译时有 1 条 `invalid escape sequence '\Z'` SyntaxWarning，
来自 bo.py:988 预筛正则串未加 r 前缀，无害，顺手可修）；`regress.py` 有 2 项失败属断言过时
（见待办第 4 条），其余用例与性能守卫全过。2026-09-20 已实测：3MB 上限、去 --root、/s 形状修复
（并发 tool_call 重载后仍成组保留）均通过；Ctrl+C 经 pty 真信号验证正常。

## 待办与环境

- 待办（2026-09-20 提交后剩余）：
  1. ~~bo_en.py 同步~~ / ~~README 中英更新~~ / ~~.gitignore 加 .ai.db~~：**均已完成**；
  2. tools/regress.py 更新 2 项过时断言：「容错替换(行尾空白)」改认新文案「已按整行匹配」
     （替换本身仍成功）；「content 与替换互斥」改为断言「已修改 + 提示忽略 content」；
     （大文件两项断言已随 3MB 上限同步为「拒绝读取」）；
  3. Python 3.6 Docker 复测本批改动（sqlite3 是标准库，理论可用，未实测）。
- 提交流程不变：先只改 bo.py，提交前同步 bo_en.py / README；提交信息中文 `fix:`/`feat:` 前缀；
  提交前确认 `.bo`、`.ai.db`、`BO.log`、`__pycache__` 未被纳入。
- 开发机 Linux armv7l，Python 3.12.3（代码向下兼容 3.6）；本机 shell 无 3.6，日常用 py36check.py
  快检，真机用上面的 Docker。
- 运行参数记忆优先顺序：命令行 > 环境变量（含 BO_DB）> `.bo` > 内置默认。
- 历史上的 /tmp 测试脚本（test_trim/test_sigint/audit_bo/equiv/fake_llm）均不入库，可能已被清理，
  需要时按上文描述重建。
