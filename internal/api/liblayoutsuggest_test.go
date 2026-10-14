package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// 媒体库布局建议（liblayoutsuggest.go）。用例编号对应 LIBRARY-LAYOUT-PLAN.md §5

func mv(path, name, tmdb string) embyReconItem {
	it := embyReconItem{Type: "Movie", Name: name, Path: "/m/" + path}
	if tmdb != "" {
		it.ProviderIds = map[string]string{"Tmdb": tmdb}
	}
	return it
}

func sr(path, name string) embyReconItem {
	return embyReconItem{Type: "Series", Name: name, Path: "/m/" + path}
}

// suggestFor 跑一遍建议：videos 是台账视频，Emby 路径 /m/... 原样映射到本地根 /m
func suggestFor(layout libCategoryLayout, videos []string, items []embyReconItem, dismissed map[string]bool) layoutSuggestResult {
	return suggestLayoutFromEmby(items, layout, newLayoutLedgerIndex(videos), "/m", func(p string) string { return p }, dismissed)
}

func findSuggestion(r layoutSuggestResult, lib, dir string) *layoutSuggestion {
	for i := range r.Suggestions {
		if r.Suggestions[i].Lib == lib && r.Suggestions[i].Dir == dir {
			return &r.Suggestions[i]
		}
	}
	return nil
}

func findUnsure(r layoutSuggestResult, lib, dir string) *layoutUnsure {
	for i := range r.Unsure {
		if r.Unsure[i].Lib == lib && r.Unsure[i].Dir == dir {
			return &r.Unsure[i]
		}
	}
	return nil
}

func TestJudgeMovieDir(t *testing.T) {
	cases := []struct {
		name  string
		items []movieDirItem
		stems []string
		want  movieDirVerdict
	}{
		{"T1 TMDB 不同 = 平铺", []movieDirItem{{"英雄本色", "1"}, {"无间道", "2"}}, nil, verdictFlat},
		{"T2 TMDB 相同 = 多版本", []movieDirItem{{"阿凡达", "9"}, {"Avatar", "9"}}, []string{"阿凡达.2009.4K", "Avatar.2009.1080p"}, verdictTitle},
		{"T3 都没 TMDB、片名不同 = 平铺", []movieDirItem{{"英雄本色", ""}, {"无间道", ""}}, nil, verdictFlat},
		{"T4 都没 TMDB、去掉版本后缀同名 = 多版本", []movieDirItem{{"双版本 - 4K", ""}, {"双版本 - 1080p", ""}}, nil, verdictTitle},
		{"T5 TMDB 说平铺、文件名是同一部 = 认不准", []movieDirItem{{"某片", "1"}, {"某片外传", "2"}}, []string{"某片.2020.2160p", "某片.2020.1080p"}, verdictUnsure},
		{"同名、有编号的不一样 = 认不准", []movieDirItem{{"小妇人", "1"}, {"小妇人", "2"}, {"小妇人", ""}}, nil, verdictUnsure},
		{"一个条目 = 一部", []movieDirItem{{"x", ""}}, nil, verdictTitle},
	}
	for _, c := range cases {
		if got, _ := judgeMovieDir(c.items, c.stems); got != c.want {
			t.Errorf("%s: got %v want %v", c.name, got, c.want)
		}
	}
}

