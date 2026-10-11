using System.Security.Cryptography;
using System.Text;

namespace Kikitori.Services;

/// <summary>
/// API Key 用 Windows DPAPI（CurrentUser）加密后再写进 settings.json；
/// 只有当前 Windows 用户能解开，拷到别的电脑/别的账户上就是一串无用的密文。
/// </summary>
public static class Secret
{
    private const string Prefix = "dpapi:";
    private static readonly byte[] Entropy = Encoding.UTF8.GetBytes("Kikitori.ApiKey.v1");

    public static string Protect(string? plain)
    {
        if (string.IsNullOrEmpty(plain)) return "";
        try
        {
            var bytes = ProtectedData.Protect(Encoding.UTF8.GetBytes(plain), Entropy, DataProtectionScope.CurrentUser);
            return Prefix + Convert.ToBase64String(bytes);
        }
        catch (Exception e)
        {
            Log.Error("加密 API Key 失败：" + e.Message);
            return "";   // 宁可丢掉也不写明文
        }
    }

    public static string Unprotect(string? stored)
    {
        if (string.IsNullOrEmpty(stored)) return "";
        if (!stored.StartsWith(Prefix, StringComparison.Ordinal)) return "";
        try
        {
            var bytes = ProtectedData.Unprotect(Convert.FromBase64String(stored[Prefix.Length..]), Entropy, DataProtectionScope.CurrentUser);
            return Encoding.UTF8.GetString(bytes);
        }
        catch (Exception e)
        {
            Log.Warn("无法解密已保存的 API Key（可能是换了电脑或 Windows 账户），需要重新填写：" + e.Message);
            return "";
        }
    }
}
