package api

// GitHub 访问：全局代理 + 可选 Token（setting "github" {token}，「系统配置 → 代理」里填）。
//
// 国内直连 github.com / api.github.com 经常超时；匿名访问 API 每小时只给 60 次，
// 下载 release 资产也会被限流。更新检测与 RE0 浏览器下载都走这里。
// Token 只加在 github.com / api.github.com 上：资产下载会 302 到 objects.githubusercontent.com，
// Go 的 http.Client 跨域跳转时自己会去掉 Authorization，不会把 Token 带到别的域名。

import (
	"encoding/json"
	"net/http"
	"strings"
	"time"
)

// githubToken 用户配的 GitHub Token（可空）
func githubToken() string {
	var cfg struct {
		Token string `json:"token"`
	}
	json.Unmarshal([]byte(settingValueCompat("github")), &cfg)
	return strings.TrimSpace(cfg.Token)
}

// githubAuth 有 Token 就带上（只对 GitHub 自己的域名）
func githubAuth(req *http.Request) {
	host := req.URL.Hostname()
	if host != "github.com" && host != "api.github.com" {
		return
	}
	if t := githubToken(); t != "" {
		req.Header.Set("Authorization", "Bearer "+t)
	}
}

// githubHTTPClient 访问 GitHub 用的客户端：配了全局代理就走代理。timeout=0 不限总时长（下载大文件用）
func githubHTTPClient(timeout time.Duration) *http.Client {
	tr := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		TLSHandshakeTimeout:   20 * time.Second,
		ResponseHeaderTimeout: 60 * time.Second,
		IdleConnTimeout:       90 * time.Second,
	}
	if pu := getProxyURL(); pu != "" {
		if p, err := parseProxyURL(pu); err == nil {
			tr.Proxy = p
		}
	}
	return &http.Client{Timeout: timeout, Transport: tr}
}
