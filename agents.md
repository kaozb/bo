本文是指导你怎么干活的指导手册
## 文档维护规则

1. 本文1-80行禁止修改；例外：确认为事实错误时可单独提出修正。
2. 80行后的内容与实际不符时，更新本文以反映实际；改动行为/参数/工具个数后顺手核对对应小节。
3. 本文超过500行必须主动压缩至500行以内

## 核心原则

1. 先理解再动手：通读任务+关联代码 → 追踪端到端数据流 → 理解问题全貌
2. 最小必要实现，不写非必要代码

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
- 改工具层后用直接调用函数的方式验证（`import bo` + 调 `tool_*`），比走真实 LLM 快且确定；端到端才用真实接口。
- 喜欢先看实测数据（行数、耗时、diff、符号表）再下结论，不接受"应该没问题"。

# BO 项目约定（长期记忆）

## 项目概况

BO 是单文件、纯标准库的最小编码智能体，走 OpenAI 兼容接口（`/v1/chat/completions`），4 个工具：

- `read_file`   读文件（带行号、offset/limit 续读）/ 列目录
- `write_file`  双模式：`content` 新建或整体覆盖；`old_string`+`new_string`（或 `edits`）精确替换。
                **不拆成 write + edit 两个工具**；删除/移动交给 `run_command` 的 rm/mv，不加 delete_file/move_file
- `search`      正则搜索（整文件预筛 + 逐行匹配）
- `run_command` shell 执行（PIPE + 采集线程，超时按进程组击杀）

完整交互写入 **SQLite 会话库**（默认 `.ai.db`，`-d/--db` 或环境变量 `BO_DB` 指定；sqlite3 是标准库，
不破坏零依赖）。交互命令：`/reset` 开新会话、`/s` 载入历史会话、`/help`、`exit`。

文件构成：

- `bo.py`      中文版，**功能改动唯一来源（single source of truth）**，约 2250 行，含可执行位
- `bo_en.py`   英文版，与 bo.py **结构完全对应**（同符号表、同 AST 结构），仅注释与用户可见文案不同
- `README.md`  中文为准，开头有 `[English]` 锚点
- `tools/py36check.py` 3.6 兼容扫描；`tools/regress.py` 工具层回归 + 性能守卫
- `.bo`         运行时加密参数记忆（0600，含 API 密钥），**已 gitignore**
- `.ai.db`      会话库（含全部对话记录），**已 gitignore**（`git log --all` 从未提交）
- `agents.md`   本文件

## 工作流程约定（重要）

1. **接到修正/修复任务只改 `bo.py`**，不要顺手改 `bo_en.py` 或 `README.md`。
2. **只在提交前同步**：把 bo.py 的改动移植到 bo_en.py（保持结构与行序）；影响用法/参数/特性才更新
   README——注意 README「特性」写明了工具个数（4 个，write_file 双模式），增删工具必须一起改。
3. 提交信息用中文 `fix:`/`feat:` 前缀；提交前确认 `.bo`、`.ai.db`、`BO.log`、`__pycache__` 未被纳入。
4. 本仓库 git 身份通过 `git config --local` 设为 `mibo <aoamo95@gmail.com>`，勿用全局身份提交。

## 编码约束

- 兼容 **Python 3.6+**：不用 3.7+ 专有 API（`subprocess.run(capture_output=/text=)`、`stream.reconfigure`、
  `:=`、dataclasses、`f"{x=}"`）；UTF-8 按 `setup_stdio`（getattr 探测 reconfigure + TextIOWrapper 兜底）。
  改完跑 `python3 tools/py36check.py bo.py`。
- **零第三方依赖**，只用标准库；保持单文件不拆模块（`tools/` 是开发工具，非运行时）。
- 用户可见输出中文（bo.py）/ 英文（bo_en.py）两套，新增文案同时留位置。
- 修改代码小步精确（old_string/new_string），不要整文件重写。
- 优先 C 级原语而非 Python 逐元素循环：`str.find/replace/count/join`、`bytes.count`、`os.scandir`。
- 刻意简化处（如忽略全局锁、O(n²)、启发式上限）用 `ponytail:` 注释标注上限与升级路径（当前未使用）。

