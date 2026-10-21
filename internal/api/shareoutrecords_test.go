package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

func TestShareOutSiteURL(t *testing.T) {
	base := "https://re0.me"
	cases := []struct {
		o    model.ShareOut
		want string
	}{
		{model.ShareOut{Site: "re0", Status: "done", Ref: "a1b2c3d4e5f647898765432112345678"}, base + "/resource/115/a1b2c3d4e5f647898765432112345678"},
		{model.ShareOut{Site: "re0", Status: "done", Ref: "123", TmdbID: 550, MediaType: "movie"}, base + "/tmdb/movie/550"},
		{model.ShareOut{Site: "dian", Status: "done", TmdbID: 1396, MediaType: "tv"}, dianBase + "/r/" + dianResourceKey("tv", 1396)},
		{model.ShareOut{Site: "re0", Status: "failed", Ref: "a1b2c3d4e5f647898765432112345678"}, ""},
	}
	for _, c := range cases {
		if got := shareOutSiteURL(c.o, base); got != c.want {
			t.Errorf("%+v: 得到 %q，应为 %q", c.o, got, c.want)
		}
	}
}

func TestShareOutRecordsAndRetry(t *testing.T) {
	h := newShareOutEnv(t)
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/shareout/records", h.ShareOutRecords)
	r.POST("/shareout/records/:id/retry", h.ShareOutRetry)

	link := dlLinkRecord(h, "https://115cdn.com/s/swabc", "share", "Fight.Club.1999.1080p", "web", nil)
	old := time.Now().Add(-time.Hour)
	rec := model.OrganizeRecord{LinkID: link, Status: "success", TmdbID: 550, MediaType: "movie", Title: "搏击俱乐部", Year: "1999",
		Files: marshalRecordFiles([]orgRecordFile{{Fid: "f", Name: "搏击俱乐部 (1999).mkv", Orig: "Fight.Club.1999.1080p.BluRay.mkv", Kind: "video", Size: 1 << 30}})}
	h.DB.Create(&rec)
	h.DB.Model(&rec).Update("updated_at", old)
	done := model.ShareOut{LinkID: link, Site: "re0", ShareCode: "swabc", TmdbID: 550, MediaType: "movie", Status: "done", Ref: "a1b2c3d4e5f647898765432112345678"}
	failed := model.ShareOut{LinkID: link, Site: "dian", ShareCode: "swabc", TmdbID: 550, MediaType: "movie", Status: "failed",
		Message: "站点挂了", Attempts: shareOutMaxAttempts}
	h.DB.Create(&done)
	h.DB.Create(&failed)

	get := func(q string) (out struct {
		Data   []shareOutRecordDTO `json:"data"`
		Total  int64               `json:"total"`
		Counts map[string]int64    `json:"counts"`
	}) {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/shareout/records"+q, nil))
		_ = json.Unmarshal(w.Body.Bytes(), &out)
		return
	}
	all := get("")
	if all.Total != 2 || all.Counts["all"] != 2 || all.Counts["done"] != 1 || all.Counts["failed"] != 1 {
		t.Fatalf("计数不对: %+v", all)
	}
	for _, d := range all.Data {
		if d.Title != "搏击俱乐部" || d.Year != "1999" || d.ShareName != "Fight.Club.1999.1080p" || d.Source != "web" {
			t.Errorf("片名 / 来源没带上: %+v", d)
		}
		if (d.Site == "re0") != (d.SiteURL != "") || d.CanRetry != (d.Status != "done") {
			t.Errorf("站上地址 / 能否重试不对: %+v", d)
		}
		if d.Status == "failed" && d.NextRetry != nil {
			t.Errorf("次数用完的不该有自动重试时间: %+v", d)
		}
	}
	if f := get("?status=failed"); f.Total != 1 || len(f.Data) != 1 || f.Data[0].Site != "dian" {
		t.Fatalf("按状态筛选不对: %+v", f)
	}

	post := func(id uint) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/shareout/records/"+strconv.Itoa(int(id))+"/retry", nil))
		return w
	}
	if w := post(done.ID); w.Code != http.StatusConflict {
		t.Fatalf("发成的不能重试，得到 %d", w.Code)
	}

	calls := 0
	oldPubs := shareOutPublishers
	t.Cleanup(func() { shareOutPublishers = oldPubs })
	shareOutPublishers = map[string]shareOutPublisher{
		shareOutSiteDian: func(shareOutPayload, shareOutSiteCfg) (string, int, error) {
			calls++
			return "", 0, &dianAPIErr{Status: 502, Msg: "还是挂着"}
		},
	}
	if w := post(failed.ID); w.Code != http.StatusAccepted {
		t.Fatalf("失败的应能重试，得到 %d %s", w.Code, w.Body.String())
	}
	for i := 0; i < 100 && shareOutRunning.Load(); i++ {
		time.Sleep(20 * time.Millisecond)
	}
	var after model.ShareOut
	h.DB.First(&after, failed.ID)
	if calls != 1 || after.Status != "failed" || after.Attempts != shareOutMaxAttempts || !strings.Contains(after.Message, "还是挂着") {
		t.Fatalf("手动重试应发一次、再失败就停住: calls=%d row=%+v", calls, after)
	}
	// 再失败之后不会被定时那一轮捡起来
	h.shareOutRun(time.Now().Add(time.Hour))
	if calls != 1 {
		t.Fatalf("手动重试失败后不该再自动重试: %d", calls)
	}
}

