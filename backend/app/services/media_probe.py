"""
媒体探测服务 - 通过 ffprobe 从 115 直链读取视频文件的编码、分辨率等元数据。
仅下载文件头部（几 MB），不下载完整文件，适用于文件名缺少资源信息的场景。
"""
import subprocess
import json
import shutil
import sys
import asyncio
from pathlib import Path
from typing import Optional
from app.core.logbuffer import get_logger

logger = get_logger()

# 项目内置 ffprobe/ffmpeg 所在目录（backend/bin/）
_BIN_DIR = Path(__file__).resolve().parent.parent.parent / "bin"

# 115 CDN 要求的 User-Agent（与 client_115.DOWNLOAD_USER_AGENT 一致）
_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _get_download_ua() -> str:
    """获取 115 下载专用的 User-Agent"""
    try:
        from app.services.client_115 import Client115Service
        return Client115Service.DOWNLOAD_USER_AGENT
    except Exception as e:
        from app.core.logbuffer import get_logger
        get_logger("app.services.media_probe").debug(f"获取下载 UA 失败，使用默认值: {e}")
        return _DEFAULT_UA


def get_ffprobe_path() -> Optional[str]:
    """
    获取 ffprobe 可执行文件路径。
    优先使用项目内置的 bin/ffprobe(.exe)，其次查找系统 PATH。
    """
    # 1. 优先使用项目内置的 ffprobe
    if sys.platform == "win32":
        builtin = _BIN_DIR / "ffprobe.exe"
    else:
        builtin = _BIN_DIR / "ffprobe"
    if builtin.exists():
        return str(builtin)

    # 2. 查找系统 PATH
    found = shutil.which("ffprobe")
    if found:
        return found

    return None


def is_ffprobe_available() -> bool:
    """检查 ffprobe 是否可用（项目内置或系统安装）"""
    return get_ffprobe_path() is not None


