package api

import (
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"115-station/internal/model"
)

// 资源键与站点前端 St() 一致：两个值是 2026-10-15 在浏览器里实际请求成功的 key
func TestDianResourceKey(t *testing.T) {
	for _, c := range []struct {
		typ  string
		id   int
		want string
	}{
		{"movie", 27205, "Bt0ohFPDIIRY1zWMS5Nr2weUINk"},
		{"tv", 1396, "Bt0ohFPDIJ1B3W3aDpcg2Q"},
	} {
		if got := dianResourceKey(c.typ, c.id); got != c.want {
			t.Errorf("%s/%d = %s，期望 %s", c.typ, c.id, got, c.want)
		}
	}
}

// testdata/dian_detail_tv.json 是绝命毒师 resource-detail 的真返回（删到三条分享）
func TestParseDianDetail(t *testing.T) {
	raw, err := os.ReadFile("testdata/dian_detail_tv.json")
	if err != nil {
		t.Fatal(err)
	}
	shares, err := parseDianDetail(raw)
	if err != nil || len(shares) != 3 {
		t.Fatalf("shares=%d err=%v", len(shares), err)
	}
	items := make([]ResourceItem, len(shares))
	for i, s := range shares {
		items[i] = dianItemOf(s)
	}

	// 整部 1–5 季的 115 分享：季写进标题，覆盖估计认得出
	all := items[0]
	if all.Kind != "share115" || all.Action != "unlock" || all.Points == nil || *all.Points != 10 || all.offline() {
		t.Fatalf("整部分享 = %+v", all)
	}
	if !strings.Contains(all.Title, "S01-S05") || !resCoverageOf(all.Title).covers(epKey{S: 3, E: 5}) {
		t.Fatalf("整部分享标题 %q 估不出覆盖第 3 季", all.Title)
	}
	if all.Ref != "516180:"+strconv.Itoa(shares[0].ResourceID) || all.SizeBytes == 0 || all.TimeUnix == 0 {
		t.Fatalf("引用 / 大小 / 时间没带上: %+v", all)
	}

	// 磁力离线分享：解锁出来走 115 离线
	mag := items[1]
	if mag.Kind != "magnet" || !mag.offline() {
		t.Fatalf("磁力分享 = %+v", mag)
	}

	// 带文件清单的单季分享：E01–E07 写进标题，文件清单留给订阅预览
	s1 := items[2]
	if !strings.Contains(s1.Title, "S01E01-E07") || len(s1.files) != 3 {
		t.Fatalf("单季分享 = %q files=%d", s1.Title, len(s1.files))
	}
	// 标题取的是第一个文件名（带着 S01E01），覆盖要按补上的范围算，不能只算第 1 集
	if cov := resCoverageOf(s1.Title); !cov.covers(epKey{S: 1, E: 7}) {
		t.Fatalf("单季分享覆盖估计 = %+v", cov)
	}
	if got := resTagsOf(s1.Title).Pix; got == "" {
		t.Fatalf("画质没从标题里认出来：%q", s1.Title)
	}
}

func TestDianSeasonMark(t *testing.T) {
	for _, c := range []struct {
		s    dianShare
		want string
	}{
		{dianShare{Season: 2, Episodes: "1,2,3"}, "S02E01-E03"},
		{dianShare{Season: 2, Episodes: "5"}, "S02E05"},
		{dianShare{Season: 2, Episodes: "1,2,4"}, "S02"}, // 中间缺集不写范围
		{dianShare{Season: 2}, "S02"},
		{dianShare{SeasonsCSV: "1,2,3"}, "S01-S03"},
		{dianShare{}, ""},
	} {
		if got := c.s.seasonMark(); got != c.want {
			t.Errorf("%+v = %q，期望 %q", c.s, got, c.want)
		}
	}
}

func TestParseDianUnlock(t *testing.T) {
	// 2026-10-15 真返回（去掉了无关字段）
	u, err := parseDianUnlock([]byte(`{"already":false,"code":"ok","new_balance":353,"owner":false,"payload":{"file_list":["盗梦空间 (2010) - 4K.HEVC.DTS.HDR10.mkv"],"receive_code":"cbd9","share_code":"swsqehl3fzi","share_id":114133,"share_kind":"115","url_115":"https://115.com/s/swsqehl3fzi?password=cbd9"},"token_used":false}`))
	if err != nil || u.Share != "https://115.com/s/swsqehl3fzi?password=cbd9" || u.Code != "cbd9" || u.Balance == nil || *u.Balance != 353 {
		t.Fatalf("真返回 115: %+v %v", u, err)
	}
	u, err = parseDianUnlock([]byte(`{"code":"ok","payload":{"share_kind":"115","url_115":"https://115cdn.com/s/swabc?password=x1y2","receive_code":"x1y2"}}`))
	if err != nil || u.Share != "https://115cdn.com/s/swabc?password=x1y2" || u.Code != "x1y2" {
		t.Fatalf("url_115: %+v %v", u, err)
	}
	u, err = parseDianUnlock([]byte(`{"code":"ok","already":true,"payload":{"share_kind":"115","share_code":"swabc","receive_code":"k9k9"}}`))
	if err != nil || u.Share != "https://115cdn.com/s/swabc?password=k9k9" || !u.Again {
		t.Fatalf("share_code 拼链接: %+v %v", u, err)
	}
	u, err = parseDianUnlock([]byte(`{"code":"ok","payload":{"share_kind":"offline","offline_type":"ed2k","urls":["ed2k://|file|a.mkv|1|A|/","ed2k://|file|b.mkv|1|B|/"]}}`))
	if err != nil || len(u.Links) != 2 || u.Share != "" {
		t.Fatalf("多条 ed2k: %+v %v", u, err)
	}
	u, err = parseDianUnlock([]byte(`{"code":"ok","payload":{"share_kind":"offline","url":"magnet:?xt=urn:btih:1\nmagnet:?xt=urn:btih:2\n"}}`))
	if err != nil || !reflect.DeepEqual(u.Links, []string{"magnet:?xt=urn:btih:1", "magnet:?xt=urn:btih:2"}) {
		t.Fatalf("按行拆 url: %+v %v", u, err)
	}
	if _, err := parseDianUnlock([]byte(`{"code":"ok","payload":{}}`)); err == nil {
		t.Fatal("没有链接应报错")
	}
}

