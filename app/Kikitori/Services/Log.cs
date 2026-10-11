namespace Kikitori.Services;

/// <summary>界面自己的日志（logs\app.log），只记启动、异常等少量信息。</summary>
public static class Log
{
    private static readonly object Sync = new();
    private const long MaxBytes = 2 * 1024 * 1024;

    public static void Info(string msg) => Write("INFO", msg);
    public static void Warn(string msg) => Write("WARN", msg);
    public static void Error(string msg) => Write("ERROR", msg);

    private static void Write(string level, string msg)
    {
        try
        {
            lock (Sync)
            {
                Directory.CreateDirectory(AppPaths.LogsDir);
                var path = AppPaths.AppLog;
                var fi = new FileInfo(path);
                if (fi.Exists && fi.Length > MaxBytes)
                    File.Move(path, path + ".1", overwrite: true);
                File.AppendAllText(path, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} [{level}] {msg}{Environment.NewLine}");
            }
        }
        catch
        {
            // 日志写不了也不能影响程序
        }
        System.Diagnostics.Debug.WriteLine($"[{level}] {msg}");
    }
}
