using Kikitori.Services;
using Microsoft.UI;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Graphics;

namespace Kikitori.Views;

public sealed partial class MainWindow : Window
{
    private static readonly Dictionary<string, Type> Pages = new()
    {
        ["home"] = typeof(HomePage),
        ["translate"] = typeof(TranslationPage),
        ["asr"] = typeof(AsrPage),
        ["style"] = typeof(StylePage),
        ["glossary"] = typeof(GlossaryPage),
        ["about"] = typeof(AboutPage),
    };

    private readonly AppController _ctl = AppController.Instance;
    private readonly DispatcherQueueTimer _noticeTimer;
    private readonly IntPtr _hwnd;

    public string CurrentPage { get; private set; } = "";

    public MainWindow()
    {
        InitializeComponent();
        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(this);

        Title = AppPaths.AppNameFull;
        SystemBackdrop = new MicaBackdrop();
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        AppWindow.TitleBar.PreferredHeightOption = TitleBarHeightOption.Tall;
        if (File.Exists(AppPaths.IconFile)) AppWindow.SetIcon(AppPaths.IconFile);
        if (File.Exists(AppPaths.LogoFile)) TitleIcon.Source = new BitmapImage(new Uri(AppPaths.LogoFile));
        if (AppWindow.Presenter is OverlappedPresenter p)
        {
            p.PreferredMinimumWidth = 760;
            p.PreferredMinimumHeight = 560;
        }
        RestorePlacement();
        RootGrid.ActualThemeChanged += (_, _) => ApplyCaptionColors();
        ApplyCaptionColors();

        _noticeTimer = DispatcherQueue.CreateTimer();
        _noticeTimer.IsRepeating = false;
        _noticeTimer.Tick += (_, _) => NoticeBar.IsOpen = false;

        AppWindow.Closing += OnClosing;
        _ctl.StateChanged += UpdateEngineBar;
        _ctl.NoticeReceived += ShowNotice;
        Closed += (_, _) =>
        {
            _ctl.StateChanged -= UpdateEngineBar;
            _ctl.NoticeReceived -= ShowNotice;
        };
        UpdateEngineBar();

        NavView.SelectedItem = NavHome;
    }

