package api

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"testing"
	"time"

	"115-station/internal/config"
	"115-station/internal/model"
)

var washTestRules = []washRule{{ResourcePix: "2160p", ResourceEffect: "DV"}, {ResourcePix: "2160p,1080p"}}

// 哪些集要洗：达标的不洗、认不出画质的不洗、在路上的不洗、范围外的不洗
func TestPlanSubWash(t *testing.T) {
	prio := subPriority{Rules: washTestRules}
	names := map[epKey]string{
		{1, 1}: "剧.S01E01.2160p.WEB-DL.DV.mkv", // 达标
		{1, 2}: "剧.S01E02.1080p.WEB-DL.mkv",    // 要洗
		{1, 3}: "剧 - S01E03 - 第 3 集.mkv",       // 认不出画质
		{1, 4}: "剧.S01E04.1080p.mkv",           // 在路上
		{2, 1}: "剧.S02E01.1080p.mkv",           // 范围外
	}
	st := planSubWash(names, func(k epKey) bool { return k.S == 1 }, map[epKey]bool{{1, 4}: true}, prio)
	if st.Top != 1 || st.Unknown != 1 || !reflect.DeepEqual(st.Want, map[epKey]string{{1, 2}: names[epKey{1, 2}]}) {
		t.Fatalf("%+v", st)
	}
}

// 标题写明画质、对每一集都不比库里好的不去列；标题没写的不下结论
func TestSubWashTitleHopeless(t *testing.T) {
	prio := subPriority{Rules: washTestRules}
	want := map[epKey]string{{1, 2}: "剧.S01E02.1080p.WEB-DL.mkv"}
	if !subWashTitleHopeless("剧 S01 1080p WEBRip", want, prio) {
		t.Fatal("1080p WEBRip 比 1080p WEB-DL 差，不该去列")
	}
	if subWashTitleHopeless("剧 S01 2160p", want, prio) || subWashTitleHopeless("剧 全集", want, prio) {
		t.Fatal("更好的 / 没写画质的要去看")
	}
}

// 洗版挑文件：只要比库里那一集好的
func TestPickShareEpisodesBeats(t *testing.T) {
	prio := subPriority{Rules: washTestRules}
	cur := map[epKey]string{{1, 1}: "剧.S01E01.1080p.WEB-DL.mkv", {1, 2}: "剧.S01E02.2160p.WEB-DL.mkv"}
	entries := []shareEntry{
		fileE("e1", "剧.S01E01.2160p.WEB-DL.mkv", 10),
		fileE("e2", "剧.S01E02.1080p.WEB-DL.mkv", 10), // 比库里差
	}
	p := pickShareEpisodes(entries, sharePickOpts{MediaType: "tv", Missing: map[epKey]bool{{1, 1}: true, {1, 2}: true},
		Compare: prio.compare, Beats: func(k epKey, n string) bool { return prio.compare(n, cur[k]) > 0 }})
	if got := pickIDs(p); !reflect.DeepEqual(got, []string{"e1"}) || !reflect.DeepEqual(p.Covered, epKeys("S01E01")) {
		t.Fatalf("挑中 %v / %v", got, p.Covered)
	}
}