func TestSuggestFlatAndMultiVersion(t *testing.T) {
	layout := layoutOf(map[string]string{"电影": "movie"})
	videos := []string{
		"115/港片/英雄本色.1986.strm", "115/港片/无间道.2002.strm",
		"115/自建/双版本 (2020)/双版本 - 4K.strm", "115/自建/双版本 (2020)/双版本 - 1080p.strm",
		"115/电影/阿凡达 (2009)/阿凡达.strm",
	}
	r := suggestFor(layout, videos, []embyReconItem{
		mv("115/港片/英雄本色.1986.strm", "英雄本色", "11"),
		mv("115/港片/无间道.2002.strm", "无间道", "22"),
		mv("115/自建/双版本 (2020)/双版本 - 4K.strm", "双版本", "33"),
		mv("115/自建/双版本 (2020)/双版本 - 1080p.strm", "双版本", "33"),
		mv("115/电影/阿凡达 (2009)/阿凡达.strm", "阿凡达", "44"), // T9：已经认得，不出建议
		mv("/别处/x.strm", "库外", ""),
	}, nil)
	if !r.MappingOK {
		t.Fatal("映射应判正常")
	}
	if s := findSuggestion(r, "115", "港片"); s == nil || s.Type != "movie" || s.Titles != 2 || s.keys[0] != "115/港片/英雄本色.1986.strm" {
		t.Errorf("平铺目录本身是分类: %+v", s)
	}
	if s := findSuggestion(r, "115", "自建"); s == nil || s.Titles != 1 || s.keys[0] != "115/自建/双版本 (2020)" {
		t.Errorf("多版本：分类是上一层: %+v", s)
	}
	if findSuggestion(r, "115", "电影") != nil || len(r.Suggestions) != 2 {
		t.Errorf("已认得的不出建议: %+v", r.Suggestions)
	}
}

// T6：电影条目的文件名带集号 → 不出建议
func TestSuggestEpisodeAsMovie(t *testing.T) {
	r := suggestFor(layoutOf(nil), []string{"115/错放/狂飙/狂飙.S01E01.strm"},
		[]embyReconItem{mv("115/错放/狂飙/狂飙.S01E01.strm", "狂飙 第1集", "")}, nil)
	if findSuggestion(r, "115", "错放") != nil {
		t.Error("不该出建议")
	}
	if u := findUnsure(r, "115", "错放"); u == nil || !strings.Contains(u.Reason, "集号") {
		t.Errorf("认不准: %+v", r.Unsure)
	}
}

// T7：剧集直接在库根下 → 建议登记库根；库根下直接放的电影视频是平铺
func TestSuggestLibRoot(t *testing.T) {
	videos := []string{"剧集库/某剧/Season 01/某剧.S01E01.strm", "剧集库/另一部/E01.strm", "电影库/无间道.2002.strm"}
	r := suggestFor(layoutOf(nil), videos, []embyReconItem{
		sr("剧集库/某剧", "某剧"), sr("剧集库/另一部", "另一部"),
		mv("电影库/无间道.2002.strm", "无间道", "1"),
	}, nil)
	if s := findSuggestion(r, "剧集库", ""); s == nil || s.Type != "tv" || s.Titles != 2 {
		t.Errorf("剧集库根: %+v", r)
	}
	if s := findSuggestion(r, "电影库", ""); s == nil || s.Type != "movie" || s.keys[0] != "电影库/无间道.2002.strm" {
		t.Errorf("电影库根平铺: %+v", r)
	}
}

// 一个目录在 Emby 里既是一部片又装着别的片：两边都认不准
func TestSuggestTitleCategoryConflict(t *testing.T) {
	videos := []string{"115/X/a.strm", "115/X/剧/E01.strm"}
	r := suggestFor(layoutOf(nil), videos, []embyReconItem{mv("115/X/a.strm", "A", "1"), sr("115/X/剧", "剧")}, nil)
	if len(r.Suggestions) != 0 {
		t.Errorf("不该有建议: %+v", r.Suggestions)
	}
	if findUnsure(r, "115", "X") == nil || findUnsure(r, "115", "") == nil {
		t.Errorf("两边都要标认不准: %+v", r.Unsure)
	}
}

