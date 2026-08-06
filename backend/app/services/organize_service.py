"""
整理服务 - 扫描 115 网盘目录，通过 TMDB 识别影视信息，检测重复，移动分类
支持基于 TMDB 元数据的二级分类（YAML 配置），整理结果保存到整理后目录
"""
import re
import os
import shutil
import time as _time
import threading
from typing import Optional
from collections import defaultdict, Counter

from app.services.client_115 import Client115Service
from app.services.tmdb_service import TmdbService
from app.services.category_helper import CategoryHelper
from app.services.media_probe import probe_media_info_async, is_ffprobe_available
from app.core.logbuffer import get_logger
from app.core.db_helper import get_api_intervals
from app.core.json_storage import read_setting, save_setting

# #18: Jinja2 可用性标志（不可用时优雅降级到现有模板引擎）
try:
    import jinja2 as _jinja2
    JINJA2_AVAILABLE = True
except ImportError:  # pragma: no cover - jinja2 缺失时的降级路径
    _jinja2 = None
    JINJA2_AVAILABLE = False


def _organize_write_interval() -> float:
    """整理写操作间隔（跟随用户配置的 API 请求间隔）"""
    return max(get_api_intervals().get("download_url_interval", 3.0), 0.3)


def _organize_retry_cooldown() -> float:
    """整理重试冷却时间（跟随用户配置的冷却时间）"""
    return max(get_api_intervals().get("retry_cooldown", 30.0), 1.0)


def _get_organize_method() -> str:
    """读取整理文件方式配置（move/copy/hardlink/softlink），默认 move。

    从 read_setting("organize_dirs") 读取 organize_method 字段，
    非法值回退为 "move"。
    """
    try:
        cfg = read_setting("organize_dirs")
        method = cfg.get("organize_method", "move")
        if method in ("move", "copy", "hardlink", "softlink"):
            return method
    except Exception:
        pass
    return "move"


def _get_overwrite_policy() -> str:
    """读取整理覆盖策略配置（#34）。

    从 read_setting("organize_dirs") 读取 overwrite_policy 字段：
    - "skip": 跳过已存在的同名文件（默认）
    - "replace": 覆盖（先删后移）
    - "rename": 自动重命名（如 movie(1).mkv）
    非法值回退为 "skip"。
    """
    try:
        cfg = read_setting("organize_dirs")
        policy = cfg.get("overwrite_policy", "skip")
        if policy in ("skip", "replace", "rename"):
            return policy
    except Exception:
        pass
    return "skip"


def _find_target_file_id(cookies: str, target_cid: str, filename: str) -> Optional[str]:
    """列出目标目录文件，返回同名文件的 file_id（无则 None）。

    分页遍历 Client115Service.list_files 结果，fid 为空表示子目录予以跳过。
    """
    if not target_cid or not filename:
        return None
    offset = 0
    limit = 100
    while True:
        try:
            resp = Client115Service.list_files(cookies, target_cid, offset=offset, limit=limit)
        except Exception as e:
            logger.warning(f"[organize] 列出目标目录文件失败 cid={target_cid}: {e}")
            return None
        if not isinstance(resp, dict) or resp.get("_error"):
            return None
        items = resp.get("data", []) or []
        for it in items:
            # fid 为空表示子目录，跳过
            if it.get("fid") and it.get("n", "") == filename:
                return it.get("fid")
        if len(items) < limit:
            return None
        offset += limit


def check_target_exists(cookies: str, target_cid: str, filename: str) -> bool:
    """检查目标目录中是否已存在同名文件（#34）。

    使用 Client115Service.list_files 列出目标目录文件，分页遍历检查是否有同名项。
    """
    return _find_target_file_id(cookies, target_cid, filename) is not None


def generate_unique_filename(cookies: str, target_cid: str, filename: str) -> str:
    """当策略为 "rename" 时，在文件名后添加序号生成唯一文件名（#34）。

    如 movie.mkv -> movie(1).mkv -> movie(2).mkv，直至不与目标目录现有文件冲突。
    """
    if not filename:
        return filename
    # 拆分扩展名
    if "." in filename:
        base, ext = filename.rsplit(".", 1)
        ext = "." + ext
    else:
        base, ext = filename, ""
    # 收集目标目录已有文件名集合
    existing_names: set = set()
    offset = 0
    limit = 100
    while True:
        try:
            resp = Client115Service.list_files(cookies, target_cid, offset=offset, limit=limit)
        except Exception as e:
            logger.warning(f"[organize] 生成唯一文件名失败 cid={target_cid}: {e}")
            break
        if not isinstance(resp, dict) or resp.get("_error"):
            break
        items = resp.get("data", []) or []
        for it in items:
            if it.get("n"):
                existing_names.add(it["n"])
        if len(items) < limit:
            break
        offset += limit
    # 依次尝试 movie(1).mkv、movie(2).mkv ...
    idx = 1
    while True:
        candidate = f"{base}({idx}){ext}"
        if candidate not in existing_names:
            return candidate
        idx += 1
        # 安全上限，避免极端死循环
        if idx > 9999:
            return candidate


def _organize_file(src: str, dst: str, method: str = "move") -> None:
    """按指定方式整理本地文件（移动/复制/硬链接/软链接）。

    用于整理场景下本地 STRM/媒体文件的迁移：
    - move: shutil.move（现有行为，移动后源文件不存在）
    - copy: shutil.copy2（复制并保留元数据，源文件保留）
    - hardlink: os.link 创建硬链接，失败（跨设备）降级为 copy2
    - softlink: os.symlink 创建软链接，失败降级为 copy2

    Args:
        src: 源文件路径
        dst: 目标文件路径
        method: 整理方式（move/copy/hardlink/softlink）
    """
    if method == "copy":
        shutil.copy2(src, dst)
    elif method == "hardlink":
        try:
            os.link(src, dst)
        except OSError:
            # 跨设备或权限不足，降级为复制
            shutil.copy2(src, dst)
    elif method == "softlink":
        try:
            os.symlink(src, dst)
        except OSError:
            # 跨设备或权限不足，降级为复制
            shutil.copy2(src, dst)
    else:
        # move（默认行为）
        shutil.move(src, dst)

logger = get_logger("app.services.organize_service")

# 视频扩展名
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".m2ts", ".rmvb", ".iso"}

# 冗余关键词（小写匹配）
REDUNDANT_KEYWORDS = {"sample", "预告", "花絮", "番外", "特典", "menu", "extras", "bonus", "trailer", "featurette"}

# 预编译冗余关键词正则：一次性匹配所有关键词（比逐个 `in` 检查更高效）
_REDUNDANT_PATTERN = re.compile(
    "|".join(re.escape(kw) for kw in REDUNDANT_KEYWORDS),
    re.IGNORECASE,
)


def extract_av_code(name: str) -> Optional[str]:
    """
    从文件名中提取 AV 番号。
    支持格式: SSIS-123, ssis123, SSIS_123, SSIS123, abc-001 等。
    支持字母后缀: SNOS-161-C, ABC-001-A 等。
    能正确处理网站水印前缀，如 hhd800.com@MARR-012 → MARR-012。
    返回标准化番号（如 "SSIS-123"），未匹配返回 None。
    """
    # 去扩展名
    base = re.sub(r'\.[^.]+$', '', name)
    # 去除网站水印前缀（如 hhd800.com@, hkbisi.com@, 2048.xxx@ 等）
    # 格式：域名/数字.域名@
    base = re.sub(r'^[A-Za-z0-9]+\.[A-Za-z]{2,}@', '', base)
    # 去除常见资源标记，避免干扰（用空格替换而非删除，保留词边界）
    base = re.sub(r'[._\s]?(1080p|720p|480p|2160p|4k|bluray|webrip|web-dl|h264|h265|x264|x265|hevc|aac|dts|hdr|atmos|remux|fps)[._\s]?', ' ', base, flags=re.IGNORECASE)

    # 番号候选列表：收集所有匹配项，选择最可能的番号
    # 番号格式：2-5个字母 + 可选分隔符(-_) + 2-5位数字 + 可选字母后缀(-A, -C, _AB 等)
    # 字母后缀常见于分盘/分集标识，如 SNOS-161-C, ABC-001-A
    candidates = []
    for m in re.finditer(r'\b([A-Za-z]{2,5})[-_]?(\d{2,5})(?:[-_]([A-Za-z]{1,3}))?\b', base):
        prefix = m.group(1).upper()
        number = m.group(2)
        suffix = m.group(3)  # 字母后缀，如 C, A, AB
        # 排除一些非番号的误匹配
        if prefix.lower() in ('ep', 'fps', 'disc', 'vol', 'cd', 'dvd', 'bd', 'hhd'):
            continue
        # 补零：数字部分不足3位时补零（如 ABC-1 → ABC-001）
        number_padded = number.zfill(3)
        code = f"{prefix}-{number_padded}"
        if suffix:
            code = f"{code}-{suffix.upper()}"
        # 优先选择带分隔符(-_)的匹配（更可能是番号）
        has_sep = bool(re.search(rf'\b{re.escape(m.group(1))}[-_]{re.escape(m.group(2))}\b', base, re.IGNORECASE))
        candidates.append((code, has_sep, m.start()))

    if candidates:
        # 优先选择带分隔符的，其次选择最先出现的
        candidates.sort(key=lambda x: (not x[1], x[2]))
        return candidates[0][0]
    return None


def auto_classify(name: str) -> str:
    """
    自动分类：通过文件名/目录名识别电影、电视剧或 AV
    返回: "movie" / "tvshow" / "av" / "unknown"
    """
    lower = name.lower()
    # 剧集标记：S01E01, S01, 第N集, 第N季, Season 1, EP01 等
    tv_patterns = [
        r'[sS]\d{1,2}[eE]\d{1,3}',           # S01E01
        r'(?:^|[._\s])[sS]\d{1,2}(?![eE]\d)(?:[._\s]|$)',  # .S01.（仅季号，无集号）
        r'第\d{1,3}集',                       # 第1集
        r'第\d{1,3}话',                       # 第1话
        r'第\d{1,2}季',                       # 第1季
        r'[eE][pP]\d{1,3}',                   # EP01
        r'[sS]eason\s*\d{1,2}',              # Season 1
        r' season\s*\d{1,2}',                #  season 1
        r'\d{1,2}x\d{1,3}',                   # 1x01
    ]
    for pat in tv_patterns:
        if re.search(pat, name):
            return "tvshow"
    # 有年份的视为电影
    if re.search(r'[（(]\s*(19|20)\d{2}\s*[）)]', name):
        return "movie"
    if re.search(r'\b(19|20)\d{2}\b', lower):
        return "movie"
    # 检查 AV 番号
    if extract_av_code(name):
        return "av"
    return "unknown"


def is_redundant(name: str) -> bool:
    """判断文件是否为冗余文件（sample、预告等）
    使用预编译正则一次性匹配所有关键词，避免逐个字符串搜索。
    """
    return bool(_REDUNDANT_PATTERN.search(name))


def extract_title(name: str) -> str:
    """
    从文件名中提取标题（去除扩展名、分辨率、编码等标记）
    用于重复检测。
    - AV 文件以番号作为去重 key
    - 剧集（含 SxxExx）保留季集信息作为去重 key，避免不同集被误判为重复
    - 电影用纯标题去重
    """
    # AV 文件：用番号作为去重 key
    av_code = extract_av_code(name)
    if av_code:
        return av_code.lower()
    # 检测是否为剧集（含 SxxExx 季集信息）
    season_episode = re.search(r'[sS]\d{1,2}[eE]\d{1,3}', name)
    # 去扩展名
    base = re.sub(r'\.[^.]+$', '', name)
    # 去方括号内容（发布组、集号、技术信息、字幕信息等）
    base = re.sub(r'\[[^\]]*\]', ' ', base)
    # 去分辨率/编码标记（用空格替换避免粘连）
    base = re.sub(r'[._\s]?(1080i|1080p|720i|720p|480i|480p|2160i|2160p|4k|bluray|blu-ray|webrip|web-dl|web|hd|h264|h265|x264|x265|hevc|aac|dts|hdr|atmos|remux|srt|ass|ssa|cr|chs|cht|big5|hevc)[._\s]?', ' ', base, flags=re.IGNORECASE)
    if season_episode:
        # 剧集：保留 SxxExx 作为去重 key 的一部分，避免不同集被误判为重复
        # 只去掉 SxxExx 前后的分隔符，保留本身
        pass
    else:
        # 电影：去 SxxExx（一般没有，保险起见）
        base = re.sub(r'[._\s]?[sS]\d{1,2}[eE]\d{1,3}[._\s]?', ' ', base)
        # 去单独的 Sxx（季号，无集号）
        base = re.sub(r'(?:^|[._\s])[sS]\d{1,2}(?![eE]\d)(?:[._\s]|$)', ' ', base)
        # 去 Season N（英文季号写法）
        base = re.sub(r'[._\s]?[sS]eason\s*\d{1,2}[._\s]?', ' ', base)
    # 去年份
    base = re.sub(r'[（(]?\s*(19|20)\d{2}\s*[）)]?', ' ', base)
    # 去全角标点
    base = re.sub(r'[？！：；]', '', base)
    # 去网站水印
    base = re.sub(r'BT[\u4e00-\u9fff]*网?', '', base, flags=re.IGNORECASE)
    # 统一小写、去空格和分隔符
    base = re.sub(r'[._\-\s]', '', base).lower()
    return base


def extract_season_episode(name: str) -> tuple[Optional[int], Optional[int]]:
    """
    从文件名中提取季号和集号。
    支持 S01E01, s01e01, 1x01, 第N集, 第N话, EP01, Season N [NN] 等格式。
    返回 (season, episode)，未找到则为 None。
    """
    # S01E01 / s01e01
    m = re.search(r'[sS](\d{1,2})[eE](\d{1,3})', name)
    if m:
        return int(m.group(1)), int(m.group(2))
    # 1x01
    m = re.search(r'(\d{1,2})x(\d{1,3})', name)
    if m:
        return int(m.group(1)), int(m.group(2))
    # 第N集 / 第N话
    m = re.search(r'第(\d{1,3})[集话]', name)
    if m:
        return 1, int(m.group(1))
    # EP01 / ep01
    m = re.search(r'[eE][pP](\d{1,3})', name)
    if m:
        return 1, int(m.group(1))
    # Season N 或 Sxx + [NN] 格式（如 "Season 2 [01]"、"S2 [11]"）
    # 先提取 Season N 中的季号
    season_match = re.search(r'[sS]eason\s*(\d{1,2})', name)
    if not season_match:
        # 也检查 Sxx 格式（如 S2，仅季号无集号）
        season_match = re.search(r'(?:^|[._\s])[sS](\d{1,2})(?![eE]\d)(?:[._\s]|$)', name)
    season_num = int(season_match.group(1)) if season_match else None
    # 再提取 [NN] 中的集号（方括号内仅含 1~3 位数字）
    episode_match = re.search(r'\[(\d{1,3})\]', name)
    episode_num = int(episode_match.group(1)) if episode_match else None
    if season_num is not None or episode_num is not None:
        return season_num or 1, episode_num
    return None, None


# #32: 集数标记模式列表 (编译正则, 模式标签) —— 顺序优先，先匹配更具体的模式
_EP_MARK_PATTERNS = [
    (re.compile(r'[sS](\d{1,2})[eE](\d{1,3})'), "SxxExx"),   # S01E01
    (re.compile(r'(\d{1,2})[xX](\d{1,3})'), "xExx"),           # 1x01
    (re.compile(r'[eE][pP](\d{1,3})'), "EPxx"),               # EP01
    (re.compile(r'第(\d{1,3})[集话]'), "CNEp"),                 # 第N集
    (re.compile(r'\[(\d{1,3})\]'), "Bracket"),                 # [01]
    (re.compile(r'(?<![0-9A-Za-z])[eE](\d{1,3})(?![0-9A-Za-z])'), "Exx"),  # E01
]


def _replace_episode_token(name: str, match, new_token: str) -> str:
    """将文件名中已匹配到的集数标记替换为 new_token，保留其余部分与扩展名。"""
    if not match:
        return name
    return name[:match.start()] + new_token + name[match.end():]


