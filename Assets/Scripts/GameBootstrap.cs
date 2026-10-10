// 入口：把网络层（GameClient）与界面层（GameUI）接起来。
//
// 场景里只需要一个挂着本组件的空物体，其余对象（Canvas、EventSystem、UI 层级）都在运行时创建。
using System.Text;
using Newtonsoft.Json.Linq;
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
            if (FindAnyObjectByType<GameBootstrap>() != null) return;
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

        void HandleWelcome(JObject m)
        {
            _joined = true;
            _roomCode = m["room"].Str();
            _scriptName = m["script"].Str();

            _ui.ShowJoinPanel(false);
            _ui.ClearLog();
            _ui.SetRoomLabel($"房号 {_roomCode}\n剧本：{_scriptName}");
            _ui.SetInputInteractable(true);
            _ui.FocusActionInput();

            _ui.AppendLog("", $"已加入房间 {_roomCode}（剧本：《{_scriptName}》）", GameUI.LogKind.System);
            if (m["late"].Bool()) _ui.AppendLog("", "你是中途加入的，下面会把已发生的剧情告诉你。", GameUI.LogKind.System);

            var agents = m["agents"];
            if (agents.Has())
            {
                var sb = new StringBuilder("模型槽位：");
                sb.Append($"DM={SlotBrief(agents["dm"])}");
                if (agents["assistant"].Has())
                    sb.Append($"｜主机小助手={SlotBrief(agents["assistant"])}");
                sb.Append($"｜小助手={SlotBrief(agents["advisor"])}");
                _ui.AppendLog("", sb.ToString(), GameUI.LogKind.System);
            }
        }

        static string SlotBrief(JToken slot)
        {
            string mode = slot["mode"].Str("scripted");
            if (mode == "scripted") return "未接入（脚本化）";
            string model = slot["model"].Str();
            return string.IsNullOrEmpty(model) ? mode : model;
        }

        void HandleState(JToken state)
        {
            _ui.SetSidebar(_roomCode, _scriptName, state);
        }

        void HandleNarration(JObject m)
        {
            string author = m["author"].Str("DM");
            string text = m["text"].Str();
            if (string.IsNullOrEmpty(text)) return;
            _ui.AppendLog(author, text, author == "DM" ? GameUI.LogKind.Dm : GameUI.LogKind.Player);
        }

        void HandleDice(JObject m)
        {
            string player = m["player"].Str();
            string skill = m["skill"].Str();
            int dc = m["dc"].Int();
            int roll = m["roll"].Int();
            var success = m["success"];

            string line;
            if (string.IsNullOrEmpty(skill))
            {
                line = $"{player} 掷出了 {roll}";
            }
            else
            {
                string verdict = success.Has() ? (success.Bool() ? "  成功" : "  失败") : "";
                line = $"{player} 的「{skill}」检定：d20 = {roll}，DC {dc}{verdict}";
            }
            if (!string.IsNullOrEmpty(m["flag"].Str())) line += "（线索到手）";

            _ui.AppendLog("", line, GameUI.LogKind.Dice);
        }

        void HandleSuggestions(JObject m)
        {
            var options = JsonUtil.Arr(m["options"]);
            if (options.Count == 0)
            {
                _ui.AppendLog("", "小助手这次没给出建议。", GameUI.LogKind.System);
                return;
            }

            var sb = new StringBuilder("小助手建议：");
            for (int i = 0; i < options.Count; i++)
                sb.Append($"\n{i + 1}. {options[i].Str()}");
            _ui.AppendLog("", sb.ToString(), GameUI.LogKind.System);
        }

        void HandleRecap(JObject m)
        {
            var recap = m["recap"];
            if (!recap.Has()) return;

            var sb = new StringBuilder("【故事回顾】");
            var path = JsonUtil.Arr(recap["scene_path"]);
            if (path.Count > 0)
            {
                sb.Append("\n走过的场景：");
                for (int i = 0; i < path.Count; i++)
                {
                    if (i > 0) sb.Append(" → ");
                    sb.Append(path[i].Str());
                }
            }

            var flags = JsonUtil.Arr(recap["flags"]);
            if (flags.Count > 0)
            {
                sb.Append($"\n已获得线索 {flags.Count} 条");
            }

            var recent = JsonUtil.Arr(recap["recent"]);
            foreach (var r in recent)
            {
                string text = r.Type == JTokenType.Object ? r["text"].Str() : r.Str();
                if (!string.IsNullOrEmpty(text)) sb.Append($"\n· {text}");
            }

            _ui.AppendLog("", sb.ToString(), GameUI.LogKind.System);
        }

        void HandleAgentStatus(JObject m)
        {
            var changed = JsonUtil.Arr(m["changed"]);
            if (changed.Count == 0) return;

            var sb = new StringBuilder("模型槽位已更新：");
            for (int i = 0; i < changed.Count; i++)
            {
                if (i > 0) sb.Append("、");
                sb.Append(SlotName(changed[i].Str()));
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
