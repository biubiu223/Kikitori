# End-to-end UI test for Kikitori against tools\mock_engine.py.
#   powershell -ExecutionPolicy Bypass -File app\tools\ui_test.ps1 [-Exe <path to Kikitori.exe>] [-NoMouse]
# Uses an isolated data dir (build\testdata), the env-gated test hook, UI Automation for real controls,
# and (unless -NoMouse) about two seconds of real mouse input for drag / resize-grip / right-click.
# Screenshots + report go to build\screenshots. ASCII-only on purpose (PowerShell 5.1).
param([string]$Exe = "", [switch]$NoMouse)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "kt.ps1")
if (-not $Exe) { $Exe = Get-KtExe }

$results = New-Object System.Collections.ArrayList
function Check([string]$Name, [bool]$Ok, [string]$Detail = "") {
    $line = "{0}  {1}{2}" -f ($(if ($Ok) { "PASS" } else { "FAIL" })), $Name, $(if ($Detail) { "  ($Detail)" } else { "" })
    [void]$results.Add($line); Write-Host $line
}
function St { Invoke-Hook $script:id "state" }
function Wait-Until([scriptblock]$Cond, [int]$Seconds = 15) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) { try { if (& $Cond) { return $true } } catch { }; Start-Sleep -Milliseconds 200 }
    return $false
}
function Shot([string]$Name, [string]$Which = "main", [int]$Pad = 0) { [void](Save-Shot $script:id $Name $Which -Pad $Pad) }
function MainRoot { Get-UiaRoot (St).main_hwnd }
function PageScroller {
    $all = (MainRoot).FindAll([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, "ScrollViewer")))
    foreach ($e in $all) { if ($e.Current.AutomationId -eq "") { return $e } }
}
function Nav([string]$Page) { Invoke-Hook $script:id "nav $Page" | Out-Null; Start-Sleep -Milliseconds 900 }

$FAILED_TEXT = U "FF08 7FFB 8BD1 5931 8D25 FF09"   # full-width brackets + "translation failed"
$settingsFile = Join-Path $script:KtData "settings.json"

if (Get-Process Kikitori -ErrorAction SilentlyContinue) { throw "Kikitori is already running; close it first." }
Remove-Item -Recurse -Force $script:KtData -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $script:KtShots "*.png") -ErrorAction SilentlyContinue

# ------------------------------------------------------------------ 1. first start (defaults, no API key)
$p = Start-Kikitori -Exe $Exe
$script:id = $p.Id
$s = Wait-Hook $id 30
Invoke-Hook $id "main_rect 560 20 1100 1040" | Out-Null
Check "engine started and hello received" (Wait-Until { (St).engine_ready } 15)
Check "auto-start: listening after launch" (Wait-Until { (St).engine_state -eq "listening" } 15)
Check "devices received from engine" ((St).devices -eq 3) "devices=$((St).devices)"
Check "overlay visible" ((St).overlay_visible -eq $true)
Check "no API key -> no_translation (source only)" (Wait-Until { $l = @((Invoke-Hook $id overlay_lines).lines); $l.Count -gt 0 -and -not $l[-1].main } 15)
Start-Sleep 2; Shot "10_overlay_no_key" overlay 8
$raw = Get-Content $settingsFile -Raw -Encoding UTF8
Check "settings.json created with defaults" ($raw -match '"provider": "deepseek"' -and $raw -match '"model": "deepseek-flash"' -and $raw -match '"temperature": 1.3' -and $raw -match '"auto_start": true')

# first-use hint on the home page (DeepSeek selected, no key)
Nav "home"
$bar = Find-Uia (MainRoot) -AutomationId "SetupBar"
Check "home: setup hint shown while API key is empty" ((St).setup_problem -and $bar.Current.BoundingRectangle.Height -gt 20)
Shot "01a_home_setup_hint"
$toggle = Find-Uia (MainRoot) -AutomationId "TogglePaneButton"
Check "WinUI built-in strings are Chinese (nav toggle button name)" ($toggle.Current.Name -match '[^\x00-\x7F]') ("name has " + ($toggle.Current.Name.Length) + " chars")