// 混合类型 → auto；Video / BoxSet 不当证据；台账没有的不当证据；忽略过的不列
func TestSuggestTypesAndFilters(t *testing.T) {
	videos := []string{"115/杂/片A/a.strm", "115/杂/剧B/E01.strm", "115/家庭/v.strm"}
	items := []embyReconItem{
		mv("115/杂/片A/a.strm", "片A", "1"), sr("115/杂/剧B", "剧B"),
		{Type: "Video", Name: "v", Path: "/m/115/家庭/v.strm"},
		mv("115/残留/x/x.strm", "残留", "9"), // 台账没有
	}
	r := suggestFor(layoutOf(nil), videos, items, nil)
	if s := findSuggestion(r, "115", "杂"); s == nil || s.Type != "auto" {
		t.Errorf("电影剧集混着 = auto: %+v", r.Suggestions)
	}
	if len(r.Suggestions) != 1 {
		t.Errorf("只该有一条: %+v", r.Suggestions)
	}
	r = suggestFor(layoutOf(nil), videos, items, map[string]bool{layoutCatKey("115", "杂"): true})
	if len(r.Suggestions) != 0 || r.Dismissed != 1 {
		t.Errorf("忽略过的: %+v", r)
	}
}

// T13：路径映射全对不上 → 不出建议
func TestSuggestMappingBroken(t *testing.T) {
	r := suggestLayoutFromEmby([]embyReconItem{mv("115/港片/a.strm", "a", "1")}, layoutOf(nil),
		newLayoutLedgerIndex([]string{"115/港片/a.strm"}), "/media", func(p string) string { return p }, nil)
	if r.MappingOK || len(r.Suggestions) != 0 {
		t.Errorf("got %+v", r)
	}
}

// T10 / T11：预览
func TestPreviewLayout(t *testing.T) {
	before := buildLibCategoryLayout([]model.CategoryRule{{MediaType: "movie", Name: "电影"}})
	videos := []string{
		"115/电影/合集/钢铁侠 (2008)/钢铁侠.strm", // 之前「合集」被当成一部片
		"115/LGNB/英雄本色 (1986)/英雄本色.strm",
		"115/LGNB/花絮/幕后.strm",
		"115/LGNB/无间道 (2002)/无间道.strm",
	}
	ix := newLayoutLedgerIndex(videos)

	// T10：新分类比已有的深 → 已有片目 key 变化
	after := buildLibCategoryLayout([]model.CategoryRule{{MediaType: "movie", Name: "电影"}}).
		withExtraDirs([]libExtraDir{{Dir: "电影/合集", Type: "movie"}})
	p := previewLayout(before, after, ix, nil, nil)
	if p.Changed != 1 || p.ChangedSamples[0] != "115/电影/合集" || p.NewTitles != 1 || p.NewSamples[0] != "115/电影/合集/钢铁侠 (2008)" {
		t.Errorf("改 key: %+v", p)
	}

	// T11：没有佐证的子目录单独列出（Emby 佐证一部、NFO 佐证一部、花絮什么都没有）
	after = buildLibCategoryLayout([]model.CategoryRule{{MediaType: "movie", Name: "电影"}}).
		withExtraDirs([]libExtraDir{{Lib: "115", Dir: "LGNB", Type: "movie"}})
	p = previewLayout(before, after, ix, map[string]bool{"115/LGNB/英雄本色 (1986)": true},
		func(k string) bool { return k == "115/LGNB/无间道 (2002)" })
	if p.NewTitles != 3 || p.Corroborated != 2 || p.UncorroboratedN != 1 || p.Uncorroborated[0] != "115/LGNB/花絮" || p.Changed != 0 {
		t.Errorf("佐证: %+v", p)
	}

	// 删条目：认不出的列在 Lost
	p = previewLayout(after, before, ix, nil, nil)
	if p.Lost != 3 {
		t.Errorf("移除: %+v", p)
	}
}

func TestApplyLayoutChange(t *testing.T) {
	cur := []libExtraDir{{Dir: "纪录片", Type: "auto"}, {Lib: "115", Dir: "", Type: "movie"}}
	got := applyLayoutChange(cur, layoutChangeReq{
		Remove: []libExtraDir{{Lib: "115", Dir: "/"}},
		Add:    []libExtraDir{{Lib: "115", Dir: "港片", Type: "movie"}},
	})
	if len(got) != 2 || got[0].Dir != "纪录片" || got[1].Dir != "港片" {
		t.Errorf("got %+v", got)
	}
}

