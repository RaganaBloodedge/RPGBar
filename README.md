# RPGBar

多人联机文字跑团（TRPG）。一个 Agent 担任 DM，用 RAG 约束 DM 遵循剧本推进剧情；玩家通过浏览器连接**服务器**实时联机，各自可召唤小助手草拟行动，骰子由服务端统一投掷。剧本外的路人 NPC 由主机小助手现场生成人物小传，不再是「空角色」。

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

## 三个 Agent 与两条模型通道

游戏里有**三个 Agent**，都按标准写法实现（模型自己决定调哪个工具 → 服务端执行 → 结果回灌 → 循环到收尾）：

| Agent | 模型量级 | 能用的工具 | 干什么 |
| --- | --- | --- | --- |
| **主机 DM**（`NarratorAgent`） | 大模型 | `read_scene` `list_exits` `list_checks` `lookup_script` `npc_card`（只读）+ `move_to` `set_flag` `roll_check` `npc_introduce`（改状态） | 推进剧情、投骰判定、扮 NPC |
| **主机小助手**（`AssistantAgent`） | 小模型 | 无（固定流程单轮 JSON） | 给剧本外 NPC 生成人物小传、算反应权重、后续做剧情摘要 |
| **玩家小助手**（`AdvisorAgent`） | 小模型 | `read_scene` `list_valid_actions` `lookup_script`（**全是只读**） | 给你 2-4 条行动建议 |

小助手改不了剧情，不是靠提示词求它别改，而是**它的工具集里根本没有写工具**。
两台小助手其实是**同一个任务引擎（`ToolAgent`）上的两条固定流程**——`persona` / `react_weights` / `action_advice`
各自是一条独立的 COT（职责 system + 输出 schema + 校验兜底），共用同一个 `server/agents/tool_agent.py`。

**默认三个槽位都是「留空」**——不接任何模型也能完整游玩（DM 走 `dm.py` 的确定性流水线，NPC 走内置原型兜底，玩法完整）。

### 在游戏里接入（推荐，不用改配置文件）

进房后点顶栏 **「⚙ 设置」** 里的**模型**部分：

- **云 API**：选 DeepSeek / OpenAI / 智谱 GLM 预设（或自定义 `base_url`），填 `api_key` 与模型名。
- **本地模型**：选 llama.cpp / Ollama / LM Studio 预设，指向本机地址，**不需要 key**。
- 点 **「测试连接」** 会真的发一次请求，并额外探测该模型**是否支持工具调用**（不支持会自动降级，见下）。
- **API Key 只存在你自己浏览器里**，服务端从不回传；设置下次进房自动生效。
- **主机 DM 与主机小助手只有房主能改**；玩家小助手人人各配各的——正好对应「玩家机器跑本地小模型、主机额外接一个大模型 + 一个小模型」。

顶栏胶囊会显示当前状态：`脚本化` / `仅 DM` / `仅助手` / `DM + 助手`。

### 用配置文件 / 环境变量接入

```bash
# 方式一：环境变量（主机 DM）
export RPGBAR_DM_MODEL_API_KEY="sk-xxx"
export RPGBAR_DM_MODEL_BASE_URL="https://api.deepseek.com/v1"
export RPGBAR_DM_MODEL_MODEL="deepseek-chat"

# 玩家小助手（可指向本机 llama.cpp）
export RPGBAR_ADVISOR_MODEL_BASE_URL="http://127.0.0.1:8080/v1"
export RPGBAR_ADVISOR_MODEL_MODEL="qwen3-4b"
export RPGBAR_ADVISOR_MODEL_KIND="local"

# 主机小助手（NPC 人设 / 反应权重 / 剧情摘要，可指向本机 llama.cpp）
export RPGBAR_ASSISTANT_MODEL_BASE_URL="http://127.0.0.1:8081/v1"
export RPGBAR_ASSISTANT_MODEL_MODEL="qwen2.5-3b-instruct"
export RPGBAR_ASSISTANT_MODEL_KIND="local"

# 方式二：复制 config.example.json 为 config.json 填 models.dm / models.assistant / models.advisor
cp config.example.json config.json
```

> 旧变量名 `RPGBAR_LLM_API_KEY` / `_BASE_URL` / `_MODEL` 仍然可用，会自动映射到主机 DM 槽位。

DeepSeek / 智谱 GLM / 通义 Qwen 均为 OpenAI 兼容接口，改 `base_url` + `model` 即可切换。
llama.cpp 的 `llama-server`、Ollama、LM Studio 同样暴露 OpenAI 兼容的 `/v1/chat/completions`，
所以**本地模型与云 API 是同一套代码，只是 `base_url` 不同**：

```bash
llama-server -m qwen3-8b.Q4_K_M.gguf --port 8080   # 本地起一个
```

### 模型不支持工具调用怎么办