func TestDianAlreadySigned(t *testing.T) {
	if !dianAlreadySigned(&dianAPIErr{Status: 409, Code: "conflict", Msg: "x"}) ||
		!dianAlreadySigned(&dianAPIErr{Status: 400, Msg: "今日已签到"}) ||
		dianAlreadySigned(&dianAPIErr{Status: 500, Msg: "服务器错误"}) {
		t.Fatal("已签到判定不对")
	}
	if !(&dianAPIErr{Status: 401, Code: "no_token"}).needLogin() || (&dianAPIErr{Status: 403, Code: "forbidden"}).needLogin() {
		t.Fatal("未登录判定不对")
	}
}

// 订阅：癫影的积分上限与 RE0 分开算；离线分享不用；文件清单当预览
func TestPlanSubCandidatesDian(t *testing.T) {
	now := time.Now()
	sub := &model.Subscription{ID: 1, MediaType: "tv"}
	missing := epKeys("S01E03")
	mk := func(title, ref, kind string, points int) ResourceItem {
		p := points
		return ResourceItem{Source: "dian", Kind: kind, Action: "unlock", Title: title, Ref: ref, Relevant: true, Points: &p, Rank: 0}
	}
	items := []ResourceItem{
		mk("剧 S01 10分", "1:9", "share115", 10),
		mk("剧 S01 磁力", "2:9", "magnet", 0),
		{Source: "re0", Kind: "share115", Action: "unlock", Title: "剧 S01 re0", Ref: "r", Relevant: true, Points: pts(10), Rank: 0},
	}
	titles := func(cs []subCand) (out []string) {
		for _, c := range cs {
			out = append(out, c.Item.Title)
		}
		return
	}
	skipped := map[string]int{}
	// 只开了癫影：癫影的进候选，RE0 的超限；磁力不用
	cands, over := planSubCandidates(items, subPickCtx{Prio: prioAny, Sub: sub, Missing: missing, Now: now, DianMax: 20, DianLeft: -1, Re0Left: -1, Skipped: skipped})
	if got := titles(cands); !reflect.DeepEqual(got, []string{"剧 S01 10分"}) || cands[0].Paid != 10 {
		t.Fatalf("候选 = %v", got)
	}
	if got := titles(over); !reflect.DeepEqual(got, []string{"剧 S01 re0"}) {
		t.Fatalf("超限 = %v", got)
	}
	if skipped["癫影离线资源"] != 1 {
		t.Fatalf("磁力应按「癫影离线资源」跳过: %v", skipped)
	}
	// 电影的磁力照常用（一部片一条链接）
	movie := &model.Subscription{ID: 2, MediaType: "movie"}
	cands, _ = planSubCandidates(items[1:2], subPickCtx{Prio: prioAny, Sub: movie, Now: now, DianMax: 20, DianLeft: -1})
	if len(cands) != 1 {
		t.Fatalf("电影磁力应可用: %v", titles(cands))
	}
	// 癫影预算只剩 5：10 分的超限
	_, over = planSubCandidates(items[:1], subPickCtx{Prio: prioAny, Sub: sub, Missing: missing, Now: now, DianMax: 20, DianLeft: 5})
	if len(over) != 1 {
		t.Fatalf("预算 5 应超限: %v", titles(over))
	}
}

func TestSubDianPreview(t *testing.T) {
	if _, err := model.InitDB("file:dianpreview_test?mode=memory&cache=shared"); err != nil {
		t.Fatalf("init db: %v", err)
	}
	defer func() { model.DB = nil }()
	sub := &model.Subscription{ID: 1, Title: "剧", MediaType: "tv"}
	r := &subRunner{h: &Handler{DB: model.DB}, cfg: testSubCfg(),
		re0Files: func(string) (*re0Preview, error) { t.Fatal("癫影不该去请求 RE0 预览"); return nil, nil }}
	fn := r.re0PreviewFn(sub, map[epKey]bool{{S: 1, E: 2}: true, {S: 1, E: 9}: true})
	it := ResourceItem{Source: "dian", Title: "剧", files: []string{"剧 S01E01.mkv", "剧 S01E02.mkv", "Season 1/剧 S01E03.mkv"}}
	covered, _, err := fn(it)
	if err != nil || !reflect.DeepEqual(covered, []epKey{{S: 1, E: 2}}) {
		t.Fatalf("covered=%v err=%v", covered, err)
	}
	it.files = []string{"剧 S01E05.mkv"}
	if covered, why, err := fn(it); err != nil || covered != nil || !strings.Contains(why, "没有缺的集") {
		t.Fatalf("没有缺集: %v %q %v", covered, why, err)
	}
	it.files = nil
	if _, _, err := fn(it); err == nil {
		t.Fatal("没有文件清单应退回按标题判")
	}
}