def align_episode_names(video_files_info: list) -> dict:
    """对齐同一季剧集文件的命名模式（#32）。

    分析文件名中的集数标记（SxxExx、EPxx、Exx、第N集、[N] 等），
    检测主要命名模式，为不符合 SxxExx 规范模式的文件生成对齐后的名称。
    统一对齐到系统标准的 SxxExx 格式，保留各文件原有分隔符。

    Args:
        video_files_info: 同一季的剧集文件信息列表，每项至少含 "name"（文件名），
                          可选 "season"（季号，用于无 SxxExx 标记时回退）

    Returns:
        {"original_name": "aligned_name", ...} 需要对齐的重命名映射；
        无需调整时返回空 dict。
    """
    if not video_files_info or len(video_files_info) < 2:
        return {}

    # 解析每个文件名：(name, 模式标签, 集号, 匹配对象)
    parsed = []
    for info in video_files_info:
        name = (info.get("name") if isinstance(info, dict) else "") or ""
        label = None
        ep_num = None
        match = None
        for pat, lbl in _EP_MARK_PATTERNS:
            m = pat.search(name)
            if m:
                try:
                    ep_num = int(m.groups()[-1])
                except (ValueError, TypeError):
                    ep_num = None
                label = lbl
                match = m
                break
        parsed.append({"name": name, "label": label, "ep": ep_num, "match": match})

    # 至少需要两个带集号的文件才能检测模式
    with_ep = [p for p in parsed if p["ep"] is not None]
    if len(with_ep) < 2:
        return {}

    # 检测主要命名模式（多数表决，用于日志与参考）
    label_counts = Counter(p["label"] for p in with_ep if p["label"])
    dominant_label = label_counts.most_common(1)[0][0] if label_counts else None
    logger.info(f"[organize] #32 命名对齐: 检测到主要命名模式 = {dominant_label}")

    # 确定目标季号：优先从 SxxExx 文件提取，其次 info["season"]，默认 "1"
    season_str = None
    ep_width = 2
    for p in with_ep:
        sm = re.search(r'[sS](\d{1,2})[eE](\d{1,3})', p["name"])
        if sm:
            season_str = sm.group(1)
            ep_width = max(len(sm.group(2)), 2)
            break
    if season_str is None:
        for info in video_files_info:
            s = info.get("season") if isinstance(info, dict) else None
            if s is not None:
                try:
                    # 零填充至 2 位，与标准 SxxExx 格式（如 S02E01）保持一致
                    season_str = str(int(s)).zfill(2)
                except (ValueError, TypeError):
                    season_str = str(s)
                break
    if season_str is None:
        season_str = "1"

    result = {}
    for p in parsed:
        if p["ep"] is None:
            continue
        # 已符合 SxxExx 规范模式则跳过
        if p["label"] == "SxxExx":
            continue
        aligned_token = f"S{season_str}E{p['ep']:0{ep_width}d}"
        new_name = _replace_episode_token(p["name"], p["match"], aligned_token)
        if new_name and new_name != p["name"]:
            result[p["name"]] = new_name
    return result


def extract_season_only(name: str) -> Optional[int]:
    """从文件名/目录名中提取季号（仅 Season N / 第N季 格式）"""
    m = re.search(r'[sS]eason\s*(\d{1,2})', name)
    if m:
        return int(m.group(1))
    m = re.search(r'第(\d{1,2})季', name)
    if m:
        return int(m.group(1))
    return None


def extract_disc_num(name: str) -> Optional[str]:
    """从文件名中提取盘号"""
    m = re.search(r'[dD]isc\s*(\d{1,2})', name)
    if m:
        return m.group(1)
    m = re.search(r'[dD](\d{1,2})\b', name)
    if m:
        return m.group(1)
    return None


def parse_resource_info(filename: str, media_info: Optional[dict] = None, prefer_filename: bool = False) -> dict:
    """
    从文件名中解析资源信息（分辨率、编码、发布组等）。

    Args:
        filename: 文件名
        media_info: ffprobe 探测结果
        prefer_filename: True=文件名优先，仅补充文件名缺失的字段；False=ffprobe 优先，探测值覆盖文件名值
    """
    info = {
        "resource_pix": "",
        "resource_version": "",
        "resource_source": "",
        "resource_type": "",
        "resource_effect": "",
        "video_encode": "",
        "audio_encode": "",
        "resource_team": "",
        "fps": "",
        "season_episode": "",
        "season_num": "",
        "episode_num": "",
        "disc_num": "",
        "ext": "",
        "original_name": filename,
    }

    # 扩展名
    ext_match = re.search(r'(\.[^.]+)$', filename)
    if ext_match:
        info["ext"] = ext_match.group(1)

    # 去扩展名的基名
    base = re.sub(r'\.[^.]+$', '', filename)
    # 去除方括号（替换为空格），使方括号内的资源信息可被正则匹配
    # 如 [CR-WebRip 1080p HEVC AAC SRT] →  CR-WebRip 1080p HEVC AAC SRT
    base = re.sub(r'[\[\]]', ' ', base)
    # 末尾补一个空格作为分隔符，确保文件名末尾的资源字段也能被正则匹配
    # （如 "蜘蛛侠.1080p.x265.AAC" 去扩展名后 AAC 在末尾，无尾部分隔符）
    base += ' '

    # 分辨率 resource_pix（含隔行扫描 1080i/720i/480i/2160i）
    pix_match = re.search(r'[._\s](2160p|1080p|720p|480p|4k|4K|2160i|1080i|720i|480i)[._\s]', base, re.IGNORECASE)
    if pix_match:
        pix = pix_match.group(1)
        if pix.lower() == "4k":
            pix = "2160p"
        info["resource_pix"] = pix

    # 资源版本 resource_version (IMAX, HQ, 3D, CC, DC, REMUX 等)
    version_match = re.search(r'[._\s](IMAX|HQ|3D|CC|DC|REMUX)[._\s]', base, re.IGNORECASE)
    if version_match:
        info["resource_version"] = version_match.group(1).upper()

    # 资源来源 resource_source (NF, DSNP, AMZN, HMAX, CR 等)
    # 注意：MA 已移除，因为它会误匹配 DTS-HD.MA 中的 MA
    source_match = re.search(r'[._\s-]((?:[A-Z]{2,4}\.)?(?:UHD|NF|DSNP|AMZN|HMAX|ATVP|PCOK|STAN|HULU|VHX|CR))[._\s-]', base)
    if source_match:
        info["resource_source"] = source_match.group(1)

    # 资源质量 resource_type (BluRay, WEB-DL, WEBRip, HDTV, DVD, Remux 等)
    # 分隔符含 - 以匹配 CR-WebRip 格式
    type_match = re.search(r'[._\s-](BluRay|Blu-Ray|BDRip|BRRip|WEB-DL|WEBRip|WEB|HDTV|DVD|DVDRip|Remux|UHD)[._\s-]', base, re.IGNORECASE)
    if type_match:
        rtype = type_match.group(1)
        rtype = rtype.replace("Blu-Ray", "BluRay")
        if rtype.lower() == "web":
            rtype = "WEB-DL"
        # 统一大小写为标准写法
        rtype_lower = rtype.lower()
        type_canonical = {
            "bluray": "BluRay", "bdrip": "BDRip", "brrip": "BRRip",
            "web-dl": "WEB-DL", "webrip": "WEBRip", "webdl": "WEB-DL",
            "hdtv": "HDTV", "dvd": "DVD", "dvdrip": "DVDRip",
            "remux": "Remux", "uhd": "UHD",
        }
        rtype = type_canonical.get(rtype_lower, rtype)
        info["resource_type"] = rtype

    # 特效 resource_effect (DV, HDR, HDR10, HDR10+, SDR, Dolby Vision 等)
    effect_match = re.search(r'[._\s](DV\.HDR|DV|HDR10\+|HDR10|HDR|SDR|Dolby\.Vision)[._\s]', base, re.IGNORECASE)
    if effect_match:
        info["resource_effect"] = effect_match.group(1).upper().replace("DOLBY.VISION", "DV")

    # 视频编码 video_encode (H.264, H.265, x264, x265, HEVC, AV1, VC1, MPEG2 等)
    # 注意：尾部分隔符含 - 以匹配 "x265-NeoNoir" 格式（编码器后紧跟发布组）
    venc_match = re.search(r'[._\s]((?:H\.?26[45]|x26[45]|HEVC|AV1|VC1|MPEG2|MPEG-2))(?:[._\s](?:10bit|8bit|12bit))?(?:[._\s]|-)', base, re.IGNORECASE)
    if venc_match:
        enc = venc_match.group(1)
        enc = enc.replace("H.265", "H265").replace("H.264", "H264")
        enc = enc.replace("MPEG-2", "MPEG2")
        # 追加 10bit/8bit 后缀：先尝试编码器紧邻位置，再全局搜索
        bit_match = re.search(r'(?:10bit|8bit|12bit)', base[venc_match.end():venc_match.end()+10], re.IGNORECASE)
        if not bit_match:
            # 全局搜索（10Bit 可能不在编码器紧邻位置，如 "10Bit.DDP5.1.x265"）
            bit_match = re.search(r'[._\s](10bit|8bit|12bit)[._\s]', base, re.IGNORECASE)
        if bit_match:
            enc = f"{enc}.{bit_match.group(1).lower()}"
        info["video_encode"] = enc

    # 音频编码 audio_encode (TrueHD, DTS-HD.MA, DTS, EAC3, AC3, AAC, FLAC 等)
    # 注意：不捕获通道数（5.1, 7.1），通道数不是编码名称的一部分
    # 支持 DTS5.1（无分隔符）和 DTS.5.1（有分隔符）两种写法
    aenc_match = re.search(r'[._\s]((?:TrueHD|DTS-HD\.MA|DTS-HD|DTS-X|DTS|EAC3|DDP|DD\+|E-AC-3|AC3|AAC|FLAC|LPCM|Atmos|DD))(?:[._\s]?\d\.\d)?[._\s]', base, re.IGNORECASE)
    if aenc_match:
        enc = aenc_match.group(1)
        # 标准化：统一大小写，DDP/DD+ 与 EAC3 是同一编码，统一为 EAC3
        enc_lower = enc.lower()
        if enc_lower in ("dd+", "ddp", "e-ac-3"):
            enc = "EAC3"
        info["audio_encode"] = enc

    # 发布组 resource_team (最后的 -XXX 部分)
    # 排除纯数字（如 MARR-012 中的 012 不是发布组）
    # 排除已被 AV 番号占用的后缀（如 SNOS-161-C 中的 C 不是发布组）
    av_code = extract_av_code(filename)
    team_match = re.search(r'-([A-Za-z0-9]+)\s*$', base)
    if team_match:
        team_candidate = team_match.group(1)
        if team_candidate.isdigit():
            # 纯数字，不是发布组
            pass
        elif av_code and av_code.endswith(f"-{team_candidate.upper()}"):
            # 后缀与 AV 番号的字母后缀相同，不是发布组
            pass
        else:
            info["resource_team"] = team_candidate

    # 帧率 fps
    fps_match = re.search(r'[._\s](\d{2,3})(?:FPS|fps)[._\s]', base)
    if fps_match:
        info["fps"] = f"{fps_match.group(1)}FPS"

    # 季集信息
    s, e = extract_season_episode(filename)
    if s is not None:
        info["season_num"] = str(s)
        info["season_episode"] = f"S{s:02d}"
    if e is not None:
        info["episode_num"] = str(e)
        if s is not None:
            info["season_episode"] = f"S{s:02d}E{e:02d}"

    # 盘号
    disc = extract_disc_num(filename)
    if disc:
        info["disc_num"] = disc

    # ffprobe 探测结果合并
    # ffprobe 能检测的字段：resource_pix, video_encode, audio_encode, fps, is_hdr
    if media_info:
        probe_fields = ["resource_pix", "video_encode", "audio_encode", "fps"]
        for field in probe_fields:
            probe_val = media_info.get(field, "")
            if not probe_val:
                continue

            old_val = info.get(field, "")

            if prefer_filename:
                # 文件名优先：仅补充文件名缺失的字段，不覆盖已有值
                if not old_val:
                    logger.debug(f"ffprobe 补充 {field}={probe_val} (文件名无此信息)")
                    info[field] = probe_val
            else:
                # ffprobe 优先：探测值覆盖文件名值
                # 特殊处理 video_encode：保留文件名中的位深后缀（10bit/8bit）
                # 例如：文件名 x265.10bit → ffprobe HEVC → 最终 HEVC.10bit
                if field == "video_encode" and old_val and probe_val:
                    bit_suffix = re.search(r'\.(10bit|8bit|12bit)$', old_val, re.IGNORECASE)
                    if bit_suffix and not re.search(r'\.(10bit|8bit|12bit)$', probe_val, re.IGNORECASE):
                        probe_val = f"{probe_val}.{bit_suffix.group(1).lower()}"
                if old_val and old_val != probe_val:
                    logger.info(f"ffprobe 覆盖 {field}: 文件名='{old_val}' → ffprobe='{probe_val}'")
                elif not old_val:
                    logger.debug(f"ffprobe 补充 {field}={probe_val} (文件名无此信息)")
                info[field] = probe_val

        # ffprobe HDR 检测 → resource_effect
        # ffprobe 通过像素格式检测 10/12bit 高位深，对应 HDR10/Dolby Vision
        if media_info.get("is_hdr") and not info.get("resource_effect"):
            info["resource_effect"] = "HDR"
            logger.debug("ffprobe 补充 resource_effect=HDR (检测到高位深像素格式)")

    return info


# ==================== 洗版策略（YAML 格式） ====================

def parse_wash_strategies(wash_yaml: str) -> list:
    """
    解析 YAML 格式的洗版策略配置，返回策略列表。
    每个策略是一个 dict，包含:
      - _name: 策略别名
      - mode: coexist/skip/replace/max_size/min_size
      - scope: all/group（默认 all）
      - media_type: movie/tv（可选，不设则匹配所有）
      - category: 分类名（可选，逗号分隔，不设则匹配所有）
      - priority_level: 优先级匹配规则列表
    """
    if not wash_yaml or not wash_yaml.strip():
        return []
    try:
        import yaml
        config = yaml.safe_load(wash_yaml)
    except Exception as e:
        logger.warning(f"洗版策略 YAML 解析失败: {e}")
        return []
    if not config or not isinstance(config, dict):
        return []
    strategies = []
    for name, settings in config.items():
        if not isinstance(settings, dict):
            continue
        s = dict(settings)
        s["_name"] = name
        s.setdefault("mode", "replace")
        s.setdefault("scope", "all")
        strategies.append(s)
    return strategies


def _match_priority_rule(file_info: dict, rule: dict) -> bool:
    """
    检查文件是否匹配某条优先级规则。
    规则中每个字段的值支持:
      - 逗号分隔多值: "2160p,4k" 表示任一匹配即可 (OR)
      - ! 前缀: "!DV" 表示排除该值
    所有字段需同时满足 (AND)。
    """
    if not isinstance(rule, dict) or not rule:
        return True  # 空规则 = 匹配所有

    for field, pattern in rule.items():
        file_val = str(file_info.get(field, "")).lower()
        patterns = [p.strip() for p in str(pattern).split(",")]

        has_inclusion = False
        inclusion_matched = False
        excluded = False

        for p in patterns:
            p = p.strip()
            if not p:
                continue
            if p.startswith("!"):
                # 排除规则：如果值包含被排除的内容，则不匹配
                if p[1:].lower() in file_val:
                    excluded = True
                    break
            else:
                has_inclusion = True
                if p.lower() in file_val:
                    inclusion_matched = True

        if excluded:
            return False
        if has_inclusion and not inclusion_matched:
            return False

    return True


def _get_wash_priority_rank(file_info: dict, priority_levels: list) -> int:
    """
    获取文件在优先级规则列表中的排名（越小越优先）。
    未匹配任何规则返回 len(priority_levels)（最低优先级）。
    无规则时返回 0（所有文件同等优先级）。
    """
    if not priority_levels:
        return 0
    for i, rule in enumerate(priority_levels):
        if _match_priority_rule(file_info, rule):
            return i
    return len(priority_levels)


