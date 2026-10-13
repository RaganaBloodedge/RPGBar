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

## 三个 Agent 与三条模型通道

游戏里有**三个 Agent**，都按标准写法实现（模型自己决定调哪个工具 → 服务端执行 → 结果回灌 → 循环到收尾）：

| Agent | 模型量级 | 能用的工具 | 干什么 |
| --- | --- | --- | --- |
| **主机 DM**（`NarratorAgent`） | 大模型 | `read_scene` `list_exits` `list_checks` `lookup_script` `npc_card` `recall_history`（只读）+ `move_to` `set_flag` `roll_check` `npc_introduce`（改状态） | 推进剧情、投骰判定、扮 NPC、回忆前情 |
| **主机小助手**（`AssistantAgent`） | 小模型 | 无（固定流程单轮 JSON） | 给剧本外 NPC 生成人物小传、算反应权重、滚动剧情摘要 |
| **玩家小助手**（`AdvisorAgent`） | 小模型 | `read_scene` `list_valid_actions` `lookup_script`（**全是只读**） | 给你 2-4 条行动建议 |

小助手改不了剧情，不是靠提示词求它别改，而是**它的工具集里根本没有写工具**。
两台小助手其实是**同一个任务引擎（`ToolAgent`）上的两条固定流程**——`persona` / `react_weights` / `action_advice`
各自是一条独立的 COT（职责 system + 输出 schema + 结果校验），共用同一个 `server/agents/tool_agent.py`。

> **默认三个槽位都是「留空」，但留空 = 玩不了。** 本仓库**没有脚本化兜底**（2026-10-13 起）：
> 槽位没接模型就没有旁白、没有剧情推进，界面会直接把「本轮没有生成（原因）」摆出来。
> 想让编出来的文字盖住故障、让玩家以为一切正常，是更坏的结果 —— **要玩就必须接一个模型**：
> ① 内置引擎（全离线，见下）；② 任意 OpenAI 兼容的云 API 或本地 `llama.cpp` / Ollama。

### 在游戏里接入（推荐，不用改配置文件）

进房后点顶栏 **「⚙ 设置」** 里的**模型**部分：

- **云 API**：选 DeepSeek / OpenAI / 智谱 GLM 预设（或自定义 `base_url`），填 `api_key` 与模型名。
- **本地模型**：选 llama.cpp / Ollama / LM Studio 预设，指向本机地址，**不需要 key**。
- 点 **「测试连接」** 会真的发一次请求，并额外探测该模型**是否支持工具调用**（不支持会改用单轮 JSON 协议，见下）。
- **API Key 只存在你自己浏览器里**，服务端从不回传；设置下次进房自动生效。
- **主机 DM 与主机小助手只有房主能改**；玩家小助手人人各配各的——正好对应「玩家机器跑本地小模型、主机额外接一个大模型 + 一个小模型」。

顶栏胶囊会显示当前状态：`未接入` / `仅 DM` / `仅助手` / `DM + 助手`（只有接了模型的那几个 Agent 才会干活）。

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

### 内置引擎（进程内，零外部服务）

第三条通道：**内置推理引擎**（`RPGBarEngine` —— 这个项目自己的 C++ 引擎）。
它**进程内直连 `rpg_engine.dll`**，不用起 llama.cpp / Ollama，也不占任何端口，
不用管版本兼容、不用管端口冲突；权重就是一份 GGUF。

**两个后端，同一个 ABI**：CPU 版（`RPGBarEngine`）与 CUDA 版（`RPGBarEngineCUDA`）
是同一份 C 接口的两份实现，所以在宿主侧"用哪个后端"就只是**加载哪一个文件**：

| 后端 | 产物 | 何时用 |
|---|---|---|
| `cpu`（默认） | `models/rpg_engine.dll` | 任何机器都能跑，1.7B 约 10–14 tok/s |
| `gpu` | `models/rpg_engine_cuda.dll` | 有 NVIDIA 卡且编出了 CUDA 版产物时，快得多 |

**第一步：把引擎产物接到本项目**（动态库 + 权重，二者都在引擎仓库里）：

```bash
python scripts/sync_engine.py                  # 两个后端都接（默认；找不到的那个会提示）
python scripts/sync_engine.py --backend gpu    # 只接 CUDA 版
python scripts/sync_engine.py --lib ~/cuda-build/librpg_engine.so   # GPU 库直接指路径
```

