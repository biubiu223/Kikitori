using System.Collections.Specialized;
using Kikitori.Models;
using Kikitori.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;

namespace Kikitori.Views;

public sealed partial class HomePage : Page
{
    private sealed record DeviceOption(string Value, string Display)
    {
        public override string ToString() => Display;
    }

    private readonly AppController _ctl = AppController.Instance;
    private bool _loading = true;   // InitializeComponent 期间控件也会触发事件，加载完才处理

    public HomePage()
    {
        InitializeComponent();
        SourceCombo.ItemsSource = Languages.Source;
        TargetCombo.ItemsSource = Languages.Target;
        SubtitleList.ItemsSource = _ctl.Subtitles.History;
        Loaded += OnLoaded;
        Unloaded += OnUnloaded;
    }

    private void OnLoaded(object sender, RoutedEventArgs e)
    {
        _ctl.StateChanged += UpdateState;
        _ctl.DevicesChanged += UpdateDevices;
        _ctl.LevelChanged += UpdateLevel;
        _ctl.LanguagesChanged += UpdateLanguages;
        _ctl.OverlayVisibilityChanged += UpdateToggles;
        _ctl.Subtitles.History.CollectionChanged += OnHistoryChanged;
        _ctl.EngineSettingsChanged += UpdateSetupHint;
        UpdateSetupHint();
        UpdateState();
        UpdateDevices();
        UpdateLanguages();
        UpdateToggles();
        UpdateLevel(_ctl.Level);
        OnHistoryChanged(null, null);
    }

    private void OnUnloaded(object sender, RoutedEventArgs e)
    {
        _ctl.StateChanged -= UpdateState;
        _ctl.DevicesChanged -= UpdateDevices;
        _ctl.LevelChanged -= UpdateLevel;
        _ctl.LanguagesChanged -= UpdateLanguages;
        _ctl.OverlayVisibilityChanged -= UpdateToggles;
        _ctl.Subtitles.History.CollectionChanged -= OnHistoryChanged;
        _ctl.EngineSettingsChanged -= UpdateSetupHint;
    }

    // ------------------------------------------------------------------ 首次使用引导
    private void UpdateSetupHint()
    {
        var problem = _ctl.TranslationSetupProblem;
        SetupBar.Message = problem ?? "";
        SetupBar.IsOpen = problem is not null;
    }

    private void GoSetup_Click(object sender, RoutedEventArgs e) => App.Current.MainWindow?.Navigate("translate");

    private void OpenAsrSettings_Click(object sender, RoutedEventArgs e) => App.Current.MainWindow?.Navigate("asr");

    // ------------------------------------------------------------------ 状态
    private void UpdateState()
    {
        var state = _ctl.EngineState;
        bool listeningLike = state is "listening" or "paused";
        bool paused = _ctl.Paused && _ctl.DesiredListening && listeningLike;
        string title = paused ? "已暂停" : AppController.StateTitle(state);
        string message = _ctl.StatusMessage;
        if (paused && state == "listening") message = "识别已暂停，录音没有停（打开“暂停识别”开关可继续）";
        StateTitle.Text = title;
        StateMessage.Text = message;
        StateMessage.Visibility = string.IsNullOrWhiteSpace(message) ? Visibility.Collapsed : Visibility.Visible;

        bool downloading = _ctl.IsDownloading;
        bool busy = state is "starting" or "loading_model" or "warming_up" || downloading;
        StateRing.IsActive = busy;
        StateIcon.Visibility = busy && !downloading ? Visibility.Collapsed : Visibility.Visible;
        (string glyph, string brushKey) = state switch
        {
            _ when paused => ("", "StatePausedBrush"),
            "listening" => ("", "StateListeningBrush"),
            "paused" => ("", "StatePausedBrush"),
            "downloading_model" or "downloading_runtime" => ("", "StateBusyBrush"),
            "starting" or "loading_model" or "warming_up" => ("", "StateBusyBrush"),
            "error" or "not_running" => ("", "StateErrorBrush"),
            "stopped" => ("", "StateIdleBrush"),
            _ => ("", "StateIdleBrush"),
        };
        StateIcon.Glyph = glyph;
        var brush = (Brush)Application.Current.Resources[brushKey];
        StateIcon.Foreground = brush;
        StateRing.Foreground = brush;

        DownloadPanel.Visibility = downloading ? Visibility.Visible : Visibility.Collapsed;
        if (_ctl.Progress is double p) { DownloadBar.IsIndeterminate = false; DownloadBar.Value = Math.Clamp(p, 0, 1); }
        else DownloadBar.IsIndeterminate = true;
        DownloadPercent.Text = _ctl.ProgressText;

        // 出错时（没有 NVIDIA 显卡、下载失败…）给个入口去换下载源或改用 CPU
        OpenAsrSettingsButton.Visibility = state == "error" ? Visibility.Visible : Visibility.Collapsed;

        if (_ctl.DesiredListening && downloading)
        {
            // 下载期间“停止”就是取消下载（已下载的部分引擎会保留，下次接着下）
            StartStopText.Text = "取消下载";
            StartStopIcon.Glyph = "";
            StartStopButton.Style = (Style)Application.Current.Resources["DefaultButtonStyle"];
        }
        else if (_ctl.DesiredListening)
        {
            StartStopText.Text = "停止监听";
            StartStopIcon.Glyph = "";
            StartStopButton.Style = (Style)Application.Current.Resources["DefaultButtonStyle"];
        }
        else
        {
            StartStopText.Text = "开始监听";
            StartStopIcon.Glyph = "";
            StartStopButton.Style = (Style)Application.Current.Resources["AccentButtonStyle"];
        }

        _loading = true;
        PauseToggle.IsEnabled = _ctl.DesiredListening && _ctl.EngineReady && listeningLike;
        PauseToggle.IsOn = _ctl.Paused;
        _loading = false;
        if (!listeningLike) UpdateLevel(0);
    }

