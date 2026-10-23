package api

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

// dashPosterSize 仪表盘海报的默认尺寸。海报墙最宽一百多像素，w342 给高分屏留足两倍；
// 原来一律拉 w500，一张七八十 KB。更小的位置（最近整理 36px、拼图格子）由前端带 size 要更小的图
const dashPosterSize = "w342"

// avCoverClient 拉 AV 封面专用客户端：连接层只放行公网地址。
// /poster 端点无鉴权（仪表盘/门户海报 <img> 带不了登录态），封面 URL 又是
// 刮削源给的完整 http(s) 地址，不加防护会被当成内网 SSRF 跳板
var avCoverClient = &http.Client{
	Timeout: 15 * time.Second,
	Transport: &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			host, _, err := net.SplitHostPort(addr)
			if err != nil {
				return nil, err
			}
			if ip := net.ParseIP(host); ip != nil && !ipIsPublicIP(ip) {
				return nil, fmt.Errorf("SSRF 防护：拒绝非公网地址 %s", host)
			}
			d := net.Dialer{Timeout: 10 * time.Second}
			return d.DialContext(ctx, network, addr)
		},
	},
}

// ipIsPublicIP 只放行公网单播地址（回环/私网/链路本地/未指定一律拒绝）
func ipIsPublicIP(ip net.IP) bool {
	return ip.IsGlobalUnicast() && !ip.IsPrivate() && !ip.IsLoopback() && !ip.IsLinkLocalUnicast()
}

// avCoverGet 走 SSRF 客户端拉封面字节。imgGet 走共享连接池、无地址校验，AV 封面不用它
func avCoverGet(u string) ([]byte, error) {
	resp, err := avCoverClient.Get(u)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, imgMaxBytes))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK || len(body) < 100 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return body, nil
}

// serveTMDBPoster GET /poster/*path[?size=w154] 为仪表盘代理 TMDB 海报，避免客户端直连 TMDB。缓存与回退见 imgcache.go
func serveTMDBPoster(c *gin.Context, dataDir string) {
	p := c.Param("path")
	if !validTMDBPath(p) {
		// AV 封面（MetaTube 刮削结果，MediaLibrary.PosterPath = "/av:<完整URL>"）：
		// 直接代理原始封面 URL，走 imgcache 通用缓存（7 天）与失败记忆；
		// 只缓存不写媒体目录；URL 校验 + 连接层公网校验双保险
		if coverURL, ok := avCoverPath(p); ok {
			data, err := cachedImage(dataDir, "av", coverURL, 7*24*time.Hour, func() ([]byte, error) {
				return avCoverGet(coverURL)
			})
			if err != nil {
				imgMissing(c)
				return
			}
			serveImage(c, data, "public, max-age=604800")
			return
		}
		c.String(http.StatusBadRequest, "bad path")
		return
	}
	size := c.DefaultQuery("size", dashPosterSize)
	if !tmdbImageSizes[size] {
		size = dashPosterSize
	}
	data, err := cachedTMDBImage(dataDir, size, p)
	if err != nil {
		imgMissing(c)
		return
	}
	serveImage(c, data, tmdbImgCacheControl)
}

// avCoverPath 识别 "/av:<完整URL>" 形态的封面路径；只放行带 host 的 http(s) URL
func avCoverPath(p string) (string, bool) {
	if !strings.HasPrefix(p, "/av:") {
		return "", false
	}
	coverURL := strings.TrimPrefix(p, "/av:")
	u, err := url.Parse(coverURL)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return "", false
	}
	return coverURL, true
}
