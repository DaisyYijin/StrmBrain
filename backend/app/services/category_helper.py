"""
二级分类匹配引擎
基于 MoviePilot 的分类逻辑，支持 YAML 格式配置
按 TMDB 元数据字段（genre_ids, original_language, origin_country, production_countries, release_year）进行匹配
"""
import yaml
from typing import Optional


# 默认分类配置（YAML 格式）
DEFAULT_CATEGORY_YAML = """# 配置电影的分类策略
movie:
  # 分类名同时也是目录名
  动画电影:
    # 匹配 genre_ids 内容类型，16是动漫
    genre_ids: '16'
  华语电影:
    # 匹配语种
    original_language: 'zh,cn,bo,za'
  # 未匹配以上条件时，分类为外语电影
  外语电影:

# 配置电视剧的分类策略
tv:
  # 分类名同时也是目录名
  国漫:
    # 匹配 genre_ids 内容类型，16是动漫
    genre_ids: '16'
    # 匹配 origin_country 国家，CN是中国大陆，TW是中国台湾，HK是中国香港
    origin_country: 'CN,TW,HK'
  日番:
    # 匹配 genre_ids 内容类型，16是动漫
    genre_ids: '16'
    # 匹配 origin_country 国家，JP是日本
    origin_country: 'JP'
  纪录片:
    # 匹配 genre_ids 内容类型，99是纪录片
    genre_ids: '99'
  儿童:
    # 匹配 genre_ids 内容类型，10762是儿童
    genre_ids: '10762'
  综艺:
    # 匹配 genre_ids 内容类型，10764 10767都是综艺
    genre_ids: '10764,10767'
  国产剧:
    # 匹配 origin_country 国家，CN是中国大陆，TW是中国台湾，HK是中国香港
    origin_country: 'CN,TW,HK'
  欧美剧:
    # 匹配 origin_country 国家，主要欧美国家列表
    origin_country: 'US,FR,GB,DE,ES,IT,NL,PT,RU,UK'
  日韩剧:
    # 匹配 origin_country 国家，主要亚洲国家列表
    origin_country: 'JP,KP,KR,TH,IN,SG'
  # 未匹配以上分类，则命名为未分类
  未分类:

# 配置 AV 的分类策略（按番号前缀匹配厂商）
av:
  S1事务所:
    # 匹配番号前缀，逗号分隔
    code_prefix: 'SSIS,SSNI,SSISD,OFJE'
  幻想家:
    code_prefix: 'MIDE,MIDEA,MIGD,MIDEF'
  爱丽丝:
    code_prefix: 'IPX,IPZ,IPSD,IPXX'
  # 未匹配以上分类，则命名为未分类
  未分类:
"""


