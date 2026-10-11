using System.Text.Json.Nodes;
using Kikitori.Models;
using Microsoft.UI.Dispatching;

namespace Kikitori.Services;

public sealed record AudioDevice(string Name, bool IsDefault);

/// <summary>引擎没在正常运行（找不到、启动失败、意外退出）。</summary>
public sealed record EngineFailure(string Title, string Message, IReadOnlyList<string> StderrTail);

/// <summary>
/// 程序的“大脑”：管理引擎、维护状态、把设置变化转成引擎命令。所有成员只在 UI 线程上使用。
/// 窗口（主窗口/字幕条/托盘）订阅这里的事件来刷新界面。
/// </summary>
public sealed class AppController
{
    public static AppController Instance { get; private set; } = null!;

    public DispatcherQueue Dispatcher { get; }
    public EngineClient Engine { get; }
    public SubtitleStore Subtitles { get; } = new();

    // ---------- 状态 ----------
    /// <summary>引擎报告的 state；"starting" = 进程已启动还没收到 hello；"not_running" = 没有引擎进程。</summary>
    public string EngineState { get; private set; } = "not_running";
    public string StatusMessage { get; private set; } = "";
    public string StatusLevel { get; private set; } = "info";
    public double? Progress { get; private set; }
    /// <summary>收到 hello 之后为 true（可以发命令）。</summary>
    public bool EngineReady { get; private set; }
    public string EngineVersion { get; private set; } = "";
    /// <summary>用户想不想监听（开始/停止按钮的状态）。引擎重启后据此自动重新 start。</summary>
    public bool DesiredListening { get; private set; }
    public bool Paused { get; private set; }
    public EngineFailure? Failure { get; private set; }
    public string LastFatal { get; private set; } = "";
    public List<AudioDevice> Devices { get; private set; } = new();
    public double Level { get; private set; }

    // ---------- 事件 ----------
    public event Action? StateChanged;
    public event Action<string, string>? NoticeReceived;          // message, level
    public event Action? DevicesChanged;
    public event Action<double>? LevelChanged;
    public event Action<JsonObject>? TranslationTestCompleted;
    public event Action? LanguagesChanged;
    public event Action? OverlayStyleChanged;
    public event Action? OverlayVisibilityChanged;

    private readonly DispatcherQueueTimer _restartTimer;
    private bool _shuttingDown;

    public AppController(DispatcherQueue dq)
    {
        Instance = this;
        Dispatcher = dq;
        Engine = new EngineClient(dq);
        Engine.EventReceived += OnEngineEvent;
        Engine.ExitedUnexpectedly += OnEngineExited;
        _restartTimer = dq.CreateTimer();
        _restartTimer.Interval = TimeSpan.FromMilliseconds(800);
        _restartTimer.IsRepeating = false;
        _restartTimer.Tick += (_, _) =>
        {
            if (DesiredListening && EngineReady) SendStart();
        };
        SubtitleItem.CurrentSourceLang = S.Engine.Asr.Language;
        SubtitleItem.CurrentTargetLang = S.Engine.Translate.TargetLanguage;
        WatchDefaultDevice();
    }

    private static AppSettings S => SettingsService.Current;

    // ================================================================== 引擎进程
    /// <summary>程序启动时调用：启动引擎；autoStart 时收到 hello 后自动开始监听。</summary>
    public void Startup(bool autoStart)
    {
        DesiredListening = autoStart;
        StartEngine();
    }

    public void StartEngine()
    {
        if (_shuttingDown) return;
        EngineReady = false;
        Failure = null;
        LastFatal = "";
        if (Engine.Start(out var error))
        {
            SetState("starting", "正在启动识别引擎…", "info");
        }
        else
        {
            Failure = new EngineFailure("无法启动识别引擎", error, []);
            SetState("not_running", error, "error");
        }
    }

