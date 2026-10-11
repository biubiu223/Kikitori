using System.Collections.ObjectModel;
using Kikitori.Models;
using Kikitori.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;

namespace Kikitori.Views;

public sealed partial class GlossaryPage : Page
{
    private readonly AppController _ctl = AppController.Instance;
    private readonly ObservableCollection<GlossaryEntry> _items;

    public GlossaryPage()
    {
        InitializeComponent();
        _items = new ObservableCollection<GlossaryEntry>(SettingsService.Current.Engine.Glossary);
        _snapshot = Snapshot(_items);
        GlossaryList.ItemsSource = _items;
        _items.CollectionChanged += (_, _) => UpdateHint();
        UpdateHint();
    }

    private void UpdateHint()
    {
        EmptyHint.Visibility = _items.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        int valid = _items.Count(i => i.Source.Trim().Length > 0);
        CountText.Text = _items.Count == 0 ? "" : $"共 {valid} 条";
    }

    private string _snapshot = "";

    /// <summary>真正会发给引擎的内容（去掉空行和首尾空格）。</summary>
    private static string Snapshot(IEnumerable<GlossaryEntry> items) =>
        string.Join("\u0001", items.Where(i => i.Source.Trim().Length > 0).Select(i => i.Source.Trim() + "\u0002" + i.Target.Trim()));

    private void Sync()
    {
        UpdateHint();
        SettingsService.Current.Engine.Glossary = _items.ToList();
        SettingsService.SaveSoon();
        var snap = Snapshot(_items);
        if (snap == _snapshot) return;          // 控件加载时触发的 TextChanged，或者只加了空行：引擎不用重启
        _snapshot = snap;
        _ctl.NotifyEngineConfigChanged();
    }

    private void Entry_TextChanged(object sender, TextChangedEventArgs e)
    {
        // x:Bind TwoWay 会把文字写回 GlossaryEntry；这里稍后再保存，保证拿到的是新值
        DispatcherQueue.TryEnqueue(Sync);
    }

    private void Add_Click(object sender, RoutedEventArgs e)
    {
        var entry = new GlossaryEntry();
        _items.Add(entry);
        Sync();
        GlossaryList.ScrollIntoView(entry);
        DispatcherQueue.TryEnqueue(() =>
        {
            if (GlossaryList.ContainerFromItem(entry) is ListViewItem c && FindTextBox(c) is TextBox tb)
                tb.Focus(FocusState.Programmatic);
        });
    }

    private void Delete_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.Tag is GlossaryEntry entry)
        {
            _items.Remove(entry);
            Sync();
        }
    }

    private static TextBox? FindTextBox(DependencyObject root)
    {
        int n = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChildrenCount(root);
        for (int i = 0; i < n; i++)
        {
            var child = Microsoft.UI.Xaml.Media.VisualTreeHelper.GetChild(root, i);
            if (child is TextBox tb) return tb;
            if (FindTextBox(child) is TextBox found) return found;
        }
        return null;
    }
}