def _find_matching_strategy(file_info: dict, media_type: str, category: str, strategies: list) -> Optional[dict]:
    """
    从策略列表中找到匹配文件的策略。
    匹配顺序:
      1. 先找 media_type 和 category 都匹配、且 priority_level 匹配的策略
      2. 再找 media_type 匹配、无 priority_level 的兜底策略
      3. 再找无 media_type 的通用策略
    返回 None 表示无匹配策略（不洗版）。
    """
    # 第一轮：找有 priority_level 且匹配的策略
    for s in strategies:
        s_media_type = s.get("media_type", "")
        if s_media_type and s_media_type != media_type:
            continue
        s_category = s.get("category", "")
        if s_category:
            cats = [c.strip() for c in s_category.split(",")]
            if category and category not in cats:
                continue
        priority_levels = s.get("priority_level", [])
        if not priority_levels:
            continue  # 跳过兜底策略，第一轮只找有规则的
        if _get_wash_priority_rank(file_info, priority_levels) < len(priority_levels):
            return s

    # 第二轮：找兜底策略（有 media_type 但无 priority_level）
    for s in strategies:
        s_media_type = s.get("media_type", "")
        if s_media_type and s_media_type != media_type:
            continue
        s_category = s.get("category", "")
        if s_category:
            cats = [c.strip() for c in s_category.split(",")]
            if category and category not in cats:
                continue
        priority_levels = s.get("priority_level", [])
        if not priority_levels:
            return s

    # 第三轮：找无 media_type 的通用兜底策略
    for s in strategies:
        s_media_type = s.get("media_type", "")
        if s_media_type:
            continue
        s_category = s.get("category", "")
        if s_category:
            cats = [c.strip() for c in s_category.split(",")]
            if category and category not in cats:
                continue
        return s

    return None


def should_replace_wash(
    new_info: dict,
    old_info: dict,
    strategy: dict,
) -> tuple:
    """
    根据洗版策略判断新文件是否应替换旧文件。
    返回 (should_replace: bool, reason: str)
    """
    mode = strategy.get("mode", "replace")

    if mode == "coexist":
        return False, "共存模式，保留所有版本"
    if mode == "skip":
        return False, "跳过模式，已存在则不替换"

    priority_levels = strategy.get("priority_level", [])
    new_rank = _get_wash_priority_rank(new_info, priority_levels)
    old_rank = _get_wash_priority_rank(old_info, priority_levels)

    if new_rank < old_rank:
        return True, f"新文件优先级更高（rank {new_rank} < {old_rank}）"
    if old_rank < new_rank:
        return False, f"旧文件优先级更高（rank {old_rank} < {new_rank}）"

    # 优先级相同
    if mode == "replace":
        return False, "优先级相同，不替换"
    if mode == "max_size":
        new_size = new_info.get("size", 0)
        old_size = old_info.get("size", 0)
        if new_size and old_size and new_size > old_size:
            return True, f"优先级相同，新文件更大（{new_size} > {old_size}）"
        return False, f"优先级相同，新文件不更大（{new_size} <= {old_size}）"
    if mode == "min_size":
        new_size = new_info.get("size", 0)
        old_size = old_info.get("size", 0)
        if new_size and old_size and new_size < old_size:
            return True, f"优先级相同，新文件更小（{new_size} < {old_size}）"
        return False, f"优先级相同，新文件不更小（{new_size} >= {old_size}）"

    return False, f"未知模式: {mode}"


def get_pinyin_info(title: str) -> tuple[str, str]:
    """
    将中文标题转为拼音。
    返回 (full_pinyin, first_letter_uppercase)
    如果标题不含中文或转换失败，返回 ("", "")
    """
    try:
        from pypinyin import lazy_pinyin, Style
        # 检查是否含中文
        if not re.search(r'[\u4e00-\u9fff]', title):
            return ("", "")
        pinyin_list = lazy_pinyin(title, style=Style.NORMAL)
        full = "".join(pinyin_list)
        first_letter = full[0].upper() if full else ""
        return (full, first_letter)
    except Exception as e:
        logger.debug(f"拼音转换失败 '{title}': {e}")
        return ("", "")


def _eval_expr(expr: str, variables: dict) -> str:
    """求值单个 {表达式}，返回字符串结果。使用 simpleeval 安全求值。"""
    expr = expr.strip()

    # :02d 零填充格式: {season_num:02d}
    m = re.match(r'^(\w+):(\d+)d$', expr)
    if m:
        var_name = m.group(1)
        width = int(m.group(2))
        val = variables.get(var_name, "")
        if val:
            try:
                return str(int(val)).zfill(width)
            except (ValueError, TypeError):
                return val
        return ""

    # 简单变量名
    if re.match(r'^\w+$', expr):
        val = variables.get(expr, "")
        return str(val) if val else ""

    # 安全表达式求值（支持 .replace()/.lower()/.upper() 等字符串方法）
    try:
        from simpleeval import simple_eval
        result = simple_eval(expr, names=variables)
        return str(result) if result is not None else ""
    except Exception as e:
        logger.debug(f"表达式求值失败 '{expr}': {e}")
        return ""


def _eval_template_string(s: str, variables: dict) -> str:
    """求值字符串中所有 {变量} 和 {Python表达式} 模式。"""
    return re.sub(r'\{([^{}]+)\}', lambda m: _eval_expr(m.group(1), variables), s)


def _evaluate_block(content: str, variables: dict) -> tuple:
    """
    求值块内容。
    返回 (evaluated_text, should_output)。
    should_output 为 True 当且仅当块内所有 {变量} 都不为空。
    """
    pattern = re.compile(r'\{([^{}]+)\}')
    matches = list(pattern.finditer(content))

    if not matches:
        # 无变量，纯文本块，始终输出
        return content, True

    should_output = True
    result_parts = []
    last_end = 0
    for m in matches:
        result_parts.append(content[last_end:m.start()])
        expr = m.group(1).strip()
        val = _eval_expr(expr, variables)
        if not val:
            should_output = False
        result_parts.append(val)
        last_end = m.end()
    result_parts.append(content[last_end:])

    return ''.join(result_parts), should_output


def _clip_filename(name: str, max_len: int = 200) -> str:
    """O4: 重命名结果超长智能裁剪（参考 LitePan naming.go FitFilenameBytes 思路，简化版）。

    若 len(name) <= max_len 直接返回原名。
    若超长：优先保留扩展名（.mkv/.mp4 等），先去掉尾部标签/空格，
    再从尾部（扩展名前）逐字符裁剪到 max_len，再拼接扩展名；
    若裁剪后仍超长（如扩展名本身超长），从头部裁。
    返回裁剪结果并记录 info 日志。
    仅用于最终文件名（renamed_to），不改变目录名逻辑。
    """
    if len(name) <= max_len:
        return name

    # 提取扩展名（取最后一个点之后的部分，如 .mkv/.mp4）
    ext = ""
    stem = name
    head, dot, tail = name.rpartition(".")
    if dot and head and len(tail) <= 12:  # 尾部过长的视为普通文件名，不当作扩展名
        ext = dot + tail
        stem = head

    # 优先保留扩展名：去掉尾部标签/空格后，从尾部（扩展名前）逐字符裁剪
    stem = stem.rstrip(" .-_")
    keep = max_len - len(ext)
    if keep > 0:
        stem = stem[:keep]
    else:
        stem = ""
    result = stem + ext

    # 仍超长（扩展名本身超长）：从头部裁
    if len(result) > max_len:
        result = result[:max_len]

    logger.info(f"[organize] 重命名结果超长已裁剪: 长度 {len(name)} -> {len(result)}（max_len={max_len}）")
    return result


def apply_rename_template(
    template: str,
    tmdb_info: Optional[dict],
    filename: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    season_detail: Optional[dict] = None,
    episode_detail: Optional[dict] = None,
    media_info: Optional[dict] = None,
    prefer_filename: bool = False,
) -> Optional[str]:
    """
    根据模板生成新名称。支持条件块语法：
    - {变量名} 取变量值
    - <...> 条件块（块内变量均不为空时才输出）
    - <{{name}}...> 命名块（输出+赋值，可用 {name} 引用）
    - <?{{name}}...> 静默命名块（只赋值不输出）
    - {Python表达式} 支持 .replace()/.lower()/.upper() 等
    - [[ ]] 转义为字面量 { }
    - Jinja2 语法（{{ var }} / {% ... %}）：检测到且不含 < 条件块标记时，
      用 jinja2.Template 渲染，context 包含现有所有变量；jinja2 不可用时降级到内置引擎。
    如果缺少必要变量，返回 None 表示跳过重命名。
    media_info: ffprobe 探测结果，用于补充文件名解析不到的资源信息。
    """
    if not template or not template.strip():
        return None

    # 解析资源信息（ffprobe 结果作为补充）
    res = parse_resource_info(filename, media_info, prefer_filename)

    # 自动提取季集号
    if season is None or episode is None:
        s, e = extract_season_episode(filename)
        if season is None:
            season = s
        if episode is None:
            episode = e

    # 构建 TMDB 变量
    title = ""
    en_title = ""
    year = ""
    tmdb_id = ""
    season_name = ""
    season_year = ""
    episode_name = ""

    if tmdb_info:
        title = tmdb_info.get("title") or tmdb_info.get("name") or ""
        en_title = tmdb_info.get("original_title") or tmdb_info.get("original_name") or ""
        date_str = tmdb_info.get("release_date") or tmdb_info.get("first_air_date") or ""
        if date_str and len(date_str) >= 4:
            year = date_str[:4]
        tmdb_id = str(tmdb_info.get("id", ""))

    # 标题为空时，检查模板是否在条件块外依赖 TMDB 变量
    if not title:
        template_no_blocks = re.sub(r'<[^>]*>', '', template)
        if re.search(r'\{(title|en_title|first_letter|year|tmdb_id|season_name|season_year|episode_name)\b', template_no_blocks):
            return None

    # 拼音信息
    full_pinyin, first_letter = get_pinyin_info(title)
    if not en_title and full_pinyin:
        en_title = full_pinyin

    # 季集详情
    if season_detail:
        season_name = season_detail.get("name") or ""
        air_date = season_detail.get("air_date") or ""
        if air_date and len(air_date) >= 4:
            season_year = air_date[:4]
    if episode_detail:
        episode_name = episode_detail.get("name") or ""

    # 季集号
    season_num = str(season) if season is not None else ""
    episode_num = str(episode) if episode is not None else ""
    season_episode = ""
    if season is not None and episode is not None:
        season_episode = f"S{season:02d}E{episode:02d}"
    elif season is not None:
        season_episode = f"S{season:02d}"

    # 原文件名（不含扩展名）
    original_name = re.sub(r'\.[^.]+$', '', filename)

    # 构建变量上下文
    av_code = ""
    if tmdb_info and isinstance(tmdb_info, dict):
        av_code = tmdb_info.get("av_code", "")

    variables = {
        "original_name": original_name,
        "ext": res["ext"],
        "title": title,
        "en_title": en_title,
        "first_letter": first_letter,
        "year": year,
        "tmdb_id": tmdb_id,
        "code": av_code,
        "resource_pix": res["resource_pix"],
        "resource_version": res["resource_version"],
        "resource_source": res["resource_source"],
        "resource_type": res["resource_type"],
        "resource_effect": res["resource_effect"],
        "video_encode": res["video_encode"],
        "audio_encode": res["audio_encode"],
        "resource_team": res["resource_team"],
        "fps": res["fps"],
        "season_episode": season_episode,
        "season_num": season_num,
        "episode_num": episode_num,
        "disc_num": res["disc_num"],
        "season_name": season_name,
        "season_year": season_year,
        "episode_name": episode_name,
        "custom_regex_match": "",
    }

    # ===== #18: Jinja2 模板渲染（向后兼容，不影响现有 <条件块> 语法） =====
    # 检测模板是否为 Jinja2 语法：含 {% 或 {{ 且不含 < 条件块标记
    _is_jinja2_syntax = ("{%" in template or "{{" in template) and "<" not in template
    if _is_jinja2_syntax:
        if not JINJA2_AVAILABLE:
            # jinja2 不可用时优雅降级：记录警告并走现有逻辑（避免完全无法重命名）
            logger.warning("[organize] 模板疑似 Jinja2 语法，但 jinja2 未安装，回退到内置模板引擎")
        else:
            # 标题为空时，检查 Jinja2 模板是否依赖 TMDB 变量（与现有逻辑保持一致）
            if not title:
                _jinja2_dep_re = re.compile(
                    r'\{\{\s*(title|en_title|first_letter|year|tmdb_id|season_name|season_year|episode_name)\b'
                )
                if _jinja2_dep_re.search(template):
                    return None
            try:
                # 使用 jinja2 渲染，context 包含现有所有变量
                _jinja2_env = _jinja2.Template(template)
                result = _jinja2_env.render(**variables)
                # 清理连续分隔符和首尾空白
                result = re.sub(r'\.{2,}', '.', result)
                result = re.sub(r'[ ]{2,}', ' ', result)
                result = re.sub(r'^[._\s]+|[._\s]+$', '', result)
                result = re.sub(r'\(\s*\)', '', result).strip()
                # 去除文件系统不允许的字符
                result = re.sub(r'[<>:"/\\|?*]', '', result)
                return result if result else None
            except Exception as e:
                logger.warning(f"[organize] Jinja2 模板渲染失败: {e}")
                return None

    # Step 1: 转义 [[ ]] -> 临时占位符（之后恢复为 { }）
    template = template.replace("[[", "\x00").replace("]]", "\x01")

    # Step 2: 处理条件块
    named_values = dict(variables)

    block_re = re.compile(r'<\?{{(\w+)}}([^>]*)>|<{{(\w+)}}([^>]*)>|<([^>]*)>')

    result_parts = []
    last_end = 0

    for m in block_re.finditer(template):
        # 块之前的文本
        text_before = template[last_end:m.start()]
        if text_before:
            result_parts.append(_eval_template_string(text_before, named_values))

        if m.group(1) is not None:
            # 静默命名块: <?{{name}}content>
            name = m.group(1)
            content = m.group(2)
            evaluated, _ = _evaluate_block(content, named_values)
            named_values[name] = evaluated
        elif m.group(3) is not None:
            # 命名块: <{{name}}content>
            name = m.group(3)
            content = m.group(4)
            evaluated, should_output = _evaluate_block(content, named_values)
            named_values[name] = evaluated
            if should_output and evaluated:
                result_parts.append(evaluated)
        else:
            # 条件块: <content>
            content = m.group(5)
            evaluated, should_output = _evaluate_block(content, named_values)
            if should_output and evaluated:
                result_parts.append(evaluated)

        last_end = m.end()

    # 剩余文本
    remaining = template[last_end:]
    if remaining:
        result_parts.append(_eval_template_string(remaining, named_values))

    result = ''.join(result_parts)

    # Step 3: 恢复转义 → { }
    result = result.replace("\x00", "{").replace("\x01", "}")

    # 清理连续分隔符
    result = re.sub(r'\.{2,}', '.', result)
    result = re.sub(r'[ ]{2,}', ' ', result)
    result = re.sub(r'^[._\s]+|[._\s]+$', '', result)
    result = re.sub(r'\(\s*\)', '', result).strip()

    # 去除文件系统不允许的字符
    result = re.sub(r'[<>:"/\\|?*]', '', result)

    return result if result else None


# ===== #16: 每目录独立配置覆盖 =====

# 可被目录覆盖覆盖的整理配置字段及其全局默认值
_DIR_OVERRIDE_FIELDS = {
    "use_ffprobe": False,
    "skip_no_info": False,
    "prefer_filename": False,
    "min_organize_size_mb": 0,
    "organize_blacklist": "",
    "ai_mode": "off",
    "auto_delete_inferior": False,
}


