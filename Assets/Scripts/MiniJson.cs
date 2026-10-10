// 极简 JSON 解析 / 序列化 —— 零第三方依赖。
//
// 为什么不用 Unity 内置的 JsonUtility：服务端的消息是「信封 + 动态字段」结构
// （welcome / state / dice ... 字段会随版本演进），JsonUtility 需要为每种消息写死
// 强类型 DTO，服务端一加字段 C# 端就得跟着改。
//
// 为什么不用 Newtonsoft：本机 packages.unity.com 不可达，装不上 UPM 包。
// 这份实现只覆盖 JSON 规范里客户端实际会用到的部分（对象 / 数组 / 字符串 / 数字 / 布尔 / null），
// 足以与服务端 `json.dumps(..., ensure_ascii=False)` 的输出互转。
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace RPGBar
{
    /// <summary>解析后的 JSON 节点。用 kind 判别类型，取值一律走 AsXxx（缺字段有默认值，不抛异常）。</summary>
    public sealed class JsonValue
    {
        public enum Kind { Null, Bool, Number, String, Array, Object }

        public Kind kind = Kind.Null;
        public bool boolValue;
        public double numberValue;
        public string stringValue;
        public List<JsonValue> arrayValue;
        public Dictionary<string, JsonValue> objectValue;

        /// <summary>空节点（等价于 JSON null）。索引越界、字段缺失时也返回它。</summary>
        public static readonly JsonValue Empty = new JsonValue();

        public bool IsNull => kind == Kind.Null;
        public bool IsObject => kind == Kind.Object;
        public bool IsArray => kind == Kind.Array;

        /// <summary>对象取值；非对象或字段缺失返回 Empty。</summary>
        public JsonValue this[string key]
        {
            get
            {
                if (kind == Kind.Object && key != null && objectValue.TryGetValue(key, out var v) && v != null)
                    return v;
                return Empty;
            }
        }

        /// <summary>数组取值；越界返回 Empty。</summary>
        public JsonValue this[int index]
        {
            get
            {
                if (kind == Kind.Array && index >= 0 && index < arrayValue.Count) return arrayValue[index];
                return Empty;
            }
        }

        public int Count => kind == Kind.Array ? arrayValue.Count : (kind == Kind.Object ? objectValue.Count : 0);

        // ---------------------------------------------------------------- 取值

        public string AsString(string fallback = "")
        {
            switch (kind)
            {
                case Kind.String: return stringValue ?? fallback;
                case Kind.Number: return numberValue.ToString(CultureInfo.InvariantCulture);
                case Kind.Bool: return boolValue ? "true" : "false";
                default: return fallback;
            }
        }

        public int AsInt(int fallback = 0)
        {
            switch (kind)
            {
                case Kind.Number: return (int)Math.Round(numberValue);
                case Kind.String:
                    return int.TryParse(stringValue, NumberStyles.Integer, CultureInfo.InvariantCulture, out var n) ? n : fallback;
                case Kind.Bool: return boolValue ? 1 : 0;
                default: return fallback;
            }
        }

        public double AsDouble(double fallback = 0)
        {
            switch (kind)
            {
                case Kind.Number: return numberValue;
                case Kind.String:
                    return double.TryParse(stringValue, NumberStyles.Float, CultureInfo.InvariantCulture, out var d) ? d : fallback;
                default: return fallback;
            }
        }

        public bool AsBool(bool fallback = false)
        {
            switch (kind)
            {
                case Kind.Bool: return boolValue;
                case Kind.Number: return Math.Abs(numberValue) > double.Epsilon;
                case Kind.String: return stringValue == "true" || stringValue == "1";
                default: return fallback;
            }
        }

        /// <summary>数组元素；非数组返回空列表（调用方可以放心 foreach）。</summary>
        public List<JsonValue> AsList() => kind == Kind.Array && arrayValue != null ? arrayValue : new List<JsonValue>();

        /// <summary>遍历对象字段；非对象返回空序列。</summary>
        public IEnumerable<KeyValuePair<string, JsonValue>> AsPairs()
        {
            if (kind != Kind.Object || objectValue == null) yield break;
            foreach (var kv in objectValue) yield return kv;
        }

        // ---------------------------------------------------------------- 构造

        public static JsonValue NewObject() => new JsonValue { kind = Kind.Object, objectValue = new Dictionary<string, JsonValue>() };
        public static JsonValue NewArray() => new JsonValue { kind = Kind.Array, arrayValue = new List<JsonValue>() };
        public static JsonValue Of(string v) => new JsonValue { kind = Kind.String, stringValue = v ?? "" };
        public static JsonValue Of(double v) => new JsonValue { kind = Kind.Number, numberValue = v };
        public static JsonValue Of(int v) => new JsonValue { kind = Kind.Number, numberValue = v };
        public static JsonValue Of(bool v) => new JsonValue { kind = Kind.Bool, boolValue = v };

        /// <summary>作为对象写入一个字段，返回自身（便于链式构造）。</summary>
        public JsonValue Set(string key, JsonValue value)
        {
            if (kind != Kind.Object)
            {
                kind = Kind.Object;
                objectValue = new Dictionary<string, JsonValue>();
            }
            objectValue[key] = value ?? Empty;
            return this;
        }

        public JsonValue Set(string key, string value) => Set(key, Of(value));
        public JsonValue Set(string key, bool value) => Set(key, Of(value));
        public JsonValue Set(string key, int value) => Set(key, Of(value));

        /// <summary>作为数组追加一个元素，返回自身。</summary>
        public JsonValue Add(JsonValue value)
        {
            if (kind != Kind.Array)
            {
                kind = Kind.Array;
                arrayValue = new List<JsonValue>();
            }
            arrayValue.Add(value ?? Empty);
            return this;
        }

        public JsonValue Add(string value) => Add(Of(value));

        // ---------------------------------------------------------------- 序列化

        public string ToJson()
        {
            var sb = new StringBuilder(256);
            Write(sb);
            return sb.ToString();
        }

        void Write(StringBuilder sb)
        {
            switch (kind)
            {
                case Kind.Null:
                    sb.Append("null");
                    break;
                case Kind.Bool:
                    sb.Append(boolValue ? "true" : "false");
                    break;
                case Kind.Number:
                    // 整数不输出小数点，保持与服务端一致的紧凑外观
                    if (Math.Abs(numberValue - Math.Round(numberValue)) < 1e-9 && Math.Abs(numberValue) < 1e15)
                        sb.Append(((long)Math.Round(numberValue)).ToString(CultureInfo.InvariantCulture));
                    else
                        sb.Append(numberValue.ToString("R", CultureInfo.InvariantCulture));
                    break;
                case Kind.String:
                    WriteString(sb, stringValue);
                    break;
                case Kind.Array:
                    sb.Append('[');
                    for (int i = 0; i < arrayValue.Count; i++)
                    {
                        if (i > 0) sb.Append(',');
                        arrayValue[i].Write(sb);
                    }
                    sb.Append(']');
                    break;
                case Kind.Object:
                    sb.Append('{');
                    bool first = true;
                    foreach (var kv in objectValue)
                    {
                        if (!first) sb.Append(',');
                        first = false;
                        WriteString(sb, kv.Key);
                        sb.Append(':');
                        kv.Value.Write(sb);
                    }
                    sb.Append('}');
                    break;
            }
        }

        static void WriteString(StringBuilder sb, string s)
        {
            sb.Append('"');
            if (s != null)
            {
                foreach (char c in s)
                {
                    switch (c)
                    {
                        case '"': sb.Append("\\\""); break;
                        case '\\': sb.Append("\\\\"); break;
                        case '\b': sb.Append("\\b"); break;
                        case '\f': sb.Append("\\f"); break;
                        case '\n': sb.Append("\\n"); break;
                        case '\r': sb.Append("\\r"); break;
                        case '\t': sb.Append("\\t"); break;
                        default:
                            if (c < ' ') sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                            else sb.Append(c);
                            break;
                    }
                }
            }
            sb.Append('"');
        }
    }

    /// <summary>递归下降的 JSON 解析器。</summary>
    public static class MiniJson
    {
        /// <summary>解析一段 JSON 文本；格式非法时抛 FormatException。</summary>
        public static JsonValue Parse(string text)
        {
            if (string.IsNullOrEmpty(text)) throw new FormatException("JSON 文本为空");
            int i = 0;
            SkipWs(text, ref i);
            var v = ParseValue(text, ref i);
            SkipWs(text, ref i);
            if (i < text.Length) throw new FormatException($"JSON 结尾有多余内容（位置 {i}）");
            return v;
        }

        /// <summary>解析失败返回 null，不抛异常（用于网络收包这种「数据不可信」场景）。</summary>
        public static JsonValue TryParse(string text)
        {
            try { return Parse(text); }
            catch (Exception) { return null; }
        }

        static void SkipWs(string s, ref int i)
        {
            while (i < s.Length)
            {
                char c = s[i];
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') i++;
                else break;
            }
        }

        static JsonValue ParseValue(string s, ref int i)
        {
            SkipWs(s, ref i);
            if (i >= s.Length) throw new FormatException("JSON 意外结束");

            char c = s[i];
            switch (c)
            {
                case '{': return ParseObject(s, ref i);
                case '[': return ParseArray(s, ref i);
                case '"': return JsonValue.Of(ParseString(s, ref i));
                case 't': Expect(s, ref i, "true"); return JsonValue.Of(true);
                case 'f': Expect(s, ref i, "false"); return JsonValue.Of(false);
                case 'n': Expect(s, ref i, "null"); return JsonValue.Empty;
                default: return JsonValue.Of(ParseNumber(s, ref i));
            }
        }

        static void Expect(string s, ref int i, string word)
        {
            if (i + word.Length > s.Length || string.CompareOrdinal(s, i, word, 0, word.Length) != 0)
                throw new FormatException($"期望 {word}（位置 {i}）");
            i += word.Length;
        }

        static JsonValue ParseObject(string s, ref int i)
        {
            var o = JsonValue.NewObject();
            i++; // {
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == '}') { i++; return o; }

            while (true)
            {
                SkipWs(s, ref i);
                if (i >= s.Length || s[i] != '"') throw new FormatException($"对象的键必须是字符串（位置 {i}）");
                string key = ParseString(s, ref i);
                SkipWs(s, ref i);
                if (i >= s.Length || s[i] != ':') throw new FormatException($"键 {key} 之后缺少冒号");
                i++; // :
                var val = ParseValue(s, ref i);
                o.objectValue[key] = val;
                SkipWs(s, ref i);
                if (i >= s.Length) throw new FormatException("对象未闭合");
                if (s[i] == ',') { i++; continue; }
                if (s[i] == '}') { i++; return o; }
                throw new FormatException($"对象里出现意外字符 {s[i]}（位置 {i}）");
            }
        }

        static JsonValue ParseArray(string s, ref int i)
        {
            var a = JsonValue.NewArray();
            i++; // [
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == ']') { i++; return a; }

            while (true)
            {
                a.arrayValue.Add(ParseValue(s, ref i));
                SkipWs(s, ref i);
                if (i >= s.Length) throw new FormatException("数组未闭合");
                if (s[i] == ',') { i++; continue; }
                if (s[i] == ']') { i++; return a; }
                throw new FormatException($"数组里出现意外字符 {s[i]}（位置 {i}）");
            }
        }

        static string ParseString(string s, ref int i)
        {
            i++; // 开引号
            var sb = new StringBuilder();
            while (i < s.Length)
            {
                char c = s[i++];
                if (c == '"') return sb.ToString();
                if (c != '\\') { sb.Append(c); continue; }

                if (i >= s.Length) break;
                char e = s[i++];
                switch (e)
                {
                    case '"': sb.Append('"'); break;
                    case '\\': sb.Append('\\'); break;
                    case '/': sb.Append('/'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'u':
                        if (i + 4 <= s.Length)
                        {
                            sb.Append((char)Convert.ToInt32(s.Substring(i, 4), 16));
                            i += 4;
                        }
                        break;
                    default: sb.Append(e); break;
                }
            }
            throw new FormatException("字符串未闭合");
        }

        static double ParseNumber(string s, ref int i)
        {
            int start = i;
            while (i < s.Length)
            {
                char c = s[i];
                if (char.IsDigit(c) || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E') i++;
                else break;
            }
            if (i == start) throw new FormatException($"不是合法的数字（位置 {start}）");
            return double.Parse(s.Substring(start, i - start), NumberStyles.Float, CultureInfo.InvariantCulture);
        }
    }
}
