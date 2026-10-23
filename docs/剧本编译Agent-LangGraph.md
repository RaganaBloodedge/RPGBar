# 用 LangGraph 重构：把编译器变成通用剧本切割 Agent

> 目标不是"套一个框架"，而是让这套东西**能被别的 Agent 调用、能在任意剧本上跑、能在中断后续跑**。
> 本文记录改了什么、为什么这么改，以及验证"改造没有改变行为"的证据。

---

## 1. 先说清什么该进框架，什么不该

这是整个重构最重要的一次判断。

| 层 | 内容 | 有没有框架依赖 |
|---|---|---|
| **内核** `compiler/*.py` | reader / layout / outline / gate / spec / classify / extract / selfcheck / retrieve / timeline | **没有**。纯函数、确定性、可单测 |
| **编排** `compiler/graph/*.py` | state / nodes / build / llm_classify | LangGraph |
| **适配** `compiler/lc/*.py` | loader / splitter / retriever / tools | LangChain |

理由：

- 内核是**这个系统的真本事**（版式怎么摸、块怎么切、字段怎么长出来）。把它写进图的节点回调里，
  就再也没法单独测了，也没法被别的框架复用。
- 编排是**流程知识**（什么时候该回去重切、什么条件下才调模型）。这部分确实适合用图表达。
- 适配是**接口**。别人要用，只该看到 `Document` / `Retriever` / `Tool`，不该读我们的 dataclass。

所以这次重构的净结果是：内核**一行没改**，新增 6 个文件、约 1400 行编排与适配代码。

---

## 2. 图长什么样

```
read ──(源文件缺失)──────────────────────────────────────────► END
  └─ profile ─ outline ─ gate ─┬─ split ─ selfcheck ⟲ ─ classify ─┬─ llm_review ─┐
                               │      （未收敛就回自己）           │（仅当启用）    │
                               └──────────────────────────────────┴──────────────► store
                                            store → extract → graph → index → report → END
```

十三个节点，两个条件边，一个自环：

| 节点 | 职责 | 为什么单独成节点 |
|---|---|---|
| `read` | 读 PDF，记页数/书签/哈希/原始总页数 | 输入校验失败也要留 trace，不能靠异常 |
| `profile` | 版式画像（证据→假设→打分→定策） | 换文档类型只改这一层 |
| `outline` | 章节树 | 抽样跑要裁书签，单独一节点才好处理 |
| `gate` | 结构层 + 页级拦截 | 拦截台账要能独立审计 |
| `split` | 细分成块 + 块级擦除 | 切分是最贵的一步，独立才能只重跑它 |
| `selfcheck` | 一轮 audit+repair | **做成自环**，见 §3 |
| `classify` | 规范驱动归类 | 要注入章节上下文与续块继承 |
| `llm_review` | 模型复核（可选） | 默认不进这条路 |
| `store` | 落盘（book/section/source/block/台账） | 写库前的那道人工闸门就卡在这里 |
| `extract` | 实体、字段字典、事实 | 字段是先到先注册，必须一次跑完 |
| `graph` | 流程图、交叉引用、场景种子 | 依赖抽取结果 |
| `index` | FTS5 + 向量 | 可关闭（`--no-vec`） |
| `report` | 报告 + **自证** | 判定"这次编译算不算成功" |

---

## 3. 三个关键设计

### 3.1 状态里只放可序列化的事实

一本 418 页的书有十几万个 bbox span。把这些对象放进状态，检查点会膨胀到几十 MB，
LangGraph 的序列化也会直接失败。

所以状态里只有：

```python
book_meta   {'title','pages','total_pages','bookmarks','sha256'}
layout      LayoutProfile.to_json()
sections    [{id,title,page_start,page_end,path,chapter,...}]
blocks      [{id,section_id,page,title,text,kind,subtype,confidence,meta}]
block_tables{block_id: [表格转储]}     # 扁平化会丢 Table 对象，抽取阶段重建用
traces      [{node,ms,note,metrics}]  # reducer=operator.add，累计不覆盖
```

重资产按路径重建：

```python
@lru_cache(maxsize=2)
def _book_cached(path, max_pages): return reader.load(path, max_pages=max_pages)

_CONNS: dict[str, sqlite3.Connection] = {}   # 连接不可序列化，按 out_dir 单例
```

**代价**：续跑的前提是源文件还在原路径（检查点里存的是路径，不是内容）。
这是一个明确的取舍 —— 存内容会让检查点大到无法管理，而重读一次 418 页 PDF 只要 3.4 秒。

### 3.2 自检做成图上的自环，不是节点里的 for 循环

原来 `selfcheck.run()` 内部 `for round in range(4)`。现在拆成：节点跑**一轮**，
条件边 `route_selfcheck` 决定是否回到自己。

好处：每轮都进检查点 —— 长书编译在第 3 轮自检时被打断，下次从第 3 轮接着修，不用从头切。
坏处：多几次状态读写，相对切分本身可忽略。

