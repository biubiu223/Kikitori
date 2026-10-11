# Test helpers for Kikitori (dot-source this file:  . app\tools\kt.ps1).
# ASCII-only on purpose (Windows PowerShell 5.1 reads BOM-less scripts with the ANSI code page).
#
#   Start-Kikitori            start the app against tools\mock_engine.py with an isolated data dir + test hook
#   Invoke-Hook $proc 'state' send a test-hook command, returns the parsed JSON reply
#   Save-Shot $proc 'name' main|overlay|menu    capture only that window's rectangle -> build\screenshots\name.png
#   Get-KtWindows $proc       list the visible top-level windows of the process
#
# All paths are relative to this script (repo layout: <repo>\app\tools\kt.ps1).

$script:KtRepo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:KtShots = Join-Path $script:KtRepo "build\screenshots"
$script:KtData = Join-Path $script:KtRepo "build\testdata"

if (-not ("KtWin" -as [type])) {
Add-Type -ReferencedAssemblies System.Drawing @"
using System;
using System.Text;
using System.Drawing;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class KtWin {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern IntPtr SetProcessDpiAwarenessContext(IntPtr v);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int a, out RECT r, int size);
  public static void DpiAware() { SetProcessDpiAwarenessContext(new IntPtr(-4)); }
  public static RECT Bounds(IntPtr h) {
    RECT r;
    if (DwmGetWindowAttribute(h, 9, out r, Marshal.SizeOf(typeof(RECT))) != 0) GetWindowRect(h, out r);
    return r;
  }
  public static List<IntPtr> Find(uint pid) {
    var res = new List<IntPtr>();
    EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p); if (p == pid && IsWindowVisible(h)) res.Add(h); return true; }, IntPtr.Zero);
    return res;
  }
  public static string Title(IntPtr h) { var t = new StringBuilder(256); GetWindowText(h, t, 256); return t.ToString(); }
  public static string Cls(IntPtr h) { var t = new StringBuilder(256); GetClassName(h, t, 256); return t.ToString(); }
  public static void Capture(int l, int t, int r, int b, string path) {
    int w = r - l, hh = b - t;
    using (var bmp = new Bitmap(w, hh))
    using (var g = Graphics.FromImage(bmp)) {
      g.CopyFromScreen(l, t, 0, 0, new Size(w, hh));
      bmp.Save(path, System.Drawing.Imaging.ImageFormat.Png);
    }
  }
}
"@
}
[KtWin]::DpiAware()

function Get-KtExe([string]$Config = "Debug") {
    $p = Join-Path $script:KtRepo "app\Kikitori\bin\x64\$Config\net8.0-windows10.0.19041.0\win-x64\Kikitori.exe"
    if (-not (Test-Path $p)) { $p = Join-Path $script:KtRepo "app\Kikitori\bin\$Config\net8.0-windows10.0.19041.0\win-x64\Kikitori.exe" }
    return $p
}

function Start-Kikitori([string]$Exe = (Get-KtExe), [switch]$NoHook, [string]$DataDir = $script:KtData, [string]$Engine = "") {
    $py = Join-Path $script:KtRepo ".venv\Scripts\python.exe"
    $mock = Join-Path $script:KtRepo "tools\mock_engine.py"
    if (-not $Engine) { $Engine = "`"$py`" `"$mock`"" }
    $env:KIKITORI_ENGINE = $Engine
    if ($DataDir) { $env:KIKITORI_DATA_DIR = $DataDir } else { Remove-Item Env:KIKITORI_DATA_DIR -ErrorAction SilentlyContinue }
    if ($NoHook) { Remove-Item Env:KIKITORI_TEST_HOOK -ErrorAction SilentlyContinue } else { $env:KIKITORI_TEST_HOOK = "1" }
    $p = Start-Process -FilePath $Exe -PassThru
    return $p
}

function Invoke-Hook([int]$ProcId, [string]$Command, [int]$TimeoutMs = 5000) {
    $pipe = New-Object IO.Pipes.NamedPipeClientStream(".", "Kikitori.TestHook.$ProcId", [IO.Pipes.PipeDirection]::InOut)
    try {
        $pipe.Connect($TimeoutMs)
        $enc = New-Object Text.UTF8Encoding($false)
        $w = New-Object IO.StreamWriter($pipe, $enc); $w.AutoFlush = $true
        $r = New-Object IO.StreamReader($pipe, $enc)
        $w.WriteLine($Command)
        $line = $r.ReadLine()
        return ($line | ConvertFrom-Json)
    } finally { $pipe.Dispose() }
}

function Wait-Hook([int]$ProcId, [int]$Seconds = 20) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try { $s = Invoke-Hook $ProcId "state" 1000; if ($s.ok) { return $s } } catch { }
        Start-Sleep -Milliseconds 300
    }
    throw "test hook did not answer within $Seconds s"
}