    // ------------------------------------------------------------------ 导航
    private void NavView_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (args.SelectedItem is NavigationViewItem { Tag: string tag }) Navigate(tag);
    }

    public void Navigate(string tag)
    {
        if (!Pages.TryGetValue(tag, out var type) || CurrentPage == tag) return;
        CurrentPage = tag;
        ContentFrame.Navigate(type, null, new Microsoft.UI.Xaml.Media.Animation.EntranceNavigationTransitionInfo());
        var item = NavView.MenuItems.Concat(NavView.FooterMenuItems).OfType<NavigationViewItem>().FirstOrDefault(i => (string)i.Tag == tag);
        if (item is not null && !ReferenceEquals(NavView.SelectedItem, item)) NavView.SelectedItem = item;
    }

    // ------------------------------------------------------------------ 提示条
    private DateTime _noticeShownAt;

    private void UpdateEngineBar()
    {
        // 设置改动后引擎重新进入 listening：之前的临时提示（比如“没有填 API Key”）已经过时，收起来
        if (NoticeBar.IsOpen && _ctl.EngineState == "listening" && _noticeShownAt < _ctl.LastStartSentAt)
        {
            NoticeBar.IsOpen = false;
            _noticeTimer.Stop();
        }

        var f = _ctl.Failure;
        if (f is null)
        {
            EngineBar.IsOpen = false;
            return;
        }
        EngineBar.Title = f.Title;
        EngineBar.Message = f.Message;
        var tail = f.StderrTail.Count > 0 ? "引擎最后的输出：\n" + string.Join("\n", f.StderrTail.TakeLast(8)) : "";
        EngineTailText.Text = tail;
        EngineTailText.Visibility = tail.Length > 0 ? Visibility.Visible : Visibility.Collapsed;
        EngineBar.IsOpen = true;
    }

    public void ShowNotice(string message, string level)
    {
        if (string.IsNullOrWhiteSpace(message)) return;
        NoticeBar.Severity = level switch
        {
            "error" => InfoBarSeverity.Error,
            "warn" or "warning" => InfoBarSeverity.Warning,
            "success" => InfoBarSeverity.Success,
            _ => InfoBarSeverity.Informational,
        };
        NoticeBar.Title = level switch { "error" => "出错了", "warn" or "warning" => "注意", _ => "提示" };
        NoticeBar.Message = message;
        NoticeBar.IsOpen = true;
        _noticeShownAt = DateTime.Now;
        // 临时提示：普通 5 秒、警告/错误 10 秒后自动消失（持续存在的问题如引擎崩溃用上面的 EngineBar，常驻）
        _noticeTimer.Stop();
        _noticeTimer.Interval = TimeSpan.FromSeconds(level is "error" or "warn" or "warning" ? 10 : 5);
        _noticeTimer.Start();
    }

    public bool IsNoticeOpen => NoticeBar.IsOpen;

    private void RestartEngine_Click(object sender, RoutedEventArgs e)
    {
        EngineBar.IsOpen = false;
        _ctl.RestartEngine();
    }

    private void OpenLogs_Click(object sender, RoutedEventArgs e) => AppPaths.OpenFolder(AppPaths.LogsDir);

    // ------------------------------------------------------------------ 窗口
    public void ShowAndActivate()
    {
        AppWindow.Show();
        Activate();
        Native.BringToFront(_hwnd);
    }

    private void OnClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (App.Current.IsExiting) return;
        // 关闭 = 隐藏到托盘；真正退出只能从托盘或字幕条菜单
        args.Cancel = true;
        SavePlacement();
        AppWindow.Hide();
        App.Current.OnMainWindowHidden();
    }

    private void ApplyCaptionColors()
    {
        var tb = AppWindow.TitleBar;
        bool dark = RootGrid.ActualTheme == ElementTheme.Dark;
        tb.ButtonBackgroundColor = Colors.Transparent;
        tb.ButtonInactiveBackgroundColor = Colors.Transparent;
        tb.ButtonForegroundColor = dark ? Colors.White : Colors.Black;
        tb.ButtonInactiveForegroundColor = dark ? Windows.UI.Color.FromArgb(0xFF, 0x9A, 0x9A, 0x9A) : Windows.UI.Color.FromArgb(0xFF, 0x80, 0x80, 0x80);
        tb.ButtonHoverBackgroundColor = dark ? Windows.UI.Color.FromArgb(0x18, 0xFF, 0xFF, 0xFF) : Windows.UI.Color.FromArgb(0x10, 0, 0, 0);
        tb.ButtonHoverForegroundColor = dark ? Colors.White : Colors.Black;
        tb.ButtonPressedBackgroundColor = dark ? Windows.UI.Color.FromArgb(0x10, 0xFF, 0xFF, 0xFF) : Windows.UI.Color.FromArgb(0x0A, 0, 0, 0);
    }

    private double Scale => Native.GetDpiForWindow(_hwnd) / 96.0;

    private void RestorePlacement()
    {
        var saved = SettingsService.Current.Ui.MainWindow;
        if (saved is { Width: > 300, Height: > 200 })
        {
            var rect = new RectInt32(saved.X, saved.Y, saved.Width, saved.Height);
            var area = DisplayArea.GetFromRect(rect, DisplayAreaFallback.None);
            if (area is not null)
            {
                // 标题栏至少要在某个屏幕里，才能拖得回来
                var wa = area.WorkArea;
                rect.Width = Math.Min(rect.Width, wa.Width);
                rect.Height = Math.Min(rect.Height, wa.Height);
                rect.X = Math.Clamp(rect.X, wa.X - rect.Width + 120, wa.X + wa.Width - 120);
                rect.Y = Math.Clamp(rect.Y, wa.Y, wa.Y + wa.Height - 60);
                AppWindow.MoveAndResize(rect);
                if (saved.Maximized && AppWindow.Presenter is OverlappedPresenter op) op.Maximize();
                return;
            }
        }
        var primary = DisplayArea.Primary.WorkArea;
        double s = Scale;
        int w = Math.Min((int)(1080 * s), primary.Width - 40);
        int h = Math.Min((int)(780 * s), primary.Height - 40);
        AppWindow.MoveAndResize(new RectInt32(primary.X + (primary.Width - w) / 2, primary.Y + (primary.Height - h) / 2, w, h));
    }

    public void SavePlacement()
    {
        try
        {
            if (!AppWindow.IsVisible) return;
            bool maximized = AppWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Maximized };
            bool minimized = AppWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Minimized };
            if (minimized) return;
            var prev = SettingsService.Current.Ui.MainWindow;
            var pos = AppWindow.Position;
            var size = AppWindow.Size;
            SettingsService.Current.Ui.MainWindow = maximized && prev is not null
                ? new Models.WindowPlacement { X = prev.X, Y = prev.Y, Width = prev.Width, Height = prev.Height, Maximized = true }
                : new Models.WindowPlacement { X = pos.X, Y = pos.Y, Width = size.Width, Height = size.Height, Maximized = maximized };
            SettingsService.SaveSoon();
        }
        catch { }
    }
}