    /// <summary>“重启引擎”按钮。</summary>
    public async void RestartEngine()
    {
        if (_shuttingDown) return;
        EngineReady = false;
        SetState("starting", "正在重启识别引擎…", "info");
        await Engine.StopAsync(1500);
        StartEngine();
    }

    private void OnEngineExited(EngineExitInfo info)
    {
        if (_shuttingDown) return;
        EngineReady = false;
        Level = 0;
        LevelChanged?.Invoke(0);
        var msg = string.IsNullOrEmpty(LastFatal)
            ? $"识别引擎意外退出（退出码 {info.ExitCode?.ToString() ?? "未知"}）。"
            : $"识别引擎出错退出：{LastFatal}";
        Failure = new EngineFailure("识别引擎已停止运行", msg, info.StderrTail);
        Log.Warn(msg);
        SetState("not_running", msg, "error");
    }

    /// <summary>退出程序：发 shutdown，3 秒没退出就强杀。</summary>
    public async Task ShutdownAsync()
    {
        _shuttingDown = true;
        _restartTimer.Stop();
        await Engine.StopAsync(3000);
        Engine.Dispose();
    }

    // ================================================================== 引擎事件
    private void OnEngineEvent(JsonObject e)
    {
        var ev = Str(e, "event");
        switch (ev)
        {
            case "hello":
                EngineReady = true;
                EngineVersion = Str(e, "version");
                Failure = null;
                LastFatal = "";
                Subtitles.Clear();                       // 引擎重启后 id 从 1 开始
                Send(new JsonObject { ["cmd"] = "list_devices" });
                if (DesiredListening) SendStart();
                SetState(DesiredListening ? "starting" : "idle", DesiredListening ? "正在准备开始监听…" : "引擎已就绪", "info");
                break;

            case "status":
            {
                var state = Str(e, "state");
                double? progress = e["progress"] is JsonValue pv && pv.TryGetValue<double>(out var pr) ? pr : null;
                if (state == "paused") Paused = true;
                if (state == "listening" && Paused && DesiredListening)
                    Send(new JsonObject { ["cmd"] = "pause", ["value"] = true });   // 用户要求的暂停在重新 start 后继续保持
                if (state is not ("listening" or "paused")) { Level = 0; LevelChanged?.Invoke(0); }
                Progress = progress;
                SetState(state, Str(e, "message"), Str(e, "level", "info"));
                break;
            }

            case "notice":
                NoticeReceived?.Invoke(Str(e, "message"), Str(e, "level", "info"));
                break;

            case "fatal":
                LastFatal = Str(e, "message");
                NoticeReceived?.Invoke(LastFatal, "error");
                break;

            case "devices":
            {
                var list = new List<AudioDevice>();
                if (e["devices"] is JsonArray arr)
                {
                    foreach (var d in arr.OfType<JsonObject>())
                    {
                        var name = Str(d, "name");
                        if (name.Length > 0) list.Add(new AudioDevice(name, d["is_default"] is JsonValue b && b.TryGetValue<bool>(out var isDef) && isDef));
                    }
                }
                Devices = list;
                DevicesChanged?.Invoke();
                break;
            }

            case "level":
                Level = e["rms"] is JsonValue rv && rv.TryGetValue<double>(out var rms) ? Math.Clamp(rms, 0, 1) : 0;
                LevelChanged?.Invoke(Level);
                break;

            case "partial":
                if (TryId(e, out var pid)) Subtitles.OnPartial(pid, Str(e, "text"));
                break;

            case "final":
                if (TryId(e, out var fid))
                {
                    double? ts = e["ts"] is JsonValue tv && tv.TryGetValue<double>(out var t) ? t : null;
                    Subtitles.OnFinal(fid, Str(e, "text"), Str(e, "mode", "translating"), Str(e, "lang"), ts);
                }
                break;

            case "translation":
                if (TryId(e, out var tid))
                {
                    bool ok = e["ok"] is not JsonValue okv || !okv.TryGetValue<bool>(out var okb) || okb;
                    Subtitles.OnTranslation(tid, ok, Str(e, "text"), Str(e, "error"));
                }
                break;

            case "remove":
                if (TryId(e, out var rid)) Subtitles.OnRemove(rid);
                break;

            case "translation_test":
                TranslationTestCompleted?.Invoke(e);
                break;
        }
    }

