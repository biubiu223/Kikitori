# Generates the application icon (app.ico + logo.png) with System.Drawing.
# Motif: sound waves reaching an "ear" + two subtitle lines ("Kikitori" = listening).
# Output: ..\Kikitori\Assets\app.ico, ..\Kikitori\Assets\logo.png
# Usage:  powershell -ExecutionPolicy Bypass -File app\tools\make_icon.ps1
# (ASCII-only script so Windows PowerShell 5.1 parses it regardless of code page.)
Add-Type -AssemblyName System.Drawing

$assets = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\Kikitori\Assets"))
New-Item -ItemType Directory -Force $assets | Out-Null

function New-RoundRect([single]$x, [single]$y, [single]$w, [single]$h, [single]$r) {
    $p = New-Object Drawing.Drawing2D.GraphicsPath
    $r = [Math]::Min($r, [Math]::Min($w, $h) / 2)
    $d = 2 * $r
    $p.AddArc($x, $y, $d, $d, 180, 90)
    $p.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $p.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $p.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $p.CloseFigure()
    return $p
}

function C([int]$a, [int]$r, [int]$g, [int]$b) { [Drawing.Color]::FromArgb($a, $r, $g, $b) }

function New-IconBitmap([int]$s) {
    $bmp = New-Object Drawing.Bitmap $s, $s, ([Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.PixelOffsetMode = [Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([Drawing.Color]::Transparent)

    # background: rounded square, blue -> violet
    $pad = [Math]::Max(0.5, $s * 0.04)
    $size = $s - 2 * $pad
    $bg = New-RoundRect $pad $pad $size $size ($size * 0.24)
    $grad = New-Object Drawing.Drawing2D.LinearGradientBrush (New-Object Drawing.PointF 0, 0), (New-Object Drawing.PointF $s, $s), (C 255 52 120 246), (C 255 139 92 246)
    $g.FillPath($grad, $bg)

    $small = $s -le 24
    $white = C 255 255 255 255

    # --- sound waves: concentric arcs opening to the upper-right, like waves entering an ear ---
    $cx = $s * 0.30; $cy = $s * 0.47
    $radii = if ($small) { @(0.20, 0.36) } else { @(0.14, 0.255, 0.37) }
    $alphas = if ($small) { @(255, 200) } else { @(255, 215, 160) }
    $penW = if ($small) { [Math]::Max(1.6, $s * 0.10) } else { $s * 0.062 }
    # source dot
    $dr = if ($small) { $s * 0.085 } else { $s * 0.06 }
    $g.FillEllipse((New-Object Drawing.SolidBrush $white), $cx - $dr, $cy - $dr, 2 * $dr, 2 * $dr)
    for ($i = 0; $i -lt $radii.Count; $i++) {
        $r = $s * $radii[$i]
        $pen = New-Object Drawing.Pen (C $alphas[$i] 255 255 255), $penW
        $pen.StartCap = [Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap = [Drawing.Drawing2D.LineCap]::Round
        $g.DrawArc($pen, $cx - $r, $cy - $r, 2 * $r, 2 * $r, -62, 98)
        $pen.Dispose()
    }

    # --- subtitle lines at the bottom ---
    if ($small) {
        $bar = New-RoundRect ($s * 0.18) ($s * 0.74) ($s * 0.64) ($s * 0.12) ($s * 0.06)
        $g.FillPath((New-Object Drawing.SolidBrush $white), $bar)
    } else {
        $bar1 = New-RoundRect ($s * 0.17) ($s * 0.665) ($s * 0.66) ($s * 0.085) ($s * 0.0425)
        $g.FillPath((New-Object Drawing.SolidBrush $white), $bar1)
        $bar2 = New-RoundRect ($s * 0.27) ($s * 0.785) ($s * 0.46) ($s * 0.065) ($s * 0.0325)
        $g.FillPath((New-Object Drawing.SolidBrush (C 170 255 255 255)), $bar2)
    }
    $g.Dispose()
    return $bmp
}

$sizes = 16, 20, 24, 32, 40, 48, 64, 256
$pngs = @()
foreach ($s in $sizes) {
    $bmp = New-IconBitmap $s
    $ms = New-Object IO.MemoryStream
    $bmp.Save($ms, [Drawing.Imaging.ImageFormat]::Png)
    $pngs += , ($ms.ToArray())
    if ($s -eq 256) { $bmp.Save((Join-Path $assets "logo.png"), [Drawing.Imaging.ImageFormat]::Png) }
    if ($env:ICON_PREVIEW_DIR) { $bmp.Save((Join-Path $env:ICON_PREVIEW_DIR "icon_$s.png"), [Drawing.Imaging.ImageFormat]::Png) }
    $bmp.Dispose()
}

# ICO container with PNG-compressed entries
$out = New-Object IO.MemoryStream
$w = New-Object IO.BinaryWriter $out
$w.Write([UInt16]0); $w.Write([UInt16]1); $w.Write([UInt16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $s = $sizes[$i]; $len = $pngs[$i].Length
    $dim = if ($s -ge 256) { 0 } else { $s }
    $w.Write([byte]$dim); $w.Write([byte]$dim); $w.Write([byte]0); $w.Write([byte]0)
    $w.Write([UInt16]1); $w.Write([UInt16]32); $w.Write([UInt32]$len); $w.Write([UInt32]$offset)
    $offset += $len
}
foreach ($p in $pngs) { $w.Write($p) }
$w.Flush()
[IO.File]::WriteAllBytes((Join-Path $assets "app.ico"), $out.ToArray())
"wrote $(Join-Path $assets 'app.ico') ($($out.Length) bytes)"
