"""
封面合成服务 - 使用 Pillow 生成精美的媒体库封面

支持多种预设风格：
- 红色动漫风（样式一）：左标题+右侧倾斜层叠海报
- 深蓝星空风（样式二）：顶部标题+底部水平等分海报
- 深蓝单图风（样式三）：左标题+右侧单张大图
- 随机（混合）
"""
import io
import math
import random
import asyncio
from typing import Optional

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from app.core.logbuffer import get_logger

logger = get_logger()

# 输出封面尺寸（Emby 媒体库封面推荐 16:9）
COVER_WIDTH = 1920
COVER_HEIGHT = 1080

# ===== 字体加载 =====

def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """获取字体，优先使用系统中文字体"""
    font_candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simfang.ttf",
        "C:/Windows/Fonts/simkai.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ]
    for fp in font_candidates:
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _draw_text_with_stroke(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    stroke_width: int = 3,
    stroke_fill: tuple[int, int, int] = (0, 0, 0),
):
    """绘制带描边的文字（使用 PIL 内置描边）"""
    x, y = xy
    try:
        draw.text(
            (x, y), text, font=font, fill=fill,
            stroke_width=stroke_width, stroke_fill=stroke_fill,
        )
    except TypeError:
        draw.text((x + 2, y + 2), text, font=font, fill=stroke_fill)
        draw.text((x, y), text, font=font, fill=fill)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        return len(text) * 20, 30


# ===== 基础图像工具 =====

def _round_corner_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def _apply_round_corners(img: Image.Image, radius: int) -> Image.Image:
    if radius <= 0:
        return img.convert("RGBA")
    mask = _round_corner_mask(img.size, radius)
    result = Image.new("RGBA", img.size, (0, 0, 0, 0))
    result.paste(img, (0, 0), mask)
    return result


