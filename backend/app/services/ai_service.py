"""
AI 辅助媒体识别服务

参考 qmediasync 的 openai/client.go 设计。

使用 OpenAI 兼容 API（支持 SiliconFlow、OpenAI、DeepSeek 等）从文件名中
提取电影/电视剧的名称、年份、季号、集号等元数据信息。

特性：
1. 兼容 OpenAI Chat Completions API 格式
2. 支持自定义 base_url（SiliconFlow / OpenAI / 本地模型等）
3. 内置重试机制
4. 响应自动解析 JSON（去除 ```json 标记）
5. 全局单例管理

AI 配置存储在 settings.json 的 "ai" 键：
{
    "api_key": "sk-xxx",
    "base_url": "https://api.siliconflow.cn",
    "model_name": "Qwen/Qwen2.5-7B-Instruct",
    "timeout": 60,
    "enabled": false
}
"""
import json
import re
import asyncio
from typing import Any, Optional

import httpx

from app.core.json_storage import read_setting, save_setting
from app.core.logbuffer import get_logger

logger = get_logger("app.services.ai_service")

# AI 配置在 settings.json 中的键名
_AI_SETTINGS_KEY = "ai"

# 默认 prompt
_DEFAULT_MOVIE_PROMPT = (
    "从文件名中提取出电影名称、年份；名称中不能有特殊字符如点、下划线、横杠、斜杠等"
)

_DEFAULT_TV_PROMPT = (
    "从文件名中提取出电视剧名称、年份、季号(season)、集号(episode)；"
    "名称中不能有特殊字符如点、下划线、横杠、斜杠等"
)

# 默认配置
_DEFAULT_CONFIG = {
    "api_key": "",
    "base_url": "https://api.siliconflow.cn",
    "model_name": "Qwen/Qwen2.5-7B-Instruct",
    "timeout": 60,
    "enabled": False,
}


