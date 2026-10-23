"""开放模式落盘：**不预设表结构**，字段是"读到才长出来"的。

为什么不用固定表（attribute 表里写死 STR/CON/SIZ/HP/护甲…）：

- 规则书之间字段就不一样（COC 有 SAN，DND 有 AC，别的系统有"道行""灵力"）；
- 同一本书里字段也不一样（这只怪物有"护甲"，那只没有；这把武器有"故障值"，那张表没有）；
- 一旦写死，遇到新字段只有两条路：改表 + 迁移，或者塞进一个 JSON 字段假装结构化。
  前者每接一本书就动一次数据库，后者等于没结构化。

所以用**三层**：

```
entity  实体     "拜亚基" 是一只 npc，"考古学" 是一个 skill
fact    事实     (实体, 字段, 值) —— 值永远带原文引用与页码
field   字段字典  "护甲" 这个字段是什么意思、什么类型、什么单位、第一次在哪出现
```

`field` 是这一层的核心：**它是 Agent 的产出，不是开发者的设计**。Agent 每读到一个
新字段（表格表头、"标签：值"行、正文里的"消耗：5 点POW"），就调 schema_skill 注册一条，
带证据（哪个块、哪一页、原文哪一句）。同一含义的不同写法（"价格"/"花费"/"售价"）会被
合并成同一个 key 的不同别名，而不是三列。

这样"角色卡长什么样"这个问题就不用预先回答：**卡 = 该实体在某 scope 下的全部 fact**，
按字段首次出现的顺序排。新书多一个"灵力"字段，角色卡上就自动多一行，一行代码都不用改。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS book (
  id TEXT PRIMARY KEY,
  title TEXT,
  doc_kind TEXT,
  source TEXT,
  sha256 TEXT,
  pages INTEGER,
  lang TEXT,
  compiled_at TEXT,
  compiler_version TEXT
);

-- 粗分：章节树（目录 / 书签 / 版式标题）
CREATE TABLE IF NOT EXISTS section (
  id TEXT PRIMARY KEY,
  book_id TEXT,
  parent_id TEXT,
  level INTEGER,
  title TEXT,
  page_start INTEGER,
  page_end INTEGER,
  ord INTEGER,
  source TEXT,
  path TEXT
);
CREATE INDEX IF NOT EXISTS idx_section_page ON section(book_id, page_start);

-- 细分：块（检索单位）。text 永远是原文
CREATE TABLE IF NOT EXISTS block (
  id TEXT PRIMARY KEY,
  book_id TEXT,
  section_id TEXT,
  ord INTEGER,
  page INTEGER,
  page_end INTEGER,
  kind TEXT,
  subtype TEXT,
  title TEXT,
  text TEXT,
  summary TEXT,
  char_len INTEGER,
  valid_from INTEGER,
  valid_to INTEGER,
  confidence REAL,
  meta TEXT,
  -- 与 kind 正交的一层：0 = 示例/演示/跑团实录/作者注（可以召回，但不该当正典引用）。
  -- 单列出来是为了检索能直接 `WHERE canonical=1`，不必解析 meta JSON。
  canonical INTEGER DEFAULT 1,
  -- 原文回指：这块在 source_page.text（阅读顺序的逐页原文）里的字符区间
  src_page INTEGER,
  src_start INTEGER,
  src_end INTEGER
);
CREATE INDEX IF NOT EXISTS idx_block_canonical ON block(book_id, canonical);

-- ★ 原文层：逐页原样文本（未加工、未抽字段），只放"游戏内容"页。
-- 作用：① 便于人直接查看/校对；② 结构化产物出错时能回原文对照；
-- ③ 抽取证据（OCR/切歪）能被追溯。**非游戏内容不写这张表**。
CREATE TABLE IF NOT EXISTS source_page (
  book_id TEXT,
  page INTEGER,
  chars INTEGER,
  text TEXT,
  PRIMARY KEY (book_id, page)
);

-- 非游戏内容的审计记录：只留页码/理由/指纹，**不留正文**。
CREATE TABLE IF NOT EXISTS non_game_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id TEXT,
  scope TEXT,          -- page / section / block
  page INTEGER,
  title TEXT,
  code TEXT,           -- copyright / credits / toc / front_matter / url / blank
  detail TEXT,
  chars INTEGER,
  fingerprint TEXT,
  at TEXT
);
CREATE INDEX IF NOT EXISTS idx_block_kind ON block(book_id, kind, subtype);
CREATE INDEX IF NOT EXISTS idx_block_page ON block(book_id, page);
CREATE INDEX IF NOT EXISTS idx_block_section ON block(section_id);

-- 实体：开放类型（etype 也是长出来的，不是写死的枚举）
CREATE TABLE IF NOT EXISTS entity (
  id TEXT PRIMARY KEY,
  book_id TEXT,
  etype TEXT,
  subtype TEXT,           -- 实体自己的细类：lore=世界观/历史/地点…，item=武器/护甲…
  name TEXT,
  name_norm TEXT,
  aliases TEXT,          -- JSON 数组
  scope TEXT,            -- 字段作用域：item / npc / pc / skill / spell / rule / lore ...
  block_id TEXT,
  page INTEGER,
  summary TEXT,
  readonly INTEGER DEFAULT 1,
  confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_entity_name ON entity(book_id, name_norm);
CREATE INDEX IF NOT EXISTS idx_entity_type ON entity(book_id, etype);
CREATE INDEX IF NOT EXISTS idx_entity_subtype ON entity(book_id, etype, subtype);

-- ★ 字段字典：Agent 自动拓展出来的表结构
CREATE TABLE IF NOT EXISTS field (
  key TEXT PRIMARY KEY,        -- scope.归一化标签，例如 npc.护甲
  scope TEXT,
  label TEXT,                  -- 原始标签（首次出现的写法）
  kind TEXT,                   -- num / dice / percent / money / enum / text / ref / list
  unit TEXT,
  domain TEXT,                 -- 值域提示（枚举候选、范围）
  cardinality TEXT,            -- one / many
  aliases TEXT,                -- JSON 数组：同义写法
  origin TEXT,                 -- table_header / label_line / inline / llm
  first_page INTEGER,
  evidence_block TEXT,
  evidence_quote TEXT,         -- 原文那一句，字段的"出生证明"
  confidence REAL,
  status TEXT,                 -- accepted / proposed / merged
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_field_scope ON field(scope);

-- 事实：值 + 证据
CREATE TABLE IF NOT EXISTS fact (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_id TEXT,
  field_key TEXT,
  raw TEXT,                    -- 原文值
  num REAL,
  js TEXT,                     -- 复杂值（列表/区间/骰子结构）
  unit TEXT,
  quote TEXT,                  -- 原文句子
  block_id TEXT,
  page INTEGER,
  confidence REAL,
  UNIQUE(entity_id, field_key, raw, block_id)
);
CREATE INDEX IF NOT EXISTS idx_fact_entity ON fact(entity_id);
CREATE INDEX IF NOT EXISTS idx_fact_field ON fact(field_key);

-- 关系图（角色关系、技能→属性、物品→规则…）
CREATE TABLE IF NOT EXISTS edge (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  src TEXT,
  dst TEXT,
  rel TEXT,
  block_id TEXT,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src, rel);

-- 规则流程图（判定步骤 → 分支 → 结果）
CREATE TABLE IF NOT EXISTS flow (
  rule_id TEXT PRIMARY KEY,
  block_id TEXT,
  kind TEXT,           -- decision / sequence
  nodes TEXT,          -- JSON: [{id,type,text,page}]
  edges TEXT,          -- JSON: [{from,to,label}]
  confidence REAL
);

-- 时间线（剧情推进层，与知识分类正交）
CREATE TABLE IF NOT EXISTS campaign (
  id TEXT PRIMARY KEY,
  book_id TEXT,
  name TEXT,
  current_section TEXT,
  current_page INTEGER,
  world_time TEXT,
  cursor TEXT,          -- JSON：已触发事件 / 已解锁 flag
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS timeline_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id TEXT,
  seq INTEGER,
  kind TEXT,            -- scene / state / flag / roll / note
  title TEXT,
  body TEXT,
  section_id TEXT,
  block_id TEXT,
  page INTEGER,
  world_time TEXT,
  payload TEXT,
  irreversible INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tl_campaign ON timeline_event(campaign_id, seq);

-- 实体运行态（真相源；与书里的只读定义分开）
CREATE TABLE IF NOT EXISTS entity_state (
  campaign_id TEXT,
  entity_id TEXT,
  status TEXT,          -- alive / dead / gone / hostile / unknown
  fields TEXT,          -- JSON
  updated_seq INTEGER,
  PRIMARY KEY (campaign_id, entity_id)
);

-- 向量（可选）
CREATE TABLE IF NOT EXISTS block_vec (
  block_id TEXT PRIMARY KEY,
  dim INTEGER,
  vec BLOB
);

-- 字段拓展审计流（每次新增/合并都留痕，报告里能复盘"表是怎么长出来的"）
CREATE TABLE IF NOT EXISTS schema_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seq INTEGER,
  action TEXT,          -- new / alias / merge / kind_fix
  field_key TEXT,
  detail TEXT,
  page INTEGER,
  block_id TEXT,
  at TEXT
);

-- 全文索引（BM25）；中文先 jieba 切好再入，查询同样切
CREATE VIRTUAL TABLE IF NOT EXISTS block_fts USING fts5(
  block_id UNINDEXED,
  title,
  text_seg,
  summary,
  tokenize='unicode61'
);

-- ================================================================
-- 五类容器（docs/知识库目标结构.md）
-- 上面那些表是"证据层 + 通用实体/事实"；下面这些是**五类各自的出口产物**：
-- 每一类有自己的成员单位、子结构与结构。运行期只查这些表，不再现拼。
-- ================================================================

-- ① 规则类：规则条目 → 子类树 → 判定步骤 → 流程图
CREATE TABLE IF NOT EXISTS rule_entry (
  id TEXT PRIMARY KEY,
  book_id TEXT,
  block_id TEXT,
  title TEXT,
  subtype_id TEXT,
  text TEXT,
  page INTEGER
);
CREATE TABLE IF NOT EXISTS rule_subtype (
  id TEXT PRIMARY KEY,
  book_id TEXT,
  name TEXT,
  parent_id TEXT,
  evidence TEXT,
  rules INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rule_step (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_id TEXT,
  seq INTEGER,
  kind TEXT,              -- step / branch / outcome
  text TEXT,
  quote TEXT,
  block_id TEXT
);
CREATE TABLE IF NOT EXISTS rule_flow (
  rule_id TEXT PRIMARY KEY,
  block_id TEXT,
  kind TEXT,              -- sequence / decision
  nodes TEXT,             -- JSON [{id,type,text}]
  edges TEXT,             -- JSON [{from,to,label}]
  confidence REAL
);

-- ② 玩家属性类：状态 / 技能 / 关系（状态与技能是"子集"，不是平行实体表）
CREATE TABLE IF NOT EXISTS pc_attribute (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id TEXT,
  block_id TEXT,
  key TEXT,
  label TEXT,
  value TEXT,
  unit TEXT,
  quote TEXT
);
CREATE TABLE IF NOT EXISTS pc_card_schema (
  book_id TEXT,
  field_key TEXT,
  label TEXT,
  kind TEXT,
  ord INTEGER,
  evidence_block TEXT,
  evidence_quote TEXT,
  PRIMARY KEY (book_id, field_key)
);
CREATE TABLE IF NOT EXISTS pc_skill (
  id TEXT PRIMARY KEY,
  book_id TEXT,
  block_id TEXT,
  name TEXT,
  base_value TEXT,
  description TEXT,
  page INTEGER
);
CREATE TABLE IF NOT EXISTS pc_relation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id TEXT,
  src TEXT,
  dst TEXT,
  rel TEXT,
  block_id TEXT,
  quote TEXT
);

-- ③ 工具类：物品是它的子集，以名字为索引（DM 按名直读）
CREATE TABLE IF NOT EXISTS item (
  id TEXT PRIMARY KEY,
  book_id TEXT,
  block_id TEXT,
  name TEXT,
  name_norm TEXT,
  aliases TEXT,
  summary TEXT,
  page INTEGER
);
CREATE INDEX IF NOT EXISTS idx_item_name ON item(book_id, name_norm);
CREATE TABLE IF NOT EXISTS item_field (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT,
  key TEXT,
  value TEXT,
  unit TEXT,
  quote TEXT
);

-- ④ NPC 类：怪物 / 人 两分，只读档案（运行态在 entity_state，不混这里）
CREATE TABLE IF NOT EXISTS npc (
  id TEXT PRIMARY KEY,
  book_id TEXT,
  block_id TEXT,
  etype TEXT,             -- monster / human
  name TEXT,
  aliases TEXT,
  summary TEXT,
  page INTEGER,
  readonly INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_npc_name ON npc(book_id, name);
CREATE TABLE IF NOT EXISTS npc_field (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  npc_id TEXT,
  key TEXT,
  value TEXT,
  dice TEXT,
  quote TEXT
);
CREATE TABLE IF NOT EXISTS npc_ability (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  npc_id TEXT,
  name TEXT,
  text TEXT,
  quote TEXT
);

-- ⑤ 常识与世界观：先摘要，再把语义相似的原文挂到同一摘要下
CREATE TABLE IF NOT EXISTS lore_topic (
  id TEXT PRIMARY KEY,
  book_id TEXT,
  summary TEXT,
  label TEXT,
  block_id TEXT,
  members INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lore_member (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id TEXT,
  block_id TEXT,
  similarity REAL,
  added_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_lore_member ON lore_member(topic_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    return con


# ---------------------------------------------------------------- 小工具

def j(d) -> str:
    return json.dumps(d, ensure_ascii=False)


def uj(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def set_meta(con: sqlite3.Connection, key: str, value) -> None:
    con.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, j(value) if not isinstance(value, str) else value),
    )


def get_meta(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    return row["value"]


def stats(con: sqlite3.Connection) -> dict:
    out = {}
    for t in ("section", "block", "entity", "field", "fact", "edge", "flow",
              "timeline_event", "source_page", "non_game_log"):
        out[t] = con.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
    out["by_kind"] = {
        r["kind"] or "?": r["c"]
        for r in con.execute("SELECT kind, COUNT(*) c FROM block GROUP BY kind ORDER BY c DESC")
    }
    out["by_etype"] = {
        r["etype"]: r["c"]
        for r in con.execute("SELECT etype, COUNT(*) c FROM entity GROUP BY etype ORDER BY c DESC")
    }
    out["by_scope"] = {
        r["scope"]: r["c"]
        for r in con.execute("SELECT scope, COUNT(*) c FROM field GROUP BY scope ORDER BY c DESC")
    }
    return out
