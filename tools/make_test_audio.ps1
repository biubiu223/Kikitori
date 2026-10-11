# 用 Windows 自带的语音合成生成测试录音（tools/testdata/*.wav），供自动化测试使用。
# 录音文件不提交到仓库，克隆后运行一次本脚本即可：
#     powershell -ExecutionPolicy Bypass -File tools\make_test_audio.ps1
#
# 需要系统里装有中文语音（Microsoft Huihui）和英文语音（Microsoft Zira），Windows 10/11 中文版通常自带。

$ErrorActionPreference = "Stop"
$outDir = Join-Path $PSScriptRoot "testdata"
New-Item -ItemType Directory -Force $outDir | Out-Null
Add-Type -AssemblyName System.Speech

function Make-Clip([string]$voiceHint, [string]$file, [string[]]$sentences) {
    $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $voice = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Name -like "*$voiceHint*" } | Select-Object -First 1
        if (-not $voice) {
            Write-Warning "没有找到语音 $voiceHint，跳过 $file"
            return
        }
        $s.SelectVoice($voice.VoiceInfo.Name)
        $path = Join-Path $outDir $file
        $s.SetOutputToWaveFile($path)
        foreach ($t in $sentences) { $s.Speak($t) }   # 每句之间会自然留出停顿，用来测试断句
        Write-Host "已生成 $path"
    } finally {
        $s.Dispose()
    }
}

Make-Clip "Huihui" "tts_zh.wav" @(
    "大家晚上好，欢迎来到今天的直播。",
    "今天我们来玩一个新游戏，好像非常难的样子，大家要帮我加油哦。",
    "哈哈哈，好可怕！",
    "那么，谢谢大家的醒目留言，我们下次再见啦。"
)

Make-Clip "Zira" "tts_en.wav" @(
    "Hey chat, welcome back to the stream!",
    "Today we are playing a horror game, and honestly I am already scared.",
    "Thank you so much for the superchat!"
)