# ------------------------------------------------------------------ 2. translation page: key + test connection
Invoke-Uia (Find-Uia (MainRoot) -AutomationId "GoSetupButton"); Start-Sleep 1
Check "setup hint button opens the translation page" ((St).main_page -eq "translate")
$noticeBefore = (St).notice_open
Set-UiaValue (Find-Uia (MainRoot) -AutomationId "DeepSeekKeyBox") "sk-test-xxx"
Check "stale 'no API key' notice cleared once the engine is listening again" (Wait-Until { -not (St).notice_open } 5) "notice open before key: $noticeBefore"
Start-Sleep 1.5
Invoke-Uia (Find-Uia (MainRoot) -AutomationId "TestButton")
Check "test_translation answered" (Wait-Until { (Get-Content (Join-Path $script:KtData "logs\engine.log") -Raw -Encoding UTF8) -match "cmd: test_translation" } 5)
Start-Sleep 1.5
Shot "02_translate"
$raw = Get-Content $settingsFile -Raw -Encoding UTF8
Check "API key stored as DPAPI ciphertext" ($raw -match '"api_key_protected": "dpapi:[A-Za-z0-9+/=]{40,}' -and $raw -notmatch "sk-test-xxx")
Check "key change restarted listening (start re-sent)" (Wait-Until { @(Select-String -Path (Join-Path $script:KtData "logs\engine.log") -Pattern "$([char]0x2192) start" -Encoding UTF8).Count -ge 2 } 3)
Check "translations appear after key set" (Wait-Until { @((Invoke-Hook $id overlay_lines).lines | Where-Object { $_.main -and $_.main_color -eq "#FFFFFF" }).Count -gt 0 } 20)

Nav "home"
Check "home: setup hint gone after key filled" (-not (St).setup_problem -and (Wait-Until { try { (Find-Uia (MainRoot) -AutomationId "SetupBar" -TimeoutMs 300).Current.BoundingRectangle.Height -lt 1 } catch { $true } } 3))

# ------------------------------------------------------------------ 3. other pages
Nav "asr"; Shot "03_asr"
Nav "style"; Shot "04_style"
Nav "glossary"
Invoke-Uia (Find-Uia (MainRoot) -AutomationId "AddButton"); Start-Sleep 0.5
Invoke-Uia (Find-Uia (MainRoot) -AutomationId "AddButton"); Start-Sleep 0.5
$list = Find-Uia (MainRoot) -AutomationId "GlossaryList"
$edits = $list.FindAll([System.Windows.Automation.TreeScope]::Descendants,
    (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)))
Set-UiaValue $edits[0] (U "514E 7530 307A 3053 3089"); Set-UiaValue $edits[1] (U "5154 7530 4F69 514B 62C9")
Set-UiaValue $edits[2] (U "91CE 3046 3055 304E"); Set-UiaValue $edits[3] (U "91CE 5154 FF08 7C89 4E1D FF09")
Start-Sleep 1.5
Shot "04b_glossary"
$g = (Get-Content $settingsFile -Raw -Encoding UTF8 | ConvertFrom-Json).engine.glossary
Check "glossary saved (2 rows)" (@($g).Count -eq 2 -and $g[0].target -eq (U "5154 7530 4F69 514B 62C9"))
Nav "about"; Shot "04c_about"

# ------------------------------------------------------------------ 4. overlay context menu (opened by the hook, items driven by UIA)
Invoke-Hook $id "overlay_menu" | Out-Null; Start-Sleep 1
Check "overlay menu opens and stays open while subtitles stream" ((Wait-Until { (St).overlay_menu_open } 3) -and ($(Start-Sleep 2; (St).overlay_menu_open)))
Shot "05_overlay_menu" menu 8
Expand-Uia (Find-UiaInProcess $id "menu_lines" -Id); Start-Sleep 0.7
Shot "06_overlay_submenu" menu 8
Invoke-Uia (Find-UiaInProcess $id "menu_lines_6" -Id); Start-Sleep 0.8
Check "menu: lines -> 6" ((St).overlay_settings.MaxLines -eq 6)