// 删除只是隐藏：列表与计数里没有了，但行还在——定时那一轮不重发、失败的不再自动重试、同一分享码照样去重
func TestShareOutDeleteHides(t *testing.T) {
	h := newShareOutEnv(t)
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/shareout/records", h.ShareOutRecords)
	r.POST("/shareout/records/delete", h.ShareOutDelete)
	r.POST("/shareout/records/:id/retry", h.ShareOutRetry)

	link := dlLinkRecord(h, "https://115cdn.com/s/swdel", "share", "Fight.Club.1999.1080p", "web", nil)
	h.DB.Model(&model.DownloadLink{}).Where("id = ?", link).Update("share_out", "re0,dian")
	rec := model.OrganizeRecord{LinkID: link, Status: "success", TmdbID: 550, MediaType: "movie", Title: "搏击俱乐部", Year: "1999",
		Files: marshalRecordFiles([]orgRecordFile{{Fid: "f", Name: "搏击俱乐部 (1999).mkv", Orig: "Fight.Club.1999.1080p.BluRay.mkv", Kind: "video", Size: 1 << 30}})}
	h.DB.Create(&rec)
	h.DB.Model(&rec).Update("updated_at", time.Now().Add(-time.Hour))
	done := model.ShareOut{LinkID: link, Site: "re0", ShareCode: "swdel", Status: "done"}
	failed := model.ShareOut{LinkID: link, Site: "dian", ShareCode: "swdel", Status: "failed", Attempts: 1}
	h.DB.Create(&done)
	h.DB.Create(&failed)

	call := func(method, path, body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		r.ServeHTTP(w, req)
		return w
	}
	if w := call(http.MethodPost, "/shareout/records/delete", `{}`); w.Code != http.StatusBadRequest {
		t.Fatalf("什么都没指定应拒绝，得到 %d", w.Code)
	}
	if w := call(http.MethodPost, "/shareout/records/delete", `{"ids":[`+strconv.Itoa(int(failed.ID))+`]}`); w.Code != http.StatusOK {
		t.Fatalf("删除失败: %d %s", w.Code, w.Body.String())
	}
	var list struct {
		Total  int64            `json:"total"`
		Counts map[string]int64 `json:"counts"`
	}
	_ = json.Unmarshal(call(http.MethodGet, "/shareout/records", "").Body.Bytes(), &list)
	if list.Total != 1 || list.Counts["all"] != 1 || list.Counts["failed"] != 0 {
		t.Fatalf("删掉的不该再列出: %+v", list)
	}
	if w := call(http.MethodPost, "/shareout/records/"+strconv.Itoa(int(failed.ID))+"/retry", ""); w.Code != http.StatusNotFound {
		t.Fatalf("删掉的不能重试，得到 %d", w.Code)
	}
	var n int64
	h.DB.Model(&model.ShareOut{}).Count(&n)
	if n != 2 {
		t.Fatalf("行应留着，只是隐藏: %d", n)
	}

	calls := 0
	oldPubs := shareOutPublishers
	t.Cleanup(func() { shareOutPublishers = oldPubs })
	pub := func(shareOutPayload, shareOutSiteCfg) (string, int, error) { calls++; return "x", 0, nil }
	shareOutPublishers = map[string]shareOutPublisher{shareOutSiteRe0: pub, shareOutSiteDian: pub}
	h.shareOutRun(time.Now().Add(2 * time.Hour))
	if calls != 0 {
		t.Fatalf("删掉的失败记录不该自动重试: %d", calls)
	}

	// 全部清空后，同一分享码换一条链接再贴，仍按「之前已经发过」跳过
	if w := call(http.MethodPost, "/shareout/records/delete", `{"status":"all"}`); w.Code != http.StatusOK {
		t.Fatalf("清空失败: %d", w.Code)
	}
	if !h.shareOutSentBefore("re0", "swdel", link+1) {
		t.Fatal("隐藏的发成记录仍要参与去重")
	}
}
