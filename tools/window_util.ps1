# Helpers: list visible top-level windows of a process, or post WM_CLOSE to them.
param([int]$ProcId, [switch]$Close)
if (-not ("W4" -as [type])) {
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class W4 {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  public static string Run(uint pid, bool close) {
    var sb = new StringBuilder();
    EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p);
      if (p == pid && IsWindowVisible(h)) {
        var t = new StringBuilder(256); GetWindowText(h, t, 256);
        var c = new StringBuilder(256); GetClassName(h, c, 256);
        sb.AppendLine("  window: class=" + c + " title=" + t);
        if (close) PostMessage(h, 0x0010, IntPtr.Zero, IntPtr.Zero);
      } return true; }, IntPtr.Zero);
    return sb.ToString();
  }
}
"@
}
[W4]::Run([uint32]$ProcId, [bool]$Close)