function Get-KtWindows([int]$ProcId) {
    foreach ($h in [KtWin]::Find([uint32]$ProcId)) {
        $r = [KtWin]::Bounds($h)
        [pscustomobject]@{ Hwnd = $h.ToInt64(); Class = [KtWin]::Cls($h); Title = [KtWin]::Title($h); L = $r.L; T = $r.T; R = $r.R; B = $r.B }
    }
}

function Save-Shot([int]$ProcId, [string]$Name, [ValidateSet("main", "overlay", "menu")] [string]$Which = "main", [int]$Pad = 0) {
    New-Item -ItemType Directory -Force $script:KtShots | Out-Null
    $s = Invoke-Hook $ProcId "state"
    $wins = @(Get-KtWindows $ProcId)
    if ($Which -eq "main") {
        $h = [IntPtr]$s.main_hwnd
        [KtWin]::SetForegroundWindow($h) | Out-Null
        Start-Sleep -Milliseconds 400
        $r = [KtWin]::Bounds($h); $l = $r.L; $t = $r.T; $rr = $r.R; $b = $r.B
    } elseif ($Which -eq "overlay") {
        $r = [KtWin]::Bounds([IntPtr]$s.overlay_hwnd); $l = $r.L; $t = $r.T; $rr = $r.R; $b = $r.B
    } else {
        # overlay + every other visible window of the process except the main window (menu popups)
        $sel = $wins | Where-Object { $_.Hwnd -ne $s.main_hwnd }
        $l = ($sel | Measure-Object L -Minimum).Minimum; $t = ($sel | Measure-Object T -Minimum).Minimum
        $rr = ($sel | Measure-Object R -Maximum).Maximum; $b = ($sel | Measure-Object B -Maximum).Maximum
    }
    $out = Join-Path $script:KtShots "$Name.png"
    [KtWin]::Capture($l - $Pad, $t - $Pad, $rr + $Pad, $b + $Pad, $out)
    return $out
}

# ---------------- UI Automation (drives the real controls; no mouse/keyboard simulation) ----------------
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$script:UIA = [System.Windows.Automation.AutomationElement]
$script:TS = [System.Windows.Automation.TreeScope]

function Get-UiaRoot([int64]$Hwnd) { return [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd) }