def _resize_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """按 cover 方式缩放裁剪（保持比例，裁掉多余部分）"""
    src_w, src_h = img.size
    if src_w == 0 or src_h == 0:
        return img.resize((target_w, target_h), Image.LANCZOS)

    src_ratio = src_w / src_h
    target_ratio = target_w / target_h

    if src_ratio > target_ratio:
        new_h = target_h
        new_w = int(new_h * src_ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - target_w) // 2
        img = img.crop((left, 0, left + target_w, target_h))
    else:
        new_w = target_w
        new_h = int(new_w / src_ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        top = (new_h - target_h) // 2
        img = img.crop((0, top, target_w, top + target_h))
    return img


def _create_shadow(w: int, h: int, radius: int) -> tuple[Image.Image, int]:
    pad = 26
    sw = w + pad * 2
    sh = h + pad * 2
    shadow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(shadow)
    draw.rounded_rectangle(
        [pad, pad, pad + w - 1, pad + h - 1],
        radius=radius,
        fill=(0, 0, 0, 160),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    return shadow, pad


def _enhance_image(img: Image.Image) -> Image.Image:
    """微调图片：增强对比度和饱和度"""
    try:
        from PIL import ImageEnhance
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(1.08)
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(1.05)
    except Exception:
        pass
    return img


# ===== 网络下载 =====

async def _download_image(url: str, timeout: float = 15.0) -> Optional[Image.Image]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            img = Image.open(io.BytesIO(resp.content))
            if img.mode != "RGB":
                img = img.convert("RGB")
            return img
    except Exception as e:
        logger.warning(f"下载封面图片失败 {url[:80]}: {e}")
        return None


async def _download_images_concurrent(urls: list[str]) -> list[Optional[Image.Image]]:
    tasks = [_download_image(url) for url in urls]
    return list(await asyncio.gather(*tasks))


# ===== 背景生成 =====

def _gradient_bg(w: int, h: int, color1: tuple[int, int, int], color2: tuple[int, int, int],
                 direction: str = "diagonal") -> Image.Image:
    """生成线性渐变背景"""
    bg = Image.new("RGB", (w, h), color1)
    px = bg.load()
    if direction == "diagonal":
        max_d = w + h
        for y in range(h):
            for x in range(w):
                t = (x + y) / max_d
                r = int(color1[0] * (1 - t) + color2[0] * t)
                g = int(color1[1] * (1 - t) + color2[1] * t)
                b = int(color1[2] * (1 - t) + color2[2] * t)
                px[x, y] = (r, g, b)
    elif direction == "vertical":
        for y in range(h):
            t = y / h
            r = int(color1[0] * (1 - t) + color2[0] * t)
            g = int(color1[1] * (1 - t) + color2[1] * t)
            b = int(color1[2] * (1 - t) + color2[2] * t)
            for x in range(w):
                px[x, y] = (r, g, b)
    return bg


def _radial_bg(w: int, h: int, center_color: tuple[int, int, int],
               edge_color: tuple[int, int, int]) -> Image.Image:
    """径向渐变背景（中心亮、边缘暗）"""
    try:
        import numpy as np
        y_coords, x_coords = np.ogrid[:h, :w]
        cx, cy = w // 2, h // 2
        max_d = math.sqrt(cx * cx + cy * cy)
        d = np.sqrt((x_coords - cx) ** 2 + (y_coords - cy) ** 2)
        t = (d / max_d).clip(0, 1)
        r = (center_color[0] * (1 - t) + edge_color[0] * t).astype(np.uint8)
        g = (center_color[1] * (1 - t) + edge_color[1] * t).astype(np.uint8)
        b = (center_color[2] * (1 - t) + edge_color[2] * t).astype(np.uint8)
        return Image.fromarray(np.stack([r, g, b], axis=-1), "RGB")
    except ImportError:
        return _gradient_bg(w, h, center_color, edge_color, "diagonal")


# ===== 样式一：红色动漫风（左标题+右侧倾斜层叠海报） =====

def _render_style_anime(
    images: list[Image.Image],
    title: str,
    subtitle: str = "",
) -> Image.Image:
    """左标题 + 右侧倾斜层叠海报"""
    w, h = COVER_WIDTH, COVER_HEIGHT
    # 红色渐变背景
    bg = _gradient_bg(w, h, (196, 30, 58), (139, 0, 0), "diagonal")
    canvas = bg.convert("RGBA")

    # 右侧倾斜层叠 5-6 张海报
    n = min(6, len(images))
    if n == 0:
        return canvas.convert("RGB")

    # 海报区域：右侧约 62% 宽
    region_left = int(w * 0.38)
    region_right = w
    region_w = region_right - region_left
    region_top = int(h * 0.08)
    region_bottom = int(h * 0.92)
    region_h = region_bottom - region_top

    # 每张海报尺寸（纵向，2:3 比例）
    poster_h = int(region_h * 0.78)
    poster_w = int(poster_h / 1.45)

    # 倾斜角度
    angle = 18  # 顺时针倾斜度数
    # 横向位置
    step_x = int((region_w - poster_w * 0.7) / max(n - 1, 1))
    center_y = (region_top + region_bottom) // 2

    for i, img in enumerate(images[:n]):
        # 缩放到目标尺寸
        resized = _resize_cover(img, poster_w, poster_h)
        # 圆角
        rounded = _apply_round_corners(resized, 12)
        # 倾斜
        rotated = rounded.rotate(-angle, resample=Image.BICUBIC, expand=True)

        # 阴影
        shadow, pad = _create_shadow(poster_w, poster_h, 12)
        shadow_rotated = shadow.rotate(-angle, resample=Image.BICUBIC, expand=True)
        sw, sh = shadow_rotated.size

        # 位置
        x = region_left + i * step_x
        y = center_y - rotated.size[1] // 2 + (i % 2) * 30 - 30

        # 粘贴阴影
        canvas.paste(shadow_rotated, (x - pad + 8, y - pad + 12), shadow_rotated)
        # 粘贴海报
        canvas.paste(rotated, (x, y), rotated)

    # 左侧标题
    draw = ImageDraw.Draw(canvas)
    # 主标题
    font_title = _get_font(110, bold=True)
    title_x = 80
    title_y = int(h * 0.40)
    _draw_text_with_stroke(draw, (title_x, title_y), title, font_title,
                           (255, 255, 255), stroke_width=5, stroke_fill=(0, 0, 0))
    # 副标题（英文）
    if subtitle:
        font_sub = _get_font(46)
        sub_y = title_y + 130
        _draw_text_with_stroke(draw, (title_x, sub_y), subtitle, font_sub,
                               (255, 215, 0), stroke_width=3, stroke_fill=(80, 30, 0))

    return canvas.convert("RGB")


# ===== 样式二：深蓝星空风（顶部标题+底部水平等分海报） =====

def _render_style_starfield(
    images: list[Image.Image],
    title: str,
    subtitle: str = "",
) -> Image.Image:
    """顶部标题 + 底部水平等分海报 + 星空背景"""
    w, h = COVER_WIDTH, COVER_HEIGHT
    # 星空背景：深紫蓝到深蓝
    bg = _radial_bg(w, h, (40, 30, 80), (10, 8, 25))

    # 加点星点装饰
    canvas = bg.convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    rng = random.Random(42)
    for _ in range(120):
        sx = rng.randint(0, w - 1)
        sy = rng.randint(0, int(h * 0.55))
        sr = rng.choice([1, 1, 1, 2, 2, 3])
        sa = rng.randint(80, 200)
        draw.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 255, 255, sa))

    # 顶部英雄区（顶部半透明星云渐变）
    hero_h = int(h * 0.45)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for y in range(hero_h):
        t = y / hero_h
        a = int(120 * (1 - t))
        od.line([(0, y), (w, y)], fill=(60, 40, 110, a))
    canvas = Image.alpha_composite(canvas, overlay)

    # 底部海报区
    n = min(5, len(images))
    if n > 0:
        poster_area_top = int(h * 0.50)
        poster_area_bottom = int(h * 0.92)
        poster_area_h = poster_area_bottom - poster_area_top
        # 5 等分
        gap = 16
        cell_w = (w - gap * (n + 1)) // n
        # 海报比例 2:3
        cell_h = int(cell_w * 1.45)
        if cell_h > poster_area_h:
            cell_h = poster_area_h
            cell_w = int(cell_h / 1.45)

        for i, img in enumerate(images[:n]):
            x = gap + i * (cell_w + gap)
            y = poster_area_top + (poster_area_h - cell_h) // 2

            resized = _resize_cover(img, cell_w, cell_h)
            rounded = _apply_round_corners(resized, 10)

            # 阴影
            shadow, pad = _create_shadow(cell_w, cell_h, 10)
            canvas.paste(shadow, (x - pad + 5, y - pad + 8), shadow)
            # 描边
            bordered = Image.new("RGBA", (cell_w + 6, cell_h + 6), (255, 255, 255, 255))
            bordered.paste(rounded, (3, 3), rounded)
            canvas.paste(bordered, (x, y), bordered)

    # 顶部标题
    draw = ImageDraw.Draw(canvas)
    font_title = _get_font(120, bold=True)
    title_x = 80
    title_y = 100
    _draw_text_with_stroke(draw, (title_x, title_y), title, font_title,
                           (255, 255, 255), stroke_width=5, stroke_fill=(10, 10, 40))
    if subtitle:
        font_sub = _get_font(48)
        sub_y = title_y + 140
        _draw_text_with_stroke(draw, (title_x, sub_y), subtitle, font_sub,
                               (180, 200, 230), stroke_width=3, stroke_fill=(10, 10, 40))

    return canvas.convert("RGB")