很多小模型、老版本服务不认 `tools` 参数。这时 Agent 会自动降级为**单轮 JSON 协议**：
把同样的意图写成 `{"narration": "...", "move_to": "...", "set_flags": [...]}`，
功能不减，只是少了多轮工具往复。「测试连接」会直接把结果告诉你。

## 架构总览

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| 剧本读取 + 自动切片 | `server/script_loader.py` | 读剧本文件 → 切片 → 生成次级 prompt；含剧本自检 |
| 剧本仓库 + RAG | `server/rag.py` | 持有已切片的剧本，jieba 分词 + BM25 检索相关场景片段 |
| 剧情状态机 | `server/state_machine.py` | 场景/flag/出口/检定，服务端权威状态 |
| 骰子 | `server/dice.py` | 服务端投掷，可设种子复现 |
| Agent 骨架 | `server/agents/base.py` | 两层提示词 + 工具注册 + 工具调用循环 + 两条降级路径 |
| 任务引擎（小模型） | `server/agents/tool_agent.py` | 一条任务 = 一条固定 COT；单轮 JSON / 带工具两条路，含 JSON 容错与校验兜底 |
| 主机 DM Agent | `server/agents/narrator.py` | 大模型；System prompt 写职责，次级 prompt 给剧本 |
| 主机小助手 Agent | `server/agents/persona.py` | 小模型；`persona`（生成人物小传）/ `react_weights`（算反应权重）两条任务 |
| 玩家小助手 Agent | `server/agents/advisor.py` | 小模型；`action_advice` 任务，**只有只读工具**，次级 prompt 为公开版 |
| NPC 子系统 | `server/npc.py` | 人物卡 / 别名索引 / 原型兜底表 / NPC 私有记忆（好感度）；反应落点交给骰子 |
| 无模型的 DM 流水线 | `server/dm.py` | 确定性剧本编排（留空 API 时的兜底），与 Agent 共用同一套状态机 |
| LLM 抽象 | `server/llm.py` | OpenAI 兼容 Provider：云 API 与本地模型同一套；含工具调用与降级判定 |
| 多人服务 | `server/main.py` | FastAPI + WebSocket 房间，三个模型槽位运行期配置，剧本读取/切换接口，NPC 同步，中途加入推送 |
| 客户端 | `web/` | 加入/建房、旁白流、掷骰、小助手建议、故事回顾、**在场人物**、设置抽屉（模型 + 剧本） |

## 关键设计

- **服务器权威 + 客户端连接**：玩家只连服务器，不自己起服务；房间按码隔离。
- **一个引擎、多条固定 COT**：两台小助手共用 `ToolAgent`；建议、生成人物小传、算反应权重各是一条独立流程（职责 system + 输出 schema + 校验兜底），互不污染。
- **职责与内容分层**：System prompt 只写 Agent 职责；剧本内容由 `script_loader` 自动切片后生成次级 prompt，换剧本不动人格。
- **守剧本 = RAG + 状态机双保险**：RAG 给 DM 递当前场景与相关片段，状态机管「剧情走到哪、允许往哪走」。
- **护栏做在工具与校验器里，不靠提示词**：DM 的 `move_to` 只接受当前场景**已解锁**的出口、`set_flag` 只接受剧本声明过的 flag、
  骰子只能由 `roll_check` 在服务端投——模型拿不到骰子，也就编不出结果。小助手则连写工具都没有。
  NPC 的反应权重由校验器裁剪归一化，**模型只给区间、给不出骰点**。
- **NPC 记忆与 DM 隔离**：NPC 的交往记录与好感度留在 `NpcMemory`；DM 只收到「人物卡 + 当前状态快照」的提炼结论，不掺流水账，剧情上下文保持干净。
- **骰子在服务端**：玩家只发意图，结果由主机统一投掷并广播，防作弊。
- **留空也能玩**：三个模型槽位默认 `off`，DM 退回确定性流水线、NPC 退回内置原型兜底；接了模型才启用 Agent 工具循环，两者共用同一套状态机。
- **可中途加入**：对局进行中也能凭房间码加入。DM 会为新玩家生成一段带入旁白（全员可见，剧情上就是「他推门进来了」），并把「行程 / 线索 / 最近动态」的私有回顾面板单独推给新玩家。
- **flag 由剧本驱动**：合法 flag 从剧本自动收集（不再硬编码），flag 的中文描述也写在剧本里，用于侧栏与新人回顾。

## 提示词分层：System 管职责，次级 prompt 管剧本

各 Agent 的提示词都分两层：

| 层 | 内容 | 随什么变 |
| --- | --- | --- |
| **System prompt** | Agent 的**职责**与边界（DM 是主持人；小助手只读给建议） | 基本不变 |
| **次级 prompt** | 剧本的**内容**：世界观、语气、切片索引 | 换剧本就换它 |

次级 prompt 排在 System prompt 之后，作为**第二条 system 消息**发给模型。这样职责与内容解耦：换剧本不用动 Agent 的人格的护栏。

