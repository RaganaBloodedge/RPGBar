# 剧本编译 Agent · 能力清单（Skill Sheet）

> 2026-10-22 现状。一句话定位：**把一本没有结构的规则书/剧本，编译成"可查询的知识库 + 可推进的时间线"，
> 并且自己验证编译结果，不合格自己重切。**
> 18 个内核模块 / 6 个编排与适配模块 / 21 条命令，全部跑在《克苏鲁的呼唤 第七版》（418 页）上验证过。
> 编排层是 **LangGraph**（13 节点 / 2 条件边 / 1 自环 / 可断点续跑），对外接口是 **LangChain**
> （Document / TextSplitter / Retriever / 8 个 Tool）。内核不依赖框架，可单独测。

---

## 0. 全景

```
S0 读入 → S1 版式画像 → S2 粗分 → S2.5 闸门 → S3 细分 → S3.5 自检重切
      → S4 归类（规范驱动）→ S5 抽取（字段自动拓展）→ S6 建图/时间线
      → S7 索引 → S8 自检报告
```

| # | Skill | 干什么 | 实现 | 现状（COC 418 页） |
|---|---|---|---|---|
| 1 | **版式画像** | 摸清"这本书该怎么读" | `layout.py` | 置信 0.887，主策略 `font_cluster` |
| 2 | **非游戏内容闸门** | 版权/目录/译者名单不进库 | `gate.py` | 拒 6 节/20 页，擦 150 行，违规 0 |
| 3 | **原文层** | 逐页原样文本 + 块级回指 | `reader.py`/`pipeline.py` | 398 页 / 54.2 万字 |
| 4 | **章节树** | 目录/书签/标题 → 树 | `outline.py` | 170 节，带 path 出处 |
| 5 | **块切分** | 段落/条目/清单/表格行/术语表 五种粒度 | `outline.py` | 2181 块，均值 261 字 |
| 6 | **正文规范** | 五类的边界/证据/禁止/产物 | `spec.py` | 合规率 100% |
| 7 | **归类** | 候选逐条过规范，不合格留空 | `classify.py` | 未分类 15（0.7%） |
| 8 | **字段自动拓展** | 字段键值表由内容长出来 | `schema_skill.py` | 100 字段，审计流留痕 |
| 9 | **实体抽取** | 技能/物品/NPC/法术/规则/世界观 | `extract.py` | 884 实体 / 3350 事实 |
| 10 | **规则流程图** | 编号步骤 → 节点/分支边 | `extract.py` | **仅 3 条（短板）** |
| 11 | **时间线 / 进度** | 团游标 + 事件流 + 状态真相源 | `timeline.py` | 31 场景，可开局推进 |
| 12 | **混合检索** | BM25 + 向量 + 时间过滤 | `retrieve.py` | 2181 块向量，512 维 |
| 13 | **自检重切** | 五类缺陷 → 自动修 → 再断言 | `selfcheck.py` | 4 轮收敛，残留 0.18% |
| 14 | **可翻阅视图** | 块 + 原文层对照 | `browse.py` | 单页 HTML |
| 15 | **编译报告** | 每个结论带证据 | `report_html.py` | 9 个板块 |
| 16 | **库运维** | 表地图 / 看表 / 只读 SQL | `cli.py` | 18 条命令 |
| 17 | **图编排** ★ | 13 节点、条件边、自检自环、断点续跑 | `graph/` | 418 页 44s，逐字复现旧管线 |
| 18 | **生态适配** | Document / Splitter / Retriever / Tool | `lc/` | 8 个 Tool，可被外部 Agent 直接调用 |
| 19 | **自证** | 四条硬指标判定"这次编译算不算成功" | `graph.nodes._verdict` | 闸门0 / 自检过 / 合规100% / 未分类0.7% |

---

## 1. 读：看懂版式与原文

### 1.1 版式画像（`layout.py`）★

**干什么**：不预设"这本书是什么类型"，先测。

四步：证据（书签/目录页/字号直方图/缩进/表格网格/文字层质量）→ 假设（标题家族）→
打分（有书签用书签算召回，没书签用自洽性）→ 定策（主策略 + 后备链）。

