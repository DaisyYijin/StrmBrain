"""
115 客户端服务 - 基于 p115client
"""
import time as _time
import hashlib
import threading
from typing import Optional, Callable
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from collections import deque, OrderedDict
import asyncio

import httpx
import json as _json
from p115client import P115Client
try:
    from p115client import P115OpenClient
    _HAS_OPEN_CLIENT = True
except Exception:
    P115OpenClient = None  # type: ignore
    _HAS_OPEN_CLIENT = False

from app.config import COOKIES_DIR

# ===== 115 开放平台 OAuth（feature #10） =====
# 账号可以是 cookie 认证或 OAuth 认证。为了不改动数百处 create_client_from_cookies
# 调用点，OAuth 账号把凭证以哨兵字符串存进 cookies 字段：
#   OPENAUTH:{json}  其中 json = {"access_token","refresh_token","app_id"}
# create_client_from_cookies 检测到该前缀时构造 P115OpenClient（接口与 P115Client 同名）。
_OPENAUTH_PREFIX = "OPENAUTH:"


def _is_openauth(cookies: str) -> bool:
    """判断凭证串是否为开放平台 OAuth 哨兵格式。"""
    return isinstance(cookies, str) and cookies.startswith(_OPENAUTH_PREFIX)


def _parse_openauth(cookies: str) -> dict:
    """解析 OAuth 哨兵串为 {access_token, refresh_token, app_id}。"""
    try:
        return _json.loads(cookies[len(_OPENAUTH_PREFIX):])
    except Exception:
        return {}


def _build_openauth_cookies(access_token: str, refresh_token: str, app_id: int) -> str:
    """把 OAuth 凭证打包为哨兵串存入 cookies 字段。"""
    return _OPENAUTH_PREFIX + _json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "app_id": int(app_id or 0),
    }, ensure_ascii=False)
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

# 并发合并表: pickcode -> {"event": threading.Event, "url": str | None}
# 同一 pickcode 的并发播放请求只向 115 请求一次（参考 LitePan 的 Coalesce 机制），
# 其余线程等待第一个线程的结果，避免多设备同时播放同一文件时重复请求 115。
_inflight_download: dict[str, dict] = {}
_inflight_lock = threading.Lock()

# 115 限流重试次数
_MAX_RETRIES = 3

# 速率限制统计计数器（线程安全）
_rate_limit_stats = {"count": 0, "total_wait": 0.0}
_rate_limit_stats_lock = threading.Lock()

# 失败路径黑名单缓存：(parent_id, name) -> timestamp
# 当 _find_subdir 查询 115 返回 None（目录不存在）时缓存，
# 后续相同查询直接短路返回 None，避免重复 API 调用。
# TTL 300 秒（5 分钟），到期后自动失效，允许重试。
_path_not_found_cache: dict[tuple[str, str], float] = {}
_path_not_found_lock = threading.Lock()
_PATH_NOT_FOUND_TTL = 300  # 5 分钟

# ===== Q4: 写后冷却栅栏（参考 LitePan-main/internal/cache/fence.go 的 mutationFence） =====
# 115 写操作（mkdir/move/rename/delete_files/upload_file）成功后，服务端目录索引
# 存在短暂不一致：此时 _find_subdir 可能读到 stale 数据，且"查无目录"结果会被
# 失败黑名单缓存污染（TTL 5 分钟）。故写操作成功后在 3 秒冷却期内：
# - _find_subdir 跳过失败黑名单缓存查询，直接走 API
# - 目录创建/找到成功后调用 _mark_dir_clear 提前解除冷却
_dir_write_cooldowns: dict[str, float] = {}
_dir_write_lock = threading.Lock()
_DIR_WRITE_COOLDOWN = 3.0  # 秒


def _human_size(num: float) -> str:
    """将字节数格式化为可读字符串（B/KB/MB/GB/TB）。"""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} PB"


def _mark_dir_written(parent_id: str) -> None:
    """记录 parent_id 的写操作时间，使其进入写后冷却期（3 秒）。

    115 写操作成功后调用：冷却期内 _find_subdir 跳过失败黑名单缓存，
    避免读到 stale 的"查无目录"记录。
    """
    if not parent_id:
        return
    with _dir_write_lock:
        _dir_write_cooldowns[str(parent_id)] = _time.time()


def _dir_in_cooldown(parent_id: str) -> bool:
    """判断 parent_id 是否处于写后冷却期。

    冷却期内 _find_subdir 不查失败黑名单缓存，直接走 API。
    冷却已过期时顺手清理记录。
    """
    if not parent_id:
        return False
    with _dir_write_lock:
        ts = _dir_write_cooldowns.get(str(parent_id))
        if ts is None:
            return False
        if _time.time() - ts < _DIR_WRITE_COOLDOWN:
            return True
        # 冷却已过期，清理记录
        _dir_write_cooldowns.pop(str(parent_id), None)
        return False


def _mark_dir_clear(parent_id: str) -> None:
    """清除 parent_id 的写后冷却记录（目录创建/找到成功后调用）。"""
    if not parent_id:
        return
    with _dir_write_lock:
        _dir_write_cooldowns.pop(str(parent_id), None)


def _resolve_parent_ids(client, file_ids, max_items: int = 10) -> list[str]:
    """best-effort 解析文件/目录的父目录 id 列表（通过 fs_file）。

    用于 rename/delete_files 的写后冷却标记：方法签名中没有父目录 id。
    - 文件条目：data["cid"] 即父目录
    - 目录条目：data["pid"] 即父目录（data["cid"] 是自身 id）
    最多解析前 max_items 个，避免大批次操作产生过多额外请求。
    """
    parents: list[str] = []
    for fid in list(file_ids)[:max_items]:
        if not fid:
            continue
        try:
            finfo = client.fs_file(fid)
            pdata = (finfo or {}).get("data") or {}
            if pdata.get("fid"):
                pid = pdata.get("cid")  # 文件：cid 即父目录
            else:
                pid = pdata.get("pid")  # 目录：pid 即父目录
            if pid:
                parents.append(str(pid))
        except Exception:
            continue
    return parents


# ===== O3: OOF 快速媒体信息缓存 =====
# key=sha1 -> {"ts": 时间戳, "data": 结果}，TTL 1 小时，上限 500，锁保护，
# 避免对同一 sha1 重复探测 115。
_oof_cache: dict[str, dict] = {}
_oof_lock = threading.Lock()
_OOF_CACHE_TTL = 3600  # 1 小时
_OOF_CACHE_MAX = 500


# ===== D2: 滑动窗口限流器 =====
# 替换全局固定间隔，实现 per-op 精细限流：
# 每种操作类型维护独立的滑动时间窗口，确保在窗口内不超过最大调用次数。
# 相比固定 sleep(interval)，滑动窗口允许突发请求，平均速率受限。
from collections import deque as _deque


class _SlidingWindowRateLimiter:
    """滑动窗口限流器：per-op 精细限流

    每种操作类型维护一个时间戳队列，超出窗口内的最大请求数时等待。
    相比固定 sleep(interval)：
    - 允许短时间突发（如连续重命名多个小文件）
    - 平均速率不超过配置上限
    - 不同操作类型独立限流（file_list 不阻塞 download_url）
    """

    def __init__(self):
        self._windows: dict[str, _deque] = {}
        self._lock = threading.Lock()

    def acquire(self, op_type: str, max_requests: int = 1, window_seconds: float = 3.0,
                context: str = "") -> float:
        """获取限流许可，必要时等待。

        op_type: 操作类型（如 "download_url", "file_list", "rename", "move", "mkdir"）
        max_requests: 窗口内最大请求数（默认 1）
        window_seconds: 窗口大小（秒，默认 3.0）
        context: 可选的操作描述（用于日志）
        返回: 实际等待时间（秒），0 表示未限流
        """
        if window_seconds <= 0 or max_requests <= 0:
            return 0.0

        wait = 0.0
        with self._lock:
            if op_type not in self._windows:
                self._windows[op_type] = _deque()
            window = self._windows[op_type]
            now = _time.time()
            # 移除窗口外的旧时间戳
            cutoff = now - window_seconds
            while window and window[0] < cutoff:
                window.popleft()
            # 检查是否超过窗口限制
            if len(window) >= max_requests:
                # 计算需要等待的时间
                oldest = window[0]
                wait = oldest + window_seconds - now
                if wait > 0:
                    with _rate_limit_stats_lock:
                        _rate_limit_stats["count"] += 1
                        _rate_limit_stats["total_wait"] += wait
                    if wait >= 1.0:
                        ctx = f" - {context}" if context else ""
                        logger.info(f"[115] 滑动窗口限流 {op_type} 等待 {wait:.1f}s{ctx}...")
                    # 释放锁后等待（不阻塞其他操作类型）
                else:
                    wait = 0.0
            # 记录本次请求时间戳
            window.append(now)

        if wait > 0:
            _time.sleep(wait)
        return wait


# 全局限流器实例（QPS 级别：download_url_interval 秒内最多 1 次）
_rate_limiter = _SlidingWindowRateLimiter()

# P0-2: QPM / QPH 级别限流器（独立实例，全局共享窗口）
_qpm_limiter = _SlidingWindowRateLimiter()   # 每分钟最多 N 次
_qph_limiter = _SlidingWindowRateLimiter()   # 每小时最多 N 次


# ===== P0-2: 请求统计 =====
# 记录最近 10000 条 115 API 请求的时间戳/操作类型/响应时间/是否限流，
# 提供 QPS/QPM/QPH/平均延迟/限流次数/缓存命中率统计。
class _RequestStats:
    """请求统计器：记录最近 10000 条请求，提供多维统计"""

    _MAX_RECORDS = 10000

    def __init__(self):
        self._records: _deque = _deque(maxlen=self._MAX_RECORDS)
        self._lock = threading.Lock()
        self._cache_hits = 0       # 缓存命中次数
        self._cache_misses = 0     # 缓存未命中次数
        self._throttle_count = 0   # 累计限流等待次数

    def record(self, op: str, duration: float, throttled: bool = False):
        """记录一次 API 请求

        op: 操作类型（如 "download_url"）
        duration: 本次请求耗时（秒）
        throttled: 是否被限流等待
        """
        with self._lock:
            self._records.append({
                "ts": _time.time(),
                "op": op,
                "duration": duration,
                "throttled": throttled,
            })
            if throttled:
                self._throttle_count += 1

    def record_cache_hit(self, hit: bool):
        """记录缓存命中/未命中"""
        with self._lock:
            if hit:
                self._cache_hits += 1
            else:
                self._cache_misses += 1

    def get_stats(self) -> dict:
        """返回当前统计快照

        返回: {
            qps, qpm, qph,             # 最近 1s/60s/3600s 内的请求数
            avg_latency,                # 平均延迟（秒）
            throttle_count,             # 累计限流次数
            cache_hit_rate,             # 缓存命中率（0~1）
            total_requests,             # 统计窗口内总请求数
        }
        """
        with self._lock:
            now = _time.time()
            records = list(self._records)
            # QPS/QPM/QPH：按时间窗口统计请求数
            qps = sum(1 for r in records if now - r["ts"] < 1)
            qpm = sum(1 for r in records if now - r["ts"] < 60)
            qph = sum(1 for r in records if now - r["ts"] < 3600)
            # 平均延迟
            if records:
                avg_latency = sum(r["duration"] for r in records) / len(records)
            else:
                avg_latency = 0.0
            # 缓存命中率
            total_cache = self._cache_hits + self._cache_misses
            cache_hit_rate = self._cache_hits / total_cache if total_cache > 0 else 0.0
            return {
                "qps": qps,
                "qpm": qpm,
                "qph": qph,
                "avg_latency": round(avg_latency, 3),
                "throttle_count": self._throttle_count,
                "cache_hit_rate": round(cache_hit_rate, 4),
                "total_requests": len(records),
            }


# 全局请求统计单例
_request_stats = _RequestStats()


# ===== P0-3: 多端播放追踪器 =====
# 记录同一 pickcode 在最近 10 秒内被哪些 UA 请求。
# 当 2+ 个不同 UA 在窗口内请求同一 pickcode 时，视为"多端播放"场景，
# 触发文件复制到 /多端播放/ 目录获取独立直链，避免 115 风控。
_multiplay_tracker: dict[str, dict] = {}   # pickcode -> {"uas": set, "first_ts": float}
_multiplay_copy_map: dict[str, str] = {}    # 原 pickcode -> 副本 pickcode（缓存复制结果）
_multiplay_copy_cid: Optional[str] = None   # /多端播放/ 目录 cid（首次创建后缓存）
_multiplay_lock = threading.Lock()
_MULTIPLAY_WINDOW = 10.0  # 多端播放检测窗口（秒）


# ===== O5: 同账号跨任务互斥 =====
# 同一 115 账号的同步/整理/清理任务串行执行，防止并发触发 115 风控。
# 账号级锁：account_id -> threading.Lock
# 使用 RLock 允许同一线程内嵌套获取（如整理内部调用同步）
_account_task_locks: dict[int, threading.RLock] = {}
_account_task_locks_guard = threading.Lock()


def get_account_lock(account_id: int) -> threading.RLock:
    """获取指定账号的任务锁（可重入）。
    
    同一 account_id 的同步/整理/清理任务通过此锁串行执行，
    避免同账号并发操作触发 115 风控。
    """
    with _account_task_locks_guard:
        if account_id not in _account_task_locks:
            _account_task_locks[account_id] = threading.RLock()
        return _account_task_locks[account_id]


# ===== Q2 + #33: 全局认证状态机（熔断器升级） =====
# 将简单的 _circuit_breaker_until（单时间戳）升级为状态机：
# - state=active：正常工作
# - state=cooldown：连续失败 1-2 次，阶梯退避（30s -> 60s -> 120s）
# - state=failed：连续失败 3+ 次，熔断 5 分钟
# - state=recovered：从熔断恢复，观察期（5 分钟内失败重新进入 cooldown）
# - 网络错误（ConnectionError/Timeout）不计入失败次数

# 状态枚举
_AUTH_STATE_ACTIVE = "active"
_AUTH_STATE_COOLDOWN = "cooldown"
_AUTH_STATE_FAILED = "failed"
_AUTH_STATE_RECOVERED = "recovered"

# 阶梯退避秒数（cooldown 状态用）
_AUTH_SM_COOLDOWN_STEPS = [30, 60, 120]
# 熔断时长（failed 状态）
_AUTH_SM_FAILED_DURATION = 300  # 5 分钟
# 恢复观察期时长（recovered 状态）
_AUTH_SM_RECOVERY_DURATION = 300  # 5 分钟
# 进入 failed 状态的失败次数阈值
_AUTH_SM_FAILED_THRESHOLD = 3

# 全局认证状态机（内存态，线程安全）
_auth_sm: dict = {
    "state": _AUTH_STATE_ACTIVE,
    "fail_count": 0,
    "last_fail_ts": 0.0,
    "cooldown_until": 0.0,
    "recovery_count": 0,
    "recovery_until": 0.0,
    "last_reason": "",
}
_auth_sm_lock = threading.Lock()

# 认证状态机状态的中文标签（日志展示用）
_AUTH_STATE_LABELS = {
    _AUTH_STATE_ACTIVE: "正常",
    _AUTH_STATE_COOLDOWN: "冷却",
    _AUTH_STATE_FAILED: "熔断",
    _AUTH_STATE_RECOVERED: "观察期",
}

# 保留旧变量名兼容（_circuit_breaker_until 仍可被外部读取，由状态机同步维护）
_circuit_breaker_until: float = 0.0
_circuit_breaker_lock = threading.Lock()
_CIRCUIT_BREAKER_DURATION = 60


def _refresh_auth_sm():
    """惰性刷新认证状态机：冷却/熔断到期后自动状态转换。

    - cooldown 到期 -> active
    - failed 到期 -> recovered（进入观察期）
    - recovered 观察期到期 -> active
    """
    now = _time.time()
    with _auth_sm_lock:
        state = _auth_sm["state"]
        if state == _AUTH_STATE_COOLDOWN and now >= _auth_sm["cooldown_until"]:
            _auth_sm["state"] = _AUTH_STATE_ACTIVE
            _auth_sm["cooldown_until"] = 0.0
            global _circuit_breaker_until
            _circuit_breaker_until = 0.0
            logger.info("[115] 认证状态机：冷却期到期，恢复为正常")
        elif state == _AUTH_STATE_FAILED and now >= _auth_sm["cooldown_until"]:
            _auth_sm["state"] = _AUTH_STATE_RECOVERED
            _auth_sm["recovery_until"] = now + _AUTH_SM_RECOVERY_DURATION
            _auth_sm["recovery_count"] += 1
            _auth_sm["cooldown_until"] = 0.0
            _circuit_breaker_until = 0.0
            logger.info("[115] 认证状态机：熔断期到期，进入观察期")
        elif state == _AUTH_STATE_RECOVERED and now >= _auth_sm["recovery_until"]:
            _auth_sm["state"] = _AUTH_STATE_ACTIVE
            _auth_sm["recovery_until"] = 0.0
            _auth_sm["fail_count"] = 0
            logger.info("[115] 认证状态机：观察期结束，恢复为正常")


