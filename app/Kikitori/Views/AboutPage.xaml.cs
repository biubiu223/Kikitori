using Kikitori.Services;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media.Imaging;

namespace Kikitori.Views;

public sealed partial class AboutPage : Page
{
    private static readonly string[] Help =
    [
        "在「翻译」页选择翻译引擎。推荐 DeepSeek：填入 API Key 后点「测试连接」确认能用。",
        "回到「首页」，选好主播说的语言和要翻译成的语言，点「开始监听」。第一次加载语音识别模型需要十几秒。",
        "打开直播，字幕会出现在屏幕下方的字幕条上。录的是电脑正在播放的声音（默认播放设备），不需要麦克风。",
        "字幕条：左键拖动移动位置，拖右下角调宽度，右键打开菜单（暂停、切换语言、字号、条数、不透明度、清空…）。",
        "主播名字、粉丝名等可以在「专有名词」页添加，识别和翻译都会更准。",
        "关闭主窗口后程序仍在托盘里工作；要退出请在托盘图标或字幕条上右键 →「退出」。",
        "每次监听的字幕会自动保存在「字幕记录」文件夹里。",
    ];

    private readonly AppController _ctl = AppController.Instance;

    public AboutPage()
    {
        InitializeComponent();
        if (File.Exists(AppPaths.LogoFile)) LogoImage.Source = new BitmapImage(new Uri(AppPaths.LogoFile));
        VersionText.Text = $"版本 {App.Version}";
        DataCard.Description = AppPaths.DataDir;
        for (int i = 0; i < Help.Length; i++)
        {
            var g = new Grid { ColumnSpacing = 10 };
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var num = new TextBlock { Text = $"{i + 1}.", Width = 18, Foreground = (Microsoft.UI.Xaml.Media.Brush)Application.Current.Resources["AccentTextFillColorPrimaryBrush"], FontWeight = Microsoft.UI.Text.FontWeights.SemiBold };
            var txt = new TextBlock { Text = Help[i], TextWrapping = TextWrapping.Wrap };
            Grid.SetColumn(txt, 1);
            g.Children.Add(num);
            g.Children.Add(txt);
            HelpPanel.Children.Add(g);
        }
        Loaded += (_, _) => { _ctl.StateChanged += UpdateEngine; UpdateEngine(); };
        Unloaded += (_, _) => _ctl.StateChanged -= UpdateEngine;
    }

    private void UpdateEngine()
    {
        var cmd = _ctl.Engine.Command;
        string ver = string.IsNullOrEmpty(_ctl.EngineVersion) ? "未连接" : _ctl.EngineVersion;
        EngineText.Text = $"识别引擎 {ver}" + (cmd is null ? "" : $"（{Path.GetFileName(cmd.FileName)}）");
    }

    private void OpenLogs_Click(object sender, RoutedEventArgs e) => AppPaths.OpenFolder(AppPaths.LogsDir);
    private void OpenData_Click(object sender, RoutedEventArgs e) => AppPaths.OpenFolder(AppPaths.DataDir);
    private void OpenRecords_Click(object sender, RoutedEventArgs e) => AppPaths.OpenFolder(AppPaths.SubtitleDir);
}
