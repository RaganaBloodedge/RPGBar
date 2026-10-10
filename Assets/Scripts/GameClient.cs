// 与 Python 服务端（server/main.py 的 /ws）通信的 WebSocket 客户端。
//
// 设计要点：
// 1) 用 .NET 内置的 ClientWebSocket（Unity 的 .NET Standard 2.1 自带），不引入第三方网络库。
// 2) 收包在后台线程，收到的消息只入队；真正的分发在 Update() 主线程做 —— 避免在后台线程碰 Unity API。
// 3) 发送用信号量串行化，防止多个 async 发送交织导致 WebSocket 帧错乱。
//
// 协议与 web/app.js 完全一致，服务端不区分客户端类型（浏览器 / Unity 走同一套消息）。
using System;
using System.Collections.Generic;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace RPGBar
{
    public class GameClient : MonoBehaviour
    {
        // —— 服务端 → 客户端 ——
        public event Action<JObject> OnWelcome;
        public event Action<JToken> OnState;
        public event Action<string> OnSystemMessage;
        public event Action<JObject> OnNarration;
        public event Action<JObject> OnDice;
        public event Action<JObject> OnSuggestions;
        public event Action<JObject> OnRecap;
        public event Action<JObject> OnAgentStatus;
        public event Action<string> OnErrorMessage;
        /// <summary>(是否已连上, 说明文本)</summary>
        public event Action<bool, string> OnConnection;

        ClientWebSocket _ws;
        CancellationTokenSource _cts;
        readonly Queue<JObject> _inbox = new Queue<JObject>();
        readonly SemaphoreSlim _sendGate = new SemaphoreSlim(1, 1);
        volatile string _pendingNotice;

        public bool Connected => _ws != null && _ws.State == WebSocketState.Open;
        public string Endpoint { get; private set; } = "";
        public string PlayerName { get; private set; } = "";
        public string RoomCode { get; private set; } = "";

        // ---------------------------------------------------------------- 连接

        /// <summary>连接并以 name 加入房间；room 为空表示新建房间（服务端分配房号）。</summary>
        public async void Connect(string wsUrl, string name, string room = "", string script = "")
        {
            Disconnect();
            Endpoint = wsUrl ?? "";
            PlayerName = string.IsNullOrEmpty(name) ? "冒险者" : name.Trim();
            RoomCode = (room ?? "").Trim().ToUpperInvariant();

            _ws = new ClientWebSocket();
            _cts = new CancellationTokenSource();
            try
            {
                await _ws.ConnectAsync(new Uri(Endpoint), _cts.Token);

                var join = new JObject
                {
                    ["type"] = "join",
                    ["name"] = PlayerName,
                    ["character"] = new JObject(),
                };
                if (!string.IsNullOrEmpty(RoomCode)) join["room"] = RoomCode;
                if (!string.IsNullOrEmpty(script)) join["script"] = script;
                await SendRawAsync(join);

                OnConnection?.Invoke(true, "已连接 " + Endpoint);
                _ = Task.Run(() => ReceiveLoop(_cts.Token));
            }
            catch (Exception e)
            {
                _pendingNotice = "连接失败：" + e.Message;
                OnConnection?.Invoke(false, _pendingNotice);
            }
        }

        public void Disconnect()
        {
            try { _cts?.Cancel(); } catch (Exception) { /* 忽略 */ }

            var ws = _ws;
            _ws = null;
            if (ws != null)
            {
                try
                {
                    if (ws.State == WebSocketState.Open || ws.State == WebSocketState.Connecting)
                        _ = ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "bye", CancellationToken.None);
                }
                catch (Exception) { /* 忽略：连接已断 */ }
            }

            try { _cts?.Dispose(); } catch (Exception) { /* 忽略 */ }
            _cts = null;
            lock (_inbox) _inbox.Clear();
        }

        void OnDestroy() => Disconnect();

        // ---------------------------------------------------------------- 发送

        public void SendAction(string text)
        {
            if (string.IsNullOrEmpty(text)) return;
            _ = SendRawAsync(new JObject { ["type"] = "action", ["text"] = text.Trim() });
        }

        public void SendSuggest() => _ = SendRawAsync(new JObject { ["type"] = "suggest" });

        public void SendRoll() => _ = SendRawAsync(new JObject { ["type"] = "roll" });

        /// <summary>运行期改模型槽位。patch 的字段会被平铺到消息顶层（dm / advisor / assistant）。</summary>
        public void SendConfigure(JToken patch)
        {
            var msg = new JObject { ["type"] = "configure" };
            foreach (var kv in JsonUtil.Pairs(patch)) msg[kv.Key] = kv.Value;
            _ = SendRawAsync(msg);
        }

        async Task SendRawAsync(JToken payload)
        {
            var ws = _ws;
            if (ws == null || ws.State != WebSocketState.Open) return;

            await _sendGate.WaitAsync().ConfigureAwait(true);
            try
            {
                if (ws.State != WebSocketState.Open) return;
                var bytes = Encoding.UTF8.GetBytes(JsonUtil.Write(payload));
                await ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, CancellationToken.None);
            }
            catch (Exception e)
            {
                _pendingNotice = "发送失败：" + e.Message;
            }
            finally
            {
                _sendGate.Release();
            }
        }

        // ---------------------------------------------------------------- 接收

        async Task ReceiveLoop(CancellationToken ct)
        {
            var ws = _ws;
            if (ws == null) return;

            var buffer = new byte[8192];
            var sb = new StringBuilder();

            try
            {
                while (!ct.IsCancellationRequested && ws.State == WebSocketState.Open)
                {
                    sb.Length = 0;
                    WebSocketReceiveResult result;
                    do
                    {
                        result = await ws.ReceiveAsync(new ArraySegment<byte>(buffer), ct);
                        if (result.MessageType == WebSocketMessageType.Close)
                        {
                            _pendingNotice = "服务端已关闭连接";
                            return;
                        }
                        sb.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
                    } while (!result.EndOfMessage);

                    if (sb.Length == 0) continue;

                    var parsed = JsonUtil.TryParse(sb.ToString());
                    if (!(parsed is JObject msg))
                    {
                        Debug.LogWarning("[RPGBar] 收到无法解析的消息：" + sb);
                        continue;
                    }
                    lock (_inbox) _inbox.Enqueue(msg);
                }
            }
            catch (OperationCanceledException) { /* 主动断开，正常路径 */ }
            catch (Exception e)
            {
                _pendingNotice = "连接中断：" + e.Message;
            }
        }

        // ---------------------------------------------------------------- 分发

        void Update()
        {
            while (true)
            {
                JObject msg;
                lock (_inbox)
                {
                    if (_inbox.Count == 0) break;
                    msg = _inbox.Dequeue();
                }
                Dispatch(msg);
            }

            var notice = _pendingNotice;
            if (notice != null)
            {
                _pendingNotice = null;
                OnConnection?.Invoke(false, notice);
            }
        }

        void Dispatch(JObject msg)
        {
            switch (msg["type"].Str())
            {
                case "welcome": OnWelcome?.Invoke(msg); break;
                case "state": OnState?.Invoke(msg["state"]); break;
                case "system": OnSystemMessage?.Invoke(msg["text"].Str()); break;
                case "narration": OnNarration?.Invoke(msg); break;
                case "dice": OnDice?.Invoke(msg); break;
                case "suggestions": OnSuggestions?.Invoke(msg); break;
                case "recap": OnRecap?.Invoke(msg); break;
                case "agent_status": OnAgentStatus?.Invoke(msg); break;
                case "error": OnErrorMessage?.Invoke(msg["message"].Str("未知错误")); break;
                default:
                    {
                        string unknown = msg["type"].Str();
                        Debug.Log("[RPGBar] 未知消息类型：" + unknown);
                        break;
                    }
            }
        }
    }
}
