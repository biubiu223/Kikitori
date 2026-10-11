using System.Text.Json.Nodes;
using Kikitori.Services;
using Microsoft.UI.Dispatching;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Kikitori.Views;

public sealed partial class TranslationPage : Page
{
    private readonly AppController _ctl = AppController.Instance;
    private readonly DispatcherQueueTimer _testTimeout;
    private bool _loading = true;   // InitializeComponent 期间控件也会触发事件，加载完才处理

    public TranslationPage()
    {
        InitializeComponent();
        _testTimeout = DispatcherQueue.CreateTimer();
        _testTimeout.Interval = TimeSpan.FromSeconds(30);
        _testTimeout.IsRepeating = false;
        _testTimeout.Tick += (_, _) => ShowTestResult(false, "测试超时", "30 秒内没有收到引擎的回应。", "");
        Loaded += (_, _) =>
        {
            _ctl.TranslationTestCompleted += OnTestCompleted;
            LoadValues();
        };
        Unloaded += (_, _) => _ctl.TranslationTestCompleted -= OnTestCompleted;
    }

    private static Models.TranslateSettings T => SettingsService.Current.Engine.Translate;

    private void LoadValues()
    {
        _loading = true;
        var t = T;
        SelectByTag(ProviderCombo, t.Provider);
        DeepSeekKeyBox.Password = t.Deepseek.ApiKey;
        if (!SelectByTag(DeepSeekModelCombo, t.Deepseek.Model))
        {
            // 用户手改过的其它模型名也显示出来
            DeepSeekModelCombo.Items.Add(new ComboBoxItem { Content = t.Deepseek.Model, Tag = t.Deepseek.Model });
            SelectByTag(DeepSeekModelCombo, t.Deepseek.Model);
        }
        DeepSeekUrlBox.Text = t.Deepseek.BaseUrl;
        DeepSeekTempBox.Value = t.Deepseek.Temperature;
        OllamaUrlBox.Text = t.Ollama.BaseUrl;
        OllamaModelBox.Text = t.Ollama.Model;
        OllamaTempBox.Value = t.Ollama.Temperature;
        OpenAiUrlBox.Text = t.OpenaiCompatible.BaseUrl;
        OpenAiKeyBox.Password = t.OpenaiCompatible.ApiKey;
        OpenAiModelBox.Text = t.OpenaiCompatible.Model;
        OpenAiTempBox.Value = t.OpenaiCompatible.Temperature;
        ContextBox.Value = t.ContextLines;
        _loading = false;
        UpdatePanels();
    }

    private static bool SelectByTag(ComboBox combo, string tag)
    {
        foreach (var item in combo.Items.OfType<ComboBoxItem>())
        {
            if ((string)item.Tag == tag)
            {
                combo.SelectedItem = item;
                return true;
            }
        }
        return false;
    }

    private static string? SelectedTag(ComboBox combo) => (combo.SelectedItem as ComboBoxItem)?.Tag as string;

    private void UpdatePanels()
    {
        var p = T.Provider;
        DeepSeekPanel.Visibility = p == "deepseek" ? Visibility.Visible : Visibility.Collapsed;
        OllamaPanel.Visibility = p == "ollama" ? Visibility.Visible : Visibility.Collapsed;
        OpenAiPanel.Visibility = p == "openai_compatible" ? Visibility.Visible : Visibility.Collapsed;
        OffInfo.Visibility = p == "off" ? Visibility.Visible : Visibility.Collapsed;
        TestCard.IsEnabled = p != "off";
    }

    private void Changed() => _ctl.NotifyEngineConfigChanged();