## 会话数据库（勿回退为文件日志）

- `db_open` 建两表：`sessions`（session_no/started_at/ended_at/status running|closed|crashed、model/
  base_url/title/prompt_tokens/completion_tokens）与 `events`（session_id, seq, ts, step, kind, role,
  content, tool_call_id…，按 (session_id, seq) 索引）。打开时把残留 `running` 会话标 `crashed`；
  库打不开直接 `sys.exit(1)`。`BO_VERSION`（`1.1.0-db`）写入 sessions。
- 事件 kinds：`session_begin/system/user/assistant/tool_call/tool_result/bad_tool_call/trim/reset/
  error/session_end`，全部经 `Output.log` 入库（`_LOG_MAP` 是 kind→入库参数表，新增 kind 必须登记，
  否则被静默丢弃）；`db_bump_tokens` 累计 usage 并把耗时写回最近一条 assistant 事件。
  **思考（reasoning）从不入库**，因此也不会被还原。
- title = 首条 user 消息首行（≤60 字符），仅在 title 为空时写。
- `/reset` → `_new_session`；`/s` → `choose_session` 列最近 10 条 → `db_load_session` 还原 →
  `load_history` 走同一 `_trim_history` 并用 `_close_tool_calls` 补齐悬空 tool 结果。

## 工具层关键设计（改动时不要回退）

- **search 整文件预筛**（`_search_prefilter`）：先对全文 `re.search`，不命中就跳过 `splitlines`+逐行匹配。
  pattern 含 `\A`/`\Z`/`(?-m` 时不用（整文件与逐行语义不同）；含 `$` 且正文含 `\r`（CRLF）时不用。
- **write_file 精确 replace_all 走 `str.replace`**；容错路径按跨度 join 拼接，不反复切片。
- **`_candidates` 未命中路径用片段子串扫描**（`_probe_fragments`+`str.find`），不对每行跑
  `difflib.SequenceMatcher`。
- **`_locate` 容错分支按行比对**（`_locate_fuzzy`），不逐字符建下标映射。
- **`_locate_tolerant` 终级兜底**：精确与 `_locate_fuzzy` 都未命中时，单行 old_string 与文件某行仅差
  「空白多少/tab/CRLF」→ 删全部空白后整行相等即定位（归一化后 <4 字符放弃，防误伤），提示
  「已按整行匹配（忽略了空白差异）」。
- **`_read_lines_window` 整读 + `_decode_bytes` 探测编码**（utf-8→gbk→latin-1）。没有流式扫描分支，
  因此不存在「小文件正常、大文件乱码」的双路径不一致。
- **`MAX_READ_BYTES = 3MB`**：read_file 读取与 write_file 整体写入的上限，超限一律拒绝并提示改用
  `run_command` 配合 head/tail/sed；`MAX_READ_MB` 供提示文案复用。
- **`_list_dir` 用 `os.scandir`**；**行切分统一走 `_split_lines`**（只按 `\n` 断行、去行尾 `\r`、
  忽略末尾空行），因此 `\x0b`/`\x0c`/`\x85`/`\u2028` 不当行分隔，行号与编辑器一致。

有意为之的行为（勿当 bug 改回）：

- `_resolve` 只做空 path 检查与 `realpath` 规范化，**无路径越界限制**（`-r/--root` 已整项移除：
  README/`-h`/`parse_args`/`CONFIG_KEYS` 都没有该参数，`.bo` 里遗留的 `root` 键被 `CONFIG_KEYS`
  过滤掉。`run_command` 本就是 shell 执行、无法用 cwd 约束，移除后文件与命令均无路径限制）。
- 行级容错定位不吞掉「被替换文本之后」的行尾空白与 `\r`。
- 整体写入 GBK 文件时新内容无法用原编码表示则回退 utf-8 **并明说**（编辑模式仍报错、不写盘）。
- `write_file` 只给 `replace_all`（缺 old_string）会点名提示该参数被忽略。
- `write_file` 同时给 content 与 new_string/edits：按**局部替换**执行并忽略 content（成功时附提示）；
  只给 content+old_string（无 new_string/edits）仍报错。