# ===== 样式三：深蓝单图风（左标题+右侧单张大图） =====

def _render_style_featured(
    images: list[Image.Image],
    title: str,
    subtitle: str = "",
) -> Image.Image:
    """左标题 + 右侧单张大图（极简左右分割）"""
    w, h = COVER_WIDTH, COVER_HEIGHT
    # 深蓝纯色背景
    bg = _gradient_bg(w, h, (26, 58, 92), (15, 35, 60), "diagonal")
    canvas = bg.convert("RGBA")

    # 右侧大图
    if images:
        main = images[0]
        # 右侧 55% 宽
        target_w = int(w * 0.55)
        target_h = int(h * 0.78)
        # 保持海报比例
        sw_, sh_ = main.size
        ratio = sw_ / sh_
        if target_w / target_h > ratio:
            cell_h = target_h
            cell_w = int(cell_h * ratio)
        else:
            cell_w = target_w
            cell_h = int(cell_w / ratio)

        resized = _resize_cover(main, cell_w, cell_h)
        rounded = _apply_round_corners(resized, 14)

        x = int(w * 0.42) + (target_w - cell_w) // 2
        y = (h - cell_h) // 2

        # 阴影
        shadow, pad = _create_shadow(cell_w, cell_h, 14)
        canvas.paste(shadow, (x - pad + 10, y - pad + 14), shadow)
        # 描边
        bordered = Image.new("RGBA", (cell_w + 8, cell_h + 8), (255, 255, 255, 240))
        bordered.paste(rounded, (4, 4), rounded)
        canvas.paste(bordered, (x, y), bordered)

    # 左侧标题
    draw = ImageDraw.Draw(canvas)
    font_title = _get_font(120, bold=True)
    title_x = 80
    title_y = int(h * 0.42)
    _draw_text_with_stroke(draw, (title_x, title_y), title, font_title,
                           (255, 255, 255), stroke_width=5, stroke_fill=(10, 20, 40))
    if subtitle:
        font_sub = _get_font(46)
        sub_y = title_y + 130
        _draw_text_with_stroke(draw, (title_x, sub_y), subtitle, font_sub,
                               (200, 220, 240), stroke_width=3, stroke_fill=(10, 20, 40))

    return canvas.convert("RGB")


