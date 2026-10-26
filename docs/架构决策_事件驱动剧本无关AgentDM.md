# 架构决策：事件驱动的剧本无关 Agent DM

本文件保存当前项目的核心设计方案，后续实现、测试、回溯和优化都以此为基准。

## 核心原则

```text
模型负责理解和表达
服务端负责事实和状态
剧本负责提供可用机制
事件负责连接所有模块
```

## 最终运行链

```text
玩家发言
  ↓
1. ActionJudgeAgent
  ↓
2. Deterministic Rule Validator
  ↓
3. Server Event Commit
  ↓
4. NarratorAgent
  ↓
5. Risk NarrationVerifier
  ↓
6. 广播 + Chronicle
```

## 1. ActionJudgeAgent：理解，不裁定事实

输入：

- 玩家原话
- 当前场景
- 当前实体
- 当前出口
- 当前检定
- 当前世界状态摘要

输出：

```json
{
  "intent": "check|move|normal_action|fact_question|blocked|meta|ambiguous",
  "target": "search_tapestry",
  "confidence": 0.88,
  "needs_clarification": false,
  "reason": "玩家正在调查挂毯"
}
```

它只回答“玩家想做什么”。它不负责掷骰、改 flag、移动场景、创造物品或判断最终成功与否。

## 2. Deterministic Rule Validator：唯一规则裁判

服务端验证：

- 当前场景是否存在目标；
- 出口是否存在；
- 前置 flag 是否满足；
- 检定是否存在；
- 实体是否在现场；
- 玩家是否有该能力；
- 行动是否可以修改状态。

它决定：允许普通行动、触发检定、允许移动、拒绝行动或要求澄清。

规则不能写成看到“开枪”就拒绝，而应判断当前场景是否有枪、当前剧本是否允许攻击、当前实体是否可攻击，以及是否存在对应检定或规则。只有规则层无法判断时，才调用模型辅助。

## 3. Event Commit：建立唯一事实源

所有真实变化都先生成标准事件：

```json
{
  "type": "check_resolved",
  "turn": 8,
  "scene_before": "hall",
  "scene_after": "hall",
  "actor": "玩家",
  "target": "search_tapestry",
  "success": true,
  "facts_added": ["found_secret_door"],
  "facts_removed": [],
  "source": "server"
}
```

然后由同一事件更新 WorldState、Chronicle、测试记录和 DM 上下文。

最终原则：

> 没有服务端事件，就没有游戏事实。

## 4. NarratorAgent：只根据已确认事实说话

Narrator 输入：当前 WorldState、本轮权威事件、当前剧本内容、必要的 Chronicle 记忆和玩家原话。

它不重新判断玩家是否成功、是否拿到物品、是否进入场景或检定是否成功，只负责解释结果、描写氛围、给出可感知细节和提供合理的下一步线索。

历史检索必须标记为内部资料，只用于理解，不得直接复述。

## 5. Risk NarrationVerifier：只审查高风险回合

不应该每轮都运行第三个模型。只在以下情况触发：

- 检定结果；
- 场景移动；
- 物品获得；
- 玩家宣布完成某事；
- 玩家要求跳过剧情；
- 产生新 NPC、地点或物品；
- 模型旁白与事件文本相似度异常；
- 失败结果后模型继续扩写。

Verifier 只判断旁白是否违反已确认事实，不能改状态、重投骰、改 flag 或重新裁定行动。普通回合使用确定性检查。

## 6. Chronicle：作为事件记忆，而不是旁白仓库

Chronicle 应优先存储事实事件：

```json
{
  "turn": 12,
  "type": "item_acquired",
  "facts": ["玩家获得铜钥匙"],
  "scene": "courtyard",
  "actor": "玩家"
}
```

旁白可以作为辅助文本保存，但不能成为当前事实的来源。

记忆查询分为：

```text
get_current_state()
recall_events()
recall_character_memory()
```

当前状态永远以 WorldState 为准。

## 7. 测试和评估体系

测试集必须和游戏共享同一套事件结构、WorldState、规则验证器和 Agent 输入输出格式，但测试运行使用独立数据库。

每条测试包含：

```json
{
  "input": "我打开暗门",
  "precondition": {
    "scene": "hall",
    "flags": ["found_secret_door"]
  },
  "expected": {
    "intent": "move",
    "scene_after": "secret_room"
  },
  "forbidden": {
    "facts_added": ["player_has_grail"]
  }
}
```

评估分为确定性评分和 Agent 语义评分。确定性评分检查状态、场景、flag、检定、非法事实和内幕泄露；语义评分检查意图理解、自由度、引导、旁白自然度和上下文一致性。评估 Agent 不能覆盖确定性失败。

## 最终推荐方案

```text
一个理解 Agent
一个叙事 Agent
一个风险触发式审查 Agent
一个确定性事实与规则核心
一个事件驱动记忆系统
一个状态感知测试和评估系统
```

不要继续扩展成多个互相竞争的 Agent。

## 最重要的工程取舍

### 自由度来自模型

玩家可以自由描述检查墙上灰尘、绕着雕像走一圈、询问 NPC 等行动，不需要为每句话预先写关键词。

### 限制来自状态和机制

系统只限制当前场景不存在的实体、当前剧本不存在的机制、不满足前置条件的移动和未经服务端确认的事实。

### 稳定性来自事件

所有状态变化先成为事件，之后才更新世界、写入记忆、生成旁白和交给测试评估。

该方案的目标是同时满足剧本通用性、玩家自由度、规则一致性、长期记忆、本地模型运行性能和可测试性。

