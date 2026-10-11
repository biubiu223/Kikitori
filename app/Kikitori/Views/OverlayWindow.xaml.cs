using Kikitori.Models;
using Kikitori.Services;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Text;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Windows.Graphics;
using Windows.UI;
using WinUIEx;

namespace Kikitori.Views;

/// <summary>
/// 悬浮字幕条：无边框、置顶、不在任务栏/Alt+Tab 里出现；背景半透明（可调）而文字不透明；
/// 左键拖动移动、右下角拖动调宽度；高度随内容自动伸缩且底边固定；最新一条在最下面；右键菜单。
/// </summary>
public sealed partial class OverlayWindow : Window
{
    // 颜色（与旧版 tkinter 字幕条一致）
    private static readonly Color FgSource = Color.FromArgb(0xFF, 0xAA, 0xB4, 0xC3);
    private static readonly Color FgMain = Color.FromArgb(0xFF, 0xFF, 0xFF, 0xFF);
    private static readonly Color FgPartial = Color.FromArgb(0xFF, 0x7F, 0xB0, 0xFF);
    private static readonly Color FgPending = Color.FromArgb(0xFF, 0x6B, 0x73, 0x80);
    private static readonly Color FgFailed = Color.FromArgb(0xFF, 0xE8, 0x91, 0x8A);
    private static readonly Color BgPanel = Color.FromArgb(0xFF, 0x14, 0x16, 0x1A);
    private static readonly Dictionary<string, Color> StatusColors = new()
    {
        ["info"] = Color.FromArgb(0xFF, 0x8F, 0xD1, 0x8F),
        ["warn"] = Color.FromArgb(0xFF, 0xF0, 0xC0, 0x60),
        ["error"] = Color.FromArgb(0xFF, 0xFF, 0x7B, 0x72),
    };

    private const int MaxRows = 6;
    private const double PtToDip = 96.0 / 72.0;

    private sealed class Row
    {
        public required StackPanel Panel;
        public required TextBlock Source;
        public required TextBlock Main;
    }

    private readonly AppController _ctl = AppController.Instance;
    private readonly IntPtr _hwnd;
    private readonly List<Row> _rows = new();
    private readonly DispatcherQueueTimer _topmostTimer;
    private readonly DispatcherQueueTimer _statusTimer;
    private readonly Dictionary<Color, SolidColorBrush> _brushes = new();

    private enum DragMode { None, Move, Resize }
    private DragMode _drag;
    private Native.POINT _dragStart;
    private PointInt32 _startPos;
    private SizeInt32 _startSize;
    private int _startBottom;

    private string _baseStatus = "", _baseLevel = "info";
    private string? _tempStatus;
    private string _tempLevel = "info";
    private DateTime _tempShownAt;
    private string _lastState = "";
    private bool _menuOpen;

    // 右键菜单
    private MenuFlyout _menu = null!;
    private ToggleMenuFlyoutItem _miPause = null!, _miShowSource = null!, _miCenter = null!;
    private MenuFlyoutSubItem _miFont = null!;
    private readonly List<RadioMenuFlyoutItem> _miSources = new(), _miTargets = new(), _miLines = new(), _miOpacity = new();

    public OverlayWindow()
    {
        InitializeComponent();
        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);
        Title = "听译 字幕条";

        // 整个窗口透明，由 Panel 的半透明背景决定“底色”，文字保持不透明
        SystemBackdrop = new TransparentTintBackdrop();
        if (AppWindow.Presenter is OverlappedPresenter p)
        {
            p.SetBorderAndTitleBar(false, false);
            p.IsResizable = false;
            p.IsMaximizable = false;
            p.IsMinimizable = false;
            p.IsAlwaysOnTop = true;
        }
        AppWindow.IsShownInSwitchers = false;                                    // 不进任务栏 / Alt+Tab
        Native.MakeBorderlessPopup(_hwnd);
        Native.AddExStyle(_hwnd, Native.WS_EX_TOOLWINDOW, Native.WS_EX_APPWINDOW);
        int none = Native.DWMWA_COLOR_NONE, noRound = Native.DWMWCP_DONOTROUND;
        Native.DwmSetWindowAttribute(_hwnd, Native.DWMWA_BORDER_COLOR, ref none, sizeof(int));
        Native.DwmSetWindowAttribute(_hwnd, Native.DWMWA_WINDOW_CORNER_PREFERENCE, ref noRound, sizeof(int));
        if (File.Exists(AppPaths.IconFile)) AppWindow.SetIcon(AppPaths.IconFile);

