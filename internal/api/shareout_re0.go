package api

// RE0 发布分享（shareout.go 调）。两条通道，和查资源 / 解锁一样：已授权 OpenAPI 的优先，否则网页登录。
//
//   - OpenAPI：POST /api/open/shares（scope write，re0Scope 早就申请了）。字段与枚举照官方文档，
//     枚举值另见 p115strmhelper `helper/hdhive/open/client.py`。
//   - 网页：站点详情页「分享资源」弹窗提交的是普通 REST —— POST /api/customer/resources（2026-10-17 读站点前端
//     static/chunks/8539-*.js 得到的，不是 Server Action）。字段和 OpenAPI 基本一样，只是影视关联用站内 id（movie_id / tv_id），
//     不收 TMDB 编号。登录态 JWT 在 Cookie 里，站点的请求客户端会把 Cookie `csrf_access_token` 放进 X-CSRF-TOKEN 请求头，
//     自己 fetch 时要照做。站内 id 从 /tmdb/{类型}/{编号} 跳转后的地址里取（/movie/{id}、/tv/{id}）。

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/go-rod/rod"
)

// re0ShareReady RE0 现在能不能发分享（空 = 能），界面提示用
func re0ShareReady(cfg *re0Cfg) string {
	switch re0Channel(cfg) {
	case "openapi":
		if !cfg.grantedScopes()["write"] {
			return "OpenAPI 授权里没有 write 权限，请重新授权一次"
		}
		return ""
	case "web":
		return ""
	}
	return "还没配置 RE0（网页登录或 OpenAPI）"
}

// re0ShareBody 两条通道共用的请求体（字段名照 OpenAPI）；网页通道把 tmdb_id + media_type 换成站内 id
func re0ShareBody(pl shareOutPayload, cfg shareOutSiteCfg) map[string]any {
	body := map[string]any{
		"url":       pl.URL,
		"title":     shareOutHeadline(pl),
		"hide_link": true,
	}
	if r := shareOutRemark(pl); r != "" {
		body["remark"] = r
	}
	if pl.Code != "" {
		body["access_code"] = pl.Code
	}
	if s := humanSize(pl.SizeBytes); s != "" {
		body["share_size"] = s
	}
	if r := re0ResolutionOf(pl.Info.Pix); r != "" {
		body["video_resolution"] = []string{r}
	}
	if s := re0SourceOf(pl.Info, pl.Ext); s != "" {
		body["source"] = []string{s}
	}
	if pl.HasSubs {
		body["subtitle_type"] = []string{"外挂"}
		if len(pl.SubLangs) > 0 {
			body["subtitle_language"] = pl.SubLangs
		}
	}
	if cfg.UnlockPoints > 0 {
		body["unlock_points"] = cfg.UnlockPoints
	}
	if cfg.Anonymous {
		body["is_anonymous"] = true
	}
	return body
}

// re0ResolutionOf 分辨率 → RE0 的枚举（480P / 720P / 1080P / 2K / 4K / 8K），认不出不填
func re0ResolutionOf(pix string) string {
	switch strings.ToLower(pix) {
	case "4320p":
		return "8K"
	case "2160p":
		return "4K"
	case "1440p":
		return "2K"
	case "1080p":
		return "1080P"
	case "720p":
		return "720P"
	case "480p", "576p":
		return "480P"
	}
	return ""
}

// re0SourceOf 片源 → RE0 的枚举。认不出就不填，别瞎填：填错了会被当成虚标
func re0SourceOf(ri ResourceInfo, ext string) string {
	t := strings.ToUpper(ri.Type)
	switch {
	case ext == "ISO":
		return "蓝光原盘/ISO"
	case t == "REMUX" || strings.Contains(strings.ToUpper(ri.VideoEncode), "REMUX"):
		return "蓝光原盘/REMUX"
	case t == "BLURAY":
		return "BDRip/BluRayEncode"
	case t == "WEB-DL" || t == "WEBRIP":
		return "WEB-DL/WEBRip"
	case t == "HDTV":
		return "HDTV/HDTVRip"
	}
	return ""
}

// re0PublishShare 发到 RE0（shareOutPublishers 里注册）
func re0PublishShare(pl shareOutPayload, cfg shareOutSiteCfg) (string, int, error) {
	if !strings.Contains(pl.URL, "115") || pl.ShareCode == "" {
		return "", 0, shareOutSkipf("不是 115 分享链接")
	}
	if shareOutBadSource(pl.Info) {
		return "", 0, shareOutSkipf("枪版资源，RE0 不收")
	}
	rc := loadRe0Cfg()
	if why := re0ShareReady(rc); why != "" {
		return "", 0, shareOutSkipf("%s", why)
	}
	body := re0ShareBody(pl, cfg)
	if re0Channel(rc) == "openapi" {
		body["tmdb_id"] = fmt.Sprint(pl.TmdbID)
		body["media_type"] = pl.MediaType
		var out struct {
			Slug string `json:"slug"`
		}
		if err := re0Call(nil, rc, http.MethodPost, "/api/open/shares", nil, body, &out); err != nil {
			return "", 0, re0ShareErr(err)
		}
		return out.Slug, 0, nil
	}
	return re0WebPublishShare(pl, body)
}