    /// <summary>测试钩子用：把一条伪造的事件当成引擎发来的处理。</summary>
    internal void InjectEvent(JsonObject e) => OnEngineEvent(e);

    private void SetState(string state, string message, string level)
    {
        EngineState = state;
        StatusMessage = message;
        StatusLevel = level;
        StateChanged?.Invoke();
    }

    // ================================================================== 用户操作
    public void StartListening()
    {
        DesiredListening = true;
        if (EngineReady) SendStart();
        else if (!Engine.IsRunning) StartEngine();
        StateChanged?.Invoke();
    }

    public void StopListening()
    {
        DesiredListening = false;
        Paused = false;
        _restartTimer.Stop();
        Send(new JsonObject { ["cmd"] = "stop" });
        Level = 0;
        LevelChanged?.Invoke(0);
        StateChanged?.Invoke();
    }

    public void SetPaused(bool paused)
    {
        if (Paused == paused) return;
        Paused = paused;
        Send(new JsonObject { ["cmd"] = "pause", ["value"] = paused });
        StateChanged?.Invoke();
    }

    public void SetLanguages(string source, string target)
    {
        source = Languages.NormalizeSource(source);
        target = Languages.NormalizeTarget(target);
        var e = S.Engine;
        if (e.Asr.Language == source && e.Translate.TargetLanguage == target) return;
        e.Asr.Language = source;
        e.Translate.TargetLanguage = target;
        SubtitleItem.CurrentSourceLang = source;
        SubtitleItem.CurrentTargetLang = target;
        SettingsService.SaveSoon();
        Send(new JsonObject { ["cmd"] = "set_languages", ["source"] = source, ["target"] = target });
        LanguagesChanged?.Invoke();
    }

    public void ReconnectAudio() => Send(new JsonObject { ["cmd"] = "reconnect_audio" });

    /// <summary>
    /// 刷新设备列表。正在录音时引擎的 list_devices 可能返回旧列表（protocol 5.5），所以先 reconnect_audio 再 list_devices。
    /// </summary>
    public void RefreshDevices()
    {
        if (EngineState is "listening" or "paused") Send(new JsonObject { ["cmd"] = "reconnect_audio" });
        Send(new JsonObject { ["cmd"] = "list_devices" });
    }

    /// <summary>Windows 默认播放设备变了（换了耳机/音箱）：引擎不会自己发现，用默认设备时自动重新连接。</summary>
    private void WatchDefaultDevice()
    {
        try
        {
            var debounce = Dispatcher.CreateTimer();
            debounce.Interval = TimeSpan.FromMilliseconds(1500);
            debounce.IsRepeating = false;
            debounce.Tick += (_, _) =>
            {
                if (S.Engine.Audio.Device.Length == 0 && EngineReady && EngineState is "listening" or "paused" or "error")
                {
                    Log.Info("默认播放设备已改变，自动重新连接音频");
                    RefreshDevices();
                }
            };
            Windows.Media.Devices.MediaDevice.DefaultAudioRenderDeviceChanged += (_, e) =>
            {
                if (e.Role != Windows.Media.Devices.AudioDeviceRole.Default) return;
                Dispatcher.TryEnqueue(() => { debounce.Stop(); debounce.Start(); });
            };
        }
        catch (Exception e)
        {
            Log.Warn("无法监听默认播放设备变化：" + e.Message);
        }
    }

    /// <summary>测试翻译连接。引擎没就绪时返回 false。</summary>
    public bool TestTranslation()
    {
        if (!EngineReady) return false;
        return Send(new JsonObject { ["cmd"] = "test_translation", ["config"] = SettingsService.BuildEngineConfig() });
    }

