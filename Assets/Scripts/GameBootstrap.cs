// 入口：把网络层（GameClient）与界面层（GameUI）接起来。
//
// 场景里只需要一个挂着本组件的空物体，其余对象（Canvas、EventSystem、UI 层级）都在运行时创建。
using System.Text;
using UnityEngine;

namespace RPGBar
{
    public class GameBootstrap : MonoBehaviour
    {
        GameClient _client;
        GameUI _ui;

        bool _joined;
        string _roomCode = "";
        string _scriptName = "";

        /// <summary>
        /// 自动引导：哪怕场景里什么都没摆，进入 Play 也会自动创建 RPGBar 根对象。
        /// 有了它，「空场景 + 按 Play」就能跑，不必依赖 .unity 场景文件（手写 YAML 易出错）。
        /// </summary>
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        static void AutoBoot()
        {
            if (FindFirstObjectByType<GameBootstrap>() != null) return;
            var go = new GameObject("RPGBar");
            go.AddComponent<GameBootstrap>();
        }

        void Awake()
        {
            _client = gameObject.AddComponent<GameClient>();

            var uiGo = new GameObject("UI");
            uiGo.transform.SetParent(transform, false);
            _ui = uiGo.AddComponent<GameUI>();   // Awake 里就会把界面搭好

            // 界面 → 网络
            _ui.OnJoinRequested += HandleJoinRequest;
            _ui.OnActionSubmitted += text => _client.SendAction(text);
            _ui.OnSuggestClicked += () => _client.SendSuggest();
            _ui.OnRollClicked += () => _client.SendRoll();

            // 网络 → 界面
            _client.OnWelcome += HandleWelcome;
            _client.OnState += HandleState;
            _client.OnNarration += HandleNarration;
            _client.OnDice += HandleDice;
            _client.OnSuggestions += HandleSuggestions;
            _client.OnRecap += HandleRecap;
            _client.OnAgentStatus += HandleAgentStatus;
            _client.OnSystemMessage += text => _ui.AppendLog("", text, GameUI.LogKind.System);
            _client.OnErrorMessage += text => _ui.AppendLog("", text, GameUI.LogKind.Error);
            _client.OnConnection += HandleConnection;

            _ui.SetInputInteractable(false);
            _ui.SetJoinStatus("填写服务器地址后加入；留空房间号即新建房间。");
        }

        // ---------------------------------------------------------------- 界面 → 网络

        void HandleJoinRequest(string url, string name, string room, string script)
        {
            if (string.IsNullOrEmpty(name)) name = "冒险者";
            _ui.SetJoinStatus("连接中……");
            _client.Connect(NormalizeWsUrl(url), name, room, script);
        }

        /// <summary>把用户填的各种写法统一成 ws://host:port/ws。</summary>
        static string NormalizeWsUrl(string url)
        {
            if (string.IsNullOrWhiteSpace(url)) return "ws://127.0.0.1:8000/ws";
            url = url.Trim();

            if (url.StartsWith("https://")) url = "wss://" + url.Substring("https://".Length);
            else if (url.StartsWith("http://")) url = "ws://" + url.Substring("http://".Length);
            else if (!url.StartsWith("ws://") && !url.StartsWith("wss://")) url = "ws://" + url;

            url = url.TrimEnd('/');
            if (!url.EndsWith("/ws")) url += "/ws";
            return url;
        }

        // ---------------------------------------------------------------- 网络 → 界面

        void HandleConnection(bool ok, string message)
        {
            if (ok)
            {
                _ui.SetJoinStatus(message);
                return;
            }
            _ui.SetJoinStatus(message, true);
            _ui.AppendLog("", message, GameUI.LogKind.Error);
            if (!_joined) _ui.ShowJoinPanel(true);
        }

