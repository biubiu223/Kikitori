using Kikitori.Services;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.Windows.AppLifecycle;

namespace Kikitori;

/// <summary>
/// 自定义入口：单实例。已经有一个在运行时，把这次启动转给它（它会把主窗口显示到前台），然后自己退出。
/// </summary>
public static class Program
{
    public const string InstanceKey = "Kikitori";

    /// <summary>另一个实例被启动时触发（在后台线程上）。</summary>
    public static event Action? ActivatedByAnotherInstance;

    [STAThread]
    private static int Main(string[] args)
    {
        WinRT.ComWrappersSupport.InitializeComWrappers();
        if (RedirectIfAlreadyRunning())
            return 0;

        Application.Start(p =>
        {
            var context = new DispatcherQueueSynchronizationContext(DispatcherQueue.GetForCurrentThread());
            SynchronizationContext.SetSynchronizationContext(context);
            _ = new App();
        });
        return 0;
    }

    private static bool RedirectIfAlreadyRunning()
    {
        AppInstance keyInstance;
        try
        {
            keyInstance = AppInstance.FindOrRegisterForKey(InstanceKey);
        }
        catch (Exception e)
        {
            Log.Warn("单实例检查失败，按首个实例运行：" + e.Message);
            return false;
        }

        if (keyInstance.IsCurrent)
        {
            keyInstance.Activated += (_, _) => ActivatedByAnotherInstance?.Invoke();
            return false;
        }

        // 已有实例在运行：允许它把窗口拉到前台，然后把激活转给它
        try
        {
            Native.AllowSetForegroundWindow(keyInstance.ProcessId);
            var args = AppInstance.GetCurrent().GetActivatedEventArgs();
            var evt = Native.CreateEvent(IntPtr.Zero, true, false, null);
            Task.Run(() =>
            {
                try { keyInstance.RedirectActivationToAsync(args).AsTask().Wait(5000); }
                catch { }
                Native.SetEvent(evt);
            });
            Native.CoWaitForMultipleObjects(0, 8000, 1, [evt], out _);
            Native.CloseHandle(evt);
        }
        catch (Exception e)
        {
            Log.Warn("转交给已运行的实例失败：" + e.Message);
        }
        return true;
    }
}
