package api

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"115-station/internal/config"
	"115-station/internal/model"
)

func shareOutRec(id uint, status string, tmdb int, mt string, at time.Time, files ...orgRecordFile) model.OrganizeRecord {
	r := model.OrganizeRecord{ID: id, LinkID: 1, Status: status, TmdbID: tmdb, MediaType: mt, Title: "绝命毒师", Year: "2008",
		PosterPath: "/p.jpg", Files: marshalRecordFiles(files)}
	r.CreatedAt, r.UpdatedAt = at, at
	return r
}

func TestShareOutJudge(t *testing.T) {
	now := time.Date(2026, 10, 17, 12, 0, 0, 0, time.UTC)
	link := model.DownloadLink{ID: 1, CreatedAt: now.Add(-time.Hour)}
	old := now.Add(-time.Hour)
	cases := []struct {
		name string
		link model.DownloadLink
		recs []model.OrganizeRecord
		wait bool
		skip string
		tmdb int
	}{
		{"还没整理", link, nil, true, "", 0},
		{"7 天都没整理", model.DownloadLink{CreatedAt: now.Add(-8 * 24 * time.Hour)}, nil, false, "7 天内没整理出来", 0},
		{"在等人工确认", link, []model.OrganizeRecord{shareOutRec(1, orgStatusAwaiting, 0, "", old)}, true, "", 0},
		{"记录刚写下，等它稳定", link, []model.OrganizeRecord{shareOutRec(1, "success", 1396, "tv", now.Add(-time.Minute))}, true, "", 0},
		{"都没认出", link, []model.OrganizeRecord{shareOutRec(1, "unrecognized", 0, "", old), shareOutRec(2, "failed", 0, "", old)}, false, "整理没认出是哪一部", 0},
		{"合集", link, []model.OrganizeRecord{shareOutRec(1, "success", 1396, "tv", old), shareOutRec(2, "success", 550, "movie", old)}, false, "合集", 0},
		{"一部，几条记录", link, []model.OrganizeRecord{shareOutRec(1, "success", 1396, "tv", old), shareOutRec(2, "exists", 1396, "tv", old), shareOutRec(3, "failed", 0, "", old)}, false, "", 1396},
	}
	for _, c := range cases {
		v := shareOutJudge(c.link, c.recs, now)
		if v.Wait != c.wait || (c.skip == "") != (v.Skip == "") || !strings.Contains(v.Skip, c.skip) || v.TmdbID != c.tmdb {
			t.Errorf("%s: 得到 %+v", c.name, v)
		}
		if c.tmdb > 0 && len(v.Records) != 2 {
			t.Errorf("%s: 成功的记录应有 2 条，得到 %d", c.name, len(v.Records))
		}
	}
}

