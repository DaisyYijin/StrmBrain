"""
路径映射系统 - 在 STRM 生成 / 本地文件落盘前对路径或内容进行映射变换。

支持四种操作（按规则顺序应用）：
- replace:     替换首个匹配子串（path.replace(from, to, 1)）
- replaceAll:  替换全部匹配子串（path.replace(from, to)）
- prefix:      在路径前追加前缀（to + path）
- suffix:      在路径后追加后缀（path + to）

每条规则可指定作用的来源类型（source）：
- local:     本地文件系统绝对路径
- strm_rel:  STRM 文件相对路径（相对本地媒体根目录）
- strm_url:  STRM 文件内容（播放 URL）
- all:       上述全部来源（默认）

配置持久化在 settings.json 的 "path_mapping" 键下（通过 read_setting / save_setting）。
"""
import hmac
import hashlib
import base64
import re
import time
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

from typing import Optional

from app.core.json_storage import read_setting, save_setting
from app.core.logbuffer import get_logger

logger = get_logger("app.services.path_mapper")

# 合法的操作类型
_VALID_OPS = {"replace", "replaceAll", "prefix", "suffix", "regex"}

# 合法的来源类型
_VALID_SOURCES = {"local", "strm_rel", "strm_url", "all"}


class PathMapper:
    """路径映射器：按规则顺序对路径/内容应用映射变换。"""

    def __init__(self, rules: Optional[list] = None):
        """
        接收路径映射规则列表，每条规则形如：
        {"op": "replace"|"prefix"|"suffix"|"replaceAll",
         "source": "local"|"strm_rel"|"strm_url"|"all",
         "from": str, "to": str}
        """
        self.rules = rules or []

    def apply(self, path: str, source_type: str) -> str:
        """
        按规则顺序应用映射。

        Args:
            path:       待映射的路径或内容字符串
            source_type: 当前来源类型（local / strm_rel / strm_url）

        Returns:
            映射后的字符串；无匹配规则时原样返回。
        """
        if not path:
            return path

        result = path
        for rule in self.rules:
            if not isinstance(rule, dict):
                continue

            # 来源类型过滤：source=all 时匹配所有，否则需精确匹配
            src = rule.get("source", "all")
            if src != "all" and src != source_type:
                continue

            op = rule.get("op", "")
            if op not in _VALID_OPS:
                logger.debug(f"[path_mapper] 跳过未知操作类型: {op}")
                continue

            frm = rule.get("from", "")
            to = rule.get("to", "")

            if op == "replace":
                # 仅替换首个匹配
                if frm:
                    result = result.replace(frm, to, 1)
            elif op == "replaceAll":
                # 替换全部匹配
                if frm:
                    result = result.replace(frm, to)
            elif op == "regex":
                # O10: 正则替换（from 为正则，to 支持 \1 反向引用），
                # 覆盖前缀/后缀/条件路径等复杂场景。正则无效则跳过该规则。
                if frm:
                    try:
                        result = re.sub(frm, to, result)
                    except re.error:
                        logger.debug(f"[path_mapper] 跳过无效正则: {frm}")
            elif op == "prefix":
                # 前缀追加
                result = to + result
            elif op == "suffix":
                # 后缀追加
                result = result + to

        return result

    @classmethod
    def get_config(cls) -> list:
        """
        从 settings.json 读取路径映射规则列表。
        返回规则列表（list of dict），未配置时返回空列表。
        """
        data = read_setting("path_mapping")
        if not isinstance(data, dict):
            return []
        rules = data.get("rules", [])
        return rules if isinstance(rules, list) else []

    @classmethod
    def save_config(cls, rules: list) -> bool:
        """
        将路径映射规则列表保存到 settings.json。
        返回是否保存成功。
        """
        if not isinstance(rules, list):
            rules = []
        # 保存前做轻量校验，剔除结构不完整的规则
        cleaned = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            op = rule.get("op", "")
            if op not in _VALID_OPS:
                continue
            src = rule.get("source", "all")
            if src not in _VALID_SOURCES:
                src = "all"
            cleaned.append({
                "op": op,
                "source": src,
                "from": str(rule.get("from", "")),
                "to": str(rule.get("to", "")),
            })
        return save_setting("path_mapping", {"rules": cleaned})

    @classmethod
    def create_from_config(cls) -> "PathMapper":
        """从已保存的配置创建 PathMapper 实例（便捷方法）。"""
        return cls(cls.get_config())

    def apply_signing(self, url: str) -> str:
        """
        根据 alist_sign 配置自动对 strm_url 类型的 URL 进行 alist 签名。

        读取 settings.json 中的 "alist_sign" 配置，若已启用且配置了
        secret_key，则对传入的 URL 调用 sign_alist_url 进行签名；
        否则原样返回。

        Args:
            url: 待签名的 strm 播放 URL

        Returns:
            签名后的 URL（若未启用签名则原样返回）
        """
        config = AlistSigner.get_config()
        if not config.get("enabled", False):
            return url
        secret_key = config.get("secret_key", "")
        if not secret_key:
            return url
        expire_seconds = config.get("expire_seconds", 7200)
        return sign_alist_url(url, secret_key, expire_seconds)