**输出**：`layout_profile.json`（含每家族得分、页眉页脚带、印刷页码偏移、告警）。

**依据**：`pdf_probe` 全部可测量；不猜。

```bash
python -m compiler.cli profile --pdf 新书.pdf
```

### 1.2 原文层（`reader.reading_text` + `source_page` 表）

**干什么**：保存"原样"，与结构化结果分离。

- 逐页、阅读顺序（先左栏后右栏）、表格不摊平；
- **只存游戏内容页**（398/418）；
- 块用 `src_page/src_start/src_end` 指回原文，可 `substr` 精确回溯。

```bash
python -m compiler.cli raw --page 260
python -m compiler.cli raw --grep 水库
python -m compiler.cli export --out 全文.md
```

### 1.3 表格识别（`reader.detect_tables`）

自研词坐标聚类（`find_tables` 整本遍历会段错误）：

- 列位置**由数据行统计**得出（≥55% 行支持），不信表头；
- 表头被合并单元格污染时，按"词 → 最近列"重新贴；
- 表头像句子的列整列丢弃（防旁边栏正文串进来）。

---

## 2. 切：把书切成可检索的单元

### 2.1 章节树（`outline.build_sections`）

三种来源按可信度：**书签 > 目录页反解 > 版式标题**；目录页要加"印刷页码偏移"（本书 = 8）。

### 2.2 五种切分粒度（`outline.split_blocks`）

| 粒度 | 判据 | 产出 |
|---|---|---|
| 段落块 | 行距/缩进变化，≤520 字 | 正文块 |
| 条目块 | 单行短标题（技能/法术/怪物名） | 一条一块 |
| **清单行块** | 连续 ≥3 行 `名称，价格` | 一个商品一块（物价表 512 行） |
| **表格行块** | 表格行 ≥3 且是物品型表 | 一行一块（武器表一行一把枪） |
| 术语块 | 术语类章节里的 `术语：解释` | 一条术语一块 |

配套两个修复：**断行续接**（`（狗皮大`+`衣)，$37.50`）、**行内条目头再切**（`名称（NN%）`）。

### 2.3 自检重切（`selfcheck.py`）★

切完自己量：超长 / 碎片 / 断名 / 一块多条 / 句子被切断 → 自动修 → 再量，最多 4 轮。

```
超长 63→0   碎片 19→0   断名 0   块 2305→2181   最长 2165→702   残留 0.18%
```

---

## 3. 判：规范驱动的归类

### 3.1 正文规范（`spec.py`）★

五类各写死 **边界 / 证据 / 禁止 / 必须产出**。三类证据等价：关键词、版式签名、**章节归属**。
允许 `unclassified`（猜错比留空更糟）。

### 3.2 归类（`classify.py`）

`打分 → 候选排序 → 逐条 spec.validate() → 第一个通过的才算 → 全不过则 unclassified`。

### 3.3 LLM 契约

`spec.build_prompt()` 把规范全文 + 文本 + JSON Schema 一起给模型；
`spec.check_llm_verdict()` 验收：类别合法、**evidence 必须逐字可查**、通过边界检查，否则降级。

```bash
python -m compiler.cli spec --kb data/kb/coc7e --prompt-for 260
```

### 3.4 闸门（`gate.py`）★

三层：结构层（整节拒）→ 页级（版权页/名单页/链接页）→ 行级擦除（署名行/译者注）。
被拒的只留"页码 + 理由 + 指纹"，正文不入库；事后断言 `violations` 必须为 0。

---

## 4. 抽：从原文长数据

### 4.1 字段自动拓展（`schema_skill.py`）★

三层：`entity / fact / field`。字段是读出来的：`observe(label, value)` → 归一化 → 同义合并 →
按值推类型（dice/num/money/percent/enum/ref）→ 记原文证据 → 写审计流。
`refine_kinds()` 在编译末尾按值的分布校正类型。

### 4.2 六个抽取器（`extract.py`）