func TestShareOutPayload(t *testing.T) {
	old := time.Now().Add(-time.Hour)
	link := model.DownloadLink{ID: 1, URL: "https://115cdn.com/s/swabc123", Hash: "swabc123", Code: "x1y2", Name: "绝命毒师 S01 4K"}
	recs := []model.OrganizeRecord{
		shareOutRec(1, "success", 1396, "tv", old,
			orgRecordFile{Fid: "a", Name: "绝命毒师 - S01E01 - 第 1 集.mkv", Orig: "Breaking.Bad.S01E01.2160p.WEB-DL.DDP5.1.H.265-GRP.mkv", Kind: "video", Size: 4 << 30},
			orgRecordFile{Fid: "b", Name: "绝命毒师 - S01E02 - 第 2 集.mkv", Orig: "Breaking.Bad.S01E02.2160p.WEB-DL.DDP5.1.H.265-GRP.mkv", Kind: "video", Size: 4 << 30},
			orgRecordFile{Fid: "c", Name: "绝命毒师 - S01E02 - 第 2 集.chs.ass", Kind: "subtitle", Size: 1 << 10}),
		shareOutRec(2, "success", 1396, "tv", old,
			orgRecordFile{Fid: "d", Name: "绝命毒师 - S01E05 - 第 5 集.mkv", Orig: "Breaking.Bad.S01E05.2160p.WEB-DL.DDP5.1.H.265-GRP.mkv", Kind: "video", Size: 4 << 30},
			orgRecordFile{Fid: "a", Name: "绝命毒师 - S01E01 - 第 1 集.mkv", Kind: "video"}), // 同一个文件出现在两条记录里只算一次
	}
	v := shareOutJudge(link, recs, time.Now())
	pl := shareOutPayloadOf(link, v)
	if pl.ShareCode != "swabc123" || pl.Code != "x1y2" || pl.TmdbID != 1396 || pl.MediaType != "tv" || pl.Title != "绝命毒师" || pl.Year != "2008" {
		t.Fatalf("基本字段不对: %+v", pl)
	}
	if len(pl.Files) != 3 || pl.Ext != "MKV" || !pl.HasSubs || pl.Info.Pix != "2160p" || len(pl.SubLangs) != 1 || pl.SubLangs[0] != "简中" {
		t.Errorf("文件 / 画质不对: files=%v ext=%s subs=%v info=%+v", pl.Files, pl.Ext, pl.HasSubs, pl.Info)
	}
	if len(pl.Seasons) != 1 || pl.Seasons[0] != 1 || episodesCSV(pl.Episodes[1]) != "1-2,5" {
		t.Errorf("季集不对: %v %v", pl.Seasons, pl.Episodes)
	}
	if want := int64(12<<30) + 1<<10; pl.SizeBytes != want {
		t.Errorf("总大小 %d，应为 %d", pl.SizeBytes, want)
	}
}

func TestEpisodesCSV(t *testing.T) {
	for in, want := range map[string]string{"": "", "3": "3", "1,2,3,5,7,8": "1-3,5,7-8", "5,1,2,2": "1-2,5"} {
		var eps []int
		for _, f := range strings.Split(in, ",") {
			if f != "" {
				n, _ := strconv.Atoi(f)
				eps = append(eps, n)
			}
		}
		if got := episodesCSV(eps); got != want {
			t.Errorf("episodesCSV(%q) = %q，应为 %q", in, got, want)
		}
	}
}

// tag 串按站点前端 tag-*.js 的规则：归一、去重、按类别 → 类别内顺序排序，前缀 DT2
func TestDianTagString(t *testing.T) {
	cases := map[string]string{
		"Breaking.Bad.S01E01.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-GRP.mkv": "DT2|4K|WEB-DL|HEVC|DV|HDR10|DDP5.1|ATMOS|MKV",
		"Movie.2019.1080p.BluRay.x264.DTS-HD.MA.7.1-FRDS.mkv":                "DT2|1080P|BLURAY|AVC|DTS-HD.MA7.1|MKV",
		"Movie.2019.2160p.UHD.BluRay.REMUX.HEVC.10bit.TrueHD.7.1.mkv":        "DT2|4K|REMUX|HEVC|10BIT|TRUEHD7.1|MKV",
	}
	for name, want := range cases {
		ri := ParseResourceInfo(name)
		if got := dianTagString(dianTagTokens(ri, "mkv", false)); got != want {
			t.Errorf("%s\n  得到 %s\n  应为 %s（解析 %+v）", name, got, want, ri)
		}
	}
	if got := dianTagString(nil); got != "" {
		t.Errorf("没有项时应为空，得到 %q", got)
	}
}

func dianTestPayload() shareOutPayload {
	return shareOutPayload{URL: "https://115cdn.com/s/swabc123", ShareCode: "swabc123", Code: "x1y2", TmdbID: 1396, MediaType: "tv",
		Title: "绝命毒师", Year: "2008", Poster: "/p.jpg", ShareName: "Breaking.Bad.S01.2160p 搬运自某群", SizeBytes: 12 << 30, Files: []string{"a.mkv"}, Ext: "MKV",
		Info: ParseResourceInfo("Breaking.Bad.S01E01.2160p.WEB-DL.H.265.mkv"), Seasons: []int{1}, Episodes: map[int][]int{1: {1, 2, 5}}}
}