    private void ProviderCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loading || SelectedTag(ProviderCombo) is not string tag || tag == T.Provider) return;
        T.Provider = tag;
        UpdatePanels();
        TestResultBar.IsOpen = false;
        Changed();
    }

    private void DeepSeekModelCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loading || SelectedTag(DeepSeekModelCombo) is not string tag) return;
        Set(T.Deepseek.Model, tag, v => T.Deepseek.Model = v);
    }

    // 只有值真的变了才通知（控件加载时也会触发这些事件）
    private void Set<TV>(TV current, TV value, Action<TV> apply)
    {
        if (EqualityComparer<TV>.Default.Equals(current, value)) return;
        apply(value);
        Changed();
    }

    private void DeepSeekKeyBox_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (_loading) return;
        Set(T.Deepseek.ApiKey, DeepSeekKeyBox.Password.Trim(), v => T.Deepseek.ApiKey = v);
    }

    private void OpenAiKeyBox_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (_loading) return;
        Set(T.OpenaiCompatible.ApiKey, OpenAiKeyBox.Password.Trim(), v => T.OpenaiCompatible.ApiKey = v);
    }

    private void Field_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (_loading) return;
        var t = T;
        if (ReferenceEquals(sender, DeepSeekUrlBox)) Set(t.Deepseek.BaseUrl, DeepSeekUrlBox.Text.Trim(), v => t.Deepseek.BaseUrl = v);
        else if (ReferenceEquals(sender, OllamaUrlBox)) Set(t.Ollama.BaseUrl, OllamaUrlBox.Text.Trim(), v => t.Ollama.BaseUrl = v);
        else if (ReferenceEquals(sender, OllamaModelBox)) Set(t.Ollama.Model, OllamaModelBox.Text.Trim(), v => t.Ollama.Model = v);
        else if (ReferenceEquals(sender, OpenAiUrlBox)) Set(t.OpenaiCompatible.BaseUrl, OpenAiUrlBox.Text.Trim(), v => t.OpenaiCompatible.BaseUrl = v);
        else if (ReferenceEquals(sender, OpenAiModelBox)) Set(t.OpenaiCompatible.Model, OpenAiModelBox.Text.Trim(), v => t.OpenaiCompatible.Model = v);
    }

    private void Number_ValueChanged(NumberBox sender, NumberBoxValueChangedEventArgs args)
    {
        if (_loading || double.IsNaN(sender.Value)) return;
        var t = T;
        if (ReferenceEquals(sender, DeepSeekTempBox)) Set(t.Deepseek.Temperature, Math.Round(sender.Value, 2), v => t.Deepseek.Temperature = v);
        else if (ReferenceEquals(sender, OllamaTempBox)) Set(t.Ollama.Temperature, Math.Round(sender.Value, 2), v => t.Ollama.Temperature = v);
        else if (ReferenceEquals(sender, OpenAiTempBox)) Set(t.OpenaiCompatible.Temperature, Math.Round(sender.Value, 2), v => t.OpenaiCompatible.Temperature = v);
        else if (ReferenceEquals(sender, ContextBox)) Set(t.ContextLines, (int)Math.Round(sender.Value), v => t.ContextLines = v);
    }

    // ------------------------------------------------------------------ 测试连接
    private void Test_Click(object sender, RoutedEventArgs e)
    {
        TestResultBar.IsOpen = false;
        if (!_ctl.TestTranslation())
        {
            ShowTestResult(false, "无法测试", "识别引擎没有在运行，请先在首页确认引擎状态（或点“重启引擎”）。", "");
            return;
        }
        TestRing.IsActive = true;
        TestButton.IsEnabled = false;
        _testTimeout.Stop();
        _testTimeout.Start();
    }

    private void OnTestCompleted(JsonObject e)
    {
        bool ok = e["ok"] is JsonValue v && v.TryGetValue<bool>(out var b) && b;
        string message = Get(e, "message");
        string detail = "";
        if (ok)
        {
            var input = Get(e, "input");
            var output = Get(e, "output");
            var elapsed = e["elapsed"] is JsonValue ev && ev.TryGetValue<double>(out var sec) ? $"（用时 {sec:0.00} 秒）" : "";
            detail = $"{input}\n→ {output}";
            message = (string.IsNullOrEmpty(message) ? "连接正常" : message) + elapsed;
        }
        ShowTestResult(ok, ok ? "测试成功" : "测试失败", message, detail);
    }

    private void ShowTestResult(bool ok, string title, string message, string detail)
    {
        _testTimeout.Stop();
        TestRing.IsActive = false;
        TestButton.IsEnabled = true;
        TestResultBar.Severity = ok ? InfoBarSeverity.Success : InfoBarSeverity.Error;
        TestResultBar.Title = title;
        TestResultBar.Message = string.IsNullOrEmpty(detail) ? message : message + "\n" + detail;
        TestResultBar.IsOpen = true;
        DispatcherQueue.TryEnqueue(() => TestResultBar.StartBringIntoView());
    }

    private static string Get(JsonObject o, string key) =>
        o[key] is JsonValue v && v.TryGetValue<string>(out var s) ? s : "";
}
