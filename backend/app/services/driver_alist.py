"""
Alist / OpenList 网关驱动（feature #5）
========================================

将 Alist / OpenList（兼容同一套 API）作为 STRMhub 的统一网盘网关：一个 Alist 实例
背后可挂载 115/阿里/百度/OneDrive 等多种存储，STRMhub 通过其 REST API 列目录、取直链，
从而间接支持更多云盘，无需为每个云盘单独写驱动。

配置（settings.json 的 driver_alist 键）：
- base_url: Alist 地址，如 http://192.168.1.10:5244
- token:    可选，Alist 令牌（有则优先用，免登录）
- username/password: 可选，用于自动登录换取 token
- default_path: 可选，默认根路径

无第三方依赖（纯 httpx）。凭证缺失/实例不可达时驱动方法返回空/False，不影响主应用。

Alist API 参考：
- POST /api/auth/login          {username,password} -> data.token
- POST /api/fs/list             {path,password,page,per_page} -> data.content[]
- POST /api/fs/get              {path,password} -> data.raw_url（直链）
- POST /api/fs/mkdir            {path}
- POST /api/fs/remove           {dir, names[]}
- POST /api/fs/move             {src_dir, dst_dir, names[]}
"""
import threading
import time as _time
from typing import Optional

import httpx

from app.core.logbuffer import get_logger
from app.core.json_storage import read_setting
from app.services.driver_base import BaseDriver, get_driver_registry

logger = get_logger("app.services.driver_alist")


