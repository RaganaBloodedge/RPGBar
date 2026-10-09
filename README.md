# RPGBar

多人联机文字跑团（TRPG）demo。一个 Agent 担任 DM，用 RAG 约束 DM 遵循剧本推进剧情；多名玩家通过浏览器实时联机，各自可召唤小助手草拟行动，骰子由服务端统一投掷。

> 当前为 **Web 联机 demo** 阶段，用于验证「DM + RAG + 剧情状态机 + 骰子 + 多人联机」的完整玩法。架构与后续 Unity 版一一对应：服务端 = 主机，浏览器标签页 = 玩家客户端。

## 快速开始

```bash
# 1. 安装依赖（建议用虚拟环境）
pip install -r requirements.txt

# 2. 启动服务器
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000

# 3. 浏览器打开
# http://127.0.0.1:8000
# 开多个标签页（或让朋友连你的局域网 IP），输入同一个房间码即可多人联机。
```

## 启用真实 DM 大模型（云 API）

默认不配置 key 时，DM 走**脚本化兜底**（剧本旁白 + 出口/检定提示），玩法完整可玩。填入 key 后启用真实 LLM 叙事：

```bash
# 方式一：环境变量（以 DeepSeek 为例）
export RPGBAR_LLM_API_KEY="sk-xxx"
export RPGBAR_LLM_BASE_URL="https://api.deepseek.com/v1"
export RPGBAR_LLM_MODEL="deepseek-chat"

# 方式二：复制 config.example.json 为 config.json 并填 key
cp config.example.json config.json
```

DeepSeek / 智谱 GLM / 通义 Qwen 均为 OpenAI 兼容接口，改 `base_url` + `model` 即可切换。

## 保留本地大模型路径

llama.cpp 的 `llama-server` 也暴露 OpenAI 兼容的 `/v1/chat/completions` 接口，因此**本地模型只是改一行配置**：

```bash
# 1. 本地启动 llama.cpp server（例如 Qwen3-8B 的 GGUF）
llama-server -m qwen3-8b.Q4_K_M.gguf --port 8080

# 2. 指向本地
export RPGBAR_LLM_BASE_URL="http://127.0.0.1:8080/v1"
export RPGBAR_LLM_MODEL="qwen3-8b"
export RPGBAR_LLM_API_KEY="none"
```

发布时由**玩家用自己算力**跑本地 DM 即走这条路；玩家端的小助手 Agent 同理。

## 架构总览

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| 剧本仓库 + RAG | `server/rag.py` | 加载结构化剧本，jieba 分词 + BM25 检索相关场景片段 |
| 剧情状态机 | `server/state_machine.py` | 场景/flag/出口/检定，服务端权威状态 |
| 骰子 | `server/dice.py` | 服务端投掷，可设种子复现 |
| DM 编排 | `server/dm.py` | LLM 只提案，服务端校验后应用（护栏）；无 key 走脚本化兜底 |
| LLM 抽象 | `server/llm.py` | OpenAI 兼容 Provider，云 API 与本地 llama.cpp 同一套 |
| 多人服务 | `server/main.py` | FastAPI + WebSocket 房间，多玩家广播 |
| 客户端 | `web/` | 加入/建房、旁白流、掷骰、小助手建议 |

## 关键设计

- **守剧本 = RAG + 状态机双保险**：RAG 给 DM 递当前场景与相关片段，状态机管「剧情走到哪、允许往哪走」。
- **LLM 只提案、服务端校验**：LLM 返回的 `move_to` / `set_flags` 必须落在剧本已知的出口与 flag 白名单内，防止跑偏。
- **骰子在服务端**：玩家只发意图，结果由主机统一投掷并广播，防作弊。
- **小助手 = 玩家的私有 Agent**：demo 里复用同一 LLM 给建议；Unity 版将下沉到玩家本地的小模型（3B/4B）。

## 样例剧本

`scripts/sample_script.json`：《古堡秘宝》，5 个场景、2 个 NPC、多个检定与 flag 分支（说服看守 → 发现暗门 → 找钥匙/破门 → 取圣杯 → 决战守护者）。

## 后续路线

1. 跑通 Web demo（当前）。
2. 把 `server/` 逻辑移植进 Unity（DM 服务端 = 主机端，WebSocket → Mirror/Netcode）。
3. 玩家端小助手下沉到本地 3B/4B 模型（llama.cpp）。