已删除、勿重新引入：`_looks_binary`、`_norm_text`、`MAX_SEARCH_LINES`、`import codecs`、
`import itertools`、`-l/--log` 与 `-L/--no-log` 及 `log_path`（由 `-d/--db` 与 db_path 取代）、
`TRIM_KEEP_TURNS`（由 TRIM_KEEP_CALLS 取代）、`-r/--root`、BO.log 文件日志。

## 历史瘦身（只动 bo.py）

- `TRIM_KEEP_CALLS = 1` + `_trim_history(messages, keep_calls)`：批次 = 一条带 tool_calls 的 assistant
  消息 + 其后对应的 role=tool 结果；只保留最近 keep_calls 个批次，更早的 tool 结果被移除、assistant
  摘掉 tool_calls 字段（去掉后 content 为空则整条删）。system 与全部 user/assistant 正文保留。
- 调用点两处：`run_turn` 开头（append 当前 user 消息前）与 `load_history`（/s 载入后走同一裁剪，
  保证重载后形状与实时对话一致）。
- **`db_load_session` 按 `step` 合并 tool_call**：同一 step 的多条 tool_call 属于同一条 assistant
  消息（模型一次并行调用），还原时并入一条带多个 tool_calls 的 assistant；否则每个 tool_call 各占一条，
  会被 `_trim_history` 当成多个批次、同一步的调用被裁掉一部分。纯正文、无工具调用的 assistant 会去掉
  空 `tool_calls` 列表，避免发出 `tool_calls: []` 被判非法。
- 丢掉纯工具往返后可能连续两条 user，`_trim_history` 会合并（`\n\n`）维持角色交替。
- 裁剪静默，仅记一条 `trim` 事件。

## 坏 tool_call 防护（勿回退为「先落盘后校验」）

模型偶尔吐出截断或跑偏的 `arguments`（如只有 `{`，或把提示里的 `<tool_call>` XML 样例当输出）。
**关键约束：先校验参数，再落盘/入历史。** 顺序反了会把非法 tool_call 写进库和历史，之后每次请求都被
接口判 `HTTP 400 input_invalid`，用户除了 `/reset` 别无他法。

- `_tool_args_error(raw)`：返回 None 表示可用；参数为空 / 非合法 JSON / 非 JSON 对象都算不可用。
- `_parse_tool_calls(tool_calls)`：分成 (可用: [(调用, 已解析参数)], 不可用: [调用])。**缺函数名也算不可用**
  （同样还原不出合法结构）。
- `run_turn`：只对可用的调用 `out.log("TOOL_CALL …")` 并 append；坏调用记一条 `bad_tool_call` 事件
  （保留原始非法参数便于排查）后丢弃，提示用户但不中断对话。若整批都不可用，本轮直接 return。
- `_drop_tool_calls_by_id(messages, ids)`：兜底，按 id 精确摘除坏调用及其 `role=tool` 结果。
  **必须就地修改（`m["tool_calls"] = keep`），不能替换为 `dict(m)` 副本**——调用方可能仍持有原消息引用。
- `db_load_session` 载入时清理旧库中已存在的坏记录，老会话无需重建即可续用。
- `Output.tool_call` 对 `name`/`raw_args` 做 `or "?"` / `or "{}"` 兜底（坏调用正是 name 可能为 None 的情形）。

## Ctrl+C 语义（只动 bo.py）

- `TurnInterrupted(BaseException)` + `_INTERRUPT = {"busy","seen"}` + `_handle_sigint` /
  `install_sigint_handler`：**一轮内第一次 Ctrl+C 只中断本轮，第二次（或空闲时一次）退出程序**。
  必须继承 BaseException，否则被 `call_llm` 里的 `except Exception` 吞掉。
- `run_turn` 开头置 `busy=True, seen=False`，main 的 `finally` 复位，计数每轮独立。
- 中断落点三处：`call_llm` 调用处（assistant 消息未入历史，直接 return）；工具循环 `execute_tool` 处
  （复用 aborted/abort_note/abort_info，给未执行 tool_calls 补结果）；main 兜底 `except TurnInterrupted`
  （用 `_close_dangling_tool_calls` 补 tool 结果，免得下一轮被判格式错误）。
