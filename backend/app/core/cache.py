"""
简易内存缓存（LRU + TTL）

为 Emby 仪表盘相关接口提供短期缓存，避免短时间内重复请求 Emby 服务器。
缓存键按 (endpoint + 参数) 计算，TTL 默认 60 秒。
支持 LRU 淘汰策略和锁字典自动清理，防止内存泄漏。
"""
import time
from typing import Any, Callable, Dict, Tuple
from collections import OrderedDict
import asyncio

# 缓存容量上限
_CACHE_MAX = 200

# 全局缓存字典：key -> (expire_ts, data)，使用 OrderedDict 实现 LRU
_CACHE: OrderedDict[str, Tuple[float, Any]] = OrderedDict()
# 锁字典：防止缓存击穿（同一 key 同时只有一个协程真正请求）
_LOCKS: Dict[str, asyncio.Lock] = {}
# 锁字典容量上限
_LOCKS_MAX = 100


def _get_lock(key: str) -> asyncio.Lock:
    """获取指定 key 的锁，超出上限时清理无竞争的锁"""
    if key not in _LOCKS:
        # 锁字典过大时，清理未被占用的锁
        if len(_LOCKS) > _LOCKS_MAX:
            _cleanup_locks()
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


def _cleanup_locks():
    """清理未被持有的锁，释放内存"""
    # 保留当前未被锁定的条目
    keys_to_del = [k for k, lock in _LOCKS.items() if not lock.locked()]
    for k in keys_to_del:
        _LOCKS.pop(k, None)


async def cached(key: str, ttl: int, factory: Callable[[], Any]) -> Any:
    """
    读取缓存；若过期则调用 factory() 获取新数据并写入缓存。
    factory 可以是同步函数或异步函数。
    同一 key 同时只有一个协程会真正执行 factory（防击穿）。
    支持 LRU 淘汰：超过容量上限时移除最久未访问的条目。
    """
    now = time.time()

    # 快速检查：命中缓存且未过期
    hit = _CACHE.get(key)
    if hit and hit[0] > now:
        # LRU: 移到末尾表示最近使用
        _CACHE.move_to_end(key)
        return hit[1]

    lock = _get_lock(key)
    async with lock:
        # 双重检查：拿到锁后可能其他协程已经刷新过
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            _CACHE.move_to_end(key)
            return hit[1]

        # 执行 factory
        result = factory()
        if asyncio.iscoroutine(result):
            result = await result

        _CACHE[key] = (now + ttl, result)
        _CACHE.move_to_end(key)

        # LRU 淘汰：超出容量时移除最久未访问的条目
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)

        return result


def invalidate(prefix: str = "") -> None:
    """清除缓存（按前缀匹配 key）。例如 Emby 配置变更后调用。"""
    if not prefix:
        _CACHE.clear()
        return
    keys_to_del = [k for k in _CACHE if k.startswith(prefix)]
    for k in keys_to_del:
        _CACHE.pop(k, None)


def cache_size() -> int:
    """返回当前缓存条目数"""
    return len(_CACHE)