# ===== 英文副标题生成 =====

def _zh_to_en_subtitle(title: str) -> str:
    """根据中文标题生成英文副标题"""
    # 常见关键词映射
    mapping = {
        "动漫": "ANIME",
        "动画": "ANIME",
        "电影": "MOVIE",
        "华语": "CN",
        "中文": "CN",
        "国语": "CN",
        "国产": "CN",
        "漫威": "MARVEL",
        "DC": "DC",
        "迪士尼": "DISNEY",
        "皮克斯": "PIXAR",
        "日剧": "JP DRAMA",
        "韩剧": "KR DRAMA",
        "美剧": "US DRAMA",
        "英剧": "UK DRAMA",
        "剧集": "DRAMA",
        "综艺": "VARIETY",
        "纪录": "DOC",
        "纪录片": "DOC",
    }
    upper = title.upper()
    for k, v in mapping.items():
        if k in title:
            return v + " " + ("MOVIE" if "电影" in title or "剧" not in title else "DRAMA")
    return ""


# ===== 通用合成接口 =====

def get_sort_label(sort_by: str, sort_order: str) -> str:
    """获取排序方式的中文标签"""
    labels = {
        "PremiereDate": "发行日期",
        "SortName": "标题",
        "DateCreated": "加入日期",
        "CommunityRating": "TMDB 评分",
        "Random": "随机",
    }
    order_label = "降序" if sort_order == "Descending" else "升序"
    base = labels.get(sort_by, sort_by)
    return f"按 {base} 排序（{order_label}）"


def render_cover(
    style: str,
    images: list[Image.Image],
    title: str,
    subtitle: str = "",
) -> Image.Image:
    """
    根据样式渲染封面
    style: "anime" / "starfield" / "featured" / "random"
    """
    if style == "random":
        style = random.choice(["anime", "starfield", "featured"])
    if style == "anime":
        return _render_style_anime(images, title, subtitle)
    elif style == "starfield":
        return _render_style_starfield(images, title, subtitle)
    elif style == "featured":
        return _render_style_featured(images, title, subtitle)
    else:
        return _render_style_featured(images, title, subtitle)


