using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.UI.Dispatching;

namespace Kikitori.Services;

/// <summary>引擎非正常退出的信息。</summary>
public sealed record EngineExitInfo(int? ExitCode, IReadOnlyList<string> StderrTail);

/// <summary>要启动的引擎命令。</summary>
public sealed record EngineCommand(string FileName, string Arguments, string WorkingDirectory, string Display);

/// <summary>
/// 管理 Python 引擎子进程（docs/protocol.md 第 1、2 节）：
/// 不显示窗口；stdin 写 JSON Lines 命令（UTF-8 无 BOM）；stdout 逐行解析事件并切回 UI 线程分发；
/// stderr 追加写入 logs\engine.log 并保留最后 50 行；进程放进 Job Object；退出时先发 shutdown，3 秒不退就强杀。
/// </summary>
public sealed class EngineClient : IDisposable
{
    public const int StderrKeep = 50;
    private const long MaxLogBytes = 5 * 1024 * 1024;

    private readonly DispatcherQueue _dq;
    private readonly JobObject _job = new();
    private readonly object _writeLock = new();
    private readonly object _logLock = new();
    private readonly LinkedList<string> _tail = new();
    private Process? _proc;
    private StreamWriter? _stdin;
    private StreamWriter? _log;
    private volatile int _gen;

    /// <summary>引擎事件（已在 UI 线程上）。</summary>
    public event Action<JsonObject>? EventReceived;

    /// <summary>引擎意外退出（不是我们让它退的）。已在 UI 线程上。</summary>
    public event Action<EngineExitInfo>? ExitedUnexpectedly;

    public EngineClient(DispatcherQueue dq) => _dq = dq;

    public EngineCommand? Command { get; private set; }

    public int? ProcessId
    {
        get { try { return _proc is { HasExited: false } p ? p.Id : null; } catch { return null; } }
    }

    public bool IsRunning
    {
        get { try { return _proc is { HasExited: false }; } catch { return false; } }
    }

    // ------------------------------------------------------------------ 查找引擎
    /// <summary>查找顺序：环境变量 KIKITORI_ENGINE（完整命令行）→ &lt;exe 目录&gt;\engine\KikitoriEngine.exe。</summary>
    public static EngineCommand? Resolve(out string error)
    {
        error = "";
        var env = Environment.GetEnvironmentVariable("KIKITORI_ENGINE");
        if (!string.IsNullOrWhiteSpace(env))
        {
            var cmd = ParseCommandLine(env.Trim());
            if (cmd is null || !File.Exists(cmd.FileName))
            {
                error = $"环境变量 KIKITORI_ENGINE 指定的程序不存在：{env}";
                return null;
            }
            return cmd;
        }
        var exe = AppPaths.BundledEngine;
        if (File.Exists(exe))
            return new EngineCommand(exe, "", Path.GetDirectoryName(exe)!, exe);
        error = $"找不到识别引擎：{exe}\n请确认程序目录里有 engine 文件夹（或设置环境变量 KIKITORI_ENGINE）。";
        return null;
    }

    /// <summary>把“程序 参数…”形式的命令行拆成程序和参数（第一个参数可以带引号）。</summary>
    public static EngineCommand? ParseCommandLine(string cmdline)
    {
        string file, args;
        if (cmdline.StartsWith('"'))
        {
            int end = cmdline.IndexOf('"', 1);
            if (end < 0) return null;
            file = cmdline[1..end];
            args = cmdline[(end + 1)..].Trim();
        }
        else if (File.Exists(cmdline))
        {
            file = cmdline;
            args = "";
        }
        else
        {
            int sp = cmdline.IndexOfAny([' ', '\t']);
            file = sp < 0 ? cmdline : cmdline[..sp];
            args = sp < 0 ? "" : cmdline[(sp + 1)..].Trim();
        }
        // 工作目录：脚本（.py）所在目录，否则程序所在目录
        string workDir = Path.GetDirectoryName(Path.GetFullPath(file)) ?? AppPaths.ExeDir;
        foreach (var token in SplitArgs(args))
        {
            if (token.EndsWith(".py", StringComparison.OrdinalIgnoreCase) && File.Exists(token))
            {
                workDir = Path.GetDirectoryName(Path.GetFullPath(token)) ?? workDir;
                break;
            }
        }
        return new EngineCommand(file, args, workDir, cmdline);
    }