def probe_media_info(download_url: str, timeout: int = 30) -> Optional[dict]:
    """
    通过 ffprobe 从下载直链读取媒体元数据。
    ffprobe 通过 HTTP Range 请求只读取文件头部，不会下载完整文件。

    Args:
        download_url: 115 文件下载直链
        timeout: ffprobe 超时时间（秒）

    Returns:
        包含以下键的字典，失败返回 None：
        resource_pix: 分辨率 (如 "1080p")
        video_encode: 视频编码 (如 "HEVC", "H264")
        audio_encode: 音频编码 (如 "TrueHD", "AAC")
        fps: 帧率 (如 "60FPS")
        duration: 时长（秒）
        bitrate: 总码率 (bps)
        width: 宽度 (px)
        height: 高度 (px)
    """
    ffprobe_path = get_ffprobe_path()
    if not ffprobe_path:
        logger.warning("ffprobe 未安装，跳过媒体探测")
        return None

    ua = _get_download_ua()
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                "-headers", f"Referer: https://115.com\r\nUser-Agent: {ua}\r\n",
                download_url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            err_msg = result.stderr[:300] if result.stderr else ""
            # 分析常见错误原因
            err_lower = err_msg.lower()
            if "403" in err_lower or "forbidden" in err_lower:
                reason = "下载链接被拒绝(403)，可能已过期或被限流"
            elif "404" in err_lower or "not found" in err_lower:
                reason = "下载链接失效(404)，文件可能已被删除"
            elif "connection refused" in err_lower or "connection reset" in err_lower:
                reason = "连接被拒绝或重置，网络问题"
            elif "timed out" in err_lower or "timeout" in err_lower:
                reason = "连接超时"
            elif "permission denied" in err_lower:
                reason = "权限被拒绝"
            elif "invalid data" in err_lower or "malformed" in err_lower:
                reason = "文件格式异常或损坏"
            elif not err_msg:
                reason = "无错误输出（可能链接失效或网络不通）"
            else:
                reason = err_msg
            logger.warning(f"ffprobe 返回码 {result.returncode}（{reason}）: {download_url[:80]}")
            return None

        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.warning(f"ffprobe 超时（{timeout}s）")
        return None
    except json.JSONDecodeError:
        logger.warning("ffprobe 输出 JSON 解析失败")
        return None
    except Exception as e:
        logger.warning(f"ffprobe 执行异常: {e}")
        return None

    streams = data.get("streams", [])
    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio_stream = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
    )
    fmt = data.get("format", {})

    info = {
        "resource_pix": "",
        "video_encode": "",
        "audio_encode": "",
        "fps": "",
        "duration": "",
        "bitrate": "",
        "width": "",
        "height": "",
        "is_hdr": False,
        "aspect_ratio": "",
        "resolution_name": "",
        "audio_channels": "",
    }

    if video_stream:
        height = int(video_stream.get("height", 0) or 0)
        width = int(video_stream.get("width", 0) or 0)
        info["width"] = str(width) if width else ""
        info["height"] = str(height) if height else ""
        info["resource_pix"] = _height_to_pix(height, width)

        codec = video_stream.get("codec_name", "")
        if codec:
            info["video_encode"] = _normalize_codec(codec)

        # 帧率
        r_frame_rate = video_stream.get("r_frame_rate", "0/1")
        fps_val = _eval_frame_rate(r_frame_rate)
        if fps_val > 0:
            info["fps"] = f"{fps_val}FPS"

        # 视频码率
        v_bitrate = video_stream.get("bit_rate", "")
        if v_bitrate:
            info["v_bitrate"] = str(int(v_bitrate))

        # HDR 检测（依据像素格式）
        pix_fmt = video_stream.get("pix_fmt", "")
        if pix_fmt:
            info["is_hdr"] = is_hdr_format(pix_fmt)

        # 宽高比
        if width and height:
            info["aspect_ratio"] = calculate_standard_aspect_ratio(width, height)

    if audio_stream:
        codec = audio_stream.get("codec_name", "")
        if codec:
            info["audio_encode"] = _normalize_audio_codec(codec)

        channels = audio_stream.get("channels", 0)
        if channels:
            info["audio_channels"] = str(channels)

        a_bitrate = audio_stream.get("bit_rate", "")
        if a_bitrate:
            info["a_bitrate"] = str(int(a_bitrate))

    # 格式级信息
    duration = fmt.get("duration", "")
    if duration:
        secs = parse_duration_to_seconds(duration)
        if secs:
            info["duration"] = str(secs)

    total_bitrate = fmt.get("bit_rate", "")
    if total_bitrate:
        info["bitrate"] = str(int(total_bitrate))
    elif video_stream:
        # 格式级比特率缺失时，按 4 种方法回退计算
        calc_bitrate = calculate_bitrate(video_stream, fmt)
        if calc_bitrate:
            info["bitrate"] = str(calc_bitrate)

    # 标准分辨率名检测
    if info["width"] and info["height"]:
        res_info = ResolutionDetector().detect_resolution(
            int(info["width"]), int(info["height"])
        )
        info["resolution_name"] = res_info.get("common_name", "")

    logger.debug(f"ffprobe 探测成功: {info}")
    return info