    /// <summary>翻译/识别/断句/设备/专有名词类设置改了：保存；正在监听就 800ms 防抖后重新 start。</summary>
    /// <summary>引擎相关设置被修改（首页据此刷新“还没填 Key”的引导）。</summary>
    public event Action? EngineSettingsChanged;

    /// <summary>最近一次发送 start 的时间：之后重新进入 listening 时，比它早的 notice 视为已过期。</summary>
    public DateTime LastStartSentAt { get; private set; } = DateTime.MinValue;

    /// <summary>翻译配置不完整时给用户看的提示（null = 没问题）。</summary>
    public string? TranslationSetupProblem
    {
        get
        {
            var t = S.Engine.Translate;
            return t.Provider switch
            {
                "deepseek" when t.Deepseek.ApiKey.Trim().Length == 0 =>
                    "还没有填写 API Key，目前只显示原文，不会翻译。",
                "openai_compatible" when t.OpenaiCompatible.BaseUrl.Trim().Length == 0 || t.OpenaiCompatible.Model.Trim().Length == 0 =>
                    "OpenAI 兼容接口还没有填写接口地址或模型，目前只显示原文，不会翻译。",
                _ => null,
            };
        }
    }

    public void NotifyEngineConfigChanged()
    {
        EngineSettingsChanged?.Invoke();
        SettingsService.SaveSoon();
        if (DesiredListening && EngineReady)
        {
            _restartTimer.Stop();
            _restartTimer.Start();
        }
    }

    /// <summary>字幕样式改了：保存并直接应用到字幕条。</summary>
    public void NotifyOverlayStyleChanged()
    {
        SettingsService.SaveSoon();
        OverlayStyleChanged?.Invoke();
    }

    /// <summary>字幕条位置/宽度变了（拖动、调宽度、重置）。</summary>
    public event Action? OverlayGeometryChanged;

    public void NotifyOverlayGeometryChanged()
    {
        SettingsService.SaveSoon();
        OverlayGeometryChanged?.Invoke();
    }

    public void SetOverlayVisible(bool visible)
    {
        if (S.Ui.ShowOverlay == visible) return;
        S.Ui.ShowOverlay = visible;
        SettingsService.SaveSoon();
        OverlayVisibilityChanged?.Invoke();
    }

    private void SendStart()
    {
        _restartTimer.Stop();
        LastStartSentAt = DateTime.Now;
        Send(new JsonObject { ["cmd"] = "start", ["config"] = SettingsService.BuildEngineConfig() });
    }

    private bool Send(JsonObject cmd) => Engine.Send(cmd);

    // ================================================================== 小工具
    private static string Str(JsonObject o, string key, string def = "")
    {
        try
        {
            return o[key] is JsonValue v && v.TryGetValue<string>(out var s) ? s : def;
        }
        catch { return def; }
    }

    private static bool TryId(JsonObject o, out int id)
    {
        id = 0;
        if (o["id"] is not JsonValue v) return false;
        if (v.TryGetValue<int>(out id)) return true;
        if (v.TryGetValue<long>(out var l)) { id = (int)l; return true; }
        if (v.TryGetValue<double>(out var d)) { id = (int)d; return true; }
        return false;
    }

    /// <summary>状态的中文标题。</summary>
    public static string StateTitle(string state) => state switch
    {
        "not_running" => "引擎未运行",
        "starting" => "正在启动",
        "idle" => "未开始监听",
        "downloading_model" => "正在下载模型",
        "loading_model" => "正在加载模型",
        "warming_up" => "正在预热",
        "listening" => "正在监听",
        "paused" => "已暂停",
        "stopped" => "已停止",
        "error" => "出错了",
        _ => state,
    };

    /// <summary>把 RMS 换算成 0~1 的电平（-60dB ~ 0dB），说话声大约在中间。</summary>
    public static double RmsToMeter(double rms)
    {
        if (rms <= 0.0001) return 0;
        var db = 20 * Math.Log10(rms);
        return Math.Clamp((db + 60) / 60, 0, 1);
    }
}