小助手拿到的是次级 prompt 的**公开版**——只有剧本名、公开背景与「只能建议剧本里真实存在的行动」这条约束，**不含场景索引与 DM 内幕**（最小权限 + 防剧透）。

### 剧本读取、切片与次级 prompt 怎么来的

`server/script_loader.py` 是「读取文件 → 自动切片 → 生成次级 prompt」的单一入口：

- **结构化剧本 JSON**：一个场景 = 一个切片，另加一个「剧本设定」切片。
- **无结构文本**（`.md` / `.txt`）：按 Markdown 标题切段，超长章节按段落装箱（默认 900 字/片），
  并**自动合成一条线性场景链**（每片指向下一片），让纯文本剧本也能直接开局试玩。
- 读取时顺带**自检**：出口断链、无法到达的场景、`start_scene` 不存在、缺 `system` 段都会报出来。

## 剧本

剧本放在 `scripts/`，用 `config.json` 的 `script` 字段或环境变量选：

```bash
# 默认：原创剧本《古堡秘宝》
python -m server.main

# 换成第二个剧本
RPGBAR_SCRIPT=scripts/totsk_l1.json python -m server.main
```

| 剧本 | 文件 | 说明 |
| --- | --- | --- |
| **古堡秘宝**（默认） | `scripts/sample_script.json` | 原创。5 场景、2 NPC、多条检定与 flag 分支 |
| **蛇王墓 · 第一层「假墓」** | `scripts/totsk_l1.json` | 改编自 Skerples 的 *Tomb of the Serpent Kings*。9 场景、12 线索、11 处检定 |

> 《蛇王墓》是**改编作品，以 CC BY-NC-SA 4.0 提供**（不是本仓库的 MIT）：可自由分享/改编/翻译，但**不得商用**，且须署名原作者 Skerples。详见文件内的 `meta` 字段。

剧本名会显示在启动横幅、`/api/version` 与网页上——**一眼看出这局跑的是哪个本**。

### 运行期读取与切换剧本

不用重启服务器，在游戏里点顶栏 **「⚙ 设置」** 的**剧本**卡片就能：读取某个剧本文件（预览切片与生成的次级 prompt）、
房主一键切换活动剧本，或直接粘贴剧本内容（自动识别 JSON / Markdown）。

对应的接口：

| 接口 | 作用 |
| --- | --- |
| `GET /api/scripts` | 列出可用的剧本文件（标题 / 场景数 / 切片数 / 自检告警） |
| `POST /api/scripts/inspect` | 读取剧本 → 自动切片 → 返回切片清单与两份次级 prompt（只看不改） |
| `POST /api/scripts/load` | 读取并切换「活动剧本」（新开的房间生效；进行中的对局不受影响） |

```bash
curl -X POST http://127.0.0.1:8000/api/scripts/inspect \
  -H "Content-Type: application/json" -d '{"path":"totsk_l1.json"}'
```

> 出于安全考虑，`path` 只允许指向 `scripts/` 目录内的文件（服务端默认绑 `0.0.0.0`，不能变成任意文件读取接口）；
> 想用别处的剧本，就把它拷进 `scripts/`，或者用 `content` 直接提交内容。

## 测试

```bash
python scripts/smoke_test.py        # 进程内 + 实时联机 + 中途加入 + 模型设置 + 剧本接口 + NPC 子系统（需先起服务器）
python scripts/smoke_test.py --unit # 仅进程内

# 可选：前端 DOM 校验（需 Node + jsdom，验证设置抽屉与在场人物面板的交互）
npm i jsdom && node scripts/ui_check.js
```

当前 **236 项全绿**（Python）+ **71 项全绿**（前端 DOM，可选）。
实时联机校验会跟随服务器当前加载的剧本自动选用对应动作，换剧本不用改测试。
Agent 层的测试不需要真实模型——用一个按脚本吐回复的假 Provider 就能验完整工具循环。

## 版本

当前版本 **v0.8.0**。每个版本的变更记录在 [CHANGELOG.md](CHANGELOG.md)，对应的 tag 与 Release 可在仓库的 Tags / Releases 页查看。

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

1. 跑通 Web 联机 demo：三 Agent + 双模型通道 + 剧本读取/切片 + NPC 自动人设（当前）。
2. **剧情档案与混合检索**：把已输出的主线全文落到本地只增 JSONL 并向量化，内置 `bge-small-zh` + BM25 混合检索；
   DM 新增 `recall_history` 只读工具，超出上下文时按需回捞关键角色行为，防止模型「忘事」。
3. 把 `server/` 逻辑移植进 Unity（DM 服务端 = 主机端，WebSocket → Mirror/Netcode）。
4. 玩家端小助手**默认**下沉到本地 3B/4B 模型（llama.cpp），主机玩家额外接大模型 + 主机小助手——通道已就位，等接默认值。
5. 有模型时用模型给切片做摘要（现在切片索引是按场景/标题生成的，摘要可交给小模型离线生成后缓存）。