def sign_alist_url(url: str, secret_key: str, expire_seconds: int = 7200) -> str:
    """
    对 alist 风格的 URL 进行 HMAC-SHA256 签名。

    提取 URL 中 /d/ 及其后的完整路径作为签名内容，
    使用 HMAC-SHA256(secret_key, path + expire) 计算签名，
    并添加 sign、expire 参数到 URL query string。

    Args:
        url:            待签名的完整 URL
        secret_key:     签名密钥
        expire_seconds: 签名有效期（秒），默认 7200（2小时）

    Returns:
        签名后的 URL；若 URL 不包含 /d/ 路径或 secret_key 为空则原样返回。
    """
    if not secret_key or "/d/" not in url:
        return url

    parsed = urlparse(url)
    # 提取 /d/ 及其后的完整路径作为签名内容
    d_index = parsed.path.find("/d/")
    sign_path = parsed.path[d_index:]

    expire = int(time.time()) + expire_seconds
    # HMAC-SHA256(secret_key, path + expire)
    raw = sign_path + str(expire)
    sign = base64.urlsafe_b64encode(
        hmac.new(
            secret_key.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    # 添加 sign、expire 到 query string
    query_params = dict(parse_qsl(parsed.query))
    query_params["sign"] = sign
    query_params["expire"] = str(expire)
    new_query = urlencode(query_params)

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment,
    ))


class AlistSigner:
    """alist 风格 URL 签名器。"""

    def __init__(self, secret_key: str = "", expire_seconds: int = 7200):
        """
        Args:
            secret_key:     签名密钥，为空时签名功能不生效
            expire_seconds: 签名有效期（秒）
        """
        self.secret_key = secret_key
        self.expire_seconds = expire_seconds

    def sign(self, url: str) -> str:
        """对 URL 进行签名（调用 sign_alist_url）。"""
        return sign_alist_url(url, self.secret_key, self.expire_seconds)

    def is_enabled(self) -> bool:
        """检查是否配置了 secret_key（签名功能是否可用）。"""
        return bool(self.secret_key)

    @classmethod
    def get_config(cls) -> dict:
        """
        从 settings.json 读取 "alist_sign" 配置。
        返回 {"enabled": bool, "secret_key": str, "expire_seconds": int}。
        """
        data = read_setting("alist_sign")
        if not isinstance(data, dict):
            return {"enabled": False, "secret_key": "", "expire_seconds": 7200}
        return {
            "enabled": bool(data.get("enabled", False)),
            "secret_key": str(data.get("secret_key", "")),
            "expire_seconds": int(data.get("expire_seconds", 7200)),
        }

    @classmethod
    def save_config(cls, enabled: bool, secret_key: str, expire_seconds: int) -> bool:
        """
        将 alist_sign 配置保存到 settings.json。
        返回是否保存成功。
        """
        return save_setting("alist_sign", {
            "enabled": enabled,
            "secret_key": secret_key,
            "expire_seconds": expire_seconds,
        })
