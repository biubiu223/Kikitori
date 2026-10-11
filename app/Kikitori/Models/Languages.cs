namespace Kikitori.Models;

public sealed record LanguageOption(string Code, string Name)
{
    public override string ToString() => Name;
}

/// <summary>语言表：说话语言（语音识别用）和翻译目标语言，与 engine/kikitori/languages.py 一致。</summary>
public static class Languages
{
    public static readonly IReadOnlyList<LanguageOption> Source =
    [
        new("ja", "日语"),
        new("en", "英语"),
        new("ko", "韩语"),
        new("zh", "中文"),
        new("ru", "俄语"),
        new("fr", "法语"),
        new("es", "西班牙语"),
        new("de", "德语"),
        new("auto", "自动识别"),
    ];

    public static readonly IReadOnlyList<LanguageOption> Target =
    [
        new("zh-Hans", "简体中文"),
        new("zh-Hant", "繁體中文"),
        new("en", "English"),
        new("ja", "日本語"),
        new("ko", "한국어"),
    ];

    public static string NormalizeSource(string? code)
    {
        code = (code ?? "").Trim().ToLowerInvariant();
        return Source.Any(l => l.Code == code) ? code : "ja";
    }

    public static string NormalizeTarget(string? code)
    {
        code = (code ?? "").Trim();
        var hit = Target.FirstOrDefault(l => string.Equals(l.Code, code, StringComparison.OrdinalIgnoreCase));
        return hit?.Code ?? "zh-Hans";
    }

    public static string SourceName(string code) => Source.FirstOrDefault(l => l.Code == code)?.Name ?? code;
    public static string TargetName(string code) => Target.FirstOrDefault(l => l.Code == code)?.Name ?? code;

    /// <summary>
    /// 字幕字体：同一个汉字中日字形不同，韩文要有韩文字形，所以按语言选字体（带后备字体）。
    /// ja: Yu Gothic UI；ko: Malgun Gothic；zh-Hant: Microsoft JhengHei UI；其它：Microsoft YaHei UI。
    /// </summary>
    public static string FontFor(string? code) => code switch
    {
        "ja" => "Yu Gothic UI, Meiryo UI, Meiryo, Microsoft YaHei UI, Segoe UI",
        "ko" => "Malgun Gothic, Microsoft YaHei UI, Segoe UI",
        "zh-Hant" => "Microsoft JhengHei UI, Microsoft JhengHei, Microsoft YaHei UI, Segoe UI",
        _ => "Microsoft YaHei UI, Microsoft YaHei, Segoe UI",
    };

    /// <summary>给 TextBlock.Language 用的 BCP-47 标签，帮助字体回退选对字形。</summary>
    public static string XmlLangFor(string? code) => code switch
    {
        "ja" => "ja-JP",
        "ko" => "ko-KR",
        "zh-Hant" => "zh-TW",
        "zh" or "zh-Hans" => "zh-CN",
        "en" => "en-US",
        "ru" => "ru-RU",
        "fr" => "fr-FR",
        "es" => "es-ES",
        "de" => "de-DE",
        _ => "zh-CN",
    };
}
