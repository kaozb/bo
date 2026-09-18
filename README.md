[English](#bo--minimal-coding-agent)

# BO — 最小编码智能体

单文件、纯标准库；只要设备能跑 Python 3.6+，本脚本就能运行，无需安装任何第三方库。
后端使用 OpenAI 兼容接口（`/v1/chat/completions`），可对接 OpenAI、DeepSeek、通义、vLLM、Ollama、LM Studio 等任何兼容服务。

`bo.py` 为中文版，`bo_en.py` 为英文版。

## 特性

- 单文件、零依赖，Python 3.6+
- 三个工具：`read_file` / `edit_file` / `run_command`
- 分级输出：`-q` / `-v` / `-vv`
- 完整交互日志落盘（0600 权限，仅本人可读）
- 可选 `--root` 限制文件访问范围，`--yes` 命令逐条确认

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

启动后会打印当前模型、接口地址与显示级别，随后进入交互循环，提示符为 `you >`。直接用自然语言描述任务即可，模型会自行调用工具读写文件、执行命令。

```
you > 看看 bo.py 用了哪些标准库，然后写进 README 的"特性"一节

  [read_file] bo.py
  [edit_file] README.md
  ... 中间的工具调用与思考过程 ...

已更新 README.md，在"特性"一节补充了……
```

常用做法：

1. **限定工作范围**（推荐）：`python3 bo.py --root ~/project`，模型只能读写该目录内的文件。
2. **执行命令要人工把关**：`python3 bo.py --yes`，每条 `run_command` 都需你确认后才执行；不加则直接放行。
3. **看细节**：加 `-v` 显示工具结果与思考，`-vv` 显示完整明细；用 `-q` 则只看最终答复。
4. **留档**：加 `-l` 把完整交互写入 `BO.log`（默认文件名，可跟自定义路径），文件权限为 0600。
5. **换模型/接口**：用 `--model` / `--base-url` / `--api-key` 覆盖环境变量，默认取 `MODEL`（或 `OPENAI_MODEL`）、`OPENAI_BASE_URL`、`OPENAI_API_KEY`。

一个带确认和日志的完整例子：

```bash
python3 bo_en.py --root ~/project --yes -v -l session.log --model deepseek-chat
```

## 参数

| 参数 | 说明 |
| :--- | :--- |
| `--model NAME` | 模型名，默认取环境变量 `MODEL` / `OPENAI_MODEL` |
| `--base-url URL` | 接口地址，默认取 `OPENAI_BASE_URL` |
| `--api-key KEY` | 密钥，默认取 `OPENAI_API_KEY` |
| `--yes` | 命令执行前逐条人工确认（不加则默认直接放行） |
| `--root DIR` | 限制 `read_file` / `edit_file` 只能访问 `DIR` 之内 |
| `--max-steps N` | 单轮最多工具调用轮数，默认 50 |
| `--http-timeout N` | 单次请求超时秒数，默认 120 |
| `-q` | 只显示最终答复，隐藏全部工具与思考过程 |
| `-v` / `-vv` | 显示工具结果与思考 / 完整明细 |
| `--no-color` | 关闭彩色输出 |
| `-l, --log [FILE]` | 完整交互写入日志，默认 `BO.log` |

## 工具

| 工具 | 说明 |
| :--- | :--- |
| `read_file` | 读取文件文本，返回带行号内容，可用 `offset` / `limit` 读片段 |
| `edit_file` | `old_string` 唯一命中后精确替换 |
| `run_command` | 执行 shell 命令，返回退出码与输出 |

## 交互命令

| 命令 | 说明 |
| :--- | :--- |
| `/reset` | 清空对话历史 |
| `/help` | 显示帮助 |
| `exit` | 退出（`quit` / `/exit` / `/quit` 亦可） |

## 约定文件

当前目录若存在 `AGENTS.md`（不区分大小写），会提示模型开工前自行读取，并按其中约定工作。

---

# BO — Minimal Coding Agent

A single-file, standard-library-only minimal coding agent. If the machine runs Python 3.6+, the script runs — no third-party library required.
The backend uses an OpenAI-compatible endpoint (`/v1/chat/completions`), so it works with OpenAI, DeepSeek, Qwen, vLLM, Ollama, LM Studio and any other compatible service.

`bo.py` is the Chinese edition, `bo_en.py` the English one.

## Features

- Single file, zero dependencies, Python 3.6+
- Three tools: `read_file` / `edit_file` / `run_command`
- Tiered output: `-q` / `-v` / `-vv`
- Full interaction log on disk (mode 0600, owner-only)
- Optional `--root` to restrict file access, `--yes` to confirm each command

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

On startup the agent prints the current model, endpoint and display level, then enters an interactive loop with the `you >` prompt. Just describe the task in plain language — the model calls the tools itself to read and write files and run commands.

```
you > check which standard-library modules bo.py uses, then add them to the Features section of the README

  [read_file] bo.py
  [edit_file] README.md
  ... tool calls and reasoning in between ...

Updated README.md, adding the module list to the Features section ...
```

Common practices:

1. **Scope the work** (recommended): `python3 bo_en.py --root ~/project` — the model can only touch files under that directory.
2. **Gate command execution**: `python3 bo_en.py --yes` asks for your confirmation before every `run_command`; without it, commands run directly.
3. **Control verbosity**: `-v` shows tool results and thinking, `-vv` shows full detail, `-q` shows only the final answer.
4. **Keep a record**: `-l` writes the full interaction to `BO.log` (default name, a custom path may follow), mode 0600.
5. **Swap model/endpoint**: `--model` / `--base-url` / `--api-key` override the environment variables, which default to `MODEL` (or `OPENAI_MODEL`), `OPENAI_BASE_URL`, `OPENAI_API_KEY`.

A complete example with confirmation and logging:

```bash
python3 bo_en.py --root ~/project --yes -v -l session.log --model deepseek-chat
```

## Options

| Option | Description |
| :--- | :--- |
| `--model NAME` | model name, defaults to the `MODEL` / `OPENAI_MODEL` environment variable |
| `--base-url URL` | endpoint URL, defaults to `OPENAI_BASE_URL` |
| `--api-key KEY` | API key, defaults to `OPENAI_API_KEY` |
| `--yes` | ask for manual confirmation before each command (without it, commands run directly) |
| `--root DIR` | restrict `read_file` / `edit_file` to `DIR` |
| `--max-steps N` | max tool-call rounds per turn, default 50 |
| `--http-timeout N` | per-request timeout in seconds, default 120 |
| `-q` | show only the final answer, hide all tool/thinking output |
| `-v` / `-vv` | show tool results and thinking / full detail |
| `--no-color` | disable colored output |
| `-l, --log [FILE]` | write the full interaction to a log, default `BO.log` |

## Tools

| Tool | Description |
| :--- | :--- |
| `read_file` | read a file with line numbers, use `offset` / `limit` for a fragment |
| `edit_file` | exact replacement once `old_string` matches uniquely |
| `run_command` | run a shell command, return exit code and output |

## Interactive Commands

| Command | Description |
| :--- | :--- |
| `/reset` | clear the conversation |
| `/help` | show help |
| `exit` | quit (`quit` / `/exit` / `/quit` also work) |

## Convention File

If `AGENTS.md` (case-insensitive) exists in the current directory, the model is told to read it before starting work and to follow what it says.
