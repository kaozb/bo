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

BO 是单文件、纯标准库的最小编码智能体，走 OpenAI 兼容接口（`/v1/chat/completions`），5 个工具：

- `read_file`   读文件（带行号、start_line/limit 续读）/ 列目录
- `write_file`  整体写入：`path` + `content`（新建或整体覆盖）。父目录自动创建，空 content 拒绝。
- `edit_file`   局部修改：`path` + `old_string`/`new_string`（或 `edits` 批量）。三级降级定位。
- `search`      正则搜索（整文件预筛 + 逐行匹配）
- `run_command` shell 执行（PIPE + 采集线程，超时按进程组击杀）

`write_file` 与 `edit_file` **是两个独立工具**（曾合一，已拆分）。删除/移动交给 `run_command` 的
rm/mv，不加 delete_file/move_file。

完整交互写入 **SQLite 会话库**（默认 `.ai.db`，`-d/--db` 或环境变量 `BO_DB` 指定；sqlite3 是标准库，
不破坏零依赖）。交互命令：`/reset` 清空并延迟开新会话、`/s` 载入历史会话、`/help`、`exit`。

启动参数：`-m/-b/-k/-s/-t/-T/-d` 为连接类参数（写入 `.bo`），`-y/-q/-v/-C` 仅本次生效；
`-l/--list-models` 拉取 `/models` 让用户按编号选模型，写入 `.bo` 后直接退出。
**`-t` 是 `--http-timeout`；工具历史轮数是 `-T/--tool`（大写 T）。**

文件构成：

- `bo.py`      中文版，**功能改动唯一来源（single source of truth）**，约 2250 行，含可执行位
- `bo_en.py`   英文版，与 bo.py **结构完全对应**（同符号表、同 AST 结构），仅注释与用户可见文案不同
- `README.md`  中文为准，开头有 `[English]` 锚点
- `tools/py36check.py` 3.6 兼容扫描；`tools/regress.py` 工具层回归 + 性能守卫
- `.bo`         运行时加密参数记忆（0600，含 API 密钥），**已 gitignore**
- `.ai.db`      会话库（含全部对话记录），**已 gitignore**
- `agents.md`   本文件

## 工作流程约定（重要）

1. **接到修正/修复任务只改 `bo.py`**，不要顺手改 `bo_en.py` 或 `README.md`。
2. **只在提交前同步**：把 bo.py 的改动移植到 bo_en.py（保持结构与行序）；影响用法/参数/特性才更新
   README——README「特性」写明了工具个数（5 个），增删工具必须一起改。
3. 提交信息用中文 `fix:`/`feat:` 前缀；提交前确认 `.bo`、`.ai.db`、`__pycache__` 未被纳入。
4. 本仓库 git 身份通过 `git config --local` 设为 `mibo <aoamo95@gmail.com>`，勿用全局身份提交。

## 编码约束

- 兼容 **Python 3.6+**：不用 3.7+ 专有 API（`subprocess.run(capture_output=/text=)`、`stream.reconfigure`、
  `:=`、dataclasses、`f"{x=}"`）；UTF-8 按 `setup_stdio`（getattr 探测 reconfigure + TextIOWrapper 兜底）。
  改完跑 `python3 tools/py36check.py bo.py`——会报 `setup_stdio` 的 `.reconfigure` 一处，那是 `hasattr`
  守卫下的 3.7+ 分支，**属工具误报，可忽略**。
- **零第三方依赖**，只用标准库；保持单文件不拆模块（`tools/` 是开发工具，非运行时）。
- 用户可见输出中文（bo.py）/ 英文（bo_en.py）两套，新增文案同时留位置。
- 修改代码小步精确（old_string/new_string），不要整文件重写。
- 优先 C 级原语而非 Python 逐元素循环：`str.find/replace/count/join`、`bytes.count`、`os.scandir`。
- 刻意简化处（如忽略全局锁、O(n²)、启发式上限）用 `ponytail:` 注释标注上限与升级路径（当前未使用）。
- **不写版本兼容代码**：明确不兼容旧 `.ai.db` / 旧 `.bo` 结构，也不需要为旧数据保留降级分支。

## 会话数据库

- `db_open` 建两表：`sessions`（id/started_at/ended_at/status running|closed|crashed、model/
  base_url/title/prompt_tokens/completion_tokens）与 `events`（session_id, seq, ts, step, kind, role,
  content, tool_call_id…，按 (session_id, seq) 索引）。打开时把残留 `running` 会话标 `crashed`；
  库打不开直接 `sys.exit(1)`。`BO_VERSION`（`1.1.0-db`）写入 sessions。
