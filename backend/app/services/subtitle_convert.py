"""
字幕格式转换（N8）
==================

SRT → ASS 转换：把纯文本 SRT 字幕转为带样式的 ASS，让支持 ASS 的播放器/渲染器
获得更好的排版（字体、描边、位置）。参考 MediaWarp 的 Subtitle.SRT2ASS 思路。

设计取舍：
- 纯函数、无外部依赖，便于单测。
- 保留 SRT 的基础内联标签（<b>/<i>/<u>）转为 ASS 覆盖码。
- 默认样式为居中底部、白字黑边，适配大多数场景。
"""
import re

# ASS 默认样式头（1080p 参考分辨率，白字黑边居中底部）
_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,1,2,20,20,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _srt_ts_to_ass(h: str, m: str, s: str, ms: str) -> str:
    """SRT 时间戳 (HH:MM:SS,mmm) → ASS 时间戳 (H:MM:SS.cc，百分秒)。"""
    centis = int(ms) // 10
    return f"{int(h)}:{m}:{s}.{centis:02d}"


def _convert_inline_tags(text: str) -> str:
    """将 SRT 内联 HTML 标签转为 ASS 覆盖码，其余标签剥离。"""
    # 换行：SRT 用真实换行 → ASS 用 \N
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n", "\\N")
    # 基础样式标签
    text = re.sub(r"(?i)<b>", r"{\\b1}", text)
    text = re.sub(r"(?i)</b>", r"{\\b0}", text)
    text = re.sub(r"(?i)<i>", r"{\\i1}", text)
    text = re.sub(r"(?i)</i>", r"{\\i0}", text)
    text = re.sub(r"(?i)<u>", r"{\\u1}", text)
    text = re.sub(r"(?i)</u>", r"{\\u0}", text)
    # 剥离其余 HTML 标签（如 <font>）
    text = re.sub(r"<[^>]+>", "", text)
    return text


def srt_to_ass(srt_text: str) -> str:
    """将 SRT 字幕文本转换为 ASS 字幕文本。

    解析 SRT 的"序号 / 时间轴 / 多行文本"块，逐条生成 ASS Dialogue 行。
    无法解析的内容跳过；返回完整可用的 ASS 文本。
    """
    if not srt_text:
        return _ASS_HEADER
    # 去 BOM
    srt_text = srt_text.lstrip("\ufeff")
    lines = srt_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    dialogues = []
    i = 0
    n = len(lines)
    while i < n:
        # 跳过空行
        if not lines[i].strip():
            i += 1
            continue
        # 可选的序号行
        if lines[i].strip().isdigit():
            i += 1
            if i >= n:
                break
        # 时间轴行
        m = _TS_RE.search(lines[i]) if i < n else None
        if not m:
            i += 1
            continue
        start = _srt_ts_to_ass(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _srt_ts_to_ass(m.group(5), m.group(6), m.group(7), m.group(8))
        i += 1
        # 收集文本行直到空行
        text_lines = []
        while i < n and lines[i].strip():
            text_lines.append(lines[i])
            i += 1
        if not text_lines:
            continue
        text = _convert_inline_tags("\n".join(text_lines))
        dialogues.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    return _ASS_HEADER + "\n".join(dialogues) + ("\n" if dialogues else "")


def is_srt_content(data: bytes) -> bool:
    """粗略判断字节内容是否为 SRT 字幕（含 --> 时间轴箭头）。"""
    if not data:
        return False
    try:
        head = data[:2048].decode("utf-8", errors="replace")
    except Exception:
        return False
    return "-->" in head and bool(_TS_RE.search(head))