默认用**硬链接**（同一磁盘卷内零额外空间 —— 权重 1 GB 量级，复制一份纯属浪费），
跨盘或文件系统不支持时自动回退为复制。`models/` 下出现 `rpg_engine.dll`（CPU 版）
与 `rpg_engine_cuda.dll`（CUDA 版，同步时按后缀改名 —— **文件名即后端**）与
`Qwen3-*.gguf` 即可。也可以跳过这一步，直接在下面把 `lib` / `model` 写成绝对路径。

> ⚠️ CUDA 版的 **Windows `.dll` 已经编出来了** —— 在 `RPGBarEngineCUDA` 仓库里跑
> `build_win.ps1`（要 **CUDA Toolkit + VS Build Tools**，不必装整装 VS）就会得到
> `build-win/rpg_engine.dll`，它是**宿主真正加载的那一份**，本项目的同步脚本会自动
> 按后缀改名为 `models/rpg_engine_cuda.dll`。WSL / Linux 下 `bash build_wsl.sh` 出的
> `librpg_engine.so` 同样能用（产物在 `~/cuda-build`，构建目录刻意放在 ext4 上），
> 用 `--lib` 指过去即可。
>
> 💡 **不发 CUDA 版产物时不用带 `cudart64_*.dll`**：CUDA 运行库已经**静态链进**
> `.dll` 里了。目标机器需要的只有 **NVIDIA 驱动 + 算力 ≥ 7.5（Turing 起）的卡**，
> 这两条都在下面的探测里查。

**第二步：改一处开关**，把想用它的槽位 `kind` 改成 `engine`：

```jsonc
{
  "engine": {
    "enabled": true,
    "backend": "cpu", // cpu（默认）/ gpu —— 选 gpu 而机器上没有 CUDA 版产物时会退回 CPU
    "lib": "",        // 留空 = 按 backend 自动找 models/rpg_engine[_cuda].dll
    "model": "",      // 留空 = 自动找 models/Qwen3-*.gguf
    "threads": 0,     // 0 = 引擎按 CPU 核数自己定（gpu 后端忽略它）
    "context": 4096,  // 每个会话的 KV Cache 容量（@4096≈896 MiB/会话）；别调到 4096 以下
    "max_tokens": 768,
    "think": false    // Qwen3 思考模式，默认关；打开 = 每轮 35-50 秒、质量略好（见下）
  },
  "models": {
    "assistant": { "kind": "engine" },   // 主机小助手：跑在主机
    "advisor":   { "kind": "engine" }    // 玩家小助手：每端一份，本地跑
  }
}
```

> 🎛️ **后端也能在游戏里选**：「⚙ 设置 → 内置引擎 → 推理后端」下拉框（房主可改，
> 全房间共用）。客户端只能在这个枚举里挑，指不了任意 dll —— 与"界面不许改
> `lib` / `model`"是同一条边界。
>
> 选了 `gpu` 而机器上没有 CUDA 版产物时会**自动退回 CPU**（游戏照常能玩），
> 而且**退没退看得见**：徽标与启动日志会显示"实际 CPU（GPU 库缺失）"，
> 首次生成后还会打一行"请求后端 gpu · 实际后端 cpu"。反过来，真跑在 GPU 上时
> 徽标显示 "· GPU ·"。**权威判据是引擎自己报的**（CUDA 版的 `model_info` 多返回
> `engine` / `backend` 两个键，CPU 版没有 —— 所以不需要动 ABI）。
>
> 🚫 **「GPU」那一项会按本机实际情况置灰**：CUDA 版产物是随包分发的（别人机器上
> 没独显，文件也照样在），所以"文件在不在"不能代表"能不能用"。服务端**启动时自动
> 探一次**（结果缓存），依次看三件事 ——
> ① CUDA 版产物在不在；② **它能不能被加载**（缺 VC 运行库在这一步就暴露，比"第一次
> 生成时崩"具体）；③ **驱动能不能报出设备**（`cuInit` + `cuDeviceGetCount > 0`，
> 顺带查主卡算力，低于 CUDA 13 的地板 **7.5 / Turing** 时给出具体原因）。
> 任何一道不过就把这一项变成灰色、**把原因写在下面**，启动日志里也会打一行。
> CPU 不受影响，没有独显的机器照样能玩。
>
> ⚠️ **② 与 ③ 必须分开**：`nvcuda.dll` 是**延迟加载**的 —— 没装驱动的机器上
> `LoadLibrary` 照样成功。所以"库能不能加载"与"有没有可用 GPU"是两件事，
> 用前者代替后者会得到"能用"的假结论。反过来，**不要再去找 `cudart64_*.dll`** ——
> CUDA 运行库已经静态链进产物里了，去找它只会永远判不可用。

