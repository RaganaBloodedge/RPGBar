# RPGBar

多人联机文字跑团（TRPG）。一个 Agent 担任 DM，用 RAG 约束 DM 遵循剧本推进剧情；玩家通过浏览器连接**服务器**实时联机，各自可召唤小助手草拟行动，骰子由服务端统一投掷。

> 完整使用说明、剧本格式、技术细节见 **[docs/说明书.md](docs/说明书.md)**。

- **架构**：服务器（FastAPI + WebSocket）承载 DM + RAG + 状态机 + 骰子；客户端是纯网页，连接服务器一起玩。
- **运行方式**：服务器跑在**某个玩家的电脑上**（发布到玩家 PC），其他玩家连进来一起玩。
- **当前形态**：Web 联机 demo，验证完整玩法；后续移植 Unity（WebSocket → Mirror/Netcode，服务端 = 主机端）。

## 快速开始（玩家 PC 当服务器）

### 1. 启动服务器

Windows 双击 `start.bat`；或命令行：

```bash
pip install -r requirements.txt
python -m server.main
```

启动后终端会打印两个地址：

```
[RPGBar] 本机访问: http://127.0.0.1:8000
[RPGBar] 局域网访问（发给朋友）: http://192.168.x.x:8000
```

### 2. 朋友加入

把「局域网访问」地址发给朋友，对方浏览器打开，输入名字 + 房间码即可联机（房间码留空自动建房）。

- **同一局域网**：直接连上面的局域网地址。
- **跨网络**：给服务器电脑做公网映射 / 内网穿透（如 frp、cpolar），或把服务器部署到云主机。

## DM 大模型：云 API 与本地 llama.cpp 共用一套

默认不配置 key 时，DM 走脚本化兜底。填入 key 后启用真实 LLM 叙事：

```bash
# 方式一：环境变量（以 DeepSeek 为例）
export RPGBAR_LLM_API_KEY="sk-xxx"
export RPGBAR_LLM_BASE_URL="https://api.deepseek.com/v1"
export RPGBAR_LLM_MODEL="deepseek-chat"

# 方式二：复制 config.example.json 为 config.json 并填 key
cp config.example.json config.json
```

DeepSeek / 智谱 GLM / 通义 Qwen 均为 OpenAI 兼容接口，改 `base_url` + `model` 即可切换。

### 保留本地大模型路径（发布到玩家 PC 的关键）

llama.cpp 的 `llama-server` 同样暴露 OpenAI 兼容的 `/v1/chat/completions` 接口，因此**本地模型只是改一行配置**：

```bash
# 1. 在玩家自己的机器上启动 llama.cpp server（例如 Qwen3-8B 的 GGUF）
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
| 多人服务 | `server/main.py` | FastAPI + WebSocket 房间，多玩家广播，中途加入判定与补课推送 |
| 客户端 | `web/` | 加入/建房、旁白流、掷骰、小助手建议、故事回顾面板 |

## 关键设计

- **服务器权威 + 客户端连接**：玩家只连服务器，不自己起服务；房间按码隔离。
- **守剧本 = RAG + 状态机双保险**：RAG 给 DM 递当前场景与相关片段，状态机管「剧情走到哪、允许往哪走」。
- **LLM 只提案、服务端校验**：LLM 返回的 `move_to` / `set_flags` 必须落在剧本已知的出口与 flag 白名单内，防止跑偏。
- **骰子在服务端**：玩家只发意图，结果由主机统一投掷并广播，防作弊。
- **小助手 = 玩家的私有 Agent**：demo 里复用同一 LLM 给建议；Unity 版将下沉到玩家本地的小模型（3B/4B）。
- **可中途加入**：对局进行中也能凭房间码加入。DM 会为新玩家生成一段带入旁白（全员可见，剧情上就是「他推门进来了」），并把「行程 / 线索 / 最近动态」的私有回顾面板单独推给新玩家。
- **flag 由剧本驱动**：合法 flag 从剧本自动收集（不再硬编码），flag 的中文描述也写在剧本里，用于侧栏与新人回顾。

## 样例剧本

`scripts/sample_script.json`：《古堡秘宝》，5 个场景、2 个 NPC、多个检定与 flag 分支（说服看守 → 发现暗门 → 找钥匙/破门 → 取圣杯 → 决战守护者）。

## 测试

```bash
python scripts/smoke_test.py        # 进程内 + 实时联机 + 中途加入（需先起服务器）
python scripts/smoke_test.py --unit # 仅进程内
```

当前 39 项全绿。

## 版本

当前版本 **v0.5.0**。每个版本的变更记录在 [CHANGELOG.md](CHANGELOG.md)，对应的 tag 与 Release 可在仓库的 Tags / Releases 页查看。

版本号只有一个来源：`server/__init__.py` 的 `__version__`。它会显示在服务器启动横幅、`GET /api/version`，以及网页的加入页与顶栏——所以"跑的是哪一版"一眼可辨。

发版流程见 CHANGELOG 顶部的「版本约定」。

## 打包发布（发给朋友）

用 PyInstaller 把服务器打成 exe，玩家双击即跑、无需装 Python：

```bash
pip install pyinstaller
pyinstaller --name RPGBarServer --onedir --noconfirm --clean \
  --add-data "web;web" --add-data "scripts;scripts" \
  --add-data "config.example.json;." \
  --collect-all jieba \
  --exclude-module uvloop --exclude-module watchfiles \
  run_server.py
```

产物在 `dist/RPGBarServer/`。**打包后把给玩家的说明拷进去再压缩**（指南源码在仓库里，避免每次重打都要重写）：

```bash
cp docs/使用指南.txt dist/RPGBarServer/使用指南.txt
python -c "import shutil; shutil.make_archive('RPGBarServer','zip',root_dir='dist',base_dir='RPGBarServer')"
```

朋友解压双击 `RPGBarServer.exe`，按包内 `使用指南.txt` 操作。详见 [docs/说明书.md](docs/说明书.md) 第十节。

## 后续路线

1. 跑通 Web 联机 demo（当前）。
2. 把 `server/` 逻辑移植进 Unity（DM 服务端 = 主机端，WebSocket → Mirror/Netcode）。
3. 玩家端小助手下沉到本地 3B/4B 模型（llama.cpp）。