        for (int i = 0; i < MaxRows; i++)
        {
            var src = new TextBlock { TextWrapping = TextWrapping.Wrap, Visibility = Visibility.Collapsed };
            var main = new TextBlock { TextWrapping = TextWrapping.Wrap, FontWeight = FontWeights.Bold, Visibility = Visibility.Collapsed };
            var panel = new StackPanel { Spacing = 1, Visibility = Visibility.Collapsed };
            panel.Children.Add(src);
            panel.Children.Add(main);
            Lines.Children.Add(panel);
            _rows.Add(new Row { Panel = panel, Source = src, Main = main });
        }
        BuildMenu();

        _topmostTimer = DispatcherQueue.CreateTimer();
        _topmostTimer.Interval = TimeSpan.FromSeconds(2);
        _topmostTimer.Tick += (_, _) => KeepOnTop();
        _statusTimer = DispatcherQueue.CreateTimer();
        _statusTimer.IsRepeating = false;
        _statusTimer.Tick += (_, _) => { _tempStatus = null; UpdateStatus(); };

        _ctl.Subtitles.Changed += Render;
        _ctl.StateChanged += OnStateChanged;
        _ctl.NoticeReceived += (msg, level) => ShowTemp(msg, level, level is "error" or "warn" or "warning" ? 10 : 5);
        _ctl.LanguagesChanged += () => { ApplyFonts(); Render(); };
        _ctl.OverlayStyleChanged += ApplyStyle;
        Root.SizeChanged += (_, _) => FitHeight();
        AppWindow.Closing += (_, e) =>
        {
            if (App.Current.IsExiting) return;
            e.Cancel = true;                 // Alt+F4 = 隐藏字幕条
            _ctl.SetOverlayVisible(false);
        };

