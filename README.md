[English](#bo--minimal-coding-agent)

# BO — 最小编码智能体

单文件、纯标准库，Python 3.6+ 即可运行，无需任何第三方依赖。后端使用 OpenAI 兼容接口（`/v1/chat/completions`），可对接 OpenAI、DeepSeek、通义、vLLM、Ollama、LM Studio 等任何兼容服务。

`bo.py` 为中文版，`bo_en.py` 为英文版。

## 特性

- 单文件、零依赖，Python 3.6+
- 三个工具：`read_file` / `edit_file` / `run_command`
- 分级输出：`-q` / `-v` / `-vv`
- 完整交互日志落盘（0600，仅本人可读）
- `--root` 限制文件访问范围，`--yes` 命令逐条确认
- 参数记忆：连接类参数加密写入当前目录 `.bo`，下次自动复用

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

  · read_file({"path": "bo.py"})
  · edit_file({"path": "README.md", ...})
  ... 中间的工具调用与思考过程 ...

已更新 README.md，在"特性"一节补充了……
```

常用做法：

1. **限定范围**（推荐）：`python3 bo.py --root ~/project`
2. **命令把关**：`python3 bo.py --yes`，不加则直接放行
3. **看细节**：`-v` 显示工具结果与思考，`-vv` 显示完整明细，`-q` 只看最终答复
4. **留档**：`-l [FILE]` 写入日志（默认 `BO.log`，0600）
5. **换模型/接口**：`-m` / `--base-url` / `--api-key`

```bash
python3 bo_en.py --root ~/project --yes -v -l session.log -m deepseek-chat
```

## 参数

| 参数 | 说明 |
| :--- | :--- |
| `-m, --model NAME` | 模型名；默认取 `MODEL` / `OPENAI_MODEL` |
| `--base-url URL` | 接口地址；默认取 `OPENAI_BASE_URL` |
| `--api-key KEY` | 密钥；默认取 `OPENAI_API_KEY` |
| `--yes` | 命令执行前逐条人工确认（不加则直接放行） |
| `--root DIR` | 限制 `read_file` / `edit_file` 只能访问 `DIR` 之内 |
| `--max-steps N` | 单轮最多工具调用轮数，默认 50 |
| `--http-timeout N` | 单次请求超时秒数，默认 120 |
| `-q` / `-v` / `-vv` | 只显示最终答复 / 工具结果与思考 / 完整明细 |
| `--no-color` | 关闭彩色输出（默认仅在终端下着色） |
| `-l, --log [FILE]` | 完整交互写入日志，默认 `BO.log` |
| `--no-log` | 撤销已记录的 `-l` / `--log`，停止写日志 |

优先级：**命令行 > 环境变量 > `.bo` > 内置默认**。只有连接类参数（`-m`、`--base-url`、`--api-key`、`--root`、`--max-steps`、`--http-timeout`、`-l`）会加密写入当前目录的 `.bo`（0600）并在下次复用；`--yes`、`-q`/`-v`、`--no-color` 仅本次生效，不写入该文件。`.bo` 的密钥由目录路径派生，与目录绑定：移动或复制后无法解密，会被当作无效配置忽略；删除 `.bo` 即恢复默认。**文件可能含 API 密钥，请勿提交**（已在 `.gitignore` 中忽略）。

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

A single-file, standard-library-only minimal coding agent. Any machine running Python 3.6+ can run it — no third-party library required. The backend uses an OpenAI-compatible endpoint (`/v1/chat/completions`), so it works with OpenAI, DeepSeek, Qwen, vLLM, Ollama, LM Studio and any other compatible service.

`bo.py` is the Chinese edition, `bo_en.py` the English one.

## Features

- Single file, zero dependencies, Python 3.6+
- Three tools: `read_file` / `edit_file` / `run_command`
- Tiered output: `-q` / `-v` / `-vv`
- Full interaction log on disk (0600, owner-only)
- `--root` to restrict file access, `--yes` to confirm each command
- Option memory: connection options are encrypted into `.bo` and reused later

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

  · read_file({"path": "bo.py"})
  · edit_file({"path": "README.md", ...})
  ... tool calls and reasoning in between ...

Updated README.md, adding the module list to the Features section ...
```

Common practices:

1. **Scope the work** (recommended): `python3 bo_en.py --root ~/project`
2. **Gate command execution**: `python3 bo_en.py --yes`; without it, commands run directly
3. **Control verbosity**: `-v` shows tool results and thinking, `-vv` full detail, `-q` final answer only
4. **Keep a record**: `-l [FILE]` writes a log (default `BO.log`, 0600)
5. **Swap model/endpoint**: `-m` / `--base-url` / `--api-key`

```bash
python3 bo_en.py --root ~/project --yes -v -l session.log -m deepseek-chat
```

## Options

| Option | Description |
| :--- | :--- |
| `-m, --model NAME` | model name; defaults to `MODEL` / `OPENAI_MODEL` |
| `--base-url URL` | endpoint URL; defaults to `OPENAI_BASE_URL` |
| `--api-key KEY` | API key; defaults to `OPENAI_API_KEY` |
| `--yes` | ask for confirmation before each command (without it, commands run directly) |
| `--root DIR` | restrict `read_file` / `edit_file` to `DIR` |
| `--max-steps N` | max tool-call rounds per turn, default 50 |
| `--http-timeout N` | per-request timeout in seconds, default 120 |
| `-q` / `-v` / `-vv` | final answer only / tool results and thinking / full detail |
| `--no-color` | disable colored output (colors only on a terminal by default) |
| `-l, --log [FILE]` | write the full interaction to a log, default `BO.log` |
| `--no-log` | undo a recorded `-l` / `--log` and stop writing a log |

Precedence: **command line > environment variable > `.bo` > built-in default**. Only connection options (`-m`, `--base-url`, `--api-key`, `--root`, `--max-steps`, `--http-timeout`, `-l`) are encrypted into `.bo` (0600) and reused next time; `--yes`, `-q`/`-v`, `--no-color` apply to the current run only and are not written there. The `.bo` key is derived from the directory path and bound to it: after moving or copying it cannot be decrypted and is ignored as invalid; delete `.bo` to reset. **The file may contain your API key, so do not commit it** (already covered by `.gitignore`).

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
