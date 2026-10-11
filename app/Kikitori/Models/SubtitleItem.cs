using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Media;

namespace Kikitori.Models;

/// <summary>一句字幕（同一个 id 先是草稿 partial，再 final，再 translation）。</summary>
public sealed class SubtitleItem : ObservableObject
{
    public int Id { get; init; }
    public DateTime Time { get; set; } = DateTime.Now;
    public string Source { get; set; } = "";
    public string Translation { get; set; } = "";
    public bool IsPartial { get; set; } = true;
    /// <summary>已 final、正在等译文。</summary>
    public bool IsPending { get; set; }
    public bool IsFailed { get; set; }
    public string Error { get; set; } = "";
    /// <summary>"" (草稿) | "translating" | "no_translation" | "same_language"</summary>
    public string Mode { get; set; } = "";
    public string Lang { get; set; } = "";

    public bool IsSameLanguage => Mode == "same_language";
    public bool IsNoTranslation => Mode == "no_translation";

    // ---------- 首页“实时字幕”列表用的显示属性 ----------
    public string TimeText => Time.ToString("HH:mm:ss");

    /// <summary>原文行（草稿不在这里显示，见 PartialText）。same_language 不显示原文行。</summary>
    public string SourceText => IsPartial || IsSameLanguage ? "" : Source;
    public Visibility SourceVisibility => SourceText.Length > 0 ? Visibility.Visible : Visibility.Collapsed;

    public string PartialText => IsPartial ? Source + " …" : "";
    public Visibility PartialVisibility => IsPartial ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>译文（大字）：same_language 时就是原文本身。</summary>
    public string MainText => IsPartial ? "" : IsSameLanguage ? Source : (!IsPending && !IsFailed ? Translation : "");
    public Visibility MainVisibility => MainText.Length > 0 ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>次要提示：“…”（等译文）/“（翻译失败）”。</summary>
    public string HintText => IsPartial ? "" : IsFailed ? "（翻译失败）" : IsPending ? "…" : "";
    public Visibility PendingVisibility => !IsPartial && IsPending && !IsFailed ? Visibility.Visible : Visibility.Collapsed;
    public Visibility FailedVisibility => !IsPartial && IsFailed ? Visibility.Visible : Visibility.Collapsed;

    // 按语言选字体（同一个汉字中日字形不同）
    /// <summary>当前说话语言 / 目标语言（由控制器在语言切换时更新）。</summary>
    public static string CurrentSourceLang { get; set; } = "ja";
    public static string CurrentTargetLang { get; set; } = "zh-Hans";

    public FontFamily SourceFont => FontCache.Get(Languages.FontFor(Lang.Length > 0 && Lang != "zh" ? Lang : CurrentSourceLang == "auto" ? "" : CurrentSourceLang));
    /// <summary>大字（译文；same_language 时是原文本身，也就是目标语言）。</summary>
    public FontFamily MainFont => FontCache.Get(Languages.FontFor(CurrentTargetLang));

    /// <summary>通知所有显示属性刷新。</summary>
    public void Refresh() => OnPropertyChanged(string.Empty);
}

internal static class FontCache
{
    private static readonly Dictionary<string, FontFamily> Cache = new();

    public static FontFamily Get(string families)
    {
        if (!Cache.TryGetValue(families, out var f))
            Cache[families] = f = new FontFamily(families);
        return f;
    }
}

/// <summary>
/// 当前引擎进程的字幕。History 给首页列表（最新在最上面，最多 MaxHistory 条），
/// Changed 给字幕条重新绘制。只在 UI 线程上调用。
/// </summary>
public sealed class SubtitleStore
{
    public const int MaxHistory = 300;

    private readonly Dictionary<int, SubtitleItem> _byId = new();

    public ObservableCollection<SubtitleItem> History { get; } = new();

    /// <summary>任何变化（字幕条据此重绘）。</summary>
    public event Action? Changed;

    public int Count => _byId.Count;

    /// <summary>按 id（时间顺序）排好的最近若干条。</summary>
    public List<SubtitleItem> Latest(int max, Func<SubtitleItem, bool>? filter = null)
    {
        IEnumerable<SubtitleItem> q = _byId.Values.OrderBy(i => i.Id);
        if (filter is not null) q = q.Where(filter);
        var list = q.ToList();
        return list.Count > max ? list.GetRange(list.Count - max, max) : list;
    }

    public void OnPartial(int id, string text)
    {
        if (_byId.TryGetValue(id, out var item))
        {
            if (!item.IsPartial) return;        // 已经 final 了，迟到的草稿不要覆盖
            item.Source = text;
            item.Refresh();
        }
        else
        {
            Add(new SubtitleItem { Id = id, Source = text, IsPartial = true });
        }
        Changed?.Invoke();
    }

    public void OnFinal(int id, string text, string mode, string lang, double? ts)
    {
        if (!_byId.TryGetValue(id, out var item))
        {
            item = new SubtitleItem { Id = id };
            Add(item);
        }
        item.Source = text;
        item.IsPartial = false;
        item.Mode = mode;
        item.Lang = lang;
        item.IsPending = mode == "translating";
        item.IsFailed = false;
        if (ts is > 0)
        {
            try { item.Time = DateTimeOffset.FromUnixTimeMilliseconds((long)(ts.Value * 1000)).LocalDateTime; } catch { }
        }
        item.Refresh();
        Changed?.Invoke();
    }

    public void OnTranslation(int id, bool ok, string text, string error)
    {
        if (!_byId.TryGetValue(id, out var item)) return;
        item.IsPending = false;
        item.IsFailed = !ok;
        item.Translation = ok ? text : "";
        item.Error = error;
        if (item.IsPartial) item.IsPartial = false;
        item.Refresh();
        Changed?.Invoke();
    }

    public void OnRemove(int id)
    {
        if (!_byId.Remove(id, out var item)) return;
        History.Remove(item);
        Changed?.Invoke();
    }

    public void Clear()
    {
        _byId.Clear();
        History.Clear();
        Changed?.Invoke();
    }

    private void Add(SubtitleItem item)
    {
        _byId[item.Id] = item;
        // id 递增 = 时间顺序；新的一般插在最上面，个别乱序的按 id 找位置
        int pos = 0;
        while (pos < History.Count && History[pos].Id > item.Id) pos++;
        History.Insert(pos, item);
        while (History.Count > MaxHistory)
        {
            var old = History[^1];
            History.RemoveAt(History.Count - 1);
            _byId.Remove(old.Id);
        }
    }
}
