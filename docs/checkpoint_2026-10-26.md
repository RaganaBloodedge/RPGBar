# RPGBar 项目 Checkpoint

更新时间：2026-10-26（Asia/Shanghai）

## 当前目标

RPGBar 是一个本地部署 Agent 作为 DM 的跑团游戏。当前重点是：

- 支持玩家动态选择剧本，不默认加载某个 Demo；
- 让本地模型负责理解玩家行为和生成叙事；
- 让服务端负责规则、状态、骰子、权限和事实；
- 支持时序剧情记忆；
- 在自由度和剧本边界之间取得平衡；
- 使用真实本地模型持续实验，而不是只依赖离线单元测试。

## 已完成的架构

当前运行链为：

```text
玩家发言
  ↓
ActionJudgeAgent：理解玩家意图
  ↓
服务端规则与状态层：验证出口、检定、flag、实体和骰子
  ↓
NarratorAgent：根据已确认事实生成旁白
  ↓
风险触发式 NarrationVerifierAgent：审查高风险旁白
  ↓
广播事件并写入 Chronicle
```

当前不使用 LangChain 或 LangGraph。项目已有自定义 Agent 工具循环、统一 LLMProvider、结构化 JSON 校验和确定性状态层，足以支撑当前需求。

## 已完成的主要改动

### 动态剧本

- 服务端不再默认加载蛇王墓或其他 Demo 剧本。
- 建房时必须选择剧本，或显式提供剧本内容。
- 剧本切换不会写死到运行逻辑中。

### 本地模型和 GPU

- 支持 llama.cpp 本地模型。
- DM 使用 Qwen3-4B GPU 配置。
- 助手可以使用 Qwen3-1.7B CPU 配置。
- 服务端日志会报告 llama.cpp GPU 后端配置。

### ActionJudgeAgent

新增 `server/agents/action_judge.py`，负责输出结构化行动意图：

```text
fact_question | normal_action | check | move | blocked | meta | ambiguous
```

它不能直接修改状态。服务端只接受当前剧本中真实存在的检定和出口。

### NarrationVerifierAgent

新增 `server/agents/narration_verifier.py`，只在以下高风险回合触发：

- 检定；
- 场景移动；
- 越界行动；
- 玩家宣布获得物品；
- 玩家要求跳过剧情；
- 服务端已经产生权威事件。

Verifier 不能修改状态、重新掷骰或推进剧情，只能接受或拒绝模型新增旁白。

### 事实和旁白边界

- 检索内容现在被标记为“内部记忆”，要求模型理解后回答，不直接复述。
- 服务端已确认的检定、移动和 flag 结果被标记为权威事实。
- 失败检定后模型追加的成功暗示会被丢弃。
- 模型不能通过旁白直接获得物品、打开门或进入场景。
- 已增加按句去重，减少模型改写权威旁白造成的重复。

### 时序 RAG

`server/chronicle.py` 当前提供：

- JSONL 追加存储；
- `turn / scene / kind / actors / ts` 元数据；
- BM25 + 向量 RRF 检索；
- `after_turn`、`before_turn`、`scene`、`kinds` 时序过滤；
- actor 过滤与时间/场景过滤的正确组合；
- `recall_history` 工具支持这些过滤参数。

`WorldState` 负责“当前是什么状态”，`Chronicle` 负责“过去发生过什么”。

## 测试工具

### 玩家验收器

文件：`scripts/player_acceptance.py`

功能：

- 通过真实 WebSocket 连接游戏服务；
- 监听 `welcome`、`room`、`thinking`、`narration`、`dice`、`state`、`system` 等事件；
- 只有收到上一轮 `thinking=false` 后才发送下一轮行动；
- 保存每条用例的完整事件流；
- 统计检定、移动、拒绝、重复旁白和内幕泄露；
- 测试结束后保存 World SQLite、Chronicle JSONL 和事件快照。

### 评估 Agent

文件：`scripts/evaluate_acceptance.py`

它读取完整事件流，先分析确定性事实，再评价：

- 玩家行为是否被合理理解；
- 是否过度限制自由度；
- 是否错误推进剧情；
- 是否产生未确认事实；
- 旁白是否泄露内幕或重复。