async def generate_library_cover(
    items: list[dict],
    title: str = "",
    subtitle: str = "",
    style: str = "anime",
    sort_by: str = "DateCreated",
    sort_order: str = "Descending",
) -> tuple[Optional[bytes], list[dict]]:
    """
    生成精美的媒体库封面（新版：支持多种风格）

    Args:
        items: 条目列表（需含 image_url, name, year）
        title: 封面主标题（媒体库名）
        subtitle: 副标题
        style: 封面样式 (anime / starfield / featured / random)
        sort_by, sort_order: 排序方式（已在外层应用）

    Returns:
        (JPEG 图片 bytes, 实际使用的条目列表)
    """
    if not items:
        return None, []

    # 决定需要下载多少张
    need_count = {"anime": 6, "starfield": 5, "featured": 1, "random": 6}.get(style, 6)
    if style == "random":
        need_count = 6

    # 下载
    urls = [it["image_url"] for it in items[:need_count * 2]]
    images = await _download_images_concurrent(urls)
    valid = [img for img in images if img is not None]
    if not valid:
        return None, []

    # 提升图片质量
    valid = [_enhance_image(img) for img in valid]

    # 不够时循环填充
    while len(valid) < need_count:
        valid.append(valid[len(valid) % len(valid)].copy())

    if style == "featured":
        used = valid[:1]
    elif style == "starfield":
        used = valid[:5]
    else:  # anime / random
        used = valid[:6]

    # 合成
    canvas = render_cover(style, used, title or "", subtitle or "")
    # 输出
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=92, subsampling=0)
    return out.getvalue(), used


# ============ 兼容旧版 generate_library_cover 签名 ============

async def generate_library_cover_legacy(
    items: list[dict],
    title: str = "",
    sort_label: str = "",
    layout: str = "3x3",
) -> tuple[Optional[bytes], list[dict]]:
    """旧版布局（3x3/2x2/4x3/1x5）封面，保持向后兼容"""
    if not items:
        return None, []

    layout_map = {"3x3": (3, 3, 9), "2x2": (2, 2, 4), "4x3": (4, 3, 12), "1x5": (5, 1, 5)}
    cols, rows, target_count = layout_map.get(layout, (3, 3, 9))

    urls = [it["image_url"] for it in items[:target_count * 2]]
    images = await _download_images_concurrent(urls)
    downloaded = []
    for i, img in enumerate(images):
        if img and len(downloaded) < target_count:
            downloaded.append({
                "image": _enhance_image(img),
                "name": items[i].get("name", ""),
                "year": items[i].get("year", ""),
            })
    if not downloaded:
        return None, []
    while len(downloaded) < target_count:
        src = downloaded[len(downloaded) % len(downloaded)]
        downloaded.append({**src, "image": src["image"].copy()})
    downloaded = downloaded[:target_count]

    # 简单网格拼图
    w, h = COVER_WIDTH, COVER_HEIGHT
    gap = 10
    cell_w = (w - gap * (cols + 1)) // cols
    cell_h = (h - gap * (rows + 1)) // rows

    canvas = _gradient_bg(w, h, (20, 20, 28), (8, 8, 16)).convert("RGBA")
    for idx, item in enumerate(downloaded):
        r, c = idx // cols, idx % cols
        x = gap + c * (cell_w + gap)
        y = gap + r * (cell_h + gap)
        resized = _resize_cover(item["image"], cell_w, cell_h)
        rounded = _apply_round_corners(resized, 8)
        shadow, pad = _create_shadow(cell_w, cell_h, 8)
        canvas.paste(shadow, (x - pad + 5, y - pad + 8), shadow)
        canvas.paste(rounded, (x, y), rounded)
    canvas = canvas.convert("RGB")

    if title:
        draw = ImageDraw.Draw(canvas)
        font_large = _get_font(80, bold=True)
        tw, th = _text_size(draw, title, font_large)
        tx = (w - tw) // 2
        ty = h - 120
        _draw_text_with_stroke(draw, (tx, ty), title, font_large,
                               (255, 255, 255), stroke_width=4, stroke_fill=(0, 0, 0))
    if sort_label:
        draw = ImageDraw.Draw(canvas)
        font_small = _get_font(32)
        sw_, _ = _text_size(draw, sort_label, font_small)
        sx = (w - sw_) // 2
        sy = h - 60
        _draw_text_with_stroke(draw, (sx, sy), sort_label, font_small,
                               (200, 200, 210), stroke_width=2, stroke_fill=(0, 0, 0))

    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=92, subsampling=0)
    return out.getvalue(), downloaded