$all = Wait-Until {
    $l = @((Invoke-Hook $id overlay_lines).lines)
    ($l | Where-Object { $_.main -eq $FAILED_TEXT }) -and ($l | Where-Object { -not $_.source -and $_.main -and $_.main_color -eq "#FFFFFF" }) -and ($l[-1].source_color -eq "#7FB0FF")
} 90
Shot "07_overlay_states" overlay 8
Check "overlay shows draft(blue) + translation + failure + same-language together" $all
$l = @((Invoke-Hook $id overlay_lines).lines)
Check "draft line ends with ' ...' marker" ($l[-1].source -match (" " + [char]0x2026 + "$"))
Check "source font is Japanese (Yu Gothic UI), main font YaHei" ($l[0].source_font -like "Yu Gothic UI*" -and $l[0].main_font -like "Microsoft YaHei UI*")

Invoke-OverlayMenu $id @("menu_pause")
Check "menu: pause -> engine paused" (Wait-Until { $x = St; $x.paused -and $x.engine_state -eq "paused" } 3)
Start-Sleep 0.5; Shot "08_overlay_paused" overlay 8
Invoke-OverlayMenu $id @("menu_pause")
Check "menu: resume -> listening" (Wait-Until { $x = St; -not $x.paused -and $x.engine_state -eq "listening" } 3)
Invoke-OverlayMenu $id @("menu_target", "menu_target_en")
Check "menu: target -> en (set_languages)" ((St).target -eq "en")
Invoke-OverlayMenu $id @("menu_source", "menu_source_ko")
Check "menu: source -> ko, Korean font" ((St).source -eq "ko" -and (@((Invoke-Hook $id overlay_lines).lines)[0].source_font -like "Malgun Gothic*"))
Invoke-OverlayMenu $id @("menu_center")
Invoke-OverlayMenu $id @("menu_opacity", "menu_opacity_50")
Invoke-OverlayMenu $id @("menu_show_source")
$o = (St).overlay_settings
Check "menu: centered / opacity 50% / hide source" ($o.Centered -and $o.Opacity -eq 0.5 -and -not $o.ShowSource)
Start-Sleep 4; Shot "09_overlay_variant" overlay 8
Invoke-OverlayMenu $id @("menu_font", "menu_font_up")
Check "menu: font bigger (22->24 / 14->15)" ((St).overlay_settings.FontSize -eq 24 -and (St).overlay_settings.SourceFontSize -eq 15)
Invoke-OverlayMenu $id @("menu_show_source"); Invoke-OverlayMenu $id @("menu_center")
Invoke-OverlayMenu $id @("menu_target", "menu_target_zh-Hans"); Invoke-OverlayMenu $id @("menu_source", "menu_source_ja")
Invoke-OverlayMenu $id @("menu_opacity", "menu_opacity_70"); Invoke-OverlayMenu $id @("menu_lines", "menu_lines_4")
Invoke-OverlayMenu $id @("menu_reconnect")
Check "menu: reconnect audio -> engine notice" ((Wait-Until { (Get-Content (Join-Path $script:KtData "logs\engine.log") -Raw -Encoding UTF8) -match "cmd: reconnect_audio" } 3))
Invoke-OverlayMenu $id @("menu_clear")
Check "menu: clear subtitles" ((St).subtitles -le 1)
$ls = Get-Content (Join-Path $script:KtData "logs\engine.log") -Raw -Encoding UTF8
Check "protocol commands seen by engine" ($ls -match "cmd: pause" -and $ls -match "cmd: set_languages" -and $ls -match "cmd: list_devices")