- **会话延迟创建**：启动 / `/reset` 只 `_new_session` 关旧会话并把 `session_id` 置空，**不写库**；
  用户首次真实回话时 `run_turn` → `_ensure_session` 才 `db_new_session` 并补写
  `session_begin`/`system`。因此只启动看看就 exit 不会留空会话，`/s` 列表天然都是有内容的会话
  （`db_list_sessions` 无需按 title 过滤）。`finally` 里 `session_id is None` 则完全不写事件；
  `db_add_event` 对 `session_id is None` 直接 return（防 NULL 侧写）。
- 事件 kinds：`session_begin/system/user/assistant/tool_call/tool_result/bad_tool_call/trim/reset/
  error/session_end`，全部经 `Output.log` 入库（`_LOG_MAP` 是 kind→入库参数表，新增 kind 必须登记，
  否则被静默丢弃）；`db_bump_tokens` 累计 usage 并把耗时写回最近一条 assistant 事件。
  **思考（reasoning）从不入库**，因此也不会被还原。
- title = 首条 user 消息首行（≤60 字符），仅在 title 为空时写。
- `/reset` → `_new_session`（延迟建库）；`/s` → `choose_session` 列最近 10 条（**只显示标题**）→
  `db_load_session` 还原 → `load_history` 走同一 `_trim_history` 并用 `_close_tool_calls` 补齐悬空结果。

## 工具层关键设计（改动时不要回退）

- **search 整文件预筛**（`_search_prefilter`）：先对全文 `re.search`，不命中就跳过 `splitlines`+逐行匹配。
  pattern 含 `\A`/`\Z`/`(?-m` 时不用（整文件与逐行语义不同）；含 `$` 且正文含 `\r`（CRLF）时不用。
- **`write_file` 整体覆盖与 `edit_file` 局部替换是两个工具**，各自只接受对应参数并互相点名提示
  （给错参数时提示改用另一个工具）。edit_file 的 `replace_all` 精确路径走 `str.replace`；
  容错路径按跨度 join 拼接，不反复切片。
- **`_candidates` 未命中路径用片段子串扫描**（`_probe_fragments`+`str.find`），不对每行跑
  `difflib.SequenceMatcher`。
- **`_locate` 三级降级**：精确 → 忽略行尾空白/CRLF（`_locate_fuzzy`，按行比对）→ 整行归一化
  （`_locate_tolerant`，单行 old 与文件某行仅差空白/tab/CRLF，删空白后整行相等即定位；归一化后
  <4 字符放弃防误伤）。
- **`_read_lines_window` 整读 + `_decode_bytes` 探测编码**（utf-8→gbk→latin-1）。没有流式扫描分支，
  因此不存在「小文件正常、大文件乱码」的双路径不一致。
- `read_file` 读文件时 **`limit` 必填且严格校验**（不经 `_int` 静默回落）：缺失、非整数、<1、>MAX_READ_LINES(100000)
  都直接返回错误字符串提示模型重试；目录列举仍默认 200、仍走 `_int`。超长/大文件靠 start_line 续读。**`MAX_READ_BYTES = 3MB`**：read_file 读取与 write_file 整体写入的上限，超限一律拒绝并提示改用
  `run_command` 配合 head/tail/sed；`MAX_READ_MB` 供提示文案复用。
- **`_list_dir` 用 `os.scandir`**；**行切分统一走 `_split_lines`**（只按 `\n` 断行、去行尾 `\r`、
  忽略末尾空行），因此 `\x0b`/`\x0c`/`\x85`/`\u2028` 不当行分隔，行号与编辑器一致。
- **超长行截断带原长**：`read_file` 与 `search` 的 `_truncate` note 为 `"... [本行已截断，原 %d 字符]"`；
  `_truncate` 用 `str.replace("%d", ...)` 而不是 `%` 格式化，note 里出现其它 `%` 也不会报错。
- **`read_file` 返回带剩余行数**：头部写「显示第 A-B 行，剩余 R 行」，footer 对应写「剩余 R 行」/「还有 R 行未显示（剩余 R 行）」；R = 总行数 - 已显示末行。目录列举不含剩余行数。

有意为之的行为（勿当 bug 改回）：