> 槽位**只需要写 `kind`** —— `lib` / `model` / `threads` / `context` 由 `engine` 段
> 统一供货。想让某个槽位用**另一份权重**（比如 DM 用 4B、小助手用 1.7B），在那个槽位里
> 写一行 `"model": "<绝对路径>.gguf"` 就行：**槽位显式写的权重优先于 `engine` 段**。

> ⚠ **`context` 别调到 4096 以下**。DM 第一轮 prompt（职责 + 剧本 + 本轮上下文 +
> 十几个工具的 JSON Schema）实测就要 2400+ token，2048 装不下会让引擎报「序列超长」、
> 那次生成直接失败（界面会标出「本轮没有生成」，不再拿脚本化旁白盖过去）。

> 💡 **嫌慢就把 `think` 关掉**（引擎 0.4.1 起可以随便关）—— **游戏内**「⚙ 设置 →
> 内置引擎」就有这个开关，不用改这个文件。实测同一份 prompt：
> `false` 时每次生成约 **4–6 秒**；`true` 时每次生成先吐 250–370 个思考 token，约
> **35–50 秒**。⚠️ 这里的「每次生成」是**一次引擎调用**，**不是玩家的一次行动** ——
> 玩家点一次行动要连着跑好几轮生成（DM 工具循环 + NPC 预备调用），
> 本机实测**一整轮：CPU 109–199 秒 / GPU 21–38 秒**。关掉只是建议质量略降，功能不减。
> 之前"开了工具就不能关思考"是引擎的
> 一个堆越界 bug 导致的（同一会话第二次生成时越界写），已修，详见说明书 §7.3。
> 另：第一次生成前有约 30 秒的权重冷缺页开销（mmap），与 `think` 无关。

**先自检这台机器能不能用**（不启动服务器，也不写任何文件）：

```bash
python scripts/engine_probe.py                  # 验 CPU 后端
python scripts/engine_probe.py --backend gpu    # 验 CUDA 后端
```

它会把「库 → 权重 → 单轮对话 → 工具调用」挨个跑一遍，并报出引擎版本与实测
tok/s，还会报出**三个**后端：你请求的、按文件名找到的那份库、以及**实际跑在
哪个后端**（读引擎的 `model_info` —— 这个才是权威）。三者不一致时会明确说出来，
"选了 GPU 其实是 CPU"在这条路上最容易让人白排查半天。

**引擎没启用、或库与权重找不到时，`kind=engine` 的槽位等价于 `off`** —— 不报错，
但那个 Agent 就不会有产出（失败会显示在界面上，理由见上）。
（例外只有一处：请求 `gpu` 但只有 CPU 版产物 —— 那时**退回 CPU 版**而不是当没接，
因为"能玩 + 说实话"优于"诚实但玩不了"。）

> 代价要提前知道：Qwen3-1.7B 权重在只有 CPU 的机器上约 7–10 tok/s，DM 一轮 25–35 秒
> （4B 更慢）。这是"全离线、不依赖任何外部服务"的固有代价，不是缺陷。

DeepSeek / 智谱 GLM / 通义 Qwen 均为 OpenAI 兼容接口，改 `base_url` + `model` 即可切换。
llama.cpp 的 `llama-server`、Ollama、LM Studio 同样暴露 OpenAI 兼容的 `/v1/chat/completions`，
所以**本地模型与云 API 是同一套代码，只是 `base_url` 不同**：

```bash
llama-server -m qwen3-8b.Q4_K_M.gguf --port 8080   # 本地起一个
```

### 模型不支持工具调用怎么办

很多小模型、老版本服务不认 `tools` 参数。这时 Agent 会改用**单轮 JSON 协议**：
把同样的意图写成 `{"narration": "...", "move_to": "...", "set_flags": [...]}`，
功能不减，只是少了多轮工具往复。「测试连接」会直接把结果告诉你。

> ⚠️ 这不是「兜底」：**它仍然是真的调了模型**，写不好 JSON 就是失败，不会伪造结果。
>（**内置引擎不需要这条**：Qwen3 原生支持工具调用，引擎的 ChatML 渲染与
> `<tool_call>` 解析都对着官方模板逐字节做过对拍。）

## 剧情档案与内置嵌入模型