async def probe_media_info_async(download_url: str, timeout: int = 30, file_name: str = "") -> Optional[dict]:
    """
    异步版本的媒体探测，使用 asyncio subprocess 避免阻塞事件循环。
    file_name: 可选，用于日志中标识是哪个文件
    """
    ffprobe_path = get_ffprobe_path()
    if not ffprobe_path:
        logger.warning("ffprobe 未安装，跳过媒体探测")
        return None

    ua = _get_download_ua()
    _label = f" ({file_name})" if file_name else ""
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe_path,
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            # 限制探测数据量，避免大文件超时（默认 ffprobe 可能读取过多数据）
            "-analyzeduration", "10M",
            "-probesize", "10M",
            "-headers", f"Referer: https://115.com\r\nUser-Agent: {ua}\r\n",
            download_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace")[:300] if stderr else ""
            # 分析常见错误原因
            err_lower = err_msg.lower()
            if "403" in err_lower or "forbidden" in err_lower:
                reason = "下载链接被拒绝(403)，可能已过期或被限流"
            elif "404" in err_lower or "not found" in err_lower:
                reason = "下载链接失效(404)，文件可能已被删除"
            elif "connection refused" in err_lower or "connection reset" in err_lower:
                reason = "连接被拒绝或重置，网络问题"
            elif "timed out" in err_lower or "timeout" in err_lower:
                reason = "连接超时"
            elif "permission denied" in err_lower:
                reason = "权限被拒绝"
            elif "invalid data" in err_lower or "malformed" in err_lower:
                reason = "文件格式异常或损坏"
            elif not err_msg:
                reason = "无错误输出（可能链接失效或网络不通）"
            else:
                reason = err_msg
            logger.warning(f"ffprobe 返回码 {proc.returncode}（{reason}）{_label}: {download_url[:80]}")
            return None

        data = json.loads(stdout.decode("utf-8"))
    except asyncio.TimeoutError:
        logger.warning(f"ffprobe 异步超时（{timeout}s）{_label}")
        return None
    except Exception as e:
        logger.warning(f"ffprobe 异步执行异常: {e}{_label}")
        return None

    streams = data.get("streams", [])
    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio_stream = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
    )
    fmt = data.get("format", {})

    info = {
        "resource_pix": "",
        "video_encode": "",
        "audio_encode": "",
        "fps": "",
        "duration": "",
        "bitrate": "",
        "width": "",
        "height": "",
        "is_hdr": False,
        "aspect_ratio": "",
        "resolution_name": "",
        "audio_channels": "",
    }

    if video_stream:
        height = int(video_stream.get("height", 0) or 0)
        width = int(video_stream.get("width", 0) or 0)
        info["width"] = str(width) if width else ""
        info["height"] = str(height) if height else ""
        info["resource_pix"] = _height_to_pix(height, width)

        codec = video_stream.get("codec_name", "")
        if codec:
            info["video_encode"] = _normalize_codec(codec)

        r_frame_rate = video_stream.get("r_frame_rate", "0/1")
        fps_val = _eval_frame_rate(r_frame_rate)
        if fps_val > 0:
            info["fps"] = f"{fps_val}FPS"

        # 视频码率
        v_bitrate = video_stream.get("bit_rate", "")
        if v_bitrate:
            info["v_bitrate"] = str(int(v_bitrate))

        # HDR 检测（依据像素格式）
        pix_fmt = video_stream.get("pix_fmt", "")
        if pix_fmt:
            info["is_hdr"] = is_hdr_format(pix_fmt)

        # 宽高比
        if width and height:
            info["aspect_ratio"] = calculate_standard_aspect_ratio(width, height)

    if audio_stream:
        codec = audio_stream.get("codec_name", "")
        if codec:
            info["audio_encode"] = _normalize_audio_codec(codec)

        channels = audio_stream.get("channels", 0)
        if channels:
            info["audio_channels"] = str(channels)

        a_bitrate = audio_stream.get("bit_rate", "")
        if a_bitrate:
            info["a_bitrate"] = str(int(a_bitrate))

    duration = fmt.get("duration", "")
    if duration:
        secs = parse_duration_to_seconds(duration)
        if secs:
            info["duration"] = str(secs)

    total_bitrate = fmt.get("bit_rate", "")
    if total_bitrate:
        info["bitrate"] = str(int(total_bitrate))
    elif video_stream:
        # 格式级比特率缺失时，按 4 种方法回退计算
        calc_bitrate = calculate_bitrate(video_stream, fmt)
        if calc_bitrate:
            info["bitrate"] = str(calc_bitrate)

    # 标准分辨率名检测
    if info["width"] and info["height"]:
        res_info = ResolutionDetector().detect_resolution(
            int(info["width"]), int(info["height"])
        )
        info["resolution_name"] = res_info.get("common_name", "")

    logger.debug(f"ffprobe 异步探测成功: {info}")
    return info


def _height_to_pix(height: int, width: int = 0) -> str:
    """将视频分辨率转换为标准分辨率标签（优先看宽度，宽银幕电影高度不够但宽度达标）"""
    # 优先按宽度判断（4K 宽银幕电影高度可能不到 2160）
    if width >= 3840 or height >= 2160:
        return "2160p"
    elif width >= 1920 or height >= 1080:
        return "1080p"
    elif width >= 1280 or height >= 720:
        return "720p"
    elif width >= 854 or height >= 480:
        return "480p"
    elif height > 0:
        return f"{height}p"
    return ""


def _normalize_codec(codec: str) -> str:
    """标准化视频编码名称"""
    codec_lower = codec.lower()
    mapping = {
        "hevc": "HEVC",
        "h265": "H265",
        "h266": "H266",
        "av1": "AV1",
        "av01": "AV1",
        "vp9": "VP9",
        "vp09": "VP9",
        "vp8": "VP8",
        "h264": "H264",
        "avc": "H264",
        "mpeg2": "MPEG2",
        "mpeg4": "MPEG4",
        "xvid": "XVID",
        "vc1": "VC1",
        "theora": "Theora",
    }
    return mapping.get(codec_lower, codec.upper())


