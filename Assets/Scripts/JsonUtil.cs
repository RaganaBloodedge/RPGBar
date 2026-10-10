// JSON 取值的语法糖。
//
// 解析/序列化本身交给 Unity 官方包 com.unity.nuget.newtonsoft-json，这里只负责一件小事：
// 服务端下发的字段「可能没有、可能是 null」，取值时不用到处判空，缺了就落到默认值。
using System.Collections.Generic;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace RPGBar
{
    public static class JsonUtil
    {
        /// <summary>解析一条 JSON；不是合法 JSON（或为空）时返回 null，由调用方决定怎么处理。</summary>
        public static JToken TryParse(string text)
        {
            if (string.IsNullOrEmpty(text)) return null;
            try
            {
                return JToken.Parse(text);
            }
            catch (JsonException)
            {
                return null;
            }
        }

        /// <summary>序列化成紧凑单行 JSON —— WebSocket 一帧一条消息，不需要缩进。</summary>
        public static string Write(JToken token) => token == null ? "null" : token.ToString(Formatting.None);

        /// <summary>按数组取；不是数组（或为 null）时给空列表。</summary>
        public static List<JToken> Arr(JToken token)
        {
            return token is JArray array ? new List<JToken>(array) : new List<JToken>();
        }

        /// <summary>遍历对象字段；不是对象时什么都不返回。</summary>
        public static IEnumerable<KeyValuePair<string, JToken>> Pairs(JToken token)
        {
            if (!(token is JObject obj)) yield break;
            foreach (var p in obj) yield return new KeyValuePair<string, JToken>(p.Key, p.Value);
        }

        // ---------------------------------------------------------------- 取值

        /// <summary>取字符串；缺失 / null 给 def。数字与布尔按字面量转。</summary>
        public static string Str(this JToken token, string def = "")
        {
            if (token == null || token.Type == JTokenType.Null || token.Type == JTokenType.Undefined) return def;
            return token.Type == JTokenType.String ? (string)token : token.ToString();
        }

        /// <summary>取整数；缺失 / null 给 def，浮点四舍五入。</summary>
        public static int Int(this JToken token, int def = 0)
        {
            if (token == null || token.Type == JTokenType.Null || token.Type == JTokenType.Undefined) return def;
            if (token.Type == JTokenType.Integer) return (int)token;
            if (token.Type == JTokenType.Float) return (int)System.Math.Round((double)token);
            return int.TryParse(token.ToString(), out int n) ? n : def;
        }

        /// <summary>取布尔；缺失 / null 给 def。</summary>
        public static bool Bool(this JToken token, bool def = false)
        {
            if (token == null || token.Type == JTokenType.Null || token.Type == JTokenType.Undefined) return def;
            if (token.Type == JTokenType.Boolean) return (bool)token;
            return bool.TryParse(token.ToString(), out bool b) ? b : def;
        }

        /// <summary>是否「有值」——null、JSON null、undefined 都算没有。</summary>
        public static bool Has(this JToken token)
            => token != null && token.Type != JTokenType.Null && token.Type != JTokenType.Undefined;
    }
}