class AlistClient:
    """极简 Alist/OpenList REST 客户端（登录 + 文件操作）。"""

    def __init__(self, base_url: str, token: str = "", username: str = "", password: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self._username = username or ""
        self._password = password or ""
        self._token_ts = 0.0
        self._lock = threading.Lock()

    def _login(self) -> str:
        """用户名密码登录换取 token（缓存 ~2 天）。"""
        if not (self._username and self._password):
            return ""
        try:
            r = httpx.post(
                f"{self.base_url}/api/auth/login",
                json={"username": self._username, "password": self._password},
                timeout=15.0,
            )
            data = r.json()
            if data.get("code") == 200:
                self._token = data["data"]["token"]
                self._token_ts = _time.time()
                return self._token
            logger.warning(f"[alist] 登录失败: {data.get('message')}")
        except Exception as e:
            logger.warning(f"[alist] 登录异常: {e}")
        return ""

    def _get_token(self) -> str:
        """获取有效 token：静态 token 直接用；否则登录换取并缓存 2 天。"""
        with self._lock:
            if self._token and (self._username == "" or _time.time() - self._token_ts < 172800):
                return self._token
            return self._login()

    def _headers(self) -> dict:
        tok = self._get_token()
        return {"Authorization": tok} if tok else {}

    def _post(self, path: str, payload: dict, timeout: float = 30.0) -> dict:
        try:
            r = httpx.post(f"{self.base_url}{path}", json=payload, headers=self._headers(), timeout=timeout)
            return r.json()
        except Exception as e:
            logger.warning(f"[alist] 请求 {path} 异常: {e}")
            return {"code": -1, "message": str(e)}

    def list_dir(self, path: str, page: int = 1, per_page: int = 0) -> list[dict]:
        """列目录，返回 content 列表（含 name/is_dir/size）。"""
        resp = self._post("/api/fs/list", {"path": path or "/", "password": "",
                                           "page": page, "per_page": per_page, "refresh": False})
        if resp.get("code") != 200:
            return []
        return (resp.get("data") or {}).get("content") or []

    def get_raw_url(self, path: str) -> str:
        """取文件直链（raw_url）。"""
        resp = self._post("/api/fs/get", {"path": path, "password": ""})
        if resp.get("code") != 200:
            return ""
        return (resp.get("data") or {}).get("raw_url") or ""

    def mkdir(self, path: str) -> bool:
        return self._post("/api/fs/mkdir", {"path": path}).get("code") == 200

    def remove(self, dir_path: str, names: list[str]) -> bool:
        return self._post("/api/fs/remove", {"dir": dir_path, "names": names}).get("code") == 200

    def move(self, src_dir: str, dst_dir: str, names: list[str]) -> bool:
        return self._post("/api/fs/move", {"src_dir": src_dir, "dst_dir": dst_dir, "names": names}).get("code") == 200

    def ping(self) -> bool:
        """连通性检测（列根目录）。"""
        try:
            r = httpx.get(f"{self.base_url}/api/public/settings", timeout=8.0)
            return r.status_code == 200
        except Exception:
            return False


class DriverAlist(BaseDriver):
    """Alist/OpenList 网关驱动（路径式，file_id 即路径）。"""

    def __init__(self):
        self._client_cache: Optional[AlistClient] = None
        self._cache_sig = ""

    @property
    def name(self) -> str:
        return "alist"

    def _load_cfg(self) -> dict:
        return read_setting("driver_alist") or {}

    def _get_client(self) -> Optional[AlistClient]:
        cfg = self._load_cfg()
        base_url = (cfg.get("base_url") or "").strip()
        if not base_url:
            return None
        sig = f"{base_url}|{cfg.get('token','')}|{cfg.get('username','')}"
        if self._client_cache is not None and sig == self._cache_sig:
            return self._client_cache
        self._client_cache = AlistClient(
            base_url, cfg.get("token", ""), cfg.get("username", ""), cfg.get("password", "")
        )
        self._cache_sig = sig
        return self._client_cache

    def is_available(self) -> bool:
        return bool((self._load_cfg().get("base_url") or "").strip())

    async def list_files(self, cid: str, **kwargs) -> list[dict]:
        """列目录。cid 此处为路径（"/" 为根）。"""
        import asyncio
        from app.services.client_115 import _executor
        client = self._get_client()
        if client is None:
            return []
        loop = asyncio.get_event_loop()

        def _do():
            path = cid or "/"
            items = client.list_dir(path)
            mapped = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                name = it.get("name", "")
                is_dir = bool(it.get("is_dir"))
                child_path = (path.rstrip("/") + "/" + name) if path != "/" else "/" + name
                mapped.append({
                    "file_id": child_path,     # 路径即 id
                    "cid": child_path,
                    "name": name,
                    "size": it.get("size", 0) or 0,
                    "is_dir": is_dir,
                    "pickcode": child_path,
                    "parent_id": path,
                })
            return mapped

        return await loop.run_in_executor(_executor, _do)

    async def get_download_url(self, file_id: str, **kwargs) -> str:
        """取直链。file_id 为文件路径。"""
        import asyncio
        from app.services.client_115 import _executor
        client = self._get_client()
        if client is None:
            return ""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, lambda: client.get_raw_url(file_id) or "")

    async def mkdir(self, parent_cid: str, name: str, **kwargs) -> str:
        import asyncio
        from app.services.client_115 import _executor
        client = self._get_client()
        if client is None:
            return ""
        parent = parent_cid or "/"
        new_path = (parent.rstrip("/") + "/" + name) if parent != "/" else "/" + name
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(_executor, lambda: client.mkdir(new_path))
        return new_path if ok else ""

    async def move_files(self, file_ids: list[str], target_cid: str, **kwargs) -> bool:
        import asyncio
        from app.services.client_115 import _executor
        client = self._get_client()
        if client is None:
            return False
        # file_ids 为完整路径；按父目录分组移动
        loop = asyncio.get_event_loop()

        def _do():
            ok_all = True
            by_dir: dict[str, list[str]] = {}
            for p in file_ids:
                d, _, n = p.rpartition("/")
                by_dir.setdefault(d or "/", []).append(n)
            for src_dir, names in by_dir.items():
                if not client.move(src_dir, target_cid or "/", names):
                    ok_all = False
            return ok_all

        return await loop.run_in_executor(_executor, _do)

    async def delete_files(self, file_ids: list[str], **kwargs) -> bool:
        import asyncio
        from app.services.client_115 import _executor
        client = self._get_client()
        if client is None:
            return False
        loop = asyncio.get_event_loop()

        def _do():
            ok_all = True
            by_dir: dict[str, list[str]] = {}
            for p in file_ids:
                d, _, n = p.rpartition("/")
                by_dir.setdefault(d or "/", []).append(n)
            for dir_path, names in by_dir.items():
                if not client.remove(dir_path, names):
                    ok_all = False
            return ok_all

        return await loop.run_in_executor(_executor, _do)

    async def copy_files(self, file_ids: list[str], target_cid: str, **kwargs) -> bool:
        import asyncio
        from app.services.client_115 import _executor
        client = self._get_client()
        if client is None:
            return False
        loop = asyncio.get_event_loop()

        def _do():
            by_dir: dict[str, list[str]] = {}
            for p in file_ids:
                d, _, n = p.rpartition("/")
                by_dir.setdefault(d or "/", []).append(n)
            ok_all = True
            for src_dir, names in by_dir.items():
                resp = client._post("/api/fs/copy", {"src_dir": src_dir, "dst_dir": target_cid or "/", "names": names})
                if resp.get("code") != 200:
                    ok_all = False
            return ok_all

        return await loop.run_in_executor(_executor, _do)


# ===== 注册 Alist 驱动 =====
_driver_alist = DriverAlist()
get_driver_registry().register("alist", _driver_alist)


def get_driver_alist() -> DriverAlist:
    return _driver_alist
