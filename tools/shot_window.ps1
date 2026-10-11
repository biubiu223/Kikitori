param([int]$ProcId, [string]$Out)
# Capture only the overlay window's rectangle (not the whole screen).
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class W {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  public static List<IntPtr> Find(uint pid) {
    var res = new List<IntPtr>();
    EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p); if (p == pid && IsWindowVisible(h)) res.Add(h); return true; }, IntPtr.Zero);
    return res;
  }
}
"@
[W]::SetProcessDPIAware() | Out-Null
$best = $null; $bestArea = 0
foreach ($h in [W]::Find([uint32]$ProcId)) {
  $t = New-Object Text.StringBuilder 256; [W]::GetWindowText($h, $t, 256) | Out-Null
  $c = New-Object Text.StringBuilder 256; [W]::GetClassName($h, $c, 256) | Out-Null
  $r = New-Object W+RECT; [W]::GetWindowRect($h, [ref]$r) | Out-Null
  $area = ($r.R - $r.L) * ($r.B - $r.T)
  "win: class=$c title=$t rect=$($r.L),$($r.T),$($r.R),$($r.B)"
  if ($area -gt $bestArea) { $best = $r; $bestArea = $area }
}
if (-not $best) { "no visible window"; exit 1 }
$w = $best.R - $best.L; $hh = $best.B - $best.T
$bmp = New-Object Drawing.Bitmap $w, $hh
$g = [Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($best.L, $best.T, 0, 0, $bmp.Size)
$bmp.Save($Out, [Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"saved $Out"
