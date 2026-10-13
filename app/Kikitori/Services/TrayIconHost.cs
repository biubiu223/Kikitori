using CommunityToolkit.Mvvm.Input;
using H.NotifyIcon;
using Microsoft.UI.Xaml.Controls;

namespace Kikitori.Services;

/// <summary>托盘图标：双击显示主窗口；右键菜单：显示主窗口、显示/隐藏字幕条、暂停/继续、退出。</summary>
public sealed class TrayIconHost : IDisposable
{
    private readonly TaskbarIcon _icon;
    private readonly MenuFlyoutItem _miOverlay;
    private readonly MenuFlyoutItem _miPause;
    private readonly AppController _ctl = AppController.Instance;

    public TrayIconHost()
    {
        var menu = new MenuFlyout();
        menu.Items.Add(new MenuFlyoutItem { Text = "显示主窗口", Command = new RelayCommand(() => App.Current.ShowMainWindow()) });
        _miOverlay = new MenuFlyoutItem { Command = new RelayCommand(ToggleOverlay) };
        _miPause = new MenuFlyoutItem { Command = new RelayCommand(TogglePause) };
        menu.Items.Add(_miOverlay);
        menu.Items.Add(_miPause);
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(new MenuFlyoutItem { Text = "退出", Command = new RelayCommand(() => _ = App.Current.ExitAppAsync()) });

        _icon = new TaskbarIcon
        {
            ToolTipText = AppPaths.AppName,
            ContextMenuMode = ContextMenuMode.PopupMenu,
            ContextFlyout = menu,
            NoLeftClickDelay = true,
            DoubleClickCommand = new RelayCommand(() => App.Current.ShowMainWindow()),
        };
        if (File.Exists(AppPaths.IconFile))
            _icon.Icon = new System.Drawing.Icon(AppPaths.IconFile);
        try
        {
            _icon.ForceCreate(false);   // false：不要把进程切到“效率模式”，实时字幕不能被降速
        }
        catch (Exception ex)
        {
            Log.Warn("首次创建托盘图标失败，将在后台重试：" + ex.Message);
            Task.Run(async () =>
            {
                await Task.Delay(1500);
                App.Current.Dispatcher?.TryEnqueue(() =>
                {
                    try { _icon.ForceCreate(false); } catch { }
                });
            });
        }

        _ctl.StateChanged += Refresh;
        _ctl.OverlayVisibilityChanged += Refresh;
        Refresh();
    }

    /// <summary>托盘菜单当前的各项文字（测试用）。</summary>
    public IEnumerable<string> MenuTexts => ((MenuFlyout)_icon.ContextFlyout).Items.OfType<MenuFlyoutItem>().Select(i => i.Text);

    /// <summary>执行托盘菜单里某一项的命令（与托盘弹出菜单点下去走同一个 Command；测试用）。</summary>
    public bool InvokeMenuItem(string text)
    {
        var items = ((MenuFlyout)_icon.ContextFlyout).Items.OfType<MenuFlyoutItem>().ToList();
        var item = text.StartsWith('#') && int.TryParse(text[1..], out var idx) && idx >= 0 && idx < items.Count
            ? items[idx]
            : items.FirstOrDefault(i => i.Text == text);
        if (item?.Command is null || !item.IsEnabled) return false;
        item.Command.Execute(null);
        return true;
    }

    /// <summary>双击托盘图标（测试用）。</summary>
    public void InvokeDoubleClick() => _icon.DoubleClickCommand?.Execute(null);

    public void Refresh()
    {
        _miOverlay.Text = SettingsService.Current.Ui.ShowOverlay ? "隐藏字幕条" : "显示字幕条";
        _miPause.Text = _ctl.Paused ? "继续识别" : "暂停识别";
        _miPause.IsEnabled = _ctl.DesiredListening;
        var tip = $"{AppPaths.AppName} · {AppController.StateTitle(_ctl.EngineState)}";
        if (_ctl.IsDownloading && _ctl.ProgressText.Length > 0) tip += " " + _ctl.ProgressText;   // 例如“正在下载显卡运行库 31%”
        if (_ctl.Paused && _ctl.EngineState is "listening" or "paused") tip = $"{AppPaths.AppName} · 已暂停";
        if (tip != _icon.ToolTipText) _icon.ToolTipText = tip;
    }

    public string ToolTip => _icon.ToolTipText ?? "";

    public void ToggleOverlay() => _ctl.SetOverlayVisible(!SettingsService.Current.Ui.ShowOverlay);

    public void TogglePause()
    {
        if (_ctl.DesiredListening) _ctl.SetPaused(!_ctl.Paused);
    }

    public void ShowBalloon(string title, string message)
    {
        try
        {
            _icon.ShowNotification(title, message);
        }
        catch (Exception e)
        {
            Log.Warn("托盘通知失败：" + e.Message);
        }
    }

    public void Dispose()
    {
        _ctl.StateChanged -= Refresh;
        _ctl.OverlayVisibilityChanged -= Refresh;
        _icon.Dispose();
    }
}
