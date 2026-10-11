"""语言表：说话语言（语音识别用）和翻译目标语言。"""

# (Whisper 语言代码, 菜单里显示的名字, 写进翻译提示词里的名字)
SOURCE_LANGS = [
    ("ja", "日语", "日语"),
    ("en", "英语", "英语"),
    ("ko", "韩语", "韩语"),
    ("zh", "中文", "中文"),
    ("ru", "俄语", "俄语"),
    ("fr", "法语", "法语"),
    ("es", "西班牙语", "西班牙语"),
    ("de", "德语", "德语"),
    ("auto", "自动识别", "外语"),
]

# (代码, 菜单里显示的名字, 写进翻译提示词里的名字)
TARGET_LANGS = [
    ("zh-Hans", "简体中文", "简体中文"),
    ("zh-Hant", "繁體中文", "繁体中文（台湾用语习惯）"),
    ("en", "English", "英语"),
    ("ja", "日本語", "日语"),
    ("ko", "한국어", "韩语"),
]

_SRC = {c: (menu, prompt) for c, menu, prompt in SOURCE_LANGS}
_TGT = {c: (menu, prompt) for c, menu, prompt in TARGET_LANGS}


def normalize_source(code: str) -> str:
    code = (code or "ja").strip().lower()
    return code if code in _SRC else "ja"


def normalize_target(code: str) -> str:
    code = (code or "zh-Hans").strip()
    for c in _TGT:
        if c.lower() == code.lower():
            return c
    return "zh-Hans"


def source_menu_name(code: str) -> str:
    return _SRC.get(code, (code, code))[0]


def source_prompt_name(code: str) -> str:
    return _SRC.get(code, (code, code))[1]


def target_menu_name(code: str) -> str:
    return _TGT.get(code, (code, code))[0]


def target_prompt_name(code: str) -> str:
    return _TGT.get(code, (code, code))[1]


def same_language(spoken: str | None, target: str) -> bool:
    """说的就是目标语言时不用翻译（例如中文直播 + 目标简体中文）。繁体目标仍然要转换。"""
    if not spoken:
        return False
    if spoken == "zh":
        return target == "zh-Hans"
    return spoken == target


# 字幕用字体：同一个汉字中日字形不同，韩文需要有韩文字形的字体
def font_for(code: str | None) -> tuple[str, ...]:
    return {
        "ja": ("Yu Gothic UI", "Meiryo UI", "Meiryo", "MS Gothic"),
        "ko": ("Malgun Gothic",),
        "zh-Hant": ("Microsoft JhengHei UI", "Microsoft JhengHei"),
    }.get(code or "", ()) + ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