- **无路径越界限制**：`-r/--root` 已整项移除（可执行文件本就 shell 执行、无法用 cwd 约束）；
  `.bo` 里遗留的 `root` 键被 `CONFIG_KEYS` 过滤掉。
- 行级容错定位不吞掉「被替换文本之后」的行尾空白与 `\r`。
- 整体写入 GBK 文件时新内容无法用原编码表示则回退 utf-8 **并明说**（编辑模式仍报错、不写盘）。

## 历史瘦身

- `TRIM_KEEP_ROUNDS = 2`（`-T/--tool` 可配并写入 `.bo`）+ `_trim_history(messages, keep_rounds)`：
  **以 user 消息为分割线分轮**，一轮 = 一条 user 及其后到下一个 user 前的全部 assistant/tool 消息；
  只保留最近 keep_rounds 轮的**完整工具往返**，更早轮次的 tool 结果被移除、assistant 摘掉 tool_calls
  字段（去掉后 content 为空则整条删）。system 与全部 user/assistant 正文保留。
  **一轮内的多步调用属于同一轮，绝不因步数被裁**——裁剪发生在 `run_turn` 开头（append 当前 user 前），
  不在 step 循环里。
- 调用点两处：`run_turn` 开头与 `load_history`（/s 载入后走同一裁剪，保证重载后形状与实时对话一致）。
- **`db_load_session` 按 `step` 合并 tool_call**：同一 step 的多条 tool_call 属于同一条 assistant
  消息（模型一次并行调用），还原时并入一条带多个 tool_calls 的 assistant；纯正文、无工具调用的
  assistant 会去掉空 `tool_calls` 列表，避免发出 `tool_calls: []` 被判非法。
- 丢掉纯工具往返后可能连续两条 user，`_trim_history` 会合并（`\n\n`）维持角色交替。
- 裁剪静默，仅记一条 `trim` 事件。

## 重复调用护栏

- 只提醒不中止：同一轮内**等价**的调用连续出现 `REPEAT_WARN_CALLS = 3` 次起，每次都**照常执行**，
  只在结果后追加一段 `[重复调用护栏]` 说明（提示该调用本轮已执行过、别再重复、换参数不算重复、
  请改用已有结果或换思路）回喂给模型，让它自己改主意；**不再有任何硬中止阈值**，重复再多也不掐断会话。
  说明**拼在结果之后**，避免掩盖真实结果开头（`_is_error` 只看开头）。
- `_repeat_key(name, args)` 做归一化，**不比对 raw_args 字符串**（否则改个 start_line/timeout 就绕过）：
  文件类工具取「工具名 + realpath 路径」；`search` 取「pattern+path+glob」；`run_command` 取命令主体
  （忽略 cwd/timeout）；其它回落参数 JSON 稳定序列化。参数非 dict 也不抛异常。

## 坏 tool_call 防护（勿回退为「先落盘后校验」）

模型偶尔吐出截断或跑偏的 `arguments`（如只有 `{`，或把提示里的 `<tool_call>` XML 样例当输出）。
**关键约束：先校验参数，再落盘/入历史。** 顺序反了会把非法 tool_call 写进库和历史，之后每次请求都被
接口判 `HTTP 400 input_invalid`，用户除了 `/reset` 别无他法。

- `_tool_args_error(raw)`：返回 None 表示可用；参数为空 / 非合法 JSON / 非 JSON 对象都算不可用。
- `_parse_tool_calls(tool_calls)`：分成 (可用: [(调用, 已解析参数)], 不可用: [调用])。**缺函数名也算不可用**。
- `run_turn`：只对可用的调用 `out.log("TOOL_CALL …")` 并 append；坏调用记一条 `bad_tool_call` 事件
  （保留原始非法参数便于排查）后丢弃，提示用户但不中断对话。若整批都不可用，本轮直接 return。
- `db_load_session` 直接按 `tool_call_id` 还原（落盘侧已保证非空），空 id 的调用/结果直接丢弃。

## Ctrl+C 语义

- `TurnInterrupted(BaseException)` + `_INTERRUPT = {"busy","seen"}` + `_handle_sigint` /
  `install_sigint_handler`：**一轮内第一次 Ctrl+C 只中断本轮，第二次（或空闲时一次）退出程序**。
  必须继承 BaseException，否则被 `call_llm` 里的 `except Exception` 吞掉。
