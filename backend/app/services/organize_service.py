"""
整理服务 - 扫描 115 网盘目录，通过 TMDB 识别影视信息，检测重复，移动分类
支持基于 TMDB 元数据的二级分类（YAML 配置），整理结果保存到整理后目录
"""
import re
from typing import Optional
from collections import defaultdict

from app.services.client_115 import Client115Service
from app.services.tmdb_service import TmdbService
from app.services.category_helper import CategoryHelper
from app.services.media_probe import probe_media_info_async, is_ffprobe_available
from app.core.logbuffer import get_logger

logger = get_logger("app.services.organize_service")

# 视频扩展名
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".m2ts", ".rmvb", ".iso"}

# 冗余关键词（小写匹配）
REDUNDANT_KEYWORDS = {"sample", "预告", "花絮", "番外", "特典", "menu", "extras", "bonus", "trailer", "featurette"}


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
    """判断文件是否为冗余文件（sample、预告等）"""
    lower = name.lower()
    return any(kw in lower for kw in REDUNDANT_KEYWORDS)


def extract_title(name: str) -> str:
    """
    从文件名中提取标题（去除扩展名、分辨率、编码等标记）
    用于重复检测。
    AV 文件以番号作为去重 key。
    """
    # AV 文件：用番号作为去重 key
    av_code = extract_av_code(name)
    if av_code:
        return av_code.lower()
    # 去扩展名
    base = re.sub(r'\.[^.]+$', '', name)
    # 去方括号内容（发布组、集号、技术信息、字幕信息等）
    base = re.sub(r'\[[^\]]*\]', ' ', base)
    # 去分辨率/编码标记
    base = re.sub(r'[._\s]?(1080p|720p|480p|2160p|4k|bluray|webrip|web-dl|h264|h265|x264|x265|hevc|aac|dts|hdr|atmos|remux|srt|ass|ssa|cr|hevc)[._\s]?', '', base, flags=re.IGNORECASE)
    # 去 SxxExx
    base = re.sub(r'[._\s]?[sS]\d{1,2}[eE]\d{1,3}[._\s]?', '', base)
    # 去单独的 Sxx（季号，无集号）
    base = re.sub(r'(?:^|[._\s])[sS]\d{1,2}(?![eE]\d)(?:[._\s]|$)', ' ', base)
    # 去 Season N（英文季号写法）
    base = re.sub(r'[._\s]?[sS]eason\s*\d{1,2}[._\s]?', ' ', base)
    # 去年份
    base = re.sub(r'[（(]?\s*(19|20)\d{2}\s*[）)]?', '', base)
    # 去全角标点
    base = re.sub(r'[？！：；]', '', base)
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

    # 分辨率 resource_pix
    pix_match = re.search(r'[._\s](2160p|1080p|720p|480p|4k|4K)[._\s]', base, re.IGNORECASE)
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

    # Step 1: 转义 [[ ]] → 临时占位符（之后恢复为 { }）
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


