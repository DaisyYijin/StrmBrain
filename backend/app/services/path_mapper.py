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
from typing import Optional

from app.core.json_storage import read_setting, save_setting
from app.core.logbuffer import get_logger

logger = get_logger("app.services.path_mapper")

# 合法的操作类型
_VALID_OPS = {"replace", "replaceAll", "prefix", "suffix"}

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