func TestDianShareBody(t *testing.T) {
	pl := dianTestPayload()
	body, err := dianShareBody(pl, shareOutSiteCfg{UnlockPoints: 5}, dianResourceRef{})
	if err != nil {
		t.Fatal(err)
	}
	if body["kind"] != "tmdb" || body["tmdb_id"] != 1396 || body["season"] != 1 || body["episodes_csv"] != "1-2,5" ||
		body["episode_count"] != 3 || body["receive_code"] != "x1y2" || body["unlock_cost_override"] != 5 ||
		body["poster_url"] != "https://image.tmdb.org/t/p/w500/p.jpg" || body["file_extension"] != "MKV" {
		t.Errorf("请求体不对: %v", body)
	}
	// 站内已有这一季：改带 resource_id
	body, _ = dianShareBody(pl, shareOutSiteCfg{}, dianResourceRef{ID: 77, Kind: "tmdb", Season: 1})
	if body["resource_id"] != 77 || body["tmdb_id"] != nil || body["unlock_cost_override"] != nil {
		t.Errorf("站内已有条目时应带 resource_id: %v", body)
	}
	// 站内条目是别的季：不能挂上去
	body, _ = dianShareBody(pl, shareOutSiteCfg{}, dianResourceRef{ID: 77, Season: 2})
	if body["resource_id"] != nil || body["tmdb_id"] != 1396 {
		t.Errorf("季对不上不该用站内条目: %v", body)
	}
	for name, mut := range map[string]func(*shareOutPayload){
		"访问码":  func(p *shareOutPayload) { p.Code = "" },
		"分辨率":  func(p *shareOutPayload) { p.Info.Pix = "" },
		"文件格式": func(p *shareOutPayload) { p.Ext = "" },
		"总大小":  func(p *shareOutPayload) { p.SizeBytes = 0 },
		"集号":   func(p *shareOutPayload) { p.Seasons = nil },
		"枪版":   func(p *shareOutPayload) { p.Info.Type = "CAM" },
	} {
		p := dianTestPayload()
		mut(&p)
		var skip *errShareOutSkip
		if _, err := dianShareBody(p, shareOutSiteCfg{}, dianResourceRef{}); err == nil || !asSkip(err, &skip) {
			t.Errorf("缺%s 应跳过，得到 %v", name, err)
		}
	}
}

func asSkip(err error, out **errShareOutSkip) bool {
	e, ok := err.(*errShareOutSkip)
	*out = e
	return ok
}

func TestRe0ShareBody(t *testing.T) {
	pl := dianTestPayload()
	pl.HasSubs, pl.SubLangs = true, []string{"简中"}
	b := re0ShareBody(pl, shareOutSiteCfg{UnlockPoints: 3, Anonymous: true})
	if b["url"] != pl.URL || b["access_code"] != "x1y2" || b["share_size"] != "12.0 GB" || b["hide_link"] != true ||
		b["unlock_points"] != 3 || b["is_anonymous"] != true || b["title"] != "绝命毒师 (2008) S01 4K WEB-DL" {
		t.Errorf("请求体不对: %v", b)
	}
	// 备注一项一行：片名年份、季集、视频、音频（没认出不写）、字幕、文件、原分享名
	wantRemark := "片名：绝命毒师 (2008)\n季集：S01 E1-2,5 · 共 3 集\n视频：4K · WEB-DL · H265\n字幕：外挂 简中\n" +
		"文件：MKV · 1 个视频 · 12.0 GB\n原分享名：Breaking.Bad.S01.2160p 搬运自某群"
	if b["remark"] != wantRemark {
		t.Errorf("备注不对:\n%v\n应为:\n%s", b["remark"], wantRemark)
	}
	if l, _ := b["subtitle_language"].([]string); len(l) != 1 || l[0] != "简中" {
		t.Errorf("字幕语言应为简中: %v", b["subtitle_language"])
	}
	if r, _ := b["video_resolution"].([]string); len(r) != 1 || r[0] != "4K" {
		t.Errorf("分辨率应为 4K: %v", b["video_resolution"])
	}
	if s, _ := b["source"].([]string); len(s) != 1 || s[0] != "WEB-DL/WEBRip" {
		t.Errorf("片源应为 WEB-DL/WEBRip: %v", b["source"])
	}
	// 认不出的不填
	pl.Info = ResourceInfo{}
	b = re0ShareBody(pl, shareOutSiteCfg{})
	if b["video_resolution"] != nil || b["source"] != nil || b["unlock_points"] != nil || b["is_anonymous"] != nil {
		t.Errorf("认不出的字段不该填: %v", b)
	}
	if re0SourceOf(ResourceInfo{Type: "BluRay"}, "ISO") != "蓝光原盘/ISO" || re0SourceOf(ResourceInfo{Type: "Remux"}, "MKV") != "蓝光原盘/REMUX" {
		t.Error("原盘 / REMUX 的片源映射不对")
	}
}

