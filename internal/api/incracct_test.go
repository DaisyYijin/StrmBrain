package api

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"115-station/internal/model"
)

// V2 阶段 6：别的账号的生活事件增量（incracct.go）

// acctStub 给桩加上「哪个账号」
type acctStub struct {
	*stubIncrDeps
	a uint
}

func (s acctStub) account() uint { return s.a }

func TestIncrOtherAccountRound(t *testing.T) {
	h, d, p := newIncrTestEnv(t, "incr_acct.db")
	forgetLibraries()
	t.Cleanup(forgetLibraries)
	model.DB.Create(&model.Library{Name: "电影", Cid: "lib", Local: "媒体库"})
	model.DB.Create(&model.Library{Name: "电影·副号", Account: 2, Cid: "lib2", Local: "电影/副号"})
	d.abs["lib2"] = "/影视2"
	d.abs["d2"] = "/影视2/X"
	d.rel["d2"] = "X"
	// 主号还有一条没消费的事件：副号这一轮不许碰它
	model.DB.Create(&model.SyncEvent{EventID: "m1", Type: evUpload, FileID: "fm", Cid: "d1", FileName: "主号.mkv", EventTime: 50})

	d.pages = [][]lifeEvent{{
		{ID: "e1", Type: evUpload, FileID: "f2", Cid: "d2", FileName: "副号片.mkv", PickCode: "pc2", Time: "200"},
	}}
	sum, err := h.executeIncrementalSyncWith(acctStub{d, 2}, p)
	if err != nil {
		t.Fatal(err)
	}
	if sum.EventsPending != 1 {
		t.Fatalf("只处理副号自己的事件：%d", sum.EventsPending)
	}
	var ev model.SyncEvent
	if model.DB.Where("event_id = ?", "a2:e1").First(&ev).Error != nil || ev.Account != 2 || ev.Status != "applied" {
		t.Fatalf("副号事件要带前缀与账号、消费掉：%+v", ev)
	}
	var mainEv model.SyncEvent
	model.DB.Where("event_id = ?", "m1").First(&mainEv)
	if mainEv.Status != "pending" {
		t.Fatal("主号的事件被副号那一轮消费了")
	}
	strm := filepath.Join(p.LocalPath, filepath.FromSlash("电影/副号/X/副号片.strm"))
	b, err := os.ReadFile(strm)
	if err != nil {
		t.Fatalf("副号的 STRM 落在它的本地目录下：%v", err)
	}
	if !strings.Contains(string(b), "2~pc2") {
		t.Fatalf("直链要带账号：%s", b)
	}
	var sf model.SyncedFile
	if model.DB.Where("file_id = ?", "f2").First(&sf).Error != nil || sf.Account != 2 {
		t.Fatalf("台账记账号：%+v", sf)
	}
}

func TestAcctRuntimeKey(t *testing.T) {
	if acctRuntimeKey(acctMain, "incr-cursor") != "incr-cursor" || acctRuntimeKey(2, "incr-cursor") != "incr-cursor@2" {
		t.Fatal("游标按账号分开存")
	}
	if acctRuntimeKey(2, "org-basic") != "org-basic" {
		t.Fatal("配置不分账号")
	}
}
