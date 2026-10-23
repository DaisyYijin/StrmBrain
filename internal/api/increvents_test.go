package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// 消费时给每条事件记下去向：整理自产的 self、转存目录里的 outside、照常处理的留空。
// 最近事件列表靠它默认藏掉整理产生的事件
func TestIncrEventNotesWrittenOnConsume(t *testing.T) {
	h, d, p := newIncrTestEnv(t, "increvents_notes.db")
	d.settings["share"] = `{"folder":"w1"}`
	d.abs["w1"] = "/StrmStation/转存"

	markSuppressed("move", []string{"f-org"})
	d.pages = [][]lifeEvent{{
		{ID: "e-org", Type: evMove, FileID: "f-org", Cid: "d1", FileName: "整理搬的.mkv", PickCode: "pc-org", Time: "100"},
		{ID: "e-in", Type: evReceive, FileID: "f-in", Cid: "w1", FileName: "刚转存的.mkv", PickCode: "pc-in", Time: "101"},
		{ID: "e-lib", Type: evUpload, FileID: "f-lib", Cid: "d1", FileName: "X - S01E01.mkv", PickCode: "pc-lib", Time: "102"},
	}}
	if _, err := h.executeIncrementalSyncWith(d, p); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"e-org": "self", "e-in": "outside", "e-lib": ""}
	for id, n := range want {
		var ev model.SyncEvent
		if err := model.DB.Where("event_id = ?", id).First(&ev).Error; err != nil {
			t.Fatal(err)
		}
		if ev.Status != "applied" || ev.Note != n {
			t.Fatalf("%s：期望 applied / %q，实得 %s / %q", id, n, ev.Status, ev.Note)
		}
	}
}

func TestIncrEventsListFiltersAndHidesSelf(t *testing.T) {
	newTestDB(t, "increvents_list.db")
	rows := []model.SyncEvent{
		{EventID: "1", Type: evUpload, FileName: "a.mkv", EventTime: 100, Status: "applied"},
		{EventID: "2", Type: evMove, FileName: "b.mkv", EventTime: 200, Status: "applied", Note: "self"},
		{EventID: "3", Type: evDelete, FileName: "c.mkv", EventTime: 300, Status: "pending"},
		{EventID: "4", Type: evRename, FileName: "d.mkv", EventTime: 400, Status: "applied"},
	}
	if err := model.DB.Create(&rows).Error; err != nil {
		t.Fatal(err)
	}
	h := &Handler{DB: model.DB}
	get := func(q string) (out struct {
		Data   []incrEventRow   `json:"data"`
		Total  int64            `json:"total"`
		Counts map[string]int64 `json:"counts"`
		Hidden int64            `json:"hidden"`
	}) {
		gin.SetMode(gin.TestMode)
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest(http.MethodGet, "/sync/incr-events?"+q, nil)
		h.IncrEvents(c)
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
			t.Fatal(err)
		}
		return
	}

	all := get("hide_self=1")
	if all.Total != 3 || all.Hidden != 1 || all.Counts["move"] != 1 || all.Counts["all"] != 3 {
		t.Fatalf("隐藏整理事件后应剩 3 条、隐藏 1 条、移动类 1 条，实得 %+v", all)
	}
	if all.Data[0].Name != "d.mkv" {
		t.Fatalf("应按事件时间倒序，第一条实得 %s", all.Data[0].Name)
	}
	if got := get("group=move"); got.Total != 2 {
		t.Fatalf("不隐藏时移动 / 改名应有 2 条，实得 %d", got.Total)
	}
	if got := get("group=del&hide_self=1"); got.Total != 1 || got.Data[0].Status != "pending" {
		t.Fatalf("删除类应只有待处理那条，实得 %+v", got.Data)
	}
}

// 空转轮次只计数，有事的才留下，且只留最近 incrRoundKeep 轮
func TestIncrRoundHistKeepsBusyRounds(t *testing.T) {
	r := &incrRoundHist{}
	now := time.Now()
	r.note(now, &incrSummary{Round: 1, Consumed: true}, "")
	r.note(now, &incrSummary{Round: 2, Consumed: true, EventsPending: 3}, "")
	r.note(now, &incrSummary{Round: 3, Consumed: false, NotConsumed: "目录读不到"}, "")
	r.note(now, &incrSummary{Round: 4}, "拉取失败")
	rows, total, idle, _ := r.snapshot()
	if total != 4 || idle != 1 || len(rows) != 3 || rows[0].Sum.Round != 4 || rows[0].Error == "" {
		t.Fatalf("实得 total=%d idle=%d rows=%d", total, idle, len(rows))
	}
	for i := 0; i < incrRoundKeep+10; i++ {
		r.note(now, &incrSummary{Consumed: true, EventsPending: 1}, "")
	}
	if rows, _, _, _ := r.snapshot(); len(rows) != incrRoundKeep {
		t.Fatalf("最多留 %d 轮，实得 %d", incrRoundKeep, len(rows))
	}
}

func TestDeepDelEpisodeLabel(t *testing.T) {
	cases := []struct {
		rels []string
		want string
	}{
		{[]string{"电影/华语/流浪地球2 (2023)/流浪地球2 (2023).strm"}, ""},
		{[]string{"剧集/X/Season 02/X - S02E05.strm", "剧集/X/Season 02/X - S02E05.nfo"}, "S02E05"},
		{[]string{"剧集/X/Season 02/X - S02E03.strm", "剧集/X/Season 02/X - S02E01.strm"}, "S02E01–E03"},
		{[]string{"剧集/X/Season 1/01.strm", "剧集/X/Season 1/02.strm"}, "S01"},
		{[]string{"剧集/X/Season 01/X - S01E01.strm", "剧集/X/Season 02/X - S02E01.strm"}, "S01–S02"},
	}
	for _, c := range cases {
		if got := deepDelEpisodeLabel(c.rels); got != c.want {
			t.Errorf("%v：期望 %q，实得 %q", c.rels, c.want, got)
		}
	}
}