// 端到端：剧已经齐了、开着洗版。E01 是 1080p（原名从整理记录认），E02 已达标；
// 分享里两集都是 2160p DV：只转 E01，订阅不结束、定下洗版截止时间；自定义优先级不洗
func TestSubWashEndToEnd(t *testing.T) {
	newTestDB(t, "subwash.db")
	subTestLayout(t)
	resetTmdbCaches()
	scrapeSeasonMu.Lock()
	scrapeSeasonCache = map[string]seasonCacheEntry{}
	scrapeSeasonMu.Unlock()
	subMediaCache.Lock()
	subMediaCache.m = map[string]subMediaHit{}
	subMediaCache.Unlock()
	model.DB.Create(&model.ScrapeRule{Type: "wash_config", Enabled: true, Config: `剧集:
  mode: replace
  media_type: tv
  priority_level:
  - resource_pix: "2160p"
    resource_effect: "DV"
  - resource_pix: "2160p,1080p"
`})
	resetWashCache()
	t.Cleanup(resetWashCache)

	tmdb := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/tv/600":
			fmt.Fprint(w, `{"id":600,"name":"剧","status":"Ended","first_air_date":"2020-01-01","seasons":[{"season_number":1,"episode_count":2}]}`)
		case "/tv/600/season/1":
			fmt.Fprint(w, `{"episodes":[{"episode_number":1,"air_date":"2020-01-01"},{"episode_number":2,"air_date":"2020-01-08"}]}`)
		default:
			http.Error(w, `{"status_code":34}`, http.StatusNotFound)
		}
	}))
	defer tmdb.Close()
	var received []url.Values
	pan := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/share/snap":
			switch r.URL.Query().Get("cid") {
			case "0":
				fmt.Fprint(w, `{"state":true,"data":{"shareinfo":{"share_title":"剧 S01"},"list":[{"cid":"300","n":"剧 S01","fc":"0"}]}}`)
			case "300":
				fmt.Fprint(w, `{"state":true,"data":{"list":[
					{"fid":"501","cid":"300","n":"剧.S01E01.2160p.WEB-DL.DV.mkv","s":10,"sha":"B1"},
					{"fid":"502","cid":"300","n":"剧.S01E02.2160p.WEB-DL.DV.mkv","s":10,"sha":"B2"}]}}`)
			}
		case "/share/receive":
			r.ParseForm()
			received = append(received, r.PostForm)
			fmt.Fprint(w, `{"state":true}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer pan.Close()
	orig := shareAPIOrigins
	shareAPIOrigins = []string{pan.URL}
	t.Cleanup(func() { shareAPIOrigins = orig })

	dir := t.TempDir()
	h := &Handler{DB: model.DB, Config: &config.Config{ConfigDir: dir, DataDir: dir}}
	model.DB.Create(&model.Storage{Type: "115", Cookie: "UID=1"})
	// 台账里是按模板改过的名字（看不出画质），原名在整理记录里
	e1 := "媒体库/剧集/剧.2020.{tmdbid=600}/Season 01/剧 - S01E01.strm"
	e2 := "媒体库/剧集/剧.2020.{tmdbid=600}/Season 01/剧 - S01E02.strm"
	model.DB.Create(&model.SyncedFile{FileID: "f1", RelPath: e1, Kind: "video"})
	model.DB.Create(&model.SyncedFile{FileID: "f2", RelPath: e2, Kind: "video"})
	model.DB.Create(&model.OrganizeRecord{Status: "success", TmdbID: 600, MediaType: "tv",
		Files: `[{"fid":"f1","name":"剧 - S01E01.mkv","orig":"剧.S01E01.1080p.WEB-DL.mkv","kind":"video"},` +
			`{"fid":"f2","name":"剧 - S01E02.mkv","orig":"剧.S01E02.2160p.WEB-DL.DV.mkv","kind":"video"}]`})
	invalidateLedgerTitles()

	sub := model.Subscription{TmdbID: 600, MediaType: "tv", Title: "剧", Year: "2020", Scope: subScopeAll, Follow: subFollowMissing,
		State: subStateActive, Wash: true}
	model.DB.Create(&sub)

	items := []ResourceItem{
		{Source: "pansou", Kind: "share115", Action: "transfer", Title: "剧 S01 2160p DV", URL: "https://115.com/s/sw9abc?password=x1y2", Relevant: true},
		{Source: "pansou", Kind: "share115", Action: "transfer", Title: "剧 S01 1080p WEBRip", URL: "https://115.com/s/sw8abc?password=x1y2", Relevant: true},
	}
	now := time.Date(2026, 10, 22, 12, 0, 0, 0, time.Local)
	mkdirs := 0
	r := &subRunner{
		h: h, cfg: testSubCfg(), target: "T", cookie: "UID=1", re0Left: -1,
		tc:     &TmdbClient{APIKey: "k", APIURL: tmdb.URL, Language: "zh-CN", httpClient: tmdb.Client()},
		now:    func() time.Time { return now },
		search: func(*model.Subscription) ([]ResourceItem, string) { return append([]ResourceItem(nil), items...), "" },
		mkdir: func(parent, name string) (string, error) {
			mkdirs++
			return fmt.Sprintf("W%d", mkdirs), nil
		},
		rmdir: func(cid string) error { return nil },
	}

	item, ran := r.run(&sub, false)
	if !ran || len(item.Submitted) != 1 || !strings.HasPrefix(item.Submitted[0], "洗版") {
		t.Fatalf("应提交一条洗版: %+v", item)
	}
	if len(received) != 1 || received[0].Get("file_id") != "501" {
		t.Fatalf("只该转没达标的 E01: %v", received)
	}
	model.DB.First(&sub, sub.ID)
	if sub.State != subStateActive || sub.WashUntil == nil || !sub.WashUntil.Equal(now.Add(30*24*time.Hour)) ||
		sub.NextCheckAt == nil || !sub.NextCheckAt.Equal(now.Add(subAfterSubmit)) || !strings.Contains(sub.LastResult, "洗版") {
		t.Fatalf("齐了但还在洗，不该结束: %+v", sub)
	}
	var att model.SubAttempt
	model.DB.Where("sub_id = ?", sub.ID).First(&att)
	if !att.Wash || att.Episodes != `["S01E01"]` {
		t.Fatalf("洗版尝试: %+v", att)
	}

	// 自定义优先级：不洗，说清为什么；没别的事就正常结束
	model.DB.Where("sub_id = ?", sub.ID).Delete(&model.SubAttempt{})
	sub.Priority = `{"mode":"custom","rules":[{"resource_pix":"2160p"}]}`
	model.DB.Save(&sub)
	r.prio, r.searched = nil, nil
	item, _ = r.run(&sub, true)
	model.DB.First(&sub, sub.ID)
	if len(item.Submitted) != 0 || sub.State != subStateDone || !strings.Contains(sub.LastResult, "自定义") {
		t.Fatalf("自定义不洗: %+v / %+v", item, sub)
	}
}
