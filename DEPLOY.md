# 部署指南(Docker Compose)

## 镜像从哪来

每次推送到 `main`(或打 `v*` 标签)时,GitHub Actions 自动跑测试门禁并构建多架构镜像(amd64 / arm64),发布到:

```
ghcr.io/daisyyijin/strmbrain
```

> **首次拉取前必看**:GitHub Packages 首次发布的包默认是**私有**的。到仓库页 → Packages → 对应镜像 → Package settings → 改为 **Public** 之后才能匿名 `docker pull`;保持私有也能用,拉取前先 `docker login ghcr.io`(用户名是你的 GitHub 用户名,密码用有 `read:packages` 权限的 Personal Access Token)。

镜像 tag 规则:`latest` = main 最新;`main-YYYYMMDD` 形态为分支构建;打了 `v*` 标签后另有 `v1.2.3` 与 `v1.2` 形式的版本 tag。

## 快速开始

新建一个目录,写入 `docker-compose.yml`(仓库根目录也附带一份):

```yaml
services:
  station:
    image: ghcr.io/daisyyijin/strmbrain:latest
    container_name: 115-station
    restart: unless-stopped
    stop_grace_period: 20s   # 优雅退出窗口:SIGTERM 后程序需要 ~3 秒收尾(停 worker、冲刷通知)
    ports:
      - "6060:6060"   # 管理后台
      - "6086:6086"   # 302 直链代理
    volumes:
      - ./config:/config
      - ./data:/data
      - ./media:/media
      - ./logs:/logs
    environment:
      - TZ=Asia/Shanghai
      - AUTH_USER=admin              # 管理后台账号(环境变量管理,网页注册已移除)
      - AUTH_PASSWORD=change-me      # 改成强密码后 up -d 重启生效
      # - ACCESS_TOKEN_EXPIRE_MINUTES=11520  # 登录有效期(分钟),默认 8 天
      # - AUTH_OTP_RESET=true        # 丢了二步验证器时临时加上重启一次,登录后去掉
```

启动:

```bash
docker compose up -d
docker compose logs -f          # 看启动日志
```

打开 `http://服务器IP:6060`,用 `AUTH_USER` / `AUTH_PASSWORD` 登录,先到「账号管理」扫码登录 115。

## 本地源码构建(不拉镜像)

```bash
git clone https://github.com/DaisyYijin/StrmBrain.git
cd StrmBrain
docker compose up -d --build    # 把 image 换成 build: . 也可以
```

## 端口、目录与环境变量

| 项 | 说明 |
|---|---|
| `6060` | 管理后台(Web UI) |
| `6086` | 302 直链代理(播放器直连 115 CDN,服务器不转发流量) |
| `./config` | 数据库与配置(**最重要**,备份主要备它) |
| `./data` | 程序数据(整理台账、缓存) |
| `./media` | STRM / NFO / 海报输出目录,挂给 Emby / Jellyfin 的就是它 |
| `./logs` | 运行日志 |
| `TZ` | 时区,影响定时任务(cron)与日志时间 |
| `AUTH_USER` / `AUTH_PASSWORD` | 后台登录账号密码,改动后 `up -d` 重启生效 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 登录有效期,默认 8 天(同 MoviePilot) |
| `AUTH_OTP_RESET` | 临时加上重启一次可重置二步验证,登录后去掉 |

## MetaTube(可选,AV 整理的元数据源)

AV 整理不配置 MetaTube 也能用(纯番号识别、分类、按番号重命名);配置后可刮到真实标题、演员、厂牌与封面。

在同一个 `docker-compose.yml` 里加一个服务:

```yaml
  metatube:
    image: ghcr.io/metatube-community/metatube-server:latest
    container_name: metatube
    restart: unless-stopped
    ports:
      - "8080:8080"
    # environment:
    #   - MT_TOKEN=change-me-token   # 可选:设了 token 本站配置里要填同一个
```

`docker compose up -d` 后,到本站「设置 → MetaTube」:

- 服务器地址:同 compose 网络直接填 `http://metatube:8080`(跨机填实际 IP)
- Token:metatube 设了 `MT_TOKEN` 就填同一个,没设留空
- 点「测试连接」,显示刮削源数量即成功,记得打开「启用识别」并保存

Emby / Jellyfin 侧的入库与刮削说明见 [USAGE.md](USAGE.md)。

## 升级与备份

```bash
docker compose pull && docker compose up -d    # 升级到最新镜像
```

- 数据全在四个挂载目录里,备份就是备份目录(优先 `./config`,数据库在里面)
- 升级不会动挂载目录;回退版本把 `image` 的 `latest` 换成具体版本 tag 再 `up -d`
- Emby 侧的 STRM 路径不变时,升级后无需重新刮削