// 对账的平铺判定与建议同一个函数：同名不同 TMDB 的标认不准
func TestReconcileUnsureDir(t *testing.T) {
	items := []embyReconItem{
		mv("115/自建/小妇人/a.strm", "小妇人", "1"),
		mv("115/自建/小妇人/b.strm", "小妇人", "2"),
		mv("115/自建/小妇人/c.strm", "小妇人", ""),
		mv("115/库根片 (2001)/x.strm", "库根片", "3"),
	}
	r := reconcileEmby(items, nil, layoutOf(nil), "/m", func(p string) string { return p }, func(string) bool { return true })
	got := map[string]embyReconDir{}
	for _, d := range r.Movie.UncategorizedBy {
		got[d.Dir] = d
	}
	if d := got["115/自建"]; !d.Unsure || d.Flat || d.Lib != "115" || d.Rel != "自建" {
		t.Errorf("认不准: %+v", d)
	}
	if d := got["115"]; d.Lib != "115" || d.Rel != "" || d.Extra != "" {
		t.Errorf("库根：Lib 有了，Rel 为空: %+v", d)
	}
}

// 接口：忽略 → 预览 → 采纳（记来源与时间、清掉忽略标记）；Emby 没配时预览只靠 NFO，照样能用
func TestLayoutAdoptDismissHandlers(t *testing.T) {
	gin.SetMode(gin.TestMode)
	newTestDB(t, "layoutadopt.db")
	notifyConfigSource = &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	t.Cleanup(func() { notifyConfigSource = nil; forgetLibLayoutCaches() })
	h := &Handler{DB: model.DB, Config: notifyConfigSource}
	seedCategoryRules(t, model.CategoryRule{MediaType: "movie", Name: "电影", IsDefault: true})
	for _, rel := range []string{"115/港片/英雄本色.1986.strm", "115/港片/无间道.2002.strm"} {
		model.DB.Create(&model.SyncedFile{FileID: rel, RelPath: rel, Kind: "video"})
	}
	call := func(fn gin.HandlerFunc, body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
		c.Request.Header.Set("Content-Type", "application/json")
		fn(c)
		return w
	}

	if w := call(h.LibraryLayoutDismiss, `{"lib":"115","dir":"港片"}`); w.Code != http.StatusOK {
		t.Fatalf("忽略: %d %s", w.Code, w.Body.String())
	}
	if !loadLayoutDismissed()[layoutCatKey("115", "港片")] {
		t.Fatal("忽略没记上")
	}

	w := call(h.LibraryLayoutPreview, `{"add":[{"lib":"115","dir":"港片","type":"movie"}]}`)
	var pv struct{ Preview layoutPreview }
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &pv) != nil || pv.Preview.NewTitles != 2 {
		t.Fatalf("预览: %d %s", w.Code, w.Body.String())
	}
	if len(loadLibExtraDirs()) != 0 {
		t.Fatal("预览不该保存")
	}

	if w := call(h.LibraryLayoutAdopt, `{"add":[{"lib":"115","dir":"港片","type":"movie"}],"source":"emby"}`); w.Code != http.StatusOK {
		t.Fatalf("采纳: %d %s", w.Code, w.Body.String())
	}
	dirs := loadLibExtraDirs()
	if len(dirs) != 1 || dirs[0].Lib != "115" || dirs[0].Dir != "港片" || dirs[0].Source != "emby" || dirs[0].At == "" {
		t.Fatalf("采纳结果: %+v", dirs)
	}
	if loadLayoutDismissed()[layoutCatKey("115", "港片")] {
		t.Error("采纳后应清掉忽略标记")
	}
	if n := len(scanLedgerTitlesCached()); n != 2 {
		t.Errorf("采纳后应认出 2 部, got %d", n)
	}
	if w := call(h.LibraryLayoutAdopt, `{"add":[{"lib":"115","dir":"x"}],"source":"瞎写"}`); w.Code != http.StatusBadRequest {
		t.Errorf("来源不认识应拒收: %d", w.Code)
	}
}
