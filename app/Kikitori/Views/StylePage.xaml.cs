using Kikitori.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;

namespace Kikitori.Views;

public sealed partial class StylePage : Page
{
    private readonly AppController _ctl = AppController.Instance;
    private bool _loading = true;

    public StylePage()
    {
        InitializeComponent();
        Loaded += (_, _) =>
        {
            _ctl.OverlayStyleChanged += LoadValues;
            _ctl.OverlayGeometryChanged += LoadValues;
            _ctl.OverlayVisibilityChanged += LoadValues;
            LoadValues();
        };
        Unloaded += (_, _) =>
        {
            _ctl.OverlayStyleChanged -= LoadValues;
            _ctl.OverlayGeometryChanged -= LoadValues;
            _ctl.OverlayVisibilityChanged -= LoadValues;
        };
    }

    private static Models.OverlaySettings O => SettingsService.Current.Ui.Overlay;

    private void LoadValues()
    {
        _loading = true;
        var o = O;
        FontSlider.Value = o.FontSize;
        SourceFontSlider.Value = o.SourceFontSize;
        ShowSourceToggle.IsOn = o.ShowSource;
        CenterToggle.IsOn = o.Centered;
        LinesSlider.Value = o.MaxLines;
        OpacitySlider.Value = Math.Round(o.Opacity * 100);
        var overlay = App.Current.Overlay;
        if (overlay is not null)
        {
            WidthSlider.Minimum = overlay.MinWidthPx;
            WidthSlider.Maximum = Math.Max(overlay.MinWidthPx + 10, overlay.MaxWidthPx);
            WidthSlider.Value = overlay.CurrentWidthPx;
        }
        VisibleToggle.IsOn = SettingsService.Current.Ui.ShowOverlay;
        UpdateLabels();
        _loading = false;
    }

    private void UpdateLabels()
    {
        FontText.Text = $"{FontSlider.Value:0}";
        SourceFontText.Text = $"{SourceFontSlider.Value:0}";
        LinesText.Text = $"{LinesSlider.Value:0} 条";
        OpacityText.Text = $"{OpacitySlider.Value:0}%";
        WidthText.Text = $"{WidthSlider.Value:0} 像素";
    }

    private void Slider_ValueChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (WidthText is null || FontText is null || SourceFontText is null || LinesText is null || OpacityText is null) return;
        UpdateLabels();
        if (_loading) return;
        var o = O;
        if (ReferenceEquals(sender, WidthSlider))
        {
            App.Current.Overlay?.SetWidth((int)Math.Round(WidthSlider.Value));
            return;
        }
        if (ReferenceEquals(sender, FontSlider)) o.FontSize = Math.Round(FontSlider.Value);
        else if (ReferenceEquals(sender, SourceFontSlider)) o.SourceFontSize = Math.Round(SourceFontSlider.Value);
        else if (ReferenceEquals(sender, LinesSlider)) o.MaxLines = (int)Math.Round(LinesSlider.Value);
        else if (ReferenceEquals(sender, OpacitySlider)) o.Opacity = Math.Round(OpacitySlider.Value) / 100.0;
        Notify();
    }

    private void Toggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_loading) return;
        O.ShowSource = ShowSourceToggle.IsOn;
        O.Centered = CenterToggle.IsOn;
        Notify();
    }

    private void VisibleToggle_Toggled(object sender, RoutedEventArgs e)
    {
        if (_loading) return;
        _ctl.SetOverlayVisible(VisibleToggle.IsOn);
    }

    private void Notify()
    {
        // 自己触发的刷新不用回填控件（避免拖动滑块时跳动）
        _ctl.OverlayStyleChanged -= LoadValues;
        _ctl.NotifyOverlayStyleChanged();
        _ctl.OverlayStyleChanged += LoadValues;
    }

    private void Reset_Click(object sender, RoutedEventArgs e)
    {
        App.Current.Overlay?.ResetPosition();
        if (!SettingsService.Current.Ui.ShowOverlay) _ctl.SetOverlayVisible(true);
    }

    private void Defaults_Click(object sender, RoutedEventArgs e)
    {
        var d = new Models.OverlaySettings();
        var o = O;
        o.FontSize = d.FontSize;
        o.SourceFontSize = d.SourceFontSize;
        o.MaxLines = d.MaxLines;
        o.Opacity = d.Opacity;
        o.ShowSource = d.ShowSource;
        o.Centered = d.Centered;
        _ctl.NotifyOverlayStyleChanged();
    }
}
