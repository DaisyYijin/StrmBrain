"""
115 客户端服务 - 基于 p115client
"""
import time as _time
import hashlib
import threading
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from collections import deque, OrderedDict
import asyncio

import httpx
from p115client import P115Client

from app.config import COOKIES_DIR
from app.core.logbuffer import get_logger
from app.core.db_helper import get_api_intervals

logger = get_logger("app.services.client_115")


# 线程池用于执行同步的 p115client 调用
_executor = ThreadPoolExecutor(max_workers=4)

# P115Client 实例缓存：cookies_hash -> P115Client
# 避免每次操作都重建 client，减少初始化开销
_clients_cache: OrderedDict[str, P115Client] = OrderedDict()
_clients_cache_lock = threading.Lock()
_CLIENTS_CACHE_MAX = 10

# 直链缓存: pickcode -> {"url": str, "ts": float, "account_id": int}
# 115 下载链接有效期约 15 分钟，缓存 10 分钟以留余量
_DOWNLOAD_URL_CACHE: dict[str, dict] = {}
_DOWNLOAD_URL_TTL = 600  # 10 分钟
_cache_lock = threading.Lock()

# 115 限流等待时间
_RATE_LIMIT_WAIT = 30  # 秒
_MAX_RETRIES = 3


def _run_in_thread(func, *args, **kwargs):
    """在线程池中运行同步函数"""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, lambda: func(*args, **kwargs))


def _is_rate_limited(exc: Exception) -> bool:
    """判断异常是否为 115 访问频率过高"""
    msg = str(exc)
    return "访问频率" in msg or "频率过高" in msg or "too many" in msg.lower()


def _fs_files_with_retry(client, params: dict, max_retries: int = _MAX_RETRIES) -> dict:
    """带限流重试的 fs_files 调用"""
    for attempt in range(max_retries):
        try:
            return client.fs_files(params)
        except Exception as e:
            if _is_rate_limited(e) and attempt < max_retries - 1:
                logger.warning(f"[115] 访问频率过高，等待 {_RATE_LIMIT_WAIT}s 后重试 (attempt {attempt+1}/{max_retries})")
                _time.sleep(_RATE_LIMIT_WAIT)
                continue
            raise
    return {}


