package api

import (
	"strings"
	"testing"

	"115-station/internal/model"
)

const gib = int64(1) << 30

func TestMatchWashSize(t *testing.T) {
	cases := []struct {
		size int64
		cond string
		want bool
	}{
		{20 * gib, "", true},
		{0, "<10G", true}, // 不知道体积不判
		{20 * gib, "<30G", true},
		{30 * gib, "<30G", false},
		{30 * gib, "<=30G", true},
		{3 * gib, ">=5G", false},
		{12 * gib, "5-30G", true},
		{12 * gib, "5G-10G", false},
		{12 * gib, "5G-10G,>11G", true},
		{900 << 20, "<1G", true},
		{2 * gib, "<1.5T", true},
		{12 * gib, "20", false}, // 写错的一段命中不了
		{12 * gib, "abc", false},
	}
	for _, c := range cases {
		if got := matchSize(c.size, c.cond); got != c.want {
			t.Errorf("matchSize(%s, %q) = %v，期望 %v", washSizeText(c.size), c.cond, got, c.want)
		}
	}
}

// 维护者的场景：1080p 规则排在 4K 前面，4K 那条优先留小体积
func TestWashSizePrefer(t *testing.T) {
	rules := []washRule{
		{ResourcePix: "1080p"},
		{ResourcePix: "2160p", SizePrefer: "small"},
	}
	big := washFile{Name: "Movie.2023.2160p.BluRay.DV.HDR.H265-A.mkv", Size: 60 * gib}
	small := washFile{Name: "Movie.2023.2160p.WEB-DL.H265-B.mkv", Size: 18 * gib}
	if c, why := washCompareFiles(small, big, rules); c <= 0 || !strings.Contains(why, "体积") {
		t.Fatalf("同命中 4K 那条应留小的：%d %s", c, why)
	}
	// 相差不到 5% 算一样大，回到默认画质：DV BluRay 赢
	near := washFile{Name: small.Name, Size: 59 * gib}
	if c, _ := washCompareFiles(near, big, rules); c >= 0 {
		t.Fatalf("体积相近应按画质比，得到 %d", c)
	}
	// 一边不知道体积：不比体积
	if c, _ := washCompareFiles(washFile{Name: small.Name}, big, rules); c >= 0 {
		t.Fatalf("不知道体积时应按画质比，得到 %d", c)
	}
	// 1080p 仍然排在 4K 前面
	hd := washFile{Name: "Movie.2023.1080p.BluRay.H264-C.mkv", Size: 10 * gib}
	if c, _ := washCompareFiles(hd, small, rules); c <= 0 {
		t.Fatalf("1080p 命中第 1 条，应赢")
	}
	// large
	rules[1].SizePrefer = "large"
	if c, _ := washCompareFiles(big, small, rules); c <= 0 {
		t.Fatalf("large 应留大的")
	}
}

func TestWashSizeCondTier(t *testing.T) {
	rules := []washRule{{ResourcePix: "2160p", ResourceSize: "<30G"}, {ResourcePix: "2160p"}}
	a := washFile{Name: "M.2160p.WEB-DL.mkv", Size: 20 * gib}
	b := washFile{Name: "M.2160p.BluRay.REMUX.mkv", Size: 70 * gib}
	if washTierOf(a, rules) != 0 || washTierOf(b, rules) != 1 {
		t.Fatalf("体积条件没生效：%d %d", washTierOf(a, rules), washTierOf(b, rules))
	}
	if c, _ := washCompareFiles(a, b, rules); c <= 0 {
		t.Fatal("命中第 1 条的应赢")
	}
}

// 整理洗版把台账里的体积带进来
func TestDecideWashSizePrefer(t *testing.T) {
	dir := "电影/Movie (2023)"
	lib := []model.SyncedFile{{FileID: "old", Kind: "video", Size: 60 * gib, RelPath: dir + "/Movie.2023.2160p.BluRay.DV.H265-A.strm"}}
	st := &washStrategy{Mode: "replace", MediaType: "movie", PriorityLevel: []washRule{{ResourcePix: "2160p", SizePrefer: "small"}}}
	var logs []string
	plan := decideWash(&TmdbMedia{MediaType: "movie", Title: "Movie"},
		washFile{Name: "Movie.2023.2160p.WEB-DL.H265-B.mkv", Size: 20 * gib}, "", dir, st, lib, nil,
		func(s string) { logs = append(logs, s) })
	if plan.decision != washReplaced || len(plan.victims) != 1 {
		t.Fatalf("小体积应顶掉大的：%s %v", plan.decision, logs)
	}
	if !strings.Contains(strings.Join(logs, "\n"), "20.0G < 60.0G") {
		t.Errorf("日志没写体积：%v", logs)
	}
}
