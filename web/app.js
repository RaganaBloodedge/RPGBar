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

  function loadVersion() {
    fetch("/api/version")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.version) return;
        var v = "v" + d.version;
        ["app-version", "app-version-top"].forEach(function (id) {
          var el = document.getElementById(id);
          if (el) el.textContent = v;
        });
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
    roomCode.textContent = "";
    sceneTitle.textContent = state.scene_title || "—";
    sceneLocation.textContent = state.location || "—";

    playersEl.innerHTML = "";
    (state.players || []).forEach(function (p) {
      var li = document.createElement("li");
      li.textContent = p.name + " · " + (p.character && p.character.cls ? p.character.cls : "");
      playersEl.appendChild(li);
    });

    flagsEl.innerHTML = "";
    if (!state.flags || state.flags.length === 0) {
      var empty = document.createElement("li");
      empty.textContent = "（暂无）";
      flagsEl.appendChild(empty);
    } else {
      state.flags.forEach(function (f) {
        var li = document.createElement("li");
        li.textContent = f;
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
        addEntry(
          "system",
          "",
          msg.late
            ? "你中途加入了房间 " + msg.room + "（你是 " + you + "），DM 正在为你补上之前的剧情…"
            : "你已加入房间 " + msg.room + "（你是 " + you + "）"
        );
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