- `run_turn` 开头置 `busy=True, seen=False`，main 的 `finally` 复位，计数每轮独立。
- 中断落点三处：`call_llm` 调用处（assistant 消息未入历史，直接 return）；工具执行处（置 aborted，
  给未执行 tool_calls 补结果）；main 兜底 `except TurnInterrupted`（用 `_close_tool_calls` 补 tool 结果，
  免得下一轮被判格式错误）。
- `tool_run_command` 的 `proc.wait` 捕 `(TurnInterrupted, KeyboardInterrupt)` 后 `_terminate_group(proc)`
  再 raise，中断长命令不留派生进程。`_atomic_write` 清理用 `except BaseException`（Ctrl+C 是
  BaseException，否则残留 `.bo-*.tmp`）。`_confirm` 只捕 EOFError，KeyboardInterrupt 交给 `_handle_sigint`。

## 双语同步与等价性

- bo_en.py 由 bo.py 逐行生成（按行号做「中文行→英文行」映射），保持结构与行序不变。
- 英文因语序需要补跨行拼接缺失的空格；成功文案前缀统一 `OK:`（对应中文 `已`），只求双语对照一致，代码里不再据此做分支判断。
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

## 模型清单选择（`-l/--list-models`）

- `fetch_models(opts)` 请求 `base_url + /models`（GET，有 key 才带 `Authorization`），读响应受
  `MAX_RESPONSE_BYTES` 限制；`HTTPError`/`URLError`/非 JSON/缺 `data`/空清单都转 `RuntimeError`（中文文案）。
- `choose_model(opts)` 打印 `[n] id` 让用户输编号；回车/非数字/越界都只提示并返回 None。
- `parse_args` 里的 `-l` 分支在**写完本次显式连接参数之后**执行，用 `dict(cfg)+updates+model` 落盘，
  因此 `.bo` 里其它键不会被抹掉；未选择则原样退出，不写盘。

## 常用验证命令

```bash
cd /root/bo
python3 -m py_compile bo.py bo_en.py                    # 语法检查
python3 tools/py36check.py bo.py bo_en.py tools/*.py    # 3.6 兼容扫描（1 处已知误报）
python3 tools/regress.py                                # 工具层回归 + 性能守卫
python3 bo.py -h                                        # 帮助/参数自检
diff bo.py bo_en.py                                     # 确认仅文案差异
git status --short                                      # 提交前检查
```

**当前状态（2026-09-21）**：`py_compile` 通过；`tools/regress.py` **全部通过**（exit 0，无 SyntaxWarning）。
regress 已按 `write_file`/`edit_file` 拆分更新：整体写入用例归 `test_write_file`，局部替换用例归
`test_edit_file`，并补了两者互相点名提示的用例。文件层行为已实测正确（含 3MB 上限、延迟建会话、
`/s` 只列标题、历史按轮裁剪、重复护栏归一化）。

## 待办与环境

- Python 3.6 真机（Docker）复测尚未做；sqlite3 是标准库，理论可用。
- 开发机 Linux armv7l，Python 3.12.3（代码向下兼容 3.6）；日常用 py36check.py 快检。
- **测 `.bo` 相关功能务必先 `export HOME=<临时目录>`**：`CONFIG_FILE` 是 `~/.bo`，直接跑会覆盖
  用户真实配置（曾用假接口 `-b 127.0.0.1` 覆盖过一次）。恢复时用 `bo.save_config(dict, path)`
  而非手拼字节（落盘键名大小写敏感）。
- 参数记忆优先级：命令行 > 环境变量（含 `BO_DB`）> `.bo` > 内置默认。只有连接类参数
  （`-m`/`-b`/`-k`/`-s`/`-t`/`-T`/`-d`）写入 `.bo`；`-y`/`-q`/`-v`/`-C` 仅本次生效。
- `.bo` 密钥由所在目录路径派生，与目录绑定：换机器/换用户无法解密，被当作无效配置忽略。

### 3.6 Docker 复测命令（需要时用）

```bash
docker run --rm --network host -v /root/bo:/root/bo -w /root/bo python:3.6-slim \
  sh -c 'printf "退出\n" | python bo.py -C -b <url> -k <key> -m <model> -v'
```

注意：容器里跑会**改写 `.bo`**，先 `cp .bo /tmp/bo.conf.bak`，验证后还原。没有真实 key 时可用标准库
`http.server` + SSE 起假接口走通全流程（脚本不入库）。