class OrganizeService:
    """网盘文件整理服务"""

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
        rename_rules: dict = None,
        wash_config: dict = None,
        use_ffprobe: bool = False,
        skip_no_info: bool = False,
        prefer_filename: bool = False,
        min_organize_size_mb: int = 0,
        organize_blacklist: str = "",
        dry_run: bool = False,
        progress_callback=None,
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
        result = {
            "total": 0,
            "organized": [],
            "redundant": [],
            "unrecognized": [],
            "errors": [],
            "dry_run": dry_run,
        }

        logger.info(f"[organize] 整理开始: source_cid={source_cid}, target_cid={target_cid}, existing_cid={existing_cid}, redundant_cid={redundant_cid}, unrecognized_cid={unrecognized_cid}")

        # 初始化分类引擎
        try:
            category_helper = CategoryHelper(classify_config) if classify_config else CategoryHelper()
        except ValueError as e:
            logger.warning(f"[organize] 分类配置解析失败，使用默认配置: {e}")
            category_helper = CategoryHelper()

        # 1. 扫描源目录所有文件
        logger.info(f"[organize] 正在扫描源目录文件...")
        all_files = Client115Service.list_all_files(
            cookies, source_cid, VIDEO_EXTS, min_size=0, recursive=True
        )

        # 按最小视频大小过滤
        min_size_bytes = min_organize_size_mb * 1024 * 1024 if min_organize_size_mb > 0 else 0
        if min_size_bytes > 0:
            before_count = len(all_files)
            all_files = [f for f in all_files if f.get("size", 0) >= min_size_bytes]
            skipped = before_count - len(all_files)
            if skipped > 0:
                logger.info(f"[organize] 最小视频大小过滤: 跳过 {skipped} 个小于 {min_organize_size_mb}MB 的文件")

        # 按整理黑名单（正则）过滤
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
                before_count = len(all_files)
                all_files = [f for f in all_files if not any(p.search(f.get("name", "")) for p in blacklist_patterns)]
                skipped = before_count - len(all_files)
                if skipped > 0:
                    logger.info(f"[organize] 黑名单过滤: 跳过 {skipped} 个匹配黑名单的文件")

        result["total"] = len(all_files)
        logger.info(f"[organize] 扫描完成: 共 {len(all_files)} 个视频文件")
        if not all_files:
            logger.info(f"[organize] 源目录无匹配文件，整理结束")
            return result

        # 2. 分类：冗余文件 / 可识别 / 不可识别
        # 同时按标题去重
        title_map = defaultdict(list)  # title -> [file_info, ...]
        for f in all_files:
            f["_title"] = extract_title(f["name"])
            title_map[f["_title"]].append(f)

        to_organize = []   # 可整理（电影/剧集）
        to_redundant = []  # 冗余/重复
        to_unrecognized = []  # 无法识别

        logger.info(f"[organize] 开始分类识别 ({len(title_map)} 个标题)...")

        _idx = 0
        for title, files in title_map.items():
            _idx += 1
            if progress_callback:
                try:
                    await progress_callback(_idx, len(title_map), title)
                except Exception:
                    pass
            if len(files) > 1:
                # 同标题多文件：保留第一个，其余视为重复
                keep = files[0]
                dups = files[1:]
                # 保留的文件正常分类
                category, tmdb_info, media_type = await cls._classify_file(keep["name"], category_helper)
                if category is None:
                    logger.warning(f"[organize] 无法识别: {keep['name']}")
                    to_unrecognized.append({"file": keep, "reason": "无法识别影视类型"})
                else:
                    logger.info(f"[organize] 识别成功: {keep['name']} -> {category} ({media_type})")
                    to_organize.append({"file": keep, "category": category, "tmdb_info": tmdb_info, "media_type": media_type})
                # 重复文件移到冗余
                for d in dups:
                    logger.info(f"[organize] 重复文件: {d['name']} (与 {keep['name']} 重复)")
                    to_redundant.append({"file": d, "reason": f"与 {keep['name']} 重复"})
            else:
                f = files[0]
                if is_redundant(f["name"]):
                    logger.info(f"[organize] 冗余文件: {f['name']}")
                    to_redundant.append({"file": f, "reason": "冗余文件（sample/预告等）"})
                else:
                    category, tmdb_info, media_type = await cls._classify_file(f["name"], category_helper)
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
                result["organized"].append({
                    "name": orig_name,
                    "category": category,
                    "from": file_info.get("parent_path", ""),
                    "to": f"{category}/{new_folder or ''}/{renamed_to}",
                    "renamed_to": renamed_to if renamed_to != orig_name else "",
                })
            for item in to_redundant:
                result["redundant"].append({"name": item["file"]["name"], "reason": item["reason"]})
            for item in to_unrecognized:
                result["unrecognized"].append({"name": item["file"]["name"], "reason": item["reason"]})
            logger.info(
                f"[organize] 预览完成: 共 {result['total']} 个文件，"
                f"可整理 {len(result['organized'])}，冗余 {len(result['redundant'])}，"
                f"无法识别 {len(result['unrecognized'])}"
            )
            return result

        # 3a. 扫描全量同步目录中已有的文件标题，用于判断是否重复
        #     排除 source_cid（待整理目录），因为它里面的文件还没被整理，不应算作"已存在"
        existing_titles: set = set()
        existing_files_list: list[dict] = []
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
                existing_files_list = Client115Service.list_all_files(
                    cookies, target_cid, VIDEO_EXTS, min_size=0, recursive=True,
                    exclude_cids=exclude_cids,
                )
                for ef in existing_files_list:
                    existing_titles.add(extract_title(ef["name"]))
                logger.info(f"[organize] 全量同步目录已有 {len(existing_titles)} 个影视标题（已排除待整理等子目录）")
            except Exception as e:
                logger.warning(f"[organize] 扫描全量同步目录失败，跳过重复检测: {e}")

        # 3b. 移动整理后的文件到「全量同步目录」下的二级分类子目录
        #     如果文件在全量同步目录中已存在 → 移到「已存在影视的目录」
        if target_cid:
            # 按二级分类分组
            category_groups = defaultdict(list)
            for item in to_organize:
                category_groups[item["category"]].append(item)

            for category, items in category_groups.items():
                # category 是路径（如 "电影/动画电影"），拆分为多级目录
                path_parts = category.split("/")
                logger.info(f"[organize] 创建分类目录: {category} ({len(items)} 个文件)")
                sub_cid = Client115Service.ensure_path(cookies, path_parts, target_cid)
                if not sub_cid:
                    logger.error(f"[organize] 创建分类目录失败: {category}")
                    for item in items:
                        result["errors"].append({
                            "name": item["file"]["name"],
                            "error": f"创建分类目录「{category}」失败",
                        })
                    continue
                for item in items:
                    file_info = item["file"]
                    tmdb_info = item.get("tmdb_info")
                    media_type = item.get("media_type", "movie")
                    orig_name = file_info["name"]
                    renamed_to = ""

                    # 重复检测：如果全量同步目录中已有相同标题的文件 → 移到「已存在影视的目录」
                    # 注意：只有当文件名完全相同时才跳过，文件名不同则继续走重命名和洗版流程
                    file_title = extract_title(orig_name)
                    existing_match = None
                    if file_title and file_title in existing_titles and existing_cid:
                        # 进一步检查：是否有完全相同的文件名
                        existing_match = next(
                            (ef for ef in existing_files_list
                             if ef["name"] == orig_name),
                            None
                        )
                        if existing_match:
                            logger.info(f"[organize] 全量同步目录已存在同名文件: {orig_name}，移到已存在影视目录")
                            ok_exist = Client115Service.move(
                                cookies, [file_info["file_id"]], existing_cid
                            )
                            if ok_exist:
                                result["organized"].append({
                                    "name": orig_name,
                                    "category": item["category"],
                                    "from": file_info.get("parent_path", ""),
                                    "to": f"已存在影视/{orig_name}",
                                    "renamed_to": "",
                                })
                            else:
                                result["errors"].append({
                                    "name": orig_name,
                                    "error": "移动到已存在影视目录失败",
                                })
                            continue
                        else:
                            logger.info(f"[organize] 全量同步目录有相同标题但不同文件名，继续整理: {orig_name}")

                    # ffprobe 探测媒体信息（始终探测，用于验证和纠正文件名中的错误标记）
                    media_info = None
                    if use_ffprobe and is_ffprobe_available() and (rename_rules or (wash_config and wash_config.get("enabled"))):
                        try:
                            download_url = Client115Service.get_download_url(cookies, file_info.get("pickcode", ""))
                            if download_url:
                                logger.info(f"ffprobe 探测: {orig_name}")
                                media_info = await probe_media_info_async(download_url, timeout=30)
                                if media_info:
                                    logger.info(f"ffprobe 结果: pix={media_info.get('resource_pix')}, enc={media_info.get('video_encode')}, audio={media_info.get('audio_encode')}, fps={media_info.get('fps')}")
                        except Exception as e:
                            logger.warning(f"ffprobe 探测失败（跳过）: {orig_name} - {e}")

                    # 洗版检查
                    if wash_config and wash_config.get("enabled"):
                        should_move, wash_reason, old_files_to_replace = await cls._check_wash_replace(
                            cookies, sub_cid, file_info, tmdb_info, media_type, wash_config, media_info, category, prefer_filename
                        )
                        if not should_move:
                            result["redundant"].append({
                                "name": orig_name,
                                "reason": f"洗版跳过: {wash_reason}",
                            })
                            continue
                        # 移走被替换的旧文件到冗余目录
                        if old_files_to_replace and redundant_cid:
                            for old_file in old_files_to_replace:
                                old_name = old_file.get("name", "")
                                logger.info(f"[organize] 洗版替换: 移走旧文件 '{old_name}' → 冗余目录")
                                ok_old = Client115Service.move(
                                    cookies, [old_file["file_id"]], redundant_cid
                                )
                                if ok_old:
                                    result["redundant"].append({
                                        "name": old_name,
                                        "reason": f"洗版被替换: {wash_reason}",
                                    })
                                else:
                                    logger.warning(f"[organize] 移走旧文件失败: {old_name}")
                                    result["errors"].append({
                                        "name": old_name,
                                        "error": "洗版替换：移走旧文件失败",
                                    })

                    # 应用重命名规则
                    if rename_rules:
                        if not tmdb_info:
                            logger.warning(f"[organize] 跳过重命名（无 TMDB 信息）: {orig_name}")
                        new_name, new_folder, season_folder = await cls._compute_rename(
                            rename_rules, orig_name, tmdb_info, media_type, media_info, skip_no_info, prefer_filename
                        )
                        logger.info(f"[organize] 重命名计算: orig='{orig_name}', new_name='{new_name}', folder='{new_folder}', season='{season_folder}'")
                        # 重命名文件
                        if new_name and new_name != orig_name:
                            ok_rename = Client115Service.rename(
                                cookies, file_info["file_id"], new_name
                            )
                            if ok_rename:
                                renamed_to = new_name
                                logger.info(f"[organize] 重命名: {orig_name} -> {new_name}")
                            else:
                                logger.warning(f"[organize] 重命名失败: {orig_name}")
                                result["errors"].append({
                                    "name": orig_name,
                                    "error": "重命名失败",
                                })
                                continue
                        elif new_name and new_name == orig_name:
                            logger.info(f"[organize] 文件名已是目标格式，无需重命名: {orig_name}")
                        else:
                            logger.warning(f"[organize] 无法计算新文件名（可能缺少 TMDB 信息或模板解析失败）: {orig_name}")
                        # 剧集需要创建季文件夹
                        if media_type == "tv" and season_folder:
                            # 在分类目录下创建剧集文件夹和季文件夹
                            tv_folder_name = new_folder or ""
                            if tmdb_info:
                                tv_folder_name = apply_rename_template(
                                    rename_rules.get("tv_folder", "{title} ({year})"),
                                    tmdb_info, orig_name, None, None, None, None, media_info, prefer_filename
                                ) or tv_folder_name
                            if tv_folder_name:
                                season_cid = Client115Service.ensure_path(
                                    cookies, [tv_folder_name, season_folder], sub_cid
                                )
                                if season_cid:
                                    ok = Client115Service.move(
                                        cookies, [file_info["file_id"]], season_cid
                                    )
                                else:
                                    result["errors"].append({
                                        "name": orig_name,
                                        "error": f"创建季目录「{tv_folder_name}/{season_folder}」失败",
                                    })
                                    continue
                            else:
                                ok = Client115Service.move(
                                    cookies, [file_info["file_id"]], sub_cid
                                )
                        elif media_type == "av":
                            # AV：创建以番号命名的文件夹
                            av_folder_name = new_folder or ""
                            if av_folder_name:
                                av_cid = Client115Service.ensure_path(
                                    cookies, [av_folder_name], sub_cid
                                )
                                if av_cid:
                                    ok = Client115Service.move(
                                        cookies, [file_info["file_id"]], av_cid
                                    )
                                else:
                                    result["errors"].append({
                                        "name": orig_name,
                                        "error": f"创建 AV 目录「{av_folder_name}」失败",
                                    })
                                    continue
                            else:
                                ok = Client115Service.move(
                                    cookies, [file_info["file_id"]], sub_cid
                                )
                        else:
                            # 电影：创建电影文件夹
                            movie_folder_name = new_folder or ""
                            if tmdb_info:
                                movie_folder_name = apply_rename_template(
                                    rename_rules.get("movie_folder", "{title} ({year})"),
                                    tmdb_info, orig_name, None, None, None, None, media_info, prefer_filename
                                ) or movie_folder_name
                            if movie_folder_name:
                                movie_cid = Client115Service.ensure_path(
                                    cookies, [movie_folder_name], sub_cid
                                )
                                if movie_cid:
                                    ok = Client115Service.move(
                                        cookies, [file_info["file_id"]], movie_cid
                                    )
                                else:
                                    result["errors"].append({
                                        "name": orig_name,
                                        "error": f"创建电影目录「{movie_folder_name}」失败",
                                    })
                                    continue
                            else:
                                ok = Client115Service.move(
                                    cookies, [file_info["file_id"]], sub_cid
                                )
                    else:
                        ok = Client115Service.move(
                            cookies, [file_info["file_id"]], sub_cid
                        )

                    if ok:
                        logger.info(f"[organize] 移动成功: {orig_name} -> {category}/{renamed_to or orig_name}")
                        result["organized"].append({
                            "name": orig_name,
                            "category": category,
                            "from": file_info.get("parent_path", ""),
                            "to": f"{category}/",
                            "renamed_to": renamed_to or "",
                        })
                    else:
                        logger.warning(f"[organize] 移动失败: {orig_name}")
                        result["errors"].append({
                            "name": orig_name,
                            "error": "移动失败",
                        })
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
                    cookies, [item["file"]["file_id"]], redundant_cid
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
                    cookies, [item["file"]["file_id"]], unrecognized_cid
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

        logger.info(
            f"[organize] 整理完成: 共 {result['total']} 个文件，"
            f"已整理 {len(result['organized'])}，冗余 {len(result['redundant'])}，"
            f"无法识别 {len(result['unrecognized'])}，失败 {len(result['errors'])}"
        )

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
    ) -> tuple:
        """
        洗版检查：在目标目录中查找同标题的已存在文件，按 YAML 策略比较。
        wash_config 应包含 'wash_yaml' 字段（YAML 格式策略字符串）。
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

            # 获取目标目录下所有文件
            existing_files = Client115Service.list_all_files(
                cookies, target_cid, VIDEO_EXTS, min_size=0, recursive=True
            )
            if not existing_files:
                return True, "目标目录无重复文件", []

            new_info = parse_resource_info(file_info["name"], media_info, prefer_filename)
            new_info["size"] = file_info.get("size", 0)

            # 为新文件找到匹配的策略
            new_strategy = _find_matching_strategy(new_info, media_type, category, strategies)
            if not new_strategy:
                return True, f"无匹配洗版策略（media_type={media_type}）", []

            logger.info(f"[wash] 文件 '{file_info['name']}' 匹配策略 '{new_strategy.get('_name', '')}' (mode={new_strategy.get('mode', '')})")

            scope = new_strategy.get("scope", "all")
            old_files_to_replace = []

            for ef in existing_files:
                if extract_title(ef["name"]) != extract_title(file_info["name"]):
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
            logger.error(f"洗版检查失败: {e}")
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
            template = rename_rules.get("episode_file", "") or "{title} - S{season_num:02d}<E{episode_num:02d}>< - {episode_name}><.{resource_pix}><.{fps}><.{resource_version}><.{resource_source}><.{resource_type}><.{resource_effect}><.{video_encode}><.{audio_encode}><-{resource_team}>{ext}"
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
    async def _classify_file(
        cls,
        name: str,
        category_helper: CategoryHelper,
    ) -> tuple:
        """
        分类文件：通过 TMDB 搜索影视元数据，再按 YAML 分类配置匹配。
        AV 文件通过番号前缀匹配厂商分类，不查询 TMDB。
        返回 (category_str, tmdb_info, media_type)。
        category_str 为分类路径（如 "电影/动画电影"），无法识别返回 (None, None, None)。
        """
        # 先用内置逻辑判断电影/电视剧/AV
        builtin = auto_classify(name)

        # AV 文件：通过番号分类，不查询 TMDB
        if builtin == "av":
            av_code = extract_av_code(name)
            if av_code:
                sub_category = category_helper.get_av_category(av_code)
                if sub_category:
                    return (f"AV/{sub_category}", {"av_code": av_code}, "av")
                return ("AV", {"av_code": av_code}, "av")
            return (None, None, None)

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
            # TMDB 未找到，回退到简单分类
            logger.warning(f"[organize] TMDB 未找到 '{name}'，回退到简单分类")
            if builtin == "movie":
                return ("电影", None, "movie")
            elif builtin == "tvshow":
                return ("电视剧", None, "tv")
            return (None, None, None)

        # 判断是电影还是电视剧
        # TMDB 搜索结果中有 release_date 的是电影，有 first_air_date 的是电视剧
        if "release_date" in tmdb_info and tmdb_info.get("release_date"):
            # 电影
            sub_category = category_helper.get_movie_category(tmdb_info)
            if sub_category:
                return (f"电影/{sub_category}", tmdb_info, "movie")
            return ("电影", tmdb_info, "movie")
        elif "first_air_date" in tmdb_info and tmdb_info.get("first_air_date"):
            # 电视剧
            sub_category = category_helper.get_tv_category(tmdb_info)
            if sub_category:
                return (f"电视剧/{sub_category}", tmdb_info, "tv")
            return ("电视剧", tmdb_info, "tv")
        else:
            # 无法确定类型，按内置判断回退
            if builtin == "movie":
                sub_category = category_helper.get_movie_category(tmdb_info)
                return (f"电影/{sub_category}" if sub_category else "电影", tmdb_info, "movie")
            elif builtin == "tvshow":
                sub_category = category_helper.get_tv_category(tmdb_info)
                return (f"电视剧/{sub_category}" if sub_category else "电视剧", tmdb_info, "tv")
            return (None, None, None)