class AIService:
    """
    AI 辅助媒体识别服务

    使用 OpenAI 兼容 API 从文件名中提取影视元数据。

    用法：
        client = AIService(api_key="sk-xxx", base_url="https://api.siliconflow.cn",
                           model_name="Qwen/Qwen2.5-7B-Instruct")
        movie_info = await client.extract_movie_name("The.Matrix.1999.1080p.BluRay.mkv")
        # {"name": "The Matrix", "year": 1999}

        tv_info = await client.extract_tv_name("Breaking.Bad.S01E01.2008.1080p.mkv")
        # {"name": "Breaking Bad", "year": 2008, "season": 1, "episode": 1}
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn",
        model_name: str = "Qwen/Qwen2.5-7B-Instruct",
        timeout: int = 60,
    ) -> None:
        """
        初始化 AI 服务客户端。

        Args:
            api_key: API 密钥（如 sk-xxx）
            base_url: API 基础地址（兼容 OpenAI 格式的服务）
            model_name: 模型名称
            timeout: 请求超时时间（秒）
        """
        self.api_key: str = api_key
        self.base_url: str = base_url.rstrip("/")
        self.model_name: str = model_name
        self.timeout: int = timeout

    # ===== 核心请求方法 =====

    async def create_chat_completion(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> Optional[dict]:
        """
        通用对话补全请求。

        向 OpenAI 兼容 API 发送 chat/completions 请求。

        Args:
            messages: 消息列表，格式为 [{"role": "system", "content": "..."}, ...]
            **kwargs: 额外请求参数（temperature, max_tokens 等）

        Returns:
            API 响应 dict，包含 choices 等字段。失败返回 None。
        """
        if not self.api_key:
            logger.warning("AI 请求失败：api_key 未配置")
            return None

        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
        }
        # 合并额外参数
        payload.update(kwargs)

        logger.debug(
            f"AI 请求: url={url}, model={self.model_name}, "
            f"messages_count={len(messages)}"
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    logger.warning(
                        f"AI 请求返回 {resp.status_code}: {resp.text[:500]}"
                    )
                    return None
                return resp.json()
        except httpx.TimeoutException:
            logger.error(f"AI 请求超时 ({self.timeout}s): url={url}")
            return None
        except httpx.ConnectError as e:
            logger.error(f"AI 连接失败: {e}")
            return None
        except Exception as e:
            logger.error(f"AI 请求异常: {type(e).__name__}: {e}")
            return None

    # ===== 响应解析 =====

    @staticmethod
    def _extract_content(response: Optional[dict]) -> str:
        """
        从 chat completion 响应中提取文本内容。

        Args:
            response: API 响应 dict

        Returns:
            模型生成的文本内容，失败返回空字符串
        """
        if not response or not isinstance(response, dict):
            return ""
        try:
            choices = response.get("choices", [])
            if not choices:
                return ""
            message = choices[0].get("message", {})
            content = message.get("content", "")
            return content.strip() if isinstance(content, str) else str(content).strip()
        except Exception as e:
            logger.warning(f"解析 AI 响应内容失败: {e}")
            return ""

    @staticmethod
    def _parse_json_response(text: str) -> Optional[dict]:
        """
        解析 AI 返回的 JSON 文本。

        自动去除 ```json 和 ``` 标记，提取 JSON 内容并解析。

        Args:
            text: AI 返回的原始文本

        Returns:
            解析后的 dict，失败返回 None
        """
        if not text:
            return None

        cleaned = text.strip()

        # 去除 ```json ... ``` 或 ``` ... ``` 标记
        # 匹配 ```json\n...\n``` 或 ```\n...\n```
        fence_pattern = r"^```(?:json)?\s*\n?(.*?)\n?\s*```$"
        match = re.match(fence_pattern, cleaned, re.DOTALL | re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip()

        # 如果还有残留的 ``` 开头/结尾，去除
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        if cleaned.endswith("```"):
            cleaned = re.sub(r"\s*```$", "", cleaned)

        cleaned = cleaned.strip()

        # 尝试直接解析
        try:
            result = json.loads(cleaned)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        # 尝试从文本中提取第一个 JSON 对象 {...}
        json_pattern = r'\{[^{}]*\}'
        match = re.search(json_pattern, cleaned, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        # 尝试提取嵌套 JSON（可能包含数组）
        nested_pattern = r'\{.*\}'
        match = re.search(nested_pattern, cleaned, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

        logger.warning(f"无法解析 AI 返回的 JSON: {text[:200]}")
        return None

    # ===== 带重试的请求 =====

    async def _request_with_retry(
        self,
        messages: list[dict[str, str]],
        max_retries: int = 1,
        retry_delay: float = 1.0,
        **kwargs: Any,
    ) -> Optional[dict]:
        """
        带重试机制的对话补全请求。

        Args:
            messages: 消息列表
            max_retries: 最大重试次数（默认 1 次）
            retry_delay: 重试间隔（秒）
            **kwargs: 额外请求参数

        Returns:
            API 响应 dict，全部失败返回 None
        """
        last_response: Optional[dict] = None
        for attempt in range(max_retries + 1):
            response = await self.create_chat_completion(messages, **kwargs)
            if response is not None:
                return response
            last_response = response
            if attempt < max_retries:
                logger.info(f"AI 请求失败，{retry_delay}s 后重试 (attempt {attempt + 1}/{max_retries + 1})")
                await asyncio.sleep(retry_delay)
        return last_response

    # ===== 业务方法 =====

    async def extract_movie_name(
        self,
        filename: str,
        prompt: str = "",
    ) -> Optional[dict]:
        """
        从文件名提取电影名称和年份。

        Args:
            filename: 视频文件名
            prompt: 自定义提取 prompt，为空时使用默认 prompt

        Returns:
            {"name": "电影名称", "year": 2023}，失败返回 None。
            year 可能为 None（文件名中无年份信息时）。
        """
        user_prompt = prompt or _DEFAULT_MOVIE_PROMPT

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个影视文件名解析助手。根据用户提供的文件名，提取电影信息。"
                    "只返回 JSON 格式数据，不要返回其他内容。"
                    "JSON 格式：{\"name\": \"电影名称\", \"year\": 年份或null}"
                ),
            },
            {
                "role": "user",
                "content": f"{user_prompt}\n\n文件名：{filename}",
            },
        ]

        response = await self._request_with_retry(
            messages,
            max_retries=1,
            retry_delay=1.0,
            temperature=0.1,
        )

        content = self._extract_content(response)
        if not content:
            logger.warning(f"AI 提取电影信息失败：响应为空, filename={filename}")
            return None

        result = self._parse_json_response(content)
        if result is None:
            logger.warning(f"AI 提取电影信息解析失败: filename={filename}, content={content[:200]}")
            return None

        # 标准化输出
        name = result.get("name", "").strip()
        if not name:
            logger.warning(f"AI 提取电影信息：name 为空, filename={filename}")
            return None

        year = result.get("year")
        # year 可能是字符串或整数，统一处理
        if year is not None:
            try:
                year = int(year)
            except (ValueError, TypeError):
                year = None

        logger.info(f"AI 提取电影信息: filename={filename} -> name={name}, year={year}")
        return {"name": name, "year": year}

    async def extract_tv_name(
        self,
        filename: str,
        prompt: str = "",
    ) -> Optional[dict]:
        """
        从文件名提取电视剧名称、年份、季号、集号。

        Args:
            filename: 视频文件名
            prompt: 自定义提取 prompt，为空时使用默认 prompt

        Returns:
            {"name": "电视剧名称", "year": 2023, "season": 1, "episode": 1}，
            失败返回 None。year/season/episode 可能为 None。
        """
        user_prompt = prompt or _DEFAULT_TV_PROMPT

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个影视文件名解析助手。根据用户提供的文件名，提取电视剧信息。"
                    "只返回 JSON 格式数据，不要返回其他内容。"
                    "JSON 格式：{\"name\": \"剧名\", \"year\": 年份或null, "
                    "\"season\": 季号或null, \"episode\": 集号或null}"
                ),
            },
            {
                "role": "user",
                "content": f"{user_prompt}\n\n文件名：{filename}",
            },
        ]

        response = await self._request_with_retry(
            messages,
            max_retries=1,
            retry_delay=1.0,
            temperature=0.1,
        )

        content = self._extract_content(response)
        if not content:
            logger.warning(f"AI 提取电视剧信息失败：响应为空, filename={filename}")
            return None

        result = self._parse_json_response(content)
        if result is None:
            logger.warning(f"AI 提取电视剧信息解析失败: filename={filename}, content={content[:200]}")
            return None

        # 标准化输出
        name = result.get("name", "").strip()
        if not name:
            logger.warning(f"AI 提取电视剧信息：name 为空, filename={filename}")
            return None

        year = self._safe_int(result.get("year"))
        season = self._safe_int(result.get("season"))
        episode = self._safe_int(result.get("episode"))

        logger.info(
            f"AI 提取电视剧信息: filename={filename} -> "
            f"name={name}, year={year}, season={season}, episode={episode}"
        )
        return {
            "name": name,
            "year": year,
            "season": season,
            "episode": episode,
        }

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """安全转换为整数，失败返回 None"""
        if value is None:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    def is_configured(self) -> bool:
        """检查 AI 服务是否已正确配置"""
        return bool(self.api_key and self.base_url and self.model_name)


# ===== 全局单例管理 =====

# 全局 AI 客户端单例
global_ai_client: Optional[AIService] = None


def init_ai_client() -> Optional[AIService]:
    """
    从 settings.json 的 "ai" 键读取配置并初始化全局 AI 客户端。

    配置格式：
    {
        "api_key": "sk-xxx",
        "base_url": "https://api.siliconflow.cn",
        "model_name": "Qwen/Qwen2.5-7B-Instruct",
        "timeout": 60,
        "enabled": false
    }

    Returns:
        初始化后的 AIService 实例，未配置或未启用时返回 None。
    """
    global global_ai_client

    config = read_setting(_AI_SETTINGS_KEY)

    api_key = config.get("api_key", "").strip()
    base_url = config.get("base_url", "").strip() or _DEFAULT_CONFIG["base_url"]
    model_name = config.get("model_name", "").strip() or _DEFAULT_CONFIG["model_name"]
    timeout = config.get("timeout", _DEFAULT_CONFIG["timeout"])
    enabled = config.get("enabled", False)

    if not enabled:
        logger.info("AI 服务未启用，跳过初始化")
        global_ai_client = None
        return None

    if not api_key:
        logger.warning("AI 服务已启用但 api_key 未配置，无法初始化")
        global_ai_client = None
        return None

    # 确保 timeout 是整数
    try:
        timeout = int(timeout)
    except (ValueError, TypeError):
        timeout = _DEFAULT_CONFIG["timeout"]

    global_ai_client = AIService(
        api_key=api_key,
        base_url=base_url,
        model_name=model_name,
        timeout=timeout,
    )

    logger.info(
        f"AI 客户端已初始化: base_url={base_url}, model={model_name}, timeout={timeout}s"
    )
    return global_ai_client


def get_ai_client() -> Optional[AIService]:
    """
    获取全局 AI 客户端。

    如果尚未初始化，会自动尝试从配置初始化。

    Returns:
        AIService 实例，未配置时返回 None。
    """
    global global_ai_client
    if global_ai_client is None:
        return init_ai_client()
    return global_ai_client


def save_ai_config(config: dict) -> bool:
    """
    保存 AI 配置到 settings.json。

    Args:
        config: AI 配置 dict，包含 api_key, base_url, model_name, timeout, enabled

    Returns:
        是否保存成功
    """
    # 合并默认配置
    merged = dict(_DEFAULT_CONFIG)
    merged.update(config)
    # 确保 enabled 是布尔值
    merged["enabled"] = bool(merged.get("enabled", False))
    # 确保 timeout 是整数
    try:
        merged["timeout"] = int(merged.get("timeout", 60))
    except (ValueError, TypeError):
        merged["timeout"] = 60

    success = save_setting(_AI_SETTINGS_KEY, merged)
    if success:
        logger.info("AI 配置已保存")
        # 配置变更后重新初始化客户端
        init_ai_client()
    return success


def get_ai_config() -> dict:
    """
    读取当前 AI 配置（脱敏：不返回完整 api_key）。

    Returns:
        AI 配置 dict，api_key 字段做脱敏处理
    """
    config = read_setting(_AI_SETTINGS_KEY)
    merged = dict(_DEFAULT_CONFIG)
    merged.update(config)

    # 脱敏 api_key
    api_key = merged.get("api_key", "")
    if api_key and len(api_key) > 8:
        merged["api_key"] = api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:]
    elif api_key:
        merged["api_key"] = "*" * len(api_key)

    return merged
