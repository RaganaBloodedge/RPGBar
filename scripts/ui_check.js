// 前端 DOM 级校验（可选）：用 jsdom 真跑 web/app.js，验证「模型设置」抽屉的完整交互。
//
// 为什么单独放在 Node 侧：Python 冒烟测试只能验协议与后端，验不了 DOM 渲染与事件绑定。
// 这里有三个 bug 就是它抓出来的（徽标首次渲染为空、切通道不换默认地址、房主身份变更后不重算权限）。
//
// 用法：
//   npm i jsdom                       # 只需一次（或用任意 node 工程里的 jsdom）
//   node scripts/ui_check.js          # 需 RPGBar 服务器已在 127.0.0.1:8000 运行
//   RPGBAR_BASE_URL=http://127.0.0.1:8010 node scripts/ui_check.js   # 测打包产物
//
// 它会在 127.0.0.1:8123 起一个假的 OpenAI 兼容服务，所以「测试连接」按钮
// 走的是**真实链路**：jsdom → 页面 fetch → FastAPI /api/models/test → 假 LLM。
const fs = require("fs");
const path = require("path");
const http = require("http");
const { JSDOM } = require("jsdom");

const WEB = path.resolve(__dirname, "../web");
// 默认打本机开发服务器；测打包产物时用 RPGBAR_BASE_URL=http://127.0.0.1:8010
const BASE = (process.env.RPGBAR_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const FAKE_LLM_PORT = 8123;

let PASS = 0;
let FAIL = 0;
function check(name, cond, extra) {
  if (cond) {
    PASS++;
    console.log("  [PASS] " + name);
  } else {
    FAIL++;
    console.log("  [FAIL] " + name + (extra ? "  " + extra : ""));
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- 假 OpenAI 兼容服务：给「测试连接」按钮一个真的能通的端点 ----
function startFakeLLM() {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        let hasTools = false;
        try {
          hasTools = !!(JSON.parse(body).tools || []).length;
        } catch (e) {}
        const msg = hasTools
          ? {
              role: "assistant",
              content: "",
              tool_calls: [
                { id: "call_ping", type: "function", function: { name: "ping", arguments: "{}" } },
              ],
            }
          : { role: "assistant", content: "就绪" };
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ choices: [{ message: msg }] }));
      });
    });
    srv.listen(FAKE_LLM_PORT, "127.0.0.1", () => resolve(srv));
  });
}