func newShareOutEnv(t *testing.T) *Handler {
	t.Helper()
	newTestDB(t, "shareout.db")
	old := notifyConfigSource
	notifyConfigSource = &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	t.Cleanup(func() { notifyConfigSource = old })
	return &Handler{DB: model.DB}
}

// 端到端：打标记 → 整理记录挂上来 → tick 发布 → 不重复发；失败按间隔重试，次数用完就停
func TestShareOutRunFlow(t *testing.T) {
	h := newShareOutEnv(t)
	if err := saveShareOutCfg(shareOutCfg{Re0: shareOutSiteCfg{Enabled: true}, Dian: shareOutSiteCfg{Enabled: true, UnlockPoints: 8}}); err != nil {
		t.Fatal(err)
	}
	calls := map[string]int{}
	var dianErr error = &dianAPIErr{Status: 502, Msg: "站点挂了"}
	oldPubs := shareOutPublishers
	t.Cleanup(func() { shareOutPublishers = oldPubs })
	shareOutPublishers = map[string]shareOutPublisher{
		shareOutSiteRe0: func(pl shareOutPayload, cfg shareOutSiteCfg) (string, int, error) {
			calls["re0"]++
			if pl.TmdbID != 550 || pl.Code != "x1y2" {
				t.Errorf("RE0 拿到的载荷不对: %+v", pl)
			}
			return "slug1", 0, nil
		},
		shareOutSiteDian: func(pl shareOutPayload, cfg shareOutSiteCfg) (string, int, error) {
			calls["dian"]++
			if cfg.UnlockPoints != 8 {
				t.Errorf("癫影的设置没带过去: %+v", cfg)
			}
			return "", 0, dianErr
		},
	}

	mkLink := func(code string) model.DownloadLink {
		id := dlLinkRecord(h, "https://115cdn.com/s/swabc123?password="+code, "share", "Fight Club", "web", nil)
		h.shareOutMark(id, code)
		var l model.DownloadLink
		h.DB.First(&l, id)
		return l
	}
	link := mkLink("x1y2")
	if link.ShareOut != "re0,dian" || link.Code != "x1y2" {
		t.Fatalf("标记没记上: %+v", link)
	}
	rec := model.OrganizeRecord{LinkID: link.ID, Status: "success", TmdbID: 550, MediaType: "movie", Title: "搏击俱乐部",
		Files: marshalRecordFiles([]orgRecordFile{{Fid: "f", Name: "搏击俱乐部 (1999).mkv", Orig: "Fight.Club.1999.1080p.BluRay.x264.mkv", Kind: "video", Size: 10 << 30}})}
	h.DB.Create(&rec)

	now := time.Now()
	h.shareOutRun(now) // 记录刚写下：等
	if calls["re0"]+calls["dian"] != 0 {
		t.Fatalf("记录刚写下不该发: %v", calls)
	}
	now = now.Add(shareOutSettle + time.Minute)
	h.shareOutRun(now)
	if calls["re0"] != 1 || calls["dian"] != 1 {
		t.Fatalf("两站各发一次，得到 %v", calls)
	}
	var outs []model.ShareOut
	h.DB.Order("site").Find(&outs)
	if len(outs) != 2 || outs[0].Site != "dian" || outs[0].Status != shareOutStatusFailed || outs[1].Status != shareOutStatusDone || outs[1].Ref != "slug1" {
		t.Fatalf("记账不对: %+v", outs)
	}

	h.shareOutRun(now.Add(time.Minute)) // 失败的还没到重试时间，成功的不再发
	if calls["re0"] != 1 || calls["dian"] != 1 {
		t.Fatalf("不该重复发: %v", calls)
	}
	for i := 0; i < 5; i++ { // 到点重试，最多 3 次
		now = now.Add(shareOutRetryGap + time.Minute)
		h.shareOutRun(now)
	}
	if calls["dian"] != shareOutMaxAttempts {
		t.Fatalf("癫影应重试到 %d 次为止，实际 %d 次", shareOutMaxAttempts, calls["dian"])
	}

	// 同一个分享又贴了一次：RE0 已经发过，不再发
	link2 := mkLink("x1y2")
	rec2 := rec
	rec2.ID, rec2.LinkID = 0, link2.ID
	h.DB.Create(&rec2)
	dianErr = nil
	h.shareOutRun(now.Add(shareOutSettle + time.Minute))
	var o model.ShareOut
	h.DB.Where("link_id = ? AND site = ?", link2.ID, "re0").First(&o)
	if calls["re0"] != 1 || o.Status != shareOutStatusSkipped || !strings.Contains(o.Message, "已经发过") {
		t.Fatalf("重复的分享应跳过: calls=%v row=%+v", calls, o)
	}
	if calls["dian"] != shareOutMaxAttempts+1 {
		t.Fatalf("癫影上次没发成，这次应该照发: %v", calls)
	}

	// 整理记录列表带出发布结果
	links := recordLinks(h.DB, []model.OrganizeRecord{rec})
	if l := links[link.ID]; l == nil || len(l.ShareOuts) != 2 {
		t.Fatalf("记录列表没带上发布结果: %+v", l)
	}

	// 开关都关着：不打标记
	_ = saveShareOutCfg(shareOutCfg{})
	id := dlLinkRecord(h, "https://115cdn.com/s/swother", "share", "x", "web", nil)
	h.shareOutMark(id, "")
	var l3 model.DownloadLink
	h.DB.First(&l3, id)
	if l3.ShareOut != "" {
		t.Fatalf("开关关着不该打标记: %+v", l3)
	}
}

