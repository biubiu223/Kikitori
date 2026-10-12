using System.Text.Json.Serialization;
using Kikitori.Services;

namespace Kikitori.Models;

// settings.json 的结构。engine 部分与 docs/protocol.md 第 3 节的 config 一一对应（默认值也相同），
// 只是 API Key 以 DPAPI 密文（api_key_protected）保存；ui 部分是界面自己的设置。

public sealed class AppSettings
{
    public int Version { get; set; } = 1;
    public EngineSettings Engine { get; set; } = new();
    public UiSettings Ui { get; set; } = new();
}

public sealed class EngineSettings
{
    public TranslateSettings Translate { get; set; } = new();
    public AsrSettings Asr { get; set; } = new();
    public VadSettings Vad { get; set; } = new();
    public AudioSettings Audio { get; set; } = new();
    public DownloadSettings Download { get; set; } = new();
    /// <summary>专有名词对照表（保持用户编辑的顺序；发给引擎时转成对象）。</summary>
    public List<GlossaryEntry> Glossary { get; set; } = new();
}

public sealed class TranslateSettings
{
    /// <summary>"deepseek" | "ollama" | "openai_compatible" | "off"</summary>
    public string Provider { get; set; } = "deepseek";
    /// <summary>"zh-Hans" | "zh-Hant" | "en" | "ja" | "ko"</summary>
    public string TargetLanguage { get; set; } = "zh-Hans";
    public int ContextLines { get; set; } = 4;
    public DeepSeekSettings Deepseek { get; set; } = new();
    public OllamaSettings Ollama { get; set; } = new();
    public OpenAiCompatibleSettings OpenaiCompatible { get; set; } = new();
}

public sealed class DeepSeekSettings
{
    /// <summary>明文只在内存里。</summary>
    [JsonIgnore] public string ApiKey { get; set; } = "";

    /// <summary>写进 settings.json 的是 DPAPI 密文。</summary>
    [JsonPropertyName("api_key_protected")]
    public string ApiKeyProtected
    {
        get => Secret.Protect(ApiKey);
        set => ApiKey = Secret.Unprotect(value);
    }

    /// <summary>兼容：用户手动在 settings.json 里写了明文 "api_key" 时读进来，下次保存就变成密文并去掉明文。</summary>
    [JsonPropertyName("api_key")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ApiKeyPlainImport
    {
        get => null;
        set { if (!string.IsNullOrWhiteSpace(value)) ApiKey = value.Trim(); }
    }

    public string BaseUrl { get; set; } = "https://api.deepseek.com";
    public string Model { get; set; } = "deepseek-chat";
    public double Temperature { get; set; } = 1.3;
}

public sealed class OllamaSettings
{
    public string BaseUrl { get; set; } = "http://localhost:11434";
    public string Model { get; set; } = "qwen3:8b";
    public double Temperature { get; set; } = 0.3;
}

public sealed class OpenAiCompatibleSettings
{
    public string BaseUrl { get; set; } = "";
    [JsonIgnore] public string ApiKey { get; set; } = "";

    [JsonPropertyName("api_key_protected")]
    public string ApiKeyProtected
    {
        get => Secret.Protect(ApiKey);
        set => ApiKey = Secret.Unprotect(value);
    }

    [JsonPropertyName("api_key")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ApiKeyPlainImport
    {
        get => null;
        set { if (!string.IsNullOrWhiteSpace(value)) ApiKey = value.Trim(); }
    }

    public string Model { get; set; } = "";
    public double Temperature { get; set; } = 0.7;
}

public sealed class AsrSettings
{
    public string Model { get; set; } = "large-v3";
    public string Device { get; set; } = "cuda";
    public string ComputeType { get; set; } = "float16";
    public string Language { get; set; } = "ja";
    public int BeamSize { get; set; } = 5;
    public bool ShowPartial { get; set; } = true;
    public double PartialInterval { get; set; } = 1.0;
}

public sealed class VadSettings
{
    public double Threshold { get; set; } = 0.5;
    public int MinSilenceMs { get; set; } = 500;
    public double MaxSegmentS { get; set; } = 8.0;
    public int MinSpeechMs { get; set; } = 250;
}

public sealed class AudioSettings
{
    /// <summary>空 = 默认播放设备。</summary>
    public string Device { get; set; } = "";
    /// <summary>测试用：非空时引擎从这个音频文件读取（界面上不提供，手动改 settings.json）。</summary>
    public string File { get; set; } = "";
}

public sealed class DownloadSettings
{
    /// <summary>显卡运行库和识别模型的下载源："auto" | "official" | "china"（protocol 第 7 节）。</summary>
    public string Source { get; set; } = "auto";
}

public sealed class GlossaryEntry
{
    public string Source { get; set; } = "";
    public string Target { get; set; } = "";
}

public sealed class UiSettings
{
    /// <summary>启动后自动开始监听。</summary>
    public bool AutoStart { get; set; } = true;
    public bool ShowOverlay { get; set; } = true;
    /// <summary>第一次关闭主窗口时提示“仍在后台运行”，提示过就不再提示。</summary>
    public bool TrayHintShown { get; set; }
    public OverlaySettings Overlay { get; set; } = new();
    public WindowPlacement? MainWindow { get; set; }
}

public sealed class OverlaySettings
{
    // 位置/宽度都是物理像素；null = 默认（主屏底部居中）
    public int? X { get; set; }
    public int? Bottom { get; set; }
    public int? Width { get; set; }

    /// <summary>译文字号（磅）。</summary>
    public double FontSize { get; set; } = 22;
    /// <summary>原文字号（磅）。</summary>
    public double SourceFontSize { get; set; } = 14;
    public int MaxLines { get; set; } = 3;
    /// <summary>背景不透明度 0.3~1.0（文字始终不透明）。</summary>
    public double Opacity { get; set; } = 0.8;
    public bool ShowSource { get; set; } = true;
    public bool Centered { get; set; }
    /// <summary>文字深色立体阴影（在亮色视频背景下清晰不反光）。</summary>
    public bool TextShadow { get; set; } = true;
}

public sealed class WindowPlacement
{
    public int X { get; set; }
    public int Y { get; set; }
    public int Width { get; set; }
    public int Height { get; set; }
    public bool Maximized { get; set; }
}
