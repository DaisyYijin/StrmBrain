package api

import (
	"errors"
	"testing"
	"time"

	"115-station/internal/config"
	"115-station/internal/model"
)

// 每个库上一次全量的结果（libsyncstat.go）

func TestLibSyncStatOf(t *testing.T) {
	start := time.Now().Add(-90 * time.Second)
	r := fullLibResult{modeUsed: "fast", complete: true, videos: 10, orphans: 2,
		st: applyStats{StrmCreated: 3, StrmFailed: 1, AssetsDownloaded: 4}}
	s := libSyncStatOf(r, nil, start)
	if !s.OK || s.Mode != "fast" || s.Videos != 10 || s.Created != 3 || s.Failed != 1 || s.Assets != 4 || s.Orphans != 2 || !s.Complete || s.Seconds < 89 {
		t.Fatalf("成功：%+v", s)
	}
	bad := libSyncStatOf(r, errors.New("读不出目录名"), start)
	if bad.OK || bad.Error != "读不出目录名" || bad.Videos != 0 {
		t.Fatalf("失败只记原因，不带半截数字：%+v", bad)
	}
	if libSyncStatKey(fullTarget{cid: "100"}) != "100" || libSyncStatKey(fullTarget{acct: 2, cid: "9"}) != "a2:9" {
		t.Fatal("key 与库卡片同一套")
	}
}

func TestSaveLibSyncStatsMergesAndPrunes(t *testing.T) {
	newTestDB(t, "libstat.db")
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	h := &Handler{DB: model.DB, Config: cfg}
	_ = cfg.SaveSetting("full", `{"cid":"100","extra_libs":[{"cid":"200"}]}`)
	_ = cfg.SaveSetting(acctLibsKey, `[{"account":2,"cid":"9","local":"副号"}]`)
	// 老记录里有一个已经移出配置的库
	_ = cfg.SaveSetting(libSyncStatsKey, `{"200":{"at":1,"ok":true,"videos":5},"300":{"at":1,"ok":true}}`)

	h.saveLibSyncStats(map[string]libSyncStat{"100": {At: 2, OK: true, Videos: 7}, "a2:9": {At: 2, Error: "x"}})
	got := parseLibSyncStats(cfg.GetSetting(libSyncStatsKey))
	if got["100"].Videos != 7 || got["200"].Videos != 5 || got["a2:9"].Error != "x" {
		t.Fatalf("应合并（这次没同步的库留着上次的）：%+v", got)
	}
	if _, ok := got["300"]; ok {
		t.Fatalf("移出配置的库应去掉：%+v", got)
	}
}