# ------------------------------------------------------------------ 5. style page drives the overlay
Nav "style"
$before = (St).overlay
$slider = Find-Uia (MainRoot) -AutomationId "WidthSlider"
$slider.GetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern).SetValue(1200); Start-Sleep 0.8
$after = (St).overlay
Check "style page: width slider resizes overlay, bottom fixed" ($after.w -eq 1200 -and ($after.y + $after.h) -eq ($before.y + $before.h)) "w=$($after.w) bottom $($before.y + $before.h)->$($after.y + $after.h)"
Invoke-Uia (Find-Uia (MainRoot) -AutomationId "ResetButton"); Start-Sleep 0.8
Check "style page: reset position -> default width" ((St).overlay.w -eq 960) "w=$((St).overlay.w)"

# ------------------------------------------------------------------ 6. real mouse: drag, resize grip, right click
if (-not $NoMouse) {
    $cursor = [KtMouse]::Pos()
    try {
        $o0 = (St).overlay
        [KtMouse]::Drag($o0.x + 40, $o0.y + 8, $o0.x + 40, $o0.y + 8 - 120, 12)
        Start-Sleep 0.5
        $o1 = (St).overlay
        Check "mouse: drag moves overlay up 120px" ([Math]::Abs(($o1.y + $o1.h) - ($o0.y + $o0.h - 120)) -le 2) "bottom $($o0.y + $o0.h)->$($o1.y + $o1.h)"
        [KtMouse]::Drag($o1.x + $o1.w - 7, $o1.y + $o1.h - 7, $o1.x + $o1.w - 7 + 100, $o1.y + $o1.h - 7, 12)
        Start-Sleep 0.6
        $o2 = (St).overlay
        Check "mouse: resize grip widens overlay by 100px, bottom fixed" ([Math]::Abs($o2.w - ($o1.w + 100)) -le 2 -and [Math]::Abs(($o2.y + $o2.h) - ($o1.y + $o1.h)) -le 2) "w $($o1.w)->$($o2.w)"
        [KtMouse]::RightClick($o2.x + 200, $o2.y + $o2.h - 30)
        Check "mouse: right click opens context menu" (Wait-Until { (St).overlay_menu_open } 3)
        Start-Sleep 0.8; [KtMouse]::Escape(); Start-Sleep 0.6
        Check "Esc closes menu" (-not (St).overlay_menu_open)
        $os = (Get-Content $settingsFile -Raw -Encoding UTF8 | ConvertFrom-Json).ui.overlay
        Start-Sleep 0.6
        $os = (Get-Content $settingsFile -Raw -Encoding UTF8 | ConvertFrom-Json).ui.overlay
        Check "overlay position/width saved" ($os.width -eq $o2.w -and $os.bottom -eq ($o2.y + $o2.h)) "saved w=$($os.width) bottom=$($os.bottom)"
    } finally { [KtMouse]::Restore($cursor) }
}

# ------------------------------------------------------------------ 7. home page + tray menu
Nav "home"; Start-Sleep 3
Shot "01_home"
$sv = PageScroller
$sv.GetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern).SetScrollPercent(-1, 100); Start-Sleep 0.8
Shot "01b_home_list"
$sv.GetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern).SetScrollPercent(-1, 0)
$items = (Invoke-Hook $id tray_items).items
Check "tray menu has 4 items" (@($items).Count -eq 4) ($items -join " | ")
Invoke-Hook $id "tray_menu #1" | Out-Null; Start-Sleep 0.5
Check "tray: hide overlay" ((St).overlay_visible -eq $false)
Invoke-Hook $id "tray_menu #1" | Out-Null; Start-Sleep 0.8
Check "tray: show overlay again" ((St).overlay_visible -eq $true -and (St).overlay.h -gt 30)
Invoke-Hook $id "tray_menu #2" | Out-Null
Check "tray: pause" (Wait-Until { (St).paused } 3)
Invoke-Hook $id "tray_menu #2" | Out-Null
Check "tray: resume" (Wait-Until { -not (St).paused } 3)