class Client115Service:
    """115 网盘客户端服务"""
    
    _qrcode_data: dict = {}  # uid -> {"token": dict, "app": str}
    _completed: dict = {}    # uid -> {"time": ts, "result": dict}
    
    @classmethod
    async def get_qrcode_for_login(cls, app: str = "web") -> tuple[str, str]:
        """获取扫码登录二维码，返回 (qrcode_image_url, uid)"""
        result = await _run_in_thread(P115Client.login_qrcode_token, app)
        
        data = result.get("data", {})
        uid = data.get("uid", "")
        
        # 只保存状态查询需要的字段
        token_for_status = {
            "uid": uid,
            "time": data.get("time"),
            "sign": data.get("sign"),
        }
        cls._qrcode_data[uid] = {"token": token_for_status, "app": app}
        
        # 使用 115 官方二维码图片 URL（不自己生成，避免内容不匹配）
        qrcode_img_url = f"https://qrcodeapi.115.com/api/1.0/{app}/1.0/qrcode?uid={uid}"
        
        return qrcode_img_url, uid
    
    @classmethod
    async def check_qrcode_status(cls, uid: str) -> dict:
        """
        检查二维码扫描状态
        使用 long-poll 方式：服务端等待最多 10 秒
        """
        # 已完成的缓存
        if uid in cls._completed:
            entry = cls._completed[uid]
            if _time.time() - entry["time"] < 60:
                return entry["result"]
            cls._completed.pop(uid, None)
        
        if uid not in cls._qrcode_data:
            return {"status": -1, "message": "二维码已过期"}
        
        token = cls._qrcode_data[uid]["token"]
        app = cls._qrcode_data[uid]["app"]
        
        try:
            # 用 httpx 直接请求状态接口，设置 10 秒超时
            # 115 的 /get/status/ 是 long-poll，会阻塞到状态变化
            params = {
                "uid": token["uid"],
                "time": token["time"],
                "sign": token["sign"],
            }
            
            status_data = await cls._poll_status(params)
            
            if status_data is None:
                # 超时，等待中
                return {"status": 0, "message": "等待扫描..."}
            
            # status_data 是 API 返回的 data 字段
            if not status_data or "status" not in status_data:
                return {"status": 0, "message": "等待扫描..."}
            
            status_code = status_data.get("status", 0)
            
            if status_code == 0:
                return {"status": 0, "message": "等待扫描..."}
            elif status_code == 1:
                return {"status": 1, "message": "已扫描，请在手机上确认"}
            elif status_code == 2:
                return await cls._handle_login_success(uid, app)
            else:
                cls._qrcode_data.pop(uid, None)
                if status_code == -1:
                    return {"status": -1, "message": "二维码已过期"}
                elif status_code == -2:
                    return {"status": -2, "message": "已取消"}
                return {"status": -1, "message": "未知状态"}
                
        except Exception as e:
            logger.warning(f"[115] 检查二维码状态异常: {e}")
            return {"status": 0, "message": "等待扫描..."}
    
    @classmethod
    async def _poll_status(cls, params: dict) -> Optional[dict]:
        """短超时查询状态，2 秒超时返回 None"""
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://qrcodeapi.115.com/get/status/",
                    params=params,
                    timeout=httpx.Timeout(connect=5.0, read=2.0, write=5.0, pool=5.0)
                )
                result = r.json()
                return result.get("data")
        except (httpx.ReadTimeout, httpx.ConnectTimeout):
            return None
        except Exception as e:
            logger.warning(f"[115] 轮询二维码状态异常: {e}")
            return None
    
    @classmethod
    def get_qrcode_app(cls, uid: str) -> Optional[str]:
        """获取扫码会话对应的设备类型"""
        if uid in cls._qrcode_data:
            return cls._qrcode_data[uid].get("app")
        if uid in cls._completed:
            return cls._completed[uid].get("app")
        return None

    @classmethod
    async def _handle_login_success(cls, uid: str, app: str) -> dict:
        """处理登录成功"""
        try:
            login_result = await _run_in_thread(
                P115Client.login_qrcode_scan_result, uid, app
            )
            
            logger.info("[115] 扫码登录成功，已获取 cookies")
            
            # 尝试多种方式提取 cookies
            data = login_result.get("data", {})
            cookies_data = data.get("cookie", data)
            
            if isinstance(cookies_data, dict):
                # 过滤空值
                cookies_str = "; ".join([
                    f"{k}={v}" for k, v in cookies_data.items() 
                    if v and k not in ("state", "code", "message", "error", "errno", "data")
                ])
            elif isinstance(cookies_data, str):
                cookies_str = cookies_data
            else:
                cookies_str = ""
            
            logger.debug(f"[115] cookies_str: {cookies_str[:100]}...")
            
            if not cookies_str:
                return {"status": -1, "message": "登录成功但未获取到 cookies"}
            
            resp = {
                "status": 2,
                "message": "登录成功",
                "cookies": cookies_str,
                # 扫码结果中已包含用户信息，优先使用
                "user_id": str(data.get("user_id", "") or ""),
                "username": data.get("user_name", "") or "",
                "avatar_url": (data.get("face") or {}).get("face_m", "") or "",
            }
            
            # 扫码结果缺少用户信息时，再调用接口补全
            if not resp["user_id"]:
                user_info = await _run_in_thread(cls._get_user_info_sync, cookies_str)
                logger.debug(f"[115] user_info: {user_info}")
                resp.update({k: v for k, v in user_info.items() if v})
            
            # 缓存成功结果（保留 app，供保存账号时读取设备类型）
            cls._qrcode_data.pop(uid, None)
            resp["app"] = app
            cls._completed[uid] = {"time": _time.time(), "result": resp, "app": app}
            return resp
            
        except Exception as e:
            logger.debug(f"[115] _handle_login_success error: {e}")
            cls._qrcode_data.pop(uid, None)
            return {"status": -1, "message": f"登录失败: {str(e)}"}
    
    @classmethod
    def _get_user_info_sync(cls, cookies: str) -> dict:
        """同步获取用户信息"""
        try:
            client = P115Client(cookies)
            result = client.user_info()
            return {
                "user_id": str(result.get("user_id", "")),
                "username": result.get("user_name", "")
            }
        except Exception as e:
            logger.warning(f"[115] 获取用户信息失败: {e}")
            return {"user_id": "", "username": ""}
    
    @classmethod
    def create_client_from_cookies(cls, cookies: str) -> P115Client:
        """创建或复用 P115Client 实例（按 cookies 哈希缓存，减少重复初始化开销）"""
        cookies_hash = hashlib.md5(cookies.encode()).hexdigest()
        with _clients_cache_lock:
            client = _clients_cache.get(cookies_hash)
            if client is not None:
                # LRU: 移到末尾表示最近使用
                _clients_cache.move_to_end(cookies_hash)
                return client
            client = P115Client(cookies)
            _clients_cache[cookies_hash] = client
            # 超出上限时淘汰最久未使用的
            while len(_clients_cache) > _CLIENTS_CACHE_MAX:
                _clients_cache.popitem(last=False)
            return client
    
    @classmethod
    def get_client(cls, account_id: int, cookies: str) -> P115Client:
        """获取缓存的 P115Client（兼容旧接口，实际按 cookies 缓存）"""
        return cls.create_client_from_cookies(cookies)
    
    @classmethod
    def remove_client(cls, account_id: int = None, cookies: str = None):
        """清除客户端缓存。传入 cookies 时清除对应缓存，否则清空全部。"""
        with _clients_cache_lock:
            if cookies:
                cookies_hash = hashlib.md5(cookies.encode()).hexdigest()
                _clients_cache.pop(cookies_hash, None)
            else:
                _clients_cache.clear()
    
    @classmethod
    def check_cookies_valid(cls, cookies: str) -> tuple[bool, dict]:
        """检测 cookies 可用性，并返回账号详情（含空间容量）"""
        try:
            client = cls.create_client_from_cookies(cookies)
            user_info = client.user_info()
            data = user_info.get("data", user_info) if isinstance(user_info, dict) else {}

            info = {
                "user_id": str(data.get("user_id", "") or ""),
                "username": data.get("user_name", "") or data.get("uname", "") or "",
                "vip_level": 0,
                "space_used": 0,
                "space_total": 0,
                "avatar_url": "",
            }

            # VIP / 头像（web cookies 场景 user_info 会带 vip 信息）
            vip = data.get("vip") or {}
            if isinstance(vip, dict):
                info["vip_level"] = int(vip.get("level", 0) or 0)
            face = data.get("face") or {}
            if isinstance(face, dict):
                info["avatar_url"] = face.get("face_m", "") or ""

            # 空间容量（接口失败不影响可用性判断）
            try:
                space = client.user_space_info()
                sdata = space.get("data", space) if isinstance(space, dict) else {}
                # 兼容多种返回结构
                for used_key, total_key in (
                    ("all_use", "all_total"),
                    ("used", "total"),
                    ("use_size", "total_size"),
                ):
                    if used_key in sdata or total_key in sdata:
                        used_v = sdata.get(used_key, 0)
                        total_v = sdata.get(total_key, 0)
                        info["space_used"] = cls._to_bytes(used_v)
                        info["space_total"] = cls._to_bytes(total_v)
                        break
            except Exception as e:
                logger.warning(f"[115] 获取空间信息失败: {e}")

            return True, info
        except Exception as e:
            return False, {"error": str(e)}

    @staticmethod
    def _to_bytes(v) -> int:
        """兼容数字或 {'size': n} 结构的容量值"""
        if isinstance(v, dict):
            v = v.get("size", 0)
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    
    @classmethod
    def list_files(cls, cookies: str, cid: str = "0", offset: int = 0, limit: int = 100) -> dict:
        try:
            client = cls.create_client_from_cookies(cookies)
            result = client.fs_files({
                "cid": cid,
                "offset": offset,
                "limit": limit,
                "show_dir": 1,
            })
            return result
        except Exception as e:
            # 用 _error 作为异常标识，避免与 115 返回的 error 字段冲突
            return {"_error": str(e)}
    
    # 统一的 User-Agent，获取直链和下载文件时必须一致
    DOWNLOAD_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    @classmethod
    def get_download_url(cls, cookies: str, pickcode: str, account_id: int = 0) -> Optional[str]:
        """获取 115 文件下载链接，带 TTL 缓存"""
        if not pickcode:
            return None
        # 检查缓存
        with _cache_lock:
            cached = _DOWNLOAD_URL_CACHE.get(pickcode)
            if cached and (_time.time() - cached["ts"]) < _DOWNLOAD_URL_TTL:
                return cached["url"]
        # 实时获取（指定 user_agent，下载时必须用同一个）
        try:
            client = cls.create_client_from_cookies(cookies)
            result = client.download_url(pickcode, user_agent=cls.DOWNLOAD_USER_AGENT)
            # p115client 可能返回 str 或 dict，统一提取 URL
            if isinstance(result, dict):
                url = result.get("url") or result.get("data", {}).get("url") if isinstance(result.get("data"), dict) else None
                if not url:
                    url = str(result) if result else None
            else:
                url = str(result) if result else None
            if url:
                with _cache_lock:
                    _DOWNLOAD_URL_CACHE[pickcode] = {
                        "url": url, "ts": _time.time(), "account_id": account_id,
                        "user_agent": cls.DOWNLOAD_USER_AGENT,
                    }
                    # 清理过期条目，防止内存泄漏
                    if len(_DOWNLOAD_URL_CACHE) > 5000:
                        cutoff = _time.time() - _DOWNLOAD_URL_TTL
                        expired = [k for k, v in _DOWNLOAD_URL_CACHE.items() if v["ts"] < cutoff]
                        for k in expired:
                            _DOWNLOAD_URL_CACHE.pop(k, None)
            return url
        except Exception as e:
            logger.warning(f"[115] get_download_url 失败 pickcode={pickcode}: {e}")
            return None

    @classmethod
    def get_download_url_with_headers(cls, cookies: str, pickcode: str, account_id: int = 0) -> Optional[dict]:
        """
        获取 115 文件下载链接及所需的 user_agent。
        返回 {"url": str, "user_agent": str} 或 None
        115 CDN 要求下载时的 user-agent 必须与获取直链时一致（f=1 参数控制）
        """
        if not pickcode:
            return None
        # 先检查缓存
        with _cache_lock:
            cached = _DOWNLOAD_URL_CACHE.get(pickcode)
            if cached and (_time.time() - cached["ts"]) < _DOWNLOAD_URL_TTL:
                return {"url": cached["url"], "user_agent": cached.get("user_agent", cls.DOWNLOAD_USER_AGENT)}
        # 调用 get_download_url 填充缓存
        url = cls.get_download_url(cookies, pickcode, account_id)
        if not url:
            return None
        with _cache_lock:
            cached = _DOWNLOAD_URL_CACHE.get(pickcode, {})
            return {"url": cached.get("url"), "user_agent": cached.get("user_agent", cls.DOWNLOAD_USER_AGENT)}

    @classmethod
    def invalidate_download_url_cache(cls, pickcode: str = None):
        """清除直链缓存（pickcode 为 None 时清除全部）"""
        with _cache_lock:
            if pickcode:
                _DOWNLOAD_URL_CACHE.pop(pickcode, None)
            else:
                _DOWNLOAD_URL_CACHE.clear()

    # ============ 网盘整理操作（同步方法，供整理服务在线程池调用） ============

    @classmethod
    def list_all_files(cls, cookies: str, cid: str, video_exts: set[str],
                       min_size: int = 0, recursive: bool = True,
                       exclude_cids: set[str] = None) -> list[dict]:
        """
        递归列出目录下的所有视频文件
        exclude_cids: 需要跳过的子目录 cid 集合（不递归进入这些目录）
        返回: [{"file_id", "pickcode", "name", "size", "parent_id", "parent_path"}]
        """
        client = cls.create_client_from_cookies(cookies)
        videos: list[dict] = []
        exclude_cids = exclude_cids or set()

        def _walk(dir_cid: str, dir_rel_path: str):
            offset = 0
            while True:
                resp = _fs_files_with_retry(client, {
                    "cid": dir_cid, "offset": offset, "limit": 1000, "show_dir": 1,
                })
                items = resp.get("data", []) or []
                if not items:
                    break
                for it in items:
                    is_dir = not it.get("fid")
                    name = it.get("n", "")
                    if is_dir:
                        if recursive:
                            child_cid = str(it.get("cid", ""))
                            # 跳过排除的子目录
                            if child_cid in exclude_cids:
                                continue
                            child_path = f"{dir_rel_path}/{name}" if dir_rel_path else name
                            _walk(child_cid, child_path)
                    else:
                        size = it.get("s", 0) or 0
                        ext = ("." + name.rsplit(".", 1)[-1]).lower() if "." in name else ""
                        if ext in video_exts and size >= min_size:
                            videos.append({
                                "file_id": it.get("fid"),
                                "pickcode": it.get("pc", ""),
                                "name": name,
                                "size": size,
                                "parent_id": dir_cid,
                                "parent_path": dir_rel_path,
                            })
                total = resp.get("count", 0)
                offset += len(items)
                if offset >= total:
                    break
                # 分页请求间隔，由用户配置
                _interval = get_api_intervals().get("file_list_interval", 0.3)
                if _interval > 0:
                    _time.sleep(_interval)

        _walk(cid, "")
        return videos

    @classmethod
    def mkdir(cls, cookies: str, name: str, parent_id: str = "0") -> Optional[str]:
        """
        新建目录，返回目录 ID。如已存在则返回已存在目录 ID。
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.fs_mkdir({"cname": name, "pid": parent_id})
            # 成功返回 {"cid": ...} 或 {"file_id": ...}
            cid = resp.get("cid") or resp.get("file_id") or resp.get("category_id")
            if cid:
                return str(cid)
            # 目录已存在：115 返回 errno=20004，需要查找已有目录
            return cls._find_subdir(client, name, parent_id)
        except Exception as e:
            # 尝试查找已存在目录
            found = cls._find_subdir(client, name, parent_id)
            if found:
                return found
            logger.warning(f"[115] mkdir 失败 {name}: {e}")
            return None

    @classmethod
    def _find_subdir(cls, client, name: str, parent_id: str) -> Optional[str]:
        """在父目录下查找同名子目录，返回其 cid"""
        try:
            offset = 0
            while True:
                resp = client.fs_files({
                    "cid": parent_id, "offset": offset, "limit": 1000, "show_dir": 1,
                })
                items = resp.get("data", []) or []
                if not items:
                    break
                for it in items:
                    if not it.get("fid") and it.get("n") == name:
                        return str(it.get("cid"))
                total = resp.get("count", 0)
                offset += len(items)
                if offset >= total:
                    break
        except Exception:
            pass
        return None

    @classmethod
    def ensure_path(cls, cookies: str, parts: list[str], root_id: str = "0") -> Optional[str]:
        """
        确保多级目录存在，逐级创建，返回最终目录 ID
        parts: ["电影", "毕正明的证明 (2025)"]
        """
        current = root_id
        for part in parts:
            if not part:
                continue
            current = cls.mkdir(cookies, part, current)
            if not current:
                return None
        return current

    @classmethod
    def rename(cls, cookies: str, file_id: str, new_name: str) -> bool:
        """重命名文件或目录

        注意：fs_rename 接受单个元组 (file_id, new_name) 或 dict，
        不能传列表。返回 dict 含 state 字段，需检查。
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.fs_rename((file_id, new_name))
            if isinstance(resp, dict) and resp.get("state") is False:
                logger.warning(f"[115] rename 失败 {file_id} -> {new_name}: {resp.get('error', '')}")
                return False
            return True
        except Exception as e:
            logger.warning(f"[115] rename 失败 {file_id} -> {new_name}: {e}")
            return False

    @classmethod
    def move(cls, cookies: str, file_ids: list[str], dest_id: str) -> bool:
        """移动文件到目标目录

        注意：fs_move 的第一个参数是位置参数 payload，
        传 list 时 p115client 会自动转为 fid[0]、fid[1] 格式。
        不能传 {"fid": [...]} 因为 API 不接受 fid 为列表。
        返回 dict 含 state 字段，需检查。
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.fs_move(file_ids, pid=dest_id)
            if isinstance(resp, dict) and resp.get("state") is False:
                logger.warning(f"[115] move 失败 -> {dest_id}: {resp.get('error', '')}")
                return False
            return True
        except Exception as e:
            logger.warning(f"[115] move 失败 -> {dest_id}: {e}")
            return False

    @classmethod
    def copy(cls, cookies: str, file_ids: list[str], dest_id: str) -> bool:
        """复制文件到目标目录

        注意：fs_copy 的调用方式与 fs_move 相同。
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.fs_copy(file_ids, pid=dest_id)
            if isinstance(resp, dict) and resp.get("state") is False:
                logger.warning(f"[115] copy 失败 -> {dest_id}: {resp.get('error', '')}")
                return False
            return True
        except Exception as e:
            logger.warning(f"[115] copy 失败 -> {dest_id}: {e}")
            return False

    @classmethod
    def upload_bytes(cls, cookies: str, data: bytes, filename: str, dest_id: str) -> bool:
        """上传字节内容为文件到网盘目录（用于 NFO、图片、整理结果 JSON）

        注意：使用 upload_file_sample 而非 upload_file，因为后者调用的
        uplb.115.com/4.0/initupload.php 接口会返回 405 Method Not Allowed。
        upload_file_sample 使用不同的 API 端点，对小文件上传更稳定。
        """
        import tempfile
        import os
        client = cls.create_client_from_cookies(cookies)
        tmp_path = None
        try:
            # 写入临时文件再上传
            fd, tmp_path = tempfile.mkstemp(suffix=f"_{filename}")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # 使用 upload_file_sample 代替 upload_file
            # upload_file 的 initupload.php 端点返回 405，upload_file_sample 更稳定
            resp = client.upload_file_sample(
                tmp_path,
                pid=dest_id,
                filename=filename,
            )
            if isinstance(resp, dict) and resp.get("state") is False:
                errno = resp.get("errno", "?")
                error = resp.get("error", str(resp)[:200])
                logger.warning(f"[115] upload 失败 {filename}: errno={errno}, error={error}")
                return False
            logger.info(f"[115] upload 成功: {filename}")
            return True
        except Exception as e:
            # 解包 MultipartUploadAbort 异常，记录原始错误
            cause = e.__cause__
            if cause:
                logger.warning(f"[115] upload 异常 {filename}: {type(cause).__name__}: {str(cause)[:200]}")
            else:
                logger.warning(f"[115] upload 异常 {filename}: {type(e).__name__}: {str(e)[:200]}")
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    @classmethod
    def upload_file(cls, cookies: str, local_path: str, filename: str, dest_id: str) -> bool:
        """上传本地文件到 115 网盘目录

        与 upload_bytes 不同，此方法直接上传本地文件，无需写入临时文件。
        适用于 nfo、图片等元数据文件的上传同步。
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.upload_file_sample(
                local_path,
                pid=dest_id,
                filename=filename,
            )
            if isinstance(resp, dict) and resp.get("state") is False:
                errno = resp.get("errno", "?")
                error = resp.get("error", str(resp)[:200])
                logger.warning(f"[115] upload_file 失败 {filename}: errno={errno}, error={error}")
                return False
            logger.info(f"[115] upload_file 成功: {filename}")
            return True
        except Exception as e:
            cause = e.__cause__
            if cause:
                logger.warning(f"[115] upload_file 异常 {filename}: {type(cause).__name__}: {str(cause)[:200]}")
            else:
                logger.warning(f"[115] upload_file 异常 {filename}: {type(e).__name__}: {str(e)[:200]}")
            return False

    # ============ STRM 同步专用方法 ============

    @classmethod
    def list_all_files_with_meta(cls, cookies: str, cid: str, exts: set,
                                  min_size: int = 0, excludes: list = None,
                                  recursive: bool = True) -> list:
        """
        递归列出目录下所有符合扩展名规则的文件（视频 + 元数据）
        返回: [{"file_id", "pickcode", "name", "size", "parent_id",
                "parent_path", "sha1"}]
        - exts: 允许的扩展名集合（带点小写）
        - min_size: 视频文件最小大小（字节），元数据文件不限制
        - excludes: 排除关键字列表（小写）
        - parent_path: 相对于同步根目录的完整相对路径（如 "电影/2025/某片"）
        """
        excludes = excludes or []
        client = cls.create_client_from_cookies(cookies)
        # 视频扩展名集合（用于判断是否应用 min_size）
        video_exts = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".m2ts", ".rmvb", ".iso"}
        results: list = []
        results_lock = threading.Lock()

        def _process_dir(dir_cid: str, dir_rel_path: str) -> list:
            """列出单个目录下的文件，返回子目录列表供并发处理"""
            subdirs = []
            offset = 0
            while True:
                try:
                    resp = _fs_files_with_retry(client, {
                        "cid": dir_cid, "offset": offset, "limit": 1150, "show_dir": 1,
                    })
                except Exception as e:
                    logger.warning(f"[115] list files failed cid={dir_cid}: {e}")
                    break
                items = resp.get("data", []) or []
                if not items:
                    break
                for it in items:
                    is_dir = not it.get("fid")
                    name = it.get("n", "")
                    # 排除规则
                    if name and excludes:
                        n_lower = name.lower()
                        if any(k in n_lower for k in excludes):
                            continue
                    if is_dir:
                        if recursive:
                            child_path = f"{dir_rel_path}/{name}" if dir_rel_path else name
                            subdirs.append((it.get("cid"), child_path))
                    else:
                        size = it.get("s", 0) or 0
                        ext = ("." + name.rsplit(".", 1)[-1]).lower() if "." in name else ""
                        if ext not in exts:
                            continue
                        # 视频文件检查大小
                        if ext in video_exts and size < min_size:
                            continue
                        with results_lock:
                            results.append({
                                "file_id": str(it.get("fid", "")),
                                "pickcode": it.get("pc", ""),
                                "name": name,
                                "size": size,
                                "parent_id": str(dir_cid),
                                "parent_path": dir_rel_path,
                                "sha1": it.get("sha", ""),
                            })
                total = resp.get("count", 0)
                offset += len(items)
                if offset >= total:
                    break
                # 分页请求间隔，由用户配置
                _interval = get_api_intervals().get("file_list_interval", 0.3)
                if _interval > 0:
                    _time.sleep(_interval)
            return subdirs

        # BFS + 并发：用线程池并发处理目录
        dir_queue = deque()
        dir_queue.append((cid, ""))
        dir_workers = 4
        with ThreadPoolExecutor(max_workers=dir_workers) as pool:
            futures: set = set()
            while dir_queue or futures:
                # 提交队列中的目录，填满 worker 槽位
                while dir_queue and len(futures) < dir_workers:
                    d_cid, d_path = dir_queue.popleft()
                    futures.add(pool.submit(_process_dir, d_cid, d_path))
                # 等待至少一个完成，批量收割所有已完成 future
                if futures:
                    done, futures = wait(futures, return_when=FIRST_COMPLETED)
                    for fut in done:
                        subdirs = fut.result()
                        for sub_cid, sub_path in subdirs:
                            dir_queue.append((sub_cid, sub_path))
        return results

    @classmethod
    def download_file(cls, cookies: str, pickcode: str, local_path: str) -> bool:
        """
        下载 115 文件到本地（用于元数据/字幕文件下载）
        遇到 403 时自动清除缓存并重试（链接可能已过期或 CDN 临时拒绝）
        """
        if not pickcode:
            return False
        import os
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)

        last_error = None
        for attempt in range(3):
            try:
                # 重试时清除缓存，强制获取新链接
                if attempt > 0:
                    cls.invalidate_download_url_cache(pickcode)
                    _time.sleep(2)

                dl_info = cls.get_download_url_with_headers(cookies, pickcode)
                if not dl_info or not dl_info.get("url"):
                    last_error = "获取下载链接失败"
                    continue

                url = dl_info["url"]
                # 必须使用获取直链时相同的 user_agent，否则 115 CDN 返回 403
                ua = dl_info.get("user_agent") or cls.DOWNLOAD_USER_AGENT
                headers = {
                    "User-Agent": ua,
                    "Referer": "https://115.com/",
                    "Accept": "*/*",
                }
                with httpx.Client(timeout=60.0, follow_redirects=True, headers=headers) as c:
                    with c.stream("GET", url) as r:
                        if r.status_code == 403:
                            # 链接可能已过期，清除缓存后重试
                            last_error = f"403 Forbidden (attempt {attempt+1})"
                            continue
                        r.raise_for_status()
                        with open(local_path, "wb") as f:
                            for chunk in r.iter_bytes(chunk_size=65536):
                                f.write(chunk)
                return True
            except Exception as e:
                last_error = str(e)
                if attempt < 2:
                    cls.invalidate_download_url_cache(pickcode)
                    _time.sleep(2)

        logger.warning(f"[115] download_file 失败 {local_path}: {last_error}")
        return False

    # ============ 特色工具：删除/回收站操作 ============

    @classmethod
    def delete_files(cls, cookies: str, file_ids: list[str]) -> dict:
        """删除文件或目录（移入回收站），返回 115 API 响应"""
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.fs_delete(file_ids)
            return resp
        except Exception as e:
            logger.warning(f"[115] fs_delete 失败: {e}")
            return {"error": str(e)}

    @classmethod
    def list_all_items(cls, cookies: str, cid: str, recursive: bool = True) -> list[dict]:
        """
        递归列出目录下所有文件和子目录（用于清空文件夹）
        返回: [{"id", "name", "is_dir", "size"}]
        """
        client = cls.create_client_from_cookies(cookies)
        items: list[dict] = []

        def _walk(dir_cid: str):
            offset = 0
            while True:
                resp = _fs_files_with_retry(client, {
                    "cid": dir_cid, "offset": offset, "limit": 1000, "show_dir": 1,
                })
                data = resp.get("data", []) or []
                if not data:
                    break
                for it in data:
                    is_dir = not it.get("fid")
                    items.append({
                        "id": str(it.get("cid") or it.get("fid") or ""),
                        "name": it.get("n", ""),
                        "is_dir": is_dir,
                        "size": it.get("s", 0) or 0,
                    })
                    if is_dir and recursive:
                        _walk(it.get("cid"))
                total = resp.get("count", 0)
                offset += len(data)
                if offset >= total:
                    break
                # 分页请求间隔，由用户配置
                _interval = get_api_intervals().get("file_list_interval", 0.3)
                if _interval > 0:
                    _time.sleep(_interval)

        _walk(cid)
        return items

    @classmethod
    def clean_recycle_bin(cls, cookies: str) -> dict:
        """清空 115 回收站"""
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.recyclebin_clean()
            return resp
        except Exception as e:
            logger.warning(f"[115] recyclebin_clean 失败: {e}")
            return {"error": str(e)}
