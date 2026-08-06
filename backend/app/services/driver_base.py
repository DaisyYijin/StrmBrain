"""
多网盘驱动抽象层 - #31

提供统一的网盘操作接口（BaseDriver）和驱动注册中心（DriverRegistry）。
不同网盘（115、阿里云盘等）可实现 BaseDriver 接口，通过注册中心统一管理。

当前仅注册了 115 驱动（适配 Client115Service），后续可扩展其他网盘。
"""
from abc import ABC, abstractmethod
from typing import Optional

from app.core.logbuffer import get_logger

logger = get_logger("app.services.driver_base")


class BaseDriver(ABC):
    """网盘驱动抽象基类

    定义统一的网盘文件操作接口，各网盘驱动实现这些方法即可接入系统。
    所有方法均为 async，以便在 FastAPI 异步上下文中使用。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """驱动名称（如 '115'、'aliyun'）"""
        ...

    @abstractmethod
    async def list_files(self, cid: str, **kwargs) -> list[dict]:
        """列出指定目录下的文件和子目录

        cid: 目录 ID（"0" = 根目录）
        返回: [{"file_id", "name", "size", "is_dir", "pickcode", "parent_id"}]
        """
        ...

    @abstractmethod
    async def get_download_url(self, file_id: str, **kwargs) -> str:
        """获取文件下载链接

        file_id: 文件 ID（或 pickcode，视网盘而定）
        返回: 下载 URL 字符串，失败返回空字符串
        """
        ...

    @abstractmethod
    async def mkdir(self, parent_cid: str, name: str) -> str:
        """新建目录

        parent_cid: 父目录 ID（"0" = 根目录）
        name: 目录名称
        返回: 新建目录的 ID，失败返回空字符串
        """
        ...

    @abstractmethod
    async def move_files(self, file_ids: list[str], target_cid: str) -> bool:
        """移动文件/目录到目标目录

        file_ids: 要移动的文件/目录 ID 列表
        target_cid: 目标目录 ID
        返回: 是否成功
        """
        ...

    @abstractmethod
    async def delete_files(self, file_ids: list[str]) -> bool:
        """删除文件/目录（移入回收站）

        file_ids: 要删除的文件/目录 ID 列表
        返回: 是否成功
        """
        ...

    @abstractmethod
    async def copy_files(self, file_ids: list[str], target_cid: str) -> bool:
        """复制文件/目录到目标目录

        file_ids: 要复制的文件/目录 ID 列表
        target_cid: 目标目录 ID
        返回: 是否成功
        """
        ...


class DriverRegistry:
    """驱动注册中心

    管理所有已注册的网盘驱动实例，提供注册、获取、列举功能。
    使用单例模式，全局共享一个注册中心。
    """

    _instance: Optional["DriverRegistry"] = None
    _drivers: dict[str, BaseDriver] = {}

    def __new__(cls) -> "DriverRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def register(self, name: str, driver: BaseDriver) -> None:
        """注册驱动实例

        name: 驱动名称（如 '115'）
        driver: BaseDriver 实例
        """
        self._drivers[name] = driver
        logger.info(f"[driver] 已注册网盘驱动: {name}")

    def get(self, name: str) -> Optional[BaseDriver]:
        """获取已注册的驱动实例

        name: 驱动名称
        返回: BaseDriver 实例，未注册时返回 None
        """
        return self._drivers.get(name)

    def list_drivers(self) -> list[str]:
        """列出所有已注册的驱动名称"""
        return list(self._drivers.keys())

    def get_driver_info(self) -> list[dict]:
        """列出所有已注册驱动的详细信息"""
        info = []
        for name, driver in self._drivers.items():
            info.append({
                "name": name,
                "class": type(driver).__name__,
            })
        return info


def get_driver_registry() -> DriverRegistry:
    """获取驱动注册中心单例"""
    return DriverRegistry()


# ===== 115 驱动适配器 =====
# 将 Client115Service 的同步方法包装为 async 接口（通过 run_in_executor 线程池执行）。

class Driver115(BaseDriver):
    """115 网盘驱动适配器

    将 Client115Service 的同步方法包装为 BaseDriver 的 async 接口。
    通过线程池执行同步调用，避免阻塞事件循环。
    """

    @property
    def name(self) -> str:
        return "115"

    def _get_cookies(self, **kwargs) -> str:
        """统一获取 cookies：优先从 kwargs 取，其次用实例已设置的 cookies"""
        return kwargs.get("cookies", getattr(self, "_cookies", ""))

    async def list_files(self, cid: str, **kwargs) -> list[dict]:
        """列出 115 目录下的文件和子目录

        kwargs 支持: cookies (必填), offset, limit
        """
        import asyncio
        from app.services.client_115 import Client115Service, _executor

        cookies = self._get_cookies(**kwargs)
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 100)
        loop = asyncio.get_event_loop()

        def _do():
            result = Client115Service.list_files(cookies, cid, offset, limit)
            if isinstance(result, dict) and result.get("_error"):
                return []
            items = result.get("data", []) if isinstance(result, dict) else []
            mapped = []
            for it in items:
                is_dir = not it.get("fid")
                mapped.append({
                    "file_id": str(it.get("fid") or ""),
                    "cid": str(it.get("cid") or ""),
                    "name": it.get("n", ""),
                    "size": it.get("s", 0) or 0,
                    "is_dir": is_dir,
                    "pickcode": it.get("pc", ""),
                    "parent_id": cid,
                })
            return mapped

        return await loop.run_in_executor(_executor, _do)

    async def get_download_url(self, file_id: str, **kwargs) -> str:
        """获取 115 文件下载链接

        file_id: 实际为 pickcode
        kwargs 支持: cookies (必填), account_id, context
        """
        import asyncio
        from app.services.client_115 import Client115Service, _executor

        cookies = self._get_cookies(**kwargs)
        account_id = kwargs.get("account_id", 0)
        context = kwargs.get("context", "")
        loop = asyncio.get_event_loop()

        def _do():
            url = Client115Service.get_download_url(
                cookies, file_id, account_id, context
            )
            return url or ""

        return await loop.run_in_executor(_executor, _do)

    async def mkdir(self, parent_cid: str, name: str, **kwargs) -> str:
        """在 115 网盘新建目录

        kwargs 支持: cookies (必填)
        """
        import asyncio
        from app.services.client_115 import Client115Service, _executor

        cookies = self._get_cookies(**kwargs)
        loop = asyncio.get_event_loop()

        def _do():
            cid = Client115Service.mkdir(cookies, name, parent_cid)
            return str(cid) if cid else ""

        return await loop.run_in_executor(_executor, _do)

    async def move_files(self, file_ids: list[str], target_cid: str, **kwargs) -> bool:
        """移动 115 文件到目标目录"""
        import asyncio
        from app.services.client_115 import Client115Service, _executor

        cookies = self._get_cookies(**kwargs)
        loop = asyncio.get_event_loop()

        def _do():
            return Client115Service.move(cookies, file_ids, target_cid)

        return await loop.run_in_executor(_executor, _do)

    async def delete_files(self, file_ids: list[str], **kwargs) -> bool:
        """删除 115 文件（移入回收站）"""
        import asyncio
        from app.services.client_115 import Client115Service, _executor

        cookies = self._get_cookies(**kwargs)
        loop = asyncio.get_event_loop()

        def _do():
            result = Client115Service.delete_files(cookies, file_ids)
            return not (isinstance(result, dict) and result.get("error"))

        return await loop.run_in_executor(_executor, _do)

    async def copy_files(self, file_ids: list[str], target_cid: str, **kwargs) -> bool:
        """复制 115 文件到目标目录"""
        import asyncio
        from app.services.client_115 import Client115Service, _executor

        cookies = self._get_cookies(**kwargs)
        loop = asyncio.get_event_loop()

        def _do():
            return Client115Service.copy(cookies, file_ids, target_cid)

        return await loop.run_in_executor(_executor, _do)

    def set_cookies(self, cookies: str) -> None:
        """设置 115 驱动使用的 cookies（调用各方法前需设置）"""
        self._cookies = cookies


# ===== 注册默认驱动 =====
# 注册 115 驱动占位实例（未设置 cookies，仅用于驱动列表展示）
# 实际使用时通过 Driver115().set_cookies(cookies) 设置凭证
_driver_115 = Driver115()
_driver_115._cookies = ""  # 占位，实际使用前需设置
get_driver_registry().register("115", _driver_115)