- 补悬空结果有两个函数，**行为不同勿合并**：`_close_dangling_tool_calls` 只补最近一批、追加到末尾
  （中断兜底用）；`_close_tool_calls` 给每批就地补、插在对应 assistant 之后（/s 载入历史用）。
- `tool_run_command` 的 `proc.wait` 捕 `(TurnInterrupted, KeyboardInterrupt)` 后 `_terminate_group(proc)`
  再 raise，中断长命令不留派生进程。`_atomic_write` 清理用 `except BaseException`（Ctrl+C 是
  BaseException，否则残留 `.bo-*.tmp`）。`_confirm` 只捕 EOFError，KeyboardInterrupt 交给 `_handle_sigint`。

## 双语同步与等价性

- bo_en.py 由 bo.py 逐行生成（按行号做「中文行→英文行」映射），保持结构与行序不变。
- 英文因语序需要补跨行拼接缺失的空格；成功文案前缀统一 `OK:`（对应中文 `已`，供
  `tool_write_file` 判断是否追加提示）。
- 符号表 diff（应只剩 `LEVEL_NAMES` 一行差异）：

```bash
cd /root/bo
grep -n "^def \|^class \|^    def \|^[A-Z_][A-Z_0-9]* = " bo.py | sed 's/ *#.*//' | cut -d: -f2- > /tmp/a.sym
grep -n "^def \|^class \|^    def \|^[A-Z_][A-Z_0-9]* = " bo_en.py | sed 's/ *#.*//' | cut -d: -f2- > /tmp/b.sym
diff /tmp/a.sym /tmp/b.sym
```

更强的检查是 AST 结构比对（节点类型序列应完全一致，只允许字符串/注释不同）：

```bash
python3 -c "
import ast
t=lambda p:[type(n).__name__ for n in ast.walk(ast.parse(open(p,encoding='utf-8').read()))]
a,b=t('bo.py'),t('bo_en.py'); print(len(a),len(b),a==b)"
```

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

**当前状态（2026-09-20）**：py_compile ✅、py36check ✅（无 SyntaxWarning）。`regress.py` 有 **2 项失败
属断言过时**，其余全过——两处失败的实际行为都是正确的（见待办）。3MB 上限、去 `--root`、`/s` 形状修复、
Ctrl+C（含进程组击杀、无孤儿进程）均已实测通过。

## 待办与环境

- **tools/regress.py 有 2 项过时断言待更新**（替换本身都成功，只是文案变了）：
  - 第 116 行 `"已忽略行尾空白"` → 应改认 `"已按整行匹配"`；
  - 第 133 行 `"不能同时使用"` → 应改为断言「已修改 + 提示忽略 content」。
- Python 3.6 真机（Docker）复测本批改动：sqlite3 是标准库、3.6 理论可用，但**尚未实测**。
- 开发机 Linux armv7l，Python 3.12.3（代码向下兼容 3.6）；日常用 py36check.py 快检。
- 运行参数记忆优先级：命令行 > 环境变量（含 `BO_DB`）> `.bo` > 内置默认。只有连接类参数
  （`-m`/`-b`/`-k`/`-s`/`-t`/`-d`）写入 `.bo`；`-y`/`-q`/`-v`/`-C` 仅本次生效。
- `.bo` 密钥由所在目录路径派生，与目录绑定：换机器/换用户无法解密，被当作无效配置忽略。

### 3.6 Docker 复测命令（需要时用）

```bash
docker run --rm --network host -v /root/bo:/root/bo -w /root/bo python:3.6-slim \
  sh -c 'printf "退出\n" | python bo.py -C -b <url> -k <key> -m <model> -v'
```

注意：容器里跑会**改写 `.bo`**，先 `cp .bo /tmp/bo.conf.bak`，验证后还原。没有真实 key 时可用标准库
`http.server` + SSE 起假接口走通全流程（脚本不入库）。