def _record_auth_failure(reason: str = "", is_network: bool = False):
    """记录一次认证失败（#33 状态机）。

    - 网络类错误（超时/连接失败）不计数，仅记录时间戳，避免误伤
    - active -> cooldown：fail_count=1，冷却 30s
    - cooldown -> cooldown/failed：fail_count++，>=3 进入 failed（5min），否则阶梯退避
    - recovered -> cooldown：观察期内失败，重新进入 cooldown（用最大阶梯 120s）
    - failed：已熔断，忽略新失败
    """
    global _circuit_breaker_until

    if is_network:
        # 网络类错误不计数，仅记录时间戳
        with _auth_sm_lock:
            _auth_sm["last_fail_ts"] = _time.time()
        return

    _refresh_auth_sm()
    now = _time.time()
    with _auth_sm_lock:
        state = _auth_sm["state"]
        _auth_sm["fail_count"] += 1
        _auth_sm["last_fail_ts"] = now
        _auth_sm["last_reason"] = reason[:200] if reason else ""

        if state == _AUTH_STATE_ACTIVE:
            # active -> cooldown（第 1 次失败）
            step = _AUTH_SM_COOLDOWN_STEPS[0]
            _auth_sm["state"] = _AUTH_STATE_COOLDOWN
            _auth_sm["cooldown_until"] = now + step
            _circuit_breaker_until = _auth_sm["cooldown_until"]
            logger.warning(
                f"[115] 认证状态机：正常 -> 冷却，冷却 {step}s。原因: {reason[:100]}"
            )

        elif state == _AUTH_STATE_COOLDOWN:
            if _auth_sm["fail_count"] >= _AUTH_SM_FAILED_THRESHOLD:
                # cooldown -> failed（连续失败 3+ 次）
                _auth_sm["state"] = _AUTH_STATE_FAILED
                _auth_sm["cooldown_until"] = now + _AUTH_SM_FAILED_DURATION
                _circuit_breaker_until = _auth_sm["cooldown_until"]
                logger.warning(
                    f"[115] 认证状态机：冷却 -> 熔断，熔断 {_AUTH_SM_FAILED_DURATION}s。"
                    f"连续失败 {_auth_sm['fail_count']} 次。原因: {reason[:100]}"
                )
            else:
                # cooldown -> cooldown（阶梯退避）
                idx = min(_auth_sm["fail_count"] - 1, len(_AUTH_SM_COOLDOWN_STEPS) - 1)
                step = _AUTH_SM_COOLDOWN_STEPS[idx]
                _auth_sm["cooldown_until"] = now + step
                _circuit_breaker_until = _auth_sm["cooldown_until"]
                logger.warning(
                    f"[115] 认证状态机：冷却升级，冷却 {step}s。"
                    f"连续失败 {_auth_sm['fail_count']} 次。原因: {reason[:100]}"
                )

        elif state == _AUTH_STATE_RECOVERED:
            # recovered -> cooldown（观察期内失败，用最大阶梯 120s）
            step = _AUTH_SM_COOLDOWN_STEPS[-1]
            _auth_sm["state"] = _AUTH_STATE_COOLDOWN
            _auth_sm["cooldown_until"] = now + step
            _auth_sm["recovery_until"] = 0.0
            _circuit_breaker_until = _auth_sm["cooldown_until"]
            logger.warning(
                f"[115] 认证状态机：观察期 -> 冷却（观察期内失败），冷却 {step}s。"
                f"原因: {reason[:100]}"
            )
        # failed 状态：已熔断，忽略新失败


def _record_auth_success():
    """记录一次认证成功（#33 状态机）。

    成功时重置状态机为 active，清除所有失败计数和冷却。
    在 API 调用成功时调用，使状态机从 cooldown/recovered 恢复。
    """
    global _circuit_breaker_until
    with _auth_sm_lock:
        prev_state = _auth_sm["state"]
        if prev_state != _AUTH_STATE_ACTIVE:
            label = _AUTH_STATE_LABELS.get(prev_state, prev_state)
            logger.info(
                f"[115] 认证状态机：{label} -> 正常（认证成功，重置失败状态）"
            )
        _auth_sm["state"] = _AUTH_STATE_ACTIVE
        _auth_sm["fail_count"] = 0
        _auth_sm["cooldown_until"] = 0.0
        _auth_sm["recovery_until"] = 0.0
        _circuit_breaker_until = 0.0


def _trip_circuit_breaker(reason: str = ""):
    """触发全局熔断器（兼容旧接口，内部调用 _record_auth_failure）。

    #33 升级后，熔断器改为阶梯状态机，此函数保留用于兼容已有调用方。
    """
    _record_auth_failure(reason=reason)


def _is_circuit_open() -> bool:
    """检查全局熔断器是否处于开启状态（cooldown 或 failed）"""
    _refresh_auth_sm()
    with _auth_sm_lock:
        return _auth_sm["state"] in (_AUTH_STATE_COOLDOWN, _AUTH_STATE_FAILED)


def _check_circuit_breaker():
    """检查熔断器状态，若开启则抛出异常拒绝调用。

    在所有 115 API 调用入口调用此函数。
    #33: 使用状态机判断，cooldown 和 failed 状态均拒绝调用。
    """
    _refresh_auth_sm()
    with _auth_sm_lock:
        state = _auth_sm["state"]
        if state == _AUTH_STATE_COOLDOWN:
            remaining = max(0, int(_auth_sm["cooldown_until"] - _time.time()))
            raise RuntimeError(
                f"115 认证冷却中（连续失败 {_auth_sm['fail_count']} 次），"
                f"请等待 {remaining}s 后重试"
            )
        if state == _AUTH_STATE_FAILED:
            remaining = max(0, int(_auth_sm["cooldown_until"] - _time.time()))
            raise RuntimeError(
                f"115 全局熔断中（连续失败 {_auth_sm['fail_count']} 次），"
                f"请等待 {remaining}s 后重试"
            )


# ===== Q1: 账号认证状态机（阶梯冷却 + 失败暂停） =====
# 内存态（不持久化），参考 LitePan internal/auth 的 state_machine / cooldown 设计：
# - 连续失败按阶梯冷却：60s / 120s / 300s / 1800s，超出后保持最后一级
# - 累计失败达到阈值后暂停该账号（failed），需手动重置或重新授权
# - 网络类错误（超时/连接失败）不计数，避免误伤（对应 LitePan 的 AuthFailureNetwork）
_AUTH_COOLDOWN_STEPS = [60, 120, 300, 1800]   # 阶梯冷却秒数
_AUTH_FAIL_ACTIVE_LIMIT = 5     # 主动失败阈值（达到后记录警告，提示接近暂停）
_AUTH_FAIL_PASSIVE_LIMIT = 10   # 累计失败达到该次数 → 暂停账号（failed）
_auth_state: dict[int, dict] = {}  # account_id -> {"fail_count","cooldown_level","cooldown_until","failed"}
_auth_state_lock = threading.Lock()


def _is_network_error(exc: Exception) -> bool:
    """判断异常是否为网络类错误（超时/连接失败），用于认证状态机不计数"""
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError,
                            ConnectionError, TimeoutError, OSError))


def _auth_record_success(account_id: int):
    """认证成功：重置失败计数 / 冷却等级 / 暂停标记（若有冷却或暂停则清除）"""
    with _auth_state_lock:
        st = _auth_state.get(account_id)
        if st is None:
            return
        if st["failed"] or st["cooldown_until"] > 0:
            logger.info(f"[115] 账号 {account_id} 认证成功，重置失败状态")
        st["fail_count"] = 0
        st["cooldown_level"] = 0
        st["cooldown_until"] = 0.0
        st["failed"] = False


def _auth_record_failure(account_id: int, is_network_error: bool = False):
    """记录一次认证失败。

    - 网络类错误（超时/连接失败）不计数（仅记录时间戳），避免误伤
    - 其它失败 fail_count+1，并按 fail_count 决定冷却等级（阶梯 60/120/300/1800s，超出用最后一级）
    - fail_count >= _AUTH_FAIL_PASSIVE_LIMIT 时置 failed=True（暂停该账号）
    """
    now = _time.time()
    with _auth_state_lock:
        st = _auth_state.setdefault(account_id, {
            "fail_count": 0, "cooldown_level": 0,
            "cooldown_until": 0.0, "failed": False,
        })
        if is_network_error:
            # 网络类错误不计数，仅记录最近一次网络错误时间戳
            st["last_network_error_ts"] = now
            return
        st["fail_count"] += 1
        # 阶梯冷却：第 1 次 60s、第 2 次 120s、第 3 次 300s、第 4 次起 1800s
        level = min(st["fail_count"], len(_AUTH_COOLDOWN_STEPS)) - 1
        st["cooldown_level"] = level
        st["cooldown_until"] = now + _AUTH_COOLDOWN_STEPS[level]
        if st["fail_count"] >= _AUTH_FAIL_PASSIVE_LIMIT:
            st["failed"] = True
            logger.warning(
                f"[115] 账号 {account_id} 连续失败 {st['fail_count']} 次，"
                f"认证已暂停（需手动重置或重新授权）"
            )
        elif st["fail_count"] == _AUTH_FAIL_ACTIVE_LIMIT:
            logger.warning(
                f"[115] 账号 {account_id} 连续失败 {st['fail_count']} 次，"
                f"即将达到暂停阈值 {_AUTH_FAIL_PASSIVE_LIMIT}"
            )
        else:
            logger.warning(
                f"[115] 账号 {account_id} 认证失败（第 {st['fail_count']} 次），"
                f"冷却 {_AUTH_COOLDOWN_STEPS[level]}s"
            )


def _auth_check_ready(account_id: int) -> Optional[str]:
    """检查账号认证状态是否就绪。

    返回 None 表示就绪；否则返回不可用原因提示：
    - failed：已暂停，需手动重置/重新授权
    - 冷却中：返回剩余秒数提示
    - 冷却已到期：自动恢复（惰性巡检），视为就绪
    """
    now = _time.time()
    with _auth_state_lock:
        st = _auth_state.get(account_id)
        if st is None:
            return None
        if st["failed"]:
            return (f"账号 {account_id} 认证已暂停（连续失败 {st['fail_count']} 次），"
                    f"请手动重置或重新授权")
        if st["cooldown_until"] > now:
            remaining = int(st["cooldown_until"] - now)
            return f"账号 {account_id} 认证冷却中，剩余 {remaining}s 后允许再次尝试"
        if st["cooldown_until"] > 0:
            # 冷却到期，自动恢复（惰性巡检）
            st["cooldown_until"] = 0.0
            st["cooldown_level"] = 0
    return None


def _auth_sweep_cooldowns():
    """后台巡检：冷却到期的账号自动恢复（failed 的账号需手动 _auth_reset 解除）"""
    now = _time.time()
    with _auth_state_lock:
        for account_id, st in list(_auth_state.items()):
            if st["failed"]:
                continue
            if st["cooldown_until"] > 0 and now >= st["cooldown_until"]:
                st["cooldown_until"] = 0.0
                st["cooldown_level"] = 0
                logger.info(f"[115] 账号 {account_id} 认证冷却到期，已自动恢复")


def _auth_reset(account_id: int):
    """手动重置账号认证状态（用户重新授权/手动重试后调用）"""
    with _auth_state_lock:
        st = _auth_state.get(account_id)
        if st is None:
            return
        was_failed = st["failed"]
        st["fail_count"] = 0
        st["cooldown_level"] = 0
        st["cooldown_until"] = 0.0
        st["failed"] = False
    if was_failed:
        logger.info(f"[115] 账号 {account_id} 认证状态已手动重置")


# 115 限流错误码（从异常消息中检测）
_RATE_LIMIT_KEYWORDS = ["访问频率", "频率过高", "too many", "REQUEST_MAX_LIMIT", "请求过于频繁"]


# ===== N6: 响应式限流管理器（参考 qmediasync throttle_manager.go） =====
# 与熔断器（连续失败 → 阶梯冷却/暂停）互补：当 115 主动返回"限流"响应时，
# 立即设置一个全局软冷却窗口（默认 60s），期间所有调用方在入口 _wait_throttle_recovery
# 处短暂等待而非直接失败，冷却到期自动恢复。这是介于"正常限流"和"熔断"之间的中间态：
# 让请求排队等待恢复，而不是像熔断那样抛异常拒绝。
_throttle_until: float = 0.0
_throttle_lock = threading.Lock()
_THROTTLE_COOLDOWN = 60.0        # 检测到限流后的全局冷却秒数
_THROTTLE_MAX_WAIT = 65.0        # 单次等待恢复的最长秒数（防止无限等待）


def _mark_throttled(reason: str = "") -> None:
    """标记进入全局限流冷却窗口（幂等：多次命中只延长到最新窗口）。"""
    global _throttle_until
    with _throttle_lock:
        _throttle_until = _time.time() + _THROTTLE_COOLDOWN
    logger.warning(f"[115] 检测到限流，进入 {_THROTTLE_COOLDOWN:.0f}s 全局冷却: {reason[:120]}")


def _throttle_remaining() -> float:
    """返回全局限流冷却剩余秒数（<=0 表示未在冷却）。"""
    with _throttle_lock:
        return max(0.0, _throttle_until - _time.time())


def _wait_throttle_recovery() -> None:
    """若处于全局限流冷却窗口，则阻塞等待其恢复（最长 _THROTTLE_MAX_WAIT）。

    在 115 API 调用入口调用；等待期间让出线程，冷却到期后继续，避免直接失败。
    """
    remaining = _throttle_remaining()
    if remaining <= 0:
        return
    wait = min(remaining, _THROTTLE_MAX_WAIT)
    logger.info(f"[115] 全局限流冷却中，等待 {wait:.1f}s 后继续")
    _time.sleep(wait)


def get_throttle_status() -> dict:
    """返回响应式限流状态（供监控/仪表盘展示）。"""
    remaining = _throttle_remaining()
    return {
        "throttled": remaining > 0,
        "remaining_seconds": round(remaining, 1),
        "cooldown_seconds": _THROTTLE_COOLDOWN,
    }


def _check_rate_limit_error(exc: Exception) -> bool:
    """检测异常是否为 115 限流，若是则触发熔断器 + 响应式限流冷却。
    返回 True 表示触发了限流处理。
    """
    msg = str(exc)
    for kw in _RATE_LIMIT_KEYWORDS:
        if kw in msg or kw.lower() in msg.lower():
            # N6: 先设置全局软冷却（让后续请求排队等待），再交由熔断器状态机计数
            _mark_throttled(reason=msg[:200])
            _trip_circuit_breaker(reason=msg[:200])
            return True
    return False


def _apply_rate_limit(operation: str = "", context: str = "") -> bool:
    """对 115 API 写操作应用速率限制（重命名、移动、获取下载链接等）

    D2: 使用滑动窗口限流器替代固定 sleep(interval)。
    每种操作类型独立限流，允许突发请求但平均速率受限。
    P0-2: 增加 QPM（每分钟）和 QPH（每小时）级别限流，三级保护。
    context: 可选的操作对象（如文件名），用于等待日志展示当前处理进度
    返回: 是否发生了限流等待（供请求统计使用）
    """
    # N6: 若处于全局限流冷却窗口，先等待恢复（让请求排队而非直接失败）
    _wait_throttle_recovery()
    intervals = get_api_intervals()
    throttled = False
    # 第一级：QPS 限流（download_url_interval 秒内最多 1 次）
    interval = intervals.get("download_url_interval", 3.0)
    if interval > 0:
        wait = _rate_limiter.acquire(
            op_type=operation or "write",
            max_requests=1,
            window_seconds=interval,
            context=context,
        )
        if wait > 0:
            throttled = True
    # P0-2 第二级：QPM 限流（每分钟最多 qpm_limit 次，0=不限）
    qpm_limit = intervals.get("qpm_limit", 0)
    if qpm_limit > 0:
        wait = _qpm_limiter.acquire(
            op_type="qpm_global",
            max_requests=qpm_limit,
            window_seconds=60,
            context=context,
        )
        if wait > 0:
            throttled = True
    # P0-2 第三级：QPH 限流（每小时最多 qph_limit 次，0=不限）
    qph_limit = intervals.get("qph_limit", 0)
    if qph_limit > 0:
        wait = _qph_limiter.acquire(
            op_type="qph_global",
            max_requests=qph_limit,
            window_seconds=3600,
            context=context,
        )
        if wait > 0:
            throttled = True
    return throttled


def _get_retry_cooldown() -> float:
    """获取限流/错误重试的冷却等待时间（秒），跟随用户配置"""
    return get_api_intervals().get("retry_cooldown", 30.0)


def _apply_file_list_interval(context: str = ""):
    """文件列表分页间隔（跟随用户配置，默认 0.3s）
    
    D2: 使用滑动窗口限流器替代固定 sleep(interval)。
    context: 可选的操作对象（如目录名），用于等待日志展示当前进度
    """
    interval = get_api_intervals().get("file_list_interval", 3.0)
    if interval > 0:
        _rate_limiter.acquire(
            op_type="file_list",
            max_requests=1,
            window_seconds=interval,
            context=context,
        )


def get_rate_limit_stats() -> dict:
    """获取速率限制统计并重置计数器"""
    with _rate_limit_stats_lock:
        stats = dict(_rate_limit_stats)
        _rate_limit_stats["count"] = 0
        _rate_limit_stats["total_wait"] = 0.0
    return stats


def _run_in_thread(func, *args, **kwargs):
    """在线程池中运行同步函数"""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, lambda: func(*args, **kwargs))


def _is_rate_limited(exc: Exception) -> bool:
    """判断异常是否为 115 访问频率过高
    
    Q2: 若检测到限流，自动触发全局熔断器。
    #33: 限流视为认证失败，记录到状态机（阶梯退避/熔断）。
    """
    msg = str(exc)
    is_limited = "访问频率" in msg or "频率过高" in msg or "too many" in msg.lower() or "REQUEST_MAX_LIMIT" in msg or "请求过于频繁" in msg
    if is_limited:
        _record_auth_failure(reason=msg[:200])
    return is_limited


