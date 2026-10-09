(function () {
  "use strict";

  var joinScreen = document.getElementById("join-screen");
  var gameScreen = document.getElementById("game-screen");
  var joinForm = document.getElementById("join-form");
  var logEl = document.getElementById("log");
  var playersEl = document.getElementById("players");
  var flagsEl = document.getElementById("flags");
  var suggestionsEl = document.getElementById("suggestions");
  var actionInput = document.getElementById("action-input");
  var sendBtn = document.getElementById("send-btn");
  var suggestBtn = document.getElementById("suggest-btn");
  var rollBtn = document.getElementById("roll-d20");
  var copyBtn = document.getElementById("copy-room");

  var roomCode = document.getElementById("room-code");
  var sceneTitle = document.getElementById("scene-title");
  var sceneLocation = document.getElementById("scene-location");

  var ws = null;
  var you = "";
  var currentScript = "";

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
    logEl.scrollTop = logEl.scrollHeight;
  }

  function renderState(state) {
    if (!state) return;
    sceneTitle.textContent = state.scene_title || "—";
    sceneLocation.textContent = state.location || "—";

    playersEl.innerHTML = "";
    (state.players || []).forEach(function (p) {
      var li = document.createElement("li");
      li.textContent = p.name + " · " + (p.character && p.character.cls ? p.character.cls : "");
      playersEl.appendChild(li);
    });

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

  function handleMessage(msg) {
    switch (msg.type) {
      case "welcome":
        you = msg.you;
        roomCode.textContent = msg.room;
        if (msg.script) setScript(msg.script);
        isOwner = !!(msg.agents && msg.agents.owner === you);
        applyAgents(msg.agents);
        addEntry(
          "system",
          "",
          msg.late
            ? "你中途加入了房间 " + msg.room + "（你是 " + you + "），DM 正在为你补上之前的剧情…"
            : "你已加入房间 " + msg.room + "（你是 " + you + "）"
        );
        pushSavedModels();
        break;
      case "agent_status":
        applyAgents(msg.agents);
        if (msg.changed && msg.changed.length) {
          var names = msg.changed.map(function (k) {
            return k === "dm" ? "主机 DM" : "玩家小助手";
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
          : msg.player + " 进行「" + msg.skill + "」检定：d20=" + msg.roll + " vs DC" + msg.dc + " → " + (msg.success ? "成功" : "失败");
        addEntry(msg.success === false ? "dice fail" : "dice", "骰子", label);
        break;
      case "suggestions":
        suggestionsEl.innerHTML = "";
        (msg.options || []).forEach(function (opt) {
          var chip = document.createElement("span");
          chip.className = "suggestion";
          chip.textContent = opt;
          chip.onclick = function () { sendAction(opt); };
          suggestionsEl.appendChild(chip);
        });
        var ag = msg.agent || {};
        if (ag.source === "scripted") {
          var tag = document.createElement("span");
          tag.className = "suggest-src";
          tag.textContent = "脚本化兜底";
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
      case "error":
        addEntry("system", "错误", msg.message);
        break;
    }
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }

  /* ==================== 模型设置抽屉 ==================== */

  var SETTINGS_KEY = "rpgbar.models";
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
    advisor: { title: "玩家小助手 · 小模型", role: "只读场景，给你 2-4 条行动建议。每位玩家配自己的，可指向本地模型。" }
  };
  var slotState = {
    dm: { kind: "off", base_url: "", model: "", api_key: "", temperature: 0.8 },
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
      ["dm", "advisor"].forEach(function (k) {
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
        advisor: slotState.advisor
      }));
    } catch (e) {}
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
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

    var locked = key === "dm" && !isOwner;
    if (locked) {
      card.classList.add("locked");
      card.appendChild(el("p", "lock-note",
        "只有房主（" + (ownerName || "首位加入者") + "）可以修改主机 DM 模型。你可以照常配置自己的小助手。"));
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
      card.appendChild(el("p", "slot-role", "未接入 —— 该 Agent 走脚本化兜底，玩法完整可玩。"));
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
    renderSlot("dm");
    renderSlot("advisor");
  }

  function badgeInfo(key) {
    var info = serverAgents && serverAgents[key];
    if (info && info.ready) {
      return { on: true, text: "已连接" + (info.model ? " · " + info.model : "") };
    }
    if (slotState[key].kind === "off") return { on: false, text: "未接入（脚本化）" };
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
    else pill.textContent = "模型：脚本化";
    pill.className = "chip model-pill" + (dmOn ? " on" : "");
  }

  function applyAgents(agents) {
    if (!agents) return;
    var wasOwner = isOwner;
    serverAgents = agents;
    if (typeof agents.owner === "string") ownerName = agents.owner;
    isOwner = typeof agents.owner === "string" && agents.owner === you;
    ["dm", "advisor"].forEach(function (k) {
      var info = agents[k];
      if (info && info.base_url && !slotState[k].base_url) slotState[k].base_url = info.base_url;
      if (info && info.model && !slotState[k].model) slotState[k].model = info.model;
    });
    refreshBadge("dm");
    refreshBadge("advisor");
    updateModelPill();
    // 房主身份变化（例如服务端换人/重连）时，DM 卡片的可编辑性跟着变
    if (wasOwner !== isOwner) rerenderSlotsIfOpen();
  }

  function rerenderSlotsIfOpen() {
    var drawer = document.getElementById("settings-drawer");
    if (drawer && !drawer.classList.contains("hidden")) {
      renderSlots();
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
    if (isOwner) payload.dm = slotPayload("dm"); // 主机 DM 仅房主可改
    payload.advisor = slotPayload("advisor");
    send(payload);
  }

  function saveSettings() {
    persistSaved();
    sendConfigure();
  }

  // 加入房间后，把本机已保存的模型设置自动推给服务端（DM 仅房主可推）
  function pushSavedModels() {
    loadSaved();
    var payload = { type: "configure" };
    var has = false;
    if (isOwner && slotState.dm.kind !== "off") {
      payload.dm = slotPayload("dm");
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
          else if (d.tool_calling === false) extra = " · 不支持工具调用（自动降级为单轮 JSON）";
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
      left.appendChild(el("div", "name", it.title || it.file));
      var meta = [it.path, it.structured ? "结构化" : "纯文本"]
        .concat(it.error ? [it.error] : [it.scenes + " 场景", it.chunks + " 切片"])
        .join(" · ");
      left.appendChild(el("div", "meta", meta));
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
    renderSlots();
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
      slotState.dm = { kind: "off", base_url: "", model: "", api_key: "", temperature: 0.8 };
      slotState.advisor = { kind: "off", base_url: "", model: "", api_key: "", temperature: 0.7 };
      sendConfigure(); // 通知服务端回到脚本化，但不再写回本机
      renderSlots();
      showMsg("已清除本机设置，两个 Agent 回到脚本化兜底。", "ok");
    };
    loadSaved();
  })();

  function sendAction(text) {
    text = (text || "").trim();
    if (!text) return;
    send({ type: "action", text: text });
    addEntry("player", you || "我", text);
    actionInput.value = "";
  }

  joinForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var name = document.getElementById("join-name").value.trim();
    var cls = document.getElementById("join-class").value;
    var room = document.getElementById("join-room").value.trim();

    var proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(proto + "://" + location.host + "/ws");

    ws.onopen = function () {
      send({
        type: "join",
        name: name,
        room: room,
        character: { name: name, cls: cls, hp: 10, skills: {} }
      });
    };
    ws.onmessage = function (ev) {
      try { handleMessage(JSON.parse(ev.data)); } catch (err) {}
    };
    ws.onclose = function () {
      addEntry("system", "", "连接已断开，刷新页面重新加入");
    };
    ws.onerror = function () {
      addEntry("system", "错误", "无法连接服务器");
    };

    joinScreen.classList.add("hidden");
    gameScreen.classList.remove("hidden");
    actionInput.focus();
  });

  sendBtn.addEventListener("click", function () { sendAction(actionInput.value); });
  actionInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { sendAction(actionInput.value); }
  });
  suggestBtn.addEventListener("click", function () { send({ type: "suggest" }); });
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
})();
