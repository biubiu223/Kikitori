using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Windows.Foundation;

namespace Kikitori.Controls;

/// <summary>
/// 设置页的内容列：宽度 = min(ColumnMaxWidth, 可用宽度)，水平居中。
/// （直接在 ScrollViewer 里的 StackPanel 上设 MaxWidth 时，内容比视口窄会按“期望宽度”居中，导致整页往右偏、右边被裁掉。）
/// </summary>
public sealed class PageColumn : Panel
{
    public double ColumnMaxWidth { get; set; } = 1064;

    protected override Size MeasureOverride(Size availableSize)
    {
        double w = double.IsInfinity(availableSize.Width) ? ColumnMaxWidth : Math.Min(ColumnMaxWidth, availableSize.Width);
        double h = 0;
        foreach (var child in Children)
        {
            child.Measure(new Size(w, availableSize.Height));
            h = Math.Max(h, child.DesiredSize.Height);
        }
        return new Size(w, h);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        double w = Math.Min(ColumnMaxWidth, finalSize.Width);
        double x = Math.Max(0, (finalSize.Width - w) / 2);
        foreach (var child in Children)
            child.Arrange(new Rect(x, 0, w, Math.Max(finalSize.Height, child.DesiredSize.Height)));
        return finalSize;
    }
}
