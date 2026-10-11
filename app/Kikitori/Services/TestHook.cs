using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Kikitori.Services;

/// <summary>
/// 自动化测试用的命名管道（只有设置了环境变量 KIKITORI_TEST_HOOK=1 才会开启，正常使用时不存在）。
/// 管道名：Kikitori.TestHook.&lt;PID&gt;；每行一条命令，回一行 JSON。
/// </summary>
internal static class TestHook
{
    private static CancellationTokenSource? _cts;

    public static void StartIfEnabled()
    {
        if (Environment.GetEnvironmentVariable("KIKITORI_TEST_HOOK") != "1") return;
        _cts = new CancellationTokenSource();
        var name = $"Kikitori.TestHook.{Environment.ProcessId}";
        Log.Info("测试钩子已开启：" + name);
        _ = Task.Run(() => LoopAsync(name, _cts.Token));
    }

    public static void Stop() => _cts?.Cancel();

    private static async Task LoopAsync(string name, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var pipe = new NamedPipeServerStream(name, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                await pipe.WaitForConnectionAsync(ct);
                using var reader = new StreamReader(pipe, new UTF8Encoding(false));
                using var writer = new StreamWriter(pipe, new UTF8Encoding(false)) { AutoFlush = true };
                string? line;
                while ((line = await reader.ReadLineAsync(ct)) is not null)
                {
                    string reply;
                    try { reply = await RunOnUiAsync(line.Trim()); }
                    catch (Exception e) { reply = JsonSerializer.Serialize(new { ok = false, error = e.Message }); }
                    await writer.WriteLineAsync(reply);
                }
            }
            catch (OperationCanceledException) { return; }
            catch (Exception e)
            {
                Log.Warn("测试钩子出错：" + e.Message);
                await Task.Delay(200, CancellationToken.None);
            }
        }
    }

    private static Task<string> RunOnUiAsync(string cmd)
    {
        var tcs = new TaskCompletionSource<string>();
        App.Current.Dispatcher.TryEnqueue(() =>
        {
            try { tcs.SetResult(Handle(cmd)); }
            catch (Exception e) { tcs.SetResult(JsonSerializer.Serialize(new { ok = false, error = e.ToString() })); }
        });
        return tcs.Task;
    }

    private static string Handle(string cmd)
    {
        var app = App.Current;
        var ctl = app.Controller;
        string verb = cmd, arg = "";
        int sep = cmd.IndexOf(' ');
        if (sep > 0) { verb = cmd[..sep]; arg = cmd[(sep + 1)..]; }

        switch (verb)
        {
            case "state":
                return State();
            case "overlay_menu":
                app.Overlay?.OpenMenuForTest();
                return Ok();
            case "overlay_menu_close":
                app.Overlay?.CloseMenu();
                return Ok();
            case "nav":
                app.ShowMainWindow();
                app.MainWindow?.Navigate(arg);
                return Ok();
            case "show_main":
                app.ShowMainWindow();
                return Ok();
            case "close_main":   // 等同于点主窗口右上角的 ×（发 WM_CLOSE）
                if (app.MainWindow is not null)
                    PostMessage(WinRT.Interop.WindowNative.GetWindowHandle(app.MainWindow), 0x0010, IntPtr.Zero, IntPtr.Zero);
                return Ok();
            case "main_rect":    // 截图用：main_rect x y w h（物理像素）
            {
                var v = arg.Split(' ').Select(int.Parse).ToArray();
                app.ShowMainWindow();
                app.MainWindow?.AppWindow.MoveAndResize(new Windows.Graphics.RectInt32(v[0], v[1], v[2], v[3]));
                return Ok();
            }
            case "tray_items":   // 托盘菜单当前文字
                return JsonSerializer.Serialize(new { ok = true, items = app.Tray?.MenuTexts.ToArray() });
            case "tray_menu":    // 执行托盘菜单某一项（和真正点菜单走同一个 Command）
                return app.Tray?.InvokeMenuItem(arg) == true ? Ok() : Fail("tray item not found/disabled: " + arg);
            case "tray_dblclick":
                app.Tray?.InvokeDoubleClick();
                return Ok();
            case "inject":
                ctl.InjectEvent((JsonObject)JsonNode.Parse(arg)!);
                return Ok();
            case "overlay_lines":
                return JsonSerializer.Serialize(new { ok = true, lines = app.Overlay?.DescribeLines(), status = app.Overlay?.StatusLine });
            case "layout":       // 调试：列出主窗口里宽度 >= arg 的元素
            {
                double min = arg.Length > 0 ? double.Parse(arg) : 900;
                var rows = new List<string>();
                var rootEl = app.MainWindow?.Content as Microsoft.UI.Xaml.UIElement;
                void Walk(Microsoft.UI.Xaml.DependencyObject d, int depth)
                {
                    if (d is Microsoft.UI.Xaml.FrameworkElement fe && fe.ActualWidth >= min && rootEl is not null)
                    {
                        var p = fe.TransformToVisual(rootEl).TransformPoint(new Windows.Foundation.Point(0, 0));
                        rows.Add($"{new string(' ', depth)}{fe.GetType().Name} '{fe.Name}' x={p.X:0} w={fe.ActualWidth:0} desired={fe.DesiredSize.Width:0} maxw={fe.MaxWidth} halign={fe.HorizontalAlignment}");
                    }
                    int n = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChildrenCount(d);
                    for (int i = 0; i < n; i++) Walk(Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChild(d, i), depth + 1);
                }
                if (rootEl is not null) Walk(rootEl, 0);
                return JsonSerializer.Serialize(new { ok = true, rows });
            }
            case "exit":
                _ = app.ExitAppAsync();
                return Ok();
            default:
                return Fail("unknown command: " + verb);
        }
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool PostMessage(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);

    private static string Ok() => "{\"ok\":true}";
    private static string Fail(string msg) => JsonSerializer.Serialize(new { ok = false, error = msg });

    private static string State()
    {
        var app = App.Current;
        var ctl = app.Controller;
        var s = SettingsService.Current;
        var ob = app.Overlay?.OverlayBounds;
        var mw = app.MainWindow;
        return JsonSerializer.Serialize(new
        {
            ok = true,
            pid = Environment.ProcessId,
            engine_pid = ctl.Engine.ProcessId,
            engine_running = ctl.Engine.IsRunning,
            engine_ready = ctl.EngineReady,
            engine_state = ctl.EngineState,
            engine_version = ctl.EngineVersion,
            status_message = ctl.StatusMessage,
            failure = ctl.Failure?.Message,
            desired_listening = ctl.DesiredListening,
            paused = ctl.Paused,
            devices = ctl.Devices.Count,
            subtitles = ctl.Subtitles.History.Count,
            source = s.Engine.Asr.Language,
            target = s.Engine.Translate.TargetLanguage,
            overlay_visible = app.Overlay?.IsOverlayVisible,
            overlay = ob is null ? null : new { x = ob.Value.X, y = ob.Value.Y, w = ob.Value.Width, h = ob.Value.Height },
            overlay_menu_open = app.Overlay?.IsMenuOpen,
            overlay_settings = s.Ui.Overlay,
            overlay_hwnd = app.Overlay is null ? 0 : WinRT.Interop.WindowNative.GetWindowHandle(app.Overlay).ToInt64(),
            main_hwnd = mw is null ? 0 : WinRT.Interop.WindowNative.GetWindowHandle(mw).ToInt64(),
            main_visible = mw?.AppWindow.IsVisible,
            notice_open = mw?.IsNoticeOpen,
            setup_problem = ctl.TranslationSetupProblem,
            progress = ctl.Progress,
            tray_tooltip = app.Tray?.ToolTip,
            download_source = s.Engine.Download.Source,
            device = s.Engine.Asr.Device,
            main_page = mw?.CurrentPage,
            settings_file = AppPaths.SettingsFile,
        });
    }
}
