(function () {
  "use strict";

  var joinScreen = document.getElementById("join-screen");
  var lobbyScreen = document.getElementById("lobby-screen");
  var gameScreen = document.getElementById("game-screen");
  var joinForm = document.getElementById("join-form");
  var joinMsgEl = document.getElementById("join-msg");
  var logEl = document.getElementById("log");
  var playersEl = document.getElementById("players");
  var flagsEl = document.getElementById("flags");
  var npcsEl = document.getElementById("npcs");
  var memoryEl = document.getElementById("memory");
  var suggestionsEl = document.getElementById("suggestions");
  var actionInput = document.getElementById("action-input");
  var sendBtn = document.getElementById("send-btn");
  var suggestBtn = document.getElementById("suggest-btn");
  var rollBtn = document.getElementById("roll-d20");
  var copyBtn = document.getElementById("copy-room");
  var waitingRoom = document.getElementById("waiting-room");

  var roomCode = document.getElementById("room-code");
  var sceneTitle = document.getElementById("scene-title");
  var sceneLocation = document.getElementById("scene-location");

  var ws = null;
  var you = "";
  var currentScript = "";

  // ---- 大厅 / 房间状态 ----
  // me：自我介绍（名字 + 职业），进大厅前填一次，之后大厅与建房都用它。
  // roomInfo：服务端最后一次下发的房间快照（welcome 的 room_info 与 room 事件同形状）。
  // playersCache：最近一次 state 里的玩家列表（等待室要拿它显示队伍，房间快照里只有人数）。
  var me = { name: "", cls: "" };
  var roomInfo = null;
  var playersCache = [];
  var lobbyTimer = null;
  var lobbyScripts = [];
  var lastJoin = null;
  var joinDeny = null;
  var closedNotice = null;

  // ---- 角色卡 ----
  // cardState：服务端下发的 {status, error, needs_card, card}（status: pending/ready/failed）
  // cardDraft：我正在调的属性值（未提交时以它为准；提交成功后由 state 回存覆盖）
  var cardState = null;
  var cardDraft = {};
  var cardSubmitted = false;

  // 剧本名由服务端提供（/api/version 与 welcome），界面各处跟随它，不再写死。
  function setScript(name) {
    if (!name) return;
    currentScript = name;
    var brand = document.querySelector(".brand-name");
    if (brand) brand.textContent = "RPGBar · " + name;
    document.title = "RPGBar · " + name;
  }

  function loadVersion() {
    fetch("/api/version")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d) return;
        if (d.script) setScript(d.script);
        if (d.version) {
          var v = "v" + d.version;
          var el = document.getElementById("app-version");
          if (el) el.textContent = v + (d.script ? " · " + d.script : "");
          var top = document.getElementById("app-version-top");
          if (top) top.textContent = v;
        }
      })
      .catch(function () {});
  }
  loadVersion();

  function copyText(text, btn, restore) {
    if (!navigator.clipboard) return;
    navigator.clipboard.writeText(text).then(function () {
      btn.textContent = "已复制";
      setTimeout(function () { btn.textContent = restore; }, 1500);
    });
  }

  function fillShareList(el, addresses) {
    if (!el) return;
    el.innerHTML = "";
    addresses.forEach(function (a) {
      var li = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "share-addr" + (a.primary ? " primary" : "");
      btn.textContent = a.url;
      btn.title = "点击复制（" + (a.name || "") + "）";
      btn.addEventListener("click", function () { copyText(a.url, btn, a.url); });
      var tag = document.createElement("span");
      tag.className = "share-tag";
      tag.textContent = a.name || "";
      li.appendChild(btn);
      li.appendChild(tag);
      el.appendChild(li);
    });
  }

  // 本机所有可被访问的地址（含虚拟网卡）——主机发给朋友用，省得自己翻 ipconfig。
  function loadNet() {
    fetch("/api/net")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.addresses || !d.addresses.length) return;
        fillShareList(document.getElementById("share-list"), d.addresses);
        fillShareList(document.getElementById("share-list-drawer"), d.addresses);
        var box = document.getElementById("share-box");
        if (box) box.classList.remove("hidden");
      })
      .catch(function () {});
  }
  loadNet();

  // 加入页的「开局剧本」下拉：建房时可选，加入别人的房间时由房主决定（自动置灰）。
  // 建房表单里的剧本下拉：由 /api/scripts 填充；并把该剧本**声明的人数**
  // （剧本 JSON 里可选的 players 段）写进提示文案与「人数上限」的默认值。
  function syncCreatePlayers() {
    var sel = document.getElementById("create-script");
    var maxEl = document.getElementById("create-max");
    var hint = document.getElementById("create-players-hint");
    var it = null;
    for (var i = 0; i < lobbyScripts.length; i++) {
      if (lobbyScripts[i].path === (sel && sel.value)) { it = lobbyScripts[i]; break; }
    }
    var p = (it && it.players) || { min: 1, max: 4, recommended: 4 };
    if (hint) {
      hint.textContent = "这个剧本建议 " + p.min + "–" + p.max + " 人（推荐 " + p.recommended +
        "）—— 上面的人数上限可以自定，最多 " + (p.max || 4) + " 人。";
    }
    if (maxEl) {
      maxEl.min = String(p.min || 1);
      maxEl.max = String(p.max || 12);
      // 只在玩家没手动改过时跟着剧本走，免得把人家填好的数字覆盖掉
      if (!maxEl.dataset.touched) maxEl.value = String(p.recommended || p.max || 4);
    }
  }

  function loadLobbyScripts() {
    var sel = document.getElementById("create-script");
    if (!sel) return;
    fetch("/api/scripts")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        lobbyScripts = ((d && d.scripts) || []).filter(function (s) { return !s.error; });
        sel.innerHTML = "";
        lobbyScripts.forEach(function (s) {
          var opt = document.createElement("option");
          opt.value = s.path;
          var mode = s.mode_label || "剧本";
          var rules = s.rules ? " · " + s.rules : "";
          opt.textContent = mode + " · " + (s.title || s.file) + rules +
            "（" + (s.scenes || 0) + " 场景 / " + (s.chunks || 0) + " 切片）";
          if (d.active && s.path === d.active) opt.selected = true;
          sel.appendChild(opt);
        });
        syncCreatePlayers();
      })
      .catch(function () {});
  }

  (function initLobbyForm() {
    var sel = document.getElementById("create-script");
    var maxEl = document.getElementById("create-max");
    if (sel) sel.addEventListener("change", syncCreatePlayers);
    if (maxEl) maxEl.addEventListener("input", function () { maxEl.dataset.touched = "1"; });
  })();

  // 「本房间」同步块：主机建房时选定的剧本 + 下发到本机的 Agent 次级 prompt。
  function renderRoomSync(msg) {
    var host = document.getElementById("room-sync");
    if (!host) return;
    host.innerHTML = "";
    var info = msg && msg.script_info;
    if (!info) {
      host.textContent = "（尚未连入房间）";
      return;
    }
    var line = el("div", "room-sync-line");
    line.appendChild(el("b", "", info.title || "未命名剧本"));
    line.appendChild(document.createTextNode(
      " · " + (info.path || "") + " · " + (info.scenes || 0) + " 场景 / " +
      (info.chunks || 0) + " 切片" + (info.structured ? "" : "（纯文本，降级为线性场景链）")
    ));
    host.appendChild(line);

    var chunks = msg.chunks || [];
    if (chunks.length) {
      var wrap = el("div", "room-sync-chunks");
      chunks.forEach(function (c) {
        var span = el("span", "script-slot");
        span.appendChild(el("b", "", c.id));
        span.appendChild(document.createTextNode(" " + (c.title || "") + " · " + c.chars + "字"));
        wrap.appendChild(span);
      });
      host.appendChild(wrap);
    }

    var prompt = msg.prompt || {};
    [["advisor", "小助手 prompt（已同步给你）"], ["dm", "DM prompt（仅房主可见）"]].forEach(function (pair) {
      var text = prompt[pair[0]];
      if (!text) return;
      var box = el("div", "prompt-block");
      box.appendChild(el("div", "prompt-label", pair[1]));
      box.appendChild(el("pre", "script-prompt", text));
      host.appendChild(box);
    });

    (info.warnings || []).forEach(function (w) {
      host.appendChild(el("div", "slot-result warn", "剧本提示：" + w));
    });
  }

  // 检定的算式：有属性加值时写全「d20 14 +2 = 16」，没有就退回原来的「d20=14」。
  // 加值是服务端按角色卡算好一起发下来的 —— 前端不自己算，免得两边算不一样。
  function diceMath(msg) {
    var mod = typeof msg.mod === "number" ? msg.mod : 0;
    if (!mod) return "d20=" + msg.roll;
    var total = typeof msg.total === "number" ? msg.total : msg.roll + mod;
    return "d20 " + msg.roll + (mod > 0 ? " +" + mod : " " + mod) + " = " + total;
  }

  function addEntry(kind, author, text) {
    var div = document.createElement("div");
    div.className = "entry " + kind;
    var meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = author || "";
    var body = document.createElement("div");
    body.className = "body";
    body.textContent = text || "";
    div.appendChild(meta);
    div.appendChild(body);
    logEl.appendChild(div);
    refreshThinking();
    logEl.scrollTop = logEl.scrollHeight;
  }

  // 在场人物：人物卡由 NPC 子系统（主机端小模型）生成，连入即同步。
  function renderNpcs(npcs) {
    if (!npcsEl) return;
    npcsEl.innerHTML = "";
    var list = npcs || [];
    if (!list.length) {
      var empty = document.createElement("li");
      empty.textContent = "（尚未遇到）";
      npcsEl.appendChild(empty);
      return;
    }
    list.forEach(function (n) {
      var li = document.createElement("li");
      var name = document.createElement("span");
      name.className = "npc-name";
      name.textContent = n.name || "?";
      li.appendChild(name);
      if (n.role) {
        var role = document.createElement("span");
        role.className = "npc-role";
        role.textContent = " · " + n.role;
        li.appendChild(role);
      }
      if (n.aliases && n.aliases.length) li.title = "别称：" + n.aliases.join("、");
      npcsEl.appendChild(li);
    });
  }

  // 剧情档案：只记「已发生的事实」，DM 忘事时按需检索（混合检索：BM25 + 向量）。
  function renderMemory(mem) {
    if (!memoryEl) return;
    if (!mem || typeof mem.entries !== "number") {
      memoryEl.textContent = "（尚未开局）";
      return;
    }
    var bits = ["已记录 " + mem.entries + " 条"];
    if (mem.actors) bits.push(mem.actors + " 个角色");
    bits.push(mem.has_summary ? "已有主线摘要" : "暂无摘要");
    bits.push(mem.embedding ? "语义检索：开" : "语义检索：关（纯关键词）");
    memoryEl.textContent = bits.join(" · ");
  }

  function renderState(state) {
    if (!state) return;
    sceneTitle.textContent = state.scene_title || "—";
    sceneLocation.textContent = state.location || "—";

    playersEl.innerHTML = "";
    (state.players || []).forEach(function (p) {
      var li = document.createElement("li");
      var bits = [p.name];
      if (p.character && p.character.cls) bits.push(p.character.cls);
      var attrs = attrsLine(p.character);
      if (attrs) bits.push(attrs);
      li.textContent = bits.join(" · ");
      playersEl.appendChild(li);
    });
    // 等待室要显示同一份队伍名单（房间快照里只有人数，没有名单），先缓存起来
    playersCache = state.players || [];
    renderWaitPlayers();
    syncCardFromState();

    flagsEl.innerHTML = "";
    var details = state.flag_details;
    if (!details || !details.length) {
      details = (state.flags || []).map(function (f) { return { id: f, label: f }; });
    }
    if (!details.length) {
      var empty = document.createElement("li");
      empty.textContent = "（暂无）";
      flagsEl.appendChild(empty);
    } else {
      details.forEach(function (f) {
        var li = document.createElement("li");
        li.textContent = f.label || f.id;
        if (f.id) li.title = f.id;
        flagsEl.appendChild(li);
      });
    }

    renderNpcs(state.npcs);
    renderMemory(state.memory);
  }

  function renderRecap(recap) {
    if (!recap) return;
    var div = document.createElement("div");
    div.className = "entry recap";

    var meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = "故事回顾 · DM 为你补课";

    var body = document.createElement("div");
    body.className = "body";

    if (recap.scene_path && recap.scene_path.length > 1) {
      body.appendChild(recapLine("行程", recap.scene_path.join(" → ")));
    }
    var flags = (recap.flags || []).map(function (f) {
      return (f && f.label) ? f.label : (f && f.id) ? f.id : String(f);
    });
    if (flags.length) {
      body.appendChild(recapLine("线索", flags.join("；")));
    }
    if (recap.recent && recap.recent.length) {
      body.appendChild(recapLine("最近动态", ""));
      recap.recent.forEach(function (h) {
        var p = document.createElement("div");
        p.className = "recap-history";
        p.textContent = "· " + h;
        body.appendChild(p);
      });
    }

    div.appendChild(meta);
    div.appendChild(body);
    logEl.appendChild(div);
    refreshThinking();
    logEl.scrollTop = logEl.scrollHeight;
  }

  function recapLine(label, text) {
    var p = document.createElement("div");
    var b = document.createElement("b");
    b.textContent = label + "：";
    p.appendChild(b);
    if (text) p.appendChild(document.createTextNode(text));
    return p;
  }

  /* ==================== 「正在思考」提示 ==================== */

  // 服务端在跑模型前会先发一条 thinking 事件；前端在点按「行动／助手建议」时也会
  // **先本地显示**（乐观 UI），这样即使服务端正忙、提示要晚一点到，玩家也立刻有反馈。
  // 内置引擎在 CPU 上跑一次行动要几十秒到几分钟，期间没有任何消息往来 —— 没有这个
  // 提示，玩家只会看到画面静止，然后以为掉线（这件事真的发生过）。
  var thinking = { dm: null, advisor: null }; // who -> 聊天框里的占位节点
  var thinkingSuggest = null; // 建议区里的助手占位（仅 advisor）

  function setBusy(on) {
    sendBtn.disabled = !!on;
    suggestBtn.disabled = !!on;
  }

  var THINK_LABEL = { dm: "DM 正在推理剧情…", advisor: "助手正在思考…" };

  // 占位提示一律挂在**聊天框（log）**里 —— 这是玩家视线所在。助手的提示另在建议区
  // 也放一份，免得建议区空白、看着像没点动。
  function showThinking(who, label) {
    who = who === "advisor" ? "advisor" : "dm";
    label = label || THINK_LABEL[who];
    if (!thinking[who]) {
      var el = document.createElement("div");
      el.className = "thinking " + (who === "advisor" ? "is-advisor" : "is-dm");
      var sp = document.createElement("span");
      sp.className = "spinner";
      el.appendChild(sp);
      el.appendChild(document.createTextNode(label));
      thinking[who] = el;
      logEl.appendChild(el);
    }
    if (who === "advisor" && !(thinkingSuggest && thinkingSuggest.parentNode)) {
      var el2 = document.createElement("div");
      el2.className = "thinking is-advisor";
      var sp2 = document.createElement("span");
      sp2.className = "spinner";
      el2.appendChild(sp2);
      el2.appendChild(document.createTextNode(label));
      thinkingSuggest = el2;
      suggestionsEl.innerHTML = "";
      suggestionsEl.appendChild(el2);
    }
    refreshThinking();
    setBusy(true);
  }

  function clearThinking(who) {
    who = who === "advisor" ? "advisor" : "dm";
    var el = thinking[who];
    if (el && el.parentNode) el.parentNode.removeChild(el);
    thinking[who] = null;
    if (who === "advisor" && thinkingSuggest) {
      if (thinkingSuggest.parentNode) thinkingSuggest.parentNode.removeChild(thinkingSuggest);
      thinkingSuggest = null;
    }
    if (!thinking.dm && !thinking.advisor) setBusy(false);
  }

  // 提示要一直待在日志末尾，别被随后到来的旁白／状态挤到中间去。
  function refreshThinking() {
    ["dm", "advisor"].forEach(function (w) {
      if (thinking[w] && thinking[w].parentNode) logEl.appendChild(thinking[w]);
    });
    if (logEl) logEl.scrollTop = logEl.scrollHeight;
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case "welcome":
        you = msg.you;
        roomCode.textContent = msg.room;
        if (msg.script) setScript(msg.script);
        isOwner = !!(msg.agents && msg.agents.owner === you);
        applyAgents(msg.agents);
        renderRoomSync(msg);
        renderNpcs(msg.npcs);
        renderMemory(msg.memory);
        // 房间快照：等待室画什么、顶栏显示什么，全看它
        renderRoomInfo(msg.room_info);
        // 角色卡：这个本要不要填卡（还没分析完就是 pending，稍后会有 character_schema）
        cardState = msg.card_state || null;
        renderCard();
        addEntry(
          "system",
          "",
          msg.late
            ? "你中途加入了房间 " + msg.room + "（你是 " + you + "），DM 正在为你补上之前的剧情…"
            : (isOwner
                ? "房间 " + msg.room + " 开好了（你是房主）—— 人齐了点「开始游戏」。"
                : "你加入了房间 " + msg.room + "（你是 " + you + "）—— 等房主开始。")
        );
        pushSavedModels();
        break;
      case "room":
        // 人数 / 房主 / 设置 / 阶段任一变化都会重发一次；等待室与顶栏跟着它刷新
        renderRoomInfo(msg.room);
        break;
      case "room_sync":
        // 房主换人时按人重发：新得的 DM 次级 prompt + 最新的房主是谁
        isOwner = !!(msg.agents && msg.agents.owner === you);
        applyAgents(msg.agents);
        renderRoomSync(msg);
        break;
      case "character_schema":
        // 剧本分析的结果（含「还在分析」与「分析失败」两个中间态）
        cardState = msg;
        cardSubmitted = false; // 新到的卡可能换了属性，草稿要按新卡重来
        cardDraft = {};
        renderCard();
        renderWaitPlayers();
        if (msg.status === "failed" && msg.error) {
          addEntry("system", "角色卡", "这个剧本的角色卡没分析出来：" + msg.error + "（房主可以在等待室里点「重试」）");
        }
        break;
      case "character_ok":
        cardSubmitted = true;
        cardMsg("已提交" + (msg.summary ? "：" + msg.summary : ""), "ok");
        renderCard();       // 按钮文案跟着变成「更新角色卡」
        renderWaitPlayers();
        break;
      case "character_rejected":
        cardMsg((msg.errors || ["没通过校验"]).join("；"), "bad");
        break;
      case "not_started":
        addEntry("system", "还不能行动", msg.message || "等房主开始这一局。");
        break;
      case "join_denied":
        // 服务端马上会关连接，onclose 里统一退回大厅并显示原因
        joinDeny = { code: msg.code, message: msg.message };
        break;
      case "kicked":
        // 服务端也会主动断连接；这里自己先关一次，界面立刻退回大厅、不用等对端
        closedNotice = { message: msg.message || "你被房主请出了房间。" };
        if (ws) { try { ws.close(); } catch (err) {} }
        break;
      case "room_closed":
        closedNotice = { message: msg.message || "房间已解散。" };
        if (ws) { try { ws.close(); } catch (err) {} }
        break;
      case "agent_status":
        applyAgents(msg.agents);
        if (msg.changed && msg.changed.length) {
          var names = msg.changed.map(function (k) {
            return k === "dm" ? "主机 DM" : (k === "assistant" ? "主机小助手" : "玩家小助手");
          });
          addEntry("system", "", "模型设置已更新：" + names.join("、"));
        }
        break;
      case "recap":
        renderRecap(msg.recap);
        break;
      case "system":
        addEntry("system", "", msg.text);
        break;
      case "narration":
        addEntry("dm", "DM", msg.text);
        break;
      case "dice":
        var label = msg.skill === "d20"
          ? msg.player + " 掷出了 d20 → " + msg.roll
          : msg.player + " 进行「" + msg.skill + "」检定："
            + diceMath(msg) + " vs DC" + msg.dc + " → " + (msg.success ? "成功" : "失败");
        addEntry(msg.success === false ? "dice fail" : "dice", "骰子", label);
        break;
      case "suggestions":
        clearThinking("advisor");
        suggestionsEl.innerHTML = "";
        thinkingSuggest = null;
        (msg.options || []).forEach(function (opt) {
          var chip = document.createElement("span");
          chip.className = "suggestion";
          chip.textContent = opt;
          chip.onclick = function () { sendAction(opt); };
          suggestionsEl.appendChild(chip);
        });
        var ag = msg.agent || {};
        if (ag.source && ag.source !== "llm") {
          var tag = document.createElement("span");
          tag.className = "suggest-src warn";
          tag.textContent = "本轮未生成";
          suggestionsEl.appendChild(tag);
        } else if (ag.source) {
          var tag2 = document.createElement("span");
          tag2.className = "suggest-src on";
          tag2.textContent = "小模型" + (ag.tools && ag.tools.length ? " · 用了 " + ag.tools.join("/") : "");
          suggestionsEl.appendChild(tag2);
        }
        break;
      case "state":
        renderState(msg.state);
        break;
      case "agent_note":
        renderAgentNote(msg);
        break;
      case "busy":
        if ((msg.who || "dm") === "advisor") clearThinking("advisor");
        addEntry("system", "请稍候", msg.message || "上一轮还在推理中，请等它结束。");
        break;
      case "thinking":
        if (msg.active) showThinking(msg.who || "dm", msg.label);
        else clearThinking(msg.who || "dm");
        break;
      case "error":
        addEntry("system", "错误", msg.message);
        clearThinking("dm");
        clearThinking("advisor");
        break;
    }
  }

  // 把「这一轮其实没有产出」摆到聊天框里。
  //
  // 2026-10-13 起不再有"脚本化兜底"这条路（用户拍板取消）：以前 DM 报错后会悄悄
  // 换成剧本化文案，玩家以为"DM 敷衍了一句"，而剧情其实原地没动。现在没有产出
  // 就是没有产出，界面必须明说。
  function renderAgentNote(msg) {
    if (!msg || msg.source === "llm") return; // 正常走模型，不必刷屏
    var who = msg.who === "advisor" ? "助手" : "DM";
    var reason = msg.stopped ? "（" + msg.stopped + "）" : "";
    var detail = msg.error ? "：" + msg.error : "";
    addEntry("system warn", "⚠ " + who + " 本轮没有生成" + reason,
      "这一轮没有产出任何内容，剧情不会推进。" + detail + (msg.hint ? "　" + msg.hint : ""));
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }

  /* ==================== 模型设置抽屉 ==================== */

  var SETTINGS_KEY = "rpgbar.models";
  // 内置引擎（RPGBarEngine）的界面开关。与三个槽位分开存：它是「同一份引擎、
  // 三个 Agent 共用」的全局设置，不属于任何一个槽位。think 默认 **false** ——
  // 关掉每次生成 4-6 秒，打开约 35-50 秒（裁定质量略好），默认选快的那个。
  // 「每次生成」= 一次引擎调用；玩家点一次行动要跑好几轮（DM 工具循环 + NPC 预备），
  // 实测一整轮：CPU 109-199 秒 / GPU 21-38 秒。
  // backend（cpu / gpu）决定加载哪一份库：cpu = rpg_engine.dll，
  // gpu = rpg_engine_cuda.dll（同一份 ABI 的另一份实现）。默认 cpu —— 与这个
  // 下拉框出现之前的行为完全一致。
  var ENGINE_KEY = "rpgbar.engine";
  var engineState = {
    enabled: false,
    backend: "cpu",
    think: false,
    context: 4096,
    max_tokens: 512
  };
  var engineStored = false; // 本机是否存过引擎设置（没存过就不干扰服务端 config.json）
  var PRESETS = {
    cloud: [
      { label: "DeepSeek", base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
      { label: "OpenAI", base_url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
      { label: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
      { label: "自定义", base_url: "", model: "" }
    ],
    local: [
      { label: "llama.cpp", base_url: "http://127.0.0.1:8080/v1", model: "local-model" },
      { label: "Ollama", base_url: "http://127.0.0.1:11434/v1", model: "qwen2.5:7b" },
      { label: "LM Studio", base_url: "http://127.0.0.1:1234/v1", model: "local-model" }
    ]
  };
  var SLOT_META = {
    dm: { title: "主机 DM · 大模型", role: "推进剧情、投骰判定、扮演 NPC。由房主配置，全房间共用。" },
    assistant: { title: "主机小助手 · 小模型", role: "为剧本外的 NPC 生成人物小传、算反应权重、滚动摘要。由房主配置，全房间共用。" },
    advisor: { title: "玩家小助手 · 小模型", role: "只读场景，给你 2-4 条行动建议。每位玩家配自己的，可指向本地模型。" }
  };
  var SLOT_KEYS = ["dm", "assistant", "advisor"];
  var slotState = {
    dm: { kind: "off", base_url: "", model: "", api_key: "", temperature: 0.8 },
    assistant: { kind: "off", base_url: "", model: "", api_key: "", temperature: 0.6 },
    advisor: { kind: "off", base_url: "", model: "", api_key: "", temperature: 0.7 }
  };
  var serverAgents = null; // 服务端回传的接线状态（永远不含 api_key）
  var isOwner = false;
  var ownerName = "";

  function loadSaved() {
    try {
      var raw = localStorage.getItem(SETTINGS_KEY);
      if (!raw) return;
      var obj = JSON.parse(raw);
      SLOT_KEYS.forEach(function (k) {
        if (obj && obj[k]) {
          var s = obj[k];
          slotState[k] = {
            kind: s.kind || "off",
            base_url: s.base_url || "",
            model: s.model || "",
            api_key: s.api_key || "",
            temperature: typeof s.temperature === "number" ? s.temperature : slotState[k].temperature
          };
        }
      });
    } catch (e) {}
  }

  function persistSaved() {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify({
        dm: slotState.dm,
        assistant: slotState.assistant,
        advisor: slotState.advisor
      }));
    } catch (e) {}
  }

  function loadEngineSaved() {
    try {
      var raw = localStorage.getItem(ENGINE_KEY);
      if (!raw) return;
      engineStored = true;
      var o = JSON.parse(raw) || {};
      if (typeof o.enabled === "boolean") engineState.enabled = o.enabled;
      if (o.backend === "cpu" || o.backend === "gpu") engineState.backend = o.backend;
      if (typeof o.think === "boolean") engineState.think = o.think;
      if (typeof o.context === "number" && o.context > 0) engineState.context = o.context;
      if (typeof o.max_tokens === "number" && o.max_tokens > 0) engineState.max_tokens = o.max_tokens;
    } catch (e) {}
  }

  function persistEngineSaved() {
    try { localStorage.setItem(ENGINE_KEY, JSON.stringify(engineState)); } catch (e) {}
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // ---- 内置引擎卡片（与三个槽位并列的一块，全局共用一份引擎）----

  function engineCheck(prop, label, hint, disabled) {
    var wrap = el("label", "check-field");
    var box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !!engineState[prop];
    box.disabled = !!disabled;
    box.onchange = function () {
      engineState[prop] = box.checked;
      renderEngineCard(); // think / context 那几行跟着启用来回显隐
    };
    wrap.appendChild(box);
    var body = el("span", "check-body");
    body.appendChild(el("span", "check-label", label));
    if (hint) body.appendChild(el("span", "check-hint", hint));
    wrap.appendChild(body);
    return wrap;
  }

  function engineField(prop, label, ph, disabled, type) {
    var wrap = el("div", "field");
    wrap.appendChild(el("label", "", label));
    var input = document.createElement("input");
    input.type = type || "text";
    input.placeholder = ph || "";
    input.value = engineState[prop];
    input.disabled = !!disabled;
    input.autocomplete = "off";
    input.oninput = function () {
      engineState[prop] = type === "number" ? (parseInt(input.value, 10) || 0) : input.value;
    };
    wrap.appendChild(input);
    return wrap;
  }

  // 下拉字段（目前只有「推理后端」用）。刻意用 <select> 而不是一排按钮或复选框：
  // 后端是个**开关式**选择（cpu 或 gpu 二选一），下拉在窄抽屉里最省地方，
  // 也不会被误认成"两个可以同时勾"的开关。
  function engineSelect(prop, label, options, hint, disabled) {
    var wrap = el("div", "field");
    wrap.appendChild(el("label", "", label));
    var sel = document.createElement("select");
    sel.disabled = !!disabled;
    options.forEach(function (o) {
      var opt = document.createElement("option");
      opt.value = o.value;
      opt.textContent = o.label;
      // **单项置灰**：后端不是偏好问题，而是"这台机器能不能用"。CUDA 版产物
      // 随包分发（没独显的机器上文件也在），所以"能不能选 GPU"必须看服务端
      // 的探测结果，而不是"文件在不在"（判据见 server/engine.py::probe_gpu）。
      if (o.disabled) opt.disabled = true;
      sel.appendChild(opt);
    });
    sel.value = engineState[prop] || options[0].value;
    sel.onchange = function () { engineState[prop] = sel.value; };
    wrap.appendChild(sel);
    if (hint) wrap.appendChild(el("span", "field-hint", hint));
    return wrap;
  }

  // 服务端报的 GPU 可用性（`{known, ok, reason}`）。`known=false` 表示还没拿到
  // 服务端状态 —— 那时**什么都不拦**（拿不到信息就把人拦在外面，比不拦更糟）。
  function engineGpuInfo() {
    var eng = serverAgents && serverAgents.engine;
    if (!eng || typeof eng.gpu_available !== "boolean") {
      return { known: false, ok: true, reason: "" };
    }
    return {
      known: true,
      ok: !!eng.gpu_available,
      reason: eng.gpu_reason || ""
    };
  }

  function buildEngineCard() {
    var card = el("div", "slot");
    card.id = "card-engine";

    var head = el("div", "slot-head");
    head.appendChild(el("span", "slot-title", "内置引擎（进程内直连）"));
    var badge = el("span", "slot-badge");
    badge.id = "badge-engine";
    paintEngineBadge(badge);
    head.appendChild(badge);
    card.appendChild(head);

    var locked = !isOwner;
    if (locked) {
      card.classList.add("locked");
      card.appendChild(el("p", "lock-note",
        "只有房主（" + (ownerName || "首位加入者") + "）可以修改内置引擎设置。"));
    }

    card.appendChild(engineCheck(
      "enabled", "启用内置引擎",
      "三个 Agent 都改走进程内引擎（覆盖上面的云 / 本地接线）。关掉则没有模型可用 —— 游戏将无法生成任何内容。",
      locked
    ));
    card.appendChild(engineCheck(
      "think", "让模型先思考再回答（think）",
      "关闭（推荐）：实测每次生成约 4–6 秒。开启：约 35–50 秒，裁定质量略好。（每次生成 = 一次引擎调用；一次行动要跑好几轮，总计 CPU 约 109–199 秒 / GPU 约 21–38 秒。）",
      locked || !engineState.enabled
    ));
    if (engineState.enabled) {
      var gpu = engineGpuInfo();
      // 本机用不了 GPU 时，把存着的 gpu 拉回 cpu：服务端本来也会在加载时退回
      // CPU，界面若不跟着回，就会出现"显示选的是 GPU、实际跑的是 CPU"。
      if (gpu.known && !gpu.ok && engineState.backend === "gpu") {
        engineState.backend = "cpu";
      }
      card.appendChild(engineSelect("backend", "推理后端", [
        { value: "cpu", label: "CPU（rpg_engine.dll）" },
        {
          value: "gpu",
          label: "GPU · CUDA（rpg_engine_cuda.dll）",
          disabled: gpu.known && !gpu.ok
        }
      ], gpu.known && !gpu.ok
        ? ("这台机器用不了 GPU 后端 —— " + (gpu.reason || "没检测到可用的 CUDA 设备")
           + "。所以 GPU 这一项是灰的，只能选 CPU。")
        : "GPU 走 CUDA 版产物（需 NVIDIA 卡）。检测不到可用设备时这一项会置灰。"
          + "实际用的是哪个后端以徽标为准。", locked));
      card.appendChild(engineField("context", "上下文长度（KV Cache 容量）", "4096", locked, "number"));
      card.appendChild(engineField("max_tokens", "单轮最多生成 token", "512", locked, "number"));
    }
    return card;
  }

  function renderEngineCard() {
    var host = document.getElementById("engine-card");
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(buildEngineCard());
  }

  function paintEngineBadge(badgeEl) {
    if (!badgeEl) return;
    var info = serverAgents && serverAgents.engine;
    var running = !!(info && info.enabled && info.ready);
    badgeEl.className = "slot-badge" + (running ? " on" : "");
    if (running) {
      // 后端要显出来，而且是**实际**那个：选了 GPU 却落回 CPU（没找到 CUDA 版
      // 产物）时，徽标必须说"实际 CPU" —— 否则玩家会以为自己在用 GPU，
      // 然后把"怎么还是这么慢"归因到别处。
      var be = engineBackendText(info);
      badgeEl.textContent = "已启用 · " + be + " · " + (info.model || "内置引擎")
        + (info.think ? " · think" : "");
    } else if (info && info.enabled && !info.ready) {
      badgeEl.textContent = "已启用，但引擎不可用"
        + (info.backend === "gpu" && !info.lib_backend ? "（没找到 GPU 库）" : "");
    } else if (engineState.enabled) {
      badgeEl.textContent = "待保存";
    } else {
      badgeEl.textContent = "未启用";
    }
  }

  // 后端的人话（供徽标用）。以服务端探测的**实际**库为准，不认界面上的选择 ——
  // 界面上的选择只是"请求"，`lib_backend` 才是事实。
  // ⚠️ 比较一律用原始码（`gpu` / `cpu`），别拿显示文案比 —— "GPU" !== "gpu"，
  // 那样写会让"回落"那句永远不触发，而徽标看起来一切正常（jsdom 那条断言抓过）。
  function engineBackendText(info) {
    info = info || {};
    var req = info.backend === "gpu" ? "gpu" : "cpu";
    var actual = (info.lib_backend === "gpu" || info.lib_backend === "cpu")
      ? info.lib_backend : "";
    if (!actual) return req === "gpu" ? "GPU" : "CPU";
    if (req === "gpu" && actual === "cpu") return "实际 CPU（GPU 库缺失）";
    return actual === "gpu" ? "GPU" : "CPU";
  }

  function enginePayload() {
    return {
      enabled: !!engineState.enabled,
      backend: engineState.backend === "gpu" ? "gpu" : "cpu",
      think: !!engineState.think,
      context: engineState.context,
      max_tokens: engineState.max_tokens
    };
  }

  // 根据服务端状态 + 本机保存值，构建一个槽位的表单
  function buildSlotCard(key) {
    var meta = SLOT_META[key];
    var s = slotState[key];
    var card = el("div", "slot");
    card.id = "card-" + key;

    var head = el("div", "slot-head");
    head.appendChild(el("span", "slot-title", meta.title));
    var badge = el("span", "slot-badge");
    badge.id = "badge-" + key;
    paintBadge(badge, key);
    head.appendChild(badge);
    card.appendChild(head);
    card.appendChild(el("p", "slot-role", meta.role));

    var locked = (key === "dm" || key === "assistant") && !isOwner;
    if (locked) {
      card.classList.add("locked");
      var note = key === "dm"
        ? "只有房主（" + (ownerName || "首位加入者") + "）可以修改主机 DM 模型。你可以照常配置自己的小助手。"
        : "只有房主（" + (ownerName || "首位加入者") + "）可以修改主机小助手模型。你可以照常配置自己的小助手。";
      card.appendChild(el("p", "lock-note", note));
    }

    // 模式切换
    var seg = el("div", "seg");
    [["off", "关闭"], ["cloud", "云 API"], ["local", "本地模型"]].forEach(function (pair) {
      var b = el("button", s.kind === pair[0] ? "active" : "", pair[1]);
      b.type = "button";
      b.disabled = locked;
      b.onclick = function () { switchKind(key, pair[0]); };
      seg.appendChild(b);
    });
    card.appendChild(seg);

    if (s.kind === "off") {
      card.appendChild(el("p", "slot-role", "未接入 —— 该 Agent 无法生成内容（必须有模型：本地引擎或云 API）。"));
      return card;
    }

    // 预设通道
    var presets = el("div", "presets");
    (PRESETS[s.kind] || []).forEach(function (p) {
      var chip = el("span", "preset", p.label);
      chip.onclick = function () {
        if (p.base_url) slotState[key].base_url = p.base_url;
        if (p.model) slotState[key].model = p.model;
        renderSlot(key);
      };
      presets.appendChild(chip);
    });
    card.appendChild(presets);

    card.appendChild(field(key, "base_url", "接口地址 Base URL", "https://api.deepseek.com/v1", locked, "text"));
    if (s.kind === "cloud") {
      card.appendChild(field(key, "api_key", "API Key", "sk-…（仅保存在本机浏览器）", locked, "password"));
    }
    card.appendChild(field(key, "model", "模型名", s.kind === "cloud" ? "deepseek-chat" : "local-model", locked, "text"));

    var actions = el("div", "slot-actions");
    var testBtn = el("button", "btn ghost", "测试连接");
    testBtn.type = "button";
    testBtn.onclick = function () { testSlot(key, testBtn); };
    actions.appendChild(testBtn);
    var result = el("span", "slot-result");
    result.id = "result-" + key;
    actions.appendChild(result);
    card.appendChild(actions);

    return card;
  }

  // 切换通道模式：若当前地址是「另一种通道的默认值」，则换成新通道的默认值
  function switchKind(key, kind) {
    var s = slotState[key];
    s.kind = kind;
    if (kind !== "off") {
      var others = [];
      ["cloud", "local"].forEach(function (k2) {
        if (k2 !== kind) {
          PRESETS[k2].forEach(function (p) { if (p.base_url) others.push(p.base_url); });
        }
      });
      var cur = (s.base_url || "").trim();
      if (!cur || others.indexOf(cur) >= 0) {
        s.base_url = PRESETS[kind][0].base_url;
        s.model = PRESETS[kind][0].model;
      }
    }
    renderSlot(key);
  }

  function field(key, prop, label, ph, disabled, type) {
    var wrap = el("div", "field");
    wrap.appendChild(el("label", "", label));
    var input = document.createElement("input");
    input.type = type || "text";
    input.placeholder = ph || "";
    input.value = slotState[key][prop] || "";
    input.disabled = !!disabled;
    input.autocomplete = "off";
    input.oninput = function () { slotState[key][prop] = input.value; };
    wrap.appendChild(input);
    return wrap;
  }

  function renderSlot(key) {
    var host = document.getElementById("slot-" + key);
    if (!host) return;
    host.innerHTML = "";
    host.appendChild(buildSlotCard(key));
  }

  function renderSlots() {
    SLOT_KEYS.forEach(function (k) { renderSlot(k); });
  }

  function badgeInfo(key) {
    var info = serverAgents && serverAgents[key];
    if (info && info.ready) {
      return { on: true, text: "已连接" + (info.model ? " · " + info.model : "") };
    }
    if (slotState[key].kind === "off") return { on: false, text: "未接入" };
    return { on: false, text: "待保存" };
  }

  function paintBadge(badgeEl, key) {
    if (!badgeEl) return;
    var b = badgeInfo(key);
    badgeEl.className = "slot-badge" + (b.on ? " on" : "");
    badgeEl.textContent = b.text;
  }

  function refreshBadge(key) {
    paintBadge(document.getElementById("badge-" + key), key);
  }

  function updateModelPill() {
    var pill = document.getElementById("model-pill");
    if (!pill) return;
    var dmOn = serverAgents && serverAgents.dm && serverAgents.dm.ready;
    var advOn = serverAgents && serverAgents.advisor && serverAgents.advisor.ready;
    if (dmOn && advOn) pill.textContent = "模型：DM + 助手";
    else if (dmOn) pill.textContent = "模型：仅 DM";
    else if (advOn) pill.textContent = "模型：仅助手";
    // 一个模型都没接 = 这局玩不了（2026-10-13 起没有脚本化兜底）。胶囊也照这个说。
    else pill.textContent = "模型：未接入";
    pill.className = "chip model-pill" + (dmOn ? " on" : "");
  }

  function applyAgents(agents) {
    if (!agents) return;
    var wasOwner = isOwner;
    serverAgents = agents;
    if (typeof agents.owner === "string") ownerName = agents.owner;
    isOwner = typeof agents.owner === "string" && agents.owner === you;
    SLOT_KEYS.forEach(function (k) {
      var info = agents[k];
      if (info && info.base_url && !slotState[k].base_url) slotState[k].base_url = info.base_url;
      if (info && info.model && !slotState[k].model) slotState[k].model = info.model;
    });
    SLOT_KEYS.forEach(refreshBadge);
    updateModelPill();
    paintEngineBadge(document.getElementById("badge-engine"));
    // 房主身份变化（例如服务端换人/重连）时，DM 卡片的可编辑性跟着变
    if (wasOwner !== isOwner) rerenderSlotsIfOpen();
  }

  function rerenderSlotsIfOpen() {
    var drawer = document.getElementById("settings-drawer");
    if (drawer && !drawer.classList.contains("hidden")) {
      renderSlots();
      renderEngineCard();  // 房主身份变化会影响引擎卡片的可编辑性
      renderScriptCard(); // 房主身份变化会影响「设为活动剧本」的可点性
    }
  }

  function slotPayload(key) {
    var s = slotState[key];
    var p = { kind: s.kind, temperature: s.temperature };
    if (s.kind !== "off") {
      p.base_url = (s.base_url || "").trim();
      p.model = (s.model || "").trim();
    }
    // 云 API 才带 key；传空串表示清除（本地模型不需要）
    if (s.kind === "cloud") p.api_key = s.api_key || "";
    else if (s.kind === "off") p.api_key = "";
    return p;
  }

  function sendConfigure() {
    var payload = { type: "configure" };
    if (isOwner) {
      payload.dm = slotPayload("dm");            // 主机 DM 仅房主可改
      payload.assistant = slotPayload("assistant"); // 主机小助手（NPC/摘要）同样仅房主可改
      payload.engine = enginePayload();           // 内置引擎（含 think）同样仅房主可改
    }
    payload.advisor = slotPayload("advisor");
    send(payload);
  }

  function saveSettings() {
    persistSaved();
    persistEngineSaved();
    engineStored = true;
    sendConfigure();
  }

  // 加入房间后，把本机已保存的模型设置自动推给服务端（DM 仅房主可推）
  function pushSavedModels() {
    loadSaved();
    loadEngineSaved();
    var payload = { type: "configure" };
    var has = false;
    if (isOwner && slotState.dm.kind !== "off") {
      payload.dm = slotPayload("dm");
      has = true;
    }
    if (isOwner && slotState.assistant.kind !== "off") {
      payload.assistant = slotPayload("assistant");
      has = true;
    }
    // 内置引擎只在**本机存过**设置时才推：没存过说明用户没在界面上动过它，
    // 那就该让服务端 config.json 的配置说话，不能被前端的默认值（未启用）盖掉。
    if (isOwner && engineStored) {
      payload.engine = enginePayload();
      has = true;
    }
    if (slotState.advisor.kind !== "off") {
      payload.advisor = slotPayload("advisor");
      has = true;
    }
    if (has) send(payload);
  }

  function testSlot(key, btn) {
    var s = slotState[key];
    var out = document.getElementById("result-" + key);
    if (!out) return;
    if (s.kind === "off") {
      out.className = "slot-result";
      out.textContent = "当前为「关闭」，无需测试。";
      return;
    }
    var payload = slotPayload(key);
    if (s.kind === "cloud" && !payload.api_key) {
      out.className = "slot-result err";
      out.textContent = "云 API 需要填写 API Key。";
      return;
    }
    btn.disabled = true;
    var old = btn.textContent;
    btn.textContent = "测试中…";
    out.className = "slot-result";
    out.textContent = "正在连接 " + payload.base_url + " …";
    fetch("/api/models/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slot: payload, probe_tools: true })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.ok) {
          out.className = "slot-result ok";
          var extra = "";
          if (d.tool_calling === true) extra = " · 支持工具调用（走标准 Agent 循环）";
          else if (d.tool_calling === false) extra = " · 不支持工具调用（改用单轮 JSON 协议）";
          out.textContent = "连接成功（" + d.elapsed_ms + "ms）：" + (d.reply || "就绪") + extra;
        } else {
          out.className = "slot-result err";
          out.textContent = "失败：" + (d.error || "未知错误");
        }
      })
      .catch(function (e) {
        out.className = "slot-result err";
        out.textContent = "请求失败：" + e;
      })
      .then(function () {
        btn.disabled = false;
        btn.textContent = old;
      });
  }

  /* ==================== 剧本卡片：读取 / 切片 / 次级 prompt ==================== */

  var scriptInfo = null;      // GET /api/scripts 的结果
  var scriptPreview = null;   // 最近一次 inspect/load 的结果
  var scriptLast = { text: "", cls: "" }; // 最近一次提示（重建卡片后要能复原）
  var scriptListLoading = false;

  function loadScripts(done) {
    if (scriptListLoading) return;
    scriptListLoading = true;
    fetch("/api/scripts")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        scriptInfo = d;
        renderScriptCard();
        if (done) done(null, d);
      })
      .catch(function (e) {
        if (done) done(e);
      })
      .then(function () { scriptListLoading = false; });
  }

  function scriptPayload() {
    var pathEl = document.getElementById("script-path");
    var contentEl = document.getElementById("script-content");
    var nameEl = document.getElementById("script-name");
    var content = contentEl ? contentEl.value.trim() : "";
    if (content) {
      return { content: content, name: (nameEl && nameEl.value.trim()) || "粘贴的剧本" };
    }
    var path = pathEl ? pathEl.value.trim() : "";
    if (path) return { path: path };
    return null;
  }

  function scriptResult(text, cls) {
    scriptLast = { text: text || "", cls: cls || "" };
    paintScriptResult();
  }

  function paintScriptResult() {
    var out = document.getElementById("script-result");
    if (!out) return;
    out.className = "slot-result" + (scriptLast.cls ? " " + scriptLast.cls : "");
    out.textContent = scriptLast.text;
  }

  function showScriptPreview(info) {
    scriptPreview = info;
    var promptEl = document.getElementById("script-prompt");
    if (promptEl) promptEl.textContent = info.script_prompt || "（无）";
    var pubEl = document.getElementById("script-prompt-public");
    if (pubEl) pubEl.textContent = info.script_prompt_public || "（无）";
    var host = document.getElementById("script-slots");
    if (host) {
      host.innerHTML = "";
      (info.chunks || []).forEach(function (c) {
        var s = el("span", "script-slot");
        var b = el("b", "", c.id);
        s.appendChild(b);
        s.appendChild(document.createTextNode(" " + (c.title || "") + " · " + c.chars + "字"));
        s.title = c.preview || "";
        host.appendChild(s);
      });
    }
  }

  function renderScriptCard() {
    var host = document.getElementById("script-card");
    if (!host) return;
    host.classList.add("script-card");
    host.innerHTML = "";

    var head = el("div", "slot-head");
    head.appendChild(el("span", "slot-title", "剧本 · 读取与切片"));
    var badge = el("span", "slot-badge on");
    badge.textContent = scriptInfo && scriptInfo.active_title ? scriptInfo.active_title : "—";
    head.appendChild(badge);
    host.appendChild(head);
    host.appendChild(el("p", "slot-role",
      "读取剧本文件 → 自动切片 → 生成排在 System prompt 之后的「次级 prompt」。"
      + "System prompt 只定义两个 Agent 各自的职责。"));

    if (!isOwner) {
      var note = el("p", "lock-note",
        "只有房主（" + (ownerName || "首位加入者") + "）可以切换活动剧本；你可以读取与预览。");
      host.appendChild(note);
    }

    // 可用剧本列表
    var list = el("div", "script-list");
    var scripts = (scriptInfo && scriptInfo.scripts) || [];
    if (!scripts.length) {
      list.appendChild(el("div", "slot-role", "正在读取剧本列表…"));
    }
    scripts.forEach(function (it) {
      var row = el("div", "script-item" + (it.path === (scriptInfo && scriptInfo.active) ? " active" : ""));
      var left = el("div");
      left.appendChild(el("div", "name", (it.mode_label ? it.mode_label + " · " : "") + (it.title || it.file)));
      var meta = [it.rules || "通用规则", it.structured ? "已切分场景图" : "文本切片"]
        .concat(it.error ? [it.error] : [it.scenes + " 场景", it.chunks + " 切片"])
        .join(" · ");
      left.appendChild(el("div", "meta", meta));
      if (it.description) row.title = it.description;
      row.appendChild(left);
      row.appendChild(el("span", "tag", it.path === (scriptInfo && scriptInfo.active) ? "当前" : "可载入"));
      row.onclick = function () {
        var p = document.getElementById("script-path");
        if (p) p.value = it.path;
        inspectScript();
      };
      list.appendChild(row);
    });
    host.appendChild(list);

    host.appendChild(field2("script-path", "读取剧本文件（限 scripts/ 目录，.json / .md / .txt）", "例如 totsk_l1.json"));

    // 切片预览
    var slots = el("div", "script-slots");
    slots.id = "script-slots";
    host.appendChild(slots);

    var actions = el("div", "slot-actions");
    var inspectBtn = el("button", "btn ghost", "读取并切片");
    inspectBtn.type = "button";
    inspectBtn.onclick = function () { inspectScript(); };
    actions.appendChild(inspectBtn);

    var loadBtn = el("button", "btn primary", "设为活动剧本");
    loadBtn.type = "button";
    loadBtn.disabled = !isOwner;
    if (!isOwner) loadBtn.title = "只有房主可以切换";
    loadBtn.onclick = function () { loadScript(); };
    actions.appendChild(loadBtn);
    var res = el("span", "slot-result");
    res.id = "script-result";
    actions.appendChild(res);
    host.appendChild(actions);

    // 粘贴内容
    var det = document.createElement("details");
    det.appendChild(el("summary", "", "或直接粘贴剧本内容（自动识别 JSON / Markdown）"));
    det.appendChild(field2("script-name", "剧本名（可选）", "例如 雾港塔"));
    var ta = document.createElement("textarea");
    ta.id = "script-content";
    ta.placeholder = "# 第一章 迷雾渡口\n夜色里渡船靠岸……";
    ta.rows = 5;
    det.appendChild(ta);
    host.appendChild(det);

    // 次级 prompt
    var det2 = document.createElement("details");
    det2.appendChild(el("summary", "", "查看生成的次级 prompt"));
    var pre = el("pre", "script-prompt");
    pre.id = "script-prompt";
    pre.textContent = "（尚未读取）";
    det2.appendChild(pre);
    var pre2 = el("pre", "script-prompt");
    pre2.id = "script-prompt-public";
    pre2.textContent = "（尚未读取）";
    det2.appendChild(pre2);
    host.appendChild(det2);

    if (scriptPreview) showScriptPreview(scriptPreview);
    paintScriptResult(); // 卡片被重建后，恢复上一次的提示文字
  }

  // 通用输入框（不绑定到模型槽位）
  function field2(id, label, ph) {
    var wrap = el("div", "field");
    wrap.appendChild(el("label", "", label));
    var input = document.createElement("input");
    input.type = "text";
    input.id = id;
    input.placeholder = ph || "";
    input.autocomplete = "off";
    wrap.appendChild(input);
    return wrap;
  }

  function inspectScript() {
    var payload = scriptPayload();
    if (!payload) {
      scriptResult("请先选择剧本、填入文件名，或粘贴剧本内容。", "err");
      return;
    }
    scriptResult("正在读取并切片…", "");
    fetch("/api/scripts/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) {
          scriptResult("读取失败：" + ((d && d.error) || "未知错误"), "err");
          return;
        }
        showScriptPreview(d);
        var warn = (d.warnings || []).length ? "；提示：" + d.warnings.join("；") : "";
        scriptResult(
          "《" + d.title + "》· " + d.chunks.length + " 个切片 · " + d.scenes + " 场景" + warn,
          warn ? "warn" : "ok"
        );
      })
      .catch(function (e) { scriptResult("请求失败：" + e, "err"); });
  }

  function loadScript() {
    var payload = scriptPayload();
    if (!payload) {
      scriptResult("请先选择剧本、填入文件名，或粘贴剧本内容。", "err");
      return;
    }
    scriptResult("正在载入…", "");
    fetch("/api/scripts/load", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) {
          scriptResult("载入失败：" + ((d && d.error) || "未知错误"), "err");
          return;
        }
        showScriptPreview(d);
        scriptResult("已切换活动剧本：《" + d.title + "》。" + (d.note || ""), "ok");
        addEntry("system", "", "活动剧本已切换为《" + d.title + "》（新开的房间生效）。");
        loadScripts();   // 重建列表（「当前」标签要跟着换）；提示文字由 scriptLast 复原
        loadVersion();
      })
      .catch(function (e) { scriptResult("请求失败：" + e, "err"); });
  }

  function openSettings() {
    var drawer = document.getElementById("settings-drawer");
    var overlay = document.getElementById("settings-overlay");
    if (!drawer) return;
    loadSaved();
    loadEngineSaved();
    renderSlots();
    renderEngineCard();
    renderScriptCard();
    loadScripts();
    hideMsg();
    overlay.classList.remove("hidden");
    drawer.classList.remove("hidden");
  }

  function closeSettings() {
    var drawer = document.getElementById("settings-drawer");
    var overlay = document.getElementById("settings-overlay");
    if (drawer) drawer.classList.add("hidden");
    if (overlay) overlay.classList.add("hidden");
  }

  function showMsg(text, cls) {
    var box = document.getElementById("settings-msg");
    if (!box) return;
    box.className = "settings-msg" + (cls ? " " + cls : "");
    box.textContent = text;
    box.classList.remove("hidden");
  }

  function hideMsg() {
    var box = document.getElementById("settings-msg");
    if (box) box.classList.add("hidden");
  }

  (function initSettings() {
    var openBtn = document.getElementById("settings-btn");
    var closeBtn = document.getElementById("settings-close");
    var overlay = document.getElementById("settings-overlay");
    var saveBtn = document.getElementById("settings-save");
    var clearBtn = document.getElementById("settings-clear");
    if (openBtn) openBtn.onclick = openSettings;
    if (closeBtn) closeBtn.onclick = closeSettings;
    if (overlay) overlay.onclick = closeSettings;
    if (saveBtn) saveBtn.onclick = function () {
      saveSettings();
      showMsg("已提交，正在重建 Agent…", "");
      setTimeout(function () {
        showMsg("已保存到本机浏览器，并推送到当前房间。", "ok");
      }, 400);
    };
    if (clearBtn) clearBtn.onclick = function () {
      try { localStorage.removeItem(SETTINGS_KEY); } catch (e) {}
      try { localStorage.removeItem(ENGINE_KEY); } catch (e) {}
      engineStored = false;
      engineState = { enabled: false, backend: "cpu", think: false, context: 4096, max_tokens: 512 };
      slotState.dm = { kind: "off", base_url: "", model: "", api_key: "", temperature: 0.8 };
      slotState.assistant = { kind: "off", base_url: "", model: "", api_key: "", temperature: 0.6 };
      slotState.advisor = { kind: "off", base_url: "", model: "", api_key: "", temperature: 0.7 };
      sendConfigure(); // 通知服务端清空槽位（没有模型就没有产出，不再有脚本化兜底）
      renderSlots();
      renderEngineCard();
      showMsg("已清除本机设置 —— 两个 Agent 现在没有模型可用。", "ok");
    };
    loadSaved();
  })();

  function sendAction(text) {
    text = (text || "").trim();
    if (!text) return;
    send({ type: "action", text: text });
    addEntry("player", you || "我", text);
    actionInput.value = "";
    showThinking("dm"); // 乐观显示：不等服务端回 thinking，点完立刻有反馈
  }

  // ---- 加入页：只收名字与职业，然后进大厅（不再直接建房/进房）----
  joinForm.addEventListener("submit", function (e) {
    e.preventDefault();
    me.name = document.getElementById("join-name").value.trim() || "冒险者";
    me.cls = document.getElementById("join-class").value;
    joinMsg("");
    openLobby();
  });

  document.getElementById("join-direct").addEventListener("click", function () {
    if (!me.name) {
      joinMsg("先在下面填个名字（上面「进入大厅」那一栏）。");
      return;
    }
    var code = document.getElementById("join-room").value.trim().toUpperCase();
    if (!code) {
      joinMsg("要先填房间码 —— 想开新房请点上面的「进入大厅」。");
      return;
    }
    joinMsg("");
    connectGame({ room: code, password: document.getElementById("join-password").value });
  });

  // ---- 连服务器并进房 ----
  // 建房与加入走**同一条路**：都先连 /ws，再发一条 join —— 区别只在 join 里带不带
  // create（建房参数）。认不认由服务端说了算（密码 / 容量 / 房号不存在都归它判）。
  function connectGame(opts) {
    opts = opts || {};
    stopLobbyPolling();
    joinDeny = null;
    closedNotice = null;
    lastJoin = {
      room: opts.room || "",
      password: opts.password || "",
      create: opts.create || null,
      script: opts.script || ""
    };
    var proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(proto + "://" + location.host + "/ws");

    ws.onopen = function () {
      var payload = {
        type: "join",
        name: me.name,
        room: lastJoin.room,
        password: lastJoin.password,
        script: lastJoin.script,
        character: { name: me.name, cls: me.cls, hp: 10, skills: {} }
      };
      if (lastJoin.create) payload.create = lastJoin.create;
      send(payload);
    };
    ws.onmessage = function (ev) {
      try { handleMessage(JSON.parse(ev.data)); } catch (err) {}
    };
    ws.onclose = function () {
      ws = null;
      clearThinking("dm");
      clearThinking("advisor");
      setBusy(false);
      if (joinDeny || closedNotice) {
        // 被拒 / 被踢 / 房间被解散：退回大厅，把原因摆在显眼处
        var why = joinDeny || closedNotice || {};
        returnToLobby(why.message || "进不去这个房间。");
        joinDeny = null;
        closedNotice = null;
        return;
      }
      if (!gameScreen.classList.contains("hidden")) {
        returnToLobby("和服务器断开了 —— 重新挑个房间，或者自己开一间。");
      }
    };
    ws.onerror = function () {
      clearThinking("dm");
      clearThinking("advisor");
    };

    // 先进游戏屏（等待室会盖在上面）：这样"进去了"立刻有反馈；真被拒的话
    // 服务端马上关连接，onclose 会把人送回大厅并说明原因（闪一下而已）。
    joinScreen.classList.add("hidden");
    lobbyScreen.classList.add("hidden");
    gameScreen.classList.remove("hidden");
    actionInput.focus();
  }

  sendBtn.addEventListener("click", function () { sendAction(actionInput.value); });
  actionInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { sendAction(actionInput.value); }
  });
  suggestBtn.addEventListener("click", function () {
    send({ type: "suggest" });
    showThinking("advisor"); // 乐观显示：点完立刻出现「助手正在思考…」
  });
  rollBtn.addEventListener("click", function () { send({ type: "roll" }); });
  copyBtn.addEventListener("click", function () {
    var code = roomCode.textContent;
    if (!code || code === "—") return;
    if (navigator.clipboard) {
      navigator.clipboard.writeText(code).then(function () {
        copyBtn.textContent = "已复制";
        setTimeout(function () { copyBtn.textContent = "复制房间码"; }, 1500);
      });
    }
  });

  /* ==================== 大厅 ==================== */

  function showScreen(which) {
    joinScreen.classList.toggle("hidden", which !== "join");
    lobbyScreen.classList.toggle("hidden", which !== "lobby");
    gameScreen.classList.toggle("hidden", which !== "game");
  }

  function joinMsg(text) {
    if (!joinMsgEl) return;
    joinMsgEl.textContent = text || "";
  }

  function lobbyMsg(text, ok) {
    var el = document.getElementById("lobby-msg");
    if (!el) return;
    el.textContent = text || "";
    el.classList.toggle("hidden", !text);
    el.classList.toggle("on", !!ok);
  }

  function openLobby() {
    showScreen("lobby");
    var chip = document.getElementById("lobby-me");
    if (chip) chip.textContent = me.name ? (me.name + " · " + me.cls) : "";
    loadLobbyScripts();
    loadRooms();
    startLobbyPolling();
  }

  function startLobbyPolling() {
    stopLobbyPolling();
    // 大厅列表 5 秒刷一次（别处开/关房间能很快反映出来）。进了房间就停 ——
    // 进房之后房间信息走 WebSocket 推送，不用再轮询 HTTP。
    lobbyTimer = setInterval(function () {
      if (lobbyScreen.classList.contains("hidden")) return;
      loadRooms();
    }, 5000);
  }

  function stopLobbyPolling() {
    if (lobbyTimer) { clearInterval(lobbyTimer); lobbyTimer = null; }
  }

  function loadRooms() {
    return fetch("/api/rooms")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var list = (d && d.rooms) || [];
        renderRooms(list);
        var act = document.getElementById("lobby-active");
        if (act && d && d.active_title) act.textContent = d.active_title;
        var empty = document.getElementById("lobby-empty");
        if (empty) empty.classList.toggle("hidden", list.length > 0);
      })
      .catch(function () {});
  }

  function roomTag(text, cls) {
    var span = document.createElement("span");
    span.className = "room-tag" + (cls ? " " + cls : "");
    span.textContent = text;
    return span;
  }

  function renderRooms(list) {
    var ul = document.getElementById("lobby-rooms");
    if (!ul) return;
    var countEl = document.getElementById("lobby-count");
    if (countEl) countEl.textContent = String(list.length);
    ul.innerHTML = "";
    list.forEach(function (r) {
      var li = document.createElement("li");
      li.className = "room-item" + (r.phase === "playing" ? " is-playing" : "");

      var main = document.createElement("div");
      main.className = "room-main";
      var title = document.createElement("div");
      title.className = "room-title";
      title.textContent = r.name || r.script || r.code;
      main.appendChild(title);

      if (r.note) {
        var note = document.createElement("div");
        note.className = "room-note";
        note.textContent = r.note;
        main.appendChild(note);
      }

      var meta = document.createElement("div");
      meta.className = "room-meta";
      meta.appendChild(roomTag("剧本：" + (r.script || "—")));
      meta.appendChild(roomTag("房主：" + (r.owner || "—"), "owner"));
      meta.appendChild(roomTag(r.online + "/" + r.max_players + " 人"));
      meta.appendChild(roomTag(r.phase_label || "等待中", r.phase === "playing" ? "playing" : "waiting"));
      meta.appendChild(roomTag("房间码 " + r.code));
      if (r.has_password) meta.appendChild(roomTag("需密码", "locked"));
      var full = r.online >= r.max_players;
      if (full) meta.appendChild(roomTag("已满", "full"));
      main.appendChild(meta);
      li.appendChild(main);

      var join = document.createElement("button");
      join.className = "btn primary room-join";
      join.textContent = full ? "已满" : (r.phase === "playing" ? "中途加入" : "进入");
      join.disabled = full;
      join.onclick = function () { joinRoomFromLobby(r); };
      li.appendChild(join);
      ul.appendChild(li);
    });
  }

  function joinRoomFromLobby(r) {
    if (!me.name) { showScreen("join"); joinMsg("先填个名字。"); return; }
    var passEl = document.getElementById("lobby-pass");
    var pw = passEl ? passEl.value : "";
    if (r.has_password && !pw) {
      lobbyMsg("「" + (r.name || r.code) + "」设了密码 —— 在上面「进房密码」里填一下再点进入。");
      if (passEl) passEl.focus();
      return;
    }
    lobbyMsg("");
    connectGame({ room: r.code, password: pw });
  }

  function createFromForm(e) {
    e.preventDefault();
    if (!me.name) { showScreen("join"); joinMsg("先填个名字。"); return; }
    var val = function (id) { var el = document.getElementById(id); return el ? el.value : ""; };
    var payload = {
      name: val("create-name").trim(),
      note: val("create-note").trim(),
      max_players: val("create-max"),
      visibility: val("create-visibility"),
      password: val("create-password"),
      script: val("create-script")
    };
    lobbyMsg("");
    connectGame({ room: "", create: payload, script: payload.script });
  }

  // 从游戏屏退回大厅（被拒 / 被踢 / 房间解散 / 断线都会走这里）。
  // 顺手把上一局的残留清干净，免得串到下一间房。
  function returnToLobby(message) {
    stopLobbyPolling();
    if (ws) { try { ws.close(); } catch (err) {} ws = null; }
    roomInfo = null;
    playersCache = [];
    waitingRoom.classList.add("hidden");
    logEl.innerHTML = "";
    playersEl.innerHTML = "";
    flagsEl.innerHTML = "";
    npcsEl.innerHTML = "";
    suggestionsEl.innerHTML = "";
    memoryEl.textContent = "（尚未开局）";
    actionInput.value = "";
    clearThinking("dm");
    clearThinking("advisor");
    setBusy(false);
    showScreen("lobby");
    if (message) lobbyMsg(message);
    loadRooms();
    startLobbyPolling();
  }

  /* ==================== 等待室 ==================== */

  function renderRoomInfo(info) {
    if (!info) return;
    roomInfo = info;
    roomCode.textContent = info.code || "—";

    var set = function (id, text) {
      var el = document.getElementById(id);
      if (el) el.textContent = text;
    };
    set("room-name", info.name || "—");
    set("room-players", (info.online || 0) + "/" + (info.max_players || 0));
    set("wait-title", info.name || "—");
    set("wait-code", info.code || "—");
    set("wait-script", info.script || "—");
    set("wait-count", (info.online || 0) + "/" + (info.max_players || 0));
    set("wait-owner", info.owner || "—");
    set("wait-pass", info.has_password ? "有" : "无");
    set("wait-phase", info.phase_label || "等待中");
    set("wait-vis", info.visibility === "private" ? "私密" : "公开");

    var note = document.getElementById("wait-note");
    if (note) {
      note.textContent = info.note || "";
      note.classList.toggle("hidden", !info.note);
    }

    var waiting = info.phase !== "playing";
    // 开局前没有剧情，场景 / 地点两个胶囊没有意义，先收起来
    [sceneTitle, sceneLocation].forEach(function (el) {
      if (el && el.parentNode) el.parentNode.classList.toggle("hidden", waiting);
    });

    isOwner = !!(info.owner && info.owner === you);
    var hostBox = document.getElementById("wait-host");
    if (hostBox) hostBox.classList.toggle("hidden", !isOwner);

    set("wait-hint", isOwner
      ? "人都到齐了就点「开始游戏」。开局前谁都不能行动（可以先去「⚙ 设置」把模型接上）。"
      : "等房主点「开始」。开局前谁都不能行动 —— 可以先去「⚙ 设置」把自己那份小助手模型接好。");

    // 把设置表单的初值同步一次。仅在折叠着的时候填 —— 房主正打字时别冲掉。
    if (isOwner) {
      var box = document.querySelector(".wait-settings");
      if (!box || !box.open) {
        var fill = function (id, v) { var el = document.getElementById(id); if (el) el.value = v; };
        fill("set-name", info.name || "");
        fill("set-note", info.note || "");
        fill("set-max", String(info.max_players || 4));
        var vis = document.getElementById("set-visibility");
        if (vis) vis.value = info.visibility || "public";
      }
    }

    renderWaitPlayers();
    waitingRoom.classList.toggle("hidden", !waiting);
  }

  function renderWaitPlayers() {
    var ul = document.getElementById("wait-players");
    if (!ul) return;
    ul.innerHTML = "";
    if (!playersCache.length) {
      var empty = document.createElement("li");
      empty.textContent = "（还没有人）";
      ul.appendChild(empty);
      return;
    }
    var owner = roomInfo ? roomInfo.owner : "";
    playersCache.forEach(function (p) {
      var row = document.createElement("li");
      var name = document.createElement("span");
      name.textContent = p.name;
      row.appendChild(name);
      if (p.character && p.character.cls) {
        var cls = document.createElement("span");
        cls.className = "cls";
        cls.textContent = p.character.cls;
        row.appendChild(cls);
      }
      if (cardState && cardState.needs_card) {
        // 房主靠这一列判断「人齐了但还有谁没填卡」，不用挨个问
        var filled = !!(p.character && p.character.attrs && Object.keys(p.character.attrs).length);
        var tag = document.createElement("span");
        tag.className = "card-tag" + (filled ? " done" : "");
        tag.textContent = filled ? "已填卡" : "未填卡";
        row.appendChild(tag);
      }
      if (p.name === owner) {
        var flag = document.createElement("span");
        flag.className = "host-flag";
        flag.textContent = "房主";
        row.appendChild(flag);
      } else if (isOwner) {
        // 房主对别人多两个动作：请出 / 设为房主（转让）
        [["kick", "请出"], ["transfer", "设为房主"]].forEach(function (pair) {
          var b = document.createElement("button");
          b.className = "btn ghost";
          b.style.fontSize = "12px";
          b.style.padding = "4px 10px";
          b.textContent = pair[1];
          b.onclick = function () { send({ type: pair[0], target: p.name }); };
          row.appendChild(b);
        });
      }
      ul.appendChild(row);
    });
  }

  // ---------------------------------------------------------------- 角色卡
  // 全部逻辑围绕两个东西：服务端下发的 cardState（权威形状）与本地 cardDraft（我的草稿）。
  // 加减号只是「帮我把数字拼对」，真正的校验在服务端 —— 改 JS 发任意数字过去也会被拒。

  function cardMsg(text, cls) {
    var el = document.getElementById("card-msg");
    if (!el) return;
    el.textContent = text || "";
    el.className = "hint small" + (cls ? " " + cls : "");
  }

  // 队伍名单里那行属性摘要（没卡 / 没填就返回空串，界面一个字都不多）
  function attrsLine(character) {
    var card = cardState && cardState.card;
    if (!card || !card.required) return "";
    var attrs = (character && character.attrs) || {};
    var parts = [];
    (card.attributes || []).forEach(function (a) {
      if (typeof attrs[a.key] === "number") parts.push(a.key + " " + attrs[a.key]);
    });
    return parts.join(" · ");
  }

  function cardUsed() {
    var card = cardState && cardState.card;
    if (!card) return 0;
    var used = 0;
    (card.attributes || []).forEach(function (a) {
      var v = typeof cardDraft[a.key] === "number" ? cardDraft[a.key] : a.base;
      used += Math.max(0, v - a.base);
    });
    return used;
  }

  function cardLeft() {
    var card = cardState && cardState.card;
    return card ? Math.max(0, card.total_points - cardUsed()) : 0;
  }

  // 草稿按当前的卡对齐一次：缺的补 base，越界的夹回区间
  function ensureCardDraft() {
    var card = cardState && cardState.card;
    if (!card) return;
    var next = {};
    (card.attributes || []).forEach(function (a) {
      var v = cardDraft[a.key];
      next[a.key] = typeof v === "number" ? Math.max(a.base, Math.min(a.max, v)) : a.base;
    });
    cardDraft = next;
  }

  function renderCard() {
    var panel = document.getElementById("card-panel");
    if (!panel) return;
    var cs = cardState || {};
    var show = !!(cs.status === "ready" && cs.needs_card && cs.card);
    panel.classList.toggle("hidden", !show);

    // 分析中 / 分析失败：只留一行状态，不占版面。失败时给个「重试分析」。
    var statusEl = document.getElementById("card-status");
    var statusText = document.getElementById("card-status-text");
    var retry = document.getElementById("card-retry");
    var pending = cs.status === "pending";
    var failed = cs.status === "failed";
    if (statusEl) statusEl.classList.toggle("hidden", !(pending || failed));
    if (statusText) {
      statusText.textContent = failed
        ? "角色卡没分析出来：" + (cs.error || "这一轮没有产出") + "（把 DM 模型接上后再试一次）"
        : "正在分析剧本，看这个本要不要角色卡…";
    }
    if (retry) retry.classList.toggle("hidden", !failed);
    if (!show) return;

    ensureCardDraft();
    var card = cardState.card;

    var budget = document.getElementById("card-budget");
    if (budget) budget.textContent = "剩余 " + cardLeft() + " / " + card.total_points + " 点";
    var reason = document.getElementById("card-reason");
    if (reason) reason.textContent = card.reason || "";

    var box = document.getElementById("card-rows");
    box.innerHTML = "";
    (card.attributes || []).forEach(function (a) {
      box.appendChild(cardRow(a));
    });

    var submit = document.getElementById("card-submit");
    if (submit) submit.textContent = cardSubmitted ? "更新角色卡" : "提交角色卡";
  }

  function cardRow(a) {
    var row = document.createElement("div");
    row.className = "card-row";

    var k = document.createElement("span");
    k.className = "k";
    k.textContent = a.key;

    var d = document.createElement("span");
    d.className = "d";
    d.textContent = a.desc || ("基础 " + a.base + "，最高 " + a.max);

    var minus = document.createElement("button");
    minus.type = "button";
    minus.className = "btn ghost step";
    minus.textContent = "−";
    // 下限是 base 不是 min（2026-10-21 修）：base 白送、扣不回来。以前只卡 min，
    // 点数花满后点减号能把属性压到 base 以下——used 被 max(0, v-base) 钳住不回升，
    // 加号又因剩余 0 被禁用，这 1 点就永久丢了。
    minus.disabled = cardDraft[a.key] <= a.base;
    minus.onclick = function () { bumpCard(a, -1); };

    var v = document.createElement("span");
    v.className = "v";
    v.textContent = cardDraft[a.key];

    var plus = document.createElement("button");
    plus.type = "button";
    plus.className = "btn ghost step";
    plus.textContent = "+";
    plus.disabled = cardDraft[a.key] >= a.max || cardLeft() <= 0;
    plus.onclick = function () { bumpCard(a, 1); };

    [k, d, minus, v, plus].forEach(function (el) { row.appendChild(el); });
    return row;
  }

  function bumpCard(a, delta) {
    var cur = typeof cardDraft[a.key] === "number" ? cardDraft[a.key] : a.base;
    // 下限 base（不是 min）：与提交校验一致，base 是白送的、减不回点数
    var next = Math.max(a.base, Math.min(a.max, cur + delta));
    if (next === cur) return;
    if (delta > 0 && cardLeft() <= 0) { cardMsg("点数用完了，先减掉某项再加", "bad"); return; }
    cardDraft[a.key] = next;
    cardMsg("", "");
    renderCard();
  }

  // 服务端回存的属性是权威的：有就盖掉草稿（提交成功、换标签页重连都会走到这里）。
  // **没提交过就绝不动草稿** —— 否则别人一进房（会广播 state）就把你正在调的点数冲掉。
  function syncCardFromState() {
    if (!cardState || !cardState.needs_card || !cardState.card) return;
    var mine = null;
    (playersCache || []).forEach(function (p) { if (p.name === you) mine = p; });
    var attrs = (mine && mine.character && mine.character.attrs) || null;
    if (!attrs) return;
    var changed = false;
    (cardState.card.attributes || []).forEach(function (a) {
      if (typeof attrs[a.key] === "number" && cardDraft[a.key] !== attrs[a.key]) {
        cardDraft[a.key] = attrs[a.key];
        changed = true;
      }
    });
    if (!cardSubmitted) { cardSubmitted = true; changed = true; }
    if (changed) renderCard();
  }

  function submitCard() {
    if (!cardState || !cardState.card) return;
    if (cardLeft() > 0) {
      // 不强制花满，但提醒一句 —— 大多数人只是忘了
      cardMsg("还有 " + cardLeft() + " 点没分配（也可以就这么提交）", "");
    }
    send({ type: "character", attrs: cardDraft });
  }

  function resetCard() {
    cardDraft = {};
    cardSubmitted = false;
    cardMsg("", "");
    renderCard();
  }

  function saveRoomSettings() {
    var val = function (id) { var el = document.getElementById(id); return el ? el.value : ""; };
    var payload = {
      type: "room_settings",
      name: val("set-name").trim(),
      note: val("set-note").trim(),
      max_players: val("set-max"),
      visibility: val("set-visibility")
    };
    var pwEl = document.getElementById("set-password");
    var clearEl = document.getElementById("set-clear-password");
    var pw = pwEl ? pwEl.value : "";
    if (clearEl && clearEl.checked) payload.clear_password = true;
    else if (pw) payload.password = pw;
    send(payload);
    if (pwEl) pwEl.value = "";
    if (clearEl) clearEl.checked = false;
  }

  (function initLobbyAndWaiting() {
    var refreshBtn = document.getElementById("lobby-refresh");
    if (refreshBtn) refreshBtn.onclick = function () { lobbyMsg(""); loadRooms(); };

    // 角色卡：提交 / 重置 / 重试分析
    var cardSubmitBtn = document.getElementById("card-submit");
    if (cardSubmitBtn) cardSubmitBtn.onclick = submitCard;
    var cardResetBtn = document.getElementById("card-reset");
    if (cardResetBtn) cardResetBtn.onclick = resetCard;
    var cardRetryBtn = document.getElementById("card-retry");
    if (cardRetryBtn) {
      cardRetryBtn.onclick = function () { cardMsg("", ""); send({ type: "card_refresh" }); };
    }

    var backBtn = document.getElementById("lobby-back");
    if (backBtn) {
      backBtn.onclick = function () {
        stopLobbyPolling();
        joinMsg("");
        showScreen("join");
      };
    }

    var createForm = document.getElementById("create-form");
    if (createForm) createForm.addEventListener("submit", createFromForm);

    var startBtn = document.getElementById("wait-start");
    if (startBtn) startBtn.onclick = function () { send({ type: "start" }); };

    // 「解散房间」用**两步点击**而不是 confirm 弹窗：弹窗在部分环境里会被拦掉，
    // 而且误点一下就把整局解散的代价太大。第一次点变成「确认解散」，4 秒后自动还原。
    var closeBtn = document.getElementById("wait-close");
    if (closeBtn) {
      var disarmed = null;
      closeBtn.onclick = function () {
        if (closeBtn.dataset.armed === "1") {
          closeBtn.dataset.armed = "";
          closeBtn.textContent = "解散房间";
          if (disarmed) clearTimeout(disarmed);
          send({ type: "close_room" });
          return;
        }
        closeBtn.dataset.armed = "1";
        closeBtn.textContent = "确认解散？再点一次";
        disarmed = setTimeout(function () {
          closeBtn.dataset.armed = "";
          closeBtn.textContent = "解散房间";
        }, 4000);
      };
    }

    var setSave = document.getElementById("set-save");
    if (setSave) setSave.onclick = saveRoomSettings;

    var waitCopy = document.getElementById("wait-copy");
    if (waitCopy) {
      waitCopy.onclick = function () {
        var code = roomInfo ? roomInfo.code : "";
        if (code && navigator.clipboard) {
          navigator.clipboard.writeText(code).then(function () {
            waitCopy.textContent = "已复制";
            setTimeout(function () { waitCopy.textContent = "复制房间码"; }, 1500);
          });
        }
      };
    }

    // 关掉页面时告诉服务端一声（服务端也会自己发现断开，这只是让"离开"更快生效）
    window.addEventListener("beforeunload", function () {
      if (ws && ws.readyState === 1) {
        try { ws.send(JSON.stringify({ type: "leave" })); } catch (err) {}
      }
    });
  })();
})();