    private void UpdateLevel(double rms)
    {
        LevelBar.Value = AppController.RmsToMeter(rms);
        bool listening = _ctl.EngineState == "listening" && !_ctl.Paused;
        LevelText.Text = !listening ? "" : rms <= 0.0005 ? "没有声音" : $"{20 * Math.Log10(rms):0} dB";
    }

    private void StartStop_Click(object sender, RoutedEventArgs e)
    {
        if (_ctl.DesiredListening) _ctl.StopListening();
        else _ctl.StartListening();
    }

    private void PauseToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_loading) return;
        _ctl.SetPaused(PauseToggle.IsOn);
    }

    // ------------------------------------------------------------------ 语言
    private void UpdateLanguages()
    {
        _loading = true;
        var e = SettingsService.Current.Engine;
        SourceCombo.SelectedItem = Languages.Source.FirstOrDefault(l => l.Code == e.Asr.Language);
        TargetCombo.SelectedItem = Languages.Target.FirstOrDefault(l => l.Code == e.Translate.TargetLanguage);
        _loading = false;
    }

    private void Language_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loading) return;
        if (SourceCombo.SelectedItem is LanguageOption src && TargetCombo.SelectedItem is LanguageOption tgt)
            _ctl.SetLanguages(src.Code, tgt.Code);
    }

    // ------------------------------------------------------------------ 设备
    private void UpdateDevices()
    {
        _loading = true;
        var current = SettingsService.Current.Engine.Audio.Device;
        var options = new List<DeviceOption> { new("", "默认播放设备") };
        foreach (var d in _ctl.Devices)
            options.Add(new DeviceOption(d.Name, d.IsDefault ? d.Name + "（当前默认）" : d.Name));
        if (current.Length > 0 && options.All(o => o.Value != current))
            options.Add(new DeviceOption(current, current + (_ctl.Devices.Count > 0 ? "（未找到）" : "")));
        DeviceCombo.ItemsSource = options;
        DeviceCombo.SelectedItem = options.First(o => o.Value == current);
        _loading = false;
    }

    private void DeviceCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loading || DeviceCombo.SelectedItem is not DeviceOption opt) return;
        var audio = SettingsService.Current.Engine.Audio;
        if (audio.Device == opt.Value) return;
        audio.Device = opt.Value;
        _ctl.NotifyEngineConfigChanged();
    }

    private void RefreshDevices_Click(object sender, RoutedEventArgs e) => _ctl.RefreshDevices();

    private void Reconnect_Click(object sender, RoutedEventArgs e) => _ctl.ReconnectAudio();

    // ------------------------------------------------------------------ 开关
    private void UpdateToggles()
    {
        _loading = true;
        OverlayToggle.IsOn = SettingsService.Current.Ui.ShowOverlay;
        AutoStartToggle.IsOn = SettingsService.Current.Ui.AutoStart;
        _loading = false;
    }

    private void OverlayToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_loading) return;
        _ctl.SetOverlayVisible(OverlayToggle.IsOn);
    }

    private void AutoStartToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_loading) return;
        SettingsService.Current.Ui.AutoStart = AutoStartToggle.IsOn;
        SettingsService.SaveSoon();
    }

    // ------------------------------------------------------------------ 字幕列表
    private void OnHistoryChanged(object? sender, NotifyCollectionChangedEventArgs? e)
    {
        EmptyHint.Visibility = _ctl.Subtitles.History.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    private void Clear_Click(object sender, RoutedEventArgs e) => _ctl.Subtitles.Clear();

    private void OpenRecords_Click(object sender, RoutedEventArgs e) => AppPaths.OpenFolder(AppPaths.SubtitleDir);
}