DM 的上下文装不下一整局对话。所以本局已经输出的旁白、玩家行动、骰点结论会**逐条落进本地存档**
（`data/chronicle-<房间码>.jsonl`，只增不改），DM 用只读工具 `recall_history` 按需回捞——
上下文不膨胀，也不会「忘事」。

检索是**混合**的：关键词（BM25）+ 语义向量，用 RRF 融合。语义侧用内置的
**bge-small-zh-v1.5**（ONNX，本地推理、离线可用）：

```bash
python scripts/fetch_embedding.py        # 下载模型到 models/（约 24MB，量化版）
python scripts/fetch_embedding.py --fp32 # 想要更准就下完整 fp32 版（约 95MB）
```

**不下也能玩**：服务器探测不到模型就自动退化成纯 BM25，其余功能一律不受影响
（启动横幅与 `/api/version` 会写明当前用的是哪种）。

## 架构总览

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| 剧本读取 + 自动切片 | `server/script_loader.py` | 读剧本文件 → 切片 → 生成次级 prompt；含剧本自检 |
| 剧本仓库 + RAG | `server/rag.py` | 持有已切片的剧本，jieba 分词 + BM25 检索相关场景片段 |
| 剧情状态机 | `server/state_machine.py` | 场景/flag/出口/检定，服务端权威状态 |
| 骰子 | `server/dice.py` | 服务端投掷，可设种子复现 |
| Agent 骨架 | `server/agents/base.py` | 两层提示词 + 工具注册 + 工具调用循环 + 失败路径（只有 `llm` / `failed` 两种结局） |
| 任务引擎（小模型） | `server/agents/tool_agent.py` | 一条任务 = 一条固定 COT；单轮 JSON / 带工具两条路，含 JSON 容错与结果校验 |
| 主机 DM Agent | `server/agents/narrator.py` | 大模型；System prompt 写职责，次级 prompt 给剧本 |
| 主机小助手 Agent | `server/agents/persona.py` | 小模型；`persona`（生成人物小传）/ `react_weights`（算反应权重）两条任务 |
| 玩家小助手 Agent | `server/agents/advisor.py` | 小模型；`action_advice` 任务，**只有只读工具**，次级 prompt 为公开版 |
| NPC 子系统 | `server/npc.py` | 人物卡 / 别名索引 / 原型兜底表 / NPC 私有记忆（好感度）；反应落点交给骰子 |
| 剧情档案（长期记忆） | `server/chronicle.py` | 只增 JSONL + BM25/向量混合检索（RRF）；滚动主线摘要；DM 用 `recall_history` 只读取用 |
| 内置嵌入模型 | `server/embedding.py` | bge-small-zh-v1.5 的本地 ONNX 封装，缺失时静默降级为纯 BM25 |
| 无模型的 DM 流水线 | `server/dm.py` | 状态机层（行动↔检定/出口匹配）的确定性实现；**运行时不走这条**，由冒烟测试覆盖 |
| LLM 抽象 | `server/llm.py` | OpenAI 兼容 Provider：云 API 与本地模型同一套；含工具调用能力探测 |
| 多人服务 | `server/main.py` | FastAPI + WebSocket 房间，三个模型槽位运行期配置，剧本读取/切换接口，NPC 同步，剧情档案落盘，中途加入推送 |
| 客户端 | `web/` | 加入/建房、旁白流、掷骰、小助手建议、故事回顾、**在场人物**、**剧情档案状态**、设置抽屉（模型 + 剧本） |

## 关键设计

- **服务器权威 + 客户端连接**：玩家只连服务器，不自己起服务；房间按码隔离。
- **一个引擎、多条固定 COT**：两台小助手共用 `ToolAgent`；建议、生成人物小传、算反应权重各是一条独立流程（职责 system + 输出 schema + 结果校验），互不污染。
- **职责与内容分层**：System prompt 只写 Agent 职责；剧本内容由 `script_loader` 自动切片后生成次级 prompt，换剧本不动人格。
- **守剧本 = RAG + 状态机双保险**：RAG 给 DM 递当前场景与相关片段，状态机管「剧情走到哪、允许往哪走」。
- **护栏做在工具与校验器里，不靠提示词**：DM 的 `move_to` 只接受当前场景**已解锁**的出口、`set_flag` 只接受剧本声明过的 flag、
  骰子只能由 `roll_check` 在服务端投——模型拿不到骰子，也就编不出结果。小助手则连写工具都没有。
  NPC 的反应权重由校验器裁剪归一化，**模型只给区间、给不出骰点**。
