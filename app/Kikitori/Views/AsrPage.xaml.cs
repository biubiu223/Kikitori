using Kikitori.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;

namespace Kikitori.Views;

public sealed partial class AsrPage : Page
{
    private readonly AppController _ctl = AppController.Instance;
    private bool _loading = true;   // InitializeComponent 期间控件也会触发事件，加载完才处理

    public AsrPage()
    {
        InitializeComponent();
        Loaded += (_, _) => LoadValues();
    }

    private static Models.EngineSettings E => SettingsService.Current.Engine;

    private void LoadValues()
    {
        _loading = true;
        var e = E;
        if (!SelectByTag(ModelCombo, e.Asr.Model))
        {
            ModelCombo.Items.Add(new ComboBoxItem { Content = e.Asr.Model, Tag = e.Asr.Model });
            SelectByTag(ModelCombo, e.Asr.Model);
        }
        SelectByTag(DeviceCombo, e.Asr.Device);
        SelectByTag(ComputeCombo, e.Asr.ComputeType);
        SelectByTag(SourceCombo, e.Download.Source);
        PartialToggle.IsOn = e.Asr.ShowPartial;
        ThresholdSlider.Value = e.Vad.Threshold;
        SilenceSlider.Value = e.Vad.MinSilenceMs;
        MaxSegSlider.Value = e.Vad.MaxSegmentS;
        BeamBox.Value = e.Asr.BeamSize;
        PartialIntervalBox.Value = e.Asr.PartialInterval;
        MinSpeechBox.Value = e.Vad.MinSpeechMs;
        UpdateLabels();
        _loading = false;
    }

    private void UpdateLabels()
    {
        ThresholdText.Text = $"{ThresholdSlider.Value:0.00}";
        SilenceText.Text = $"{SilenceSlider.Value:0} 毫秒";
        MaxSegText.Text = $"{MaxSegSlider.Value:0.#} 秒";
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

    // 只有值真的变了才通知（控件加载时也会触发这些事件）
    private void Set<TV>(TV current, TV value, Action<TV> apply)
    {
        if (EqualityComparer<TV>.Default.Equals(current, value)) return;
        apply(value);
        _ctl.NotifyEngineConfigChanged();
    }

    private void Combo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loading || sender is not ComboBox { SelectedItem: ComboBoxItem { Tag: string tag } } combo) return;
        var asr = E.Asr;
        if (ReferenceEquals(combo, SourceCombo))
        {
            // 下载源：和其它识别设置一样，防抖后重发 start
            Set(E.Download.Source, tag, v => E.Download.Source = v);
            return;
        }
        if ((ReferenceEquals(combo, ModelCombo) && asr.Model == tag) || (ReferenceEquals(combo, DeviceCombo) && asr.Device == tag)
            || (ReferenceEquals(combo, ComputeCombo) && asr.ComputeType == tag)) return;
        if (ReferenceEquals(combo, ModelCombo)) asr.Model = tag;
        else if (ReferenceEquals(combo, DeviceCombo))
        {
            asr.Device = tag;
            // CPU 不支持 float16，顺手换成 int8
            if (tag == "cpu" && asr.ComputeType != "int8") { asr.ComputeType = "int8"; _loading = true; SelectByTag(ComputeCombo, "int8"); _loading = false; }
            if (tag == "cuda" && asr.ComputeType == "int8") { asr.ComputeType = "float16"; _loading = true; SelectByTag(ComputeCombo, "float16"); _loading = false; }
        }
        else if (ReferenceEquals(combo, ComputeCombo)) asr.ComputeType = tag;
        _ctl.NotifyEngineConfigChanged();
    }

    private void PartialToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_loading) return;
        Set(E.Asr.ShowPartial, PartialToggle.IsOn, v => E.Asr.ShowPartial = v);
    }

    private void Slider_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (ThresholdText is null || SilenceText is null || MaxSegText is null) return;   // InitializeComponent 期间
        UpdateLabels();
        if (_loading) return;
        var vad = E.Vad;
        if (ReferenceEquals(sender, ThresholdSlider)) Set(vad.Threshold, Math.Round(ThresholdSlider.Value, 2), v => vad.Threshold = v);
        else if (ReferenceEquals(sender, SilenceSlider)) Set(vad.MinSilenceMs, (int)Math.Round(SilenceSlider.Value), v => vad.MinSilenceMs = v);
        else if (ReferenceEquals(sender, MaxSegSlider)) Set(vad.MaxSegmentS, Math.Round(MaxSegSlider.Value, 1), v => vad.MaxSegmentS = v);
    }

    private void Number_ValueChanged(NumberBox sender, NumberBoxValueChangedEventArgs args)
    {
        if (_loading || double.IsNaN(sender.Value)) return;
        var e = E;
        if (ReferenceEquals(sender, BeamBox)) Set(e.Asr.BeamSize, (int)Math.Round(sender.Value), v => e.Asr.BeamSize = v);
        else if (ReferenceEquals(sender, PartialIntervalBox)) Set(e.Asr.PartialInterval, Math.Round(sender.Value, 2), v => e.Asr.PartialInterval = v);
        else if (ReferenceEquals(sender, MinSpeechBox)) Set(e.Vad.MinSpeechMs, (int)Math.Round(sender.Value), v => e.Vad.MinSpeechMs = v);
    }
}
