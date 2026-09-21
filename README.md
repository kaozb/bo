[English](#bo--minimal-coding-agent)

# BO — 最小编码智能体

单文件、纯标准库，Python 3.6+ 即可运行，无需任何第三方依赖。后端使用 OpenAI 兼容接口（`/v1/chat/completions`），可对接 OpenAI、DeepSeek、通义、vLLM、Ollama、LM Studio 等任何兼容服务。

`bo.py` 为中文版，`bo_en.py` 为英文版。

## 特性

- 单文件、零依赖，Python 3.6+
- 四个工具：`read_file` / `write_file`（整体写入与精确替换双模式）/ `search` / `run_command`
- 对话历史自动瘦身：每轮只保留最近一次工具调用与结果，更早的自动移除
- Ctrl+C 第一次只中断当前一轮（生成中或命令执行中），再按一次退出程序；中断不打乱对话历史
- 分级输出：`-q` / `-v` / `-vv`
- 完整交互记录写入 SQLite 会话库（默认 `.ai.db`，0600，仅本人可读）；可用 `/s` 载入并继续历史会话
- `-y, --yes` 命令逐条确认
- 常用参数同时支持单字母与全名（`-m` / `--model`、`-b` / `--base-url`、`-k` / `--api-key` 等）
- 参数记忆：连接类参数加密写入用户主目录的 `.bo`，下次自动复用
- `-l, --list-models` 列出接口支持的模型，按编号选择后写入 `.bo` 并退出
- 读取与整体写入有 3 MB 上限，超过的文件请用 `run_command` 配合 `head` / `tail` / `sed`

## 环境要求

Python 3.6 或更高版本，无需第三方库。

## 快速开始

```bash
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_API_KEY=sk-xxxx
export MODEL=deepseek-chat

python3 bo.py        # 中文版
python3 bo_en.py     # 英文版
```

## 使用方式

启动后打印模型、接口与显示级别，进入 `你 >` 交互循环。用自然语言描述任务即可，模型会自行调用工具读写文件、执行命令。

```
你 > 看看 bo.py 用了哪些标准库，然后写进 README 的"特性"一节

  · search({"pattern": "^import ", "path": "bo.py"})
  · read_file({"path": "bo.py", "offset": 1, "limit": 60})
  · write_file({"path": "README.md", "old_string": "- 三个工具：...", "new_string": "- 四个工具：..."})
  ... 中间的工具调用与思考过程 ...

已更新 README.md，在"特性"一节补充了……
```

常用做法：

1. **命令把关**：`python3 bo.py -y`，不加则直接放行
2. **看细节**：`-v` 显示工具结果与思考，`-vv` 显示完整明细，`-q` 只看最终答复
3. **留档**：默认写入 `.ai.db`；用 `-d FILE` 换个会话库文件
4. **翻旧账**：启动后输入 `/s`，从最近 10 个会话里挑一个继续
5. **换模型/接口**：`-m` / `-b` / `-k`
6. **不知道有哪些模型**：`python3 bo.py -l`，从接口拉取模型清单按编号选一个，写入 `.bo` 后退出

```bash
python3 bo_en.py -y -v -d ~/sessions/my.db -m deepseek-chat
```

常用参数都同时支持单字母与全名两种写法，例如 `-b` 与 `--base-url`、`-s` 与 `--max-steps` 等价。

## 参数

| 参数 | 说明 |
| :--- | :--- |
| `-m, --model NAME` | 模型名；默认取 `MODEL` / `OPENAI_MODEL` |
| `-b, --base-url URL` | 接口地址；默认取 `OPENAI_BASE_URL` |
| `-k, --api-key KEY` | 密钥；默认取 `OPENAI_API_KEY` |
| `-y, --yes` | 命令执行前逐条人工确认（不加则直接放行） |
| `-s, --max-steps N` | 单轮最多工具调用轮数，默认 50 |
| `-t, --http-timeout N` | 单次请求超时秒数，默认 120 |
| `-q` / `-v` / `-vv` | 只显示最终答复 / 工具结果与思考 / 完整明细 |
| `-C, --no-color` | 关闭彩色输出（默认仅在终端下着色） |
| `-d, --db FILE` | 会话数据库文件（默认 `.ai.db`），完整交互记录写入此处；也可用环境变量 `BO_DB` 指定 |
| `-l, --list-models` | 列出接口支持的模型，按编号选择后写入 `.bo` 并退出 |

优先级：**命令行 > 环境变量 > `.bo` > 内置默认**。只有连接类参数（`-m`、`-b`、`-k`、`-s`、`-t`、`-d`）会加密写入用户主目录的 `.bo`（0600）并在下次复用；`-y`、`-q`/`-v`、`-C` 仅本次生效，不写入该文件。`.bo` 的密钥由所在目录路径派生，与目录绑定：换机器或换用户后无法解密，会被当作无效配置忽略；删除 `.bo` 即恢复默认。**文件可能含 API 密钥，请勿提交**。

## 工具

| 工具 | 说明 |
| :--- | :--- |
| `read_file` | 读取文件，返回带行号内容；`offset` 从 1 开始，读到中途会提示续读位置；传目录则列出目录内容（支持 `offset` / `limit` 翻项）。超过 3 MB 的文件拒绝读取（请用 `run_command` 的 `head` / `tail` / `sed`）；二进制文件拒绝读取 |
| `write_file` | 两种模式：给 `content` 新建或整文件覆盖，父目录自动创建（新建文件请用它，不要用 shell 重定向）；给 `old_string` + `new_string`（或 `edits`）在已有文件中精确替换，`replace_all` 替换全部。两种模式不能混用；失败时列出候选行，并可忽略行尾空白 / CRLF 差异；替换成功返回 diff。整体写入同样受 3 MB 上限约束 |
| `search` | 用正则搜索文件或目录，返回 `文件:行号:匹配行`，支持 `glob`、`max_results`、`context_lines` |
| `run_command` | 执行 shell 命令，返回退出码与输出；可指定 `cwd`；输出过长时保留首尾两端；超时按进程组击杀 |

## 交互命令

| 命令 | 说明 |
| :--- | :--- |
| `/reset` | 清空对话历史，开启新会话 |
| `/s` | 列出最近 10 个历史会话，选择其中一个载入并继续 |
| `/help` | 显示帮助 |
| `exit` | 退出（`quit` / `/exit` / `/quit` 亦可） |
| `Ctrl+C` | 第一次只中断当前一轮（生成中或命令执行中）并回到提示符，再按一次退出程序；命令被中断时会连同派生进程一起清理 |

## 约定文件

当前目录若存在 `AGENTS.md`（不区分大小写），会提示模型开工前自行读取，并按其中约定工作。

---

# BO — Minimal Coding Agent

A single-file, standard-library-only minimal coding agent. Any machine running Python 3.6+ can run it — no third-party library required. The backend uses an OpenAI-compatible endpoint (`/v1/chat/completions`), so it works with OpenAI, DeepSeek, Qwen, vLLM, Ollama, LM Studio and any other compatible service.

`bo.py` is the Chinese edition, `bo_en.py` the English one.

## Features

- Single file, zero dependencies, Python 3.6+
- Four tools: `read_file` / `write_file` (whole-file write and exact replacement in one) / `search` / `run_command`
- Automatic history slimming: each turn keeps only the most recent tool call and its result, earlier ones are dropped
- Ctrl+C interrupts the current turn on the first press (generation or running command) and quits on the second; an interrupt never corrupts the conversation history
- Tiered output: `-q` / `-v` / `-vv`
- The full interaction record is written to a SQLite session database (default `.ai.db`, 0600, owner-only); use `/s` to load and continue a past session
- `-y, --yes` to confirm each command
- Common options take both a short and a long form (`-m` / `--model`, `-b` / `--base-url`, `-k` / `--api-key`, ...)
- Option memory: connection options are encrypted into `.bo` in your home directory and reused later
- `-l, --list-models` lists the models the endpoint supports; pick one by number, it is written to `.bo` and the program exits
- Reads and whole-file writes are capped at 3 MB; for larger files use `run_command` with `head` / `tail` / `sed`

## Requirements

Python 3.6+, no third-party libraries.

## Quick Start

```bash
export OPENAI_BASE_URL=https://api.deepseek.com/v1
export OPENAI_API_KEY=sk-xxxx
export MODEL=deepseek-chat

python3 bo.py        # Chinese edition
python3 bo_en.py     # English edition
```

## Usage

On startup the agent prints the model, endpoint and display level, then enters a `you >` loop. Just describe the task in plain language — the model calls the tools itself to read and write files and run commands.

```
you > check which standard-library modules bo.py uses, then add them to the Features section of the README

  · search({"pattern": "^import ", "path": "bo.py"})
  · read_file({"path": "bo.py", "offset": 1, "limit": 60})
  · write_file({"path": "README.md", "old_string": "- Three tools: ...", "new_string": "- Four tools: ..."})
  ... tool calls and reasoning in between ...

Updated README.md, adding the module list to the Features section ...
```

Common practices:

1. **Gate command execution**: `python3 bo_en.py -y`; without it, commands run directly
2. **Control verbosity**: `-v` shows tool results and thinking, `-vv` full detail, `-q` final answer only
3. **Keep a record**: written to `.ai.db` by default; use `-d FILE` for a different database file
4. **Resume a session**: type `/s` and pick one of the last 10 sessions to continue
5. **Swap model/endpoint**: `-m` / `-b` / `-k`
6. **Not sure which models exist**: run `python3 bo_en.py -l`, pick one from the endpoint's model list by number, it is written to `.bo` and the program exits

```bash
python3 bo_en.py -y -v -d ~/sessions/my.db -m deepseek-chat
```

Every common option accepts both a short and a long form, e.g. `-b` is the same as `--base-url` and `-s` the same as `--max-steps`.

## Options

| Option | Description |
| :--- | :--- |
| `-m, --model NAME` | model name; defaults to `MODEL` / `OPENAI_MODEL` |
| `-b, --base-url URL` | endpoint URL; defaults to `OPENAI_BASE_URL` |
| `-k, --api-key KEY` | API key; defaults to `OPENAI_API_KEY` |
| `-y, --yes` | ask for confirmation before each command (without it, commands run directly) |
| `-s, --max-steps N` | max tool-call rounds per turn, default 50 |
| `-t, --http-timeout N` | per-request timeout in seconds, default 120 |
| `-q` / `-v` / `-vv` | final answer only / tool results and thinking / full detail |
| `-C, --no-color` | disable colored output (colors only on a terminal by default) |
| `-d, --db FILE` | session database file (default `.ai.db`) holding the full interaction record; the `BO_DB` environment variable works too |
| `-l, --list-models` | list the models the endpoint supports, pick one by number, write it to `.bo` and exit |

Precedence: **command line > environment variable > `.bo` > built-in default**. Only connection options (`-m`, `-b`, `-k`, `-s`, `-t`, `-d`) are encrypted into `.bo` in your home directory (0600) and reused next time; `-y`, `-q`/`-v`, `-C` apply to the current run only and are not written there. The `.bo` key is derived from the directory path and bound to it: on another machine or as another user it cannot be decrypted and is ignored as invalid; delete `.bo` to reset. **The file may contain your API key, so do not commit it**.

## Tools

| Tool | Description |
| :--- | :--- |
| `read_file` | read a file with line numbers; `offset` is 1-based and a continuation offset is reported; pass a directory to list it (with `offset` / `limit` paging). Files over 3 MB are refused (use `head` / `tail` / `sed` through `run_command`); binary files are refused |
| `write_file` | two modes: with `content` it creates or overwrites the whole file, creating parent directories (use this for new files, not shell redirection); with `old_string` + `new_string` (or `edits`) it makes an exact replacement in an existing file, `replace_all` replacing every occurrence. The modes cannot be mixed; failures list candidate lines and trailing-whitespace/CRLF differences may be ignored; a successful replacement returns a diff. Whole-file writes obey the same 3 MB cap |
| `search` | regex search across a file or directory, returning `file:line:match`, with `glob`, `max_results` and `context_lines` |
| `run_command` | run a shell command, return exit code and output; accepts a `cwd`; long output keeps head and tail; on timeout the whole process group is killed |

## Interactive Commands

| Command | Description |
| :--- | :--- |
| `/reset` | clear the conversation and start a new session |
| `/s` | list the last 10 sessions and load one to continue |
| `/help` | show help |
| `exit` | quit (`quit` / `/exit` / `/quit` also work) |
| `Ctrl+C` | the first press interrupts the current turn (generation or running command) and returns to the prompt, the second quits; an interrupted command is cleaned up together with its spawned processes |

## Convention File

If `AGENTS.md` (case-insensitive) exists in the current directory, the model is told to read it before starting work and to follow what it says.
