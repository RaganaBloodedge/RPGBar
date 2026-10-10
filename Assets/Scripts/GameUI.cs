// 运行时用代码搭出来的 UGUI 界面 —— 不依赖任何 prefab / 美术资源 / 第三方包。
//
// 布局：
//   ┌──────────────────────────┬──────────┐
//   │ 旁白 / 行动 / 骰子 滚动区   │ 侧栏      │
//   │                          │ 房号      │
//   ├──────────────────────────┤ 玩家      │
//   │ 输入框 [发送][建议][掷骰]  │ 线索      │
//   └──────────────────────────┴──────────┘
//   连接面板（覆盖层）：地址 / 名字 / 房间号 / 剧本
//
// 之所以全部用代码建：Unity 的 .unity 场景是 YAML，手写易错；而 batchmode 下用
// Editor 脚本生成场景时，能引用的只有脚本组件，建 UI 交给运行时最稳。
using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace RPGBar
{
    public class GameUI : MonoBehaviour
    {
        // (地址, 名字, 房间号, 剧本) —— 由 GameBootstrap 接走
        public event Action<string, string, string, string> OnJoinRequested;
        public event Action<string> OnActionSubmitted;
        public event Action OnSuggestClicked;
        public event Action OnRollClicked;

        // —— 配色（暗色，贴合跑团的氛围）——
        static readonly Color BgRoot = new Color(0.078f, 0.086f, 0.110f, 1f);
        static readonly Color BgPanel = new Color(0.110f, 0.122f, 0.157f, 1f);
        static readonly Color BgField = new Color(0.063f, 0.070f, 0.094f, 1f);
        static readonly Color BgButton = new Color(0.180f, 0.220f, 0.310f, 1f);
        static readonly Color TextMain = new Color(0.902f, 0.910f, 0.925f, 1f);
        static readonly Color TextDim = new Color(0.580f, 0.610f, 0.660f, 1f);
        static readonly Color ColDm = new Color(0.910f, 0.784f, 0.478f, 1f);
        static readonly Color ColPlayer = new Color(0.560f, 0.840f, 0.940f, 1f);
        static readonly Color ColDice = new Color(0.788f, 0.627f, 0.940f, 1f);
        static readonly Color ColSystem = new Color(0.560f, 0.600f, 0.660f, 1f);
        static readonly Color ColError = new Color(0.940f, 0.600f, 0.600f, 1f);

        Canvas _canvas;
        ScrollRect _scroll;
        RectTransform _content;
        Text _sidebar;
        InputField _actionInput;
        GameObject _joinPanel;
        InputField _urlInput, _nameInput, _roomInput, _scriptInput;
        Text _joinStatus;
        Text _roomLabel;

        public static Font UiFont { get; private set; }

        void Awake()
        {
            EnsureFont();
            EnsureEventSystem();
            BuildCanvas();
            BuildLogArea();
            BuildSidebar();
            BuildBottomBar();
            BuildJoinPanel();
        }

        // ---------------------------------------------------------------- 字体 / 事件系统

        static void EnsureFont()
        {
            if (UiFont != null) return;
            // Unity 内置字体不含中文字形，直接用会显示方块 —— 改用系统字体。
            UiFont = Font.CreateDynamicFontFromOSFont(
                new[] { "Microsoft YaHei", "微软雅黑", "SimHei", "Noto Sans CJK SC", "PingFang SC", "Arial" }, 16);
            if (UiFont == null) UiFont = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
        }

        static void EnsureEventSystem()
        {
            if (FindFirstObjectByType<EventSystem>() != null) return;
            var go = new GameObject("EventSystem");
            go.AddComponent<EventSystem>();
            go.AddComponent<StandaloneInputModule>();
        }

        // ---------------------------------------------------------------- 搭建

        void BuildCanvas()
        {
            var go = new GameObject("RPGBarCanvas", typeof(RectTransform));
            _canvas = go.AddComponent<Canvas>();
            _canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            var scaler = go.AddComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1280f, 720f);
            scaler.matchWidthOrHeight = 0.5f;
            go.AddComponent<GraphicRaycaster>();

            var bg = Node("Background", go.transform).AddComponent<Image>();
            bg.color = BgRoot;
            Stretch(RT(bg.gameObject));

            _canvas.transform.SetParent(transform, false);
        }

        void BuildLogArea()
        {
            var panel = Node("LogPanel", _canvas.transform);
            var prt = RT(panel);
            prt.anchorMin = new Vector2(0f, 0.13f);
            prt.anchorMax = new Vector2(0.72f, 1f);
            prt.offsetMin = new Vector2(12f, 6f);
            prt.offsetMax = new Vector2(-6f, -12f);
            panel.AddComponent<Image>().color = BgPanel;

            var viewport = Node("Viewport", panel.transform);
            Stretch(RT(viewport), 1f, 1f, 1f, 1f);
            viewport.AddComponent<RectMask2D>();

            var content = Node("Content", viewport.transform);
            _content = RT(content);
            _content.anchorMin = new Vector2(0f, 1f);
            _content.anchorMax = new Vector2(1f, 1f);
            _content.pivot = new Vector2(0.5f, 1f);
            _content.offsetMin = new Vector2(0f, 0f);
            _content.offsetMax = new Vector2(0f, 0f);

            var vlg = content.AddComponent<VerticalLayoutGroup>();
            vlg.childControlWidth = true;
            vlg.childControlHeight = true;
            vlg.childForceExpandWidth = true;
            vlg.childForceExpandHeight = false;
            vlg.spacing = 8f;
            vlg.padding = new RectOffset(14, 14, 14, 14);
            var fitter = content.AddComponent<ContentSizeFitter>();
            fitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;

            _scroll = panel.AddComponent<ScrollRect>();
            _scroll.viewport = RT(viewport);
            _scroll.content = _content;
            _scroll.horizontal = false;
            _scroll.vertical = true;
            _scroll.movementType = ScrollRect.MovementType.Clamped;
            _scroll.scrollSensitivity = 28f;
        }

        void BuildSidebar()
        {
            var panel = Node("SidePanel", _canvas.transform);
            var prt = RT(panel);
            prt.anchorMin = new Vector2(0.72f, 0f);
            prt.anchorMax = new Vector2(1f, 1f);
            prt.offsetMin = new Vector2(6f, 6f);
            prt.offsetMax = new Vector2(-12f, -12f);
            panel.AddComponent<Image>().color = BgPanel;

            var vlg = panel.AddComponent<VerticalLayoutGroup>();
            vlg.childControlWidth = true;
            vlg.childControlHeight = true;
            vlg.childForceExpandWidth = true;
            vlg.childForceExpandHeight = false;
            vlg.spacing = 10f;
            vlg.padding = new RectOffset(12, 12, 12, 12);

            _roomLabel = MakeText("Room", panel.transform, "未连接", 14, TextAnchor.UpperLeft, TextDim);

            _sidebar = MakeText("Info", panel.transform, "", 14, TextAnchor.UpperLeft, TextMain);
            _sidebar.lineSpacing = 1.3f;
        }

        void BuildBottomBar()
        {
            var bar = Node("BottomBar", _canvas.transform);
            var brt = RT(bar);
            brt.anchorMin = new Vector2(0f, 0f);
            brt.anchorMax = new Vector2(0.72f, 0.13f);
            brt.offsetMin = new Vector2(12f, 12f);
            brt.offsetMax = new Vector2(-6f, -6f);
            bar.AddComponent<Image>().color = BgPanel;

            var hlg = bar.AddComponent<HorizontalLayoutGroup>();
            hlg.childControlWidth = true;
            hlg.childControlHeight = true;
            hlg.childForceExpandWidth = false;
            hlg.childForceExpandHeight = true;
            hlg.spacing = 8f;
            hlg.padding = new RectOffset(10, 10, 10, 10);

            _actionInput = MakeInput("ActionInput", bar.transform, "输入你的行动，回车发送……");
            var le = _actionInput.gameObject.AddComponent<LayoutElement>();
            le.flexibleWidth = 1f;
            le.minWidth = 160f;
            _actionInput.onSubmit.AddListener(_ => SubmitAction());

            var sendBtn = MakeButton("Send", bar.transform, "发送", SubmitAction);
            AddFixed(sendBtn.gameObject, 84f);
            var suggestBtn = MakeButton("Suggest", bar.transform, "建议", () => OnSuggestClicked?.Invoke());
            AddFixed(suggestBtn.gameObject, 84f);
            var rollBtn = MakeButton("Roll", bar.transform, "掷 d20", () => OnRollClicked?.Invoke());
            AddFixed(rollBtn.gameObject, 84f);
        }

        static void AddFixed(GameObject go, float width)
        {
            var le = go.AddComponent<LayoutElement>();
            le.preferredWidth = width;
            le.minWidth = width;
        }

        void BuildJoinPanel()
        {
            _joinPanel = Node("JoinPanel", _canvas.transform);
            Stretch(RT(_joinPanel));
            _joinPanel.AddComponent<Image>().color = new Color(0.04f, 0.05f, 0.07f, 0.96f);

            var card = Node("Card", _joinPanel.transform);
            var crt = RT(card);
            crt.anchorMin = crt.anchorMax = new Vector2(0.5f, 0.5f);
            crt.pivot = new Vector2(0.5f, 0.5f);
            crt.sizeDelta = new Vector2(520f, 400f);
            crt.anchoredPosition = Vector2.zero;
            card.AddComponent<Image>().color = BgPanel;

            var vlg = card.AddComponent<VerticalLayoutGroup>();
            vlg.childControlWidth = true;
            vlg.childControlHeight = true;
            vlg.childForceExpandWidth = true;
            vlg.childForceExpandHeight = false;
            vlg.spacing = 10f;
            vlg.padding = new RectOffset(28, 28, 24, 24);

            MakeText("Title", card.transform, "RPGBar · 文字跑团", 22, TextAnchor.MiddleCenter, TextMain)
                .gameObject.AddComponent<LayoutElement>().preferredHeight = 40f;
            MakeText("Sub", card.transform, "由 AI 担任 DM 的多人跑团（Unity 客户端）", 13, TextAnchor.MiddleCenter, TextDim)
                .gameObject.AddComponent<LayoutElement>().preferredHeight = 26f;

            _urlInput = MakeInput("Url", card.transform, "服务器地址 ws://127.0.0.1:8000/ws");
            _urlInput.text = "ws://127.0.0.1:8000/ws";
            Row(_urlInput.gameObject, 38f);

            _nameInput = MakeInput("Name", card.transform, "你的名字");
            Row(_nameInput.gameObject, 38f);

            _roomInput = MakeInput("Room", card.transform, "房间号（留空 = 新建房间）");
            Row(_roomInput.gameObject, 38f);

            _scriptInput = MakeInput("Script", card.transform, "剧本文件（留空 = 用服务器默认）");
            Row(_scriptInput.gameObject, 38f);

            var joinBtn = MakeButton("Join", card.transform, "加入游戏", () =>
            {
                OnJoinRequested?.Invoke(_urlInput.text.Trim(), _nameInput.text.Trim(),
                    _roomInput.text.Trim(), _scriptInput.text.Trim());
            });
            Row(joinBtn.gameObject, 44f);

            _joinStatus = MakeText("Status", card.transform, "", 13, TextAnchor.MiddleCenter, TextDim);
            _joinStatus.gameObject.AddComponent<LayoutElement>().preferredHeight = 24f;
        }

        static void Row(GameObject go, float height)
        {
            var le = go.GetComponent<LayoutElement>();
            if (le == null) le = go.AddComponent<LayoutElement>();
            le.preferredHeight = height;
            le.minHeight = height;
        }

        // ---------------------------------------------------------------- 对外接口

        public void ShowJoinPanel(bool visible)
        {
            if (_joinPanel != null) _joinPanel.SetActive(visible);
        }

        public void SetJoinStatus(string text, bool isError = false)
        {
            if (_joinStatus == null) return;
            _joinStatus.text = text ?? "";
            _joinStatus.color = isError ? ColError : TextDim;
        }

        public void ClearLog()
        {
            foreach (var line in new List<GameObject>(LogLines))
            {
                if (line != null) Destroy(line);
            }
            LogLines.Clear();
        }

        readonly List<GameObject> LogLines = new List<GameObject>();

        /// <summary>往旁白区追加一条。</summary>
        public void AppendLog(string author, string text, LogKind kind)
        {
            string prefix = "";
            Color color = TextMain;
            switch (kind)
            {
                case LogKind.Dm: color = ColDm; break;
                case LogKind.Player: prefix = $"{author}："; color = ColPlayer; break;
                case LogKind.Dice: color = ColDice; break;
                case LogKind.System: color = ColSystem; break;
                case LogKind.Error: color = ColError; break;
            }

            var t = MakeText("Line", _content, prefix + text, 15, TextAnchor.UpperLeft, color);
            t.lineSpacing = 1.35f;
            LogLines.Add(t.gameObject);

            // 上限保护：一局长了不至于把 DOM/Canvas 拖垮
            while (LogLines.Count > 400)
            {
                if (LogLines[0] != null) Destroy(LogLines[0]);
                LogLines.RemoveAt(0);
            }

            Canvas.ForceUpdateCanvases();
            if (_scroll != null) _scroll.verticalNormalizedPosition = 0f;
        }

        public void SetRoomLabel(string text) => _roomLabel.text = text ?? "";

        /// <summary>刷新右侧信息栏。</summary>
        public void SetSidebar(string roomCode, string script, JsonValue state)
        {
            var sb = new System.Text.StringBuilder();

            var players = state?["players"].AsList();
            sb.AppendLine("玩家");
            if (players == null || players.Count == 0) sb.AppendLine("· （无）");
            else foreach (var p in players) sb.AppendLine("· " + p.AsString());

            sb.AppendLine();
            sb.AppendLine("线索");
            var flags = state?["flag_details"].AsList();
            if (flags == null || flags.Count == 0) sb.AppendLine("· （尚未发现）");
            else
                foreach (var f in flags)
                {
                    string label = f["label"].AsString();
                    sb.AppendLine("· " + (string.IsNullOrEmpty(label) ? f["id"].AsString() : label));
                }

            sb.AppendLine();
            sb.AppendLine("在场人物");
            var npcs = state?["npcs"].AsList();
            if (npcs == null || npcs.Count == 0) sb.AppendLine("· （尚未遇到）");
            else
                foreach (var n in npcs)
                {
                    string role = n["role"].AsString();
                    sb.AppendLine("· " + n["name"].AsString() + (string.IsNullOrEmpty(role) ? "" : $"（{role}）"));
                }

            var memory = state?["memory"];
            if (memory != null && !memory.IsNull)
            {
                sb.AppendLine();
                sb.AppendLine("剧情档案");
                sb.AppendLine($"· 记录 {memory["entries"].AsInt()} 条");
                if (memory["summary"].AsBool()) sb.AppendLine("· 已有主线摘要");
                if (memory["semantic"].AsBool()) sb.AppendLine("· 语义检索可用");
            }

            _sidebar.text = sb.ToString();
        }

        public void FocusActionInput()
        {
            if (_actionInput != null)
            {
                _actionInput.ActivateInputField();
                _actionInput.Select();
            }
        }

        void SubmitAction()
        {
            if (_actionInput == null) return;
            string text = _actionInput.text;
            if (string.IsNullOrEmpty(text) || text.Trim().Length == 0) return;
            _actionInput.text = "";
            _actionInput.ActivateInputField();
            OnActionSubmitted?.Invoke(text.Trim());
        }

        // ---------------------------------------------------------------- 构件工具

        public enum LogKind { Dm, Player, Dice, System, Error }

        static GameObject Node(string name, Transform parent)
        {
            var go = new GameObject(name, typeof(RectTransform));
            go.transform.SetParent(parent, false);
            return go;
        }

        static RectTransform RT(GameObject go) => (RectTransform)go.transform;

        static void Stretch(RectTransform rt, float left = 0f, float bottom = 0f, float right = 0f, float top = 0f)
        {
            rt.anchorMin = Vector2.zero;
            rt.anchorMax = Vector2.one;
            rt.offsetMin = new Vector2(left, bottom);
            rt.offsetMax = new Vector2(-right, -top);
        }

        static Text MakeText(string name, Transform parent, string text, int size, TextAnchor anchor, Color color)
        {
            var go = Node(name, parent);
            var t = go.AddComponent<Text>();
            t.font = UiFont;
            t.fontSize = size;
            t.text = text;
            t.alignment = anchor;
            t.color = color;
            t.supportRichText = false;
            t.horizontalOverflow = HorizontalWrapMode.Wrap;
            t.verticalOverflow = VerticalWrapMode.Overflow;
            return t;
        }

        static Button MakeButton(string name, Transform parent, string label, Action onClick)
        {
            var go = Node(name, parent);
            var img = go.AddComponent<Image>();
            img.color = BgButton;
            var btn = go.AddComponent<Button>();
            btn.targetGraphic = img;
            if (onClick != null) btn.onClick.AddListener(() => onClick());

            var txt = MakeText("Label", go.transform, label, 15, TextAnchor.MiddleCenter, TextMain);
            Stretch(RT(txt.gameObject), 4f, 2f, 4f, 2f);
            return btn;
        }

        static InputField MakeInput(string name, Transform parent, string placeholder)
        {
            var go = Node(name, parent);
            var img = go.AddComponent<Image>();
            img.color = BgField;

            var textGo = Node("Text", go.transform);
            var text = textGo.AddComponent<Text>();
            text.font = UiFont;
            text.fontSize = 15;
            text.color = TextMain;
            text.supportRichText = false;
            text.alignment = TextAnchor.MiddleLeft;
            text.horizontalOverflow = HorizontalWrapMode.Overflow;
            text.verticalOverflow = VerticalWrapMode.Truncate;
            Stretch(RT(textGo), 12f, 4f, 12f, 4f);

            var phGo = Node("Placeholder", go.transform);
            var ph = phGo.AddComponent<Text>();
            ph.font = UiFont;
            ph.fontSize = 15;
            ph.color = new Color(TextDim.r, TextDim.g, TextDim.b, 0.7f);
            ph.text = placeholder;
            ph.supportRichText = false;
            ph.alignment = TextAnchor.MiddleLeft;
            ph.horizontalOverflow = HorizontalWrapMode.Overflow;
            Stretch(RT(phGo), 12f, 4f, 12f, 4f);

            var input = go.AddComponent<InputField>();
            input.targetGraphic = img;
            input.textComponent = text;
            input.placeholder = ph;
            input.lineType = InputField.LineType.SingleLine;
            return input;
        }

        public void SetInputInteractable(bool on)
        {
            if (_actionInput != null) _actionInput.interactable = on;
        }
    }
}