// RE0 OpenAPI 通道：请求体照文档，400 当作拒收不重试
func TestRe0PublishShareOpenAPI(t *testing.T) {
	newShareOutEnv(t)
	var got map[string]any
	reject := ""
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/open/shares" || r.Method != http.MethodPost {
			w.Write([]byte(`{"success":false,"code":"NOT_FOUND"}`))
			return
		}
		if r.Header.Get("X-API-Key") != "sec" || r.Header.Get("Authorization") != "Bearer at" {
			t.Errorf("要带应用 Secret 和用户 Token")
		}
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &got)
		switch reject {
		case "400":
			w.WriteHeader(http.StatusBadRequest)
			w.Write([]byte(`{"success":false,"code":"400","message":"分享链接已存在"}`))
			return
		case "200dup": // 重复也可能是 200 + success:false
			w.Write([]byte(`{"success":false,"code":"SHARE_DUPLICATED","message":"该链接已被分享"}`))
			return
		case "400other":
			w.WriteHeader(http.StatusBadRequest)
			w.Write([]byte(`{"success":false,"code":"400","message":"链接无效"}`))
			return
		case "500":
			w.WriteHeader(http.StatusInternalServerError)
			w.Write([]byte(`{"success":false,"code":"500","message":"internal"}`))
			return
		}
		w.Write([]byte(`{"success":true,"code":"200","data":{"slug":"abc"}}`))
	}))
	defer srv.Close()
	if err := saveRe0Cfg(&re0Cfg{BaseURL: srv.URL, ClientSecret: "sec", AccessToken: "at", RefreshToken: "rt",
		TokenExp: time.Now().Add(time.Hour).Unix(), Scope: re0Scope}); err != nil {
		t.Fatal(err)
	}
	pl := dianTestPayload()
	pl.MediaType, pl.TmdbID, pl.Seasons = "movie", 550, nil
	ref, _, err := re0PublishShare(pl, shareOutSiteCfg{})
	if err != nil || ref != "abc" {
		t.Fatalf("发布: ref=%q err=%v", ref, err)
	}
	if got["tmdb_id"] != "550" || got["media_type"] != "movie" || got["access_code"] != "x1y2" || got["url"] != pl.URL {
		t.Errorf("请求体不对: %v", got)
	}
	var skip *errShareOutSkip
	for _, c := range []struct{ reject, want string }{
		{"400", "RE0 上已经有这条分享"},
		{"200dup", "RE0 上已经有这条分享"},
		{"400other", "RE0 拒收：链接无效"},
	} {
		reject = c.reject
		if _, _, err := re0PublishShare(pl, shareOutSiteCfg{}); !asSkip(err, &skip) || err.Error() != c.want {
			t.Errorf("%s 应当作拒收（不重试）「%s」，得到 %v", c.reject, c.want, err)
		}
	}
	reject = "500" // 站点临时出错：照失败记，到点重试
	if _, _, err := re0PublishShare(pl, shareOutSiteCfg{}); err == nil || asSkip(err, &skip) {
		t.Errorf("500 应按失败重试，得到 %v", err)
	}
	reject = ""

	// 授权里没有 write：不发
	_ = saveRe0Cfg(&re0Cfg{BaseURL: srv.URL, ClientSecret: "sec", AccessToken: "at", TokenExp: time.Now().Add(time.Hour).Unix(), Scope: "meta query unlock"})
	if _, _, err := re0PublishShare(pl, shareOutSiteCfg{}); !asSkip(err, &skip) || !strings.Contains(err.Error(), "write") {
		t.Errorf("没有 write 权限应跳过，得到 %v", err)
	}
}

