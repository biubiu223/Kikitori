using System.Reflection;
using Kikitori.Services;
using Kikitori.Views;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;

namespace Kikitori;

public partial class App : Application
{
    public static new App Current => (App)Application.Current;

    public static string Version { get; } =
        (typeof(App).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "1.0.0")
        .Split('+')[0];

    public AppController Controller { get; private set; } = null!;
    public MainWindow? MainWindow { get; private set; }
    public OverlayWindow? Overlay { get; private set; }
    public DispatcherQueue Dispatcher { get; private set; } = null!;
    private TrayIconHost? _tray;
    internal TrayIconHost? Tray => _tray;

    public bool IsExiting { get; private set; }

    public App()
    {
        InitializeComponent();
        // 主窗口关掉只是隐藏到托盘，程序只在用户选“退出”时结束
        DispatcherShutdownMode = DispatcherShutdownMode.OnExplicitShutdown;
        UnhandledException += (_, e) =>
        {
            Log.Error("未处理的异常：" + e.Exception);
            e.Handled = true;
        };
        TaskScheduler.UnobservedTaskException += (_, e) =>
        {
            Log.Error("后台任务异常：" + e.Exception.GetBaseException());
            e.SetObserved();
        };
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Log.Error("进程级未处理异常：" + e.ExceptionObject);
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        Dispatcher = DispatcherQueue.GetForCurrentThread();
        AppPaths.EnsureDirs();
        Log.Info($"听译 v{Version} 启动（PID {Environment.ProcessId}）");
        SettingsService.Load(Dispatcher);

        Controller = new AppController(Dispatcher);
        Controller.OverlayVisibilityChanged += ApplyOverlayVisibility;

        Overlay = new OverlayWindow();
        if (SettingsService.Current.Ui.ShowOverlay) Overlay.ShowOverlay();

        CreateMainWindow();
        MainWindow!.ShowAndActivate();

        try
        {
            _tray = new TrayIconHost();
        }
        catch (Exception e)
        {
            Log.Error("创建托盘图标失败：" + e);
        }

        Program.ActivatedByAnotherInstance += () => Dispatcher.TryEnqueue(ShowMainWindow);

        // 启动引擎；“启动后自动开始监听”开着就在收到 hello 后自动 start
        Controller.Startup(SettingsService.Current.Ui.AutoStart);

        TestHook.StartIfEnabled();
    }

    public void ShowMainWindow()
    {
        if (IsExiting) return;
        try
        {
            if (MainWindow is null) CreateMainWindow();
            MainWindow!.ShowAndActivate();
        }
        catch (Exception e)
        {
            // 窗口对象已失效：重新建一个
            Log.Warn("显示主窗口失败，重新创建：" + e.Message);
            MainWindow = null;
            try { CreateMainWindow(); MainWindow!.ShowAndActivate(); }
            catch (Exception e2) { Log.Error("重新创建主窗口失败：" + e2); }
        }
    }

    private void CreateMainWindow()
    {
        var w = new MainWindow();
        w.Closed += (_, _) =>
        {
            // 正常情况下关闭只是隐藏；万一真的被关掉了，下次需要时重新创建
            if (ReferenceEquals(MainWindow, w)) MainWindow = null;
        };
        MainWindow = w;
    }

    private void ApplyOverlayVisibility()
    {
        if (Overlay is null) return;
        if (SettingsService.Current.Ui.ShowOverlay) Overlay.ShowOverlay();
        else Overlay.HideOverlay();
    }

    /// <summary>主窗口被关掉（隐藏到托盘）。第一次给个提示。</summary>
    public void OnMainWindowHidden()
    {
        var ui = SettingsService.Current.Ui;
        if (!ui.TrayHintShown)
        {
            ui.TrayHintShown = true;
            SettingsService.SaveSoon();
            _tray?.ShowBalloon("听译仍在后台运行",
                "关闭主窗口不会退出程序。双击托盘图标可重新打开；要退出请在托盘图标或字幕条上右键 →「退出」。");
        }
    }

    /// <summary>真正退出（托盘菜单 / 字幕条菜单的“退出”）。</summary>
    public async Task ExitAppAsync()
    {
        if (IsExiting) return;
        IsExiting = true;
        Log.Info("正在退出");
        try
        {
            Overlay?.SaveState();
            MainWindow?.SavePlacement();
            SettingsService.Save();
        }
        catch (Exception e)
        {
            Log.Error("退出时保存设置失败：" + e.Message);
        }
        try { Overlay?.HideOverlay(); } catch { }
        try { MainWindow?.AppWindow.Hide(); } catch { }
        try { _tray?.Dispose(); } catch { }

        await Controller.ShutdownAsync();   // shutdown → 最多等 3 秒 → 强杀
        TestHook.Stop();

        try { Overlay?.Close(); } catch { }
        try { MainWindow?.Close(); } catch { }
        Log.Info("已退出");
        Exit();
    }
}