def _normalize_audio_codec(codec: str) -> str:
    """标准化音频编码名称"""
    codec_lower = codec.lower()
    mapping = {
        "truehd": "TrueHD",
        "dts": "DTS",
        "dtshd": "DTS-HD",
        "dts-hd_ma": "DTS-HD.MA",
        "eac3": "EAC3",
        "ac3": "AC3",
        "aac": "AAC",
        "flac": "FLAC",
        "opus": "Opus",
        "vorbis": "Vorbis",
        "mp3": "MP3",
        "mp2": "MP2",
        "pcm_s16le": "LPCM",
        "pcm_s24le": "LPCM",
        "lpcm": "LPCM",
        "alac": "ALAC",
    }
    return mapping.get(codec_lower, codec.upper())


def _eval_frame_rate(rate_str: str) -> int:
    """解析 ffprobe 的帧率表达式（如 '60000/1001'）"""
    try:
        if "/" in rate_str:
            num, den = rate_str.split("/")
            num = int(num)
            den = int(den)
            if den == 0:
                return 0
            return round(num / den)
        return int(float(rate_str))
    except (ValueError, ZeroDivisionError):
        return 0


# =============================================================================
# 增强功能：HDR 检测 / 分辨率标准库 / 宽高比 / 比特率 / 时长解析
# 参考 qmediasync helpers/ffprobe.go 设计
# =============================================================================

# HDR 像素格式集合（10/12bit 高位深，常见于 HDR10/HDR10+/Dolby Vision 源）
_HDR_PIXEL_FORMATS = frozenset({
    "yuv420p10le", "yuv420p10be",
    "yuv422p10le", "yuv422p10be",
    "yuv444p10le", "yuv444p10be",
    "yuv420p12le", "yuv420p12be",
    "yuv422p12le", "yuv422p12be",
    "yuv444p12le", "yuv444p12be",
    "gbrp10le", "gbrp10be",
    "gbrp12le", "gbrp12be",
    "p010le", "p010be",
    "p012le", "p012be",
})


def is_hdr_format(pixel_format: str) -> bool:
    """检测像素格式是否为 HDR（高动态范围）格式。

    通过 ffprobe 的 pix_fmt 字段判断，10/12bit 高位深的 YUV/GBR/P010/P012
    格式通常对应 HDR10、HDR10+、Dolby Vision 等 HDR 内容。

    Args:
        pixel_format: ffprobe 返回的 pix_fmt 值，如 "yuv420p10le"

    Returns:
        True 表示为 HDR 像素格式
    """
    if not pixel_format:
        return False
    return pixel_format.lower() in _HDR_PIXEL_FORMATS


