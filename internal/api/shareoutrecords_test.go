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
		if (d.Site == "re0") != (d.SiteURL != "") || d.CanRetry != (d.Status == "failed") {
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