    private static IEnumerable<string> SplitArgs(string args)
    {
        var sb = new StringBuilder();
        bool quoted = false;
        foreach (var ch in args)
        {
            if (ch == '"') { quoted = !quoted; continue; }
            if (!quoted && char.IsWhiteSpace(ch))
            {
                if (sb.Length > 0) { yield return sb.ToString(); sb.Clear(); }
                continue;
            }
            sb.Append(ch);
        }
        if (sb.Length > 0) yield return sb.ToString();
    }

    // ------------------------------------------------------------------ 启动 / 停止
    public bool Start(out string error)
    {
        KillCurrent();
        var cmd = Resolve(out error);
        if (cmd is null)
        {
            LogLine($"===== {DateTime.Now:yyyy-MM-dd HH:mm:ss} 无法启动引擎：{error} =====");
            return false;
        }
        Command = cmd;

        var utf8 = new UTF8Encoding(false);
        var psi = new ProcessStartInfo(cmd.FileName, cmd.Arguments)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = utf8,      // 协议要求：UTF-8，无 BOM
            StandardOutputEncoding = utf8,
            StandardErrorEncoding = utf8,
            WorkingDirectory = cmd.WorkingDirectory,
        };
        psi.Environment["PYTHONIOENCODING"] = "utf-8";
        psi.Environment["PYTHONUTF8"] = "1";
        psi.Environment["PYTHONUNBUFFERED"] = "1";

        var p = new Process { StartInfo = psi };
        try
        {
            RotateLogIfNeeded();
            p.Start();
        }
        catch (Exception e)
        {
            error = $"无法启动识别引擎：{e.Message}\n命令：{cmd.Display}";
            LogLine($"===== {DateTime.Now:yyyy-MM-dd HH:mm:ss} {error} =====");
            p.Dispose();
            return false;
        }
        _job.Assign(p.Handle);

        int gen = Interlocked.Increment(ref _gen);
        lock (_tail) _tail.Clear();
        _proc = p;
        _stdin = p.StandardInput;
        _stdin.AutoFlush = false;
        LogLine($"===== {DateTime.Now:yyyy-MM-dd HH:mm:ss} 启动引擎（PID {p.Id}）=====");

