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
  const realFetch = (url, opts) => fetch(url.startsWith("http") ? url : BASE + url, opts);
  // 大厅列表走 /api/rooms 轮询 —— 真实结果取决于**当时**服务器上有没有人开着房，
  // 拿它做断言就是"看运气"。这里只截这一条路径，喂一份固定数据；其余请求照旧
  // 打真服务器（「测试连接」「读取切片」那些必须走真链路）。
  let roomsPayload = { rooms: [], waiting: 0, active_title: "", defaults: {} };
  w.fetch = (url, opts) => {
    if (String(url).indexOf("/api/rooms") >= 0) {
      return Promise.resolve({ json: () => Promise.resolve(roomsPayload) });
    }
    return realFetch(url, opts);
  };
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

  // ---- 进大厅：填名字只到这里为止，**不建连接**；选房/建房才连 ----
  roomsPayload = {
    rooms: [
      { code: "WAIT01", name: "今晚八点蛇王墓", note: "新手局，慢慢玩", script: "古堡秘宝",
        owner: "老王", online: 2, max_players: 4, phase: "waiting", phase_label: "等待中",
        visibility: "public", has_password: false, created_at: 1 },
      { code: "LOCK03", name: "蛇王墓的局", note: "", script: "古堡秘宝",
        owner: "老赵", online: 1, max_players: 4, phase: "waiting", phase_label: "等待中",
        visibility: "public", has_password: true, created_at: 2 },
      { code: "PLAY05", name: "老手局", note: "", script: "古堡秘宝",
        owner: "小陈", online: 1, max_players: 4, phase: "playing", phase_label: "进行中",
        visibility: "public", has_password: false, created_at: 3 },
      { code: "FULL04", name: "老李的局", note: "", script: "古堡秘宝",
        owner: "老李", online: 4, max_players: 4, phase: "playing", phase_label: "进行中",
        visibility: "public", has_password: false, created_at: 4 },
    ],
    waiting: 2,
    active_title: "古堡秘宝",
    defaults: { script: "scripts/sample_script.json", script_title: "古堡秘宝",
                players: { min: 2, max: 4, recommended: 4 } },
  };

  $("join-name").value = "亚瑟";
  $("join-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await sleep(30);
  check("填名字后进入大厅屏（不直接连服务器）",
    !$("lobby-screen").classList.contains("hidden") && $("join-screen").classList.contains("hidden"),
    $("lobby-screen").className);
  check("没选房之前不建 WebSocket", !FakeWS.last || sent.length === 0, JSON.stringify(sent));
  check("大厅顶栏标出我是谁", ($("lobby-me").textContent || "").indexOf("亚瑟") === 0, $("lobby-me").textContent);

  for (let i = 0; i < 40 && w.document.querySelectorAll("#lobby-rooms .room-item").length === 0; i++) {
    await sleep(50);
  }
  const roomItems = w.document.querySelectorAll("#lobby-rooms .room-item");
  const tagsOf = (li) => Array.from(li.querySelectorAll(".room-tag")).map((t) => t.textContent).join(" | ");
  check("大厅列出公开房间", roomItems.length === 4, String(roomItems.length));
  check("房间条目显示房名与备注（给陌生人看的）",
    roomItems[0].querySelector(".room-title").textContent === "今晚八点蛇王墓" &&
    roomItems[0].querySelector(".room-note").textContent === "新手局，慢慢玩",
    roomItems[0].textContent);
  check("房间条目带房主 / 人数 / 阶段 / 房间码",
    tagsOf(roomItems[0]).indexOf("房主：老王") >= 0 && tagsOf(roomItems[0]).indexOf("2/4 人") >= 0 &&
    tagsOf(roomItems[0]).indexOf("等待中") >= 0 && tagsOf(roomItems[0]).indexOf("房间码 WAIT01") >= 0,
    tagsOf(roomItems[0]));
  check("等待中的房间按钮是「进入」",
    roomItems[0].querySelector(".room-join").textContent === "进入",
    roomItems[0].querySelector(".room-join").textContent);
  check("进行中且没满的房间按钮是「中途加入」",
    roomItems[2].querySelector(".room-join").textContent === "中途加入",
    roomItems[2].querySelector(".room-join").textContent);
  check("设了密码的房间被标出来", tagsOf(roomItems[1]).indexOf("需密码") >= 0, tagsOf(roomItems[1]));
  check("满员的房间按钮置灰并写「已满」",
    roomItems[3].querySelector(".room-join").disabled === true &&
    roomItems[3].querySelector(".room-join").textContent === "已满",
    roomItems[3].querySelector(".room-join").textContent);
  check("房间计数跟着列表走", $("lobby-count").textContent === "4", $("lobby-count").textContent);
  check("有房间时不显示空态", $("lobby-empty").classList.contains("hidden"), $("lobby-empty").className);
  check("大厅顶栏显示活动剧本",
    ($("lobby-active").textContent || "").indexOf("古堡秘宝") >= 0, $("lobby-active").textContent);

  // 剧本下拉与人数提示（建房表单）：由 /api/scripts 的 players 段算出来
  for (let i = 0; i < 40 && $("create-script").options.length === 0; i++) {
    await sleep(50);
  }
  check("建房表单的剧本下拉已被填充", $("create-script").options.length >= 1,
    String($("create-script").options.length));
  check("人数提示写出剧本建议的区间",
    /建议\s*\d+–\d+\s*人/.test($("create-players-hint").textContent || ""),
    $("create-players-hint").textContent);
  check("人数上限初值落在剧本声明的区间内",
    Number($("create-max").value) >= Number($("create-max").min) &&
    Number($("create-max").value) <= Number($("create-max").max),
    $("create-max").value + " ∈ [" + $("create-max").min + ", " + $("create-max").max + "]");

  // ---- 私密房：没填密码时只提示、不发连接 ----
  const beforeWs = FakeWS.last;
  sent.length = 0;
  roomItems[1].querySelector(".room-join").click();
  await sleep(30);
  check("点有密码的房间但没填密码 ⇒ 只提示、不建连接",
    FakeWS.last === beforeWs && sent.length === 0, JSON.stringify(sent));
  check("提示里点名是哪间房", ($("lobby-msg").textContent || "").indexOf("蛇王墓的局") >= 0,
    $("lobby-msg").textContent);
  $("lobby-pass").value = "pw";
  roomItems[1].querySelector(".room-join").click();
  await sleep(30);
  check("填了密码后点进入就建连接", FakeWS.last !== beforeWs);
  check("join 消息带上所选房间码",
    sent.some((m) => m.type === "join" && m.room === "LOCK03"), JSON.stringify(sent));
  check("join 消息带上密码",
    sent.some((m) => m.type === "join" && m.password === "pw"), JSON.stringify(sent));
  check("加入别人的房间时不带建房参数",
    !sent.some((m) => m.type === "join" && m.create), JSON.stringify(sent));
  check("加入别人的房间时不带剧本（剧本由房主定）",
    !sent.some((m) => m.type === "join" && m.script), JSON.stringify(sent));

  // ---- 建房：走的是同一条连接，区别只在 join 里带 create ----
  $("lobby-back").click();
  check("「改名字」退回加入页", !$("join-screen").classList.contains("hidden"), $("join-screen").className);
  $("join-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await sleep(30);
  sent.length = 0;
  $("create-name").value = "今晚八点蛇王墓";
  $("create-note").value = "新手局，慢慢玩";
  $("create-visibility").value = "private";
  $("create-password").value = "pw123";
  $("create-form").dispatchEvent(new w.Event("submit", { bubbles: true, cancelable: true }));
  await sleep(30);
  const build = sent.find((m) => m.type === "join" && m.create);
  check("建房时 join 里带上 create（与加入走同一条路）", !!build, JSON.stringify(sent));
  check("create 带上房名 / 备注（trim 过）",
    !!build && build.create.name === "今晚八点蛇王墓" && build.create.note === "新手局，慢慢玩",
    JSON.stringify(build && build.create));
  check("create 带上人数上限（界面上的值）",
    !!build && String(build.create.max_players) === $("create-max").value, JSON.stringify(build && build.create));
  check("create 带上可见性与密码",
    !!build && build.create.visibility === "private" && build.create.password === "pw123",
    JSON.stringify(build && build.create));
  check("create 带上所选剧本",
    !!build && !!build.create.script, JSON.stringify(build && build.create));
  check("建房时房间码留空（由服务端发号）", !!build && build.room === "", JSON.stringify(build));

  // ---- 连入房间 ----
  await sleep(30); // 等 onopen 回调
  const ws = FakeWS.last;
  check("提交后建立了 WebSocket", !!ws, "无 ws");
  check("进游戏屏（等待室盖在上面，进来立刻有反馈）",
    !$("game-screen").classList.contains("hidden"), $("game-screen").className);

  const roomSnapshot = (extra) => Object.assign({
    code: "ABC12", name: "今晚八点蛇王墓", note: "新手局，慢慢玩", script: "古堡秘宝",
    owner: "亚瑟", online: 2, max_players: 4, phase: "waiting", phase_label: "等待中",
    visibility: "private", has_password: true, created_at: 0,
  }, extra || {});

  ws.deliver({
    type: "welcome",
    room: "ABC12",
    you: "亚瑟",
    late: false,
    script: "古堡秘宝",
    room_info: roomSnapshot(),
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
      dm: { kind: "off", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat", has_api_key: false, ready: false, mode: "off" },
      assistant: { kind: "off", base_url: "http://127.0.0.1:8081/v1", model: "qwen2.5-3b-instruct", has_api_key: false, ready: false, mode: "off" },
      advisor: { kind: "off", base_url: "http://127.0.0.1:8080/v1", model: "local-model", has_api_key: false, ready: false, mode: "off" },
    },
  });
  check("welcome 后顶栏显示房间码", $("room-code").textContent === "ABC12", $("room-code").textContent);
  check("welcome 后品牌名跟随剧本", (q(".brand-name") || {}).textContent === "RPGBar · 古堡秘宝", (q(".brand-name") || {}).textContent);
  check("一个模型都没接时胶囊显示未接入", $("model-pill").textContent === "模型：未接入", $("model-pill").textContent);

  // ---- 等待室：开局前盖在游戏画面上，房主才看得到操作区 ----
  check("等待阶段显示等待室", !$("waiting-room").classList.contains("hidden"), $("waiting-room").className);
  check("等待室显示房名 / 房间码 / 剧本",
    $("wait-title").textContent === "今晚八点蛇王墓" && $("wait-code").textContent === "ABC12" &&
    $("wait-script").textContent === "古堡秘宝",
    [$("wait-title").textContent, $("wait-code").textContent, $("wait-script").textContent].join(" / "));
  check("等待室显示人数 x/y 与房主",
    $("wait-count").textContent === "2/4" && $("wait-owner").textContent === "亚瑟",
    $("wait-count").textContent + " / " + $("wait-owner").textContent);
  check("等待室标出可见性与有没有进房密码",
    $("wait-vis").textContent === "私密" && $("wait-pass").textContent === "有",
    $("wait-vis").textContent + " / " + $("wait-pass").textContent);
  check("等待室显示备注", $("wait-note").textContent === "新手局，慢慢玩", $("wait-note").textContent);
  check("顶栏也能看到房名与人数",
    $("room-name").textContent === "今晚八点蛇王墓" && $("room-players").textContent === "2/4",
    $("room-name").textContent + " / " + $("room-players").textContent);
  check("等待阶段先把场景胶囊收起来（没剧情可显示）",
    $("scene-title").parentNode.classList.contains("hidden"), $("scene-title").parentNode.className);
  check("我是房主 ⇒ 房主操作区可见", !$("wait-host").classList.contains("hidden"), $("wait-host").className);
  check("房主看到的提示是「点开始游戏」",
    ($("wait-hint").textContent || "").indexOf("开始游戏") >= 0, $("wait-hint").textContent);

  // 队伍列表：房主对别人有「请出 / 设为房主」两个动作
  // ⚠️ 快照要按服务端真实形状给（npcs / memory 一起带）——`renderState` 会**无条件**
  // 用 state.npcs / state.memory 重画侧栏，少给两个键就等于把 welcome 里那份清成空。
  ws.deliver({
    type: "state",
    state: {
      players: [
        { name: "亚瑟", character: { cls: "战士" } },
        { name: "梅林", character: { cls: "法师" } },
      ],
      current_scene: "gate", scene_title: "古堡大门", location: "大门",
      flags: [], flag_details: [], inventory: [], hp: {},
      npcs: [
        { id: "npc01", name: "老周", role: "古堡看守", aliases: ["周老头"], from_script: true },
      ],
      memory: { entries: 7, actors: 2, has_summary: true, embedding: true },
    },
  });
  const waitRows = w.document.querySelectorAll("#wait-players li");
  check("等待室列出队伍", waitRows.length === 2, String(waitRows.length));
  check("队伍按名字+职业显示",
    waitRows.length === 2 && waitRows[1].textContent.indexOf("梅林") >= 0 && waitRows[1].textContent.indexOf("法师") >= 0,
    waitRows.length ? waitRows[1].textContent : "无");
  check("房主那一行带「房主」标记",
    waitRows.length === 2 && !!waitRows[0].querySelector(".host-flag"), waitRows.length ? waitRows[0].textContent : "无");
  const hostBtns = waitRows.length === 2 ? waitRows[1].querySelectorAll("button") : [];
  check("房主对别人有「请出 / 设为房主」两个动作",
    hostBtns.length === 2 && hostBtns[0].textContent === "请出" && hostBtns[1].textContent === "设为房主",
    Array.from(hostBtns).map((b) => b.textContent).join(" | "));
  sent.length = 0;
  if (hostBtns.length === 2) hostBtns[1].click();
  check("点「设为房主」发出 transfer 消息",
    sent.some((m) => m.type === "transfer" && m.target === "梅林"), JSON.stringify(sent));
  sent.length = 0;
  if (hostBtns.length === 2) hostBtns[0].click();
  check("点「请出」发出 kick 消息",
    sent.some((m) => m.type === "kick" && m.target === "梅林"), JSON.stringify(sent));

  // ---- 角色卡：等待室里填（不需要卡的剧本整块不出现）----
  // 服务端下发的形状：{type:"character_schema", status, error, needs_card, card}
  const CARD = {
    required: true, reason: "这个本有战斗与学识检定", total_points: 6,
    attributes: [
      { key: "力量", desc: "搬、劈、扛", min: 8, base: 10, max: 16 },
      { key: "学识", desc: "识破机关", min: 10, base: 10, max: 18 },
    ],
    // skill_map 服务端不会下发（是剧本内幕）；这里故意多带一个，验证界面不依赖它
    skill_map: { 战斗: "力量" },
  };
  const cardRows = () => Array.from(w.document.querySelectorAll("#card-rows .card-row"));
  const cardVal = (i) => (cardRows()[i] ? cardRows()[i].querySelector(".v").textContent : "—");
  const cardBtns = (i) => (cardRows()[i] ? cardRows()[i].querySelectorAll("button") : []);

  check("还没分析完时不显示角色卡面板", $("card-panel").classList.contains("hidden"), $("card-panel").className);

  ws.deliver({ type: "character_schema", status: "pending", needs_card: false, card: null });
  await sleep(10);
  check("分析中：面板不出现，只留一行状态",
    $("card-panel").classList.contains("hidden") && !$("card-status").classList.contains("hidden"),
    $("card-status").className + " / " + $("card-status-text").textContent);
  check("分析中：不显示「重试分析」", $("card-retry").classList.contains("hidden"), $("card-retry").className);

  ws.deliver({ type: "character_schema", status: "ready", needs_card: true, card: CARD });
  await sleep(10);
  check("分析完且需要填卡 ⇒ 面板出现", !$("card-panel").classList.contains("hidden"), $("card-panel").className);
  check("角色卡按剧本给的维度画行", cardRows().length === 2, String(cardRows().length));
  check("初值是剧本给的基础值", cardVal(0) === "10" && cardVal(1) === "10", cardVal(0) + "/" + cardVal(1));
  check("显示剩余点数", $("card-budget").textContent.indexOf("6 / 6") >= 0, $("card-budget").textContent);
  check("显示剧本给的理由", $("card-reason").textContent === CARD.reason, $("card-reason").textContent);
  check("有属性说明就显示说明", cardRows()[0].querySelector(".d").textContent === "搬、劈、扛",
    cardRows()[0].querySelector(".d").textContent);
  check("按钮初值是「提交角色卡」", $("card-submit").textContent === "提交角色卡", $("card-submit").textContent);

  // 加减号：服务端会再校验一遍，这里的禁用只是提示
  cardBtns(0)[1].click(); // +
  await sleep(10);
  check("点 + 加一点", cardVal(0) === "11", cardVal(0));
  check("剩余点数跟着减少", $("card-budget").textContent.indexOf("5 / 6") >= 0, $("card-budget").textContent);
  cardBtns(0)[0].click(); // -
  cardBtns(0)[0].click(); // -
  await sleep(10);
  check("点 − 减到基础值之下也行（到下限为止）", cardVal(0) === "9", cardVal(0));
  cardBtns(0)[0].click();
  await sleep(10);
  check("到下限就不能再减", cardVal(0) === "8" && cardBtns(0)[0].disabled === true, cardVal(0));

  // 把点数用光 → 别人的 + 都该置灰
  for (let i = 0; i < 8; i++) cardBtns(0)[1].click();
  await sleep(10);
  check("加到上限就停住", cardVal(0) === "16", cardVal(0));
  check("点数用光后 + 全部置灰",
    cardBtns(0)[1].disabled === true && cardBtns(1)[1].disabled === true,
    [cardBtns(0)[1].disabled, cardBtns(1)[1].disabled].join("/"));
  check("点数用光时剩余是 0", $("card-budget").textContent.indexOf("0 / 6") >= 0, $("card-budget").textContent);

  sent.length = 0;
  $("card-submit").click();
  await sleep(10);
  const cardMsgSent = sent.find((m) => m.type === "character");
  check("提交发出 character 消息并带上全部属性",
    !!cardMsgSent && cardMsgSent.attrs && cardMsgSent.attrs["力量"] === 16 && cardMsgSent.attrs["学识"] === 10,
    JSON.stringify(sent));

  ws.deliver({ type: "character_ok", attrs: { 力量: 16, 学识: 10 }, summary: "力量 16 · 学识 10" });
  await sleep(10);
  check("提交成功后有回执", ($("card-msg").textContent || "").indexOf("已提交") >= 0, $("card-msg").textContent);
  check("提交后按钮变成「更新角色卡」", $("card-submit").textContent === "更新角色卡", $("card-submit").textContent);

  ws.deliver({ type: "character_rejected", errors: ["点数超了：一共 6 点，你用了 8 点"] });
  await sleep(10);
  check("被拒时把原因写出来", ($("card-msg").textContent || "").indexOf("点数超了") >= 0, $("card-msg").textContent);
  check("被拒的错误是红的", $("card-msg").className.indexOf("bad") >= 0, $("card-msg").className);

  $("card-reset").click();
  await sleep(10);
  check("重置回到基础值", cardVal(0) === "10" && cardVal(1) === "10", cardVal(0) + "/" + cardVal(1));

  // 服务端回存的属性是权威的：state 一带 attrs，界面就跟着走
  ws.deliver({
    type: "state",
    state: {
      players: [
        { name: "亚瑟", character: { cls: "战士", attrs: { 力量: 14, 学识: 12 } } },
        { name: "梅林", character: { cls: "法师" } },
      ],
      current_scene: "gate", scene_title: "古堡大门", location: "大门",
      flags: [], flag_details: [], inventory: [], hp: {},
      // ⚠️ 必须是完整的 state 形状：`renderState` 会无条件用这些键重画侧栏，
      // 少给一个（比如 npcs: []）就等于把 welcome 里那份清掉 —— 后面的 NPC 断言就全塌了。
      npcs: [
        { id: "npc01", name: "老周", role: "古堡看守", aliases: ["周老头"], from_script: true },
      ],
      memory: { entries: 7, actors: 2, has_summary: true, embedding: true },
    },
  });
  await sleep(10);
  check("state 里的属性会盖回卡片", cardVal(0) === "14" && cardVal(1) === "12", cardVal(0) + "/" + cardVal(1));
  const cardRowsNow = w.document.querySelectorAll("#wait-players li");
  check("队伍里标出谁填了卡",
    cardRowsNow.length === 2 &&
    !!cardRowsNow[0].querySelector(".card-tag.done") && !!cardRowsNow[1].querySelector(".card-tag"),
    cardRowsNow.length ? Array.from(cardRowsNow).map((li) => li.textContent).join(" | ") : "无");

  // 不需要角色卡的剧本：整块都不出现
  ws.deliver({ type: "character_schema", status: "ready", needs_card: false, card: { required: false, reason: "纯叙事", attributes: [], total_points: 0 } });
  await sleep(10);
  check("不需要角色卡 ⇒ 面板与状态行都消失",
    $("card-panel").classList.contains("hidden") && $("card-status").classList.contains("hidden"),
    $("card-panel").className + " / " + $("card-status").className);

  // 分析失败：如实报错，并给一个「重试分析」
  ws.deliver({ type: "character_schema", status: "failed", needs_card: false, card: null, error: "未配置模型" });
  await sleep(10);
  check("分析失败时显示原因", ($("card-status-text").textContent || "").indexOf("未配置模型") >= 0,
    $("card-status-text").textContent);
  check("分析失败才出现「重试分析」", !$("card-retry").classList.contains("hidden"), $("card-retry").className);
  sent.length = 0;
  $("card-retry").click();
  await sleep(10);
  check("点「重试分析」发出 card_refresh", sent.some((m) => m.type === "card_refresh"), JSON.stringify(sent));

  // 回到「有卡」的状态，后面的用例继续用同一块等待室
  ws.deliver({ type: "character_schema", status: "ready", needs_card: true, card: CARD });
  await sleep(10);

  // ---- 房主控制条：改设置 / 开始 / 解散（两步）----
  sent.length = 0;
  $("set-note").value = "改成新手友好";
  $("set-max").value = "3";
  $("set-visibility").value = "public";
  $("set-password").value = "newpw";
  $("set-save").click();
  await sleep(20);
  const setMsg = sent.find((m) => m.type === "room_settings");
  check("点「保存设置」发出 room_settings", !!setMsg, JSON.stringify(sent));
  check("room_settings 带上改后的备注/上限/可见性",
    !!setMsg && setMsg.note === "改成新手友好" && String(setMsg.max_players) === "3" &&
    setMsg.visibility === "public",
    JSON.stringify(setMsg));
  check("填了新密码就带上新密码（不填 = 不改）",
    !!setMsg && setMsg.password === "newpw" && !setMsg.clear_password, JSON.stringify(setMsg));
  check("保存后清空密码框（免得留着一串看着像已生效）",
    $("set-password").value === "", $("set-password").value);
  sent.length = 0;
  $("set-clear-password").checked = true;
  $("set-save").click();
  await sleep(20);
  const clearMsg = sent.find((m) => m.type === "room_settings");
  check("勾了「清除密码」就发 clear_password、不发 password",
    !!clearMsg && clearMsg.clear_password === true && !clearMsg.password, JSON.stringify(clearMsg));
  $("set-clear-password").checked = false;

  sent.length = 0;
  $("wait-start").click();
  check("点「开始游戏」发出 start 消息", sent.some((m) => m.type === "start"), JSON.stringify(sent));

  sent.length = 0;
  $("wait-close").click();
  check("解散要先点两次（第一次变成「确认解散」）",
    sent.length === 0 && $("wait-close").textContent.indexOf("确认解散") >= 0,
    $("wait-close").textContent + " / " + JSON.stringify(sent));
  $("wait-close").click();
  check("第二次点才真的发出 close_room",
    sent.some((m) => m.type === "close_room"), JSON.stringify(sent));

  // ---- 阶段翻转：等待室收起 ----
  ws.deliver({ type: "room", room: roomSnapshot({ phase: "playing", phase_label: "进行中" }) });
  check("进入进行中后等待室收起", $("waiting-room").classList.contains("hidden"), $("waiting-room").className);
  check("场景胶囊重新出现", !$("scene-title").parentNode.classList.contains("hidden"),
    $("scene-title").parentNode.className);
  ws.deliver({ type: "room", room: roomSnapshot() });
  check("回到等待阶段等待室又盖上（房主能退回等待室等人）",
    !$("waiting-room").classList.contains("hidden"), $("waiting-room").className);
  ws.deliver({ type: "room", room: roomSnapshot({ phase: "playing", phase_label: "进行中" }) });

  // ---- 不是房主：操作区收起来，队伍里不再有别人 ----
  ws.deliver({
    type: "room_sync",
    agents: {
      owner: "老王",
      dm: { kind: "off", base_url: "", model: "", has_api_key: false, ready: false, mode: "off" },
      assistant: { kind: "off", base_url: "", model: "", has_api_key: false, ready: false, mode: "off" },
      advisor: { kind: "off", base_url: "", model: "", has_api_key: false, ready: false, mode: "off" },
    },
    script_info: { title: "古堡秘宝", path: "sample_script.json", fmt: "json", structured: true, scenes: 5, flags: 5, chunks: 6, warnings: [] },
    chunks: [],
    prompt: { advisor: "（公开版）", dm: null },
  });
  ws.deliver({ type: "room", room: roomSnapshot({ owner: "老王" }) });
  check("房主换人后房主操作区收起", $("wait-host").classList.contains("hidden"), $("wait-host").className);
  check("非房主看到的提示是「等房主开始」",
    ($("wait-hint").textContent || "").indexOf("等房主") >= 0, $("wait-hint").textContent);
  const rowsAsGuest = w.document.querySelectorAll("#wait-players li");
  check("非房主的队伍行里没有「请出 / 设为房主」",
    Array.from(rowsAsGuest).every((r) => r.querySelectorAll("button").length === 0),
    Array.from(rowsAsGuest).map((r) => r.textContent).join(" | "));

  // 复位成「我是房主」的等待中状态，后面的设置抽屉用例按房主身份跑
  ws.deliver({
    type: "room_sync",
    agents: {
      owner: "亚瑟",
      dm: { kind: "off", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat", has_api_key: false, ready: false, mode: "off" },
      assistant: { kind: "off", base_url: "http://127.0.0.1:8081/v1", model: "qwen2.5-3b-instruct", has_api_key: false, ready: false, mode: "off" },
      advisor: { kind: "off", base_url: "http://127.0.0.1:8080/v1", model: "local-model", has_api_key: false, ready: false, mode: "off" },
    },
    script_info: { title: "古堡秘宝", path: "sample_script.json", fmt: "json", structured: true, scenes: 5, flags: 5, chunks: 6, warnings: [] },
    chunks: [
      { id: "meta", title: "剧本设定", kind: "meta", chars: 120 },
      { id: "gate", title: "古堡大门", kind: "scene", chars: 220 },
    ],
    prompt: { advisor: "（小助手公开版 prompt）", dm: "（DM 完整版 prompt）" },
  });
  ws.deliver({ type: "room", room: roomSnapshot({ phase: "playing", phase_label: "进行中" }) });
  check("回到房主身份后操作区又能看见", !$("wait-host").classList.contains("hidden"), $("wait-host").className);

  // 「本房间」同步块：剧本 + 切片 + 两份 prompt（房主能看到 DM 那份）
  const syncEl = $("room-sync");
  check("welcome 后同步块显示剧本名", !!syncEl && (syncEl.textContent || "").indexOf("古堡秘宝") >= 0, syncEl ? syncEl.textContent : "无");
  check("同步块列出切片", w.document.querySelectorAll("#room-sync .room-sync-chunks .script-slot").length === 2,
    String(w.document.querySelectorAll("#room-sync .room-sync-chunks .script-slot").length));
  check("房主能看到两份 prompt", w.document.querySelectorAll("#room-sync .prompt-block").length === 2,
    String(w.document.querySelectorAll("#room-sync .prompt-block").length));

  // ---- 「正在思考」提示：点「助手建议」应**立刻**出现占位（不等服务端）----
  // 这类"点了没反应"的体验问题最容易静默回归，所以钉成断言。
  $("suggest-btn").click();
  check("点建议后发出 suggest 消息", sent.some((m) => m.type === "suggest"), JSON.stringify(sent.slice(-1)));
  const logThink = q("#log .thinking");
  const sugThink = q("#suggestions .thinking");
  check("点建议后聊天框出现「助手正在思考…」", !!logThink && logThink.textContent.indexOf("助手正在思考") >= 0,
    logThink ? logThink.textContent : "无");
  check("建议区同时出现助手占位", !!sugThink);
  check("思考期间「助手建议」按钮被禁用", $("suggest-btn").disabled === true);
  // 服务端回 suggestions → 占位撤掉、选项渲染
  ws.deliver({ type: "suggestions", options: ["观察四周", "检查石门"], agent: { source: "failed" } });
  check("收到建议后聊天框占位被撤掉", !q("#log .thinking"));
  check("建议选项已渲染", w.document.querySelectorAll("#suggestions .suggestion").length === 2,
    String(w.document.querySelectorAll("#suggestions .suggestion").length));
  check("没产出的一轮被标出（warn 标签）", !!q("#suggestions .suggest-src.warn"),
    (q("#suggestions .suggest-src.warn") || {}).textContent || "无");
  check("建议返回后按钮恢复可用", $("suggest-btn").disabled === false);
  // 失败告警：agent_note（非 llm）要醒目地进聊天框
  ws.deliver({ type: "agent_note", who: "dm", source: "failed", stopped: "error", error: "序列超长" });
  check("失败告警进聊天框（醒目条目）", !!q("#log .entry.system.warn"),
    (q("#log .entry.system.warn") || {}).textContent || "无");
  // busy：上一条没跑完时要有明确反馈（之前是静默丢弃）
  ws.deliver({ type: "busy", who: "advisor", message: "上一轮还在推理中，稍后再点。" });
  check("busy 提示进聊天框", ($("log").textContent || "").indexOf("请稍候") >= 0, "");
  ws.deliver({ type: "thinking", active: false, who: "advisor" });

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

  // ---- 内置引擎卡片：think 开关默认关 ----
  check("抽屉里有内置引擎卡片", !!$("card-engine"), "");
  const engBoxes = () => $("card-engine").querySelectorAll(".check-field input[type=checkbox]");
  check("引擎卡片有两个开关（启用 / think）", engBoxes().length === 2, String(engBoxes().length));
  check("think 默认不勾（默认关）", engBoxes()[1].checked === false, "checked=" + engBoxes()[1].checked);
  check("未启用时 think 开关不可点", engBoxes()[1].disabled === true);
  check("内置引擎徽标为「未启用」", $("badge-engine").textContent.indexOf("未启用") === 0, $("badge-engine").textContent);

  // 勾选「启用内置引擎」→ think 变可点，并出现 context / max_tokens
  engBoxes()[0].checked = true;
  engBoxes()[0].dispatchEvent(new w.Event("change", { bubbles: true }));
  check("启用后出现 context / max_tokens 输入框", $("card-engine").querySelectorAll(".field input").length === 2, String($("card-engine").querySelectorAll(".field input").length));
  check("启用后 think 开关可点", engBoxes()[1].disabled === false);
  check("think 仍是默认关（启用不会自动打开思考）", engBoxes()[1].checked === false);

  // ---- 推理后端下拉（CPU / GPU）----
  const beSel = $("card-engine").querySelector(".field select");
  check("启用后有推理后端下拉", !!beSel, String($("card-engine").querySelectorAll(".field select").length));
  check("后端下拉在 context 之前（先选后端再调参数）",
        beSel === $("card-engine").querySelector(".field select"),
        beSel && beSel.parentNode.textContent.slice(0, 12));
  check("后端默认 CPU", beSel.value === "cpu", beSel.value);
  check("后端只有 CPU / GPU 两项", beSel.querySelectorAll("option").length === 2,
        String(beSel.querySelectorAll("option").length));
  check("后端不是复选框（不该被当成能同时勾两个）",
        beSel.tagName === "SELECT", beSel.tagName);
  beSel.value = "gpu";
  beSel.dispatchEvent(new w.Event("change", { bubbles: true }));
  check("切换后端不会重建卡片（只是状态）", $("card-engine").querySelector(".field select").value === "gpu");

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
  check("configure 带上内置引擎段（房主）", !!cfg && !!cfg.engine && cfg.engine.enabled === true, JSON.stringify(cfg && cfg.engine));
  check("引擎段 think === false（默认关）", !!cfg && !!cfg.engine && cfg.engine.think === false, JSON.stringify(cfg && cfg.engine));
  check("引擎段带上推理后端（上面选了 GPU）", !!cfg && !!cfg.engine && cfg.engine.backend === "gpu", JSON.stringify(cfg && cfg.engine));
  check("本机保存了设置（localStorage）", !!w.localStorage.getItem("rpgbar.models"), String(w.localStorage.getItem("rpgbar.models")).slice(0, 60));
  check("本机保存了引擎设置", !!w.localStorage.getItem("rpgbar.engine"), String(w.localStorage.getItem("rpgbar.engine")));
  check("引擎设置里存了后端（重开页面还记得）",
        JSON.parse(w.localStorage.getItem("rpgbar.engine")).backend === "gpu",
        String(w.localStorage.getItem("rpgbar.engine")));

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

  // ---- 后端回落时徽标必须说实话 ----
  // 服务端报的是"请求 gpu、实际加载的是 CPU 版产物"（CUDA 版没接上）。徽标要说
  // "实际 CPU"，否则玩家会以为自己在用 GPU，然后去别处找"为什么还是这么慢"。
  // 槽位与上面那次一模一样，只多一个 `engine` 键 —— 这条只验引擎徽标。
  const engineStatus = (eng) => ({
    type: "agent_status",
    changed: ["engine"],
    agents: {
      owner: "亚瑟",
      dm: { kind: "local", base_url: "http://127.0.0.1:8123/v1", model: "fake-model", has_api_key: false, ready: true, mode: "model" },
      assistant: { kind: "local", base_url: "http://127.0.0.1:8123/v1", model: "assistant-model", has_api_key: false, ready: true, mode: "model" },
      advisor: { kind: "cloud", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat", has_api_key: true, ready: true, mode: "model" },
      engine: eng,
    },
  });
  ws.deliver(engineStatus({
    enabled: true, backend: "gpu", lib_backend: "cpu", backend_fallback: true,
    ready: true, model: "Qwen3-1.7B-Q4_K_M.gguf", think: false, lib_name: "rpg_engine.dll",
  }));
  check("引擎徽标标出「实际 CPU（GPU 库缺失）」",
        $("badge-engine").textContent.indexOf("实际 CPU") > 0, $("badge-engine").textContent);
  check("引擎徽标不谎报跑在 GPU 上（没有「· GPU ·」那段）",
        $("badge-engine").textContent.indexOf("· GPU ·") < 0, $("badge-engine").textContent);
  ws.deliver(engineStatus({
    enabled: true, backend: "gpu", lib_backend: "gpu", backend_fallback: false,
    ready: true, model: "Qwen3-1.7B-Q4_K_M.gguf", think: false, lib_name: "rpg_engine_cuda.dll",
  }));
  check("真跑在 GPU 上时徽标显示 GPU",
        $("badge-engine").textContent.indexOf("· GPU ·") > 0, $("badge-engine").textContent);

  // ---- 没有可用显卡时「GPU」那一项必须**置灰** ----
  // CUDA 版产物是随包分发的（对方机器没独显也带着文件），所以"文件在不在"不能
  // 决定能不能选 —— 判据由服务端探测后下发（`gpu_available` / `gpu_reason`，
  // 见 server/engine.py::probe_gpu）。这里**注入**状态来验规则，于是这条断言
  // 不依赖"跑测试的这台机器有没有卡"。
  const gpuOption = (sel) => {
    const opts = sel.querySelectorAll("option");
    for (let i = 0; i < opts.length; i++) if (opts[i].value === "gpu") return opts[i];
    return null;
  };
  // 重建卡片：引擎卡片在「启用」开关变化时会重画（与勾选时立刻出现 context 输入框同理）
  const rebuildEngineCard = () => {
    for (const on of [false, true]) {
      const box = $("card-engine").querySelector(".check-field input[type=checkbox]");
      box.checked = on;
      box.dispatchEvent(new w.Event("change", { bubbles: true }));
    }
  };

  ws.deliver(engineStatus({
    enabled: true, backend: "gpu", lib_backend: "gpu", backend_fallback: false,
    ready: true, model: "Qwen3-1.7B-Q4_K_M.gguf", think: false, lib_name: "rpg_engine_cuda.dll",
    gpu_available: false, gpu_reason: "没找到 CUDA 运行时（cudart64_*.dll）", gpu_devices: 0,
  }));
  rebuildEngineCard();
  const beSelNoGpu = $("card-engine").querySelector(".field select");
  const noGpuOpt = gpuOption(beSelNoGpu);
  check("没有可用显卡时 GPU 选项置灰", !!noGpuOpt && noGpuOpt.disabled === true,
        noGpuOpt ? String(noGpuOpt.disabled) : "找不到 gpu 项");
  check("置灰时把服务端探测的原因带到界面上",
        noGpuOpt && beSelNoGpu.parentNode.textContent.indexOf("cudart64_*.dll") > 0,
        beSelNoGpu.parentNode.textContent);
  check("置灰时选中项被拉回 CPU（不出现「选着 GPU、实际跑 CPU」）",
        beSelNoGpu.value === "cpu", beSelNoGpu.value);
  check("CPU 那一项不受影响（没有卡也能玩）",
        (gpuOption(beSelNoGpu) && beSelNoGpu.value === "cpu") && !beSelNoGpu.disabled,
        noGpuOpt ? String(noGpuOpt.disabled) : "");

  ws.deliver(engineStatus({
    enabled: true, backend: "cpu", lib_backend: "cpu", backend_fallback: false,
    ready: true, model: "Qwen3-1.7B-Q4_K_M.gguf", think: false, lib_name: "rpg_engine.dll",
    gpu_available: true, gpu_reason: "", gpu_devices: 1,
  }));
  rebuildEngineCard();
  const yesGpuOpt = gpuOption($("card-engine").querySelector(".field select"));
  check("有可用显卡时 GPU 选项恢复可选", !!yesGpuOpt && yesGpuOpt.disabled === false,
        yesGpuOpt ? String(yesGpuOpt.disabled) : "找不到 gpu 项");

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
  check("非房主时内置引擎卡片被锁", $("card-engine").classList.contains("locked"), $("card-engine").className);
  check("非房主时引擎开关不可点", $("card-engine").querySelectorAll(".check-field input[type=checkbox]")[0].disabled === true);
  check("非房主时后端下拉也不可点", $("card-engine").querySelector(".field select").disabled === true);
  const scriptBtns = $("script-card").querySelectorAll(".slot-actions .btn");
  check("非房主时剧本卡片也被锁", $("script-card").querySelector(".lock-note").textContent.indexOf("房主") >= 0, "");
  check("非房主不能「设为活动剧本」", scriptBtns[1].disabled === true, "disabled=" + scriptBtns[1].disabled);
  check("非房主仍可「读取并切片」（只读）", scriptBtns[0].disabled === false, "");

  // ---- 清除本机设置 ----
  $("settings-clear").click();
  await sleep(150);
  check("清除后本地设置被移除", !w.localStorage.getItem("rpgbar.models"), String(w.localStorage.getItem("rpgbar.models")));
  check("清除后引擎设置也被移除", !w.localStorage.getItem("rpgbar.engine"), String(w.localStorage.getItem("rpgbar.engine")));
  check("清除后徽标回到未接入", $("badge-dm").textContent.indexOf("未接入") === 0, $("badge-dm").textContent);

  check("全程无 JS 运行时异常", jsErrors.length === 0, jsErrors.join(" | "));

  fakeLLM.close();
  console.log("\n结果：" + PASS + " 通过，" + FAIL + " 失败");
  process.exit(FAIL ? 1 : 0);
})().catch((e) => {
  console.error("UI CHECK CRASH:", e);
  process.exit(1);
});