实测这条边的行为：

```
[selfcheck] 第 1 轮：256 个缺陷 → 修 255 处
[selfcheck] 第 2 轮：7 个缺陷 → 修 3 处
[selfcheck] 第 3 轮：4 个缺陷 → 修 0 处
[selfcheck] 第 4 轮：4 个缺陷 → 修 0 处
[selfcheck] 收敛：4 轮，残留 4 条
```

第 3、4 轮修不动了（剩下的是"结尾为分号的引导句"，是排版结构不是错误），
到轮数上限后放行，但把 4 条残留记进 `open_defects` 让报告说明。

### 3.3 写库前是一道可中断的人工闸门

`build_graph(interrupt_before=["store"])` 让图在落库前停下并保存检查点。

实测：

```
中断后 下一节点: ('store',) | 块数: 141
已落 kb.sqlite 吗: False
续跑后 stage: report | 自证: True
已落 kb.sqlite 吗: True
```

这条能力的意义：切分与归类做完（此时最贵的一步已完成、结果可审），人可以翻过块再决定要不要入库。
不是靠"记得别跑最后一步"，而是图本身停在那里。

---

## 4. LangChain 适配层

| 组件 | 类/函数 | 说明 |
|---|---|---|
| 加载 | `load_documents(path)` | 一页一个 `Document`，metadata 带 `page/chars/doc_kind` |
| 切分 | `SpecAwareSplitter` | `TextSplitter` 子类。`split_documents()` 走版式路径，`split_text()` 走纯文本降级 |
| 检索 | `KBRetriever` | `BaseRetriever` 子类，BM25 + 向量 + **世界时间过滤** |
| 工具 | `build_tools(kb_path=...)` | 8 个 `StructuredTool` |

### 4.1 为什么切分器要自己写

`RecursiveCharacterTextSplitter` 无论怎么调 `chunk_size`/分隔符，都会在这两个地方出错：

```
│ 6.2 常规接触战斗                  ← 标题被当成正文首行
│ 战斗轮按 DEX 顺序进行。每轮…       ← 句子中间被切断
│ 男装
│ 精纺毛料礼服，$17.95              ← 20 个商品被粘成一块
```

因为**边界不在字符层面，而在版式层面**（行距、缩进、表格网格）。所以判据是版式与规范，
不是字符数；`max_chars` 只是兜底上限。

接口上仍满足 `TextSplitter` 契约，能塞进任何现成 pipeline；没有版式信息时降级为
"清单行 → 条目头 → 空行段落 → 句末"四级判定，比字符切分好，但不如版式路径准（这一点如实写在类文档里）。

### 4.2 八个工具

编译侧 `compile_script`；运行侧 `kb_search` / `kb_card` / `kb_rule_flow` / `kb_sections` /
`kb_state` / `kb_advance` / `kb_progress`。

两条克制的原则写在 `tools.py` 顶部：**返回原文不只返回摘要**（摘要会丢掉"若目标已受伤则…"这类条件）、
**工具不做推理**（推理藏进黑盒，出错无从复盘）。

```
kb_card("燧发手枪") → 伤害 1D6+1 / 射程 … / 价格 …（字段都是编译时从原文读出来的）
kb_search("被吓到要做什么检定") → p12 游戏范例（理智检定）、p97 6.4 其它战斗情况
```

---

## 5. LLM 节点：便宜且不许乱说

三个约束：

1. **只喂判不了的块**：`unclassified` 或置信度 < 0.5 的，2200 块里通常只有几十块，
   `llm_budget` 再设上限。
2. **规范与验收共用一份**：提示词来自 `spec.build_prompt()`，答案由 `spec.check_llm_verdict()` 验收。
   `evidence` 无法在原文里逐字找到 → 判废，不圆场。
3. **模型可替换**：只依赖 `BaseChatModel`，任何 OpenAI 兼容端点（含本地 vLLM / Ollama adapter）都能接。

```bash
# 声明模型：gpt-4o-mini@https://my-endpoint/v1|sk-xxx（也可只写模型名走 OPENAI_* 环境变量）
RPGBAR_LLM="qwen2.5-7b-instruct@http://localhost:8000/v1|none" \
  python -m compiler.cli graph --pdf 书.pdf --out data/kb/x --use-llm
```

**默认关闭**。没有模型时整条 LLM 分支在图上不会被走到，这本书在完全离线的机器上照样编译
—— 这是"通用 Agent"必须有的降级能力。

---

## 6. 验证：改造没有改变行为

这是最该看的一节。把 600 行顺序流程改成状态图，最容易出的错不是跑不起来，而是
「跑起来了但结果变了」。

`scripts/check_graph_parity.py` 逐表、逐块比对两条管线在同一本书上的产出：