func TestSubLangOf(t *testing.T) {
	for name, want := range map[string]string{
		"Show.S01E01.chs.ass":     "简中",
		"Show.S01E01.cht.srt":     "繁中",
		"Show.S01E01.chs&eng.ass": "简英双语",
		"Show.S01E01.简日双语.ass":    "简日双语",
		"Show.S01E01.zh-Hant.srt": "繁中",
		"Show.S01E01.ass":         "",
		"Show.S01E01.eng.srt":     "",
		"Show.S01E01.default.ass": "",
	} {
		if got := subLangOf(name); got != want {
			t.Errorf("subLangOf(%q) = %q，应为 %q", name, got, want)
		}
	}
}

// 电影：标题不带季，备注没有季集那行，音频写出来
func TestShareOutRemarkMovie(t *testing.T) {
	pl := shareOutPayload{Title: "搏击俱乐部", Year: "1999", MediaType: "movie", ShareName: "搏击俱乐部", Ext: "MKV",
		Files: []string{"a.mkv"}, SizeBytes: 10 << 30,
		Info: ParseResourceInfo("Fight.Club.1999.1080p.BluRay.x264.DTS-HD.MA.5.1-FRDS.mkv")}
	if h := shareOutHeadline(pl); h != "搏击俱乐部 (1999) 1080P BluRay" {
		t.Errorf("标题 %q", h)
	}
	r := shareOutRemark(pl)
	if strings.Contains(r, "季集") || strings.Contains(r, "原分享名") || !strings.Contains(r, "音频：DTS-HD.MA.5.1") ||
		!strings.Contains(r, "视频：1080P · BluRay · ") || !strings.Contains(r, "FRDS") {
		t.Errorf("备注不对:\n%s", r)
	}
}

