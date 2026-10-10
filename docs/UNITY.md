# Unity 客户端路线（RPGBar-Unity · 分支 UnityRoute）

本目录是 Web demo（`main` 分支）的**Unity 分支副本**：Python 服务端、剧本、测试、文档原样继承，
新增的是 `Assets/` 下的 Unity 工程。往后所有开发都在 `UnityRoute` 分支上做，Web 版 `main` 保持可用。

## 一、为什么要单开一条线

Web demo 已经把玩法跑通：三个 Agent 槽位（DM / 主机小助手 / 玩家小助手）、RAG 约束剧本、
剧情状态机、服务端权威骰子、NPC 自动人设与反应权重、剧情档案与混合检索。
Python 侧 266 项测试 + 前端 74 项测试全绿。

但最终目标是**上 Steam 的桌面游戏**。浏览器给不了这些：

| 需要的能力 | 浏览器 | Unity |
| --- | --- | --- |
| 原生窗口 / 全屏 / 分辨率 | ✗ | ✓ |
| 资源打包与增量更新 | ✗ | ✓ |
| 手柄、音频、本地存档 | 受限 | ✓ |
| Steamworks（成就 / 云存档 / 好友组队 / 创意工坊） | ✗ | ✓ |
| 打包成单个可执行文件分发 | ✗ | ✓ |

所以从 `main` 拉出 `UnityRoute`，在这条线上把壳换掉。

## 二、架构决策：这一步只换壳，不动脑子

```
┌────────────────────────────┐         ┌──────────────────────────────────┐
│   Unity 客户端（C#）        │         │   Python 服务端（server/）        │
│                            │   ws    │                                  │
│  GameBootstrap  装配        │ ◄─────► │  FastAPI + WebSocket 房间         │
│  GameClient     WebSocket   │  join   │  ├─ NarratorAgent（主机 DM）      │
│  GameUI         运行时 UGUI │  action │  ├─ AssistantAgent（主机小助手）  │
│  MiniJson       零依赖解析   │  suggest│  ├─ AdvisorAgent（玩家小助手）    │
│                            │  roll   │  ├─ NPC 子系统 + 剧情档案         │
│  （同一套协议，服务端不区分  │         │  ├─ RAG 检索 + 状态机 + 骰子      │
│    浏览器还是 Unity）        │         │  └─ 三个模型槽位                  │
└────────────────────────────┘         └──────────────────────────────────┘
```

**关键取舍：服务端继续用 Python，不在这条线上重写。**

理由：服务端逻辑（RAG 切片检索、剧情状态机、Agent 工具循环、骰子、NPC 记忆、档案向量化）
已经成熟，且有 266 项测试守着。一次性移植成 C# 属于「高风险、零新功能」的工程，
现在做会拖慢「先让它能在 Unity 里跑起来」这个目标。

**代价**：分发的时侯要把 Python 运行时一起带上（PyInstaller `--onedir` 产物，本仓库已有方案）。
这层在后续路线里解决（见第五节）。

## 三、目录布局

Unity 工程与 Python 服务端**并排**放在项目根目录：

```
RPGBar-Unity/
├── Assets/                     ← Unity 资源（脚本 / 场景）
│   ├── Editor/
│   │   └── RPGBarSceneBuilder.cs   Editor 脚本：用菜单或命令行生成主场景
│   ├── Scripts/
│   │   ├── GameBootstrap.cs        入口：装配网络层与界面层
│   │   ├── GameClient.cs           WebSocket 客户端
│   │   ├── GameUI.cs               运行时构建的 UGUI 界面
│   │   └── MiniJson.cs             极简 JSON 解析/序列化（零依赖）
│   └── Scenes/                 ← 主场景（由 Editor 脚本生成）
├── Packages/                   ← Unity 包清单
├── ProjectSettings/            ← Unity 项目设置
├── server/                     ← Python 服务端（原样继承）
├── web/                        ← Web 客户端（保留作参考与降级）
├── scripts/                    ← 测试与工具
└── docs/
```

## 四、怎么跑

1. 启动服务端（任选其一）：
   ```bash
   python -m server.main          # 或双击 start.bat
   ```
2. 用 Unity Hub 打开 `E:\Workspace\RPGBar-Unity`（Unity 6000.4.2f1）。
3. 打开场景 `Assets/Scenes/Main.unity`。
   - 若场景还没生成，用菜单 **RPGBar → 生成主场景**；
   - **即使没有场景也能跑**：`GameBootstrap` 带自动引导，任意场景按 Play 都会自建根对象。
4. 点 Play，在连接面板填服务器地址 `ws://127.0.0.1:8000/ws` 和名字；
   房间号留空 = 新建房间，填房号 = 加入别人的房间。

## 五、脚本分工

| 文件 | 职责 |
| --- | --- |
| `MiniJson.cs` | JSON 的解析与序列化。**为什么不用 Newtonsoft**：本机 `packages.unity.com` 不可达，装不上 UPM 包；**为什么不用 JsonUtility**：服务端消息是动态字段结构，强类型 DTO 会随服务端演进不断返工。 |
| `GameClient.cs` | WebSocket 连接、收发、消息派发。收包在后台线程只入队，**分发统一在 `Update()` 主线程做**，避免后台线程碰 Unity API。发送用信号量串行化。 |
| `GameUI.cs` | 全部界面用代码搭（UGUI），不依赖任何 prefab / 美术资源。中文字体走 `Font.CreateDynamicFontFromOSFont`（Unity 内置字体不含中文字形）。 |
| `GameBootstrap.cs` | 把两者接起来：界面事件 → 发消息，网络消息 → 刷界面。含 `RuntimeInitializeOnLoadMethod` 自动引导。 |
| `Editor/RPGBarSceneBuilder.cs` | 用 Editor API 生成主场景，免手写易错的 `.unity`（YAML）。 |

## 六、与 Web 版的一致性约定

- **协议零改动**：Unity 与浏览器走同一套 WebSocket 消息（`join` / `action` / `suggest` / `roll` / `configure`），
  服务端不需要知道对面是什么客户端。
- **服务端仍是唯一权威**：骰子、状态机、剧本门控都在服务端，客户端只负责呈现与输入。
- **零第三方依赖**：不引入任何需要联网下载的包，保证离线也能构建。

## 七、后续路线

1. **当前**：Unity 客户端能连上服务端，完成加入房间 / 看旁白 / 发行动 / 掷骰 / 看建议的闭环。
2. **界面正式化**：把代码搭建的临时 UGUI 换成 UI Toolkit（UXML/USS），做跑团风格的正式界面
   （旁白分栏、角色卡、骰子动画、房间大厅）。
3. **分发自包含**：把 PyInstaller 打好的服务端随 Unity 构建一起发布，Unity 启动时拉起子进程并管理生命周期，
   玩家不需要单独装 Python。
4. **服务端下沉 C#**：把 `server/` 的逻辑逐步移植（WebSocket → Mirror/Netcode，服务端 = 主机端），
   最终统一为单一运行时。
5. **内置推理引擎**：小助手（persona / react_weights / action_advice / summarize）默认走内置引擎 + 本地小模型，
   DM 保持云端大模型。
6. **Steamworks 集成**：成就、云存档、好友组队（用 Steam 的 lobby 替代当前的房间码）。