```
指标                    旧序贯   LangGraph
section                  170         170  =
block                   2181        2181  =
entity                   884         884  =
field                    100         100  =
fact                    3350        3350  =
source_page              398         398  =
non_game_log             191         191  =
flow                       2           2  =
edge                      29          29  =
by_kind: = {lore: 624, npc: 408, pc: 330, rule: 376, tool: 428, unclassified: 15}
by_etype: = {attribute: 8, item: 370, lore: 215, monster: 13, npc: 2, rule: 169, skill: 87, spell: 20}
块 id：= 共 2181 个
正文：= 2181 块前 80 字全一致
```

**2181 个块的 id 与正文逐字相同。** 这证明重构是纯编排改造：业务判断一处没动。

### 6.1 耗时（418 页，单机，无 GPU）

| 节点 | 耗时 |
|---|---|
| read | 3.4 s |
| profile | 0.56 s |
| outline | 2 ms |
| gate | 2.5 s |
| split | 2.8 s |
| selfcheck（4 轮） | 2.4 s |
| classify | 0.17 s |
| store | 4.6 s |
| extract | 3.0 s |
| graph / report | 0.17 s |
| index（2181 条向量，bge-small-zh） | 11.7 s |
| **合计** | **约 44 s（含向量）；`--no-vec` 约 28 s** |

框架开销可以忽略 —— 图本身不慢，慢的是 PDF 解析与 NER 式的抽取。

---

## 7. 换一本书要改什么

按代价从低到高：

| 情况 | 要改的 | 不用改的 |
|---|---|---|
| 同类型规则书（D&D、克苏鲁的其他模组） | **什么都不用改**，直接跑 | 全部 |
| 另一种版式（单栏小说、双栏期刊、带侧栏的手册） | `layout.FAMILIES` 里加一个标题家族（约 5 行） | 切分、抽取、存储 |
| 另一种语言（英文为主） | 分词器换 `jieba` → 空白切分；`spec` 的关键词表换英文 | 图、状态、检查点 |
| 完全不同的内容体系（不是跑团书） | 改 `spec.py` 的五类定义与边界 | 图的拓扑、检查点、工具接口 |
| 扫描件（无文字层） | 需要先 OCR（当前会判 `scanned` 并明确告警，不硬跑） | — |

第 2、3 类是"通用"最常遇到的，代价都在 5–20 行以内 —— 因为版式画像本来就是**独立的一层**，
这也是当初把"摸清版式"单独立层的原因。

---

## 8. 命令

```bash
PY="C:/Users/18060/.workbuddy/binaries/python/envs/rpgbar/Scripts/python.exe"

# 图式编译（推荐）：逐节点打印进度，结束给自证结论
$PY -m compiler.cli graph --pdf 书.pdf --out data/kb/x --thread my-run

# 断点续跑（上次被打断、或人工审过块之后放行）
$PY -m compiler.cli resume --out data/kb/x --thread my-run

# 看图结构
$PY -m compiler.cli mermaid

# 一致性检查（改内核后必跑）
$PY scripts/check_graph_parity.py --a data/kb/coc7e/kb.sqlite --b data/kb/coc7e-graph/kb.sqlite

# 旧的顺序管线还在，用于对照（不是废弃代码，是基准）
$PY -m compiler.cli ingest --pdf 书.pdf --out data/kb/x
```

Python 里：

```python
from compiler.graph import run_compile, resume_compile, build_graph, make_checkpointer
from compiler.graph import build_graph

st = run_compile("书.pdf", "data/kb/x", options={"max_chars": 520, "use_llm": False})
st["verdict"]        # {'passed': True, 'reasons': [...], 'metrics': {...}}

# 人工闸门
g = build_graph(make_checkpointer("data/kb/x"), interrupt_before=["store"])
```

---

## 9. 已知不足（不粉饰）

| 问题 | 影响 | 方向 |
|---|---|---|
| 检查点存的是 PDF **路径**而非内容 | 源文件被移动/删除后无法续跑 | 加 `content_sha256` 校验，路径失效时明确报错而不是静默重读 |
| 检查点体积随块数×步数线性增长 | 2181 块的书约 **44 MB**（20 个快照，每份含全部块的正文） | 状态里只存 block_id + 摘要，正文从 `source_page` 按区间回取；或让 `split` 先把块落库、后续节点从库读。当前先用 `--memory-checkpoint` 关掉落盘 |
| `llm_review` 逐块调用 | 几十块 = 几十次请求 | 可合并为一次多样本调用（每块带编号），但会牺牲"证据必须逐字命中"的严格性 |
| 自检第 3、4 轮修不动 | 4 条残留（分号引导句，属正常排版） | 已识别为"不该修"，报告里说明，不计失败 |
| `KBRetriever` 未接向量库后端 | 不能直接用 Chroma/FAISS 的检索接口 | 我们自己的三路融合比通用向量库准，如需接入再加一个 adapter |
| 图里没有并行分支 | `gate` 与 `split` 串行 | `gate` 的页级判定可按页并行（LangGraph 的 `Send` API），当前 2.5s 不值得优化 |
