namespace Kikitori.Services;

/// <summary>程序用到的所有路径。用户数据放在 %LOCALAPPDATA%\Kikitori\。</summary>
public static class AppPaths
{
    public const string AppName = "听译";
    public const string AppNameFull = "听译 Kikitori";
    public const string ProjectUrl = "https://github.com/biubiu223/Kikitori";

    /// <summary>程序（Kikitori.exe）所在目录。</summary>
    public static string ExeDir { get; } = AppContext.BaseDirectory;

    /// <summary>
    /// 用户数据目录。默认 %LOCALAPPDATA%\Kikitori；
    /// 测试时可以用环境变量 KIKITORI_DATA_DIR 指到别处，避免弄脏真实设置。
    /// </summary>
    public static string DataDir { get; } = ResolveDataDir();

    public static string SettingsFile => Path.Combine(DataDir, "settings.json");
    public static string LogsDir => Path.Combine(DataDir, "logs");
    public static string EngineLog => Path.Combine(LogsDir, "engine.log");
    public static string AppLog => Path.Combine(LogsDir, "app.log");
    public static string SubtitleDir => Path.Combine(DataDir, "字幕记录");

    public static string IconFile => Path.Combine(ExeDir, "Assets", "app.ico");
    public static string LogoFile => Path.Combine(ExeDir, "Assets", "logo.png");

    /// <summary>打包后的引擎位置：&lt;exe 目录&gt;\engine\KikitoriEngine.exe</summary>
    public static string BundledEngine => Path.Combine(ExeDir, "engine", "KikitoriEngine.exe");

    private static string ResolveDataDir()
    {
        var overrideDir = Environment.GetEnvironmentVariable("KIKITORI_DATA_DIR");
        if (!string.IsNullOrWhiteSpace(overrideDir))
            return Path.GetFullPath(overrideDir.Trim().Trim('"'));
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Kikitori");
    }

    public static void EnsureDirs()
    {
        foreach (var d in new[] { DataDir, LogsDir, SubtitleDir })
        {
            try { Directory.CreateDirectory(d); } catch { /* 下次用到时再报错 */ }
        }
    }

    /// <summary>用资源管理器打开文件夹（不存在就先建）。</summary>
    public static void OpenFolder(string dir)
    {
        try
        {
            Directory.CreateDirectory(dir);
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = "explorer.exe",
                ArgumentList = { dir },
                UseShellExecute = false,
            });
        }
        catch (Exception e)
        {
            Log.Error("打开文件夹失败：" + dir + "：" + e.Message);
        }
    }
}
