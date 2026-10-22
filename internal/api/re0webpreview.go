package api

// RE0 网页通道的文件预览（不解锁、不花积分）。
//
// 2026-10-22 读站点前端：资源页（/resource/115/{slug}）解锁前就显示「文件列表」，数据来自
// GET /go-api/customer/resources/{slug}/file-list（站点的请求客户端把 /go-api/ 改写成 /api/，再走 signedFetch），
// 页面组件的 fileListPreviewEnabled 由服务端下发。此前以为网页通道没有预览、订阅一律不付费解锁，是误判。
// 请求必须借站点自己的 signedFetch（re0WebSignedJS，见 shareout_re0.go），普通 fetch 会 401。
// 返回格式按 OpenAPI 的 file-list 认（{success, data:{files, share_title, …}}）；对不上就报错、不猜，
// 订阅这时不花积分（errRe0NoPreview）

import (
	"encoding/json"
	"fmt"
	"log"
	"time"

	"github.com/go-rod/rod"
)

// re0WebPreviewFiles 打开资源页，在页面里调文件列表接口。出错一律包上 errRe0NoPreview
func re0WebPreviewFiles(slug string) (*re0Preview, error) {
	if !re0SlugRe.MatchString(slug) {
		return nil, fmt.Errorf("%w（资源编号无效）", errRe0NoPreview)
	}
	var p *re0Preview
	err := re0Web("文件预览 "+truncateStr(slug, 16), "/resource/115/"+slug, 2*time.Minute, func(pg *rod.Page) error {
		re0Step("读文件列表")
		res, err := pg.Eval(re0WebSignedJS, "/api/customer/resources/"+slug+"/file-list", nil)
		if err != nil {
			return fmt.Errorf("页面里调用文件列表接口失败: %v", err)
		}
		status, text := res.Value.Get("status").Int(), res.Value.Get("text").Str()
		if status == 0 {
			return fmt.Errorf("%s", text) // 拿不到 signedFetch 等：helper 自己写好了原因
		}
		p, err = parseRe0WebPreview(status, text)
		if err != nil {
			log.Printf("[RE0网页]   文件列表返回（HTTP %d）: %s", status, truncateStr(text, 300))
			return err
		}
		log.Printf("[RE0网页]   文件预览读到 %d 个文件", len(p.Entries))
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("%w（%v）", errRe0NoPreview, err)
	}
	return p, nil
}

// parseRe0WebPreview 网页接口的返回 → 预览。外层是站点通用的 {success, message, code, data}，
// data 里认 OpenAPI file-list 的字段；一个都没有就当格式变了报错（否则会被读成「预览里没有文件 = 失效」）
func parseRe0WebPreview(status int, text string) (*re0Preview, error) {
	var env struct {
		Success *bool           `json:"success"`
		Message string          `json:"message"`
		Msg     string          `json:"msg"`
		Code    any             `json:"code"`
		Data    json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal([]byte(text), &env); err != nil {
		return nil, fmt.Errorf("文件列表返回不是 JSON（HTTP %d）", status)
	}
	if status < 200 || status >= 300 || (env.Success != nil && !*env.Success) {
		msg := firstNonEmpty(env.Message, env.Msg)
		if msg == "" && env.Code != nil {
			msg = fmt.Sprint(env.Code)
		}
		return nil, fmt.Errorf("文件列表接口报错（HTTP %d）：%s", status, firstNonEmpty(msg, truncateStr(text, 120)))
	}
	raw := []byte(text)
	if len(env.Data) > 0 && string(env.Data) != "null" {
		raw = env.Data
	}
	var keys map[string]json.RawMessage
	if json.Unmarshal(raw, &keys) != nil || (keys["files"] == nil && keys["result_type"] == nil && keys["resource_validate_message"] == nil) {
		return nil, fmt.Errorf("文件列表返回格式认不出（站点可能改版）")
	}
	return parseRe0Preview(raw)
}