// 从 RE0 / 癫影拿到的链接（找资源提交、订阅提交）又被贴进链接转存：两站都不发
func TestShareOutFromSite(t *testing.T) {
	h := newShareOutEnv(t)
	_ = saveShareOutCfg(shareOutCfg{Re0: shareOutSiteCfg{Enabled: true}, Dian: shareOutSiteCfg{Enabled: true}})
	calls := 0
	oldPubs := shareOutPublishers
	t.Cleanup(func() { shareOutPublishers = oldPubs })
	pub := func(shareOutPayload, shareOutSiteCfg) (string, int, error) { calls++; return "x", 0, nil }
	shareOutPublishers = map[string]shareOutPublisher{shareOutSiteRe0: pub, shareOutSiteDian: pub}

	// 找资源从癫影解锁转存过一次（来源链接记着站名）；订阅从 RE0 转存过另一个（站名在 SubAttempt 上）
	dlLinkRecord(h, "https://115cdn.com/s/swfromdian", "share", "x", "癫影", nil)
	subLink := dlLinkRecord(h, "https://115cdn.com/s/swfromre0", "share", "x", "订阅", nil)
	h.DB.Create(&model.SubAttempt{SubID: 1, LinkID: subLink, Source: "re0", Hash: "swfromre0"})
	// 观影来的不算
	dlLinkRecord(h, "https://115cdn.com/s/swfromgy", "share", "x", "观影", nil)

	now := time.Now()
	for _, code := range []string{"swfromdian", "swfromre0", "swfromgy"} {
		id := dlLinkRecord(h, "https://115cdn.com/s/"+code, "share", "x", "web", nil)
		h.shareOutMark(id, "abcd")
		rec := model.OrganizeRecord{LinkID: id, Status: "success", TmdbID: 550, MediaType: "movie", Title: "x",
			Files: marshalRecordFiles([]orgRecordFile{{Fid: code, Name: "x.mkv", Orig: "x.1080p.mkv", Kind: "video", Size: 1 << 30}})}
		h.DB.Create(&rec)
	}
	h.shareOutRun(now.Add(shareOutSettle + time.Minute))
	if calls != 2 {
		t.Fatalf("只有观影来的那条该发（两站各一次），实际发了 %d 次", calls)
	}
	var skipped []model.ShareOut
	h.DB.Where("status = ?", shareOutStatusSkipped).Find(&skipped)
	if len(skipped) != 4 {
		t.Fatalf("从两站来的两条 × 两站应全部跳过，得到 %+v", skipped)
	}
	for _, o := range skipped {
		if !strings.Contains(o.Message, "从 RE0 / 癫影拿到的") {
			t.Errorf("跳过原因不对: %+v", o)
		}
	}
}

// 发布接口要的是站内数字编号，取自详情页分享按钮的属性（2026-10-18 现场传了地址里的 uuid，回「参数错误」）
func TestRe0StationNumOf(t *testing.T) {
	movie := `["$","$L4b",null,{"movieId":273,"tvId":"","title":"盗梦空间 (2010)","size":"small","submitLocked":false}]`
	tv := `x{"movieId":"","tvId":1024,"title":"绝命毒师 (2008)"}`
	cases := []struct{ rsc, kind, want string }{
		{movie, "movie", "273"},
		{movie, "tv", ""},
		{tv, "tv", "1024"},
		{tv, "movie", ""},
		{`{"related":{"movieId":9}}`, "movie", "9"}, // 属性改版了：退回宽松匹配
		{`没有`, "movie", ""},
	}
	for _, c := range cases {
		if got := re0StationNumOf(c.rsc, c.kind); got != c.want {
			t.Errorf("%s / %s: 得到 %q，应为 %q", c.rsc, c.kind, got, c.want)
		}
	}
}