function Find-Uia($Root, [string]$AutomationId = "", [string]$Name = "", [int]$TimeoutMs = 4000) {
    $prop = if ($AutomationId) { [System.Windows.Automation.AutomationElement]::AutomationIdProperty } else { [System.Windows.Automation.AutomationElement]::NameProperty }
    $val = if ($AutomationId) { $AutomationId } else { $Name }
    $cond = New-Object System.Windows.Automation.PropertyCondition($prop, $val)
    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    do {
        $el = $Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
        if ($el) { return $el }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    throw "UIA element not found: $AutomationId$Name"
}

# Find an element in any top-level window of the process (menu popups live in their own windows).
# -Id looks up AutomationId, otherwise Name.
function Find-UiaInProcess([int]$ProcId, [string]$Name, [int]$TimeoutMs = 4000, [switch]$Id) {
    $prop = if ($Id) { [System.Windows.Automation.AutomationElement]::AutomationIdProperty } else { [System.Windows.Automation.AutomationElement]::NameProperty }
    $cond = New-Object System.Windows.Automation.PropertyCondition($prop, $Name)
    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    do {
        foreach ($w in Get-KtWindows $ProcId) {
            $root = Get-UiaRoot $w.Hwnd
            $el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
            if ($el) { return $el }
        }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    throw "UIA element not found in process: $Name"
}

function Invoke-Uia($El) {
    $p = $null
    if ($El.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); return }
    if ($El.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$p)) { $p.Toggle(); return }
    if ($El.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) { $p.Select(); return }
    if ($El.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) { $p.Expand(); return }
    throw "element has no invoke/toggle/select/expand pattern"
}

function Expand-Uia($El) {
    $p = $null
    if ($El.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) { $p.Expand(); return }
    throw "element cannot expand"
}

function Set-UiaValue($El, [string]$Value) {
    $p = $null
    if ($El.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$p)) { $p.SetValue($Value); return }
    throw "element has no value pattern"
}

function Get-UiaToggle($El) {
    $p = $El.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
    return $p.Current.ToggleState.ToString()
}

# Chinese UI strings without putting non-ASCII characters into this file: U '6682 505C' -> the string of those code points
function U([string]$Hex) { -join ($Hex.Split(' ') | ForEach-Object { [char][Convert]::ToInt32($_, 16) }) }

# Open the overlay context menu (via test hook) and click an item; nested items: Invoke-OverlayMenu $id 'parent' 'child'
# Path items are AutomationIds of the menu items (menu_pause, menu_lines, menu_lines_6, ...).
function Invoke-OverlayMenu([int]$ProcId, [string[]]$Path) {
    Invoke-Hook $ProcId "overlay_menu" | Out-Null
    Start-Sleep -Milliseconds 700
    for ($i = 0; $i -lt $Path.Count; $i++) {
        $el = Find-UiaInProcess $ProcId $Path[$i] -Id
        if ($i -lt $Path.Count - 1) { Expand-Uia $el; Start-Sleep -Milliseconds 500 } else { Invoke-Uia $el }
    }
    Start-Sleep -Milliseconds 600
}

# ---------------- real mouse input (used briefly for drag / resize-grip / right-click tests) ----------------
if (-not ("KtMouse" -as [type])) {
Add-Type @"
using System;
using System.Threading;
using System.Runtime.InteropServices;
public static class KtMouse {
  [DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT p);
  [DllImport("user32.dll")] static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
  [DllImport("user32.dll")] static extern void keybd_event(byte vk, byte scan, uint flags, IntPtr extra);
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
  public static POINT Pos() { POINT p; GetCursorPos(out p); return p; }
  public static void Drag(int x0, int y0, int x1, int y1, int steps) {
    SetCursorPos(x0, y0); Thread.Sleep(80);
    mouse_event(0x0002, 0, 0, 0, IntPtr.Zero); Thread.Sleep(80);
    for (int i = 1; i <= steps; i++) { SetCursorPos(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps); Thread.Sleep(15); }
    Thread.Sleep(80); mouse_event(0x0004, 0, 0, 0, IntPtr.Zero); Thread.Sleep(80);
  }
  public static void RightClick(int x, int y) {
    SetCursorPos(x, y); Thread.Sleep(80);
    mouse_event(0x0008, 0, 0, 0, IntPtr.Zero); Thread.Sleep(40); mouse_event(0x0010, 0, 0, 0, IntPtr.Zero);
  }
  public static void Escape() { keybd_event(0x1B, 0, 0, IntPtr.Zero); keybd_event(0x1B, 0, 2, IntPtr.Zero); }
  public static void Restore(POINT p) { SetCursorPos(p.X, p.Y); }
}
"@
}

function Get-EngineProcesses {
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -match 'mock_engine\.py' }
}