# ===== 样式预览缩略图（用于前端展示）=====

def render_style_preview(style: str) -> bytes:
    """生成样式预览缩略图（前端 2x2 卡片用）"""
    w, h = 320, 180
    if style == "anime":
        bg = _gradient_bg(w, h, (196, 30, 58), (139, 0, 0), "diagonal")
    elif style == "starfield":
        bg = _radial_bg(w, h, (40, 30, 80), (10, 8, 25))
        # 加几个星点
        canvas = bg.convert("RGBA")
        d = ImageDraw.Draw(canvas)
        rng = random.Random(7)
        for _ in range(20):
            sx, sy = rng.randint(0, w - 1), rng.randint(0, h - 1)
            d.ellipse([sx - 1, sy - 1, sx + 1, sy + 1], fill=(255, 255, 255, 180))
        bg = canvas.convert("RGB")
    elif style == "featured":
        bg = _gradient_bg(w, h, (26, 58, 92), (15, 35, 60), "diagonal")
    else:
        # 随机：三色拼接
        bw = w // 3
        bg = Image.new("RGB", (w, h), (0, 0, 0))
        bg.paste(_gradient_bg(bw, h, (196, 30, 58), (139, 0, 0), "diagonal"), (0, 0))
        bg.paste(_gradient_bg(bw, h, (40, 30, 80), (10, 8, 25), "diagonal"), (bw, 0))
        bg.paste(_gradient_bg(bw, h, (26, 58, 92), (15, 35, 60), "diagonal"), (bw * 2, 0))

    # 加一些装饰（小白条/小白点表示海报位置）
    canvas = bg.convert("RGBA")
    d = ImageDraw.Draw(canvas)
    if style == "anime":
        # 右侧倾斜小白条
        for i in range(4):
            x = int(w * 0.45) + i * 22
            d.rectangle([x, int(h * 0.18), x + 22, int(h * 0.82)], fill=(255, 255, 255, 220))
        # 左标题
        d.text((14, int(h * 0.42)), "标题", fill=(255, 255, 255))
        d.text((14, int(h * 0.42) + 22), "EN", fill=(255, 215, 0))
    elif style == "starfield":
        # 顶部标题
        d.text((14, 16), "标题", fill=(255, 255, 255))
        # 底部 5 个白条
        for i in range(5):
            x = 8 + i * (w - 16) // 5
            d.rectangle([x, int(h * 0.55), x + (w - 40) // 5, int(h * 0.92)], fill=(255, 255, 255, 220))
    elif style == "featured":
        # 右侧大图块
        d.rounded_rectangle(
            [int(w * 0.42), int(h * 0.12), int(w * 0.95), int(h * 0.88)],
            radius=6, fill=(255, 255, 255, 230),
        )
        d.text((14, int(h * 0.42)), "标题", fill=(255, 255, 255))
    else:
        # 随机：分成 3 列不同色
        d.text((10, 12), "随", fill=(255, 255, 255))
        d.text((w - 30, h - 28), "机", fill=(255, 255, 255))

    canvas = canvas.convert("RGB")
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=85)
    return out.getvalue()


# 兼容旧版保留的接口名（用 anime 风格作为默认）
async def _compat_old(items, title="", sort_label="", layout="3x3"):
    return await generate_library_cover_legacy(items, title=title, sort_label=sort_label, layout=layout)
