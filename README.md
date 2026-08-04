# STRMhub

115 网盘 STRM 生成与影视整理工具，基于 Emby 302 重定向实现云盘原画播放。

## 核心功能

### 转存下载

合并离线下载与分享转存为单一输入框，自动识别链接类型：115 分享链接获取文件列表后转存，磁力/HTTP/FTP/电驴链接直接添加为离线下载任务。转存成功后自动触发影视整理。

### 影视整理

扫描 115 网盘源目录，通过 TMDB 匹配影视信息，按电影/电视剧/AV 自动分类移动。支持二级分类自定义根目录、重命名规则（完整/常规/精简三档预设）、洗版策略（SHA1 去重 + 分辨率/编码多维比较）、AI 辅助识别（TMDB 匹配失败时用 AI 提取标题重新搜索）。整理完成后非视频文件自动归入冗余目录，源目录自动清理空文件夹。

### 同步归档

- **全量同步**：扫描网盘目录，为视频文件生成 `.strm` 文件（内容指向本服务 302 跳转接口），图片和 NFO/字幕等元数据直接下载到本地
- **增量同步**：通过清单文件（file_id + SHA1）对比网盘与本地差异，仅处理新增/变更/移动/删除的文件，支持 Cron 定时执行
- **上传同步**：将 Emby 刮削产生的 NFO/图片/字幕上传回 115 网盘

### Emby 集成

STRM 文件通过本服务 302 重定向实时获取 115 直链，播放时直接使用网盘带宽，无需下载到本地。同步完成后自动触发 Emby 媒体库刷新，刮削后将元数据上传回网盘。

### 特色工具

- **媒体库封面生成**：为 Emby 所有媒体库自动生成拼图封面
- **115 文件夹清空**：定时清理网盘指定文件夹

## 部署

### Docker Compose（推荐）

```yaml
services:
  strmhub:
    image: ghcr.io/daisyyijin/strmhub:latest
    container_name: strmhub
    restart: unless-stopped
    ports:
      - "6060:6060"
    volumes:
      - strmhub_data:/app/data       # 账号、Cookie、同步清单、备份
      - strmhub_config:/app/config   # 设置、管理员账号、同步计划
      - strmhub_log:/app/log         # 运行日志
      - strmhub_media:/media         # STRM 文件输出目录
    environment:
      - TZ=Asia/Shanghai

volumes:
  strmhub_data:
  strmhub_config:
  strmhub_log:
  strmhub_media:
```

```bash
docker compose up -d
```

访问 `http://服务器IP:6060`，默认账号 `admin` / `admin123`。

### 使用流程

1. **核心配置** → 扫码登录 115 账号，配置 Emby/TMDB/STRM 服务器地址
2. **转存下载** → 粘贴链接，自动转存到网盘并触发整理
3. **整理功能** → 配置整理目录、分类规则、重命名规则、洗版策略
4. **同步归档** → 执行全量同步生成 STRM 文件，设置增量同步定时计划
5. **通知配置** → 按需配置企业微信/Telegram/QQ 通知

## 技术栈

Python 3.12 + FastAPI + Vue 3 (CDN) + Docker，支持 amd64/arm64 双架构。