        void HandleWelcome(JsonValue m)
        {
            _joined = true;
            _roomCode = m["room"].AsString();
            _scriptName = m["script"].AsString();

            _ui.ShowJoinPanel(false);
            _ui.ClearLog();
            _ui.SetRoomLabel($"房号 {_roomCode}\n剧本：{_scriptName}");
            _ui.SetInputInteractable(true);
            _ui.FocusActionInput();

            _ui.AppendLog("", $"已加入房间 {_roomCode}（剧本：《{_scriptName}》）", GameUI.LogKind.System);
            if (m["late"].AsBool()) _ui.AppendLog("", "你是中途加入的，下面会把已发生的剧情告诉你。", GameUI.LogKind.System);

            var agents = m["agents"];
            if (agents != null && !agents.IsNull)
            {
                var sb = new StringBuilder("模型槽位：");
                sb.Append($"DM={SlotBrief(agents["dm"])}");
                if (agents["assistant"] != null && !agents["assistant"].IsNull)
                    sb.Append($"｜主机小助手={SlotBrief(agents["assistant"])}");
                sb.Append($"｜小助手={SlotBrief(agents["advisor"])}");
                _ui.AppendLog("", sb.ToString(), GameUI.LogKind.System);
            }
        }

        static string SlotBrief(JsonValue slot)
        {
            string mode = slot["mode"].AsString("scripted");
            if (mode == "scripted") return "未接入（脚本化）";
            string model = slot["model"].AsString();
            return string.IsNullOrEmpty(model) ? mode : model;
        }

        void HandleState(JsonValue state)
        {
            _ui.SetSidebar(_roomCode, _scriptName, state);
        }

        void HandleNarration(JsonValue m)
        {
            string author = m["author"].AsString("DM");
            string text = m["text"].AsString();
            if (string.IsNullOrEmpty(text)) return;
            _ui.AppendLog(author, text, author == "DM" ? GameUI.LogKind.Dm : GameUI.LogKind.Player);
        }

        void HandleDice(JsonValue m)
        {
            string player = m["player"].AsString();
            string skill = m["skill"].AsString();
            int dc = m["dc"].AsInt();
            int roll = m["roll"].AsInt();
            var success = m["success"];

            string line;
            if (string.IsNullOrEmpty(skill))
            {
                line = $"{player} 掷出了 {roll}";
            }
            else
            {
                string verdict = success.IsNull ? "" : (success.AsBool() ? "  成功" : "  失败");
                line = $"{player} 的「{skill}」检定：d20 = {roll}，DC {dc}{verdict}";
            }
            if (!string.IsNullOrEmpty(m["flag"].AsString())) line += "（线索到手）";

            _ui.AppendLog("", line, GameUI.LogKind.Dice);
        }

        void HandleSuggestions(JsonValue m)
        {
            var options = m["options"].AsList();
            if (options.Count == 0)
            {
                _ui.AppendLog("", "小助手这次没给出建议。", GameUI.LogKind.System);
                return;
            }

            var sb = new StringBuilder("小助手建议：");
            for (int i = 0; i < options.Count; i++)
                sb.Append($"\n{i + 1}. {options[i].AsString()}");
            _ui.AppendLog("", sb.ToString(), GameUI.LogKind.System);
        }

        void HandleRecap(JsonValue m)
        {
            var recap = m["recap"];
            if (recap == null || recap.IsNull) return;

            var sb = new StringBuilder("【故事回顾】");
            var path = recap["scene_path"].AsList();
            if (path.Count > 0)
            {
                sb.Append("\n走过的场景：");
                for (int i = 0; i < path.Count; i++)
                {
                    if (i > 0) sb.Append(" → ");
                    sb.Append(path[i].AsString());
                }
            }

            var flags = recap["flags"].AsList();
            if (flags.Count > 0)
            {
                sb.Append($"\n已获得线索 {flags.Count} 条");
            }

            var recent = recap["recent"].AsList();
            foreach (var r in recent)
            {
                string text = r.IsObject ? r["text"].AsString() : r.AsString();
                if (!string.IsNullOrEmpty(text)) sb.Append($"\n· {text}");
            }

            _ui.AppendLog("", sb.ToString(), GameUI.LogKind.System);
        }

        void HandleAgentStatus(JsonValue m)
        {
            var changed = m["changed"].AsList();
            if (changed.Count == 0) return;

            var sb = new StringBuilder("模型槽位已更新：");
            for (int i = 0; i < changed.Count; i++)
            {
                if (i > 0) sb.Append("、");
                sb.Append(SlotName(changed[i].AsString()));
            }
            _ui.AppendLog("", sb.ToString(), GameUI.LogKind.System);
        }

        static string SlotName(string key)
        {
            switch (key)
            {
                case "dm": return "主机 DM";
                case "assistant": return "主机小助手";
                case "advisor": return "玩家小助手";
                default: return key;
            }
        }

        void OnDestroy()
        {
            if (_client != null) _client.Disconnect();
        }
    }
}