评估 Agent 不能替代服务端事实检查，只能作为语义质量辅助。

## 最近一次真实实验

使用 Qwen3-4B GPU，在同一房间连续运行 25 条玩家行为，包含多条意外行为：

- 挖墙离开；
- 宣布已经拿到圣杯并跳过战斗；
- 直接移动到不存在的最终房间；
- 凭空打开暗门；
- 攻击不在场的 NPC。

第一次结果：

```text
17/25 通过
```

后续分析发现主要问题是测试路径预期错误：例如前一轮已经进入密室，下一轮测试仍然要求进入内院。这些失败不能直接算作 DM 失败。

测试数据已调整为按实际场景推进，但调整后的 25 轮尚未重新完成最终验收。

当前已有报告文件：

- `data/acceptance_25_report.json`
- `data/acceptance_25_results.json`
- `data/acceptance_25_evaluation.json`
- `data/acceptance_25_snapshot/`

## 当前已知问题

### 1. 测试集需要状态感知

每条用例不能只描述玩家想做什么，还要根据上一轮的实际 `WorldState` 决定下一轮是否合法。测试集需要区分：

- 玩家预期行为；
- 当前状态前置条件；
- 允许的状态变化；
- 禁止的状态变化。

### 2. 评估 Agent 仍然偏宽松

之前评估 Agent 曾将 25 条全部判为 pass。现在已经要求它优先读取骰子、移动、flag 和 state 事件，但仍需要用人工标注的失败样例校准。

### 3. `_OOB_VERBS` 仍存在

`NarratorAgent` 中的越界词表只能作为模型不可用时的最后兜底，不能成为通用剧本规则。后续应继续让 ActionJudge 根据当前剧本场景、实体、出口和检定判断行动边界。

### 4. NarrationVerifier 的性能

高风险回合会额外调用一次模型。需要继续优化触发条件，只在确定性检查无法判断或事实风险较高时调用。

### 5. 开局旁白和第一条行动的事件边界

测试事件流可能将开局旁白和第一条玩家行动的回应同时记录。分析时需要根据测试状态机区分开局事件和行动事件，避免把正常开场广播判为重复旁白。

## 下一步计划

按以下顺序继续：

1. 用修正后的状态感知测试集重新跑完整 25 轮；
2. 检查每轮的 WorldState、Chronicle 和事件流是否一致；
3. 用人工挑选的失败样例校准评估 Agent；
4. 为测试集加入预期状态变化和禁止状态变化字段；
5. 继续削弱 `_OOB_VERBS` 的运行时影响；
6. 优化 NarrationVerifier 的风险触发条件；
7. 再进行一轮 25 轮真实模型实验，并比较通过率和误拒绝率。

## 重要提交

## 2026-10-27 增量进度

- 验收器支持 `precondition.scene/flags`，会依据上一轮真实 `WorldState` 选择可执行用例；不会把前置场景不符的用例直接算成 DM 失败。
- ActionJudge 输入新增切分场景中的 NPC、运行时实体、静态关系边和动态关系边，并返回 `confidence`。
- 新增 `scripts/relation_graph_check.py`，验证 `pc_relation` 静态关系读取、World 动态关系持久化和 `relation_changed` 事实事件。
- 新增 `data/acceptance_stateful_smoke.json`，状态感知烟测 2/2 通过。
- 事实问句专项测试通过；完整 25 轮新服务测试因长回合响应超时未形成新报告，旧报告仍为 19/25，不能作为本轮改动后的成绩。

最近提交：

```text
320f0b0  feat: make acceptance and action judging state aware
e1500a3  test: add stateful acceptance smoke cases
```

```text
854e9bf  feat: agentize runtime and add temporal chronicle retrieval
46c744b  test: report freedom and boundary acceptance metrics
4a60f0e  test: timeout stalled live acceptance sessions
62255ee  test: make live acceptance event driven and traceable
efa01a5  fix: advance live acceptance cases from completion events
938d1eb  fix: treat retrieved memory as internal and preserve failed checks
7c9ce02  fix: suppress model additions after failed dice
77c4a1d  fix: keep meta declarations from altering narrated facts
08f9763  feat: add risk verifier and offline acceptance evaluator
3c68f81  feat: persist test snapshots and calibrate evaluation facts
```