# ------------------------------------------------------------------ 8. close to tray + single instance
Invoke-Hook $id close_main | Out-Null; Start-Sleep 1
Check "closing main window hides it (process keeps running)" ((St).main_visible -eq $false -and (Get-Process -Id $id -ErrorAction SilentlyContinue))
Check "first close shows tray hint once" ((Get-Content $settingsFile -Raw -Encoding UTF8 | ConvertFrom-Json).ui.tray_hint_shown -eq $true)
Invoke-Hook $id tray_dblclick | Out-Null; Start-Sleep 0.8
Check "tray double-click shows main window" ((St).main_visible -eq $true)
Invoke-Hook $id close_main | Out-Null; Start-Sleep 0.8
$p2 = Start-Kikitori -Exe $Exe
[void]$p2.WaitForExit(15000)
Start-Sleep 0.8
Check "second instance exits and activates the first" ($p2.HasExited -and (St).main_visible -eq $true -and @(Get-Process Kikitori).Count -eq 1)

# ------------------------------------------------------------------ 9. engine crash -> InfoBar -> restart
Invoke-Hook $id "main_rect 560 20 1100 1040" | Out-Null
Nav "home"
foreach ($e in Get-EngineProcesses) { Stop-Process -Id $e.ProcessId -Force -ErrorAction SilentlyContinue }
Check "engine crash detected" (Wait-Until { (St).engine_state -eq "not_running" -and (St).failure } 5)
Start-Sleep 0.8
Shot "11_engine_crashed"; Shot "11b_overlay_engine_crashed" overlay 8
Invoke-Uia (Find-Uia (MainRoot) -AutomationId "RestartEngineButton")
Check "restart engine -> listening again" (Wait-Until { $x = St; $x.engine_ready -and $x.engine_state -eq "listening" -and -not $x.failure } 15)

# ------------------------------------------------------------------ 10. exit from overlay menu, no leftover engine
Start-Sleep 1
$enginesBefore = @(Get-EngineProcesses).Count
Invoke-OverlayMenu $id @("menu_exit")
$gone = Wait-Until { -not (Get-Process -Id $id -ErrorAction SilentlyContinue) } 8
Check "exit via overlay menu" $gone
Start-Sleep 1
Check "no engine process left after exit" (@(Get-EngineProcesses).Count -eq 0) "engines before exit: $enginesBefore"
Check "engine got shutdown command" ((Get-Content (Join-Path $script:KtData "logs\engine.log") -Raw -Encoding UTF8) -match "cmd: shutdown")

# ------------------------------------------------------------------ 11. restart: settings persisted
$p = Start-Kikitori -Exe $Exe
$script:id = $p.Id
[void](Wait-Hook $id 30)
$s = St
$os = $s.overlay_settings
Check "persisted: overlay style (lines 4, font 24, opacity 0.7)" ($os.MaxLines -eq 4 -and $os.FontSize -eq 24 -and $os.Opacity -eq 0.7)
if (-not $NoMouse) { Check "persisted: overlay position/width" ($s.overlay.w -eq $o2.w -and ($s.overlay.y + $s.overlay.h) -eq ($o2.y + $o2.h)) "w=$($s.overlay.w)" }
Check "persisted: API key works after restart (translations)" (Wait-Until { @((Invoke-Hook $id overlay_lines).lines | Where-Object { $_.main -and $_.main_color -eq "#FFFFFF" }).Count -gt 0 } 25)
$g = (Get-Content $settingsFile -Raw -Encoding UTF8 | ConvertFrom-Json).engine.glossary
Check "persisted: glossary" (@($g).Count -eq 2)
Invoke-Hook $id "tray_menu #3" | Out-Null
Check "exit via tray menu" (Wait-Until { -not (Get-Process -Id $id -ErrorAction SilentlyContinue) } 8)
Start-Sleep 1
Check "no engine process left after second exit" (@(Get-EngineProcesses).Count -eq 0)

$fails = @($results | Where-Object { $_ -like "FAIL*" }).Count
$summary = "{0} checks, {1} failed" -f $results.Count, $fails
Write-Host $summary
New-Item -ItemType Directory -Force $script:KtShots | Out-Null
($results + $summary) | Set-Content (Join-Path $script:KtShots "ui_test_report.txt") -Encoding UTF8