        var outTask = Task.Run(() => ReadStdoutAsync(p, gen));
        var errTask = Task.Run(() => ReadStderrAsync(p, gen));
        _ = Task.Run(async () =>
        {
            try { await outTask; } catch { }
            try { await errTask.WaitAsync(TimeSpan.FromSeconds(2)); } catch { }
            try { await p.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5)); } catch { }
            int? code = null;
            try { if (p.HasExited) code = p.ExitCode; } catch { }
            LogLine($"===== {DateTime.Now:yyyy-MM-dd HH:mm:ss} 引擎进程已退出（PID {SafeId(p)}，退出码 {code?.ToString() ?? "未知"}）=====");
            if (gen != _gen) return;   // 我们主动停掉/重启的，不算意外
            var tail = GetStderrTail(StderrKeep);
            _dq.TryEnqueue(() =>
            {
                if (gen != _gen) return;
                ExitedUnexpectedly?.Invoke(new EngineExitInfo(code, tail));
            });
        });
        // venv 的 python.exe 会再起一个子进程；稍后把子孙进程也收进 Job（正常情况下它们已经继承了 Job）
        _ = Task.Run(async () =>
        {
            foreach (var delay in new[] { 300, 1500, 5000 })
            {
                await Task.Delay(delay);
                if (gen != _gen) return;
                try { _job.AssignDescendants(p.Id); } catch { }
            }
        });
        return true;
    }

    private static int SafeId(Process p) { try { return p.Id; } catch { return -1; } }

    /// <summary>发送一条命令（JSON 对象，一行）。引擎没在运行时返回 false。</summary>
    public bool Send(JsonObject cmd)
    {
        var line = cmd.ToJsonString(SettingsService.WireOptions);
        lock (_writeLock)
        {
            var w = _stdin;
            if (w is null || !IsRunning) return false;
            try
            {
                w.Write(line);
                w.Write('\n');
                w.Flush();
                LogLine($"[界面] → {cmd["cmd"]}");   // 只记命令名（config 里有 API Key，不能写进日志）
                return true;
            }
            catch (Exception e)
            {
                LogLine($"[界面] 发送命令失败（{cmd["cmd"]}）：{e.Message}");
                return false;
            }
        }
    }

    /// <summary>正常退出：发 shutdown 并关闭 stdin，3 秒内没退出就强制结束。</summary>
    public async Task StopAsync(int timeoutMs = 3000)
    {
        var p = _proc;
        if (p is null) return;
        Interlocked.Increment(ref _gen);   // 之后的事件和退出都不再通知
        bool exited;
        try { exited = p.HasExited; } catch { exited = true; }
        if (!exited)
        {
            lock (_writeLock)
            {
                try
                {
                    _stdin?.Write("{\"cmd\":\"shutdown\"}\n");
                    _stdin?.Flush();
                }
                catch { }
                try { _stdin?.Close(); } catch { }   // 同时给 EOF，引擎按协议也会自己退出
                _stdin = null;
            }
            try
            {
                await p.WaitForExitAsync().WaitAsync(TimeSpan.FromMilliseconds(timeoutMs));
                LogLine($"[界面] 引擎已正常退出");
            }
            catch (TimeoutException)
            {
                LogLine($"[界面] 引擎 {timeoutMs / 1000.0:0.#} 秒内没有退出，强制结束");
                try { p.Kill(entireProcessTree: true); } catch { }
            }
            catch { }
        }
        lock (_writeLock) { _stdin = null; }
        if (ReferenceEquals(_proc, p)) _proc = null;
    }

    /// <summary>立即结束当前引擎（重启用），不触发“意外退出”。</summary>
    public void KillCurrent()
    {
        var p = _proc;
        if (p is null) return;
        Interlocked.Increment(ref _gen);
        lock (_writeLock)
        {
            try { _stdin?.Close(); } catch { }
            _stdin = null;
        }
        try { if (!p.HasExited) p.Kill(entireProcessTree: true); } catch { }
        _proc = null;
    }

    // ------------------------------------------------------------------ 读取输出
    private async Task ReadStdoutAsync(Process p, int gen)
    {
        var reader = p.StandardOutput;
        while (true)
        {
            string? line;
            try { line = await reader.ReadLineAsync(); }
            catch { break; }
            if (line is null) break;
            line = line.TrimStart('﻿').Trim();
            if (line.Length == 0) continue;
            JsonObject? obj = null;
            try { obj = JsonNode.Parse(line) as JsonObject; } catch { }
            if (obj is null || obj["event"] is null)
            {
                LogLine("[界面] 引擎 stdout 输出了非协议内容：" + (line.Length > 300 ? line[..300] + "…" : line));
                continue;
            }
            if (gen != _gen) continue;
            _dq.TryEnqueue(() =>
            {
                if (gen != _gen) return;
                try { EventReceived?.Invoke(obj); }
                catch (Exception e) { Log.Error("处理引擎事件出错：" + e); }
            });
        }
    }

    private async Task ReadStderrAsync(Process p, int gen)
    {
        var reader = p.StandardError;
        while (true)
        {
            string? line;
            try { line = await reader.ReadLineAsync(); }
            catch { break; }
            if (line is null) break;
            LogLine(line);
            lock (_tail)
            {
                _tail.AddLast(line);
                while (_tail.Count > StderrKeep) _tail.RemoveFirst();
            }
        }
    }

    public IReadOnlyList<string> GetStderrTail(int n)
    {
        lock (_tail) return _tail.Skip(Math.Max(0, _tail.Count - n)).ToList();
    }

    // ------------------------------------------------------------------ engine.log
    private void LogLine(string line)
    {
        lock (_logLock)
        {
            try
            {
                if (_log is null)
                {
                    Directory.CreateDirectory(AppPaths.LogsDir);
                    var fs = new FileStream(AppPaths.EngineLog, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
                    _log = new StreamWriter(fs, new UTF8Encoding(false)) { AutoFlush = true };
                }
                _log.WriteLine(line);
            }
            catch
            {
                // 日志写不了不影响运行
            }
        }
    }

    private void RotateLogIfNeeded()
    {
        lock (_logLock)
        {
            try
            {
                var fi = new FileInfo(AppPaths.EngineLog);
                if (!fi.Exists || fi.Length < MaxLogBytes) return;
                _log?.Dispose();
                _log = null;
                File.Move(fi.FullName, fi.FullName + ".1", overwrite: true);
            }
            catch { }
        }
    }

    public void Dispose()
    {
        KillCurrent();
        lock (_logLock) { _log?.Dispose(); _log = null; }
        _job.Dispose();
    }
}
