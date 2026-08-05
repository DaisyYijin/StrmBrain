"""
TMDB (The Movie Database) API 服务
用于搜索影视信息，获取元数据（genre_ids, original_language, origin_country 等）
供二级分类使用
"""
import difflib
import re
import time
from collections import OrderedDict
from typing import Optional

import httpx

from app.core.logbuffer import get_logger

logger = get_logger()

# 默认域名（国内可访问的镜像地址）
DEFAULT_API_DOMAIN = "https://api.tmdb.org"
DEFAULT_IMAGE_DOMAIN = "https://image.tmdb.org"

# 缓存 TMDB 搜索结果，避免重复请求（文件名 -> metadata）
# 使用 OrderedDict 实现 LRU + TTL 过期机制
_SEARCH_CACHE_MAX_SIZE = 500
_SEARCH_CACHE_TTL = 3600  # 缓存有效期（秒），1 小时
_search_cache: OrderedDict[str, tuple] = OrderedDict()


def _extract_search_title(name: str) -> tuple[str, Optional[str]]:
    """
    从文件名中提取标题和年份。
    返回 (title, year)
    """
    # 去除扩展名（最后一个 .xxx）
    base = re.sub(r'\.[^.]+$', '', name)
    # 去除方括号内容（发布组 [CheeseAni]、集号 [01]、技术信息 [CR-WebRip 1080p HEVC AAC SRT]、字幕 [简繁内封] 等）
    base = re.sub(r'\[[^\]]*\]', ' ', base)
    # 去除中文方括号【】内容（如【合集】、【国语版】等，通常为标记而非标题）
    base = re.sub(r'【[^】]*】', ' ', base)
    # 去除圆括号内容中的网址水印（如 (www.btsj6.com) ）
    base = re.sub(r'\([^)]*www\.[^)]*\)', ' ', base)
    # 去除 @水印@ 格式（如 @电影天堂@www.dygod.net ）
    base = re.sub(r'@[^@\s]*@', ' ', base)
    # 去除发布组（末尾 -WORD 格式，如 -KIN, -NTb, -RARBG）
    # 仅当末尾 - 后跟 2~20 个字母数字时去除（避免误伤 Spider-Man 等标题）
    base = re.sub(r'-[A-Za-z0-9]{2,20}$', '', base)
    # 尝试匹配年份
    year_match = re.search(r'[（(]?\s*(19|20)\d{2}\s*[）)]?', base)
    year = year_match.group(0).strip('()（ ）') if year_match else None
    # 去除分辨率/编码/来源/质量标记（按点或空格分隔）
    # 第一轮：非音频声道标签（标签前后不能紧邻字母，避免 ma 匹配 Man）
    # 注意：组合标签（如 DDP5.1）必须放在单独编解码器标签之前，否则会先匹配到单独标签
    tags_list = [
        # 编解码器+声道组合（如 DDP5.1, EAC3.7.1, AC3.5.1 等）
        r'(?:ddp|dd\+|dd|eac3|ac3|dts|truehd|aac)[._\s]?\d[.\s]\d',
        r'2160p', r'1080p', r'1080i', r'720p', r'720i', r'480p', r'480i', r'4k',
        r'bluray', r'blu-ray', r'webrip', r'web-dl', r'web', r'webdl',
        r'h\.?264', r'h\.?265', r'x264', r'x265', r'hevc', r'av1', r'vc-?1',
        r'dts-?hd', r'dts-?ma',
        r'avc', r'dovi', r'doVi',
        r'10bit', r'8bit',
        r'aac', r'dts', r'truehd', r'eac3', r'ac3', r'flac', r'lpcm', r'atmos',
        r'ddp', r'dd\+', r'dd',
        r'hdr10\+', r'hdr10', r'hdr', r'dv', r'dolby\.vision', r'sdr',
        r'remux', r'imax', r'hq', r'3d', r'cc', r'dc',
        r'nf', r'dsnp', r'amzn', r'hmax', r'atvp', r'pcok', r'stan', r'hulu', r'ma',
        r'cr', r'bili', r'bilibili', r'viu',
        r'uhd', r'hdtv', r'dvd', r'dvdrip', r'bdrip', r'brrip',
        r'bd',
        # HD 前缀（放在 uhd/hdtv 之后避免部分匹配）
        r'hd',
        # 中文字幕/语言标记
        r'chs', r'cht', r'chc', r'gb', r'big5',
        r'简体', r'繁体', r'简繁', r'繁简', r'内封字幕', r'内嵌字幕', r'外挂字幕',
        r'内封', r'内嵌', r'外挂', r'双语', r'中字', r'中英',
        r'multi', r'hybrid', r'repack', r'proper', r'retail',
        r'complete', r'season[._\s]*complete',
        r'srt', r'ass', r'ssa', r'pgs', r'vobsub',
        r'\d{2,3}fps',
        # 版本标记（v2, v3 等）
        r'v\d+',
    ]
    # 标签前后不能紧邻字母（避免 ma 匹配 Man、dd 匹配 Adding、cr 匹配 scratch 等）
    tags_pattern = r'[._\s]?(?<![a-zA-Z])(?:' + r'|'.join(tags_list) + r')(?![a-zA-Z])[._\s]?'
    # 用空格替换而非删除，避免相邻标签粘连导致后续无法匹配
    for _ in range(5):
        new_base = re.sub(tags_pattern, ' ', base, flags=re.IGNORECASE)
        if new_base == base:
            break
        base = new_base
    # 第二轮：音频声道标签（5.1, 7.1 等）
    audio_tags = [r'5[.\s]1', r'7[.\s]1', r'2[.\s]0', r'2[.\s]1', r'1[.\s]0']
    audio_pattern = r'[._\s]?(?<![0-9])(?:' + r'|'.join(audio_tags) + r')(?![a-zA-Z])[._\s]?'
    for _ in range(3):
        new_base = re.sub(audio_pattern, ' ', base, flags=re.IGNORECASE)
        if new_base == base:
            break
        base = new_base
    # 去集数标记及其后的集名内容
    # 所有集数标记后面通常跟着集名（如 S01E11.行凶者 / 第11集.集名 / EP11.集名），
    # 集名不是标题的一部分，直接截断标记及其后所有内容，只保留标记之前的标题
    # 支持：SxxExx, Exx, EPxx, 第N集, 第N话, 1x01 格式
    _episode_markers = [
        r'[._\s]?[sS]\d{1,2}[eE]\d{1,3}(?:[-–][eE]?\d{1,3})?[._\s]?',  # S01E01 / S01E01-E12
        r'[._\s]?[eE][pP]\d{1,3}[._\s]?',                               # EP01
        r'[._\s]?[eE]\d{1,3}(?![a-zA-Z])[._\s]?',                       # E01 (不含EP)
        r'[._\s]?\d{1,2}x\d{1,3}[._\s]?',                               # 1x01
        r'[._\s]?第\d{1,3}[集话][._\s]?',                               # 第11集 / 第11话
    ]
    for _pat in _episode_markers:
        _m = re.search(_pat, base)
        if _m:
            if _m.start() > 0:
                # 标记前有标题内容 → 截断标记及其后所有内容（集名等）
                base = base[:_m.start()]
            else:
                # 标记在开头 → 仅移除标记本身，保留后面的内容
                base = re.sub(_pat, ' ', base)
            break  # 只需匹配第一个集数标记
    # 去单独的 Sxx（季号，无集号）
    base = re.sub(r'(?:^|[._\s])[sS]\d{1,2}(?![eE]\d)(?:[._\s]|$)', ' ', base)
    # 去 Season N（英文季号写法）
    base = re.sub(r'[._\s]?[sS]eason\s*\d{1,2}[._\s]?', ' ', base)
    # 去年份
    base = re.sub(r'[（(]?\s*(19|20)\d{2}\s*[）)]?', ' ', base)
    # 去中文季数标记
    base = re.sub(r'第\d{1,2}季', ' ', base)
    # 去「全N集」「全N话」标记
    base = re.sub(r'全\d{1,3}[集话部]', ' ', base)
    # 替换分隔符为空格
    title = re.sub(r'[._]', ' ', base).strip()
    # 清理全角标点（日文/中文问号、感叹号等）
    title = re.sub(r'[？！：；]', '', title)
    # 清理网站水印（BT世界网、BT之家、高清MP4、电影天堂 等常见下载站名称）
    title = re.sub(r'BT[\u4e00-\u9fff]*网?', '', title, flags=re.IGNORECASE)
    title = re.sub(r'高清MP4', '', title, flags=re.IGNORECASE)
    # 清理网址残留（www.xxx.com 等，连同域名后缀一起清除）
    title = re.sub(r'www\s+\S*', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\b(?:com|net|org|cn|cc|tv|io|me|info)\b', '', title, flags=re.IGNORECASE)
    # 清理「版)」残留（来自「(2024版)」去年份后残留）
    title = re.sub(r'版\)', '', title)
    # 清理 H.264 拆分后残留的单独字母 H（仅当两侧为空格或边界时）
    title = re.sub(r'(?:^|\s)[Hh](?:\s|$)', ' ', title)
    # 清理可能残留的首尾空格和多余空格
    title = re.sub(r'\s+', ' ', title).strip()
    # 去除首尾的逗号、句号等残留标点
    title = re.sub(r'^[,\s]+|[,\s]+$', '', title).strip()
    return title, year


def _extract_chinese_title(title: str) -> str:
    """
    从混合标题中提取最长的连续中文片段（用于 TMDB 搜索回退）。
    例如 'Spirited Away 千与千寻' -> '千与千寻'
         '一家之主 HD1080PCHS BT世界网' -> '一家之主'
    """
    # 找所有连续中文片段（含·间隔号），取最长的一个
    segments = re.findall(r'[\u4e00-\u9fff·]+', title)
    if not segments:
        return ''
    return max(segments, key=len)


# ===== 搜索置信度评分（参考 qmediasync：候选评分 score 字段） =====

# 低置信度阈值：评分低于此值的搜索结果视为"匹配存疑"
# 整理服务根据 _confidence 决定是否移入识别不准目录
CONFIDENCE_THRESHOLD = 0.45


def _normalize_title(s: str) -> str:
    """标题归一化：小写、去空格和常见标点，用于相似度比较。
    如 'Spider-Man: No Way Home' -> 'spidermannowayhome'"""
    if not s:
        return ""
    s = s.lower()
    return re.sub(r'[\s._\-\[\]()【】（）:：,，;；!！?？\'"·&]+', '', s)


def _score_candidate(query_title: str, query_year: Optional[str], candidate: dict) -> float:
    """
    计算 TMDB 候选结果的匹配评分（0.0 ~ 1.0）。

    参考 qmediasync：对搜索结果计算 score（标题相似度 + 年份一致性），
    选评分最高的候选，避免盲选 results[0] 导致识别错误。

    评分构成：
    - 标题相似度（权重 0.7）：归一化后完全相等=1.0，包含关系=0.85，否则 SequenceMatcher 相似度
    - 年份一致性（权重 0.3）：候选年份与查询年份一致=1.0，查询无年份=0.6 中性，不一致=0.0
    """
    query_norm = _normalize_title(query_title)
    if not query_norm:
        return 0.0

    # 标题候选：title/name/original_title/original_name 任一个匹配取最高分
    # （中英文标题都可能出现，全部参与比较避免漏匹配）
    cand_titles = [
        candidate.get("title", ""),
        candidate.get("name", ""),
        candidate.get("original_title", ""),
        candidate.get("original_name", ""),
    ]
    best_title_score = 0.0
    for t in cand_titles:
        tn = _normalize_title(t)
        if not tn:
            continue
        if tn == query_norm:
            score = 1.0
        elif query_norm in tn or tn in query_norm:
            score = 0.85
        else:
            score = difflib.SequenceMatcher(None, query_norm, tn).ratio()
        best_title_score = max(best_title_score, score)

    # 年份一致性
    year_score = 0.6  # 查询无年份时中性
    if query_year:
        cand_date = candidate.get("release_date") or candidate.get("first_air_date") or ""
        cand_year = cand_date[:4] if len(cand_date) >= 4 else ""
        if cand_year:
            year_score = 1.0 if cand_year == query_year else 0.0

    return round(0.7 * best_title_score + 0.3 * year_score, 3)


def _pick_best_result(results: list, query_title: str, query_year: Optional[str], kind: str) -> Optional[dict]:
    """
    从搜索结果中按评分选取最佳候选，附加置信度字段。
    kind: "movie" / "tv"，仅用于日志。
    返回附加了 _confidence / _candidates 字段的候选 dict（浅拷贝），无结果返回 None。
    """
    if not results:
        return None
    scored = [(_score_candidate(query_title, query_year, cand), cand) for cand in results]
    scored.sort(key=lambda x: x[0], reverse=True)

    top_score, best = scored[0]

    # 日志展示前 5 候选（参考 qmediasync：搜索结果前5）
    log_candidates = [
        {
            "title": c.get("title") or c.get("name") or "",
            "year": (c.get("release_date") or c.get("first_air_date") or "")[:4],
            "tmdb_id": c.get("id"),
            "score": round(s, 2),
        }
        for s, c in scored[:5]
    ]
    logger.info(f"TMDB 搜索{kind}候选前{len(log_candidates)}: {log_candidates}")

    tmdb_id = best.get("id")
    if not tmdb_id:
        return None

    logger.info(f"TMDB 搜索{kind}: 选中 id={tmdb_id}, title={best.get('title') or best.get('name')}, score={top_score:.2f}")

    # 浅拷贝并附加置信度字段（_ 前缀为内部字段，不污染 TMDB 原始数据）
    info = dict(best)
    info["_confidence"] = top_score
    info["_candidates"] = log_candidates
    return info


class TmdbService:
    """TMDB API 服务"""

    @staticmethod
    def _get_settings() -> dict:
        """从数据库读取 TMDB 全部设置（api_key, api_domain, image_domain）"""
        from app.core.db_helper import read_setting
        return read_setting("tmdb")

    @classmethod
    def _get_api_key(cls) -> str:
        """获取 TMDB API Key"""
        return cls._get_settings().get("api_key", "")

    @classmethod
    def _get_api_domain(cls) -> str:
        """获取 TMDB API 域名（带 /3 后缀）"""
        domain = cls._get_settings().get("api_domain", "").strip().rstrip("/")
        if not domain:
            domain = DEFAULT_API_DOMAIN
        return f"{domain}/3"

    @classmethod
    def _get_image_domain(cls) -> str:
        """获取 TMDB 图片域名"""
        domain = cls._get_settings().get("image_domain", "").strip().rstrip("/")
        if not domain:
            domain = DEFAULT_IMAGE_DOMAIN
        return domain

    @classmethod
    def _get_language(cls) -> str:
        """获取 TMDB 搜索语言: both=中文+英文, zh=仅中文, en=仅英文"""
        lang = cls._get_settings().get("language", "both")
        if lang not in ("both", "zh", "en"):
            lang = "both"
        return lang

    @classmethod
    def _get_tmdb_language_param(cls) -> str:
        """获取 TMDB API 的 language 参数值"""
        lang = cls._get_language()
        if lang == "en":
            return "en-US"
        # both 和 zh 都使用 zh-CN（both 模式下 TMDB 自然回退到英文）
        return "zh-CN"

    @classmethod
    async def search_media(
        cls,
        name: str,
        media_type: Optional[str] = None,
    ) -> Optional[dict]:
        """
        搜索影视信息，返回 TMDB 元数据。

        Args:
            name: 文件名或标题
            media_type: "movie" / "tv" / None（自动判断）

        Returns:
            TMDB 元数据字典，包含 genre_ids, original_language,
            origin_country/production_countries, release_date/first_air_date 等。
            未找到返回 None。
        """
        api_key = cls._get_api_key()
        if not api_key:
            logger.warning("TMDB API Key 未配置，无法搜索影视信息")
            return None

        # 检查缓存（带 TTL 过期检查）
        cache_key = f"{media_type or 'auto'}:{name}"
        if cache_key in _search_cache:
            cached_info, cached_time = _search_cache[cache_key]
            if time.time() - cached_time < _SEARCH_CACHE_TTL:
                logger.debug(f"TMDB 缓存命中: {name}")
                _search_cache.move_to_end(cache_key)  # LRU 更新
                return cached_info
            else:
                _search_cache.pop(cache_key)  # 过期，移除

        title, year = _extract_search_title(name)
        if not title:
            logger.warning(f"TMDB 搜索: 无法从文件名提取标题: {name}")
            return None

        logger.info(f"TMDB 搜索: title='{title}', year={year}, media_type={media_type}, language={cls._get_language()}, api_key={api_key[:8]}***")

        # 自动判断类型
        if not media_type:
            media_type = cls._guess_media_type(name)

        try:
            if media_type == "tv":
                info = await cls._search_tv(title, year)
                # TV 搜索失败时，尝试电影（文件名可能误判类型）
                if not info:
                    logger.info(f"TMDB: TV 搜索无结果，尝试电影搜索: '{title}'")
                    info = await cls._search_movie(title, year)
            elif media_type == "movie":
                info = await cls._search_movie(title, year)
                # 电影搜索失败时，尝试 TV
                if not info:
                    logger.info(f"TMDB: 电影搜索无结果，尝试 TV 搜索: '{title}'")
                    info = await cls._search_tv(title, year)
            else:
                # 先搜电影，再搜电视剧
                info = await cls._search_movie(title, year)
                if not info:
                    info = await cls._search_tv(title, year)
        except Exception as e:
            import traceback
            logger.warning(
                f"TMDB 搜索失败 '{title}': {type(e).__name__}: {e}\n"
                f"Traceback: {traceback.format_exc()}"
            )
            return None

        if info:
            logger.info(f"TMDB 搜索成功: '{title}' -> id={info.get('id')}, title={info.get('title') or info.get('name')}")
            _search_cache[cache_key] = (info, time.time())
            # LRU 淘汰：超过最大缓存数时移除最旧的条目
            while len(_search_cache) > _SEARCH_CACHE_MAX_SIZE:
                _search_cache.popitem(last=False)
        else:
            # 回退：用纯中文标题重试（文件名可能残留水印/技术标记干扰搜索）
            chinese_title = _extract_chinese_title(title)
            if chinese_title and chinese_title != title:
                logger.info(f"TMDB 回退搜索: 用纯中文标题 '{chinese_title}' 重试 (原 title='{title}')")
                try:
                    if media_type == "tv":
                        info = await cls._search_tv(chinese_title, year)
                        if not info:
                            info = await cls._search_movie(chinese_title, year)
                    elif media_type == "movie":
                        info = await cls._search_movie(chinese_title, year)
                        if not info:
                            info = await cls._search_tv(chinese_title, year)
                    else:
                        info = await cls._search_movie(chinese_title, year)
                        if not info:
                            info = await cls._search_tv(chinese_title, year)
                except Exception as e2:
                    logger.warning(f"TMDB 回退搜索失败 '{chinese_title}': {e2}")

                if info:
                    logger.info(f"TMDB 回退搜索成功: '{chinese_title}' -> id={info.get('id')}, title={info.get('title') or info.get('name')}")
                    _search_cache[cache_key] = (info, time.time())
                    while len(_search_cache) > _SEARCH_CACHE_MAX_SIZE:
                        _search_cache.popitem(last=False)
                else:
                    logger.warning(f"TMDB 搜索无结果: '{title}' (回退 '{chinese_title}' 也无结果, year={year})")
        return info

    @classmethod
    async def _search_movie(cls, title: str, year: Optional[str] = None) -> Optional[dict]:
        """搜索电影（带年份回退）"""
        base_url = cls._get_api_domain()

        # 第一轮：带年份搜索
        if year:
            params_with_year = {
                "api_key": cls._get_api_key(),
                "query": title,
                "language": cls._get_tmdb_language_param(),
                "page": 1,
                "year": year,
            }
            logger.info(f"TMDB 搜索电影 (带年份): query='{title}', year={year}")
            data = await cls._http_get(f"{base_url}/search/movie", params_with_year)
            if data and data.get("results"):
                return await cls._pick_movie_result(data["results"], title, year)

        # 第二轮：不带年份搜索
        params = {
            "api_key": cls._get_api_key(),
            "query": title,
            "language": cls._get_tmdb_language_param(),
            "page": 1,
        }
        logger.info(f"TMDB 搜索电影 (不带年份): query='{title}'")
        data = await cls._http_get(f"{base_url}/search/movie", params)
        if not data or not data.get("results"):
            logger.info(f"TMDB 搜索电影: 无结果 (title='{title}')")
            return None

        return await cls._pick_movie_result(data["results"], title, year)

    @classmethod
    async def _pick_movie_result(cls, results: list, query_title: str = "", query_year: Optional[str] = None) -> Optional[dict]:
        """从电影搜索结果中按评分选取最佳匹配并获取详情。
        query_title/query_year: 搜索上下文，用于置信度评分（参考 qmediasync score）。"""
        best = _pick_best_result(results, query_title, query_year, "电影")
        if not best:
            return None
        tmdb_id = best.get("id")

        # 获取详情（需要 production_countries）
        detail = await cls._get_movie_detail(tmdb_id)
        if detail:
            # 保留置信度字段
            detail = dict(detail)
            detail["_confidence"] = best.get("_confidence", 0.0)
            detail["_candidates"] = best.get("_candidates", [])
            return detail
        return best

    @classmethod
    async def _search_tv(cls, title: str, year: Optional[str] = None) -> Optional[dict]:
        """搜索电视剧（带年份回退）"""
        base_url = cls._get_api_domain()

        # 第一轮：带年份搜索
        if year:
            params_with_year = {
                "api_key": cls._get_api_key(),
                "query": title,
                "language": cls._get_tmdb_language_param(),
                "page": 1,
                "first_air_date_year": year,
            }
            logger.info(f"TMDB 搜索电视剧 (带年份): query='{title}', year={year}")
            data = await cls._http_get(f"{base_url}/search/tv", params_with_year)
            if data and data.get("results"):
                return await cls._pick_tv_result(data["results"], title, year)

        # 第二轮：不带年份搜索（年份可能不准确或 TMDB 未收录该年份）
        params = {
            "api_key": cls._get_api_key(),
            "query": title,
            "language": cls._get_tmdb_language_param(),
            "page": 1,
        }
        logger.info(f"TMDB 搜索电视剧 (不带年份): query='{title}'")
        data = await cls._http_get(f"{base_url}/search/tv", params)
        if not data or not data.get("results"):
            logger.info(f"TMDB 搜索电视剧: 无结果 (title='{title}')")
            return None

        return await cls._pick_tv_result(data["results"], title, year)

    @classmethod
    async def _pick_tv_result(cls, results: list, query_title: str = "", query_year: Optional[str] = None) -> Optional[dict]:
        """从电视剧搜索结果中按评分选取最佳匹配并获取详情。
        query_title/query_year: 搜索上下文，用于置信度评分（参考 qmediasync score）。"""
        best = _pick_best_result(results, query_title, query_year, "电视剧")
        if not best:
            return None
        tmdb_id = best.get("id")

        # 获取详情
        detail = await cls._get_tv_detail(tmdb_id)
        if detail:
            # 保留置信度字段
            detail = dict(detail)
            detail["_confidence"] = best.get("_confidence", 0.0)
            detail["_candidates"] = best.get("_candidates", [])
            return detail
        return best

    @classmethod
    async def _http_get(cls, url: str, params: dict, retries: int = 2) -> Optional[dict]:
        """
        带重试的 HTTP GET 请求。
        超时 30 秒，失败自动重试。
        """
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, params=params)
                    if resp.status_code != 200:
                        logger.warning(f"TMDB API 返回 {resp.status_code}: {url} - {resp.text[:200]}")
                        return None
                    return resp.json()
            except Exception as e:
                logger.warning(
                    f"TMDB HTTP 异常 (attempt {attempt+1}/{retries+1}): "
                    f"{type(e).__module__}.{type(e).__name__}: {e}"
                )
                if attempt < retries:
                    import asyncio
                    await asyncio.sleep(1)
                else:
                    logger.warning(f"TMDB HTTP 重试耗尽，抛出异常: {type(e).__name__}")
                    raise

    @classmethod
    async def _get_movie_detail(cls, tmdb_id: int) -> Optional[dict]:
        """获取电影详情"""
        base_url = cls._get_api_domain()
        params = {
            "api_key": cls._get_api_key(),
            "language": cls._get_tmdb_language_param(),
            "append_to_response": "alternative_titles,translations",
        }
        data = await cls._http_get(f"{base_url}/movie/{tmdb_id}", params)
        if not data:
            return None

        # 将 genres 转换为 genre_ids
        if data.get("genres"):
            data["genre_ids"] = [g["id"] for g in data["genres"]]

        # 确保使用中文标题：如果 title 与 original_title 相同（说明无中文翻译），
        # 从 translations 中查找中文标题
        cls._ensure_chinese_title(data, "title", "original_title")

        return data

    @classmethod
    async def _get_tv_detail(cls, tmdb_id: int) -> Optional[dict]:
        """获取电视剧详情"""
        base_url = cls._get_api_domain()
        params = {
            "api_key": cls._get_api_key(),
            "language": cls._get_tmdb_language_param(),
            "append_to_response": "alternative_titles,translations",
        }
        data = await cls._http_get(f"{base_url}/tv/{tmdb_id}", params)
        if not data:
            return None

        # 将 genres 转换为 genre_ids
        if data.get("genres"):
            data["genre_ids"] = [g["id"] for g in data["genres"]]

        # 确保使用中文标题：如果 name 与 original_name 相同（说明无中文翻译），
        # 从 translations 中查找中文标题
        cls._ensure_chinese_title(data, "name", "original_name")

        return data

    @classmethod
    def _ensure_chinese_title(cls, data: dict, title_key: str, original_key: str):
        """
        确保数据中使用中文标题。
        如果 title/name 与 original_title/original_name 相同，且标题不包含中文字符，
        说明 TMDB 没有返回中文翻译，此时从 translations 列表中查找中文标题并替换。
        语言模式行为：
          - both（中文+英文）：优先简体中文(zh-CN)，其次繁体中文(zh-TW)，最后任意 zh
          - zh（仅中文）：仅使用简体中文(zh-CN)，不回退繁体，避免繁体标题
          - en（仅英文）：不执行替换
        注意：如果标题已经包含中文字符（如国产剧原名就是中文），则不需要替换。
        """
        lang = cls._get_language()
        # 仅英文模式：不需要强制中文标题
        if lang == "en":
            return
        title = data.get(title_key, "") or ""
        original = data.get(original_key, "") or ""
        # 标题已包含中文字符 → 已经是中文标题，无需从 translations 替换
        # （国产剧/中文剧的 original_name 本身就是中文，title == original 是正常的）
        has_chinese = bool(re.search(r'[\u4e00-\u9fff]', title))
        if has_chinese:
            return
        # 标题与原标题相同，且原标题非空，且标题不含中文 → 可能没有中文翻译
        if title and original and title == original:
            translations = data.get("translations", {}).get("translations", [])
            zh_cn_title = ""
            zh_tw_title = ""
            zh_title = ""
            for t in translations:
                iso = t.get("iso_639_1") or ""
                country = t.get("iso_3166_1") or ""
                if iso == "zh":
                    t_data = t.get("data", {})
                    candidate = t_data.get(title_key, "") or ""
                    if candidate and candidate != original:
                        if country == "CN":
                            zh_cn_title = candidate
                        elif country == "TW":
                            zh_tw_title = candidate
                        elif not zh_title:
                            zh_title = candidate
            # 按语言模式选择
            if lang == "zh":
                # 仅中文：只用简体，不回退繁体
                final = zh_cn_title
            else:
                # 中文+英文：简体 → 繁体 → 任意中文
                final = zh_cn_title or zh_tw_title or zh_title
            if final:
                data[title_key] = final
                logger.info(f"TMDB 从 translations 提取中文标题: {original} -> {final}")

    @classmethod
    async def get_season_detail(cls, tmdb_id: int, season_num: int) -> Optional[dict]:
        """获取电视剧某一季的详情（含季名、播出日期等）"""
        base_url = cls._get_api_domain()
        params = {"api_key": cls._get_api_key(), "language": cls._get_tmdb_language_param()}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{base_url}/tv/{tmdb_id}/season/{season_num}", params=params
                )
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception as e:
            logger.warning(f"TMDB 获取季详情失败 tv={tmdb_id} season={season_num}: {e}")
            return None

    @classmethod
    async def get_episode_detail(cls, tmdb_id: int, season_num: int, episode_num: int) -> Optional[dict]:
        """获取电视剧某一集的详情（含集名等）"""
        base_url = cls._get_api_domain()
        params = {"api_key": cls._get_api_key(), "language": "zh-CN"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{base_url}/tv/{tmdb_id}/season/{season_num}/episode/{episode_num}",
                    params=params,
                )
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception as e:
            logger.warning(f"TMDB 获取集详情失败 tv={tmdb_id} S{season_num}E{episode_num}: {e}")
            return None

    @staticmethod
    def _guess_media_type(name: str) -> str:
        """
        根据文件名猜测影视类型
        返回: "movie" / "tv"
        """
        lower = name.lower()
        tv_patterns = [
            r'[sS]\d{1,2}[eE]\d{1,3}',
            r'(?:^|[._\s])[sS]\d{1,2}(?![eE]\d)(?:[._\s]|$)',  # .S01.（仅季号）
            r'第\d{1,3}集',
            r'第\d{1,3}话',
            r'第\d{1,2}季',
            r'[eE][pP]\d{1,3}',
            r'[sS]eason\s*\d{1,2}',
            r'\d{1,2}x\d{1,3}',
        ]
        for pat in tv_patterns:
            if re.search(pat, name):
                return "tv"
        return "movie"

    @classmethod
    def clear_cache(cls):
        """清空搜索缓存"""
        _search_cache.clear()