def _is_method_not_allowed(exc) -> bool:
    """判断是否为 405 Method Not Allowed 错误"""
    msg = str(exc)
    return "405" in msg or "Method Not Allowed" in msg


# 端点级冷却状态：记录每个端点最近一次限流时间，避免一个端点限流影响全局
_endpoint_cooldowns: dict[str, float] = {}
_endpoint_cooldowns_lock = threading.Lock()


def _is_endpoint_in_cooldown(endpoint: str) -> bool:
    """检查某端点是否仍在冷却期（限流后需要等待冷却时间）"""
    with _endpoint_cooldowns_lock:
        cd_until = _endpoint_cooldowns.get(endpoint, 0)
        return _time.time() < cd_until


def _mark_endpoint_cooldown(endpoint: str, duration: float = 0):
    """标记端点进入冷却期（duration=0 时使用用户配置的 retry_cooldown）"""
    if duration <= 0:
        duration = _get_retry_cooldown()
    with _endpoint_cooldowns_lock:
        _endpoint_cooldowns[endpoint] = _time.time() + duration


def _call_write_with_405_fallback(client, primary_method_name: str, app_method_name: str,
                                   *args, max_retries: int = _MAX_RETRIES,
                                   arg_adapter: Callable = None, **kwargs):
    """
    通用写操作 405 降级：先调 primary_method，405 时降级到 app_method。
    支持限流重试和端点级冷却。

    - primary_method_name: 主方法名（如 'fs_move'）
    - app_method_name: 降级方法名（如 'fs_move_app'）
    - arg_adapter: 可选参数转换函数，降级调用 app_method 时传入
      （如 fs_mkdir 用 {"cname": name}，fs_mkdir_app 用 {"name": name}，需要转换）
    - 返回 API 响应 dict，或抛出最终异常
    """
    primary = getattr(client, primary_method_name, None)
    app_fallback = getattr(client, app_method_name, None)
    cooldown = _get_retry_cooldown()

    for attempt in range(max_retries):
        # 端点级冷却：主端点在冷却期时直接用 app 端点
        use_app = _is_endpoint_in_cooldown(primary_method_name) and app_fallback
        method = app_fallback if use_app else primary
        method_name = app_method_name if use_app else primary_method_name
        if method is None:
            continue

        try:
            if use_app and arg_adapter:
                return method(*arg_adapter(*args, **kwargs))
            return method(*args, **kwargs)
        except Exception as e:
            # Q1: 记录认证失败（限流不计入认证失败；网络类错误不计数；无法识别账号时传 0）
            _rate_limited = _is_rate_limited(e)
            if not _rate_limited:
                _auth_record_failure(0, is_network_error=_is_network_error(e))
            if _is_method_not_allowed(e):
                if app_fallback and not use_app:
                    logger.info(f"[115] {primary_method_name} 返回 405，降级到 {app_method_name}")
                    _mark_endpoint_cooldown(primary_method_name, duration=300)  # 主端点冷却 5 分钟
                    try:
                        if arg_adapter:
                            return app_fallback(*arg_adapter(*args, **kwargs))
                        return app_fallback(*args, **kwargs)
                    except Exception as e2:
                        if _is_method_not_allowed(e2):
                            _mark_endpoint_cooldown(app_method_name, duration=300)
                            if attempt < max_retries - 1:
                                _time.sleep(cooldown)
                                continue
                            raise
                        if _is_rate_limited(e2) and attempt < max_retries - 1:
                            logger.warning(f"[115] {app_method_name} 访问频率过高，等待 {cooldown}s 后重试 (attempt {attempt+1}/{max_retries})")
                            _mark_endpoint_cooldown(app_method_name)
                            _time.sleep(cooldown)
                            continue
                        raise
                if attempt < max_retries - 1:
                    _time.sleep(cooldown)
                    continue
                raise
            if _rate_limited and attempt < max_retries - 1:
                logger.warning(f"[115] {method_name} 访问频率过高，等待 {cooldown}s 后重试 (attempt {attempt+1}/{max_retries})")
                _mark_endpoint_cooldown(method_name)
                _time.sleep(cooldown)
                continue
            raise
    return {}


def _fs_files_with_retry(client, params: dict, max_retries: int = _MAX_RETRIES) -> dict:
    """带限流重试的 fs_files 调用，405 时依次降级: fs_files → fs_files_app → fs_files_aps。

    注意：web 版（fs_files）与 app 版（fs_files_app/fs_files_aps）返回字段名不同：
    - web:  n / fid / cid / pid / s / sha
    - app:  file_name / file_id / category_id / parent_id / file_size / file_sha1
    降级返回后统一做字段标准化（_normalize_fs_resp），保证下游解析一致。
    """
    cooldown = _get_retry_cooldown()  # 冷却时间跟随用户配置
    for attempt in range(max_retries):
        try:
            return _normalize_fs_resp(client.fs_files(params))
        except Exception as e:
            # Q1: 记录认证失败（限流不计入认证失败；网络类错误不计数；无法识别账号时传 0）
            _rate_limited = _is_rate_limited(e)
            if not _rate_limited:
                _auth_record_failure(0, is_network_error=_is_network_error(e))
            # 405 错误：依次降级到 fs_files_app、fs_files_aps
            if _is_method_not_allowed(e):
                logger.info(f"[115] fs_files 返回 405，降级到 fs_files_app")
                try:
                    return _normalize_fs_resp(client.fs_files_app(params))
                except Exception as e2:
                    if _is_method_not_allowed(e2):
                        logger.info(f"[115] fs_files_app 返回 405，降级到 fs_files_aps")
                        try:
                            return _normalize_fs_resp(client.fs_files_aps(params))
                        except Exception as e3:
                            if _is_rate_limited(e3) and attempt < max_retries - 1:
                                logger.warning(f"[115] fs_files_aps 访问频率过高，等待 {cooldown}s 后重试 (attempt {attempt+1}/{max_retries})")
                                _time.sleep(cooldown)
                                continue
                            raise
                    if _is_rate_limited(e2) and attempt < max_retries - 1:
                        logger.warning(f"[115] fs_files_app 访问频率过高，等待 {cooldown}s 后重试 (attempt {attempt+1}/{max_retries})")
                        _time.sleep(cooldown)
                        continue
                    raise
            if _rate_limited and attempt < max_retries - 1:
                logger.warning(f"[115] 访问频率过高，等待 {cooldown}s 后重试 (attempt {attempt+1}/{max_retries})")
                _time.sleep(cooldown)
                continue
            raise
    return {}


def _normalize_fs_item(it: dict) -> dict:
    """将 115 文件列表条目标准化为 web 短字段格式（n/fid/cid/pid/s/sha）。

    兼容 proapi（fs_files_app/fs_files_aps）返回的完整字段：
    file_name/category_name、file_id、category_id、parent_id、file_size、file_sha1。
    已是 web 短字段（含 "n"）的条目原样返回。
    """
    if not isinstance(it, dict):
        return it
    # 已是 web 短字段格式（n/fn），直接返回
    if "n" in it or "fn" in it:
        return it
    if "file_name" not in it and "category_name" not in it:
        return it  # 无法识别的格式，原样返回
    out = dict(it)
    # 判断目录/文件
    is_dir = False
    if "file_category" in it:
        is_dir = int(it.get("file_category") or 0) == 0
    elif "category_id" in it and "file_id" not in it:
        is_dir = True
    elif it.get("file_sha1") in (None, "") and it.get("sha1") in (None, ""):
        is_dir = True
    # 名称
    out["n"] = it.get("category_name") or it.get("file_name") or ""
    if is_dir:
        # 目录：cid=category_id，无 fid
        out["cid"] = it.get("category_id") or it.get("cid") or ""
        out.pop("fid", None)
        if it.get("parent_id"):
            out["pid"] = it.get("parent_id")
    else:
        # 文件：fid=file_id，cid=parent_id（与 web 版一致：文件的 cid 即父目录）
        out["fid"] = it.get("file_id") or it.get("fid") or ""
        out["cid"] = it.get("parent_id") or it.get("cid") or ""
        out["pid"] = it.get("parent_id") or it.get("pid") or ""
    # 大小与哈希
    if "file_size" in it and "s" not in it:
        out["s"] = it.get("file_size") or 0
    if "file_sha1" in it and "sha" not in it:
        out["sha"] = it.get("file_sha1") or ""
    return out


def _normalize_fs_resp(resp: dict) -> dict:
    """对 fs_files 系列响应 data 列表逐项标准化（兼容 proapi 完整字段）。"""
    if not isinstance(resp, dict):
        return resp
    data = resp.get("data")
    if isinstance(data, list):
        resp["data"] = [_normalize_fs_item(it) for it in data]
    return resp


def _extract_download_url(result) -> str:
    """从 p115client 各种下载接口返回值中提取直链 URL（兼容多端点返回格式）"""
    if result is None:
        return ""
    # P115URL / str：直接转字符串
    if isinstance(result, str):
        s = str(result).strip()
        return s if s.startswith("http") else ""
    if isinstance(result, dict):
        # 1. 直接 url 字段
        u = result.get("url")
        if isinstance(u, str) and u.strip().startswith("http"):
            return u.strip()
        # 2. file_url_302 字段（web 接口 302 跳转链接）
        u = result.get("file_url_302")
        if isinstance(u, str) and u.strip().startswith("http"):
            return u.strip()
        # 3. data 字段（dict 或 list）
        data = result.get("data")
        if isinstance(data, dict):
            u = data.get("url")
            if isinstance(u, dict):  # {"url": "https://..."}
                inner = u.get("url")
                if isinstance(inner, str) and inner.strip().startswith("http"):
                    return inner.strip()
            elif isinstance(u, str) and u.strip().startswith("http"):
                return u.strip()
            u = data.get("file_url_302")
            if isinstance(u, str) and u.strip().startswith("http"):
                return u.strip()
        elif isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                u = first.get("url")
                if isinstance(u, dict):
                    inner = u.get("url")
                    if isinstance(inner, str) and inner.strip().startswith("http"):
                        return inner.strip()
                elif isinstance(u, str) and u.strip().startswith("http"):
                    return u.strip()
    return ""


def _validate_download_url(url: str, ua: str) -> bool:
    """P0-1: HEAD 校验直链有效性

    用 httpx.Client 发 HEAD 请求校验 115 直链是否仍然有效。
    - 必须携带与获取时相同的 UA（115 直链与 UA 绑定，f=1 参数控制）
    - 超时 5 秒，HEAD 失败（非 200/206）返回 False
    - HEAD 请求不经过限流器（是校验不是 API 调用，不消耗 115 配额）
    """
    if not url:
        return False
    headers = {
        "User-Agent": ua or Client115Service.DOWNLOAD_USER_AGENT,
        "Referer": "https://115.com/",
    }
    try:
        with httpx.Client(timeout=5.0, follow_redirects=False, headers=headers) as c:
            r = c.head(url)
            return r.status_code in (200, 206)
    except Exception as e:
        logger.debug(f"[115] HEAD 校验直链失败 url={url[:80]}: {e}")
        return False


