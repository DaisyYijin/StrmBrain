"""
STRM 刮削服务（O2）— NFO/海报刮削 + SQLite 海报墙索引（精简版）

参考 LitePan-main/internal/strmscrape/ 的实现思路，做了大幅精简：
1. scan_for_scrape: 用 Client115Service.list_all_files_with_meta 列出视频文件，按 parent_path 分组
2. scrape_group: 对组内第一个文件名做标题/年份猜测，调 TMDB 搜索（movie 接口）
3. write_nfo: 在本地目录写 <title>.nfo（movie 标签 XML 简版）并下载 <title>-poster.jpg
4. build_sqlite_index: 扫描本地媒体目录，把已有 nfo 信息 upsert 进 SQLite 索引（DATA_DIR/strmscrape_index.db）

注意：本模块仅提供服务层，不注册 API（避免与 S4/Q3 等工具接口命名冲突）。
后续如需暴露接口，可在 tools.py 中挂载（参考 strm-clean / account-repair 的工具接口模式）。
"""
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

from app.config import DATA_DIR
from app.core.logbuffer import get_logger
from app.services.client_115 import Client115Service

logger = get_logger("app.services.strmscrape")

# 视频扩展名（与 sync_service 保持一致）
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".m2ts", ".rmvb", ".iso"}

# 标题 -> 刮削结果 的 TTL 缓存（上限 500 条，超出后整体清空重建）
_TMDB_CACHE: dict[str, dict] = {}
_TMDB_CACHE_MAX = 500
_TMDB_CACHE_LOCK = threading.Lock()

# SQLite 索引文件（海报墙索引）
INDEX_DB = DATA_DIR / "strmscrape_index.db"
_INDEX_DB_LOCK = threading.Lock()


