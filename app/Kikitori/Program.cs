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
        Log.Info($"[Main] 进程启动 PID={Environment.ProcessId}");
        WinRT.ComWrappersSupport.InitializeComWrappers();
        if (RedirectIfAlreadyRunning())
        {
            Log.Info($"[Main] RedirectIfAlreadyRunning 返回 true，退出当前进程");
            return 0;
        }

        Log.Info($"[Main] 进入 Application.Start");
        Application.Start(p =>
        {
            var context = new DispatcherQueueSynchronizationContext(DispatcherQueue.GetForCurrentThread());
            SynchronizationContext.SetSynchronizationContext(context);
            _ = new App();
        });
        Log.Info($"[Main] Application.Start 正常退出");
        return 0;
    }

    private static EventWaitHandle? _wakeupEvent;

    private static bool RedirectIfAlreadyRunning()
    {
        AppInstance keyInstance;
        try
        {
            _ = AppInstance.GetCurrent().GetActivatedEventArgs();
            keyInstance = AppInstance.FindOrRegisterForKey(InstanceKey);
        }
        catch (Exception e)
        {
            Log.Warn("单实例检查失败，按首个实例运行：" + e.Message);
            return false;
        }

        if (keyInstance.IsCurrent)
        {
            // 注册 Windows App SDK 激活事件
            keyInstance.Activated += (_, _) => ActivatedByAnotherInstance?.Invoke();
            // 同时建立跨进程内核事件监听，确保 unpackaged 模式下 100% 能够收到唤醒
            try
            {
                _wakeupEvent = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\Kikitori_Wakeup_Event");
                Task.Run(() =>
                {
                    while (true)
                    {
                        _wakeupEvent.WaitOne();
                        Log.Info("[Program] 收到另一个实例发来的唤醒信号，拉起主窗口");
                        ActivatedByAnotherInstance?.Invoke();
                    }
                });
            }
            catch (Exception ex)
            {
                Log.Warn("创建唤醒事件监听失败：" + ex.Message);
            }
            return false;
        }

        // 检查 keyInstance 注册的进程是否真实存活
        bool isProcessAlive = false;
        try
        {
            var proc = System.Diagnostics.Process.GetProcessById((int)keyInstance.ProcessId);
            if (!proc.HasExited && proc.ProcessName.Contains("Kikitori", StringComparison.OrdinalIgnoreCase))
            {
                isProcessAlive = true;
            }
        }
        catch
        {
            isProcessAlive = false;
        }

        if (!isProcessAlive)
        {
            Log.Warn($"发现残留的单实例登记 PID={keyInstance.ProcessId}，但对应进程已退出，按首个实例继续运行");
            try
            {
                keyInstance.Activated += (_, _) => ActivatedByAnotherInstance?.Invoke();
            }
            catch { }
            try
            {
                _wakeupEvent = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\Kikitori_Wakeup_Event");
                Task.Run(() =>
                {
                    while (true)
                    {
                        _wakeupEvent.WaitOne();
                        Log.Info("[Program] 收到另一个实例发来的唤醒信号，拉起主窗口");
                        ActivatedByAnotherInstance?.Invoke();
                    }
                });
            }
            catch { }
            return false;
        }

        // 已有实例在真实运行：允许它把窗口拉到前台，并通过内核事件与 COM 双通道唤醒它
        try
        {
            Native.AllowSetForegroundWindow(keyInstance.ProcessId);
            Log.Info($"[Main] 已有实例在运行 (PID={keyInstance.ProcessId})，发出唤醒信号并退出当前进程");

            // 1. 内核事件唤醒（保证 unpackaged 模式 100% 唤醒）
            try
            {
                using var wakeup = EventWaitHandle.OpenExisting(@"Local\Kikitori_Wakeup_Event");
                wakeup.Set();
            }
            catch (Exception exEvent)
            {
                Log.Warn("发送内核唤醒信号失败：" + exEvent.Message);
            }

            // 2. COM 激活重定向（作为辅助兜底）
            try
            {
                var actArgs = AppInstance.GetCurrent().GetActivatedEventArgs();
                keyInstance.RedirectActivationToAsync(actArgs).AsTask().Wait(2000);
            }
            catch { }
        }
        catch (Exception e)
        {
            Log.Warn("转交给已运行的实例失败：" + e.Message);
        }
        return true;
    }
}
