package api

import (
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"
)

// V2 阶段 5：转存放进哪个库（xfertarget.go）

func TestXferLibPick(t *testing.T) {
	newTestDB(t, "xfer.db")
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	h := &Handler{DB: model.DB, Config: cfg}
	one := []model.Library{{ID: 1, Name: "电影", Cid: "100", StagingCid: "s1"}, {ID: 2, Name: "备份", Cid: "200"}}
	if l, err := h.xferLib("", one); err != nil || l.ID != 1 {
		t.Fatalf("只有一个要整理的库：直接放进它：%+v %v", l, err)
	}
	two := append(one, model.Library{ID: 3, Name: "电影·副号", Account: 2, Cid: "300", StagingCid: "s3"})
	if _, err := h.xferLib("", two); err != errXferNeedLib {
		t.Fatalf("多个库、没设默认：要问：%v", err)
	}
	if l, err := h.xferLib("lib:3", two); err != nil || l.ID != 3 || xferTargetOf(l) != "a2:s3" {
		t.Fatalf("这次选了：%+v %v", l, err)
	}
	if _, err := h.xferLib("lib:2", two); err == nil {
		t.Fatal("没设转存文件夹的库不能选")
	}
	_ = cfg.SaveSetting(xferTargetKey, `{"lib":3}`)
	if l, err := h.xferLib("", two); err != nil || l.ID != 3 {
		t.Fatalf("设了默认：%+v %v", l, err)
	}
	if acct, cid := splitXferTarget("a2:s3"); acct != 2 || cid != "s3" {
		t.Fatalf("拆目标：%d %s", acct, cid)
	}
	if acct, cid := splitXferTarget("s1"); acct != acctMain || cid != "s1" {
		t.Fatalf("主号目标：%d %s", acct, cid)
	}
	// 订阅只进主号的库：默认是副号的库时退回主号第一个
	if l, err := h.subTargetLib("", "", two); err != nil || l.ID != 1 {
		t.Fatalf("订阅目标：%+v %v", l, err)
	}
	if l, err := h.subTargetLib("lib:3", "", two); err != nil || l.ID != 1 {
		t.Fatalf("订阅选了副号的库也不认：%+v %v", l, err)
	}
}