class StrmScrapeService:
    """STRM 刮削服务（仅服务层，不注册 API）"""

    # ============ 扫描分组 ============

    @classmethod
    def scan_for_scrape(cls, cookies: str, source_cid: str) -> list[dict]:
        """列出 115 网盘源目录下的视频文件，按 parent_path 分组。

        返回 [{"group": str, "files": [文件名...], "count": int}]。
        group 为相对同步根目录的路径（根目录记为 "/"）。
        """
        files = Client115Service.list_all_files_with_meta(
            cookies, source_cid, VIDEO_EXTS, min_size=0, recursive=True
        )
        groups: dict[str, list] = {}
        for f in files:
            group = f.get("parent_path", "") or "/"
            groups.setdefault(group, []).append(f["name"])
        return [
            {"group": g, "files": names, "count": len(names)}
            for g, names in sorted(groups.items())
        ]

    # ============ 标题/年份猜测 + TMDB 搜索 ============

    @staticmethod
    def _guess_title_year(filename: str) -> tuple[str, Optional[int]]:
        """简易标题/年份猜测：正则 (.+?)[. _]?(\\d{4})。

        从文件名（去扩展名）中提取标题与 4 位年份；
        未匹配年份时年份为 None。
        """
        base = re.sub(r"\.[^.]+$", "", filename or "")  # 去扩展名
        m = re.search(r"(.+?)[. _]?(\d{4})", base)
        if m:
            title = re.sub(r"[._\s]+", " ", m.group(1)).strip()
            try:
                year = int(m.group(2))
            except ValueError:
                year = None
            return title, year
        return base.strip(), None

    @classmethod
    def scrape_group(cls, cookies: str, tmdb_api_key: str, group: str, files: list) -> dict:
        """对组内第一个文件名做标题/年份猜测，调 TMDB 搜索（movie 接口）。

        成功返回 {"title", "year", "tmdb_id", "poster_path"}；
        失败（无 key/无文件/请求失败/无结果）返回 {"status": "miss"}。
        标题->结果 结果带 TTL 缓存（dict，上限 500）。
        cookies 参数保留用于后续扩展（如按账号做限流），当前未使用。
        """
        if not tmdb_api_key or not files:
            return {"status": "miss"}
        filename = files[0]
        title, year = cls._guess_title_year(filename)
        if not title:
            return {"status": "miss"}

        cache_key = f"{title}|{year}"
        with _TMDB_CACHE_LOCK:
            cached = _TMDB_CACHE.get(cache_key)
        if cached:
            return cached

        try:
            params = {"api_key": tmdb_api_key, "query": title}
            if year:
                params["year"] = year
            resp = httpx.get(
                "https://api.themoviedb.org/3/search/movie",
                params=params, timeout=15.0,
            )
            if resp.status_code != 200:
                logger.warning(f"[strmscrape] TMDB 搜索 HTTP {resp.status_code}: {title}")
                return {"status": "miss"}
            results = (resp.json() or {}).get("results") or []
            if not results:
                return {"status": "miss"}
            first = results[0]
            release_date = first.get("release_date") or ""
            result = {
                "title": first.get("title") or first.get("original_title") or title,
                "year": release_date[:4] if release_date else (str(year) if year else ""),
                "tmdb_id": first.get("id"),
                "poster_path": first.get("poster_path") or "",
            }
        except Exception as e:
            logger.warning(f"[strmscrape] TMDB 搜索失败 {title}: {e}")
            return {"status": "miss"}

        # 写入缓存（上限 500，超出后清空重建，简单 TTL 策略）
        with _TMDB_CACHE_LOCK:
            if len(_TMDB_CACHE) >= _TMDB_CACHE_MAX:
                _TMDB_CACHE.clear()
            _TMDB_CACHE[cache_key] = result
        return result

    # ============ 写 NFO / 下载海报 ============

    @classmethod
    def write_nfo(cls, local_dir: str, title: str, year, tmdb_id, poster_url: str) -> Optional[str]:
        """在 local_dir 下写 <title>.nfo（movie 标签 XML 简版）并下载海报 <title>-poster.jpg。

        - 海报下载失败仅记录日志，不影响 nfo 写入
        - 返回 nfo 文件绝对路径；写入失败返回 None
        """
        try:
            root = Path(local_dir)
            root.mkdir(parents=True, exist_ok=True)
            # 文件名不能含文件系统非法字符
            safe_title = re.sub(r'[<>:"/\\|?*]', "", title or "").strip() or "movie"
            nfo_path = root / f"{safe_title}.nfo"
            year_str = str(year or "")
            xml = (
                '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
                "<movie>\n"
                f"  <title>{title or ''}</title>\n"
                f"  <year>{year_str}</year>\n"
                f"  <tmdbid>{tmdb_id or ''}</tmdbid>\n"
                "</movie>\n"
            )
            nfo_path.write_text(xml, encoding="utf-8")

            # 下载海报（失败跳过，不影响 nfo）
            if poster_url:
                try:
                    resp = httpx.get(poster_url, timeout=20.0)
                    if resp.status_code == 200:
                        (root / f"{safe_title}-poster.jpg").write_bytes(resp.content)
                    else:
                        logger.warning(f"[strmscrape] 海报下载 HTTP {resp.status_code}: {poster_url}")
                except Exception as e:
                    logger.warning(f"[strmscrape] 海报下载失败 {poster_url}: {e}")

            logger.info(f"[strmscrape] 已写 NFO: {nfo_path}")
            return str(nfo_path)
        except Exception as e:
            logger.warning(f"[strmscrape] 写 NFO 失败 {local_dir}: {e}")
            return None

    # ============ SQLite 海报墙索引 ============

    @staticmethod
    def _find_nfo(dir_path: Path) -> Optional[Path]:
        """在目录中查找一个可用的 .nfo 文件（优先 movie.nfo，否则取第一个）。"""
        if not dir_path.exists() or not dir_path.is_dir():
            return None
        nfo_list = sorted(dir_path.glob("*.nfo"))
        if not nfo_list:
            return None
        # 优先 movie.nfo（Emby 标准命名）
        for nfo in nfo_list:
            if nfo.name.lower() == "movie.nfo":
                return nfo
        return nfo_list[0]

    @staticmethod
    def _parse_nfo(nfo_path: Path) -> dict:
        """简易解析 nfo XML：提取 title / year / tmdbid（正则，不依赖第三方 XML 库）。"""
        meta = {"title": "", "year": "", "tmdb_id": ""}
        try:
            content = nfo_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return meta
        m = re.search(r"<title>(.*?)</title>", content, re.S)
        if m:
            meta["title"] = m.group(1).strip()
        m = re.search(r"<year>(.*?)</year>", content, re.S)
        if m:
            meta["year"] = m.group(1).strip()
        m = re.search(r"<tmdbid>(.*?)</tmdbid>", content, re.S)
        if m:
            meta["tmdb_id"] = m.group(1).strip()
        return meta

    @classmethod
    def build_sqlite_index(cls, local_media_dir: str) -> int:
        """扫描 local_media_dir 下所有 .strm 对应的目录，把已有 nfo 信息 upsert 进 SQLite 索引。

        索引库: DATA_DIR/strmscrape_index.db，表 items(id, title, year, tmdb_id, path, poster, scraped_at)。
        以目录相对路径为唯一键（path）upsert；返回索引总条数。
        """
        root = Path(local_media_dir)
        if not root.exists() or not root.is_dir():
            return 0

        # 初始化数据库表
        with _INDEX_DB_LOCK:
            conn = sqlite3.connect(str(INDEX_DB))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT,
                        year TEXT,
                        tmdb_id TEXT,
                        path TEXT UNIQUE,
                        poster TEXT,
                        scraped_at TEXT
                    )
                """)
                conn.commit()
            finally:
                conn.close()

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        strm_files = list(root.rglob("*.strm"))
        for sp in strm_files:
            dir_path = sp.parent
            try:
                rel = str(dir_path.relative_to(root)).replace("\\", "/") or "/"
            except ValueError:
                continue
            nfo = cls._find_nfo(dir_path)
            if not nfo:
                continue
            meta = cls._parse_nfo(nfo)
            if not meta.get("title"):
                continue
            poster_name = nfo.name[:-4] + "-poster.jpg"
            poster = poster_name if (dir_path / poster_name).exists() else ""
            rows.append((meta["title"], meta["year"], meta["tmdb_id"], rel, poster, now))

        with _INDEX_DB_LOCK:
            conn = sqlite3.connect(str(INDEX_DB))
            try:
                conn.executemany(
                    "INSERT INTO items (title, year, tmdb_id, path, poster, scraped_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "title=excluded.title, year=excluded.year, tmdb_id=excluded.tmdb_id, "
                    "poster=excluded.poster, scraped_at=excluded.scraped_at",
                    rows,
                )
                conn.commit()
                count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            finally:
                conn.close()

        logger.info(f"[strmscrape] SQLite 索引构建完成: 写入 {len(rows)} 条 NFO 信息，索引共 {count} 条")
        return count
