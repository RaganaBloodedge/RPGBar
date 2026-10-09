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

  function handleMessage(msg) {
    switch (msg.type) {
      case "welcome":
        you = msg.you;
        roomCode.textContent = msg.room;
        addEntry("system", "", "你已加入房间 " + msg.room + "（你是 " + you + "）");
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