def get_dir_config(source_path: str) -> dict:
    """
    获取指定源目录的整理配置（合并全局配置和目录覆盖）。

    从 read_setting("organize_dirs") 读取配置：
    - 全局配置：organize_dirs 顶层的整理参数字段
    - 目录覆盖：organize_dirs["dir_overrides"] 列表，每项形如 {"path": str, "config": dict}
    合并规则：目录覆盖中的字段优先；值为 -1 或缺失时继承全局配置。

    Args:
        source_path: 源目录路径（网盘路径或本地路径，用于匹配 dir_overrides 中的 path）

    Returns:
        合并后的配置字典，包含 use_ffprobe / skip_no_info / prefer_filename /
        min_organize_size_mb / organize_blacklist / ai_mode / auto_delete_inferior 等字段。
    """
    data = read_setting("organize_dirs")
    if not isinstance(data, dict):
        data = {}

    # 1. 读取全局配置默认值
    config = {}
    for field, default_val in _DIR_OVERRIDE_FIELDS.items():
        config[field] = data.get(field, default_val)

    # 2. 查找匹配的目录覆盖
    dir_overrides = data.get("dir_overrides", [])
    if not isinstance(dir_overrides, list) or not source_path:
        return config

    # 规范化 source_path 用于前缀匹配（去尾部分隔符）
    norm_source = source_path.rstrip("/").rstrip("\\")
    best_match = None
    best_match_len = -1

    for entry in dir_overrides:
        if not isinstance(entry, dict):
            continue
        entry_path = (entry.get("path") or "").strip()
        if not entry_path:
            continue
        # 规范化覆盖路径
        norm_entry = entry_path.rstrip("/").rstrip("\\")
        # 匹配规则：source_path 等于 entry_path，或以 entry_path + 分隔符 开头
        if norm_source == norm_entry or norm_source.startswith(norm_entry + "/") or norm_source.startswith(norm_entry + "\\"):
            # 选择最长匹配（最具体的覆盖优先）
            if len(norm_entry) > best_match_len:
                best_match_len = len(norm_entry)
                best_match = entry

    if not best_match:
        return config

    # 3. 合并目录覆盖（-1 或缺失表示继承全局）
    override_config = best_match.get("config", {})
    if not isinstance(override_config, dict):
        override_config = {}

    for field in _DIR_OVERRIDE_FIELDS:
        if field not in override_config:
            continue
        val = override_config[field]
        # -1 表示继承全局（仅对数值/布尔字段有意义）
        if val is None or val == -1:
            continue
        config[field] = val

    logger.info(f"[organize] 目录覆盖生效: source_path='{source_path}', 匹配='{best_match.get('path', '')}'")
    return config


# ===== #21: 关联文件批量整理 + 自动删除劣质源 =====

# 关联字幕文件扩展名
LINKED_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}

# 外挂音轨扩展名
LINKED_AUDIO_EXTS = {".mka", ".ac3", ".dts"}

# 关联文件扩展名集合（字幕 + 外挂音轨）
LINKED_FILE_EXTS = LINKED_SUBTITLE_EXTS | LINKED_AUDIO_EXTS

# 分辨率优先级排名（数值越大越优）
_PIX_RANK_MAP = {
    "480p": 1,
    "720p": 2,
    "1080i": 3,
    "1080p": 4,
    "1440p": 5,
    "2160i": 6,
    "2160p": 7,
}


def _find_linked_files(video_path: str, dir_files: list) -> list:
    """
    找同目录下同名字幕文件和外挂音轨文件。

    匹配规则：文件基名（不含扩展名）与视频文件基名相同，
    且扩展名属于字幕（.srt/.ass/.ssa/.sub/.vtt）或外挂音轨（.mka/.ac3/.dts）。

    Args:
        video_path: 视频文件名（或路径，取基名部分）
        dir_files:  同目录下的文件列表，每项为 dict（至少含 "name" 字段）

    Returns:
        关联文件路径/名称列表（list[str]）
    """
    # 提取视频文件基名（不含扩展名）
    video_name = video_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    video_base = video_name.rsplit(".", 1)[0] if "." in video_name else video_name
    if not video_base:
        return []

    linked = []
    for f in dir_files:
        if not isinstance(f, dict):
            continue
        fname = f.get("name", "")
        if not fname or fname == video_name:
            continue
        # 提取扩展名（小写带点）
        ext = ("." + fname.rsplit(".", 1)[-1].lower()) if "." in fname else ""
        if ext not in LINKED_FILE_EXTS:
            continue
        # 提取文件基名
        f_base = fname.rsplit(".", 1)[0] if "." in fname else fname
        if f_base == video_base:
            linked.append(fname)
    return linked


def _pix_rank(pix: str) -> int:
    """获取分辨率排名（数值越大越优），未知分辨率返回 0。"""
    return _PIX_RANK_MAP.get((pix or "").lower(), 0)


def auto_delete_inferior_source(new_path: str, old_path: str, new_info: dict, old_info: dict) -> bool:
    """
    比较新旧文件的分辨率/码率，如果新文件更优则删除旧文件。

    比较维度（按优先级）：
      1. 分辨率（resource_pix）：2160p > 1080p > 720p > 480p
      2. 码率（bitrate）：数值越大越优（来自 ffprobe 探测结果）
    若新文件分辨率更高，或分辨率相同但码率更高，则判定为新文件更优。

    配置开关：read_setting("organize_dirs") 中的 auto_delete_inferior（默认 False）。
    开关关闭时直接返回 False（不删除）。

    Args:
        new_path:  新文件路径/名称（用于日志）
        old_path:  旧文件路径/名称（用于日志；若为本地路径则尝试直接删除）
        new_info:  新文件资源信息（parse_resource_info 或 ffprobe 结果）
        old_info:  旧文件资源信息

    Returns:
        True 表示新文件更优且旧文件已被删除或应被删除；False 表示不删除。
    """
    # 检查配置开关
    cfg = read_setting("organize_dirs")
    if not isinstance(cfg, dict) or not cfg.get("auto_delete_inferior", False):
        return False

    new_info = new_info or {}
    old_info = old_info or {}

    # 1. 分辨率比较
    new_pix_rank = _pix_rank(new_info.get("resource_pix", ""))
    old_pix_rank = _pix_rank(old_info.get("resource_pix", ""))

    if new_pix_rank > old_pix_rank:
        logger.info(
            f"[organize] 自动删除劣质源: 新文件分辨率更优 "
            f"'{new_info.get('resource_pix', '')}' > '{old_info.get('resource_pix', '')}' "
            f"new='{new_path}' old='{old_path}'"
        )
        _try_delete_local(old_path)
        return True

    if new_pix_rank < old_pix_rank:
        return False

    # 2. 分辨率相同，比较码率（来自 ffprobe 的 bitrate 字段）
    new_br = 0
    old_br = 0
    try:
        new_br = int(new_info.get("bitrate", 0) or 0)
    except (ValueError, TypeError):
        pass
    try:
        old_br = int(old_info.get("bitrate", 0) or 0)
    except (ValueError, TypeError):
        pass

    if new_br > 0 and new_br > old_br:
        logger.info(
            f"[organize] 自动删除劣质源: 新文件码率更优 {new_br} > {old_br} "
            f"new='{new_path}' old='{old_path}'"
        )
        _try_delete_local(old_path)
        return True

    return False


def _try_delete_local(path: str) -> bool:
    """尝试删除本地文件（若 path 为本地文件路径）。返回是否已删除。"""
    if not path:
        return False
    try:
        from pathlib import Path
        p = Path(path)
        if p.is_file():
            p.unlink()
            logger.info(f"[organize] 已删除劣质源本地文件: {path}")
            return True
    except Exception as e:
        logger.debug(f"[organize] 删除本地文件失败（可能为网盘路径）: {path}: {e}")
    return False