class ResolutionDetector:
    """标准分辨率检测器。

    维护从 8K UHD 到 144p 的完整标准分辨率表，支持精确匹配、容差匹配
    及基于宽度与宽高比的估算，参考 qmediasync helpers/ffprobe.go。
    """

    # 标准 16:9 分辨率表（由高到低）
    RESOLUTIONS = [
        {"standard_name": "4320p", "common_name": "8K UHD",  "width": 7680, "height": 4320, "is_standard": True},
        {"standard_name": "2160p", "common_name": "4K UHD",  "width": 3840, "height": 2160, "is_standard": True},
        {"standard_name": "1440p", "common_name": "2K QHD",  "width": 2560, "height": 1440, "is_standard": True},
        {"standard_name": "1080p", "common_name": "Full HD", "width": 1920, "height": 1080, "is_standard": True},
        {"standard_name": "720p",  "common_name": "HD",      "width": 1280, "height": 720,  "is_standard": True},
        {"standard_name": "576p",  "common_name": "SD PAL",  "width": 1024, "height": 576,  "is_standard": True},
        {"standard_name": "480p",  "common_name": "SD NTSC", "width": 854,  "height": 480,  "is_standard": True},
        {"standard_name": "360p",  "common_name": "nHD",     "width": 640,  "height": 360,  "is_standard": True},
        {"standard_name": "240p",  "common_name": "QVGA",    "width": 426,  "height": 240,  "is_standard": True},
        {"standard_name": "144p",  "common_name": "144p",    "width": 256,  "height": 144,  "is_standard": True},
    ]

    # 非 16:9 或变形分辨率别名（精确匹配时也可命中）
    _ALIASES = [
        {"standard_name": "576p",  "common_name": "SD PAL",  "width": 720,  "height": 576,  "is_standard": True},
        {"standard_name": "480p",  "common_name": "SD NTSC", "width": 720,  "height": 480,  "is_standard": True},
        {"standard_name": "240p",  "common_name": "QVGA",    "width": 320,  "height": 240,  "is_standard": True},
        {"standard_name": "1440p", "common_name": "2K QHD",  "width": 2048, "height": 1080, "is_standard": False},
        {"standard_name": "1080p", "common_name": "Full HD", "width": 1440, "height": 1080, "is_standard": False},
        {"standard_name": "1080p", "common_name": "Full HD", "width": 1920, "height": 800,  "is_standard": False},
        {"standard_name": "2160p", "common_name": "4K UHD",  "width": 3840, "height": 1600, "is_standard": False},
    ]

    # 容差（像素）
    TOLERANCE = 64

    def __init__(self):
        self._all_resolutions = list(self.RESOLUTIONS) + list(self._ALIASES)

    def _exact_match(self, width: int, height: int) -> Optional[dict]:
        for r in self._all_resolutions:
            if r["width"] == width and r["height"] == height:
                return r
        return None

    def _tolerance_match(self, width: int, height: int) -> Optional[dict]:
        """在容差范围内寻找最接近的标准分辨率。"""
        best = None
        best_diff = None
        for r in self._all_resolutions:
            dw = abs(r["width"] - width)
            dh = abs(r["height"] - height)
            if dw <= self.TOLERANCE and dh <= self.TOLERANCE:
                diff = dw + dh
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best = r
        return best

    def _estimate_by_width(self, width: int, height: int) -> Optional[dict]:
        """基于宽度与宽高比估算最接近的标准分辨率。"""
        if width <= 0 or height <= 0:
            return None
        try:
            input_ar = width / height
        except ZeroDivisionError:
            return None
        best = None
        best_score = None
        for r in self.RESOLUTIONS:
            try:
                std_ar = r["width"] / r["height"]
            except ZeroDivisionError:
                continue
            # 综合宽度差距与宽高比差距评分（宽高比差距放大权重）
            score = abs(r["width"] - width) + abs(std_ar - input_ar) * 100.0
            if best_score is None or score < best_score:
                best_score = score
                best = r
        return best

    def detect_resolution(self, width: int, height: int) -> dict:
        """检测给定宽高对应的标准分辨率。

        匹配顺序：
          1. 精确匹配宽高
          2. 容差匹配（64px 容差）
          3. 基于宽度与宽高比估算

        Returns:
            包含 standard_name / common_name / width / height / is_standard 的字典；
            无匹配时返回以高度命名的非标准结果。
        """
        if not width or not height:
            return {
                "standard_name": "",
                "common_name": "",
                "width": width,
                "height": height,
                "is_standard": False,
            }
        m = self._exact_match(width, height)
        if m:
            return dict(m)
        m = self._tolerance_match(width, height)
        if m:
            return dict(m)
        m = self._estimate_by_width(width, height)
        if m:
            return dict(m)
        return {
            "standard_name": f"{height}p",
            "common_name": f"{height}p",
            "width": width,
            "height": height,
            "is_standard": False,
        }


def get_resolution_level(width: int, height: int) -> str:
    """便捷函数：获取分辨率标准名（如 "2160p"）。"""
    return ResolutionDetector().detect_resolution(width, height).get("standard_name", "")


# 常见宽高比映射（比值 -> 标签），容差 0.05
_ASPECT_RATIOS = [
    (16.0 / 9.0,  "16:9"),
    (16.0 / 10.0, "16:10"),
    (4.0 / 3.0,   "4:3"),
    (21.0 / 9.0,  "21:9"),
    (18.0 / 9.0,  "18:9"),
    (3.0 / 2.0,   "3:2"),
    (1.0,         "1:1"),
]
_ASPECT_TOLERANCE = 0.05


def _gcd(a: int, b: int) -> int:
    """计算两个整数的最大公约数。"""
    a, b = abs(int(a)), abs(int(b))
    while b:
        a, b = b, a % b
    return a