(async function main() {
  const fakeLLM = await startFakeLLM();

  const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
  const appjs = fs.readFileSync(path.join(WEB, "app.js"), "utf8");

  const sent = [];
  const jsErrors = [];

  class FakeWS {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      FakeWS.last = this;
      setTimeout(() => this.onopen && this.onopen(), 0);
    }
    send(s) {
      sent.push(JSON.parse(s));
    }
    close() {
      this.readyState = 3;
    }
    deliver(obj) {
      if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) });
    }
  }
  FakeWS.OPEN = 1;

  const dom = new JSDOM(html, { url: BASE + "/", runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  w.WebSocket = FakeWS;
  w.fetch = (url, opts) => fetch(url.startsWith("http") ? url : BASE + url, opts);
  w.addEventListener("error", (e) => jsErrors.push(String((e && e.error) || e.message || e)));
  w.onerror = (m) => jsErrors.push(String(m));

  console.log("== 前端 DOM 校验：模型设置抽屉 ==");
  w.eval(appjs);
  const $ = (id) => w.document.getElementById(id);
  const q = (sel) => w.document.querySelector(sel);

  check("app.js 执行无异常", jsErrors.length === 0, jsErrors.join(" | "));

  // ---- 联机地址（/api/net）：加入页与设置抽屉都该列出可分享地址 ----
  // 轮询等待 loadNet 的 fetch 回来（Node 首次 fetch 有连接池预热，固定 sleep 不可靠）
  for (let i = 0; i < 40 && w.document.querySelectorAll("#share-list .share-addr").length === 0; i++) {
    await sleep(50);
  }
  const shareBox = $("share-box");
  check("加入页出现「联机地址」折叠块", !!shareBox && !shareBox.classList.contains("hidden"));
  const shareItems = w.document.querySelectorAll("#share-list .share-addr");
  check("联机地址列表已填充", shareItems.length >= 1, String(shareItems.length));
  check(
    "地址条以 http:// 开头且带网卡名标签",
    shareItems.length > 0 && shareItems[0].textContent.indexOf("http://") === 0,
    shareItems.length ? shareItems[0].textContent : ""
  );
  check(
    "设置抽屉里也能看到联机地址",
    w.document.querySelectorAll("#share-list-drawer .share-addr").length >= 1,
    String(w.document.querySelectorAll("#share-list-drawer .share-addr").length)
  );

  // ---- 建房选剧本：加入页下拉由 /api/scripts 填充；填了房号则由房主决定（置灰）----
  for (let i = 0; i < 40 && (!$("join-script") || $("join-script").options.length === 0); i++) {
    await sleep(50);
  }
  const joinScript = $("join-script");
  check("加入页有「开局剧本」下拉", !!joinScript);
  check("下拉已被剧本列表填充", !!joinScript && joinScript.options.length >= 1, joinScript ? String(joinScript.options.length) : "无");
  const pickedScript = joinScript ? joinScript.value : "";
  $("join-room").value = "ABC12";
  $("join-room").dispatchEvent(new w.Event("input", { bubbles: true }));
  check("填了房号后剧本下拉置灰（由房主决定）", !!joinScript && joinScript.disabled === true);
  $("join-room").value = "";
  $("join-room").dispatchEvent(new w.Event("input", { bubbles: true }));
  check("清空房号后剧本下拉恢复可选", !!joinScript && joinScript.disabled === false);

  // ---- 模拟进入房间 ----
  $("join-name").value = "亚瑟";
  $("join-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await sleep(30); // 等 onopen 回调
  const ws = FakeWS.last;
  check("提交加入后建立了 WebSocket", !!ws, "无 ws");
  check("onopen 后发出 join 消息", sent.some((m) => m.type === "join" && m.name === "亚瑟"), JSON.stringify(sent));
  check(
    "建房时 join 消息带上所选剧本",
    !!pickedScript && sent.some((m) => m.type === "join" && m.script === pickedScript),
    JSON.stringify(sent)
  );

  ws.deliver({
    type: "welcome",
    room: "ABC12",
    you: "亚瑟",
    late: false,
    script: "古堡秘宝",
    script_info: {
      title: "古堡秘宝",
      path: "sample_script.json",
      fmt: "json",
      structured: true,
      scenes: 5,
      flags: 5,
      chunks: 6,
      warnings: [],
    },
    chunks: [
      { id: "meta", title: "剧本设定", kind: "meta", chars: 120 },
      { id: "gate", title: "古堡大门", kind: "scene", chars: 220 },
    ],
    npcs: [
      { id: "npc01", name: "老周", role: "古堡看守", aliases: ["周老头"], from_script: true },
    ],
    memory: { entries: 7, actors: 2, has_summary: true, embedding: true },
    prompt: { advisor: "（小助手公开版 prompt）", dm: "（DM 完整版 prompt）" },
    agents: {
      owner: "亚瑟",
      dm: { kind: "off", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat", has_api_key: false, ready: false, mode: "scripted" },
      assistant: { kind: "off", base_url: "http://127.0.0.1:8081/v1", model: "qwen2.5-3b-instruct", has_api_key: false, ready: false, mode: "scripted" },
      advisor: { kind: "off", base_url: "http://127.0.0.1:8080/v1", model: "local-model", has_api_key: false, ready: false, mode: "scripted" },
    },
  });
  check("welcome 后顶栏显示房间码", $("room-code").textContent === "ABC12", $("room-code").textContent);
  check("welcome 后品牌名跟随剧本", (q(".brand-name") || {}).textContent === "RPGBar · 古堡秘宝", (q(".brand-name") || {}).textContent);
  check("模型状态胶囊显示脚本化", $("model-pill").textContent === "模型：脚本化", $("model-pill").textContent);
  // 「本房间」同步块：剧本 + 切片 + 两份 prompt（房主能看到 DM 那份）
  const syncEl = $("room-sync");
  check("welcome 后同步块显示剧本名", !!syncEl && (syncEl.textContent || "").indexOf("古堡秘宝") >= 0, syncEl ? syncEl.textContent : "无");
  check("同步块列出切片", w.document.querySelectorAll("#room-sync .room-sync-chunks .script-slot").length === 2,
    String(w.document.querySelectorAll("#room-sync .room-sync-chunks .script-slot").length));
  check("房主能看到两份 prompt", w.document.querySelectorAll("#room-sync .prompt-block").length === 2,
    String(w.document.querySelectorAll("#room-sync .prompt-block").length));

  // ---- 在场人物：welcome 里的 npcs 应渲染到侧栏（人物卡由主机小助手生成，连入即同步）----
  const npcItems = w.document.querySelectorAll("#npcs li");
  check("侧栏渲染出在场人物", npcItems.length === 1, String(npcItems.length));
  check("NPC 条目显示名字", npcItems.length > 0 && npcItems[0].querySelector(".npc-name").textContent === "老周",
    npcItems.length ? npcItems[0].textContent : "无");
  check("NPC 条目带身份标签", npcItems.length > 0 && npcItems[0].querySelector(".npc-role").textContent.indexOf("古堡看守") >= 0,
    npcItems.length ? npcItems[0].textContent : "无");
  check("NPC 条目以 tooltip 收别名（外号）", npcItems.length > 0 && (npcItems[0].title || "").indexOf("周老头") >= 0,
    npcItems.length ? npcItems[0].title : "无");

  // ---- 剧情档案：welcome 里的 memory 应渲染到侧栏 ----
  const memText = ($("memory") || {}).textContent || "";
  check("侧栏渲染剧情档案状态", memText.indexOf("已记录 7 条") >= 0, memText);
  check("档案状态标出语义检索可用", memText.indexOf("语义检索：开") >= 0, memText);
  check("档案状态标出已有主线摘要", memText.indexOf("已有主线摘要") >= 0, memText);

  // ---- 打开设置 ----
  $("settings-btn").click();
  check("点击「模型」后抽屉打开", !$("settings-drawer").classList.contains("hidden"));
  check("遮罩同时出现", !$("settings-overlay").classList.contains("hidden"));
  check("渲染出三个槽位卡片", !!$("card-dm") && !!$("card-assistant") && !!$("card-advisor"));
  check("DM 卡片未被锁（我是房主）", !$("card-dm").classList.contains("locked"));
  check("主机小助手卡片未被锁（我是房主）", !$("card-assistant").classList.contains("locked"));
  check("初始徽标为「未接入（脚化）」", $("badge-dm").textContent.indexOf("未接入") === 0, $("badge-dm").textContent);

  // ---- 切到本地模型 ----
  const dmSeg = $("card-dm").querySelectorAll(".seg button");
  check("模式切换有三个选项", dmSeg.length === 3, String(dmSeg.length));
  dmSeg[2].click(); // 本地模型
  const dmCard2 = $("card-dm");
  check("切成「本地模型」后出现预设通道", dmCard2.querySelectorAll(".preset").length >= 3, String(dmCard2.querySelectorAll(".preset").length));
  check("本地模型不显示 API Key 输入框", dmCard2.querySelectorAll(".field input").length === 2, String(dmCard2.querySelectorAll(".field input").length));
  const bu = dmCard2.querySelectorAll(".field input")[0];
  check("默认填入 llama.cpp 地址", bu.value.indexOf("127.0.0.1:8080") > 0, bu.value);

  // 改成本地假 LLM
  bu.value = "http://127.0.0.1:" + FAKE_LLM_PORT + "/v1";
  bu.dispatchEvent(new w.Event("input", { bubbles: true }));
  const md = dmCard2.querySelectorAll(".field input")[1];
  md.value = "fake-model";
  md.dispatchEvent(new w.Event("input", { bubbles: true }));

  // ---- 助手槽位切到云 API 并填 key ----
  $("card-advisor").querySelectorAll(".seg button")[1].click();
  const ac = $("card-advisor");
  const aInputs = ac.querySelectorAll(".field input");
  check("云 API 显示 key 输入框（password 型）", aInputs.length === 3 && aInputs[1].type === "password", String(aInputs.length));
  check("云 API 默认预填 DeepSeek 地址", aInputs[0].value.indexOf("deepseek") > 0, aInputs[0].value);
  aInputs[1].value = "sk-ui-check";
  aInputs[1].dispatchEvent(new w.Event("input", { bubbles: true }));
  aInputs[2].value = "deepseek-chat";
  aInputs[2].dispatchEvent(new w.Event("input", { bubbles: true }));

  // ---- 主机小助手槽位切到本地模型并填地址（房主可配）----
  $("card-assistant").querySelectorAll(".seg button")[2].click();
  const asst = $("card-assistant");
  const asstInputs = asst.querySelectorAll(".field input");
  check("小助手切本地模型后出现地址框", asstInputs.length === 2, String(asstInputs.length));
  check("小助手默认填入 8081 地址", asstInputs[0].value.indexOf("127.0.0.1:8081") > 0, asstInputs[0].value);
  asstInputs[0].value = "http://127.0.0.1:" + FAKE_LLM_PORT + "/v1";
  asstInputs[0].dispatchEvent(new w.Event("input", { bubbles: true }));
  asstInputs[1].value = "assistant-model";
  asstInputs[1].dispatchEvent(new w.Event("input", { bubbles: true }));

  // ---- 测试连接（走真实链路：浏览器 → 服务端 /api/models/test → 假 LLM） ----
  const testBtn = $("card-dm").querySelector(".slot-actions .btn");
  testBtn.click();
  await sleep(1500);
  const rtxt = $("result-dm").textContent;
  check("「测试连接」打通本地模型链路", $("result-dm").className.indexOf("ok") >= 0, rtxt);
  check("探测出该模型支持工具调用", rtxt.indexOf("支持工具调用") > 0, rtxt);

  // ---- 剧本卡片：读取 / 切片 / 次级 prompt ----
  check("抽屉里有剧本卡片", !!$("script-card"), "");
  const rows = $("script-card").querySelectorAll(".script-item");
  check("剧本列表渲染出至少 2 个条目", rows.length >= 2, String(rows.length));
  const activeRow = Array.from(rows).find((r) => r.querySelector(".tag").textContent === "当前");
  check("当前活动剧本被标出", !!activeRow, Array.from(rows).map((r) => r.textContent).join(" | "));

  // 点列表项 → 填进文件框
  const otherRow = Array.from(rows).find((r) => r !== activeRow);
  otherRow.click();
  await sleep(600);
  check("点击列表项会填入剧本文件", !!$("script-path").value, $("script-path").value);
  check("切片预览出现", $("script-slots").children.length >= 1, String($("script-slots").children.length));
  check("读取结果给出切片数", $("script-result").textContent.indexOf("切片") > 0, $("script-result").textContent);
  check("次级 prompt（DM）已生成", $("script-prompt").textContent.indexOf("【剧本】") >= 0, $("script-prompt").textContent.slice(0, 60));
  check("次级 prompt（小助手）已生成且不含切片索引",
    $("script-prompt-public").textContent.indexOf("【剧本】") >= 0
      && $("script-prompt-public").textContent.indexOf("【切片索引】") < 0,
    $("script-prompt-public").textContent.slice(0, 60));

  // 越界路径必须被服务端拦下，界面上显示为错误
  $("script-path").value = "../../server/main.py";
  $("script-card").querySelectorAll(".slot-actions .btn")[0].click();
  await sleep(600);
  check("越界路径在界面上报错", $("script-result").className.indexOf("err") >= 0, $("script-result").textContent);

  // 粘贴内容也能切片
  $("script-path").value = "";
  $("script-content").value = "# 序章\n渡船靠岸。\n\n# 尾声\n钟声响起。";
  $("script-card").querySelectorAll(".slot-actions .btn")[0].click();
  await sleep(600);
  check("粘贴内容也能切片（2 片）",
    $("script-slots").children.length === 2 && $("script-result").className.indexOf("err") < 0,
    $("script-slots").children.length + " / " + $("script-result").textContent);
  $("script-content").value = "";

  // 设为活动剧本（房主）—— 指向当前脚本，幂等
  $("script-path").value = activeRow ? activeRow.querySelector(".meta").textContent.split(" · ")[0] : "";
  $("script-card").querySelectorAll(".slot-actions .btn")[1].click();
  await sleep(800);
  check("房主可切换活动剧本", $("script-result").className.indexOf("ok") >= 0, $("script-result").textContent);
  check("切换后提示写进旁白流", w.document.getElementById("log").textContent.indexOf("活动剧本已切换") > 0, "");

  // ---- 保存并连接 ----
  sent.length = 0;
  $("settings-save").click();
  await sleep(200);
  const cfg = sent.find((m) => m.type === "configure");
  check("保存后发出 configure 消息", !!cfg, JSON.stringify(sent));
  check("configure 带上 DM 槽位（房主）", !!cfg && cfg.dm && cfg.dm.kind === "local", JSON.stringify(cfg && cfg.dm));
  check("configure 带上助手槽位与 key", !!cfg && cfg.advisor && cfg.advisor.kind === "cloud" && cfg.advisor.api_key === "sk-ui-check", JSON.stringify(cfg && cfg.advisor));
  check("configure 带上主机小助手槽位（房主）", !!cfg && cfg.assistant && cfg.assistant.kind === "local" && cfg.assistant.model === "assistant-model", JSON.stringify(cfg && cfg.assistant));
  check("本机保存了设置（localStorage）", !!w.localStorage.getItem("rpgbar.models"), String(w.localStorage.getItem("rpgbar.models")).slice(0, 60));

  // ---- 服务端回执后徽标/胶囊刷新 ----
  ws.deliver({
    type: "agent_status",
    changed: ["dm", "assistant", "advisor"],
    agents: {
      owner: "亚瑟",
      dm: { kind: "local", base_url: "http://127.0.0.1:8123/v1", model: "fake-model", has_api_key: false, ready: true, mode: "model" },
      assistant: { kind: "local", base_url: "http://127.0.0.1:8123/v1", model: "assistant-model", has_api_key: false, ready: true, mode: "model" },
      advisor: { kind: "cloud", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat", has_api_key: true, ready: true, mode: "model" },
    },
  });
  check("DM 徽标变为已连接", $("badge-dm").className.indexOf("on") >= 0 && $("badge-dm").textContent.indexOf("已连接") === 0, $("badge-dm").textContent);
  check("主机小助手徽标变为已连接", $("badge-assistant").className.indexOf("on") >= 0 && $("badge-assistant").textContent.indexOf("已连接") === 0, $("badge-assistant").textContent);
  check("顶栏胶囊显示 DM+助手", $("model-pill").textContent === "模型：DM + 助手", $("model-pill").textContent);

  // ---- 非房主：DM 锁死 ----
  ws.deliver({
    type: "agent_status",
    changed: [],
    agents: {
      owner: "别人",
      dm: { kind: "off", base_url: "", model: "", has_api_key: false, ready: false, mode: "scripted" },
      assistant: { kind: "off", base_url: "", model: "", has_api_key: false, ready: false, mode: "scripted" },
      advisor: { kind: "off", base_url: "", model: "", has_api_key: false, ready: false, mode: "scripted" },
    },
  });
  $("settings-close").click();
  check("关闭按钮收起抽屉", $("settings-drawer").classList.contains("hidden"));
  $("settings-btn").click();
  check("非房主时 DM 卡片被锁", $("card-dm").classList.contains("locked"), $("card-dm").className);
  check("非房主时 DM 无法切换模式", $("card-dm").querySelectorAll(".seg button")[0].disabled === true);
  check("非房主时给出说明文案", $("card-dm").querySelector(".lock-note").textContent.indexOf("房主") >= 0, $("card-dm").querySelector(".lock-note").textContent);
  check("非房主时主机小助手卡片被锁", $("card-assistant").classList.contains("locked"), $("card-assistant").className);
  check("非房主时主机小助手无法切换模式", $("card-assistant").querySelectorAll(".seg button")[0].disabled === true);
  check("非房主时玩家小助手仍可编辑", !$("card-advisor").classList.contains("locked"), $("card-advisor").className);
  const scriptBtns = $("script-card").querySelectorAll(".slot-actions .btn");
  check("非房主时剧本卡片也被锁", $("script-card").querySelector(".lock-note").textContent.indexOf("房主") >= 0, "");
  check("非房主不能「设为活动剧本」", scriptBtns[1].disabled === true, "disabled=" + scriptBtns[1].disabled);
  check("非房主仍可「读取并切片」（只读）", scriptBtns[0].disabled === false, "");

  // ---- 清除本机设置 ----
  $("settings-clear").click();
  await sleep(150);
  check("清除后本地设置被移除", !w.localStorage.getItem("rpgbar.models"), String(w.localStorage.getItem("rpgbar.models")));
  check("清除后徽标回到未接入", $("badge-dm").textContent.indexOf("未接入") === 0, $("badge-dm").textContent);

  check("全程无 JS 运行时异常", jsErrors.length === 0, jsErrors.join(" | "));

  fakeLLM.close();
  console.log("\n结果：" + PASS + " 通过，" + FAIL + " 失败");
  process.exit(FAIL ? 1 : 0);
})().catch((e) => {
  console.error("UI CHECK CRASH:", e);
  process.exit(1);
});