| 抽取器 | 锚点 | 产出 |
|---|---|---|
| 技能 | `名称（NN%）` | 87 个技能（基础值/描述/对抗技能） |
| 属性 | `力量（STR）`、`HP：14` | 8 个基础属性 + 派生值 |
| NPC | `属性/平均/掷骰` + 标签行 | 13 个 monster（**覆盖率是短板**） |
| 物品 | 表格表头 = 字段名 | 370 个 item |
| 法术 | 短行 + `消耗：/施法用时：` | 20 个法术 |
| 世界观 | 主题块 | 215 个 lore（摘要+原文+补充原文） |

### 4.3 规则流程图

编号步骤 → 节点，`如果…则…` → 判定分支，`见第X页` → 引用边。
**当前只抽出 3 条**：块切小之后编号步骤被拆到不同块，需要"同一条目的步骤跨块聚合"。

---

## 5. 建：图、索引、时间线

### 5.1 关系图
- 章节间交叉引用 29 条（`见第143页` → 目标章节）
- 技能/物品/规则之间的语义关系尚未建（扩展点）

### 5.2 混合检索（`retrieve.py`）
BM25（jieba 预切词 + FTS5）+ 向量（bge-small-zh 512 维，可缺省）+ 标题命中加权；
**时间过滤** `year=` 按 `valid_from/valid_to` 裁剪。

### 5.3 时间线（`timeline.py`）
`campaign`（游标）+ `timeline_event`（只追加）+ `entity_state`（真相源，死者留墓碑）。

```bash
python -m compiler.cli story --kb data/kb/coc7e            # 31 个场景节点
python -m compiler.cli timeline --kb data/kb/coc7e --start 试玩 --advance
```

---

## 6. 查：给 DM 的 7 个工具（运行期）

| 工具 | 路径 | 用途 |
|---|---|---|
| `kb_search(q, kind?, year?)` | BM25+向量+时间过滤 | 玩家用自然语言描述时 |
| `kb_item(name)` | 名字索引 | 报得出物品名时 |
| `kb_skill(name)` | 名字索引 | 玩家声明要用技能时 |
| `kb_npc(name)` | 名字索引（只读） | 怪物卡 |
| `kb_rule(name)` + `flow()` | 规则实体 + 流程图 | 判定流程 |
| `kb_state(campaign, entity)` | `entity_state` | **状态以这里为准** |
| `kb_progress(campaign)` | 游标 | 下一步可走哪些场景 |

所有工具**返回原文**，摘要只当钩子。

---

## 7. 运维命令（18 条）

```bash
# 编译
ingest --pdf 书.pdf --out data/kb/xxx        # 一条命令跑完 9 个阶段
profile --pdf 书.pdf                        # 只摸底

# 看结果
browse  --kb ...      # 可翻阅网页（块 + 原文层）
report  --kb ...      # HTML 编译报告（9 板块）
tables  --kb ...      # 表地图（行数/字段/用途）
show    --kb ... <表>  # 看某张表
sql     --kb ... "SELECT ..."               # 只读 SQL
lore    --kb ...      # 世界观按子类
story / timeline      # 世界线
raw / export          # 原文层
gate    --kb ...      # 拦截台账 + 自查
spec    --kb ...      # 正文规范 / LLM 提示词
demo    --kb ...      # DM 典型提问跑一圈（7 个）
card / flow           # 单实体卡 / 规则流程
```

---

## 8. 已知短板（照实写）

| 短板 | 影响 | 修法 |
|---|---|---|
| **NPC 覆盖不足**（13/60+） | 怪物卡不全 | 单栏属性块需要第二种锚点；或 LLM 复核 |
| **流程图只 3 条** | 规则不能"照着走" | 同一条目的步骤跨块聚合后再抽 |
| 物品价格偶有错列 | 少量商品字段错位 | 表头被合并时按词贴列的二次校正 |
| 参考表（伤害加值表等）归类粗 | 块级别正确、子类不够细 | 给参考表单独子类 |
| 无训练/微调 | — | 不涉及 |

---

## 9. 接一本新书要改什么

