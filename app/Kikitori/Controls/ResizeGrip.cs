using Microsoft.UI;
using Microsoft.UI.Input;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;

namespace Kikitori.Controls;

/// <summary>字幕条右下角的“拖动调宽度”把手：三道斜线，鼠标变成左右箭头。</summary>
public sealed class ResizeGrip : Grid
{
    private readonly Microsoft.UI.Xaml.Shapes.Path _path;

    public ResizeGrip()
    {
        Background = new SolidColorBrush(Colors.Transparent);   // 透明但能接收鼠标
        ProtectedCursor = InputSystemCursor.Create(InputSystemCursorShape.SizeWestEast);
        _path = new Microsoft.UI.Xaml.Shapes.Path
        {
            Stroke = new SolidColorBrush(Windows.UI.Color.FromArgb(0x80, 0xFF, 0xFF, 0xFF)),
            StrokeThickness = 1.3,
            StrokeStartLineCap = PenLineCap.Round,
            StrokeEndLineCap = PenLineCap.Round,
            HorizontalAlignment = HorizontalAlignment.Right,
            VerticalAlignment = VerticalAlignment.Bottom,
            Margin = new Thickness(0, 0, 5, 5),
            Data = Build(),
        };
        Children.Add(_path);
        PointerEntered += (_, _) => _path.Stroke = new SolidColorBrush(Windows.UI.Color.FromArgb(0xE0, 0xFF, 0xFF, 0xFF));
        PointerExited += (_, _) => _path.Stroke = new SolidColorBrush(Windows.UI.Color.FromArgb(0x80, 0xFF, 0xFF, 0xFF));
    }

    private static Geometry Build()
    {
        var g = new GeometryGroup();
        foreach (var (a, b) in new[] { (0.0, 10.0), (4.0, 10.0), (8.0, 10.0) })
        {
            g.Children.Add(new LineGeometry
            {
                StartPoint = new Windows.Foundation.Point(10, a),
                EndPoint = new Windows.Foundation.Point(a, b),
            });
        }
        return g;
    }
}