- **NPC 记忆与 DM 隔离**：NPC 的交往记录与好感度留在 `NpcMemory`；DM 只收到「人物卡 + 当前状态快照」的提炼结论，不掺流水账，剧情上下文保持干净。
- **DM 的分层记忆**：热点层 = 滚动主线摘要 + 最近若干轮原文（直接进上下文）；冷层 = 全部主线原文的只增档案，靠 `recall_history` 按需检索（内置 bge-small-zh 向量 + BM25 混合召回）。上下文不膨胀，也不会「忘事」；档案由服务端落盘，模型只读。
- **骰子在服务端**：玩家只发意图，结果由主机统一投掷并广播，防作弊。
- **不伪造产出**：没有模型、模型报错、模型没吐出可用内容 —— 一律**显式失败**（`source=failed`），
  界面直接标出「本轮没有生成 + 原因」。绝不拿脚本化文案顶替：那会让玩家以为一切正常，
  把真实的模型故障盖住。唯一不算兜底的是**单轮 JSON 协议**——它仍然真的调了模型。
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

当前 **328 项**（Python，含需要服务器的实时联机校验）+ **115 项**（前端 DOM，可选）。
> ⚠️ 不带服务器跑 Python 冒烟只会打印 `256 通过 / 0 失败` —— 实时那部分打了 `[SKIP]`，
> 那是"一半没跑"，不是"整套全过"。`ui_check.js` 默认打 `127.0.0.1:8000`，
> 服务器在别的端口时用 `RPGBAR_BASE_URL` 指过去。
实时联机校验会跟随服务器当前加载的剧本自动选用对应动作，换剧本不用改测试。
Agent 层的测试不需要真实模型——用一个按脚本吐回复的假 Provider 就能验完整工具循环。

## 版本

当前版本 **v0.11.0**。每个版本的变更记录在 [CHANGELOG.md](CHANGELOG.md)，对应的 tag 与 Release 可在仓库的 Tags / Releases 页查看。

版本号只有一个来源：`server/__init__.py` 的 `__version__`。它会显示在服务器启动横幅、`GET /api/version`，以及网页的加入页与顶栏——所以"跑的是哪一版"一眼可辨。

发版流程见 CHANGELOG 顶部的「版本约定」。

## 打包发布（发给朋友）

用 PyInstaller 把服务器打成 exe，玩家双击即跑、无需装 Python：

```bash
pip install pyinstaller
python scripts/fetch_embedding.py          # 可选：带上内置嵌入模型（约 24MB）
pyinstaller --name RPGBarServer --onedir --noconfirm --clean \
  --add-data "web;web" --add-data "scripts;scripts" \
  --add-data "config.example.json;." \
  --add-data "models;models" \
  --collect-all jieba \
  --exclude-module uvloop --exclude-module watchfiles \
  run_server.py
```

> `--add-data "models;models"` 只在确实下载了嵌入模型时加；没有 `models/` 目录就删掉这一行
> （不带嵌入模型也能玩，剧情档案会自动退化成纯关键词检索 —— 这跟"有没有大模型"无关）。

产物在 `dist/RPGBarServer/`。**打包后把给玩家的说明拷进去再压缩**（指南源码在仓库里，避免每次重打都要重写）：

```bash
cp docs/使用指南.txt dist/RPGBarServer/使用指南.txt
python -c "import shutil; shutil.make_archive('RPGBarServer','zip',root_dir='dist',base_dir='RPGBarServer')"
```

朋友解压双击 `RPGBarServer.exe`，按包内 `使用指南.txt` 操作。详见 [docs/说明书.md](docs/说明书.md) 第十节。

## 后续路线

1. 跑通 Web 联机 demo：三 Agent + 双模型通道 + 剧本读取/切片 + NPC 自动人设 + 剧情档案混合检索（当前）。
2. 把 `server/` 逻辑移植进 Unity（DM 服务端 = 主机端，WebSocket → Mirror/Netcode）。
3. 玩家端小助手**默认**下沉到本地 3B/4B 模型（llama.cpp），主机玩家额外接大模型 + 主机小助手——通道已就位，等接默认值。
4. 有模型时用模型给切片做摘要（现在切片索引是按场景/标题生成的，摘要可交给小模型离线生成后缓存）。
5. 房间状态持久化（存档）：目前剧情档案已落盘，房间与人物状态仍在内存里。