def calculate_standard_aspect_ratio(width: int, height: int) -> str:
    """计算标准宽高比字符串。

    先在常见宽高比表中按容差 0.05 匹配；无匹配时用最大公约数(GCD)简化
    原始宽高（如 1920x1080 -> 16:9，1366x768 -> 683:384）。

    Args:
        width: 视频宽度
        height: 视频高度

    Returns:
        宽高比字符串，如 "16:9"；输入无效时返回空串
    """
    if not width or not height:
        return ""
    try:
        ratio = width / height
    except ZeroDivisionError:
        return ""
    for ref_ratio, label in _ASPECT_RATIOS:
        if abs(ratio - ref_ratio) <= _ASPECT_TOLERANCE:
            return label
    # 无匹配：用 GCD 简化
    g = _gcd(int(width), int(height))
    if g > 0:
        return f"{int(width) // g}:{int(height) // g}"
    return f"{width}:{height}"


def _estimate_bitrate_by_resolution(width: int, height: int) -> int:
    """根据分辨率估算典型码率（bps），作为最后回退。"""
    typical = [
        (7680, 25_000_000),  # 8K
        (3840, 15_000_000),  # 4K
        (2560, 8_000_000),   # 1440p
        (1920, 5_000_000),   # 1080p
        (1280, 2_500_000),   # 720p
        (854,  1_500_000),   # 480p
        (0,    800_000),     # 更低
    ]
    for ref_w, rate in typical:
        if width >= ref_w:
            return rate
    return 800_000


def calculate_bitrate(video_stream: Optional[dict], fmt: Optional[dict]) -> int:
    """计算视频比特率（bps），按优先级 4 种方法回退。

    1. 视频流比特率（video_stream.bit_rate）
    2. 格式总比特率（format.bit_rate）
    3. 文件大小 / 时长计算
    4. 帧数 / 帧率估算时长后结合文件大小，最后回退分辨率典型值

    Args:
        video_stream: ffprobe 的视频流字典
        fmt: ffprobe 的 format 字典

    Returns:
        比特率（bps），无法估算返回 0
    """
    video_stream = video_stream or {}
    fmt = fmt or {}

    # 1. 流比特率
    v_bit = video_stream.get("bit_rate")
    if v_bit:
        try:
            val = int(v_bit)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass

    # 2. 格式总比特率
    f_bit = fmt.get("bit_rate")
    if f_bit:
        try:
            val = int(f_bit)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass

    size = fmt.get("size")
    duration = fmt.get("duration")

    # 3. 文件大小 / 时长
    if size and duration:
        try:
            dur = float(duration)
            if dur > 0:
                return int(float(size) * 8.0 / dur)
        except (ValueError, TypeError):
            pass

    # 4. 帧数 / 帧率估算时长，结合文件大小
    nb_frames = video_stream.get("nb_frames")
    avg_fps = video_stream.get("avg_frame_rate", "0/1")
    if nb_frames and avg_fps:
        fps = _eval_frame_rate(avg_fps)
        if fps > 0:
            try:
                dur = int(nb_frames) / fps
                if dur > 0 and size:
                    return int(float(size) * 8.0 / dur)
            except (ValueError, TypeError, ZeroDivisionError):
                pass

    # 最后回退：分辨率典型码率
    width = int(video_stream.get("width", 0) or 0)
    height = int(video_stream.get("height", 0) or 0)
    if width or height:
        return _estimate_bitrate_by_resolution(width, height)
    return 0


def parse_duration_to_seconds(duration_str) -> int:
    """将时长字符串解析为整秒数。

    支持以下格式：
      - 秒（浮点数）：如 "123.45" -> 123
      - HH:MM:SS：如 "01:02:03" -> 3723
      - HH:MM:SS.ms：如 "01:02:03.456" -> 3723
      - MM:SS：如 "02:03" -> 123

    Args:
        duration_str: 时长字符串

    Returns:
        整秒数，解析失败返回 0
    """
    if duration_str is None:
        return 0
    s = str(duration_str).strip()
    if not s:
        return 0

    # 纯数字（秒，可能带小数）
    if ":" not in s:
        try:
            return int(float(s))
        except ValueError:
            return 0

    # 冒号分隔
    parts = s.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0

    if len(nums) == 3:
        h, m, sec = nums
        return int(h * 3600 + m * 60 + sec)
    if len(nums) == 2:
        m, sec = nums
        return int(m * 60 + sec)
    if len(nums) == 1:
        return int(nums[0])
    return 0