class CategoryHelper:
    """
    二级分类匹配引擎

    配置格式（YAML）：
    movie:
      动画电影:
        genre_ids: '16'
      华语电影:
        original_language: 'zh,cn,bo,za'
      外语电影:           # 无条件 = 兜底分类

    tv:
      国漫:
        genre_ids: '16'
        origin_country: 'CN,TW,HK'
      未分类:
    """

    def __init__(self, yaml_str: str = ""):
        """
        初始化分类引擎

        Args:
            yaml_str: YAML 格式的分类配置字符串。
                      为空时使用默认配置。
        """
        self._categories: dict = {}
        self._movie_categories: dict = {}
        self._tv_categories: dict = {}
        self._av_categories: dict = {}
        self._yaml_str = yaml_str or DEFAULT_CATEGORY_YAML
        self._load()

    def _load(self):
        """解析 YAML 配置"""
        try:
            self._categories = yaml.safe_load(self._yaml_str) or {}
            if self._categories:
                self._movie_categories = self._categories.get("movie", {}) or {}
                self._tv_categories = self._categories.get("tv", {}) or {}
                self._av_categories = self._categories.get("av", {}) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"YAML 配置解析失败: {e}")

    @staticmethod
    def get_category(categories: dict, tmdb_info: dict) -> str:
        """
        根据分类配置和 TMDB 元数据匹配分类。

        匹配逻辑：
        - 按 YAML 中分类的先后顺序遍历，先匹配先返回
        - 同一分类下所有条件必须全部满足（AND 逻辑）
        - 逗号分隔多值，任一匹配即可（OR 逻辑）
        - ! 前缀表示排除该值
        - 无条件的分类作为兜底分类

        Args:
            categories: 分类配置字典
            tmdb_info: TMDB 元数据字典

        Returns:
            匹配到的分类名称（如 "动画电影"），未匹配返回空字符串
        """
        if not tmdb_info or not categories:
            return ""

        for key, item in categories.items():
            if not item:
                # 无条件 = 兜底分类，直接返回
                return key

            match_flag = True
            # 遍历该分类下的所有条件（AND 逻辑：需全部满足）
            for attr, value in item.items():
                if not value:
                    continue

                # 特殊处理 release_year
                if attr == "release_year":
                    info_value = tmdb_info.get("release_date") or tmdb_info.get("first_air_date")
                    if info_value:
                        info_value = str(info_value)[:4]
                else:
                    info_value = tmdb_info.get(attr)

                if not info_value:
                    match_flag = False
                    continue
                elif attr == "production_countries":
                    # 制片国家：从 [{iso_3166_1: "CN", name: "..."}] 提取国家代码
                    info_values = [str(val.get("iso_3166_1")).upper() for val in info_value]
                else:
                    if isinstance(info_value, list):
                        info_values = [str(val).upper() for val in info_value]
                    else:
                        info_values = [str(info_value).upper()]

                # 解析配置值：逗号分隔 + 范围展开 + 排除值
                values = [str(val) for val in str(value).split(",") if val]
                expanded_values = []
                for v in values:
                    if "-" not in v:
                        expanded_values.append(v)
                        continue
                    # 范围展开：如 2020-2025 → [2020, 2021, ..., 2025]
                    value_begin, value_end = v.split("-", 1)
                    prefix = ""
                    if value_begin.startswith("!"):
                        prefix = "!"
                        value_begin = value_begin[1:]
                    if value_begin.isdigit() and value_end.isdigit():
                        expanded_values.extend(
                            f"{prefix}{val}" for val in range(int(value_begin), int(value_end) + 1)
                        )
                    else:
                        expanded_values.extend([f"{prefix}{value_begin}", f"{prefix}{value_end}"])

                values_upper = list(map(str.upper, expanded_values))
                # 分离排除值（! 开头）和正常值
                invert_values = [val[1:] for val in values_upper if val.startswith("!")]
                normal_values = [val for val in values_upper if not val.startswith("!")]

                # 正常值：需有交集（OR 逻辑）
                if normal_values and not set(normal_values).intersection(set(info_values)):
                    match_flag = False
                # 排除值：有交集则不匹配
                if invert_values and set(invert_values).intersection(set(info_values)):
                    match_flag = False

            if match_flag:
                return key

        return ""

    def get_movie_category(self, tmdb_info: dict) -> str:
        """获取电影的二级分类"""
        return self.get_category(self._movie_categories, tmdb_info)

    def get_tv_category(self, tmdb_info: dict) -> str:
        """获取电视剧的二级分类"""
        return self.get_category(self._tv_categories, tmdb_info)

    def get_av_category(self, av_code: str) -> str:
        """
        获取 AV 的二级分类（按番号前缀匹配厂商）。
        av_code 格式如 "SSIS-123"，提取前缀 "SSIS" 进行匹配。
        """
        if not av_code or not self._av_categories:
            return ""
        # 提取番号前缀（- 前面的字母部分）
        prefix = av_code.split("-")[0].upper() if "-" in av_code else av_code[:5].upper()
        for key, item in self._av_categories.items():
            if not item:
                # 无条件 = 兜底分类，直接返回
                return key
            code_prefixes = item.get("code_prefix", "")
            if not code_prefixes:
                continue
            prefixes = [p.strip().upper() for p in str(code_prefixes).split(",") if p.strip()]
            if prefix in prefixes:
                return key
        return ""

    @property
    def movie_categories(self) -> dict:
        return self._movie_categories

    @property
    def tv_categories(self) -> dict:
        return self._tv_categories

    @property
    def av_categories(self) -> dict:
        return self._av_categories