def _download_url_with_retry(client, pickcode: str, user_agent: str, max_retries: int = _MAX_RETRIES):
    """
    带限流重试的下载链接获取，405 时依次降级:
    download_url → download_url_app → download_url_web2
    返回原始结果（可能为 P115URL / dict / str），由 _extract_download_url 统一提取。
    """
    cooldown = _get_retry_cooldown()  # 冷却时间跟随用户配置
    for attempt in range(max_retries):
        try:
            return client.download_url(pickcode, user_agent=user_agent)
        except Exception as e:
            # 405 错误：依次降级到 download_url_app、download_url_web2
            if _is_method_not_allowed(e):
                logger.info("[115] download_url 返回 405，降级到 download_url_app")
                try:
                    return client.download_url_app(pickcode, user_agent=user_agent)
                except Exception as e2:
                    if _is_method_not_allowed(e2):
                        logger.info("[115] download_url_app 返回 405，降级到 download_url_web2")
                        try:
                            return client.download_url_web2(pickcode, user_agent=user_agent)
                        except Exception as e3:
                            if _is_rate_limited(e3) and attempt < max_retries - 1:
                                logger.warning(f"[115] download_url_web2 访问频率过高，等待 {cooldown}s 后重试 (attempt {attempt+1}/{max_retries})")
                                _time.sleep(cooldown)
                                continue
                            raise
                    if _is_rate_limited(e2) and attempt < max_retries - 1:
                        logger.warning(f"[115] download_url_app 访问频率过高，等待 {cooldown}s 后重试 (attempt {attempt+1}/{max_retries})")
                        _time.sleep(cooldown)
                        continue
                    raise
            if _is_rate_limited(e) and attempt < max_retries - 1:
                logger.warning(f"[115] 访问频率过高，等待 {cooldown}s 后重试 (attempt {attempt+1}/{max_retries})")
                _time.sleep(cooldown)
                continue
            raise
    return None


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

    # ===== 115 开放平台 OAuth 登录（feature #10） =====
    # 开放平台扫码会话缓存（与普通扫码分开，避免混淆）
    _open_qrcode_data: dict = {}
    _open_completed: dict = {}

    @classmethod
    async def get_open_qrcode(cls, app_id: int) -> dict:
        """获取开放平台 OAuth 登录二维码。

        app_id: 115 开放平台应用 ID（在 https://open.115.com 申请）
        返回 {"qrcode_url", "uid"}；app_id 无效时抛异常。
        """
        if not _HAS_OPEN_CLIENT:
            raise RuntimeError("当前 p115client 版本不支持开放平台 OAuth")
        result = await _run_in_thread(P115OpenClient.login_qrcode_token_open, int(app_id))
        if result.get("code"):
            raise RuntimeError(f"无效 AppID 或获取二维码失败: {result.get('error') or result.get('message')}")
        data = result.get("data", {})
        uid = data.get("uid", "")
        # 保存状态查询所需字段 + app_id
        cls._open_qrcode_data[uid] = {
            "token": {"uid": uid, "time": data.get("time"), "sign": data.get("sign")},
            "app_id": int(app_id),
        }
        qrcode_img_url = data.get("qrcode") or f"https://qrcodeapi.115.com/api/1.0/web/1.0/qrcode?uid={uid}"
        return {"qrcode_url": qrcode_img_url, "uid": uid}

    @classmethod
    async def check_open_qrcode_status(cls, uid: str) -> dict:
        """检查开放平台 OAuth 扫码状态；登录成功时换取 access_token/refresh_token。

        返回 {"status", "message", ...}；status=2 且含 openauth 凭证时表示成功。
        """
        if uid in cls._open_completed:
            entry = cls._open_completed[uid]
            if _time.time() - entry["time"] < 60:
                return entry["result"]
            cls._open_completed.pop(uid, None)
        if uid not in cls._open_qrcode_data:
            return {"status": -1, "message": "二维码已过期"}

        sess = cls._open_qrcode_data[uid]
        token = sess["token"]
        app_id = sess["app_id"]
        try:
            status_data = await cls._poll_status(token)
            if not status_data or "status" not in status_data:
                return {"status": 0, "message": "等待扫描..."}
            status_code = status_data.get("status", 0)
            if status_code == 0:
                return {"status": 0, "message": "等待扫描..."}
            if status_code == 1:
                return {"status": 1, "message": "已扫描，请在手机上确认"}
            if status_code == 2:
                return await cls._handle_open_login_success(uid, app_id)
            cls._open_qrcode_data.pop(uid, None)
            return {"status": -1, "message": "二维码已过期" if status_code == -1 else "登录失败"}
        except Exception as e:
            logger.debug(f"[115] check_open_qrcode_status error: {e}")
            return {"status": 0, "message": "等待扫描..."}

    @classmethod
    async def _handle_open_login_success(cls, uid: str, app_id: int) -> dict:
        """扫码确认后换取 access_token/refresh_token 并组装账号凭证。"""
        try:
            token_resp = await _run_in_thread(
                P115OpenClient.login_qrcode_access_token_open, uid
            )
            data = token_resp.get("data", token_resp) if isinstance(token_resp, dict) else {}
            access_token = data.get("access_token", "")
            refresh_token = data.get("refresh_token", "")
            if not access_token:
                return {"status": -1, "message": "登录成功但未获取到 access_token"}

            openauth_cookies = _build_openauth_cookies(access_token, refresh_token, app_id)
            # 获取用户信息（用 open client）
            user_id, username, avatar = "", "", ""
            try:
                client = P115OpenClient(access_token, refresh_token, app_id)
                ui = await _run_in_thread(client.user_info)
                ui_data = ui.get("data", ui) if isinstance(ui, dict) else {}
                user_id = str(ui_data.get("user_id", "") or "")
                username = ui_data.get("user_name", "") or ui_data.get("uname", "") or ""
                avatar = (ui_data.get("face") or {}).get("face_m", "") if isinstance(ui_data.get("face"), dict) else ""
            except Exception as e:
                logger.debug(f"[115] open user_info 获取失败: {e}")

            resp = {
                "status": 2,
                "message": "开放平台登录成功",
                "cookies": openauth_cookies,   # 哨兵串，存入 cookies 字段
                "user_id": user_id,
                "username": username,
                "avatar_url": avatar,
                "app": "open",
                "auth_type": "open",
            }
            cls._open_qrcode_data.pop(uid, None)
            cls._open_completed[uid] = {"time": _time.time(), "result": resp}
            logger.info("[115] 开放平台 OAuth 登录成功")
            return resp
        except Exception as e:
            logger.warning(f"[115] 开放平台登录换取 token 失败: {e}")
            cls._open_qrcode_data.pop(uid, None)
            return {"status": -1, "message": f"登录失败: {str(e)}"}

    @classmethod
    def refresh_open_token(cls, cookies: str) -> Optional[str]:
        """刷新开放平台 access_token，返回新的 openauth 哨兵串（失败返回 None）。

        用于 token 过期时续期；调用方负责把新串写回账号存储。
        """
        if not _is_openauth(cookies) or not _HAS_OPEN_CLIENT:
            return None
        info = _parse_openauth(cookies)
        refresh_token = info.get("refresh_token", "")
        app_id = info.get("app_id", 0)
        if not refresh_token:
            return None
        try:
            resp = P115OpenClient.login_refresh_token_open(refresh_token)
            data = resp.get("data", resp) if isinstance(resp, dict) else {}
            new_access = data.get("access_token", "")
            new_refresh = data.get("refresh_token", refresh_token) or refresh_token
            if not new_access:
                logger.warning(f"[115] 刷新 token 未返回 access_token: {resp}")
                return None
            new_cookies = _build_openauth_cookies(new_access, new_refresh, app_id)
            # 使旧客户端缓存失效
            cls.remove_client(cookies=cookies)
            logger.info("[115] 开放平台 access_token 已刷新")
            return new_cookies
        except Exception as e:
            logger.warning(f"[115] 刷新开放平台 token 失败: {e}")
            return None

    @classmethod
    def create_client_from_cookies(cls, cookies: str, account_id: int = 0, skip_auth_check: bool = False) -> P115Client:
        """创建或复用 P115Client 实例（按 cookies 哈希缓存，减少重复初始化开销）

        Q1: 增加 account_id 参数（默认 0），创建前检查认证状态机：
        - 冷却/暂停中的账号直接抛 RuntimeError，避免继续打 115
        - 现有调用不传 account_id 的默认 0，不受影响
        - skip_auth_check=True 时跳过检查（用于用户主动校验 cookies 等恢复场景，
          校验成功后由调用方调用 _auth_record_success 重置状态）
        """
        # Q1: 认证状态机检查（冷却/暂停时拒绝创建客户端）
        if not skip_auth_check:
            block_reason = _auth_check_ready(account_id)
            if block_reason:
                raise RuntimeError(f"[115] {block_reason}")
        cookies_hash = hashlib.md5(cookies.encode()).hexdigest()
        with _clients_cache_lock:
            client = _clients_cache.get(cookies_hash)
            if client is not None:
                # LRU: 移到末尾表示最近使用
                _clients_cache.move_to_end(cookies_hash)
                return client
            # OAuth 账号：构造 P115OpenClient（凭证存于 cookies 哨兵串）
            if _is_openauth(cookies):
                if not _HAS_OPEN_CLIENT:
                    raise RuntimeError("[115] 当前 p115client 版本不支持开放平台 OAuth")
                info = _parse_openauth(cookies)
                client = P115OpenClient(
                    info.get("access_token", ""),
                    info.get("refresh_token", ""),
                    info.get("app_id", 0),
                )
            else:
                client = P115Client(cookies)
            _clients_cache[cookies_hash] = client
            # 超出上限时淘汰最久未使用的
            while len(_clients_cache) > _CLIENTS_CACHE_MAX:
                _clients_cache.popitem(last=False)
            return client
    
    @classmethod
    def get_client(cls, account_id: int, cookies: str) -> P115Client:
        """获取缓存的 P115Client（兼容旧接口，实际按 cookies 缓存）
        Q1: account_id 透传给认证状态机检查
        """
        return cls.create_client_from_cookies(cookies, account_id=account_id)
    
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
        """检测 cookies 可用性，并返回账号详情（含空间容量）

        Q1: 用户主动校验场景跳过认证状态机检查（避免暂停状态下无法重新校验），
        校验成功时重置认证状态（视为认证成功）。
        """
        try:
            client = cls.create_client_from_cookies(cookies, skip_auth_check=True)
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

            # Q1: 用户主动校验成功 → 视为认证成功，重置失败状态
            _auth_record_success(0)
            return True, info
        except Exception as e:
            return False, {"error": str(e)}

    @classmethod
    def daily_checkin(cls, cookies: str) -> dict:
        """115 每日签到（积分签到）。

        调用 p115client 的 user_points_sign_post 方法执行签到：
        - POST https://proapi.115.com/android/2.0/user/points_sign
        - 注意：不能用 web（浏览器）cookies，否则会失败，需使用 android 等 app 的 cookies
        返回结果 dict：
        - 成功：签到接口返回的 dict（如 {"state": true, "data": {...}}）
        - 失败：{"error": 原因}
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            # 先尝试用 android 签到（p115client 默认 app="android"）
            result = client.user_points_sign_post()
            if not isinstance(result, dict):
                return {"error": f"签到返回异常: {result}"}
            # state=false 时尝试其他 app 类型（部分账号需特定 app 环境）
            if result.get("state") is False:
                for app in ("ios", "alipaymini", "115ios"):
                    try:
                        retry = client.user_points_sign_post(app=app)
                        if isinstance(retry, dict) and retry.get("state") is not False:
                            return retry
                    except Exception:
                        continue
                return result
            return result
        except Exception as e:
            logger.warning(f"[115] 每日签到失败: {e}")
            return {"error": str(e)}

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
            result = _fs_files_with_retry(client, {
                "cid": cid,
                "offset": offset,
                "limit": limit,
                "show_dir": 1,
            })
            return result
        except Exception as e:
            # 用 _error 作为异常标识，避免与 115 返回的 error 字段冲突
            return {"_error": str(e)}

    @classmethod
    def search_files(cls, cookies: str, keyword: str, cid: str = "0",
                     offset: int = 0, limit: int = 40) -> dict:
        """G8: 全盘/指定目录关键词搜索文件和目录。

        调用 115 files/search 接口（fs_search，405 时降级 fs_search_app）。
        keyword: 搜索关键词（文件/目录名）
        cid: 限定搜索的目录 id（"0" 为全盘搜索）
        limit + offset 需 <= 10000（115 接口限制）。
        返回 115 原始响应（含 data 列表、count），异常时返回 {"_error": str}。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return {"_error": "搜索关键词不能为空"}
        # 115 限制 limit + offset <= 10000
        if offset + limit > 10000:
            limit = max(1, 10000 - offset)
        _apply_rate_limit("search")
        try:
            client = cls.create_client_from_cookies(cookies)
            payload = {
                "search_value": keyword,
                "cid": cid,
                "offset": offset,
                "limit": limit,
                "show_dir": 1,
            }
            try:
                result = client.fs_search(payload)
            except Exception as e:
                if _is_method_not_allowed(e):
                    logger.info("[115] fs_search 返回 405，降级到 fs_search_app")
                    result = client.fs_search_app(payload)
                else:
                    raise
            return result if isinstance(result, dict) else {"_error": "搜索返回格式异常"}
        except Exception as e:
            logger.warning(f"[115] 搜索失败 keyword={keyword}: {e}")
            return {"_error": str(e)}
    
    # 统一的 User-Agent，获取直链和下载文件时必须一致
    DOWNLOAD_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    @classmethod
    def get_download_url(cls, cookies: str, pickcode: str, account_id: int = 0, context: str = "") -> Optional[str]:
        """获取 115 文件下载链接，带 TTL 缓存 + 并发合并（Coalesce）
        P0-1: 缓存超过 80% TTL 时 HEAD 校验有效性，失效则重新获取
        P0-2: 记录缓存命中/未命中统计
        context: 可选的操作对象（如文件名），用于等待日志展示当前进度
        """
        _check_circuit_breaker()  # Q2: 熔断器检查
        if not pickcode:
            return None
        # 检查缓存
        cached_url = None
        cached_ua = ""
        need_validate = False
        with _cache_lock:
            cached = _DOWNLOAD_URL_CACHE.get(pickcode)
            if cached and (_time.time() - cached["ts"]) < _DOWNLOAD_URL_TTL:
                cached_url = cached["url"]
                cached_ua = cached.get("user_agent", cls.DOWNLOAD_USER_AGENT)
                # P0-1: 缓存超过 80% TTL 时需 HEAD 校验有效性
                need_validate = (_time.time() - cached["ts"]) > _DOWNLOAD_URL_TTL * 0.8
        # P0-1: 缓存命中后按需 HEAD 校验
        if cached_url:
            if need_validate:
                if _validate_download_url(cached_url, cached_ua):
                    _request_stats.record_cache_hit(True)
                    return cached_url
                # HEAD 校验失败，缓存已失效，清除后重新获取
                with _cache_lock:
                    _DOWNLOAD_URL_CACHE.pop(pickcode, None)
                logger.info(f"[115] 直链缓存 HEAD 校验失败，重新获取 pickcode={pickcode}")
            else:
                _request_stats.record_cache_hit(True)
                return cached_url
        _request_stats.record_cache_hit(False)
        # 并发合并：同一 pickcode 已有一个线程在请求，则等待其结果（参考 LitePan Coalesce）
        with _inflight_lock:
            inflight = _inflight_download.get(pickcode)
            if inflight:
                event = inflight["event"]
            else:
                event = threading.Event()
                _inflight_download[pickcode] = {"event": event, "url": None}
                inflight = None
        if inflight is not None:
            # 已有线程在请求，等待完成
            event.wait(timeout=15)
            with _cache_lock:
                cached = _DOWNLOAD_URL_CACHE.get(pickcode)
                if cached and (_time.time() - cached["ts"]) < _DOWNLOAD_URL_TTL:
                    return cached["url"]
            # 等待超时或失败：降级为自行请求（不做合并）
            inflight = None
        try:
            url = cls._fetch_download_url(cookies, pickcode, account_id, context)
            with _inflight_lock:
                entry = _inflight_download.get(pickcode)
                if entry:
                    entry["event"].set()  # 唤醒等待线程，使其立即从缓存读取结果
                    _inflight_download.pop(pickcode, None)
            return url
        except Exception as e:
            with _inflight_lock:
                entry = _inflight_download.get(pickcode)
                if entry:
                    entry["event"].set()  # 失败也要唤醒，让等待线程降级自行请求
                    _inflight_download.pop(pickcode, None)
            logger.warning(f"[115] get_download_url 失败 pickcode={pickcode}: {e}")
            return None

    @classmethod
    def _fetch_download_url(cls, cookies: str, pickcode: str, account_id: int = 0, context: str = "") -> Optional[str]:
        """实际请求 115 获取直链（被 get_download_url 调用，含缓存写入）
        405 时自动降级备用端点：download_url -> download_url_app -> download_url_web2
        P0-2: 记录请求耗时和限流状态到 _request_stats
        """
        # 实时获取（指定 user_agent，下载时必须用同一个）
        throttled = _apply_rate_limit("download_url", context)
        _start_ts = _time.time()
        try:
            client = cls.create_client_from_cookies(cookies)
            result = _download_url_with_retry(client, pickcode, cls.DOWNLOAD_USER_AGENT)
            url = _extract_download_url(result)
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
            # P0-2: 记录请求统计
            _request_stats.record("download_url", _time.time() - _start_ts, throttled)
            return url or None
        except Exception as e:
            # P0-2: 失败也记录统计
            _request_stats.record("download_url", _time.time() - _start_ts, throttled)
            logger.warning(f"[115] get_download_url 失败 pickcode={pickcode}: {e}")
            return None

    @classmethod
    def get_download_url_with_headers(cls, cookies: str, pickcode: str, account_id: int = 0, context: str = "") -> Optional[dict]:
        """
        获取 115 文件下载链接及所需的 user_agent。
        返回 {"url": str, "user_agent": str} 或 None
        115 CDN 要求下载时的 user-agent 必须与获取直链时一致（f=1 参数控制）
        context: 可选的操作对象（如文件名），用于等待日志展示当前进度
        """
        if not pickcode:
            return None
        # 先检查缓存
        with _cache_lock:
            cached = _DOWNLOAD_URL_CACHE.get(pickcode)
            if cached and (_time.time() - cached["ts"]) < _DOWNLOAD_URL_TTL:
                return {"url": cached["url"], "user_agent": cached.get("user_agent", cls.DOWNLOAD_USER_AGENT)}
        # 调用 get_download_url 填充缓存
        url = cls.get_download_url(cookies, pickcode, account_id, context=context)
        if not url:
            return None
        with _cache_lock:
            cached = _DOWNLOAD_URL_CACHE.get(pickcode, {})
            return {"url": cached.get("url"), "user_agent": cached.get("user_agent", cls.DOWNLOAD_USER_AGENT)}

    @classmethod
    def get_download_url_with_ua(cls, cookies: str, pickcode: str, ua: str, account_id: int = 0, context: str = "") -> Optional[str]:
        """
        使用指定 UA 获取 115 下载链接（带 TTL 缓存，缓存 key 含 UA）。
        115 直链要求下载 UA 与获取 UA 一致（f=1 参数控制），播放器用自己的
        UA 直连时，必须用该 UA 换取直链，否则 115 拒绝 → NoCompatibleStream。
        参考 emby2Alist fetchLastLink：携带客户端 UA 换直链。
        405 时自动降级备用端点：download_url -> download_url_app -> download_url_web2
        P0-1: 缓存超过 80% TTL 时 HEAD 校验有效性，失效则重新获取
        P0-2: 记录请求耗时和缓存命中/未命中统计
        """
        if not pickcode or not ua:
            return None
        # 缓存 key 区分 UA，避免不同客户端互相污染
        cache_key = f"{pickcode}|{ua}"
        # 检查缓存
        cached_url = None
        need_validate = False
        with _cache_lock:
            cached = _DOWNLOAD_URL_CACHE.get(cache_key)
            if cached and (_time.time() - cached["ts"]) < _DOWNLOAD_URL_TTL:
                cached_url = cached["url"]
                # P0-1: 缓存超过 80% TTL 时需 HEAD 校验有效性
                need_validate = (_time.time() - cached["ts"]) > _DOWNLOAD_URL_TTL * 0.8
        # P0-1: 缓存命中后按需 HEAD 校验
        if cached_url:
            if need_validate:
                if _validate_download_url(cached_url, ua):
                    _request_stats.record_cache_hit(True)
                    return cached_url
                # HEAD 校验失败，缓存已失效，清除后重新获取
                with _cache_lock:
                    _DOWNLOAD_URL_CACHE.pop(cache_key, None)
                logger.info(f"[115] 直链缓存 HEAD 校验失败，重新获取 pickcode={pickcode}")
            else:
                _request_stats.record_cache_hit(True)
                return cached_url
        _request_stats.record_cache_hit(False)

        # O4 单飞（singleflight）：同一 pickcode|ua 的并发播放请求（多设备/播放器
        # seek 分片/直链过期后多个 Range 同时刷新）只向 115 请求一次，其余线程等待
        # 并复用结果，避免"惊群"打爆 115。合并键使用 cache_key（含 UA），与无 UA
        # 的 get_download_url 合并键（纯 pickcode）天然隔离，不会互相干扰。
        with _inflight_lock:
            inflight = _inflight_download.get(cache_key)
            if inflight:
                event = inflight["event"]
            else:
                event = threading.Event()
                _inflight_download[cache_key] = {"event": event, "url": None}
                inflight = None
        if inflight is not None:
            # 已有线程在请求相同 pickcode|ua，等待其完成后复用缓存
            event.wait(timeout=15)
            with _cache_lock:
                cached = _DOWNLOAD_URL_CACHE.get(cache_key)
                if cached and (_time.time() - cached["ts"]) < _DOWNLOAD_URL_TTL:
                    return cached["url"]
            # 等待超时或首个线程失败：降级为自行请求（不再合并）

        throttled = _apply_rate_limit("download_url", context)
        _start_ts = _time.time()
        try:
            client = cls.create_client_from_cookies(cookies)
            # 使用带 405 降级和限流重试的下载链接获取
            result = _download_url_with_retry(client, pickcode, ua)
            url = _extract_download_url(result)
            if url:
                with _cache_lock:
                    _DOWNLOAD_URL_CACHE[cache_key] = {
                        "url": url, "ts": _time.time(), "account_id": account_id,
                        "user_agent": ua,
                    }
                    # 清理过期条目
                    if len(_DOWNLOAD_URL_CACHE) > 5000:
                        cutoff = _time.time() - _DOWNLOAD_URL_TTL
                        expired = [k for k, v in _DOWNLOAD_URL_CACHE.items() if v["ts"] < cutoff]
                        for k in expired:
                            _DOWNLOAD_URL_CACHE.pop(k, None)
            # P0-2: 记录请求统计
            _request_stats.record("download_url", _time.time() - _start_ts, throttled)
            return url or None
        except Exception as e:
            # P0-2: 失败也记录统计
            _request_stats.record("download_url", _time.time() - _start_ts, throttled)
            logger.warning(f"[115] get_download_url_with_ua 失败 pickcode={pickcode}: {e}")
            return None
        finally:
            # O4: 唤醒等待线程（成功从缓存读取，失败则降级自行请求），并清理合并表
            with _inflight_lock:
                entry = _inflight_download.get(cache_key)
                if entry:
                    entry["event"].set()
                    _inflight_download.pop(cache_key, None)

    @classmethod
    def invalidate_download_url_cache(cls, pickcode: str = None):
        """清除直链缓存
        - pickcode 为 None 时清除全部
        - pickcode 指定时，同时清除 pickcode 和 pickcode|ua（不同 UA 的缓存变体）
        """
        with _cache_lock:
            if pickcode:
                # 清除无 UA 的缓存键
                _DOWNLOAD_URL_CACHE.pop(pickcode, None)
                # 清除所有 pickcode|ua 变体（不同客户端 UA）
                prefix = f"{pickcode}|"
                keys_to_remove = [k for k in _DOWNLOAD_URL_CACHE if k.startswith(prefix)]
                for k in keys_to_remove:
                    _DOWNLOAD_URL_CACHE.pop(k, None)
            else:
                _DOWNLOAD_URL_CACHE.clear()

    @classmethod
    def get_rate_limit_stats(cls) -> dict:
        """P0-2: 获取速率限制和请求统计

        整合现有的 _rate_limit_stats（QPS 级限流计数）和新的 _request_stats
        （QPS/QPM/QPH/延迟/限流/缓存命中率），供 /api/115/rate-stats 端点调用。
        """
        # 获取 QPS 级限流统计（现有计数器，读取后重置）
        qps_stats = get_rate_limit_stats()
        # 获取多维请求统计（新的 _RequestStats）
        request_stats = _request_stats.get_stats()
        # 直链缓存大小
        with _cache_lock:
            cache_size = len(_DOWNLOAD_URL_CACHE)
        # 合并返回
        return {
            "qps": request_stats.get("qps", 0),
            "qpm": request_stats.get("qpm", 0),
            "qph": request_stats.get("qph", 0),
            "avg_latency": request_stats.get("avg_latency", 0),
            "throttle_count": request_stats.get("throttle_count", 0),
            "cache_hit_rate": request_stats.get("cache_hit_rate", 0),
            "cache_size": cache_size,
            "total_requests": request_stats.get("total_requests", 0),
            # QPS 级限流统计（自上次读取以来的限流次数和总等待时间）
            "qps_throttle_count": qps_stats.get("count", 0),
            "qps_total_wait": round(qps_stats.get("total_wait", 0.0), 3),
            # N6: 响应式限流（全局冷却）状态
            "reactive_throttle": get_throttle_status(),
        }

    # ============ P0-3: 多端播放副本 ============

    @classmethod
    def get_download_url_multiplay(cls, cookies: str, pickcode: str, ua: str,
                                   account_id: int = 0, context: str = "") -> Optional[str]:
        """P0-3: 多端播放直链获取

        检测同一 pickcode 在最近 10 秒内是否被不同 UA 请求：
        - 非多端播放（仅 1 个 UA）：走正常 get_download_url_with_ua 流程
        - 多端播放（2+ 个不同 UA）：将文件复制到 /多端播放/ 目录，
          用副本 pickcode 获取独立直链，避免 115 风控判定多端播放异常

        副本 pickcode 缓存在 _multiplay_copy_map 中，后续多端请求复用副本，
        避免每次多端播放都触发复制操作。
        """
        if not pickcode or not ua:
            return None

        now = _time.time()
        is_multiplay = False

        # 多端播放检测
        with _multiplay_lock:
            tracker = _multiplay_tracker.get(pickcode)
            if tracker is None:
                # 首次请求该 pickcode
                _multiplay_tracker[pickcode] = {"uas": {ua}, "first_ts": now}
            else:
                # 已有追踪记录
                tracker["uas"].add(ua)
                # 超过检测窗口，重置追踪
                if now - tracker["first_ts"] > _MULTIPLAY_WINDOW:
                    _multiplay_tracker[pickcode] = {"uas": {ua}, "first_ts": now}
                elif len(tracker["uas"]) >= 2:
                    # 10 秒内 2+ 个不同 UA，判定为多端播放
                    is_multiplay = True

        if not is_multiplay:
            # 非多端播放，走正常流程
            return cls.get_download_url_with_ua(cookies, pickcode, ua, account_id, context)

        # 多端播放：检查是否已有副本 pickcode
        with _multiplay_lock:
            copy_pickcode = _multiplay_copy_map.get(pickcode)

        if copy_pickcode:
            # 已有副本，直接用副本 pickcode 获取直链
            logger.info(f"[115] 多端播放 pickcode={pickcode} 使用已有副本 pickcode={copy_pickcode}")
            url = cls.get_download_url_with_ua(cookies, copy_pickcode, ua, account_id, context)
            if url:
                return url
            # 副本直链获取失败，清除映射并重新复制
            with _multiplay_lock:
                _multiplay_copy_map.pop(pickcode, None)
            logger.warning(f"[115] 多端播放副本直链获取失败，尝试重新复制 pickcode={pickcode}")

        # 复制文件获取新 pickcode
        copy_pickcode = cls._create_multiplay_copy(cookies, pickcode, account_id)
        if copy_pickcode:
            with _multiplay_lock:
                _multiplay_copy_map[pickcode] = copy_pickcode
            url = cls.get_download_url_with_ua(cookies, copy_pickcode, ua, account_id, context)
            if url:
                return url

        # 复制失败或副本直链获取失败，降级为正常获取
        logger.warning(f"[115] 多端播放复制失败，降级为正常获取 pickcode={pickcode}")
        return cls.get_download_url_with_ua(cookies, pickcode, ua, account_id, context)

    @classmethod
    def _create_multiplay_copy(cls, cookies: str, pickcode: str, account_id: int = 0) -> Optional[str]:
        """P0-3: 复制文件到 /多端播放/ 目录，返回新 pickcode

        流程：
        1. 用 fs_document(pickcode) 获取文件 file_id 和 file_name
        2. 确保 /多端播放/ 目录存在（cid 缓存在 _multiplay_copy_cid）
        3. 用 fs_copy 复制文件到目标目录
        4. 从复制响应提取新 file_id，用 to_pickcode 转换为 pickcode
        5. 转换失败时列目录查找同名文件获取 pickcode
        """
        global _multiplay_copy_cid
        try:
            client = cls.create_client_from_cookies(cookies)

            # 1. 用 fs_document 获取文件信息（file_id, file_name）
            doc_resp = client.fs_document(pickcode)
            doc_data = (doc_resp or {}).get("data") or {}
            file_id = str(doc_data.get("file_id") or "")
            file_name = str(doc_data.get("file_name") or "")
            if not file_id:
                logger.warning(f"[115] 多端播放：无法获取 file_id pickcode={pickcode}")
                return None

            # 2. 确保 /多端播放/ 目录存在
            if not _multiplay_copy_cid:
                _multiplay_copy_cid = cls.mkdir(cookies, "多端播放", "0")
            dest_cid = _multiplay_copy_cid
            if not dest_cid:
                logger.warning("[115] 多端播放：创建副本目录 /多端播放/ 失败")
                return None

            # 3. 复制文件
            resp = _call_write_with_405_fallback(
                client, "fs_copy", "fs_copy_app",
                file_id, pid=dest_cid,
            )
            if isinstance(resp, dict) and resp.get("state") is False:
                logger.warning(f"[115] 多端播放：复制失败 {resp.get('error', '')}")
                return None

            # 4. 从复制响应中提取新 file_id
            new_file_id = ""
            data = resp.get("data") if isinstance(resp, dict) else None
            if isinstance(data, dict):
                # file_id 可能是逗号分隔的字符串
                raw_fid = str(data.get("file_id") or "")
                new_file_id = raw_fid.split(",")[0].strip() if raw_fid else ""
            elif isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict):
                    new_file_id = str(first.get("file_id") or "")

            # 5. 用 to_pickcode 将新 file_id 转换为 pickcode
            if new_file_id:
                try:
                    new_pickcode = client.to_pickcode(new_file_id, stable_point=pickcode)
                    logger.info(f"[115] 多端播放：已复制文件 file_id={file_id} -> "
                                f"新 pickcode={new_pickcode}")
                    return new_pickcode
                except Exception as e:
                    logger.warning(f"[115] 多端播放：to_pickcode 转换失败 file_id={new_file_id}: {e}")

            # 6. 转换失败时列目录查找同名文件的 pickcode
            if file_name and dest_cid:
                logger.info(f"[115] 多端播放：列目录查找副本 pickcode dest_cid={dest_cid}")
                resp_list = _fs_files_with_retry(client, {
                    "cid": dest_cid, "offset": 0, "limit": 1000, "show_dir": 1,
                })
                items = resp_list.get("data", []) or []
                for it in items:
                    if it.get("fid") and it.get("n") == file_name:
                        new_pickcode = str(it.get("pc", ""))
                        if new_pickcode:
                            logger.info(f"[115] 多端播放：列目录找到副本 pickcode={new_pickcode}")
                            return new_pickcode

            logger.warning(f"[115] 多端播放：无法获取副本 pickcode pickcode={pickcode}")
            return None
        except Exception as e:
            logger.warning(f"[115] 多端播放复制异常 pickcode={pickcode}: {e}")
            return None

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
                # 分页请求间隔，由用户配置（>=1s 时显示当前目录进度）
                _apply_file_list_interval(context=dir_rel_path or "根目录")

        _walk(cid, "")
        return videos

    @classmethod
    def list_all_files_full(cls, cookies: str, cid: str,
                            recursive: bool = True) -> list[dict]:
        """
        递归列出目录下的所有文件（不限扩展名），用于整理时识别配套文件。
        返回: [{"file_id", "pickcode", "name", "size", "parent_id", "parent_path"}]
        """
        client = cls.create_client_from_cookies(cookies)
        results: list[dict] = []

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
                            child_path = f"{dir_rel_path}/{name}" if dir_rel_path else name
                            _walk(child_cid, child_path)
                    else:
                        results.append({
                            "file_id": str(it.get("fid", "")),
                            "pickcode": it.get("pc", ""),
                            "name": name,
                            "size": it.get("s", 0) or 0,
                            "parent_id": str(dir_cid),
                            "parent_path": dir_rel_path,
                        })
                total = resp.get("count", 0)
                offset += len(items)
                if offset >= total:
                    break
                _apply_file_list_interval(context=dir_rel_path or "根目录")

        _walk(cid, "")
        return results

    @classmethod
    def mkdir(cls, cookies: str, name: str, parent_id: str = "0") -> Optional[str]:
        """
        新建目录，返回目录 ID。如已存在则返回已存在目录 ID。
        405 时自动降级到 fs_mkdir_app（注意参数格式差异：
        fs_mkdir 用 {"cname": name}，fs_mkdir_app 内部走 fs_folder_update_app 用 {"name": name}，
        降级时必须转换参数，否则 name 为空导致创建失败）。
        """
        _apply_rate_limit("mkdir")
        client = cls.create_client_from_cookies(cookies)
        # 参数转换：fs_mkdir（web）用 {"cname": name}，fs_mkdir_app 内部走
        # fs_folder_update_app 用 {"name": name}（p115client edit.makedir 官方实现
        # 是传 name 字符串 + pid 关键字，这里用等效 payload dict）。
        # 返回结构差异：web 直接返回 {"cid": ...}，proapi 返回 {"data": {"category_id": ...}}。
        def _adapt(payload: dict):
            if "cname" in payload and "name" not in payload:
                payload = dict(payload)
                payload["name"] = payload.pop("cname")
            return (payload,)
        try:
            resp = _call_write_with_405_fallback(
                client, "fs_mkdir", "fs_mkdir_app",
                {"cname": name, "pid": parent_id},
                arg_adapter=_adapt,
            )
            # 解析新目录 ID：兼容 web 直接返回 cid/file_id，以及 proapi 的 data.category_id
            cid = (
                resp.get("cid") or resp.get("file_id") or resp.get("category_id")
                or (isinstance(resp.get("data"), dict) and (
                    resp["data"].get("category_id") or resp["data"].get("file_id")
                    or resp["data"].get("cid")
                ))
            )
            if cid:
                # 目录创建成功，清除失败黑名单缓存
                cache_key = (str(parent_id), name)
                with _path_not_found_lock:
                    _path_not_found_cache.pop(cache_key, None)
                # Q4: 标记父目录进入写后冷却（新目录可能尚未在服务端索引中可见）
                _mark_dir_written(parent_id)
                return str(cid)
            # 目录已存在：115 返回 errno=20004，需要查找已有目录
            found = cls._find_subdir(client, name, parent_id)
            if found:
                # 目录找到，清除失败黑名单缓存
                cache_key = (str(parent_id), name)
                with _path_not_found_lock:
                    _path_not_found_cache.pop(cache_key, None)
            return found
        except Exception as e:
            # 尝试查找已存在目录
            found = cls._find_subdir(client, name, parent_id)
            if found:
                # 目录找到，清除失败黑名单缓存
                cache_key = (str(parent_id), name)
                with _path_not_found_lock:
                    _path_not_found_cache.pop(cache_key, None)
                return found
            logger.warning(f"[115] mkdir 失败 {name}: {e}")
            return None

    @classmethod
    def _find_subdir(cls, client, name: str, parent_id: str) -> Optional[str]:
        """在父目录下查找同名子目录，返回其 cid（使用 405 降级重试）
        
        集成失败黑名单缓存：查询返回 None 时缓存 (parent_id, name)，
        后续相同查询在 TTL 内直接短路返回 None，避免重复 API 调用。
        """
        # 失败黑名单短路：TTL 内的"查无"记录直接返回 None
        # Q4: 写后冷却期内跳过黑名单（避免刚写入的目录被 stale 的"查无"记录短路），直接走 API
        cache_key = (str(parent_id), name)
        if not _dir_in_cooldown(parent_id):
            with _path_not_found_lock:
                cached_ts = _path_not_found_cache.get(cache_key)
                if cached_ts is not None:
                    if _time.time() - cached_ts < _PATH_NOT_FOUND_TTL:
                        return None
                    else:
                        # 过期，移除旧记录
                        del _path_not_found_cache[cache_key]

        try:
            offset = 0
            while True:
                resp = _fs_files_with_retry(client, {
                    "cid": parent_id, "offset": offset, "limit": 1000, "show_dir": 1,
                })
                items = resp.get("data", []) or []
                if not items:
                    break
                for it in items:
                    if not it.get("fid") and it.get("n") == name:
                        # Q4: 目录找到成功，解除该目录的写后冷却
                        _mark_dir_clear(parent_id)
                        return str(it.get("cid"))
                total = resp.get("count", 0)
                offset += len(items)
                if offset >= total:
                    break
                # 分页请求间隔（跟随用户配置，与文件列表一致）
                _apply_file_list_interval(context=f"查找目录 {name}")
        except Exception:
            pass

        # 查无此目录，写入失败黑名单缓存
        with _path_not_found_lock:
            _path_not_found_cache[cache_key] = _time.time()
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
    def rename(cls, cookies: str, file_id: str, new_name: str, context: str = "") -> bool:
        """重命名文件或目录

        注意：fs_rename 接受单个元组 (file_id, new_name) 或 dict，
        不能传列表。返回 dict 含 state 字段，需检查。
        405 时自动降级到 fs_rename_app。
        context: 可选的操作对象（如文件名），用于等待日志展示当前进度
        """
        _apply_rate_limit("rename", context)
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = _call_write_with_405_fallback(
                client, "fs_rename", "fs_rename_app",
                (file_id, new_name),
            )
            if isinstance(resp, dict) and resp.get("state") is False:
                logger.warning(f"[115] rename 失败 {file_id} -> {new_name}: {resp.get('error', '')}")
                return False
            # Q4: 重命名成功，解析源父目录并标记写后冷却（best-effort）
            for pid in _resolve_parent_ids(client, [file_id]):
                _mark_dir_written(pid)
            return True
        except Exception as e:
            logger.warning(f"[115] rename 失败 {file_id} -> {new_name}: {e}")
            return False

    @classmethod
    def move(cls, cookies: str, file_ids: list[str], dest_id: str, context: str = "") -> bool:
        """移动文件到目标目录

        注意：fs_move 的第一个参数是位置参数 payload，
        传 list 时 p115client 会自动转为 fid[0]、fid[1] 格式。
        不能传 {"fid": [...]} 因为 API 不接受 fid 为列表。
        返回 dict 含 state 字段，需检查。
        遇到"操作尚未执行完成"时自动等待重试。
        405 时自动降级到 fs_move_app。
        context: 可选的操作对象（如文件名），用于等待日志展示当前进度
        """
        _check_circuit_breaker()  # Q2: 熔断器检查

        def _do_move(cli, fids, pid):
            """带 405 降级的 fs_move 调用"""
            try:
                return cli.fs_move(fids, pid=pid)
            except Exception as e:
                if _is_method_not_allowed(e):
                    logger.info("[115] fs_move 返回 405，降级到 fs_move_app")
                    _mark_endpoint_cooldown("fs_move", duration=300)
                    return cli.fs_move_app(fids, pid=pid)
                raise

        _apply_rate_limit("move", context)
        client = cls.create_client_from_cookies(cookies)
        # 异步操作等待基础值（跟随用户配置的直链间隔，避免低于配置）
        base_wait = max(get_api_intervals().get("download_url_interval", 3.0), 1.0)
        max_retries = 5
        for attempt in range(max_retries):
            try:
                resp = _do_move(client, file_ids, dest_id)
                if isinstance(resp, dict) and resp.get("state") is False:
                    err_msg = resp.get("error", "")
                    # 115 移动操作是异步的，连续操作可能返回"操作尚未执行完成"
                    if "尚未执行完成" in err_msg and attempt < max_retries - 1:
                        wait = max(base_wait * (attempt + 1), 2 * (attempt + 1))
                        logger.warning(f"[115] move 等待重试 ({attempt+1}/{max_retries}), {wait}s 后重试: {err_msg}")
                        _time.sleep(wait)
                        continue
                    logger.warning(f"[115] move 失败 -> {dest_id}: {err_msg}")
                    return False
                # Q4: 移动成功，目标目录内容已变化，标记写后冷却
                _mark_dir_written(dest_id)
                return True
            except Exception as e:
                if "尚未执行完成" in str(e) and attempt < max_retries - 1:
                    wait = 2 * (attempt + 1)
                    logger.warning(f"[115] move 等待重试 ({attempt+1}/{max_retries}), {wait}s 后重试: {e}")
                    _time.sleep(wait)
                    continue
                logger.warning(f"[115] move 失败 -> {dest_id}: {e}")
                return False
        return False

    @classmethod
    def copy(cls, cookies: str, file_ids: list[str], dest_id: str) -> bool:
        """复制文件到目标目录

        注意：fs_copy 的调用方式与 fs_move 相同。
        405 时自动降级到 fs_copy_app。
        """
        _apply_rate_limit("copy")
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = _call_write_with_405_fallback(
                client, "fs_copy", "fs_copy_app",
                file_ids, pid=dest_id,
            )
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

        支持秒传优化：上传前计算 SHA1，传给 115 服务端检查是否已有相同哈希的文件，
        若已存在则跳过实际上传，节省带宽和时间。
        """
        import tempfile
        import os
        client = cls.create_client_from_cookies(cookies)
        tmp_path = None
        try:
            # 计算文件的 SHA1 哈希，用于秒传判断
            sha1 = hashlib.sha1(data).hexdigest()
            # 写入临时文件再上传
            fd, tmp_path = tempfile.mkstemp(suffix=f"_{filename}")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # 使用 upload_file_sample 代替 upload_file
            # upload_file 的 initupload.php 端点返回 405，upload_file_sample 更稳定
            # 传入 sha1 参数，让 115 服务端检查是否已有相同哈希的文件（秒传）
            resp = client.upload_file_sample(
                tmp_path,
                pid=dest_id,
                filename=filename,
                sha1=sha1,
            )
            if isinstance(resp, dict):
                # 检查是否秒传成功（文件已存在，跳过实际上传）
                if resp.get("bak_num") or resp.get("already_exists"):
                    logger.info(f"[115] 秒传成功: {filename}")
                    return True
                if resp.get("state") is False:
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
            # Q4: 上传成功，目标目录内容已变化，标记写后冷却
            _mark_dir_written(dest_id)
            return True
        except Exception as e:
            cause = e.__cause__
            if cause:
                logger.warning(f"[115] upload_file 异常 {filename}: {type(cause).__name__}: {str(cause)[:200]}")
            else:
                logger.warning(f"[115] upload_file 异常 {filename}: {type(e).__name__}: {str(e)[:200]}")
            return False

    @classmethod
    def _sha1_range_of_url(cls, url: str, start: int, length: int) -> str:
        """N3: 计算远端 URL 指定字节范围的 SHA1（大写），用于 115 秒传 sign_check。

        通过 HTTP Range 请求只读取需要的片段，不下载完整文件。
        参考 MoviePilot p115strmhelper ali2115 calculate_sha1_range。
        """
        import httpx
        end = start + length - 1
        headers = {"Range": f"bytes={start}-{end}"}
        with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=60.0) as r:
            r.raise_for_status()
            h = hashlib.sha1()
            for chunk in r.iter_bytes(chunk_size=8192):
                h.update(chunk)
            return h.hexdigest().upper()

    @classmethod
    def instant_upload_from_url(cls, cookies: str, download_url: str, filename: str,
                                file_size: int, full_sha1: str, dest_id: str = "0") -> dict:
        """N3: 跨云秒传——用远端直链的 SHA1 尝试将文件秒传进 115（不下载字节）。

        适用于任意可 Range 访问的云盘直链（阿里云盘分享直链等）。若 115 已存在相同
        SHA1+size 的文件则秒传成功；否则 115 会通过 sign_check 要求对文件某个字节范围
        二次 SHA1 校验，此时用 _sha1_range_of_url 对远端直链算该范围 SHA1 应答。

        参数:
            download_url: 源文件的可 Range 访问直链
            filename: 目标文件名
            file_size: 文件大小（字节）
            full_sha1: 整文件 SHA1（大写十六进制，需调用方预先获得，如源云盘 API 提供）
            dest_id: 115 目标目录 cid
        返回:
            {"status": "instant"|"need_upload"|"error", "message": str, "resp": <115 原始响应>}
            status=instant 表示秒传成功；need_upload 表示 115 无此文件需实际上传（本方法不做完整上传）。
        """
        if not (download_url and filename and full_sha1 and file_size > 0):
            return {"status": "error", "message": "参数不完整（需 url/filename/size/sha1）"}

        client = cls.create_client_from_cookies(cookies)

        def _sign_check_cb(sign_check: str):
            # sign_check 形如 "起始-结束"，对该范围算 SHA1 应答
            start_str, end_str = sign_check.split("-")
            start, end = int(start_str), int(end_str)
            return cls._sha1_range_of_url(download_url, start, end - start + 1)

        _apply_rate_limit("upload")
        try:
            resp = client.upload_file_init(
                filename=filename,
                filesize=int(file_size),
                filesha1=full_sha1.upper(),
                pid=dest_id,
                read_range_bytes_or_hash=_sign_check_cb,
            )
        except Exception as e:
            logger.warning(f"[115] 跨云秒传异常 {filename}: {e}")
            return {"status": "error", "message": str(e)}

        if not isinstance(resp, dict):
            return {"status": "error", "message": "115 返回格式异常", "resp": resp}
        # status==2 表示秒传成功（文件已存在）；status==1 表示需要实际上传
        status = resp.get("status") or (resp.get("data") or {}).get("status")
        if status == 2 or resp.get("bak_num") or resp.get("already_exists"):
            logger.info(f"[115] 跨云秒传成功: {filename}")
            _mark_dir_written(dest_id)
            return {"status": "instant", "message": "秒传成功", "resp": resp}
        return {"status": "need_upload", "message": "115 无此文件，需实际上传（未执行）", "resp": resp}

    # ============ STRM 同步专用方法 ============

    # ===== S2: 115 导出目录树（export_dir 快速扫描） =====
    # 参考 MoviePilot p115strmhelper 的 increment.py 设计思想：
    # 用 115 服务端"导出目录树"一次性导出整个目录的路径清单（比逐目录递归 fs_files
    # 少一次全量遍历），再用 fs_files 为树中的文件补全元数据（file_id/pickcode/size/sha1）。
    # 注意：115 同一时间只允许运行一个导出目录树任务，需加锁串行。
    _export_dir_lock = threading.Lock()
    _EXPORT_DIR_TIMEOUT = 600  # 导出任务等待超时（秒）

    @classmethod
    def export_dir_tree(cls, cookies: str, cid: str, timeout: float = 600.0,
                        expected_root: str = "") -> dict:
        """使用 115 导出目录树功能快速获取目录结构（含元数据补全）。

        返回: {
            "files": [{"name", "path", "size", "pickcode", "file_id", "sha1", "parent_path"}],
            "dirs":  [{"name", "path"}],
        }
        - path 为相对同步根目录（cid）的完整路径，如 "电影/2025/某片/xxx.mkv"
        - 115 导出树仅含路径名，size/pickcode 等元数据由 _fill_tree_meta 逐目录补全
        - 返回 {} 表示不支持 / 导出失败（调用方回退递归扫描）
        - expected_root: 期望的同步根目录名（配置的 source_path 最后一段）。
          当导出树的根高于实际源目录（如导出的是"影视测试"而源是"影视测试/俱乐部"）
          时，剥离第一层后路径仍带多余层级，此时用 expected_root 做二次剥离。
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            from p115client.tool.export_dir import (
                export_dir_start, export_dir_result, export_dir_parse_iter,
            )
        except ImportError:
            logger.warning("[115] 当前 p115client 不支持导出目录树，降级为递归扫描")
            return {}
        try:
            with cls._export_dir_lock:
                # 1. 提交导出目录树任务（layer_limit=0 表示不限深度）
                export_id = export_dir_start(client, file_ids=str(cid), layer_limit=0)
                export_file_id = ""
                try:
                    # 2. 轮询等待任务完成（超时抛 TimeoutError，不取消远程任务）
                    result = export_dir_result(client, export_id, timeout=timeout, check_interval=2)
                    export_file_id = str((result or {}).get("file_id", ""))
                    # 3. 下载并解析目录树（按路径解析，根路径为第一项）
                    # 注意：这里用 delete=False，由下方 finally 统一兜底删除，
                    # 避免解析中途异常时 115 端临时文件残留（名为"xxx_目录树.txt"）
                    paths = list(export_dir_parse_iter(
                        client, export_id, parse_iter="path", delete=False,
                    ))
                finally:
                    # 4. 兜底清理：无论成功/失败/超时，都尝试删除 115 端的导出临时文件
                    if not export_file_id:
                        try:
                            result = export_dir_result(client, export_id, timeout=15, check_interval=1)
                            export_file_id = str((result or {}).get("file_id", ""))
                        except Exception:
                            pass
                    if export_file_id:
                        try:
                            client.fs_delete(export_file_id)
                            logger.info(f"[115] 已清理导出目录树临时文件 file_id={export_file_id}")
                        except Exception as e:
                            logger.warning(f"[115] 清理导出目录树临时文件失败 file_id={export_file_id}: {e}")
        except Exception as e:
            logger.warning(f"[115] export_dir 树导出失败 cid={cid}: {e}")
            return {}
        if not paths:
            return {}

        # 4. 剥离导出根路径前缀，得到相对路径清单；用前缀关系区分目录与文件
        root = paths[0]
        rel_paths = []
        for p in paths[1:]:
            if p == root:
                continue
            if p.startswith(root + "/"):
                rel_paths.append(p[len(root) + 1:])
            else:
                rel_paths.append(p)

        # 4.1 二次剥离：当导出树的根高于实际源目录时（例如导出的是"影视测试"，
        #     而同步源配置为"影视测试/俱乐部"），剥离第一层后路径仍带多余层级。
        #     用配置的 source_path 最后一段（expected_root）做锚点：
        #     若所有路径的第一层目录恰好等于 expected_root，说明这一层是多余层级，剥掉。
        if expected_root and rel_paths:
            first_parts = {p.split("/", 1)[0] for p in rel_paths if p}
            if first_parts == {expected_root}:
                rel_paths = [
                    p[len(expected_root) + 1:] if p.startswith(expected_root + "/") else p
                    for p in rel_paths
                ]
                logger.info(f"[115] export_dir 树存在多余层级 '{expected_root}'，已二次剥离")
        dir_set: set[str] = set()
        for p in rel_paths:
            parts = p.split("/")
            for i in range(1, len(parts)):
                dir_set.add("/".join(parts[:i]))
        dir_paths: list[dict] = [{"name": p.rsplit("/", 1)[-1], "path": p}
                                 for p in sorted(dir_set)]
        file_paths: list[dict] = []
        for p in rel_paths:
            if p in dir_set:
                continue
            file_paths.append({
                "name": p.rsplit("/", 1)[-1],
                "path": p,
                "size": 0,
                "pickcode": "",
                "file_id": "",
                "sha1": "",
                "parent_path": p.rsplit("/", 1)[0] if "/" in p else "",
            })

        # 5. 元数据补全：按树中目录路径逐目录 fs_files，为文件补全 fid/pickcode/size/sha1
        if not cls._fill_tree_meta(client, str(cid), dir_paths, file_paths):
            logger.warning("[115] export_dir 树元数据补全失败，回退递归扫描")
            return {}

        return {"files": file_paths, "dirs": dir_paths}

    @classmethod
    def _fill_tree_meta(cls, client, root_cid: str, dir_paths: list[dict],
                        file_paths: list[dict]) -> bool:
        """为 export_dir 树中的文件补全元数据（file_id/pickcode/size/sha1）。

        按树中目录路径逐目录调用 fs_files（分页），把目录内条目按 "相对路径" 建立
        索引后回填到 files 列表。任一层级失败即返回 False（调用方回退递归扫描）。
        注意：115 导出树不导出空目录，故树中目录必然存在；对树外目录不继续下钻。
        """
        if not file_paths:
            return True
        dir_path_set = {d["path"] for d in dir_paths}
        # 相对路径 -> cid 映射（"" 为同步根）
        cid_of: dict[str, str] = {"": str(root_cid)}
        # 相对路径 -> 元数据
        meta: dict[str, dict] = {}
        pending = [""]
        while pending:
            p = pending.pop()
            cur_cid = cid_of.get(p)
            if cur_cid is None:
                continue
            offset = 0
            while True:
                try:
                    resp = _fs_files_with_retry(client, {
                        "cid": cur_cid, "offset": offset, "limit": 1150, "show_dir": 1,
                    })
                except Exception as e:
                    logger.warning(f"[115] export_dir 元数据补全失败 cid={cur_cid}: {e}")
                    return False
                items = resp.get("data", []) or []
                if not items:
                    break
                for it in items:
                    name = it.get("n", "")
                    if not name:
                        continue
                    key = f"{p}/{name}" if p else name
                    if not it.get("fid"):
                        # 子目录：仅当在树中时才继续下钻（避免扫描树外目录）
                        if key in dir_path_set:
                            cid_of[key] = str(it.get("cid", ""))
                            pending.append(key)
                    else:
                        meta[key] = {
                            "file_id": str(it.get("fid", "")),
                            "pickcode": it.get("pc", ""),
                            "size": it.get("s", 0) or 0,
                            "sha1": it.get("sha", ""),
                        }
                total = resp.get("count", 0)
                offset += len(items)
                if offset >= total:
                    break
                _apply_file_list_interval(context=p or "根目录")
        # 回填元数据（树中已导出但 fs_files 未命中的文件保留空元数据，极罕见）
        for f in file_paths:
            m = meta.get(f["path"])
            if m:
                f.update(m)
        return True

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
        _check_circuit_breaker()  # Q2: 熔断器检查
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
                # 分页请求间隔，由用户配置（>=1s 时显示当前目录进度）
                _apply_file_list_interval(context=dir_rel_path or "根目录")
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
    def download_file(cls, cookies: str, pickcode: str, local_path: str, context: str = "") -> bool:
        """
        下载 115 文件到本地（用于元数据/字幕文件下载）
        遇到 403 时自动清除缓存并重试（链接可能已过期或 CDN 临时拒绝）
        context: 可选的操作对象（如文件名），用于等待日志展示当前进度
        """
        if not pickcode:
            return False
        import os
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)

        # 重试等待时间：跟随用户配置的直链间隔（默认 0.3s），至少 1 秒
        retry_wait = max(get_api_intervals().get("download_url_interval", 3.0) * 5, 1.0)
        last_error = None
        for attempt in range(3):
            try:
                # 重试时清除缓存，强制获取新链接
                if attempt > 0:
                    cls.invalidate_download_url_cache(pickcode)
                    _time.sleep(retry_wait)

                dl_info = cls.get_download_url_with_headers(cookies, pickcode, context=context)
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
                    _time.sleep(retry_wait)

        logger.warning(f"[115] download_file 失败 {local_path}: {last_error}")
        return False

    @classmethod
    def download_files_batch(cls, cookies: str, tasks: list[dict]) -> list[dict]:
        """批量下载文件（用于字幕、图片、NFO 等元数据文件并发下载）

        策略（参考 MoviePilot 批量字幕下载优化）：
        1. 先串行获取所有下载链接（受速率限制，避免并发触发 115 风控）
        2. 再并发下载文件内容（CDN 下载不受 API 速率限制）
        3. 下载失败的文件自动重试（最多 2 次）

        tasks: [{"pickcode": str, "local_path": str, "context": str}, ...]
        返回: [{"pickcode": str, "local_path": str, "success": bool, "error": str}, ...]
        """
        if not tasks:
            return []

        import os
        results = []
        # 第一阶段：串行获取下载链接（含速率限制）
        dl_infos = []  # [(task, url, ua), ...]
        for task in tasks:
            pickcode = task.get("pickcode", "")
            local_path = task.get("local_path", "")
            context = task.get("context", "")
            if not pickcode:
                results.append({"pickcode": pickcode, "local_path": local_path,
                                "success": False, "error": "无 pickcode"})
                continue
            dl_info = cls.get_download_url_with_headers(cookies, pickcode, context=context)
            if not dl_info or not dl_info.get("url"):
                results.append({"pickcode": pickcode, "local_path": local_path,
                                "success": False, "error": "获取下载链接失败"})
                continue
            dl_infos.append((task, dl_info["url"], dl_info.get("user_agent") or cls.DOWNLOAD_USER_AGENT))

        # 第二阶段：并发下载文件内容
        def _download_one(task_info):
            task, url, ua = task_info
            local_path = task["local_path"]
            context = task.get("context", "")
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            headers = {
                "User-Agent": ua,
                "Referer": "https://115.com/",
                "Accept": "*/*",
            }
            last_error = None
            for attempt in range(2):
                try:
                    with httpx.Client(timeout=60.0, follow_redirects=True, headers=headers) as c:
                        with c.stream("GET", url) as r:
                            if r.status_code == 403:
                                # 链接过期，重新获取
                                pickcode = task["pickcode"]
                                cls.invalidate_download_url_cache(pickcode)
                                new_info = cls.get_download_url_with_headers(
                                    cookies, pickcode, context=context)
                                if new_info and new_info.get("url"):
                                    url = new_info["url"]
                                    headers["User-Agent"] = new_info.get("user_agent") or ua
                                last_error = "403 Forbidden"
                                continue
                            r.raise_for_status()
                            with open(local_path, "wb") as f:
                                for chunk in r.iter_bytes(chunk_size=65536):
                                    f.write(chunk)
                    return {"pickcode": task["pickcode"], "local_path": local_path,
                            "success": True, "error": ""}
                except Exception as e:
                    last_error = str(e)
                    if attempt < 1:
                        _time.sleep(1)
            return {"pickcode": task["pickcode"], "local_path": local_path,
                    "success": False, "error": last_error}

        # 使用线程池并发下载（最多 4 个并发）
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_download_one, info): info for info in dl_infos}
            for future in futures:
                try:
                    results.append(future.result(timeout=120))
                except Exception as e:
                    info = futures[future]
                    results.append({"pickcode": info[0]["pickcode"],
                                    "local_path": info[0]["local_path"],
                                    "success": False, "error": str(e)})

        _ok = sum(1 for r in results if r["success"])
        _fail = len(results) - _ok
        if _fail > 0:
            logger.info(f"[115] 批量下载完成: 成功 {_ok}, 失败 {_fail}")
        return results

    # ============ 特色工具：删除/回收站操作 ============

    @classmethod
    def delete_files(cls, cookies: str, file_ids: list[str]) -> dict:
        """删除文件或目录（移入回收站），返回 115 API 响应
        405 时自动降级到 fs_delete_app。
        """
        _check_circuit_breaker()  # Q2: 熔断器检查
        _apply_rate_limit("delete")
        client = cls.create_client_from_cookies(cookies)
        # Q4: 删除会改变源父目录内容。fs_file 对已入回收站的文件不可用，
        # 故在删除前解析父目录 id（best-effort，最多前 10 个，避免大批次额外请求）
        deleted_parents = _resolve_parent_ids(client, file_ids)
        try:
            resp = _call_write_with_405_fallback(
                client, "fs_delete", "fs_delete_app",
                file_ids,
            )
            # Q4: 删除成功，标记源父目录进入写后冷却
            if not (isinstance(resp, dict) and resp.get("error")):
                for pid in deleted_parents:
                    _mark_dir_written(pid)
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
                _apply_file_list_interval(context="遍历目录")

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

    @classmethod
    def recycle_bin_info(cls, cookies: str) -> dict:
        """N4: 获取回收站占用信息（文件数/占用空间）。

        返回 {"count": 文件数, "size": 字节数, "size_str": 可读大小}；失败返回 {"error": str}。
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.recyclebin_info()
        except Exception as e:
            logger.warning(f"[115] recyclebin_info 失败: {e}")
            return {"error": str(e)}
        data = resp.get("data", resp) if isinstance(resp, dict) else {}
        # 115 rb 信息字段：rb_count / total_count / size 等，做兼容取值
        count = data.get("count") or data.get("rb_count") or data.get("total_count") or 0
        size = data.get("size") or data.get("total_size") or 0
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        return {"count": count, "size": size, "size_str": _human_size(size)}

    # ===== 离线下载（转存下载） =====

    @classmethod
    def clouddownload_add_urls(cls, cookies: str, urls: list[str], wp_path_id: str = "") -> dict:
        """
        添加离线下载任务（支持 HTTP/HTTPS/FTP/磁力链/电驴链接）
        urls: 链接列表
        wp_path_id: 保存到的目录 cid（留空=根目录）
        使用 ssp 端点 (clouddownload.115.com) 逐个添加，避免 proapi 端点 405/错误问题
        返回 results 中包含 info_hash 供后续下载状态轮询使用
        """
        client = cls.create_client_from_cookies(cookies)
        clean_urls = [u.strip() for u in urls if u.strip()]
        if not clean_urls:
            return {"error": "无有效链接"}

        logger.info(f"[115] 添加离线下载任务: {len(clean_urls)} 个链接, 保存目录 cid={wp_path_id or '根目录'}")
        results = []
        has_error = False
        error_msg = ""
        info_hashes = []
        for url in clean_urls:
            payload = {"url": url}
            if wp_path_id:
                payload["wp_path_id"] = wp_path_id
            try:
                resp = client.clouddownload_task_add_url(payload)
                if isinstance(resp, dict):
                    data = resp.get("data", resp)
                    if isinstance(data, dict) and data.get("state") is False:
                        errcode = data.get("errcode", 0)
                        msg = data.get("error_msg", "添加失败")
                        # 10008=任务已存在，视为成功（警告而非错误）
                        if errcode == 10008:
                            info_hash = data.get("info_hash", "")
                            logger.info(f"[115] 离线下载任务已存在: {url[:80]} (hash={info_hash})")
                            results.append({"url": url, "state": True, "message": "任务已存在", "info_hash": info_hash})
                            if info_hash:
                                info_hashes.append(info_hash)
                        else:
                            logger.warning(f"[115] 离线下载添加失败: {msg} (errcode={errcode})")
                            has_error = True
                            error_msg = msg
                            results.append({"url": url, "state": False, "error": msg})
                    else:
                        info_hash = data.get("info_hash", "") if isinstance(data, dict) else ""
                        logger.info(f"[115] 离线下载任务添加成功: {url[:80]} (hash={info_hash})")
                        results.append({"url": url, "state": True, "info_hash": info_hash})
                        if info_hash:
                            info_hashes.append(info_hash)
                else:
                    results.append({"url": url, "state": True})
            except Exception as e:
                logger.warning(f"[115] 离线下载添加异常: {e}")
                has_error = True
                error_msg = str(e)
                results.append({"url": url, "state": False, "error": str(e)})

        success_count = sum(1 for r in results if r.get("state"))
        if has_error and success_count == 0:
            return {"error": error_msg, "state": False}
        return {"state": True, "results": results, "success_count": success_count, "total": len(clean_urls), "info_hashes": info_hashes}

    # ===== #27: 视频多清晰度（虚拟多版本源） =====
    # 清晰度编码映射（115 definition → 展示名），参考 qmediasync definition_list_new
    _DEFINITION_NAMES = {
        1: "标清", 2: "高清", 3: "超清", 4: "1080P", 5: "4K", 100: "原画",
    }
    # 视频清晰度列表缓存: pickcode -> {"ts": float, "defs": [...]}，TTL 30 分钟
    _video_def_cache: dict = {}
    _video_def_lock = threading.Lock()
    _VIDEO_DEF_TTL = 1800

    @classmethod
    def get_video_definitions(cls, cookies: str, pickcode: str) -> list[dict]:
        """#27: 获取视频的多清晰度播放地址列表（用于注入 Emby 多版本源）。

        调用 115 fs_video（webapi.115.com/files/video），返回各清晰度的 m3u8 直链。
        若视频从未转码，115 会自动推送转码（此时可能只返回部分清晰度）。
        结果按 pickcode 缓存 30 分钟。

        返回: [{"definition": int, "name": str, "url": str, "width": int, "height": int}]
        失败或无转码返回空列表。
        """
        if not pickcode:
            return []
        with cls._video_def_lock:
            cached = cls._video_def_cache.get(pickcode)
            if cached and (_time.time() - cached["ts"]) < cls._VIDEO_DEF_TTL:
                return cached["defs"]
        _apply_rate_limit("video")
        try:
            client = cls.create_client_from_cookies(cookies)
            resp = client.fs_video({"pickcode": pickcode})
        except Exception as e:
            logger.warning(f"[115] 获取视频清晰度失败 pickcode={pickcode}: {e}")
            return []
        if not isinstance(resp, dict):
            return []
        data = resp.get("data", resp) if isinstance(resp.get("data"), dict) else resp
        video_url = data.get("video_url") or []
        defs = []
        for v in video_url:
            if not isinstance(v, dict):
                continue
            url = v.get("url", "")
            if not url:
                continue
            d = v.get("definition", 0)
            defs.append({
                "definition": d,
                "name": v.get("title") or cls._DEFINITION_NAMES.get(d, f"清晰度{d}"),
                "url": url,
                "width": v.get("width", 0) or 0,
                "height": v.get("height", 0) or 0,
            })
        # 按清晰度从高到低排序（原画 100 最高）
        defs.sort(key=lambda x: x["definition"], reverse=True)
        with cls._video_def_lock:
            cls._video_def_cache[pickcode] = {"ts": _time.time(), "defs": defs}
            # 清理过期
            if len(cls._video_def_cache) > 200:
                cutoff = _time.time() - cls._VIDEO_DEF_TTL
                for k in [k for k, val in cls._video_def_cache.items() if val["ts"] < cutoff]:
                    cls._video_def_cache.pop(k, None)
        return defs

    @classmethod
    def clouddownload_torrent_info(cls, cookies: str, torrent_url: str = "", torrent_sha1: str = "") -> dict:
        """N1: 解析磁力/种子的文件列表（供用户勾选后只下选中项）。

        参考 LitePan 的 PrepareTorrent 流程：先解析出文件清单，用户选择后再提交。
        torrent_url: 磁力链接（magnet:）或种子下载 url
        torrent_sha1: 已上传种子的 sha1（二选一）
        返回 {"info_hash", "torrent_name", "file_count", "files": [{"index","name","size"}]}；
        失败返回 {"error": str}。
        """
        client = cls.create_client_from_cookies(cookies)
        payload: dict = {}
        if torrent_sha1:
            payload["sha1"] = torrent_sha1
        elif torrent_url:
            payload["url"] = torrent_url
        else:
            return {"error": "缺少磁力链接或种子"}
        try:
            resp = client.clouddownload_torrent(payload)
        except Exception as e:
            logger.warning(f"[115] 解析种子文件列表失败: {e}")
            return {"error": str(e)}
        if not isinstance(resp, dict):
            return {"error": "解析返回格式异常"}
        # 115 返回 {state, info_hash, torrent_name, file_count, torrent_filelist_web:[{wanted,size,path}...]}
        data = resp.get("data", resp)
        if isinstance(data, dict) and data.get("state") is False:
            return {"error": data.get("error_msg") or "解析失败"}
        raw_files = data.get("torrent_filelist_web") or data.get("files") or []
        files = []
        for idx, it in enumerate(raw_files):
            if not isinstance(it, dict):
                continue
            files.append({
                "index": idx,
                "name": str(it.get("path") or it.get("name") or ""),
                "size": it.get("size") or 0,
                "wanted": it.get("wanted", 1),
            })
        return {
            "info_hash": data.get("info_hash", ""),
            "torrent_name": data.get("torrent_name", ""),
            "file_count": data.get("file_count", len(files)),
            "files": files,
        }

    @classmethod
    def clouddownload_add_bt(cls, cookies: str, info_hash: str, wanted_indexes: list[int],
                             wp_path_id: str = "", torrent_sha1: str = "") -> dict:
        """N1: 提交 BT 任务，仅下载 wanted_indexes 指定的文件。

        info_hash: clouddownload_torrent_info 返回的 info_hash
        wanted_indexes: 用户勾选的文件序号列表；空列表表示全部
        wp_path_id: 保存目录 cid
        torrent_sha1: 种子 sha1（部分场景需要）
        返回 115 原始响应；失败返回 {"error": str}。
        """
        client = cls.create_client_from_cookies(cookies)
        payload: dict = {"info_hash": info_hash}
        if wanted_indexes:
            # 115 用逗号分隔的 wanted 序号字符串
            payload["wanted"] = ",".join(str(i) for i in wanted_indexes)
        if wp_path_id:
            payload["wp_path_id"] = wp_path_id
        if torrent_sha1:
            payload["sha1"] = torrent_sha1
        try:
            resp = client.clouddownload_task_add_bt(payload)
        except Exception as e:
            logger.warning(f"[115] 添加 BT 任务失败: {e}")
            return {"error": str(e)}
        if isinstance(resp, dict):
            data = resp.get("data", resp)
            if isinstance(data, dict) and data.get("state") is False:
                return {"error": data.get("error_msg") or "添加失败"}
            return {"state": True, "info_hash": data.get("info_hash", info_hash) if isinstance(data, dict) else info_hash}
        return {"state": True, "info_hash": info_hash}

    @classmethod
    def clouddownload_quota(cls, cookies: str) -> dict:
        """N1: 获取离线下载配额信息（剩余额度/总额度）。

        返回 115 原始配额结构；失败返回 {"error": str}。
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.clouddownload_quota_info()
        except Exception as e:
            logger.warning(f"[115] 获取离线配额失败: {e}")
            return {"error": str(e)}
        if isinstance(resp, dict):
            return resp.get("data", resp)
        return {"error": "配额返回格式异常"}

    @classmethod
    def clouddownload_check_status(cls, cookies: str, info_hashes: list[str]) -> dict:
        """
        检查离线下载任务的完成状态
        info_hashes: 要检查的 info_hash 列表
        返回 {info_hash: {"completed": bool, "percent": int, "status": int, "status_text": str}}
        """
        if not info_hashes:
            return {"tasks": {}}
        client = cls.create_client_from_cookies(cookies)
        hash_set = set(info_hashes)
        result = {}
        page = 1
        # 逐页扫描，直到找到所有 hash 或遍历完
        max_pages = 20
        while hash_set and page <= max_pages:
            try:
                resp = client.clouddownload_task_list({"page": page, "page_size": 50})
            except Exception as e:
                logger.warning(f"[115] 获取下载任务列表失败: {e}")
                break
            tasks = resp.get("tasks", []) if isinstance(resp, dict) else []
            if not tasks:
                break
            for t in tasks:
                ih = t.get("info_hash", "")
                if ih in hash_set:
                    status = t.get("status", 0)
                    percent = t.get("percentDone", 0)
                    # status: 2=完成, 1=进行中, 其他=未完成/失败
                    completed = (status == 2)
                    result[ih] = {
                        "completed": completed,
                        "percent": percent,
                        "status": status,
                        "status_text": t.get("status_text", ""),
                        "name": t.get("name", ""),
                    }
                    hash_set.discard(ih)
            total_count = resp.get("count", 0)
            if page * 50 >= total_count:
                break
            page += 1

        # 未找到的任务标记为未知
        for ih in hash_set:
            result[ih] = {"completed": False, "percent": 0, "status": -1, "status_text": "未找到任务"}

        all_done = all(v.get("completed") for v in result.values())
        logger.info(f"[115] 下载状态检查: {len(result)} 个任务, 全部完成={all_done}")
        return {"tasks": result, "all_completed": all_done}

    @classmethod
    def clouddownload_list(cls, cookies: str, page: int = 1, page_size: int = 30) -> dict:
        """获取离线下载任务列表"""
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.clouddownload_task_list(page)
            return resp
        except Exception as e:
            logger.warning(f"[115] 离线下载任务列表获取失败: {e}")
            return {"error": str(e)}

    @classmethod
    def clouddownload_del(cls, cookies: str, info_hashes: list[str], flag: int = 0) -> dict:
        """
        删除离线下载任务
        info_hashes: 任务的 info_hash 列表
        flag: 0=仅删除任务 1=删除任务及源文件
        """
        client = cls.create_client_from_cookies(cookies)
        del_source = 1 if flag else 0
        results = []
        has_error = False
        for h in info_hashes:
            if not h:
                continue
            payload = {"info_hash": h, "del_source_file": del_source}
            try:
                resp = client.clouddownload_task_del(payload)
                results.append(resp)
            except Exception as e:
                logger.warning(f"[115] 离线下载任务删除失败: {e}")
                has_error = True
        if has_error and not results:
            return {"error": "删除失败"}
        return {"data": results, "count": len(results)}

    @classmethod
    def clouddownload_clear(cls, cookies: str, flag: int = 0) -> dict:
        """
        清空离线下载任务
        flag: 0=已完成 1=全部 2=已失败 3=进行中 4=已完成+删除源文件 5=全部+删除源文件
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.clouddownload_task_clear(flag)
            return resp
        except Exception as e:
            logger.warning(f"[115] 离线下载清空失败: {e}")
            return {"error": str(e)}

    @classmethod
    def clouddownload_task_details(cls, cookies: str, info_hash: str) -> dict:
        """
        获取离线下载任务明细（名称/状态/进度/速度/文件列表）
        info_hash: 任务的 info_hash
        返回 {info_hash, name, status, status_text, percent, speed, file_count,
              files: [{name, size, path}], create_time, finish_time}
        找不到任务时返回 {"info_hash": ih, "not_found": true}
        """
        if not info_hash:
            return {"info_hash": info_hash, "not_found": True}
        client = cls.create_client_from_cookies(cookies)
        page = 1
        max_pages = 20
        # 逐页扫描，直到找到目标 hash 或遍历完
        while page <= max_pages:
            try:
                resp = client.clouddownload_task_list({"page": page, "page_size": 50})
            except Exception as e:
                logger.warning(f"[115] 获取下载任务列表失败: {e}")
                return {"info_hash": info_hash, "error": str(e)}
            tasks = resp.get("tasks", []) if isinstance(resp, dict) else []
            if not tasks:
                break
            for t in tasks:
                if t.get("info_hash", "") == info_hash:
                    return cls._format_task_detail(t)
            total_count = resp.get("count", 0)
            if page * 50 >= total_count:
                break
            page += 1
        return {"info_hash": info_hash, "not_found": True}

    @staticmethod
    def _format_task_detail(t: dict) -> dict:
        """把 115 下载任务原始字段解析为统一明细结构（适配不同字段名）"""
        # 文件列表：适配 file_list / files 字段（取 name/size/path）
        files = []
        raw_files = t.get("file_list") or t.get("files") or []
        if isinstance(raw_files, list):
            for f in raw_files:
                if not isinstance(f, dict):
                    continue
                name = f.get("name") or f.get("file_name") or f.get("n") or ""
                size = f.get("size")
                if size is None:
                    size = f.get("file_size")
                if size is None:
                    size = f.get("s")
                try:
                    size = int(size or 0)
                except (TypeError, ValueError):
                    size = 0
                path = f.get("path") or f.get("file_path") or ""
                files.append({"name": str(name), "size": size, "path": str(path)})
        # 下载速度：适配 download_speed / speed 字段（B→KB/MB/GB）
        raw_speed = t.get("download_speed")
        if raw_speed is None:
            raw_speed = t.get("speed")
        speed = Client115Service._format_speed(raw_speed)
        # 时间字段：适配 create_time / finish_time 及常见别名
        create_time = t.get("create_time") or t.get("user_ptime") or t.get("add_time") or ""
        finish_time = t.get("finish_time") or t.get("end_time") or t.get("update_time") or ""
        status = t.get("status", 0)
        return {
            "info_hash": t.get("info_hash", ""),
            "name": t.get("name", ""),
            "status": status,
            "status_text": t.get("status_text", ""),
            "percent": t.get("percentDone", 0),
            "speed": speed,
            "file_count": len(files),
            "files": files,
            "create_time": str(create_time),
            "finish_time": str(finish_time),
        }

    @staticmethod
    def _format_speed(raw) -> str:
        """把 B/s 数值格式化为可读速度文本（已格式化字符串则原样返回）"""
        if isinstance(raw, str) and raw:
            s = raw.strip()
            if not s.isdigit():
                return s or "0KB/s"
        try:
            bps = int(raw or 0)
        except (TypeError, ValueError):
            return "0KB/s"
        if bps <= 0:
            return "0KB/s"
        for unit in ("B/s", "KB/s", "MB/s", "GB/s", "TB/s"):
            if bps < 1024:
                return f"{bps:.1f}{unit}"
            bps /= 1024
        return f"{bps:.1f}PB/s"

    # ===== 分享链接转存 =====

    @classmethod
    def share_snap(cls, cookies: str, share_url: str, cid: str = "0") -> dict:
        """
        获取分享链接中的文件列表
        share_url: 115 分享链接
        cid: 分享中的目录 cid（0=根目录）
        """
        try:
            from p115client.util import share_extract_payload
        except ImportError:
            return {"error": "p115client 版本过低，不支持分享链接解析"}

        client = cls.create_client_from_cookies(cookies)
        try:
            payload = share_extract_payload(share_url)
            resp = client.share_snap({
                "share_code": payload["share_code"],
                "receive_code": payload.get("receive_code", ""),
                "cid": cid,
                "limit": 100,
                "offset": 0,
            })
            return resp
        except Exception as e:
            logger.warning(f"[115] 获取分享文件列表失败: {e}")
            return {"error": str(e)}

    @classmethod
    def share_dedup_probe(cls, cookies: str, share_url: str, cid: str = "0") -> dict:
        """O12: 转存前秒传 dry-run 探测。

        列出分享中的文件（含 sha1），逐个用 probe_by_sha1 探测本账号网盘是否已存在，
        向用户展示"X/Y 个文件已在网盘（可秒传/可跳过）"，便于大批量转存前预估。
        非破坏性：只读探测，不实际转存。

        返回: {
            "total": 文件总数,
            "existing": 已存在文件数,
            "missing": 不存在文件数,
            "files": [{"name", "sha1", "size", "exists"}],
        }
        目录（无 sha1 条目）不计入探测，但会标注 is_dir。
        """
        snap = cls.share_snap(cookies, share_url, cid)
        if isinstance(snap, dict) and snap.get("error"):
            return {"error": snap["error"]}
        # share_snap 返回 115 原始结构，文件列表在 data.list
        data = snap.get("data") if isinstance(snap, dict) else None
        items = []
        if isinstance(data, dict):
            items = data.get("list") or []
        elif isinstance(snap, dict):
            items = snap.get("list") or []

        files: list[dict] = []
        existing = 0
        missing = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            name = str(it.get("n") or it.get("fn") or "")
            # 目录条目无 sha1（115 用 fid 存在与否/‘c’ 字段区分），跳过探测
            sha1 = str(it.get("sha") or it.get("sha1") or "")
            is_dir = not sha1 and (it.get("c") is not None or it.get("fid") is None)
            if is_dir or not sha1:
                files.append({"name": name, "sha1": "", "size": it.get("s") or 0,
                              "exists": False, "is_dir": True})
                continue
            exists = cls.probe_by_sha1(cookies, sha1).get("exists", False)
            if exists:
                existing += 1
            else:
                missing += 1
            files.append({"name": name, "sha1": sha1, "size": it.get("s") or 0,
                          "exists": exists, "is_dir": False})
        return {
            "total": existing + missing,
            "existing": existing,
            "missing": missing,
            "files": files,
        }

    @classmethod
    def share_receive(cls, cookies: str, share_url: str, file_ids: list[str], target_cid: str = "0") -> dict:
        """
        转存分享链接中的文件到自己的网盘
        share_url: 115 分享链接
        file_ids: 要转存的文件/目录 id 列表
        target_cid: 保存到自己的网盘目录 cid（0=根目录）
        """
        try:
            from p115client.util import share_extract_payload
        except ImportError:
            return {"error": "p115client 版本过低，不支持分享链接解析"}

        client = cls.create_client_from_cookies(cookies)
        try:
            payload = share_extract_payload(share_url)
            resp = client.share_receive(
                {
                    "share_code": payload["share_code"],
                    "receive_code": payload.get("receive_code", ""),
                    "file_id": ",".join(str(fid) for fid in file_ids),
                    "cid": target_cid,
                },
                share_url=share_url,
            )
            return resp
        except Exception as e:
            logger.warning(f"[115] 分享转存失败: {e}")
            return {"error": str(e)}

    # ===== 分享链接管理（创建/列表/取消） =====

    @classmethod
    def share_create(cls, cookies: str, file_ids: list[str], share_to: str = "all",
                     password: str = "", expire_days: int = 0) -> dict:
        """
        创建分享链接（我发出的分享）
        file_ids: 要分享的文件/目录 id 列表
        share_to: 分享对象（all=所有人）
        password: 访问密码（留空=无密码）
        expire_days: 有效期天数（0=长期）
        说明：p115client 的 share_send(payload) 会把 payload 与默认参数合并后原样
        POST 到 https://webapi.115.com/share/send，官方封装保证 file_ids/is_asc/
        order/ignore_warn 字段；115 web 分享接口的 share_to/pwd/expire_days/source
        字段同样随 payload 透传，此处按其合法参数组合构造。
        """
        if not file_ids:
            return {"error": "请选择要分享的文件"}
        client = cls.create_client_from_cookies(cookies)
        payload = {
            "file_ids": ",".join(str(fid) for fid in file_ids),
            "share_to": share_to,
            "source": 1,
        }
        if password:
            payload["pwd"] = password
        if expire_days and expire_days > 0:
            payload["expire_days"] = int(expire_days)
        try:
            resp = client.share_send(payload)
            return resp
        except Exception as e:
            logger.warning(f"[115] 创建分享失败: {e}")
            # 兜底：share_send 不可用时，改用 usershare_share 创建单个文件的分享
            try:
                if len(file_ids) == 1:
                    resp = client.usershare_share({
                        "file_id": str(file_ids[0]),
                        "share_opt": 1,
                        "safe_pwd": password or "",
                        "ignore_warn": 1,
                    })
                    return resp
            except Exception as e2:
                logger.warning(f"[115] 创建分享兜底失败: {e2}")
            return {"error": str(e)}

    @classmethod
    def share_list_created(cls, cookies: str, page: int = 1, page_size: int = 20) -> dict:
        """
        获取我发出的分享列表
        page/page_size: 分页参数（p115client 的 share_list 使用 limit/offset，此处换算）
        返回 {"shares": [{"share_code", "name", "file_count", "create_time", "expire_time", "status", "share_url"}], "count": int}
        """
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.share_list({
                "limit": max(1, page_size),
                "offset": max(0, (page - 1) * page_size),
            })
            if not isinstance(resp, dict):
                return {"shares": [], "count": 0}
            raw = resp.get("data") or []
            if not isinstance(raw, list):
                raw = []
            shares = []
            for s in raw:
                if not isinstance(s, dict):
                    continue
                shares.append({
                    "share_code": s.get("share_code", ""),
                    "name": s.get("title") or s.get("name") or "",
                    "file_count": s.get("file_count", 0),
                    "create_time": s.get("share_time", ""),
                    "expire_time": s.get("expire_time", 0),
                    "status": s.get("status", 0),
                    "share_url": s.get("share_url", ""),
                })
            return {"shares": shares, "count": resp.get("count", len(shares))}
        except Exception as e:
            logger.warning(f"[115] 获取分享列表失败: {e}")
            return {"error": str(e)}

    @classmethod
    def share_cancel(cls, cookies: str, share_code: str) -> dict:
        """
        取消分享
        说明：p115client 的 share_update(payload) 官方参数中无 status 字段，
        115 web 接口通过 share/updateshare 的 action="cancel" 取消分享（signature 已核查）。
        """
        if not share_code:
            return {"error": "缺少 share_code"}
        client = cls.create_client_from_cookies(cookies)
        try:
            resp = client.share_update({"share_code": share_code, "action": "cancel"})
            return resp
        except Exception as e:
            logger.warning(f"[115] 取消分享失败: {e}")
            return {"error": str(e)}

    @classmethod
    def cleanup_empty_dirs(cls, cookies: str, root_cid: str) -> int:
        """
        递归删除 root_cid 下的所有空子目录（不删除 root_cid 本身）。
        从最深层开始清理，确保子目录删除后父目录也可能变为空并被一并清理。
        只删除确认为空的目录（无任何文件或子目录），不删除含文件的目录。
        返回删除的目录数量。
        """
        client = cls.create_client_from_cookies(cookies)

        def _collect_subdirs(cid: str) -> list[dict]:
            """列出 cid 下的所有直接子目录"""
            subdirs = []
            offset = 0
            while True:
                resp = _fs_files_with_retry(client, {
                    "cid": cid, "offset": offset, "limit": 1000, "show_dir": 1,
                })
                items = resp.get("data", []) or []
                if not items:
                    break
                for it in items:
                    if not it.get("fid"):  # 是目录
                        subdirs.append({"cid": str(it.get("cid", "")), "name": it.get("n", "")})
                total = resp.get("count", 0)
                offset += len(items)
                if offset >= total:
                    break
                _apply_file_list_interval(context="扫描空目录")
            return subdirs

        def _is_empty(cid: str) -> bool:
            """检查目录是否为空（无任何文件或子目录）"""
            resp = _fs_files_with_retry(client, {
                "cid": cid, "offset": 0, "limit": 1, "show_dir": 1,
            })
            items = resp.get("data", []) or []
            return len(items) == 0

        def _cleanup(cid: str) -> int:
            count = 0
            subdirs = _collect_subdirs(cid)
            for subdir in subdirs:
                # 先递归清理子目录的子目录
                count += _cleanup(subdir["cid"])
                # 再检查子目录是否已变空
                if _is_empty(subdir["cid"]):
                    try:
                        resp = _call_write_with_405_fallback(
                            client, "fs_delete", "fs_delete_app",
                            [subdir["cid"]],
                        )
                        if isinstance(resp, dict) and resp.get("state") is False:
                            logger.warning(f"[115] 删除空目录失败: {subdir['name']}: {resp.get('error', '')}")
                        else:
                            count += 1
                            logger.info(f"[115] 删除空目录: {subdir['name']}")
                    except Exception as e:
                        logger.warning(f"[115] 删除空目录异常: {subdir['name']}: {e}")
            return count

        return _cleanup(root_cid)

    # ===== O3: OOF 快速媒体信息（sha1 秒传探测，免下载） =====
    # 参考 MoviePilot-Plugins p115strmhelper 的 OOF 思路：用 115 的"文件校验/秒传"
    # 接口按 sha1 探测网盘是否已存在该文件，并尝试拉取同目录的 nfo/jpg 媒体信息，
    # 免实际下载。p115client 提供了 fs_shasearch（GET /files/shasearch）实现探测。

    @classmethod
    def probe_by_sha1(cls, cookies: str, sha1: str) -> dict:
        """按 sha1 探测 115 网盘是否已存在该文件（秒传校验思路，免下载）。

        调用 p115client 的 fs_shasearch（GET https://webapi.115.com/files/shasearch）。
        该接口最多返回一条记录；未命中时 115 返回 {"state": false, "error": "文件错误"}，
        p115client 会将其转为异常抛出，此处统一视为"不存在"。

        返回: {
            "exists": bool,
            "file_id": str,   # 命中时为文件 id，否则空串
            "pickcode": str,  # 命中时为 pickcode，否则空串
            "parent_id": str, # 命中时为父目录 id（供 fetch_media_info_fast 列目录用）
            "name": str,      # 命中时为文件名
        }
        若当前 p115client 不支持 fs_shasearch，则降级返回
        {"exists": False, "reason": "p115client 不支持 sha1 探测", ...}。
        """
        if not sha1:
            return {"exists": False, "file_id": "", "pickcode": "", "parent_id": "", "name": ""}
        client = cls.create_client_from_cookies(cookies)
        if not hasattr(client, "fs_shasearch"):
            # 降级：p115client 版本过低，无 sha1 探测能力（遍历/搜索不可行，直接返回未命中）
            return {"exists": False, "reason": "p115client 不支持 sha1 探测",
                    "file_id": "", "pickcode": "", "parent_id": "", "name": ""}
        try:
            resp = client.fs_shasearch(sha1)
            if not (isinstance(resp, dict) and resp.get("state") and resp.get("data")):
                return {"exists": False, "file_id": "", "pickcode": "", "parent_id": "", "name": ""}
            data = resp["data"]
            return {
                "exists": True,
                "file_id": str(data.get("fid") or data.get("file_id") or ""),
                "pickcode": str(data.get("pc") or data.get("pickcode") or ""),
                "parent_id": str(data.get("cid") or data.get("category_id") or ""),
                "name": str(data.get("n") or data.get("file_name") or ""),
            }
        except Exception as e:
            # 未命中（state:false 抛异常）或网络错误均视为不存在
            logger.debug(f"[115] probe_by_sha1 未命中 {str(sha1)[:12]}: {e}")
            return {"exists": False, "file_id": "", "pickcode": "", "parent_id": "", "name": ""}

    @classmethod
    def fetch_media_info_fast(cls, cookies: str, sha1: str) -> dict:
        """OOF 快速媒体信息：按 sha1 探测文件存在后，拉取同目录 nfo/jpg 信息列表。

        先 probe_by_sha1 探测；探测成功拿到 file_id 后调用 list_files 列出其父目录
        下的 nfo/jpg 文件并返回信息列表（不实际下载文件本体）。
        结果按 sha1 缓存 1 小时（上限 500，锁保护），避免重复探测 115。

        返回: {
            "nfo_files": [{"name", "file_id", "pickcode", "size"}],
            "image_files": [...],
        }
        探测失败或无父目录信息时返回空 dict {}。
        """
        if not sha1:
            return {}
        # 1. 命中缓存直接返回
        with _oof_lock:
            cached = _oof_cache.get(sha1)
            if cached is not None:
                if _time.time() - cached.get("ts", 0) < _OOF_CACHE_TTL:
                    return cached.get("data") or {}
                else:
                    _oof_cache.pop(sha1, None)
        # 2. 按 sha1 探测是否已存在
        probe = cls.probe_by_sha1(cookies, sha1)
        if not probe.get("exists"):
            result: dict = {}
            # 探测失败（含"网盘无此文件"）：也缓存，避免重复探测
            return cls._oof_cache_put(sha1, result)
        # 3. 探测成功：列出父目录下所有条目，筛出 nfo/jpg
        parent_id = probe.get("parent_id") or ""
        nfo_files: list[dict] = []
        image_files: list[dict] = []
        if parent_id:
            resp = cls.list_files(cookies, parent_id, offset=0, limit=1000)
            if not (isinstance(resp, dict) and not resp.get("_error")):
                # 列目录失败：返回空（不缓存，下次重试）
                return {}
            for it in (resp.get("data") or []):
                if not isinstance(it, dict) or not it.get("fid"):
                    continue  # 仅关注文件条目，跳过目录
                it_name = str(it.get("n") or "")
                low = it_name.lower()
                entry = {
                    "name": it_name,
                    "file_id": str(it.get("fid") or ""),
                    "pickcode": str(it.get("pc") or ""),
                    "size": it.get("s") or 0,
                }
                if low.endswith(".nfo"):
                    nfo_files.append(entry)
                elif low.endswith((".jpg", ".jpeg", ".png", ".webp")):
                    image_files.append(entry)
        result = {"nfo_files": nfo_files, "image_files": image_files}
        return cls._oof_cache_put(sha1, result)

    @classmethod
    def _oof_cache_put(cls, sha1: str, data: dict) -> dict:
        """写入 OOF 缓存（TTL 1 小时，上限 500，锁保护），并返回原数据。"""
        with _oof_lock:
            if len(_oof_cache) >= _OOF_CACHE_MAX:
                now = _time.time()
                # 先清过期条目
                expired = [k for k, v in _oof_cache.items()
                           if now - v.get("ts", 0) >= _OOF_CACHE_TTL]
                for k in expired:
                    _oof_cache.pop(k, None)
                # 仍超上限则移除最旧条目
                if len(_oof_cache) >= _OOF_CACHE_MAX and _oof_cache:
                    oldest_key = min(_oof_cache, key=lambda k: _oof_cache[k].get("ts", 0))
                    _oof_cache.pop(oldest_key, None)
            _oof_cache[sha1] = {"ts": _time.time(), "data": data}
        return data