// re0ShareErr 站点的错误里分出「不该再试」的：只有限流、5xx 这种过一会儿可能好的才重试，
// 其余（重复、链接无效、权限不够、参数不对）重试也一样被拒。重复不一定是 4xx，也可能是 200 + success:false
func re0ShareErr(err error) error {
	var e *re0Err
	if !errors.As(err, &e) {
		return err // 网络错误、要重新授权：等下一轮
	}
	code := strings.ToUpper(e.Code)
	if e.Status == http.StatusTooManyRequests || e.Status >= 500 || strings.Contains(code, "RATE_LIMIT") || code == "OPENAPI_COOLDOWN" {
		return err
	}
	return shareOutRefusal("RE0", firstNonEmpty(e.Description, e.Message, e.Code))
}

var re0StationIDRe = regexp.MustCompile(`/(movie|tv)/([A-Za-z0-9_-]{6,})(?:[/?#]|$)`)

// re0WebShareJS 在页面里调站点自己的发布接口。X-CSRF-TOKEN 照站点请求客户端的拦截器取 Cookie `csrf_access_token`
const re0WebShareJS = `async (path, body) => {
  const m = document.cookie.match(/(?:^|;\s*)csrf_access_token=([^;]+)/)
  const h = { 'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*' }
  if (m) h['X-CSRF-TOKEN'] = decodeURIComponent(m[1])
  const r = await fetch(path, { method: 'POST', credentials: 'include', headers: h, body: JSON.stringify(body) })
  return { status: r.status, text: await r.text() }
}`

// re0WebPublishShare 网页通道：打开 /tmdb/{类型}/{编号}，等它跳到站内详情页拿站内 id，再在页面里调发布接口
func re0WebPublishShare(pl shareOutPayload, body map[string]any) (string, int, error) {
	target := fmt.Sprintf("/tmdb/%s/%d", pl.MediaType, pl.TmdbID)
	var ref string
	err := re0Web("发布分享 "+pl.MediaType+"/"+fmt.Sprint(pl.TmdbID), target, 3*time.Minute, func(p *rod.Page) error {
		re0Step("找站内条目")
		var kind, id string
		deadline := time.Now().Add(15 * time.Second)
		for time.Now().Before(deadline) {
			if m := re0StationIDRe.FindStringSubmatch(re0URL(p)); m != nil {
				kind, id = m[1], m[2]
				break
			}
			if re0OnLogin(p) {
				return fmt.Errorf("登录态失效（被跳回登录页）")
			}
			time.Sleep(300 * time.Millisecond)
		}
		if id == "" {
			log.Printf("[RE0网页]   %s 没跳到站内详情页（停在 %s）", target, re0URL(p))
			return shareOutSkipf("RE0 上没有这部片的条目")
		}
		if kind != pl.MediaType {
			return shareOutSkipf("RE0 把这部片认成了%s", map[string]string{"movie": "电影", "tv": "剧集"}[kind])
		}
		if kind == "tv" {
			body["tv_id"] = id
		} else {
			body["movie_id"] = id
		}
		re0Step("提交分享")
		res, err := p.Eval(re0WebShareJS, "/api/customer/resources", body)
		if err != nil {
			return fmt.Errorf("页面里调用发布接口失败: %v", err)
		}
		status := res.Value.Get("status").Int()
		text := res.Value.Get("text").Str()
		var out struct {
			Success *bool  `json:"success"`
			Message string `json:"message"`
			Msg     string `json:"msg"`
			Code    string `json:"code"`
			ErrCode string `json:"error_code"`
			Data    struct {
				Slug string `json:"slug"`
				ID   any    `json:"id"`
			} `json:"data"`
		}
		_ = json.Unmarshal([]byte(text), &out)
		msg := firstNonEmpty(out.Message, out.Msg, out.ErrCode, out.Code)
		switch {
		case status == http.StatusUnauthorized:
			return fmt.Errorf("RE0 登录态失效（HTTP 401）")
		case status >= 400 && status < 500 && status != http.StatusTooManyRequests:
			return shareOutRefusal("RE0", firstNonEmpty(msg, fmt.Sprintf("HTTP %d", status)))
		case status < 200 || status >= 300:
			return fmt.Errorf("RE0 发布失败（HTTP %d）：%s", status, firstNonEmpty(msg, truncateStr(text, 80)))
		case out.Success != nil && !*out.Success:
			return shareOutRefusal("RE0", firstNonEmpty(msg, "未说明原因"))
		}
		ref = out.Data.Slug
		if ref == "" && out.Data.ID != nil {
			ref = fmt.Sprint(out.Data.ID)
		}
		log.Printf("[RE0网页]   分享已发布（%s，站内 %s/%s）", firstNonEmpty(ref, "没返回编号"), kind, id)
		return nil
	})
	return ref, 0, err
}
