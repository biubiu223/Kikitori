using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using Kikitori.Models;
using Microsoft.UI.Dispatching;

namespace Kikitori.Services;

/// <summary>读写 %LOCALAPPDATA%\Kikitori\settings.json。只在 UI 线程上改设置。</summary>
public static class SettingsService
{
    public static AppSettings Current { get; private set; } = new();

    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        NumberHandling = JsonNumberHandling.AllowReadingFromString,
        ReadCommentHandling = JsonCommentHandling.Skip,
        AllowTrailingCommas = true,
    };

    /// <summary>发给引擎的 JSON 用：中文不转义，便于看日志；本身是 UTF-8 无 BOM。</summary>
    public static readonly JsonSerializerOptions WireOptions = new()
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        WriteIndented = false,
    };

    private static DispatcherQueueTimer? _saveTimer;
    private static readonly object FileLock = new();

    public static void Load(DispatcherQueue dq)
    {
        _saveTimer = dq.CreateTimer();
        _saveTimer.Interval = TimeSpan.FromMilliseconds(400);
        _saveTimer.IsRepeating = false;
        _saveTimer.Tick += (_, _) => Save();

        var path = AppPaths.SettingsFile;
        try
        {
            if (File.Exists(path))
            {
                var json = File.ReadAllText(path);
                Current = JsonSerializer.Deserialize<AppSettings>(json, Options) ?? new AppSettings();
            }
            else
            {
                Current = new AppSettings();
            }
        }
        catch (Exception e)
        {
            Log.Error("settings.json 读取失败，已备份为 settings.json.bad 并使用默认设置：" + e.Message);
            try { File.Copy(path, path + ".bad", overwrite: true); } catch { }
            Current = new AppSettings();
        }
        Normalize(Current);
        Save();   // 补齐新字段；顺便把手动写的明文 Key 换成密文
    }

    /// <summary>修正越界/缺失的值，防止手改 settings.json 后程序出错。</summary>
    public static void Normalize(AppSettings s)
    {
        s.Engine ??= new();
        s.Ui ??= new();
        var e = s.Engine;
        e.Translate ??= new();
        e.Asr ??= new();
        e.Vad ??= new();
        e.Audio ??= new();
        e.Glossary ??= new();
        var t = e.Translate;
        t.Deepseek ??= new();
        t.Ollama ??= new();
        t.OpenaiCompatible ??= new();
        if (t.Provider is not ("deepseek" or "ollama" or "openai_compatible" or "off")) t.Provider = "deepseek";
        t.TargetLanguage = Languages.NormalizeTarget(t.TargetLanguage);
        t.ContextLines = Math.Clamp(t.ContextLines, 0, 20);
        t.Deepseek.BaseUrl ??= "https://api.deepseek.com";
        t.Deepseek.Model = string.IsNullOrWhiteSpace(t.Deepseek.Model) ? "deepseek-flash" : t.Deepseek.Model;
        t.Deepseek.Temperature = Math.Clamp(t.Deepseek.Temperature, 0, 2);
        t.Ollama.BaseUrl ??= "http://localhost:11434";
        t.Ollama.Model ??= "qwen3:8b";
        t.Ollama.Temperature = Math.Clamp(t.Ollama.Temperature, 0, 2);
        t.OpenaiCompatible.BaseUrl ??= "";
        t.OpenaiCompatible.Model ??= "";
        t.OpenaiCompatible.Temperature = Math.Clamp(t.OpenaiCompatible.Temperature, 0, 2);

        var a = e.Asr;
        if (string.IsNullOrWhiteSpace(a.Model)) a.Model = "large-v3";
        if (a.Device is not ("cuda" or "cpu")) a.Device = "cuda";
        if (a.ComputeType is not ("float16" or "int8_float16" or "int8")) a.ComputeType = "float16";
        a.Language = Languages.NormalizeSource(a.Language);
        a.BeamSize = Math.Clamp(a.BeamSize, 1, 10);
        a.PartialInterval = Math.Clamp(a.PartialInterval, 0.3, 5);

        var v = e.Vad;
        v.Threshold = Math.Clamp(v.Threshold, 0.1, 0.95);
        v.MinSilenceMs = Math.Clamp(v.MinSilenceMs, 100, 3000);
        v.MaxSegmentS = Math.Clamp(v.MaxSegmentS, 2, 30);
        v.MinSpeechMs = Math.Clamp(v.MinSpeechMs, 0, 2000);
        e.Audio.Device ??= "";
        e.Audio.File ??= "";
        e.Download ??= new();
        if (e.Download.Source is not ("auto" or "official" or "china")) e.Download.Source = "auto";
        e.Glossary.RemoveAll(g => g is null);
        foreach (var g in e.Glossary) { g.Source ??= ""; g.Target ??= ""; }

        var o = s.Ui.Overlay ??= new();
        o.FontSize = Math.Clamp(o.FontSize, 10, 60);
        o.SourceFontSize = Math.Clamp(o.SourceFontSize, 8, 40);
        o.MaxLines = Math.Clamp(o.MaxLines, 1, 6);
        o.Opacity = Math.Clamp(o.Opacity, 0.3, 1.0);
        if (o.Width is < 200) o.Width = null;
    }

    /// <summary>防抖保存（400ms 内多次修改只写一次）。</summary>
    public static void SaveSoon()
    {
        if (_saveTimer is null) { Save(); return; }
        _saveTimer.Stop();
        _saveTimer.Start();
    }

    public static void Save()
    {
        _saveTimer?.Stop();
        try
        {
            var json = JsonSerializer.Serialize(Current, Options);
            lock (FileLock)
            {
                Directory.CreateDirectory(AppPaths.DataDir);
                var path = AppPaths.SettingsFile;
                var tmp = path + ".tmp";
                File.WriteAllText(tmp, json, new System.Text.UTF8Encoding(false));
                File.Move(tmp, path, overwrite: true);
            }
        }
        catch (Exception e)
        {
            Log.Error("保存设置失败：" + e.Message);
        }
    }

    /// <summary>按 docs/protocol.md 第 3 节生成发给引擎的 config（API Key 在这里才解密成明文）。</summary>
    public static JsonObject BuildEngineConfig()
    {
        var e = Current.Engine;
        var t = e.Translate;
        var glossary = new JsonObject();
        foreach (var g in e.Glossary)
        {
            var src = g.Source.Trim();
            if (src.Length == 0 || glossary.ContainsKey(src)) continue;
            glossary[src] = g.Target.Trim();
        }
        return new JsonObject
        {
            ["translate"] = new JsonObject
            {
                ["provider"] = t.Provider,
                ["target_language"] = t.TargetLanguage,
                ["context_lines"] = t.ContextLines,
                ["deepseek"] = new JsonObject
                {
                    ["api_key"] = t.Deepseek.ApiKey.Trim(),
                    ["base_url"] = t.Deepseek.BaseUrl.Trim(),
                    ["model"] = t.Deepseek.Model.Trim(),
                    ["temperature"] = Math.Round(t.Deepseek.Temperature, 2),
                },
                ["ollama"] = new JsonObject
                {
                    ["base_url"] = t.Ollama.BaseUrl.Trim(),
                    ["model"] = t.Ollama.Model.Trim(),
                    ["temperature"] = Math.Round(t.Ollama.Temperature, 2),
                },
                ["openai_compatible"] = new JsonObject
                {
                    ["base_url"] = t.OpenaiCompatible.BaseUrl.Trim(),
                    ["api_key"] = t.OpenaiCompatible.ApiKey.Trim(),
                    ["model"] = t.OpenaiCompatible.Model.Trim(),
                    ["temperature"] = Math.Round(t.OpenaiCompatible.Temperature, 2),
                },
            },
            ["asr"] = new JsonObject
            {
                ["model"] = e.Asr.Model,
                ["device"] = e.Asr.Device,
                ["compute_type"] = e.Asr.ComputeType,
                ["language"] = e.Asr.Language,
                ["beam_size"] = e.Asr.BeamSize,
                ["show_partial"] = e.Asr.ShowPartial,
                ["partial_interval"] = Math.Round(e.Asr.PartialInterval, 2),
            },
            ["vad"] = new JsonObject
            {
                ["threshold"] = Math.Round(e.Vad.Threshold, 3),
                ["min_silence_ms"] = e.Vad.MinSilenceMs,
                ["max_segment_s"] = Math.Round(e.Vad.MaxSegmentS, 2),
                ["min_speech_ms"] = e.Vad.MinSpeechMs,
            },
            ["audio"] = new JsonObject
            {
                ["device"] = e.Audio.Device,
                ["file"] = e.Audio.File,
            },
            ["download"] = new JsonObject
            {
                ["source"] = e.Download.Source,
            },
            ["glossary"] = glossary,
            ["log_dir"] = AppPaths.SubtitleDir,
        };
    }
}