class OrganizeService:
    """网盘文件整理服务"""

    # 整理并发锁：手动整理（API /run）和定时整理（_run_scheduled_organize）
    # 共用一把锁，防止并发调用 115 API 触发风控。非阻塞获取，获取不到即跳过。
    _organize_lock = threading.Lock()

    @classmethod
    async def scan_and_organize(
        cls,
        cookies: str,
        source_cid: str,
        target_cid: str = "",
        existing_cid: str = "",
        redundant_cid: str = "",
        unrecognized_cid: str = "",
        classify_config: str = "",
        category_roots: dict = None,
        rename_rules: dict = None,
        wash_config: dict = None,
        use_ffprobe: bool = False,
        skip_no_info: bool = False,
        prefer_filename: bool = False,
        min_organize_size_mb: int = 0,
        organize_blacklist: str = "",
        ai_mode: str = "off",  # off=关闭, assist=TMDB失败时辅助, force=强制使用AI
        dry_run: bool = False,
        progress_callback=None,
        source_path: str = "",  # #16: 源目录路径，用于每目录配置覆盖
    ) -> dict:
        """
        扫描源目录并整理文件

        Args:
            cookies: 115 账号 cookies
            source_cid: 源目录 cid
            target_cid: 全量同步目录 cid（整理后影视文件移入此处）
            existing_cid: 已存在影视的目录 cid（全量同步目录中已存在的重复文件移入此处）
            redundant_cid: 冗余文件存在的目录 cid（重复/冗余文件移入）
            unrecognized_cid: 识别不准的目录 cid（无法识别的文件移入）
            classify_config: 二级分类 YAML 配置字符串
            rename_rules: 重命名规则字典
            wash_config: 洗版策略配置字典
            dry_run: True=仅预览，不实际移动/重命名文件
            progress_callback: 可选的异步回调函数 async callback(current, total, filename)
            source_path: 源目录路径（#16 用于每目录配置覆盖）

        Returns:
            {
                "total": int,
                "organized": [{"name", "category", "from", "to", "renamed_to"}],
                "redundant": [{"name", "reason"}],
                "unrecognized": [{"name", "reason"}],
                "errors": [{"name", "error"}],
                "dry_run": bool,
            }
        """
        # 并发保护：非阻塞获取整理锁，已有整理任务在运行时直接跳过本次
        # （手动整理 API 侧另有 progress_manager 保护，这里是服务层兜底，
        #   覆盖定时整理路径，避免与手动整理并发触发 115 风控）
        if not cls._organize_lock.acquire(blocking=False):
            logger.warning("[organize] 已有整理任务正在运行，跳过本次整理（防止并发触发风控）")
            return {
                "total": 0, "organized": [], "redundant": [],
                "unrecognized": [], "errors": [],
                "skipped_concurrent": True, "dry_run": dry_run,
            }
        try:
            return await cls._scan_and_organize_locked(
                cookies=cookies, source_cid=source_cid, target_cid=target_cid,
                existing_cid=existing_cid, redundant_cid=redundant_cid,
                unrecognized_cid=unrecognized_cid, classify_config=classify_config,
                category_roots=category_roots, rename_rules=rename_rules,
                wash_config=wash_config, use_ffprobe=use_ffprobe,
                skip_no_info=skip_no_info, prefer_filename=prefer_filename,
                min_organize_size_mb=min_organize_size_mb,
                organize_blacklist=organize_blacklist, ai_mode=ai_mode,
                dry_run=dry_run, progress_callback=progress_callback,
                source_path=source_path,
            )
        finally:
            cls._organize_lock.release()

    @classmethod
    async def _scan_and_organize_locked(
        cls,
        cookies: str,
        source_cid: str,
        target_cid: str = "",
        existing_cid: str = "",
        redundant_cid: str = "",
        unrecognized_cid: str = "",
        classify_config: str = "",
        category_roots: dict = None,
        rename_rules: dict = None,
        wash_config: dict = None,
        use_ffprobe: bool = False,
        skip_no_info: bool = False,
        prefer_filename: bool = False,
        min_organize_size_mb: int = 0,
        organize_blacklist: str = "",
        ai_mode: str = "off",
        dry_run: bool = False,
        progress_callback=None,
        source_path: str = "",
    ) -> dict:
        """整理核心逻辑（已持有 _organize_lock，仅由 scan_and_organize 调用）"""
        result = {
            "total": 0,
            "organized": [],
            "redundant": [],
            "unrecognized": [],
            "errors": [],
            "dry_run": dry_run,
        }

        # #16: 每目录独立配置覆盖 - 合并全局配置和目录覆盖（目录配置优先）
        if source_path:
            _dir_cfg = get_dir_config(source_path)
            use_ffprobe = _dir_cfg.get("use_ffprobe", use_ffprobe)
            skip_no_info = _dir_cfg.get("skip_no_info", skip_no_info)
            prefer_filename = _dir_cfg.get("prefer_filename", prefer_filename)
            min_organize_size_mb = _dir_cfg.get("min_organize_size_mb", min_organize_size_mb)
            organize_blacklist = _dir_cfg.get("organize_blacklist", organize_blacklist)
            ai_mode = _dir_cfg.get("ai_mode", ai_mode)

        logger.info(f"[organize] 整理开始: source_cid={source_cid}, target_cid={target_cid}, existing_cid={existing_cid}, redundant_cid={redundant_cid}, unrecognized_cid={unrecognized_cid}")
        _organize_start_ts = _time.time()

        # 检查 API 速率限制配置并在实时日志显示
        from app.core.db_helper import get_api_intervals
        _api_intervals = get_api_intervals()
        _rate_interval = _api_intervals.get("download_url_interval", 0)
        if _rate_interval > 0:
            logger.info(f"[organize] 已启用 API 速率限制: 间隔 {_rate_interval}s/次（重命名、移动、创建目录等操作均受限流）")
        else:
            logger.info(f"[organize] API 速率限制未启用")

        # 检查 TMDB API Key 是否已配置（未配置时在实时日志显示警告，已配置则不显示）
        from app.services.tmdb_service import TmdbService
        tmdb_api_key = TmdbService._get_api_key()
        if not tmdb_api_key:
            logger.warning(f"[organize] TMDB API Key 未配置，所有影视将无法识别，将被移到识别不准的目录")

        # 初始化分类引擎
        try:
            category_helper = CategoryHelper(classify_config) if classify_config else CategoryHelper()
        except ValueError as e:
            logger.warning(f"[organize] 分类配置解析失败，使用默认配置: {e}")
            category_helper = CategoryHelper()

        # 加载全量同步配置，获取后缀分类（媒体图片 / 媒体数据）
        image_exts: set = set()
        data_exts: set = set()
        try:
            from app.services.sync_service import SyncService
            sync_config = SyncService.load_schedule()
            if sync_config:
                image_exts_str = sync_config.get("image_exts_str", "")
                data_exts_str = sync_config.get("data_exts_str", "")
                if image_exts_str:
                    image_exts = {ext.strip().lower() for ext in image_exts_str.split(",") if ext.strip()}
                if data_exts_str:
                    data_exts = {ext.strip().lower() for ext in data_exts_str.split(",") if ext.strip()}
                logger.info(f"[organize] 后缀分类: 视频={VIDEO_EXTS}, 媒体数据={data_exts}, 媒体图片={image_exts}")
        except Exception as e:
            logger.warning(f"[organize] 加载同步配置失败，仅整理视频文件: {e}")

        # 1. 扫描源目录所有匹配文件（视频 + 媒体数据 + 媒体图片）
        logger.info(f"[organize] 正在扫描源目录文件...")
        all_scan_exts = VIDEO_EXTS | data_exts | image_exts
        all_files = Client115Service.list_all_files_with_meta(
            cookies, source_cid, all_scan_exts, min_size=0, recursive=True
        )

        # 按类型分离文件
        def _get_ext(name: str) -> str:
            return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""

        video_files = [f for f in all_files if _get_ext(f["name"]) in VIDEO_EXTS]
        data_files = [f for f in all_files if _get_ext(f["name"]) in data_exts]
        image_files = [f for f in all_files if _get_ext(f["name"]) in image_exts]
        logger.info(f"[organize] 扫描完成: 视频 {len(video_files)} 个, 媒体数据 {len(data_files)} 个, 媒体图片 {len(image_files)} 个")

        # 视频文件按最小大小过滤
        min_size_bytes = min_organize_size_mb * 1024 * 1024 if min_organize_size_mb > 0 else 0
        if min_size_bytes > 0:
            before_count = len(video_files)
            video_files = [f for f in video_files if f.get("size", 0) >= min_size_bytes]
            skipped = before_count - len(video_files)
            if skipped > 0:
                logger.info(f"[organize] 最小视频大小过滤: 跳过 {skipped} 个小于 {min_organize_size_mb}MB 的文件")

        # 按整理黑名单（正则）过滤视频文件
        if organize_blacklist and organize_blacklist.strip():
            blacklist_patterns = []
            for line in organize_blacklist.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    blacklist_patterns.append(re.compile(line))
                except re.error as e:
                    logger.warning(f"[organize] 黑名单正则无效，已忽略: {line} ({e})")
            if blacklist_patterns:
                before_count = len(video_files)
                video_files = [f for f in video_files if not any(p.search(f.get("name", "")) for p in blacklist_patterns)]
                skipped = before_count - len(video_files)
                if skipped > 0:
                    logger.info(f"[organize] 黑名单过滤: 跳过 {skipped} 个匹配黑名单的文件")

        result["total"] = len(video_files)
        logger.info(f"[organize] 待整理视频文件: {len(video_files)} 个")
        if not video_files:
            logger.info(f"[organize] 源目录无匹配视频文件")
            # 即使没有视频文件，也要将残留的非视频文件移到冗余目录并清理源目录
            if not dry_run and redundant_cid:
                # 移动媒体图片和媒体数据文件到冗余目录
                for img_f in image_files:
                    try:
                        ok_img = Client115Service.move(cookies, [img_f["file_id"]], redundant_cid, context=img_f["name"])
                        if ok_img:
                            logger.info(f"[organize] 残留媒体图片移到冗余: {img_f['name']}")
                            result["redundant"].append({"name": img_f["name"], "reason": "残留媒体图片文件"})
                        else:
                            logger.warning(f"[organize] 媒体图片移动失败: {img_f['name']}")
                    except Exception as e:
                        logger.warning(f"[organize] 媒体图片移动异常: {img_f['name']}: {e}")
                for df in data_files:
                    try:
                        ok_df = Client115Service.move(cookies, [df["file_id"]], redundant_cid, context=df["name"])
                        if ok_df:
                            logger.info(f"[organize] 残留媒体数据移到冗余: {df['name']}")
                            result["redundant"].append({"name": df["name"], "reason": "残留媒体数据文件"})
                        else:
                            logger.warning(f"[organize] 媒体数据移动失败: {df['name']}")
                    except Exception as e:
                        logger.warning(f"[organize] 媒体数据移动异常: {df['name']}: {e}")
                # 清理源目录：将所有残留子项（目录和文件）移到冗余目录
                try:
                    logger.info(f"[organize] 开始清理源目录残留...")
                    remaining_items = Client115Service.list_all_items(
                        cookies, source_cid, recursive=False
                    )
                    if remaining_items:
                        moved = 0
                        failed_items = []  # 记录第一轮移动失败的项
                        for item in remaining_items:
                            item_id = item.get("id", "")
                            item_name = item.get("name", "")
                            if not item_id:
                                continue
                            try:
                                ok = Client115Service.move(cookies, [item_id], redundant_cid, context=item_name)
                                if ok:
                                    moved += 1
                                    logger.info(f"[organize] 残留移到冗余目录: {item_name}")
                                else:
                                    failed_items.append(item)
                            except Exception as e:
                                logger.warning(f"[organize] 残留移动异常: {item_name}: {e}")
                                failed_items.append(item)
                            _time.sleep(_organize_write_interval())
                        # 第二轮：重试第一轮失败的项（等待更长时间）
                        if failed_items:
                            logger.info(f"[organize] {len(failed_items)} 个残留项移动失败，等待冷却后重试...")
                            _time.sleep(_organize_retry_cooldown())
                            still_failed = []
                            for item in failed_items:
                                item_id = item.get("id", "")
                                item_name = item.get("name", "")
                                try:
                                    ok = Client115Service.move(cookies, [item_id], redundant_cid, context=item_name)
                                    if ok:
                                        moved += 1
                                        logger.info(f"[organize] 残留重试成功: {item_name}")
                                    else:
                                        still_failed.append(item)
                                        logger.warning(f"[organize] 残留重试仍失败: {item_name}")
                                except Exception as e:
                                    still_failed.append(item)
                                    logger.warning(f"[organize] 残留重试异常: {item_name}: {e}")
                                _time.sleep(_organize_write_interval())
                            # 仍然失败的项记录到结果中
                            if still_failed:
                                result["cleanup_failed"] = [
                                    {"name": item.get("name", ""), "id": item.get("id", "")}
                                    for item in still_failed
                                ]
                                logger.warning(f"[organize] {len(still_failed)} 个残留项移动失败，需手动处理")
                        if moved > 0:
                            result["cleaned_items"] = moved
                            logger.info(f"[organize] 已将 {moved} 个残留项移到冗余目录")
                    else:
                        logger.info(f"[organize] 源目录已清空")
                except Exception as e:
                    logger.warning(f"[organize] 清理源目录失败: {e}")
            logger.info(f"[organize] 整理完成: 无视频文件, 耗时 {_time.time() - _organize_start_ts:.1f}s")
            return result

        # 2. 分类：冗余文件 / 可识别 / 不可识别
        # 按 SHA1 去重：仅 SHA1 完全一致才视为重复文件
        # 无 SHA1 的文件不参与去重，各自独立处理
        sha1_map = defaultdict(list)   # sha1 -> [file_info, ...]
        _no_sha1_files: list = []      # 无 SHA1 的文件，不参与去重
        for f in video_files:
            sha1 = f.get("sha1", "")
            if sha1:
                sha1_map[sha1].append(f)
            else:
                _no_sha1_files.append(f)

        to_organize = []   # 可整理（电影/剧集）
        to_redundant = []  # 冗余/重复
        to_unrecognized = []  # 无法识别

        # 每组同 SHA1 文件只保留第一个，其余标记为重复；无 SHA1 的全部保留
        deduped_files: list = []
        for sha1, files in sha1_map.items():
            keep = files[0]
            deduped_files.append(keep)
            for d in files[1:]:
                logger.info(f"[organize] 重复文件(SHA1一致): {d['name']} (与 {keep['name']} 重复)")
                to_redundant.append({"file": d, "reason": f"与 {keep['name']} 重复(SHA1一致)"})
        deduped_files.extend(_no_sha1_files)

        logger.info(f"[organize] 开始分类识别 ({len(deduped_files)} 个文件，已去重 {len(video_files) - len(deduped_files)} 个)...")

        _idx = 0
        for f in deduped_files:
            _idx += 1
            if progress_callback:
                try:
                    await progress_callback(_idx, len(deduped_files), f["name"])
                except Exception:
                    pass
            if is_redundant(f["name"]):
                logger.info(f"[organize] 冗余文件: {f['name']}")
                to_redundant.append({"file": f, "reason": "冗余文件（sample/预告等）"})
            else:
                category, tmdb_info, media_type = await cls._classify_file(
                    f["name"], category_helper, category_roots, ai_mode,
                    parent_path=f.get("parent_path", ""),
                )
                if category is None:
                    logger.warning(f"[organize] 无法识别: {f['name']}")
                    to_unrecognized.append({"file": f, "reason": "无法识别影视类型"})
                else:
                    logger.info(f"[organize] 识别成功: {f['name']} -> {category} ({media_type})")
                    to_organize.append({"file": f, "category": category, "tmdb_info": tmdb_info, "media_type": media_type})

        logger.info(f"[organize] 分类完成: 可整理 {len(to_organize)}，冗余 {len(to_redundant)}，无法识别 {len(to_unrecognized)}")

        # 3. 执行移动操作
        if dry_run:
            # ===== 预览模式：只计算目标路径，不实际移动 =====
            logger.info(f"[organize] 预览模式：不执行实际移动操作")
            for item in to_organize:
                file_info = item["file"]
                orig_name = file_info["name"]
                category = item["category"]
                # 计算重命名后的文件名
                renamed_to = ""
                new_folder = ""
                if rename_rules and item.get("tmdb_info"):
                    new_name, new_folder, _ = await cls._compute_rename(
                        rename_rules, orig_name, item.get("tmdb_info"),
                        item.get("media_type", "movie"), None, skip_no_info, prefer_filename
                    )
                    renamed_to = new_name or orig_name
                else:
                    renamed_to = orig_name
                # O4: 最终文件名超长智能裁剪（仅作用于 renamed_to，不改变目录名逻辑）
                _clipped = _clip_filename(renamed_to)
                _is_clipped = _clipped != renamed_to
                renamed_to = _clipped
                _preview_entry = {
                    "name": orig_name,
                    "category": category,
                    "from": file_info.get("parent_path", ""),
                    "to": f"{category}/{new_folder or ''}/{renamed_to}",
                    "renamed_to": renamed_to if renamed_to != orig_name else "",
                }
                if _is_clipped:
                    _preview_entry["clipped"] = True
                result["organized"].append(_preview_entry)
            for item in to_redundant:
                result["redundant"].append({"name": item["file"]["name"], "reason": item["reason"]})
            for item in to_unrecognized:
                result["unrecognized"].append({"name": item["file"]["name"], "reason": item["reason"]})
            # 预览：报告媒体数据文件和媒体图片文件的处理方式
            for df in data_files:
                result["redundant"].append({"name": df["name"], "reason": "媒体数据文件（预览：将跟随视频或移到冗余）"})
            for img_f in image_files:
                result["redundant"].append({"name": img_f["name"], "reason": "媒体图片文件（移到冗余）"})
            logger.info(
                f"[organize] 预览完成: 共 {result['total']} 个文件，"
                f"可整理 {len(result['organized'])}，冗余 {len(result['redundant'])}，"
                f"无法识别 {len(result['unrecognized'])}，"
                f"耗时 {_time.time() - _organize_start_ts:.1f}s"
            )
            return result

        # 3a. 扫描全量同步目录中已有文件的 SHA1，用于判断是否重复
        #     排除 source_cid（待整理目录），因为它里面的文件还没被整理，不应算作"已存在"
        existing_sha1s: set = set()
        if target_cid and existing_cid:
            logger.info(f"[organize] 正在扫描全量同步目录已有文件（用于重复检测）...")
            try:
                # 排除待整理目录、已存在影视目录、冗余目录等子目录
                exclude_cids = {source_cid}
                if existing_cid:
                    exclude_cids.add(existing_cid)
                if redundant_cid:
                    exclude_cids.add(redundant_cid)
                if unrecognized_cid:
                    exclude_cids.add(unrecognized_cid)
                existing_files_list = Client115Service.list_all_files_with_meta(
                    cookies, target_cid, VIDEO_EXTS, min_size=0, recursive=True,
                )
                # 过滤掉排除的子目录（list_all_files_with_meta 不支持 exclude_cids）
                if exclude_cids:
                    existing_files_list = [
                        ef for ef in existing_files_list
                        if ef.get("parent_id", "") not in exclude_cids
                    ]
                for ef in existing_files_list:
                    sha1 = ef.get("sha1", "")
                    if sha1:
                        existing_sha1s.add(sha1)
                logger.info(f"[organize] 全量同步目录已有 {len(existing_sha1s)} 个文件 SHA1（已排除待整理等子目录）")
            except Exception as e:
                logger.warning(f"[organize] 扫描全量同步目录失败，跳过重复检测: {e}")

        # 3b. 批量整理：先全部 ffprobe+重命名，再统一移动
        #     避免逐个处理导致的中间状态混乱
        # 已跟随视频移动的媒体数据文件 ID（避免重复移动）
        moved_data_file_ids: set = set()

        if target_cid:
            # 按二级分类分组
            category_groups = defaultdict(list)
            for item in to_organize:
                category_groups[item["category"]].append(item)

            # ===== 阶段 1：ffprobe 探测 + 洗版检查 + 重命名（不移动） =====
            logger.info(f"[organize] 阶段 1/2：开始 ffprobe 探测和重命名（共 {len(to_organize)} 个文件）")
            rename_results = []  # [{item, renamed_to, new_folder, season_folder, media_info, skip_move, final_target_cid}]

            for category, items in category_groups.items():
                # category 是路径（如 "电影/动画电影"），拆分为多级目录
                path_parts = category.split("/")
                sub_cid = Client115Service.ensure_path(cookies, path_parts, target_cid)
                if not sub_cid:
                    logger.warning(f"[organize] 创建分类目录失败: {category}")
                    for item in items:
                        result["errors"].append({
                            "name": item["file"]["name"],
                            "error": f"创建分类目录「{category}」失败",
                        })
                        rename_results.append({"item": item, "skip_move": True})
                    continue

                # 按分类目录缓存已有文件列表（含 sha1），避免洗版检查时重复 API 调用
                wash_cached_files = None
                if wash_config and wash_config.get("enabled"):
                    try:
                        wash_cached_files = Client115Service.list_all_files(
                            cookies, sub_cid, VIDEO_EXTS, min_size=0, recursive=True
                        )
                        logger.info(f"[organize] 洗版缓存: 目录「{category}」已加载 {len(wash_cached_files)} 个已有文件")
                    except Exception as e:
                        logger.warning(f"[organize] 洗版缓存加载失败（目录「{category}」），将逐个查询: {e}")
                        wash_cached_files = None

                for item in items:
                    file_info = item["file"]
                    tmdb_info = item.get("tmdb_info")
                    media_type = item.get("media_type", "movie")
                    orig_name = file_info["name"]
                    renamed_to = ""

                    # 重复检测：全量同步目录中已有相同 SHA1 的文件 → 移到「已存在影视的目录」
                    file_sha1 = file_info.get("sha1", "")
                    if file_sha1 and file_sha1 in existing_sha1s and existing_cid:
                        logger.info(f"[organize] 全量同步目录已存在相同SHA1文件: {orig_name}，移到已存在影视目录")
                        rename_results.append({
                            "item": item, "skip_move": True, "skip_reason": "existing",
                            "renamed_to": "", "sub_cid": sub_cid,
                        })
                        continue

                    # I1: OOF 快速媒体信息探测（免下载，先于 ffprobe）
                    # 命中 115 已有同 sha1 文件时，直接复用其 nfo/jpg 信息，跳过 ffprobe 硬下载
                    oof_media_info = None
                    if wash_config and wash_config.get("enabled"):
                        try:
                            f_sha1 = file_info.get("sha1", "")
                            if f_sha1:
                                oof_result = Client115Service.fetch_media_info_fast(cookies, f_sha1)
                                if oof_result and (oof_result.get("nfo_files") or oof_result.get("image_files")):
                                    oof_media_info = oof_result
                                    logger.info(f"[organize] OOF 命中媒体信息: {orig_name} (sha1={f_sha1[:8]}...), nfo={len(oof_result.get('nfo_files', []))}, img={len(oof_result.get('image_files', []))}")
                        except Exception as e:
                            logger.warning(f"[organize] OOF 探测失败（跳过）: {orig_name} - {e}")

                    # ffprobe 探测媒体信息（OOF 已命中时跳过，避免硬下载）
                    media_info = None
                    if use_ffprobe and is_ffprobe_available() and (rename_rules or (wash_config and wash_config.get("enabled"))) and not oof_media_info:
                        try:
                            download_url = Client115Service.get_download_url(cookies, file_info.get("pickcode", ""), context=orig_name)
                            if download_url:
                                logger.info(f"ffprobe 探测: {orig_name}")
                                media_info = await probe_media_info_async(download_url, timeout=30, file_name=orig_name)
                                if media_info:
                                    logger.info(f"ffprobe 结果: pix={media_info.get('resource_pix')}, enc={media_info.get('video_encode')}, audio={media_info.get('audio_encode')}, fps={media_info.get('fps')}")
                        except Exception as e:
                            logger.warning(f"ffprobe 探测失败（跳过）: {orig_name} - {e}")

                    # 洗版检查
                    skip_move = False
                    skip_reason = ""
                    if wash_config and wash_config.get("enabled"):
                        should_move, wash_reason, old_files_to_replace = await cls._check_wash_replace(
                            cookies, sub_cid, file_info, tmdb_info, media_type, wash_config, media_info or oof_media_info, category, prefer_filename,
                            cached_existing_files=wash_cached_files,
                        )
                        if not should_move:
                            skip_move = True
                            skip_reason = f"洗版跳过: {wash_reason}"
                        elif old_files_to_replace:
                            # #21: 自动删除劣质源 / 洗版替换旧文件处理
                            _new_info = parse_resource_info(file_info["name"], media_info, prefer_filename)
                            _new_info["size"] = file_info.get("size", 0)
                            for old_file in old_files_to_replace:
                                old_name = old_file.get("name", "")
                                _old_info = parse_resource_info(old_name, None, prefer_filename)
                                _old_info["size"] = old_file.get("size", 0)
                                # 判断是否自动删除劣质源（内部检查配置开关）
                                if auto_delete_inferior_source(file_info["name"], old_name, _new_info, _old_info):
                                    # 新文件更优 -> 删除旧文件（移入 115 回收站）
                                    del_resp = Client115Service.delete_files(cookies, [old_file["file_id"]])
                                    if not (isinstance(del_resp, dict) and del_resp.get("error")):
                                        logger.info(f"[organize] 自动删除劣质源: {old_name}")
                                        result["redundant"].append({"name": old_name, "reason": f"自动删除劣质源: {wash_reason}"})
                                    elif redundant_cid:
                                        # 删除失败，回退到移动到冗余目录
                                        logger.warning(f"[organize] 自动删除失败，回退到移动: {old_name}")
                                        ok_old = Client115Service.move(cookies, [old_file["file_id"]], redundant_cid, context=old_name)
                                        if ok_old:
                                            result["redundant"].append({"name": old_name, "reason": f"洗版被替换: {wash_reason}"})
                                        else:
                                            result["errors"].append({"name": old_name, "error": "洗版替换：移走旧文件失败"})
                                    else:
                                        result["errors"].append({"name": old_name, "error": "自动删除失败且未配置冗余目录"})
                                elif redundant_cid:
                                    # 不满足自动删除条件 -> 移走旧文件到冗余目录
                                    logger.info(f"[organize] 洗版替换: 移走旧文件 '{old_name}' -> 冗余目录")
                                    ok_old = Client115Service.move(cookies, [old_file["file_id"]], redundant_cid, context=old_name)
                                    if ok_old:
                                        result["redundant"].append({"name": old_name, "reason": f"洗版被替换: {wash_reason}"})
                                    else:
                                        logger.warning(f"[organize] 移走旧文件失败: {old_name}")
                                        result["errors"].append({"name": old_name, "error": "洗版替换：移走旧文件失败"})
                                else:
                                    logger.warning(f"[organize] 无法处理旧文件（未配置冗余目录且未启用自动删除）: {old_name}")

                    if skip_move:
                        result["redundant"].append({"name": orig_name, "reason": skip_reason})
                        rename_results.append({"item": item, "skip_move": True, "skip_reason": "wash"})
                        continue

                    # 计算重命名
                    new_name, new_folder, season_folder = "", "", ""
                    is_clipped = False
                    if rename_rules:
                        if not tmdb_info:
                            logger.warning(f"[organize] 跳过重命名（无 TMDB 信息）: {orig_name}")
                        new_name, new_folder, season_folder = await cls._compute_rename(
                            rename_rules, orig_name, tmdb_info, media_type, media_info, skip_no_info, prefer_filename
                        )
                        logger.info(f"[organize] 重命名计算: orig='{orig_name}', new_name='{new_name}', folder='{new_folder}', season='{season_folder}'")
                        # 执行重命名
                        if new_name and new_name != orig_name:
                            ok_rename = Client115Service.rename(cookies, file_info["file_id"], new_name, context=orig_name)
                            if ok_rename:
                                renamed_to = new_name
                                # O4: 重命名结果超长智能裁剪（仅作用于最终文件名的结果记录，不改目录名）
                                clipped_renamed = _clip_filename(renamed_to)
                                if clipped_renamed != renamed_to:
                                    is_clipped = True
                                    renamed_to = clipped_renamed
                                logger.info(f"[organize] 重命名: {orig_name} -> {new_name}")
                            else:
                                logger.warning(f"[organize] 重命名失败: {orig_name}")
                                result["errors"].append({"name": orig_name, "error": "重命名失败"})
                                rename_results.append({"item": item, "skip_move": True, "skip_reason": "rename_failed"})
                                continue
                        elif new_name and new_name == orig_name:
                            logger.info(f"[organize] 文件名已是目标格式，无需重命名: {orig_name}")
                        else:
                            logger.warning(f"[organize] 无法计算新文件名（可能缺少 TMDB 信息或模板解析失败）: {orig_name}")

                    rename_results.append({
                        "item": item, "skip_move": False,
                        "renamed_to": renamed_to, "new_folder": new_folder,
                        "season_folder": season_folder, "media_info": media_info,
                        "sub_cid": sub_cid, "category": category,
                    })
                    if is_clipped:
                        rename_results[-1]["clipped"] = True

            logger.info(f"[organize] 阶段 1/2 完成：重命名和 ffprobe 探测结束")

            # #32: 文件命名对齐 - Phase 1 识别完成后、Phase 2 移动前，
            # 对同一电视剧同一季的剧集文件集调用对齐函数，将对齐结果合并到重命名映射
            _tv_align_groups: dict = defaultdict(list)
            for _rr in rename_results:
                if _rr.get("skip_move"):
                    continue
                _rr_item = _rr.get("item", {})
                if _rr_item.get("media_type") != "tv":
                    continue
                _show_folder = _rr.get("new_folder", "") or ""
                _season_folder = _rr.get("season_folder", "") or ""
                if not _show_folder:
                    continue
                _cur_name = _rr.get("renamed_to", "") or _rr_item.get("file", {}).get("name", "")
                _tv_align_groups[(_show_folder, _season_folder)].append((_rr, _cur_name))

            for (_show_folder, _season_folder), _members in _tv_align_groups.items():
                if len(_members) < 2:
                    continue
                _files_info = [{"name": _n} for (_r, _n) in _members]
                # 从 season_folder 提取季号供对齐回退使用
                _season_num = extract_season_only(_season_folder)
                if _season_num is not None:
                    for _fi in _files_info:
                        _fi["season"] = _season_num
                _align_map = align_episode_names(_files_info)
                if not _align_map:
                    continue
                logger.info(
                    f"[organize] #32 命名对齐: 剧目「{_show_folder}」季「{_season_folder}」"
                    f"需对齐 {len(_align_map)} 个文件"
                )
                for _rr, _cur_name in _members:
                    _aligned_name = _align_map.get(_cur_name)
                    if not _aligned_name or _aligned_name == _cur_name:
                        continue
                    _f_info = _rr["item"]["file"]
                    _ok_align = Client115Service.rename(
                        cookies, _f_info["file_id"], _aligned_name, context=_cur_name
                    )
                    if _ok_align:
                        logger.info(f"[organize] #32 命名对齐: {_cur_name} -> {_aligned_name}")
                        _rr["renamed_to"] = _aligned_name
                    else:
                        logger.warning(f"[organize] #32 命名对齐重命名失败: {_cur_name} -> {_aligned_name}")

            # ===== 阶段 2：统一移动所有文件 =====
            logger.info(f"[organize] 阶段 2/2：开始统一移动文件")

            # C1: 目标同名预检 — 移动前预检目标目录是否已有同名文件
            # 收集所有即将移动的文件的目标路径和文件名，批量查询目标目录查重
            # 同名文件（文件名相同但 SHA1 不同）移到「已存在影视的目录」而非覆盖
            target_name_map: dict[str, list[dict]] = {}  # "cid:filename" -> [rename_result, ...]
            for rr in rename_results:
                if rr.get("skip_move"):
                    continue
                item = rr["item"]
                file_info = item["file"]
                renamed_to = rr.get("renamed_to", "") or file_info["name"]
                sub_cid = rr.get("sub_cid", "")
                if sub_cid and renamed_to:
                    key = f"{sub_cid}:{renamed_to}"
                    if key not in target_name_map:
                        target_name_map[key] = []
                    target_name_map[key].append(rr)

            # 对有潜在重名的目标目录执行预检
            if target_name_map and existing_cid and not dry_run:
                # 收集需要查询的唯一 cid 集合
                check_cids = set()
                for k in target_name_map:
                    cid_part = k.split(":", 1)[0]
                    check_cids.add(cid_part)
                
                # 预检：列出每个目标目录已有文件名
                target_existing_names: dict[str, set] = {}  # cid -> {filename, ...}
                for cid in check_cids:
                    try:
                        existing_items = Client115Service.list_all_files_with_meta(
                            cookies, cid, VIDEO_EXTS, min_size=0, recursive=False
                        )
                        target_existing_names[cid] = {f["name"] for f in existing_items}
                        logger.info(f"[organize] C1 预检: 目标目录已有 {len(target_existing_names[cid])} 个文件")
                    except Exception as e:
                        logger.warning(f"[organize] C1 预检失败 cid={cid}: {e}")

                # 标记同名文件为 skip_move（移到已存在影视目录）
                for key, rrs in target_name_map.items():
                    cid_part, name_part = key.split(":", 1)
                    existing_names = target_existing_names.get(cid_part, set())
                    if name_part in existing_names:
                        for rr in rrs:
                            rr["skip_move"] = True
                            rr["skip_reason"] = "target_duplicate"
                            logger.info(f"[organize] C1 预检: 目标目录已有同名文件「{name_part}」，移到已存在影视目录")
            for rr in rename_results:
                if rr.get("skip_move"):
                    # 处理已存在文件的移动（SHA1 重复 或 C1 目标同名预检）
                    if rr.get("skip_reason") in ("existing", "target_duplicate"):
                        item = rr["item"]
                        file_info = item["file"]
                        orig_name = file_info["name"]
                        ok_exist = Client115Service.move(cookies, [file_info["file_id"]], existing_cid, context=orig_name)
                        if ok_exist:
                            reason_label = "目标同名" if rr.get("skip_reason") == "target_duplicate" else "已存在影视"
                            result["organized"].append({
                                "name": orig_name,
                                "category": item["category"],
                                "from": file_info.get("parent_path", ""),
                                "to": f"{reason_label}/{orig_name}",
                                "renamed_to": "",
                                "media_type": item.get("media_type", "movie"),
                            })
                        else:
                            result["errors"].append({"name": orig_name, "error": "移动到已存在影视目录失败"})
                    continue

                item = rr["item"]
                file_info = item["file"]
                tmdb_info = item.get("tmdb_info")
                media_type = item.get("media_type", "movie")
                orig_name = file_info["name"]
                renamed_to = rr.get("renamed_to", "")
                new_folder = rr.get("new_folder", "")
                season_folder = rr.get("season_folder", "")
                media_info = rr.get("media_info")
                sub_cid = rr.get("sub_cid", "")
                category = rr.get("category", item["category"])

                final_target_cid = sub_cid

                # 根据类型创建子目录并移动
                if rename_rules and tmdb_info:
                    if media_type == "tv" and season_folder:
                        tv_folder_name = new_folder or ""
                        if tmdb_info:
                            tv_folder_name = apply_rename_template(
                                rename_rules.get("tv_folder", "{first_letter}-{title} ({year})"),
                                tmdb_info, orig_name, None, None, None, None, media_info, rr.get("prefer_filename", prefer_filename)
                            ) or tv_folder_name
                        if tv_folder_name:
                            season_cid = Client115Service.ensure_path(cookies, [tv_folder_name, season_folder], sub_cid)
                            if season_cid:
                                final_target_cid = season_cid
                        else:
                            final_target_cid = sub_cid
                    elif media_type == "av":
                        av_folder_name = new_folder or ""
                        if av_folder_name:
                            av_cid = Client115Service.ensure_path(cookies, [av_folder_name], sub_cid)
                            if av_cid:
                                final_target_cid = av_cid
                    else:
                        # 电影
                        movie_folder_name = new_folder or ""
                        if tmdb_info:
                            movie_folder_name = apply_rename_template(
                                rename_rules.get("movie_folder", "{title} ({year})"),
                                tmdb_info, orig_name, None, None, None, None, media_info, rr.get("prefer_filename", prefer_filename)
                            ) or movie_folder_name
                        if movie_folder_name:
                            movie_cid = Client115Service.ensure_path(cookies, [movie_folder_name], sub_cid)
                            if movie_cid:
                                final_target_cid = movie_cid

                # #34: 整理覆盖检查 - 移动前检查目标目录是否已存在同名文件
                target_filename = renamed_to or orig_name
                overwrite_policy = _get_overwrite_policy()
                if final_target_cid and check_target_exists(cookies, final_target_cid, target_filename):
                    if dry_run:
                        # dry_run 模式下只记录不执行
                        result["errors"].append({
                            "name": orig_name,
                            "error": f"[预览] 目标目录已存在同名文件「{target_filename}」，"
                                     f"按 {overwrite_policy} 策略处理（未执行）",
                        })
                        logger.info(
                            f"[organize] #34 覆盖检查: [预览] 目标已存在「{target_filename}」，"
                            f"按 {overwrite_policy} 策略处理"
                        )
                        continue
                    if overwrite_policy == "skip":
                        # skip 策略：跳过已存在的同名文件，记录到错误列表
                        result["errors"].append({
                            "name": orig_name,
                            "error": f"目标目录已存在同名文件「{target_filename}」，按 skip 策略跳过移动",
                        })
                        logger.info(f"[organize] #34 覆盖检查: 目标已存在「{target_filename}」，skip 策略跳过")
                        continue
                    elif overwrite_policy == "replace":
                        # replace 策略：先删除目标同名文件再移动
                        _dup_fid = _find_target_file_id(cookies, final_target_cid, target_filename)
                        if _dup_fid:
                            _del_resp = Client115Service.delete_files(cookies, [_dup_fid])
                            if isinstance(_del_resp, dict) and not _del_resp.get("error"):
                                logger.info(
                                    f"[organize] #34 覆盖检查: replace 策略已删除目标同名文件「{target_filename}」"
                                )
                            else:
                                result["errors"].append({
                                    "name": orig_name,
                                    "error": f"replace 策略删除目标同名文件「{target_filename}」失败，跳过移动",
                                })
                                logger.warning(
                                    f"[organize] #34 覆盖检查: replace 删除失败「{target_filename}」"
                                )
                                continue
                        else:
                            # 未找到目标文件 id（可能已被其他流程处理），直接尝试移动
                            logger.info(
                                f"[organize] #34 覆盖检查: 未找到目标同名文件 id，继续移动「{target_filename}」"
                            )
                    elif overwrite_policy == "rename":
                        # rename 策略：生成唯一文件名，先重命名源文件再移动
                        unique_name = generate_unique_filename(cookies, final_target_cid, target_filename)
                        if unique_name and unique_name != target_filename:
                            _rn_ok = Client115Service.rename(
                                cookies, file_info["file_id"], unique_name, context=orig_name
                            )
                            if _rn_ok:
                                logger.info(
                                    f"[organize] #34 覆盖检查: rename 策略重命名「{target_filename}」->「{unique_name}」"
                                )
                                renamed_to = unique_name
                            else:
                                result["errors"].append({
                                    "name": orig_name,
                                    "error": f"rename 策略重命名「{target_filename}」失败，跳过移动",
                                })
                                logger.warning(
                                    f"[organize] #34 覆盖检查: rename 重命名失败「{target_filename}」"
                                )
                                continue
                        else:
                            logger.info(
                                f"[organize] #34 覆盖检查: rename 策略未生成新文件名，继续移动「{target_filename}」"
                            )

                # 执行移动
                ok = Client115Service.move(cookies, [file_info["file_id"]], final_target_cid, context=orig_name)
                if ok:
                    logger.info(f"[organize] 移动成功: {orig_name} -> {category}/{renamed_to or orig_name}")
                    season_num, episode_num = extract_season_episode(orig_name)
                    # 提取剧名用于汇总显示
                    tv_title = ""
                    if media_type == "tv" and tmdb_info:
                        tv_title = tmdb_info.get("name") or tmdb_info.get("title") or ""
                        if not tv_title and new_folder:
                            tv_title = new_folder
                    elif media_type == "tv" and new_folder:
                        tv_title = new_folder
                    result["organized"].append({
                        "name": orig_name,
                        "category": category,
                        "from": file_info.get("parent_path", ""),
                        "to": f"{category}/",
                        "renamed_to": renamed_to or "",
                        "media_type": media_type,
                        "season": season_num,
                        "episode": episode_num,
                        "tv_title": tv_title,
                    })

                    # 移动关联的媒体数据文件（字幕等）到视频所在目录
                    if data_files:
                        video_base = orig_name.rsplit(".", 1)[0] if "." in orig_name else orig_name
                        new_base = (renamed_to.rsplit(".", 1)[0] if renamed_to and "." in renamed_to
                                    else (renamed_to or video_base))
                        video_parent_id = file_info.get("parent_id", "")
                        for df in data_files:
                            if df["file_id"] in moved_data_file_ids:
                                continue
                            if df.get("parent_id", "") != video_parent_id:
                                continue
                            df_base = df["name"].rsplit(".", 1)[0] if "." in df["name"] else df["name"]
                            if df_base != video_base:
                                continue
                            # 视频被重命名时，数据文件同步重命名
                            if renamed_to and renamed_to != orig_name:
                                df_ext = ("." + df["name"].rsplit(".", 1)[-1]) if "." in df["name"] else ""
                                new_df_name = new_base + df_ext
                                if new_df_name != df["name"]:
                                    Client115Service.rename(cookies, df["file_id"], new_df_name, context=df["name"])
                                    logger.info(f"[organize] 关联数据文件重命名: {df['name']} -> {new_df_name}")
                            ok_df = Client115Service.move(cookies, [df["file_id"]], final_target_cid, context=df["name"])
                            if ok_df:
                                moved_data_file_ids.add(df["file_id"])
                                logger.info(f"[organize] 关联数据文件已移动: {df['name']} -> {category}/")
                            else:
                                logger.warning(f"[organize] 关联数据文件移动失败: {df['name']}")

                    # #21: 关联文件批量整理 - 查找并移动同名字幕/外挂音轨（补充 data_files 未覆盖的扩展名）
                    _video_parent_id = file_info.get("parent_id", "")
                    _same_dir_files = [f for f in all_files if f.get("parent_id", "") == _video_parent_id]
                    _linked_names = _find_linked_files(orig_name, _same_dir_files)
                    if _linked_names:
                        _video_base = orig_name.rsplit(".", 1)[0] if "." in orig_name else orig_name
                        _new_base = (renamed_to.rsplit(".", 1)[0] if renamed_to and "." in renamed_to
                                     else (renamed_to or _video_base))
                        for _lf_name in _linked_names:
                            _lf_dict = next((f for f in _same_dir_files if f.get("name") == _lf_name), None)
                            if not _lf_dict:
                                continue
                            _lf_file_id = _lf_dict.get("file_id", "")
                            if not _lf_file_id or _lf_file_id in moved_data_file_ids:
                                continue  # 已被 data_files 逻辑移动，跳过
                            # 视频被重命名时，关联文件同步重命名（保持同名）
                            if renamed_to and renamed_to != orig_name:
                                _lf_ext = ("." + _lf_name.rsplit(".", 1)[-1]) if "." in _lf_name else ""
                                _new_lf_name = _new_base + _lf_ext
                                if _new_lf_name != _lf_name:
                                    Client115Service.rename(cookies, _lf_file_id, _new_lf_name, context=_lf_name)
                                    logger.info(f"[organize] 关联文件重命名: {_lf_name} -> {_new_lf_name}")
                            ok_lf = Client115Service.move(cookies, [_lf_file_id], final_target_cid, context=_lf_name)
                            if ok_lf:
                                moved_data_file_ids.add(_lf_file_id)
                                logger.info(f"[organize] 关联文件已移动: {_lf_name} -> {category}/")
                            else:
                                logger.warning(f"[organize] 关联文件移动失败: {_lf_name}")
                else:
                    logger.warning(f"[organize] 移动失败: {orig_name}")
                    result["errors"].append({"name": orig_name, "error": "移动失败"})

            logger.info(f"[organize] 阶段 2/2 完成：文件移动结束")
        else:
            # 没有全量同步目录，只记录不移动
            logger.warning(f"[organize] 未配置全量同步目录（target_cid 为空），文件不会被移动")
            for item in to_organize:
                result["organized"].append({
                    "name": item["file"]["name"],
                    "category": item["category"],
                    "from": item["file"].get("parent_path", ""),
                    "to": "（未配置全量同步目录）",
                    "renamed_to": "",
                })

        # 3c. 移动冗余文件到「冗余文件存在的目录」
        if redundant_cid and to_redundant:
            for item in to_redundant:
                ok = Client115Service.move(
                    cookies, [item["file"]["file_id"]], redundant_cid,
                    context=item["file"]["name"],
                )
                if ok:
                    result["redundant"].append({
                        "name": item["file"]["name"],
                        "reason": item["reason"],
                    })
                else:
                    result["errors"].append({
                        "name": item["file"]["name"],
                        "error": "移动冗余文件失败",
                    })
        else:
            for item in to_redundant:
                result["redundant"].append({
                    "name": item["file"]["name"],
                    "reason": item["reason"],
                })

        # 3c. 移动无法识别的文件到「识别不准的目录」
        if unrecognized_cid and to_unrecognized:
            for item in to_unrecognized:
                ok = Client115Service.move(
                    cookies, [item["file"]["file_id"]], unrecognized_cid,
                    context=item["file"]["name"],
                )
                if ok:
                    result["unrecognized"].append({
                        "name": item["file"]["name"],
                        "reason": item["reason"],
                    })
                else:
                    result["errors"].append({
                        "name": item["file"]["name"],
                        "error": "移动无法识别文件失败",
                    })
        else:
            for item in to_unrecognized:
                result["unrecognized"].append({
                    "name": item["file"]["name"],
                    "reason": item["reason"],
                })

        # 3d. 移动媒体图片文件和未关联的媒体数据文件到「冗余文件存在的目录」
        #     - 媒体图片（jpg/png 等）：Emby 可自动生成，移到冗余
        #     - 未关联的媒体数据（未跟随视频移动的字幕等）：移到冗余
        if not dry_run and redundant_cid:
            for img_f in image_files:
                try:
                    ok_img = Client115Service.move(cookies, [img_f["file_id"]], redundant_cid, context=img_f["name"])
                    if ok_img:
                        logger.info(f"[organize] 媒体图片移到冗余: {img_f['name']}")
                        result["redundant"].append({"name": img_f["name"], "reason": "媒体图片文件"})
                    else:
                        logger.warning(f"[organize] 媒体图片移动失败: {img_f['name']}")
                except Exception as e:
                    logger.warning(f"[organize] 媒体图片移动异常: {img_f['name']}: {e}")

            for df in data_files:
                if df["file_id"] in moved_data_file_ids:
                    continue
                try:
                    ok_df = Client115Service.move(cookies, [df["file_id"]], redundant_cid, context=df["name"])
                    if ok_df:
                        logger.info(f"[organize] 未关联媒体数据移到冗余: {df['name']}")
                        result["redundant"].append({"name": df["name"], "reason": "未关联的媒体数据文件"})
                    else:
                        logger.warning(f"[organize] 未关联媒体数据移动失败: {df['name']}")
                except Exception as e:
                    logger.warning(f"[organize] 未关联媒体数据移动异常: {df['name']}: {e}")

        logger.info(
            f"已整理 {len(result['organized'])}，冗余 {len(result['redundant'])}，"
            f"无法识别 {len(result['unrecognized'])}，失败 {len(result['errors'])}"
        )

        # 3e. 电视剧整理汇总：按剧集分组，显示每部剧的季集范围和重命名情况
        tv_items = [item for item in result["organized"] if item.get("media_type") == "tv"]
        if tv_items:
            # 按 category（分类路径）分组，同一分类下的同一部剧归为一组
            # 用 renamed_to 的目录部分或 name 的标题部分作为剧集标识
            tv_groups = defaultdict(list)
            for item in tv_items:
                # 用 to 路径的目录部分作为分组 key（同一目录 = 同一部剧的同一季）
                group_key = item.get("to", "").rstrip("/")
                tv_groups[group_key].append(item)

            tv_summary = []
            for group_key, episodes in sorted(tv_groups.items()):
                # 按季号分组
                season_groups = defaultdict(list)
                for ep in episodes:
                    s = ep.get("season") or 1
                    season_groups[s].append(ep)

                for season in sorted(season_groups.keys()):
                    eps = season_groups[season]
                    # 提取集号范围
                    ep_nums = [e.get("episode") for e in eps if e.get("episode") is not None]
                    ep_count = len(eps)
                    renamed_count = sum(1 for e in eps if e.get("renamed_to"))

                    if ep_nums:
                        ep_min, ep_max = min(ep_nums), max(ep_nums)
                        if ep_min == ep_max:
                            ep_range = f"S{season:02d}E{ep_min:02d}"
                        else:
                            ep_range = f"S{season:02d}E{ep_min:02d}-E{ep_max:02d}"
                    else:
                        ep_range = f"S{season:02d}（{ep_count}集）"

                    # 取第一个文件作为示例
                    sample = eps[0]
                    # 提取剧名（取所有集中的第一个有 tv_title 的）
                    tv_title = ""
                    for e in eps:
                        if e.get("tv_title"):
                            tv_title = e["tv_title"]
                            break
                    summary_item = {
                        "path": group_key,
                        "tv_title": tv_title,
                        "season": season,
                        "episode_range": ep_range,
                        "episode_count": ep_count,
                        "renamed_count": renamed_count,
                        "sample_original": sample.get("name", ""),
                        "sample_renamed": sample.get("renamed_to", ""),
                    }
                    tv_summary.append(summary_item)

                    title_display = f"{tv_title} " if tv_title else ""
                    logger.info(
                        f"[organize] 电视剧汇总: {title_display}{ep_range} "
                        f"({ep_count}集, 重命名{renamed_count}个) → {group_key}"
                    )
                    if sample.get("renamed_to"):
                        logger.info(f"  示例: {sample['name']} → {sample['renamed_to']}")

            result["tv_summary"] = tv_summary
            logger.info(f"[organize] 电视剧整理汇总: 共 {len(tv_summary)} 组")

        # 4. 清理源目录：将残留的子目录和散落文件移到冗余目录（仅非预览模式）
        if not dry_run and result["total"] > 0 and redundant_cid:
            try:
                # 收集本次整理涉及的所有文件 ID（成功/失败/冗余/无法识别都算），
                # 清理残留时排除它们，避免"整理失败但仍在源目录"的文件被误当残留移走
                involved_ids: set = set()
                for _group in (to_organize, to_redundant, to_unrecognized):
                    for _it in _group:
                        _fid = str(_it.get("file", {}).get("file_id", ""))
                        if _fid:
                            involved_ids.add(_fid)
                # 重命名/移动阶段已处理的文件 ID 也纳入保护
                for _rr in rename_results:
                    _fid = str(_rr.get("item", {}).get("file", {}).get("file_id", ""))
                    if _fid:
                        involved_ids.add(_fid)

                logger.info(f"[organize] 开始清理源目录残留...")
                # 扫描源目录下的直接子项（目录和文件）
                remaining_items = Client115Service.list_all_items(
                    cookies, source_cid, recursive=False
                )
                if remaining_items:
                    moved = 0
                    failed_items = []  # 记录第一轮移动失败的项
                    for item in remaining_items:
                        item_id = str(item.get("id", ""))
                        item_name = item.get("name", "")
                        if not item_id:
                            continue
                        # 跳过本次整理涉及的文件（避免误移整理失败/待重试的文件）
                        if item_id in involved_ids:
                            logger.info(f"[organize] 残留清理跳过整理中文件: {item_name}")
                            continue
                        try:
                            ok = Client115Service.move(cookies, [item_id], redundant_cid, context=item_name)
                            if ok:
                                moved += 1
                                logger.info(f"[organize] 残留移到冗余目录: {item_name}")
                            else:
                                failed_items.append(item)
                        except Exception as e:
                            logger.warning(f"[organize] 残留移动异常: {item_name}: {e}")
                            failed_items.append(item)
                        _time.sleep(_organize_write_interval())
                    # 第二轮：重试第一轮失败的项（等待更长时间）
                    if failed_items:
                        logger.info(f"[organize] {len(failed_items)} 个残留项移动失败，等待冷却后重试...")
                        _time.sleep(_organize_retry_cooldown())
                        still_failed = []
                        for item in failed_items:
                            item_id = item.get("id", "")
                            item_name = item.get("name", "")
                            if str(item_id) in involved_ids:
                                logger.info(f"[organize] 残留重试跳过整理中文件: {item_name}")
                                continue
                            try:
                                ok = Client115Service.move(cookies, [item_id], redundant_cid, context=item_name)
                                if ok:
                                    moved += 1
                                    logger.info(f"[organize] 残留重试成功: {item_name}")
                                else:
                                    still_failed.append(item)
                                    logger.warning(f"[organize] 残留重试仍失败: {item_name}")
                            except Exception as e:
                                still_failed.append(item)
                                logger.warning(f"[organize] 残留重试异常: {item_name}: {e}")
                            _time.sleep(_organize_write_interval())
                        # 仍然失败的项记录到结果中
                        if still_failed:
                            result["cleanup_failed"] = [
                                {"name": item.get("name", ""), "id": item.get("id", "")}
                                for item in still_failed
                            ]
                            logger.warning(f"[organize] {len(still_failed)} 个残留项移动失败，需手动处理")
                    if moved > 0:
                        result["cleaned_items"] = moved
                        logger.info(f"[organize] 已将 {moved} 个残留项移到冗余目录")
                else:
                    logger.info(f"[organize] 源目录已清空")
            except Exception as e:
                logger.warning(f"[organize] 清理源目录失败: {e}")

        # 完成日志（含总耗时）
        organized = len(result.get("organized", []))
        redundant = len(result.get("redundant", []))
        unrecognized = len(result.get("unrecognized", []))
        errors = len(result.get("errors", []))
        logger.info(
            f"[organize] 整理完成: 成功 {organized}, 冗余 {redundant}, "
            f"无法识别 {unrecognized}, 失败 {errors}, "
            f"耗时 {_time.time() - _organize_start_ts:.1f}s"
        )

        # #23: 发布 ORGANIZE_COMPLETED 事件到事件总线
        try:
            from app.core.event_bus import get_event_bus, EventBus
            get_event_bus().publish(EventBus.ORGANIZE_COMPLETED, {
                "summary": f"成功 {organized}, 冗余 {redundant}, 无法识别 {unrecognized}, 失败 {errors}",
                "total": result.get("total", 0),
                "organized": organized,
                "redundant": redundant,
                "unrecognized": unrecognized,
                "errors": errors,
                "dry_run": dry_run,
            })
        except Exception:
            pass

        return result

    @classmethod
    async def _check_wash_replace(
        cls,
        cookies: str,
        target_cid: str,
        file_info: dict,
        tmdb_info: Optional[dict],
        media_type: str,
        wash_config: dict,
        media_info: Optional[dict] = None,
        category: str = "",
        prefer_filename: bool = False,
        cached_existing_files: Optional[list] = None,
    ) -> tuple:
        """
        洗版检查：在目标目录中查找同标题的已存在文件，按 YAML 策略比较。
        wash_config 应包含 'wash_yaml' 字段（YAML 格式策略字符串）。
        cached_existing_files: 预先获取的目标目录文件列表（含 sha1），避免重复 API 调用。
        返回 (should_move: bool, reason: str, old_files_to_replace: list)
        old_files_to_replace: 需要被替换的旧文件列表
        """
        try:
            wash_yaml = ""
            if isinstance(wash_config, dict):
                wash_yaml = wash_config.get("wash_yaml", "") or wash_config.get("enabled_yaml", "")
            if not wash_yaml:
                return True, "未配置洗版策略", []

            strategies = parse_wash_strategies(wash_yaml)
            if not strategies:
                return True, "洗版策略为空，跳过洗版", []

            # 获取目标目录下所有文件（优先使用缓存）
            if cached_existing_files is not None:
                existing_files = cached_existing_files
            else:
                existing_files = Client115Service.list_all_files(
                    cookies, target_cid, VIDEO_EXTS, min_size=0, recursive=True
                )
            if not existing_files:
                return True, "目标目录无重复文件", []

            new_info = parse_resource_info(file_info["name"], media_info, prefer_filename)
            new_info["size"] = file_info.get("size", 0)
            new_sha1 = file_info.get("sha1", "")

            # 为新文件找到匹配的策略
            new_strategy = _find_matching_strategy(new_info, media_type, category, strategies)
            if not new_strategy:
                return True, f"无匹配洗版策略（media_type={media_type}）", []

            logger.info(f"[wash] 文件 '{file_info['name']}' 匹配策略 '{new_strategy.get('_name', '')}' (mode={new_strategy.get('mode', '')})")

            scope = new_strategy.get("scope", "all")
            old_files_to_replace = []

            for ef in existing_files:
                # 洗版前置条件：仅 SHA1 相同的文件才进行洗版比较
                old_sha1 = ef.get("sha1", "")
                if not new_sha1 or not old_sha1 or new_sha1 != old_sha1:
                    continue

                old_info = parse_resource_info(ef["name"], None, prefer_filename)
                old_info["size"] = ef.get("size", 0)

                if scope == "group":
                    # group 模式：旧文件也需要匹配同策略的 priority_level
                    old_strategy = _find_matching_strategy(old_info, media_type, category, strategies)
                    if not old_strategy or old_strategy.get("_name") != new_strategy.get("_name"):
                        continue  # 不同组，跳过

                should_replace, reason = should_replace_wash(new_info, old_info, new_strategy)
                if should_replace:
                    # 旧文件需要被替换，加入列表
                    old_files_to_replace.append(ef)
                else:
                    return False, f"{new_strategy.get('_name', '')}: {reason}", []

            return True, f"策略 '{new_strategy.get('_name', '')}' 检查通过", old_files_to_replace
        except Exception as e:
            logger.warning(f"洗版检查失败: {e}")
            return True, f"洗版检查异常: {e}", []

    @classmethod
    async def _compute_rename(
        cls,
        rename_rules: dict,
        filename: str,
        tmdb_info: Optional[dict],
        media_type: str,
        media_info: Optional[dict] = None,
        skip_no_info: bool = False,
        prefer_filename: bool = False,
    ) -> tuple:
        """
        根据重命名规则计算新文件名和文件夹名。
        media_info: ffprobe 探测结果，用于补充文件名解析不到的资源信息。
        skip_no_info: 为 True 时，若文件名和 ffprobe 都未提供资源信息（分辨率/编码等），
            则跳过重命名，返回 (None, None, None)。
        prefer_filename: True=文件名优先，仅补充缺失字段；False=ffprobe优先，探测值覆盖文件名值。
        返回 (new_filename, folder_name, season_folder_name)。
        若无法重命名（无 TMDB 信息），返回 (None, None, None)。
        """
        # AV 文件：用番号命名，不需要 TMDB 信息
        if media_type == "av":
            av_code = ""
            if tmdb_info and isinstance(tmdb_info, dict):
                av_code = tmdb_info.get("av_code", "")
            if not av_code:
                av_code = extract_av_code(filename) or ""
            if not av_code:
                return (None, None, None)

            # 检查资源信息是否充足
            if skip_no_info:
                res_info = parse_resource_info(filename, media_info, prefer_filename)
                key_fields = ["resource_pix", "video_encode", "audio_encode", "fps"]
                has_any_info = any(res_info.get(f) for f in key_fields)
                if not has_any_info:
                    logger.info(f"[organize] AV 跳过重命名（无资源信息）: {filename}")
                    return (None, None, None)

            # AV 文件命名模板
            template = rename_rules.get("av_file", "") or "{code}<.{resource_pix}><.{resource_type}><.{video_encode}><.{audio_encode}><-{resource_team}>{ext}"
            new_name = apply_rename_template(
                template, {"av_code": av_code}, filename, None, None, None, None, media_info, prefer_filename
            )
            # AV 文件夹名 = 番号
            folder_template = rename_rules.get("av_folder", "") or "{code}"
            folder_name = apply_rename_template(
                folder_template, {"av_code": av_code}, filename, None, None, None, None, media_info, prefer_filename
            )
            return (new_name, folder_name, None)

        if not rename_rules or not tmdb_info:
            return (None, None, None)

        # 检查资源信息是否充足（文件名 + ffprobe 合并后）
        if skip_no_info:
            res_info = parse_resource_info(filename, media_info, prefer_filename)
            key_fields = ["resource_pix", "video_encode", "audio_encode", "fps"]
            has_any_info = any(res_info.get(f) for f in key_fields)
            if not has_any_info:
                logger.info(f"[organize] 跳过重命名（文件名和 ffprobe 均无资源信息）: {filename}")
                return (None, None, None)

        season, episode = extract_season_episode(filename)

        # 获取季集详情（仅剧集）
        season_detail = None
        episode_detail = None
        if media_type == "tv" and tmdb_info.get("id"):
            tmdb_id = tmdb_info["id"]
            if season is not None:
                season_detail = await TmdbService.get_season_detail(tmdb_id, season)
            if season is not None and episode is not None:
                episode_detail = await TmdbService.get_episode_detail(
                    tmdb_id, season, episode
                )

        if media_type == "tv":
            # 剧集文件（apply_rename_template 已原生支持 {season_num:02d} 等零填充格式）
            template = rename_rules.get("episode_file", "") or "{title}.S{season_num:02d}<E{episode_num:02d}><.{episode_name}><.{resource_pix}><.{fps}><.{resource_version}><.{resource_source}><.{resource_type}><.{resource_effect}><.{video_encode}><.{audio_encode}><.{resource_team}>{ext}"
            new_name = apply_rename_template(
                template, tmdb_info, filename, season, episode, season_detail, episode_detail, media_info, prefer_filename
            )
            # 季文件夹
            season_template = rename_rules.get("season_folder", "") or "Season {season_num:02d}"
            season_folder = apply_rename_template(
                season_template, tmdb_info, filename, season or 1, None, season_detail, None, media_info, prefer_filename
            )
            if not season_folder:
                season_folder = f"Season {(season or 1):02d}"
            return (new_name, None, season_folder)
        else:
            # 电影文件
            template = rename_rules.get("movie_file", "") or "{title}.{year}<.{resource_pix}><.{fps}><.{resource_version}><.{resource_source}><.{resource_type}><.{resource_effect}><.{video_encode}><.{audio_encode}><-{resource_team}>{ext}"
            new_name = apply_rename_template(
                template, tmdb_info, filename, None, None, None, None, media_info, prefer_filename
            )
            # 电影文件夹
            folder_template = rename_rules.get("movie_folder", "") or "{first_letter}-{title}-{year}-[[tmdb={tmdb_id}]]"
            movie_folder = apply_rename_template(
                folder_template, tmdb_info, filename, None, None, None, None, media_info, prefer_filename
            )
            return (new_name, movie_folder, None)

    @classmethod
    async def _ai_extract_and_search(
        cls,
        filename: str,
        media_type: Optional[str] = None,
    ) -> Optional[tuple]:
        """
        用 AI 从文件名提取标题，再用 TMDB 搜索元数据。
        返回 (tmdb_info, media_type)，失败返回 None。
        """
        try:
            from app.services.ai_service import get_ai_client
            client = get_ai_client()
            if client is None:
                logger.warning("[organize] AI 服务未配置或未启用，跳过 AI 辅助识别")
                return None

            # 先用 AI 提取标题信息
            # 如果已知 media_type 就用对应方法，否则先试电影再试电视剧
            if media_type == "tv":
                ai_info = await client.extract_tv_name(filename)
            elif media_type == "movie":
                ai_info = await client.extract_movie_name(filename)
            else:
                # 未知类型：先试电影，再试电视剧
                ai_info = await client.extract_movie_name(filename)
                if not ai_info or not ai_info.get("name"):
                    ai_info = await client.extract_tv_name(filename)

            if not ai_info or not ai_info.get("name"):
                logger.warning(f"[organize] AI 未能从文件名提取标题: {filename}")
                return None

            ai_name = ai_info["name"]
            ai_year = ai_info.get("year")

            # 用 AI 提取的标题重新搜索 TMDB
            # 构造搜索用的标题（带年份）
            search_name = ai_name
            if ai_year:
                search_name = f"{ai_name}.{ai_year}"

            # 判断类型：如果 AI 返回了 season/episode，按电视剧搜索
            if ai_info.get("season") is not None or ai_info.get("episode") is not None:
                search_type = "tv"
            elif media_type:
                search_type = media_type
            else:
                search_type = None

            tmdb_info = await TmdbService.search_media(search_name, search_type)
            if tmdb_info:
                # 确定最终类型
                if "release_date" in tmdb_info and tmdb_info.get("release_date"):
                    final_type = "movie"
                elif "first_air_date" in tmdb_info and tmdb_info.get("first_air_date"):
                    final_type = "tv"
                else:
                    final_type = search_type or "movie"
                return (tmdb_info, final_type)

            logger.warning(f"[organize] AI 提取标题 '{ai_name}' 在 TMDB 中未找到结果")
            return None

        except Exception as e:
            logger.warning(f"[organize] AI 辅助识别异常: {e}")
            return None

    @staticmethod
    def _extract_parent_title(parent_path: str) -> Optional[str]:
        """
        从父目录路径中提取有效的搜索标题（向上遍历，跳过纯季号/技术标记目录）。
        例如 parent_path="A-愛你 (2025)/Season 01" → "A-愛你 (2025)"
        返回第一个有实质内容的目录名，找不到返回 None。
        """
        if not parent_path:
            return None
        parts = [p.strip() for p in parent_path.split("/") if p.strip()]
        # 从最近一级（最深层）向上遍历
        for part in reversed(parts):
            # 跳过纯季号目录：Season N / 第N季 / S0N
            if extract_season_only(part) is not None:
                continue
            # 跳过纯集号目录
            if re.match(r'^[eE][pP]?\d{1,3}$', part):
                continue
            # 跳过纯数字目录
            if re.match(r'^\d+$', part):
                continue
            # 跳过仅含技术标记的目录（如 2160p, WEB-DL 等纯资源标签）
            cleaned = re.sub(
                r'[._\s]?(1080i|1080p|720i|720p|480i|480p|2160i|2160p|4k|bluray|blu-ray|webrip|web-dl|web|hd|h264|h265|x264|x265|hevc|aac|dts|hdr|atmos|remux|uhd|hdtv|dvd|bdrip|brrip)[._\s]?',
                '', part, flags=re.IGNORECASE
            ).strip()
            cleaned = re.sub(r'[._\-\s]', '', cleaned)
            if not cleaned:
                continue
            # 跳过用户自定义根目录名（电影/电视剧/AV 等）
            if part in ('电影', '电视剧', 'AV', 'Movies', 'TV', 'TV Shows'):
                continue
            return part
        return None

    @classmethod
    async def _classify_file(
        cls,
        name: str,
        category_helper: CategoryHelper,
        category_roots: dict = None,
        ai_mode: str = "off",
        parent_path: str = "",
    ) -> tuple:
        """
        分类文件：通过 TMDB 搜索影视元数据，再按 YAML 分类配置匹配。
        AV 文件通过番号前缀匹配厂商分类，不查询 TMDB。
        返回 (category_str, tmdb_info, media_type)。
        category_str 为分类路径（如 "电影/动画电影"），无法识别返回 (None, None, None)。
        category_roots: 自定义根目录名称 {"movie":"电影", "tv":"电视剧", "av":"AV"}
        ai_mode: off=关闭AI, assist=TMDB失败时辅助AI, force=强制使用AI
        parent_path: 文件所在父目录路径（如 "A-愛你 (2025)/Season 01"），用于回退识别
        """
        # 根目录名称（支持用户自定义）
        roots = category_roots or {}
        movie_root = roots.get("movie", "") or "电影"
        tv_root = roots.get("tv", "") or "电视剧"
        av_root = roots.get("av", "") or "AV"

        # 先用内置逻辑判断电影/电视剧/AV
        builtin = auto_classify(name)

        # AV 文件：通过番号分类，不查询 TMDB
        if builtin == "av":
            av_code = extract_av_code(name)
            if av_code:
                sub_category = category_helper.get_av_category(av_code)
                if sub_category:
                    return (f"{av_root}/{sub_category}", {"av_code": av_code}, "av")
                return (av_root, {"av_code": av_code}, "av")
            return (None, None, None)

        # ===== 强制 AI 模式：跳过内置正则识别，直接用 AI 提取标题 =====
        if ai_mode == "force":
            ai_result = await cls._ai_extract_and_search(name, None)
            if ai_result:
                tmdb_info, media_type = ai_result
                if media_type == "movie":
                    sub_category = category_helper.get_movie_category(tmdb_info)
                    return (f"{movie_root}/{sub_category}" if sub_category else movie_root, tmdb_info, "movie")
                elif media_type == "tv":
                    sub_category = category_helper.get_tv_category(tmdb_info)
                    return (f"{tv_root}/{sub_category}" if sub_category else tv_root, tmdb_info, "tv")
            # AI 也失败 → 尝试用父目录名回退搜索
            parent_title = cls._extract_parent_title(parent_path)
            if parent_title:
                logger.info(f"[organize] 强制AI失败 '{name}'，尝试父目录名: '{parent_title}'")
                tmdb_info = await TmdbService.search_media(parent_title, None)
                if tmdb_info:
                    logger.info(f"[organize] 父目录名搜索成功: '{parent_title}' -> {tmdb_info.get('title') or tmdb_info.get('name', '')}")
                    if "release_date" in tmdb_info and tmdb_info.get("release_date"):
                        sub_category = category_helper.get_movie_category(tmdb_info)
                        return (f"{movie_root}/{sub_category}" if sub_category else movie_root, tmdb_info, "movie")
                    elif "first_air_date" in tmdb_info and tmdb_info.get("first_air_date"):
                        sub_category = category_helper.get_tv_category(tmdb_info)
                        return (f"{tv_root}/{sub_category}" if sub_category else tv_root, tmdb_info, "tv")
            # TMDB 未找到 → 移到识别不准的目录
            logger.warning(f"[organize] 强制AI模式识别失败: '{name}'，移到识别不准的目录")
            return (None, None, None)

        # ===== 正常模式 / 辅助 AI 模式 =====
        if builtin == "movie":
            media_type = "movie"
        elif builtin == "tvshow":
            media_type = "tv"
        else:
            # 无法判断类型，尝试用 TMDB 搜索（自动判断类型）
            media_type = None

        # 通过 TMDB 搜索元数据
        tmdb_info = await TmdbService.search_media(name, media_type)

        if not tmdb_info:
            # TMDB 未找到 → 尝试用父目录名回退搜索
            parent_title = cls._extract_parent_title(parent_path)
            if parent_title:
                logger.info(f"[organize] 文件名搜索失败 '{name}'，尝试父目录名: '{parent_title}'")
                tmdb_info = await TmdbService.search_media(parent_title, media_type)
                if tmdb_info:
                    logger.info(f"[organize] 父目录名搜索成功: '{parent_title}' -> {tmdb_info.get('title') or tmdb_info.get('name', '')}")

        if not tmdb_info:
            # TMDB 未找到
            # 辅助 AI 模式：用 AI 提取标题后重新搜索 TMDB
            if ai_mode == "assist":
                logger.info(f"[organize] TMDB 未找到 '{name}'，尝试 AI 辅助识别...")
                ai_result = await cls._ai_extract_and_search(name, media_type)
                if ai_result:
                    tmdb_info, media_type = ai_result
                    logger.info(f"[organize] AI 辅助识别成功: '{name}' -> {tmdb_info.get('title') or tmdb_info.get('name', '')}")

            if not tmdb_info:
                # TMDB 未找到 → 移到识别不准的目录
                logger.warning(f"[organize] TMDB 未找到 '{name}'，移到识别不准的目录")
                return (None, None, None)

        # 置信度评估（参考 qmediasync score 字段）：
        # TMDB 返回的候选可能不是目标影视（如重名/同名不同作品），
        # 评分低于阈值时移到识别不准目录，而不是盲选造成错误整理。
        from app.services.tmdb_service import CONFIDENCE_THRESHOLD
        confidence = tmdb_info.get("_confidence", 1.0)
        if confidence < CONFIDENCE_THRESHOLD:
            best_cand = (tmdb_info.get("_candidates") or [{}])[0]
            cand_title = best_cand.get("title", "")
            cand_year = best_cand.get("year", "")
            logger.warning(
                f"[organize] TMDB 匹配置信度过低 {confidence:.2f} '{name}' "
                f"(候选: '{cand_title}' {cand_year})，移到识别不准的目录"
            )
            return (None, None, None)

        # 判断是电影还是电视剧
        # TMDB 搜索结果中有 release_date 的是电影，有 first_air_date 的是电视剧
        if "release_date" in tmdb_info and tmdb_info.get("release_date"):
            # 电影
            sub_category = category_helper.get_movie_category(tmdb_info)
            if sub_category:
                return (f"{movie_root}/{sub_category}", tmdb_info, "movie")
            return (movie_root, tmdb_info, "movie")
        elif "first_air_date" in tmdb_info and tmdb_info.get("first_air_date"):
            # 电视剧
            sub_category = category_helper.get_tv_category(tmdb_info)
            if sub_category:
                return (f"{tv_root}/{sub_category}", tmdb_info, "tv")
            return (tv_root, tmdb_info, "tv")
        else:
            # 无法确定类型，按内置判断回退
            if builtin == "movie":
                sub_category = category_helper.get_movie_category(tmdb_info)
                return (f"{movie_root}/{sub_category}" if sub_category else movie_root, tmdb_info, "movie")
            elif builtin == "tvshow":
                sub_category = category_helper.get_tv_category(tmdb_info)
                return (f"{tv_root}/{sub_category}" if sub_category else tv_root, tmdb_info, "tv")
            return (None, None, None)