        PlaceInitial();
        ApplyStyle();
        OnStateChanged();
    }

    // ================================================================== 显示 / 隐藏
    public bool IsOverlayVisible => AppWindow.IsVisible;

    public void ShowOverlay()
    {
        AppWindow.Show(false);   // 不抢焦点
        KeepOnTop();
        _topmostTimer.Start();
        DispatcherQueue.TryEnqueue(FitHeight);
    }

    public void HideOverlay()
    {
        _topmostTimer.Stop();
        AppWindow.Hide();
    }

    private void KeepOnTop()
    {
        // 有些全屏播放器/其它置顶窗口会盖住字幕条，定期重新置顶（菜单打开时不动）
        if (_menuOpen || !AppWindow.IsVisible) return;
        Native.SetWindowPos(_hwnd, Native.HWND_TOPMOST, 0, 0, 0, 0,
            Native.SWP_NOMOVE | Native.SWP_NOSIZE | Native.SWP_NOACTIVATE | Native.SWP_NOOWNERZORDER);
    }

    // ================================================================== 绘制
    private static OverlaySettings O => SettingsService.Current.Ui.Overlay;

    private SolidColorBrush Brush(Color c)
    {
        if (!_brushes.TryGetValue(c, out var b)) _brushes[c] = b = new SolidColorBrush(c);
        return b;
    }

    private void ApplyStyle()
    {
        var o = O;
        byte a = (byte)Math.Round(Math.Clamp(o.Opacity, 0.3, 1.0) * 255);
        Panel.Background = new SolidColorBrush(Color.FromArgb(a, BgPanel.R, BgPanel.G, BgPanel.B));
        Panel.BorderBrush = new SolidColorBrush(Color.FromArgb((byte)Math.Round(o.Opacity * 0x2C), 0xFF, 0xFF, 0xFF));
        var align = o.Centered ? TextAlignment.Center : TextAlignment.Left;
        foreach (var r in _rows)
        {
            r.Main.FontSize = o.FontSize * PtToDip;
            r.Source.FontSize = o.SourceFontSize * PtToDip;
            r.Main.TextAlignment = align;
            r.Source.TextAlignment = align;
        }
        Placeholder.HorizontalAlignment = o.Centered ? HorizontalAlignment.Center : HorizontalAlignment.Left;
        ApplyFonts();
        Render();
    }

    /// <summary>原文按说话语言选字体，译文按目标语言选字体。</summary>
    private void ApplyFonts()
    {
        var e = SettingsService.Current.Engine;
        var srcLang = e.Asr.Language == "auto" ? "" : e.Asr.Language;
        var tgtLang = e.Translate.TargetLanguage;
        var srcFont = new FontFamily(Languages.FontFor(srcLang));
        var mainFont = new FontFamily(Languages.FontFor(tgtLang));
        foreach (var r in _rows)
        {
            r.Source.FontFamily = srcFont;
            r.Source.Language = Languages.XmlLangFor(srcLang);
            r.Main.FontFamily = mainFont;
            r.Main.Language = Languages.XmlLangFor(tgtLang);
        }
    }

    private static bool HasContent(SubtitleItem i) => i.Source.Length > 0 || i.Translation.Length > 0 || i.IsPending || i.IsFailed;

    /// <summary>当前显示的每一行（测试钩子用）。</summary>
    public List<object> DescribeLines()
    {
        var list = new List<object>();
        foreach (var r in _rows.Where(r => r.Panel.Visibility == Visibility.Visible))
        {
            list.Add(new
            {
                source = r.Source.Visibility == Visibility.Visible ? r.Source.Text : null,
                source_color = r.Source.Visibility == Visibility.Visible ? ColorHex(r.Source.Foreground) : null,
                main = r.Main.Visibility == Visibility.Visible ? r.Main.Text : null,
                main_color = r.Main.Visibility == Visibility.Visible ? ColorHex(r.Main.Foreground) : null,
                main_font = r.Main.FontFamily?.Source,
                source_font = r.Source.FontFamily?.Source,
            });
        }
        return list;
    }

    private static string? ColorHex(Brush? b) => b is SolidColorBrush s ? $"#{s.Color.R:X2}{s.Color.G:X2}{s.Color.B:X2}" : null;

    public void Render()
    {
        var o = O;
        var items = _ctl.Subtitles.Latest(o.MaxLines, i => (o.ShowSource || !i.IsPartial) && HasContent(i));
        double mainSize = o.FontSize * PtToDip;
        for (int r = 0; r < MaxRows; r++)
        {
            var row = _rows[r];
            if (r >= items.Count)
            {
                row.Panel.Visibility = Visibility.Collapsed;
                continue;
            }
            var it = items[r];
            row.Panel.Visibility = Visibility.Visible;

            // 原文行（小字灰色；草稿蓝色带“ …”）
            string? src = null;
            var srcColor = FgSource;
            if (it.IsPartial) { src = it.Source + " …"; srcColor = FgPartial; }
            else if (!it.IsSameLanguage && (o.ShowSource || it.IsNoTranslation)) src = it.Source;
            SetLine(row.Source, src, srcColor);

            // 译文行（大字白色加粗；待翻译灰色“…”；失败“（翻译失败）”）
            string? main = null;
            var mainColor = FgMain;
            bool bold = true;
            double size = mainSize;
            if (!it.IsPartial && !it.IsNoTranslation)
            {
                if (it.IsSameLanguage) main = it.Source;
                else if (it.IsFailed) { main = "（翻译失败）"; mainColor = FgFailed; bold = false; size = Math.Max(O.SourceFontSize * PtToDip, mainSize * 0.75); }
                else if (it.IsPending) { main = "…"; mainColor = FgPending; }
                else if (it.Translation.Length > 0) main = it.Translation;
            }
            SetLine(row.Main, main, mainColor);
            row.Main.FontWeight = bold ? FontWeights.Bold : FontWeights.Normal;
            row.Main.FontSize = size;
        }
        Placeholder.Visibility = items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        FitHeight();
    }

    private void SetLine(TextBlock tb, string? text, Color color)
    {
        if (string.IsNullOrEmpty(text))
        {
            tb.Visibility = Visibility.Collapsed;
            return;
        }
        tb.Text = text;
        tb.Foreground = Brush(color);
        tb.Visibility = Visibility.Visible;
    }

    // ================================================================== 尺寸 / 位置
    private double Scale => Math.Max(1.0, Native.GetDpiForWindow(_hwnd) / 96.0);

    public int MinWidthPx => (int)(320 * Scale);

    public int MaxWidthPx
    {
        get
        {
            var area = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Nearest);
            return area.OuterBounds.Width;
        }
    }

    public int CurrentWidthPx => AppWindow.Size.Width;

    /// <summary>按内容调整窗口高度，保持底边不动（像字幕一样往上长）。</summary>
    private void FitHeight()
    {
        if (_menuOpen) return;   // 菜单关掉后再调整
        double scale = Scale;
        var size = AppWindow.Size;
        var pos = AppWindow.Position;
        if (size.Width <= 0) return;
        double widthDip = size.Width / scale;
        Panel.Measure(new Windows.Foundation.Size(widthDip, double.PositiveInfinity));
        int need = (int)Math.Ceiling(Panel.DesiredSize.Height * scale);
        var area = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Nearest);
        need = Math.Clamp(need, (int)(40 * scale), Math.Max((int)(40 * scale), area.OuterBounds.Height));
        if (Math.Abs(need - size.Height) < 2) return;
        int bottom = pos.Y + size.Height;
        AppWindow.MoveAndResize(new RectInt32(pos.X, bottom - need, size.Width, need));
    }

    /// <summary>启动时摆放：用上次的位置/宽度，防止跑到屏幕外（例如上次在副屏、这次副屏没接）。</summary>
    private void PlaceInitial()
    {
        var o = O;
        var primary = DisplayArea.Primary;
        double scale = Scale;
        int h = (int)(60 * scale);
        int w = o.Width ?? (int)(960 * scale);
        int x = o.X ?? primary.WorkArea.X + (primary.WorkArea.Width - w) / 2;
        int bottom = o.Bottom ?? primary.WorkArea.Y + primary.WorkArea.Height - (int)(64 * scale);

        var area = DisplayArea.GetFromRect(new RectInt32(x, bottom - h, w, h), DisplayAreaFallback.Nearest);
        var b = area.OuterBounds;
        w = Math.Clamp(w, Math.Min(MinWidthPx, b.Width), b.Width);
        x = Math.Clamp(x, b.X, b.X + b.Width - w);
        bottom = Math.Clamp(bottom, b.Y + h, b.Y + b.Height);
        AppWindow.MoveAndResize(new RectInt32(x, bottom - h, w, h));
    }

    public void ResetPosition()
    {
        var o = O;
        o.X = o.Bottom = o.Width = null;
        PlaceInitial();
        FitHeight();
        SettingsService.SaveSoon();
        _ctl.NotifyOverlayGeometryChanged();
    }

    public void SetWidth(int width)
    {
        var area = DisplayArea.GetFromWindowId(AppWindow.Id, DisplayAreaFallback.Nearest).OuterBounds;
        width = Math.Clamp(width, MinWidthPx, area.Width);
        var pos = AppWindow.Position;
        var size = AppWindow.Size;
        int x = Math.Clamp(pos.X, area.X, area.X + area.Width - width);
        AppWindow.MoveAndResize(new RectInt32(x, pos.Y, width, size.Height));
        SaveState();
    }

    /// <summary>记住位置（左边 + 底边）和宽度。</summary>
    public void SaveState()
    {
        var o = O;
        var pos = AppWindow.Position;
        var size = AppWindow.Size;
        if (size.Width <= 0) return;
        o.X = pos.X;
        o.Bottom = pos.Y + size.Height;
        o.Width = size.Width;
        SettingsService.SaveSoon();
    }

    public RectInt32 OverlayBounds => new(AppWindow.Position.X, AppWindow.Position.Y, AppWindow.Size.Width, AppWindow.Size.Height);

    // ================================================================== 拖动 / 调宽度
    private void Root_PointerPressed(object sender, PointerRoutedEventArgs e)
    {
        if (!e.GetCurrentPoint(Root).Properties.IsLeftButtonPressed) return;
        BeginDrag(DragMode.Move, e);
    }

    private void Grip_PointerPressed(object sender, PointerRoutedEventArgs e)
    {
        if (!e.GetCurrentPoint(Grip).Properties.IsLeftButtonPressed) return;
        BeginDrag(DragMode.Resize, e);
    }

    private void BeginDrag(DragMode mode, PointerRoutedEventArgs e)
    {
        _drag = mode;
        Native.GetCursorPos(out _dragStart);
        _startPos = AppWindow.Position;
        _startSize = AppWindow.Size;
        _startBottom = _startPos.Y + _startSize.Height;
        Root.CapturePointer(e.Pointer);
        // 窗口一边移动/改大小，XAML 的指针捕获可能会丢；所以另外用定时器直接读鼠标位置和按键状态，直到松开左键
        _dragTimer ??= CreateDragTimer();
        _dragTimer.Start();
        e.Handled = true;
    }

    private DispatcherQueueTimer? _dragTimer;

    private DispatcherQueueTimer CreateDragTimer()
    {
        var t = DispatcherQueue.CreateTimer();
        t.Interval = TimeSpan.FromMilliseconds(8);
        t.Tick += (_, _) =>
        {
            if (!Native.IsPrimaryButtonDown()) { EndDrag(); return; }
            ApplyDrag();
        };
        return t;
    }

    private void Root_PointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (_drag == DragMode.None) return;
        ApplyDrag();
        e.Handled = true;
    }

    private void ApplyDrag()
    {
        if (_drag == DragMode.None) return;
        Native.GetCursorPos(out var p);
        int dx = p.X - _dragStart.X, dy = p.Y - _dragStart.Y;
        if (_drag == DragMode.Move)
        {
            var size = AppWindow.Size;
            AppWindow.Move(new PointInt32(_startPos.X + dx, _startBottom + dy - size.Height));
        }
        else
        {
            int w = Math.Clamp(_startSize.Width + dx, MinWidthPx, Math.Max(MinWidthPx, MaxWidthPx));
            var size = AppWindow.Size;
            if (w != size.Width) AppWindow.Resize(new SizeInt32(w, size.Height));
        }
    }

    private void Root_PointerReleased(object sender, PointerRoutedEventArgs e) => EndDrag();

    // 捕获丢了不代表松手（定时器会检测真正的松开）
    private void Root_PointerCaptureLost(object sender, PointerRoutedEventArgs e) { }

    private void EndDrag()
    {
        _dragTimer?.Stop();
        if (_drag == DragMode.None) return;
        ApplyDrag();
        _drag = DragMode.None;
        Root.ReleasePointerCaptures();
        SaveState();
        _ctl.NotifyOverlayGeometryChanged();
    }

    // ================================================================== 状态行
    private void OnStateChanged()
    {
        var state = _ctl.EngineState;
        string msg = _ctl.StatusMessage;
        bool pausedNow = _ctl.Paused && _ctl.DesiredListening;
        (_baseStatus, _baseLevel) = state switch
        {
            "not_running" => ("⚠ 识别引擎没有运行（右键 →「打开主窗口」可重启引擎）", "error"),
            "starting" => ("正在启动识别引擎…", "info"),
            "idle" => (_ctl.DesiredListening ? "正在准备…" : "未开始监听（右键 →「打开主窗口」→「开始监听」）", _ctl.DesiredListening ? "info" : "warn"),
            // 首次运行下载：百分比放最前面，消息太长被截断时也能看到进度
            "downloading_runtime" => (DownloadLine("正在下载显卡运行库", msg), "info"),
            "downloading_model" => (DownloadLine("正在下载识别模型", msg), "info"),
            "loading_model" or "warming_up" => (msg.Length > 0 ? msg : "正在加载语音识别模型…", "info"),
            "listening" when pausedNow => ("⏸ 已暂停（右键 → 取消「暂停识别」继续）", "warn"),
            "listening" => ("", "info"),
            "paused" => ("⏸ 已暂停（右键 → 取消「暂停识别」继续）", "warn"),
            "stopped" => ("■ 已停止监听", "warn"),
            "error" => ("⚠ " + (msg.Length > 0 ? msg : "出错了"), "error"),
            _ => (msg, "info"),
        };
        // 设置改动后重新进入 listening：比这次 start 更早的临时提示已经过时
        if (state == "listening" && _tempStatus is not null && _tempShownAt < _ctl.LastStartSentAt)
        {
            _tempStatus = null;
            _statusTimer.Stop();
        }
        if (state == "listening" && _lastState != "listening" && !pausedNow && msg.Length > 0)
            ShowTemp("▶ " + msg, "info", 4);
        else if (state == "listening" && _lastState == "paused")
            ShowTemp("▶ 继续识别", "info", 3);
        _lastState = state;
        UpdateStatus();
    }

    private string DownloadLine(string title, string msg)
    {
        var pct = _ctl.ProgressText;
        var head = pct.Length > 0 ? $"⬇ {pct} " : "⬇ ";
        return head + (msg.Length > 0 ? msg : title + "（只需一次）…");
    }

    private void ShowTemp(string text, string level, double seconds)
    {
        if (string.IsNullOrWhiteSpace(text)) return;
        _tempStatus = text;
        _tempLevel = level;
        _tempShownAt = DateTime.Now;
        _statusTimer.Stop();
        _statusTimer.Interval = TimeSpan.FromSeconds(seconds);
        _statusTimer.Start();
        UpdateStatus();
    }

    private void UpdateStatus()
    {
        var (text, level) = _tempStatus is not null ? (_tempStatus, _tempLevel) : (_baseStatus, _baseLevel);
        StatusText.Text = text;
        StatusText.Foreground = Brush(StatusColors.GetValueOrDefault(level == "warning" ? "warn" : level, StatusColors["info"]));
    }

    public string StatusLine => StatusText.Text;

    // ================================================================== 右键菜单
    private static FontIcon Glyph(string g) => new() { Glyph = g };

    /// <summary>给菜单项设 AutomationId（UI 自动化测试用）。</summary>
    private static void Id(DependencyObject d, string id) => Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(d, id);

    private void BuildMenu()
    {
        _menu = new MenuFlyout { ShouldConstrainToRootBounds = false };   // 字幕条很矮，菜单要能伸出窗口
        // 菜单打开期间不改窗口大小：窗口一动，WinUI 会把弹出菜单当成“点到外面”自动关掉
        _menu.Opening += (_, _) => { _menuOpen = true; SyncMenu(); };
        _menu.Opened += (_, _) => _menuOpen = true;
        _menu.Closed += (_, _) => { _menuOpen = false; FitHeight(); };

        _miPause = new ToggleMenuFlyoutItem { Text = "暂停识别", Icon = Glyph("") };
        _miPause.Click += (_, _) =>
        {
            if (_ctl.DesiredListening) _ctl.SetPaused(_miPause.IsChecked);
            else _miPause.IsChecked = false;
        };
        Id(_miPause, "menu_pause"); _menu.Items.Add(_miPause);
        _menu.Items.Add(new MenuFlyoutSeparator());

        var src = new MenuFlyoutSubItem { Text = "说话语言", Icon = Glyph("") }; Id(src, "menu_source");
        foreach (var l in Languages.Source)
        {
            var item = new RadioMenuFlyoutItem { Text = l.Name, GroupName = "kikitori_source", Tag = l.Code }; Id(item, "menu_source_" + l.Code);
            item.Click += (_, _) => _ctl.SetLanguages(l.Code, SettingsService.Current.Engine.Translate.TargetLanguage);
            src.Items.Add(item);
            _miSources.Add(item);
        }
        _menu.Items.Add(src);

        var tgt = new MenuFlyoutSubItem { Text = "翻译成", Icon = Glyph("") }; Id(tgt, "menu_target");
        foreach (var l in Languages.Target)
        {
            var item = new RadioMenuFlyoutItem { Text = l.Name, GroupName = "kikitori_target", Tag = l.Code }; Id(item, "menu_target_" + l.Code);
            item.Click += (_, _) => _ctl.SetLanguages(SettingsService.Current.Engine.Asr.Language, l.Code);
            tgt.Items.Add(item);
            _miTargets.Add(item);
        }
        _menu.Items.Add(tgt);
        _menu.Items.Add(new MenuFlyoutSeparator());

        _miShowSource = new ToggleMenuFlyoutItem { Text = "显示原文" };
        _miShowSource.Click += (_, _) => { O.ShowSource = _miShowSource.IsChecked; _ctl.NotifyOverlayStyleChanged(); };
        Id(_miShowSource, "menu_show_source"); _menu.Items.Add(_miShowSource);
        _miCenter = new ToggleMenuFlyoutItem { Text = "字幕居中" };
        _miCenter.Click += (_, _) => { O.Centered = _miCenter.IsChecked; _ctl.NotifyOverlayStyleChanged(); };
        Id(_miCenter, "menu_center"); _menu.Items.Add(_miCenter);

        _miFont = new MenuFlyoutSubItem { Text = "字号", Icon = Glyph("") };
        var bigger = new MenuFlyoutItem { Text = "放大  A+", Icon = Glyph("") };
        bigger.Click += (_, _) => ChangeFont(+2);
        var smaller = new MenuFlyoutItem { Text = "缩小  A-", Icon = Glyph("") };
        smaller.Click += (_, _) => ChangeFont(-2);
        Id(_miFont, "menu_font"); Id(bigger, "menu_font_up"); Id(smaller, "menu_font_down"); _miFont.Items.Add(bigger);
        _miFont.Items.Add(smaller);
        _menu.Items.Add(_miFont);

        var lines = new MenuFlyoutSubItem { Text = "显示条数", Icon = Glyph("") }; Id(lines, "menu_lines");
        for (int n = 1; n <= 6; n++)
        {
            int count = n;
            var item = new RadioMenuFlyoutItem { Text = $"{n} 条", GroupName = "kikitori_lines", Tag = n }; Id(item, $"menu_lines_{n}");
            item.Click += (_, _) => { O.MaxLines = count; _ctl.NotifyOverlayStyleChanged(); };
            lines.Items.Add(item);
            _miLines.Add(item);
        }
        _menu.Items.Add(lines);

        var opacity = new MenuFlyoutSubItem { Text = "不透明度", Icon = Glyph("") }; Id(opacity, "menu_opacity");
        foreach (var v in new[] { 100, 90, 80, 70, 60, 50, 40 })
        {
            int value = v;
            var item = new RadioMenuFlyoutItem { Text = $"{v}%", GroupName = "kikitori_opacity", Tag = v }; Id(item, $"menu_opacity_{v}");
            item.Click += (_, _) => { O.Opacity = value / 100.0; _ctl.NotifyOverlayStyleChanged(); };
            opacity.Items.Add(item);
            _miOpacity.Add(item);
        }
        _menu.Items.Add(opacity);
        _menu.Items.Add(new MenuFlyoutSeparator());

        var clear = new MenuFlyoutItem { Text = "清空字幕", Icon = Glyph("") };
        clear.Click += (_, _) => _ctl.Subtitles.Clear();
        Id(clear, "menu_clear"); _menu.Items.Add(clear);
        var reconnect = new MenuFlyoutItem { Text = "重新连接音频设备", Icon = Glyph("") };
        reconnect.Click += (_, _) => _ctl.ReconnectAudio();
        Id(reconnect, "menu_reconnect"); _menu.Items.Add(reconnect);
        _menu.Items.Add(new MenuFlyoutSeparator());

        var openMain = new MenuFlyoutItem { Text = "打开主窗口", Icon = Glyph("") };
        openMain.Click += (_, _) => App.Current.ShowMainWindow();
        Id(openMain, "menu_open_main"); _menu.Items.Add(openMain);
        var exit = new MenuFlyoutItem { Text = "退出", Icon = Glyph("") };
        exit.Click += (_, _) => _ = App.Current.ExitAppAsync();
        Id(exit, "menu_exit"); _menu.Items.Add(exit);
    }

    private void SyncMenu()
    {
        var o = O;
        var e = SettingsService.Current.Engine;
        _miPause.IsEnabled = _ctl.DesiredListening;
        _miPause.IsChecked = _ctl.Paused && _ctl.DesiredListening;
        foreach (var i in _miSources) i.IsChecked = (string)i.Tag == e.Asr.Language;
        foreach (var i in _miTargets) i.IsChecked = (string)i.Tag == e.Translate.TargetLanguage;
        _miShowSource.IsChecked = o.ShowSource;
        _miCenter.IsChecked = o.Centered;
        _miFont.Text = $"字号（{o.FontSize:0}）";
        foreach (var i in _miLines) i.IsChecked = (int)i.Tag == o.MaxLines;
        int op = (int)(Math.Round(o.Opacity * 10) * 10);
        foreach (var i in _miOpacity) i.IsChecked = (int)i.Tag == op;
    }

    private void ChangeFont(int delta)
    {
        var o = O;
        o.FontSize = Math.Clamp(o.FontSize + delta, 10, 60);
        o.SourceFontSize = Math.Clamp(o.SourceFontSize + (delta > 0 ? 1 : -1), 8, 40);
        _ctl.NotifyOverlayStyleChanged();
    }

    private void Root_RightTapped(object sender, RightTappedRoutedEventArgs e)
    {
        _menu.ShowAt(Root, new FlyoutShowOptions { Position = e.GetPosition(Root) });
        e.Handled = true;
    }

    /// <summary>测试钩子：用代码打开右键菜单。</summary>
    public void OpenMenuForTest()
    {
        bool fg = Native.ForceForeground(_hwnd);
        _menu.ShowAt(Root, new FlyoutShowOptions { Position = new Windows.Foundation.Point(60, 24) });
        Log.Info($"测试：打开字幕条菜单（前台 {fg}，IsOpen {_menu.IsOpen}）");
    }

    public void CloseMenu() => _menu.Hide();

    public bool IsMenuOpen => _menuOpen;
}