| 换什么 | 改 | 不改 |
|---|---|---|
| 同类规则书 | 什么都不用改 | 全部 |
| 无书签的书 | `layout.FAMILIES` 加一个标题家族 | 切分/抽取/存储/检索 |
| 另一套系统（DND） | `spec` 的词汇与 `SYNONYMS` 补词 | 版式画像、三层存储、时间线 |
| 小说/模组 | 加"场景转移"识别 | 同上 |
| 扫描件 | 前置 OCR | 同上 |

**判据**：需要改存储或检索接口 = 设计错了；只需要加家族/词汇 = 设计对了。

---

## 10. 编排与对外接口：LangGraph + LangChain

### 10.1 三层分工（内核不依赖框架）

| 层 | 位置 | 依赖 | 职责 |
|---|---|---|---|
| 内核 | `compiler/*.py` | 无框架 | 版式、切分、闸门、规范、抽取 —— 确定性，可单测 |
| 编排 | `compiler/graph/` | LangGraph | 什么时候做什么、做不干净要不要回去重做 |
| 适配 | `compiler/lc/` | LangChain | 让外部 Agent 用标准对象调用 |

### 10.2 图（13 节点 / 2 条件边 / 1 自环）

```
read ──(源文件缺失)──────────────────────────────────────► END
  └─ profile ─ outline ─ gate ─ split ─ selfcheck ⟲ ─ classify ─┬─ llm_review ─┐
                                          （未收敛回自己）       │（仅当启用）    │
                                                                 └──────────────► store
                                        store → extract → graph → index → report → END
```

| 能力 | 怎么做 | 为什么 |
|---|---|---|
| **断点续跑** | `SqliteSaver` + `thread_id` | 长书编译被打断，下次从断点接着跑 |
| **人工闸门** | `interrupt_before=["store"]` | 切完、归类完停下，人审过块再入库 |
| **自检自环** | `route_selfcheck` 条件边 | 每轮进检查点，第 3 轮崩了不用从头切 |
| **省钱的 LLM** | `route_llm` 只看 unclassified + 低置信 | 2200 块里通常只有几十块需要模型 |
| **可复现** | 状态只放可序列化事实，重资产按路径重建 | 检查点不膨胀、不炸序列化 |

### 10.3 LangChain 四件套

| 组件 | 类 | 说明 |
|---|---|---|
| 加载 | `load_documents(path)` | 一页一个 `Document`，metadata 带页码 |
| 切分 | `SpecAwareSplitter` | `TextSplitter` 子类；有版式走版式，无版式走四级降级 |
| 检索 | `KBRetriever` | `BaseRetriever` 子类；BM25 + 向量 + **世界时间过滤** |
| 工具 | `build_tools(kb_path=...)` | 8 个 `StructuredTool` |

```python
from compiler.lc import build_tools, KBRetriever, SpecAwareSplitter
tools = build_tools(kb_path="data/kb/coc7e-graph/kb.sqlite")
tools[1].invoke({"query": "被吓到要做什么检定", "k": 3})     # kb_search
KBRetriever(kb_path="...", k=5, kinds=["rule"], year=1925)    # 带时间过滤的检索
```

### 10.4 改造有没有改变行为：逐块比对

`scripts/check_graph_parity.py` 拿两条管线在同一本书上的产物逐表逐块比：

```
section 170=170 · block 2181=2181 · entity 884=884 · field 100=100 · fact 3350=3350
source_page 398=398 · non_game_log 191=191 · flow 2=2 · edge 29=29
块 id 集合相同（2181 个）· 每块正文前 80 字全一致
```

**结论：这是纯编排改造，业务判断一处没动。** 改内核之后跑这个脚本，就能立刻发现行为漂移。

### 10.5 命令增量

```bash
$PY -m compiler.cli graph   --pdf 书.pdf --out data/kb/x --thread my-run   # 图式编译（推荐）
$PY -m compiler.cli resume  --out data/kb/x --thread my-run                # 断点续跑
$PY -m compiler.cli mermaid                                                # 图结构
$PY scripts/check_graph_parity.py --a 旧库 --b 新库                        # 一致性检查
```

`ingest`（旧序贯管线）保留 —— 它不是废弃代码，是一致性检查的基准。
