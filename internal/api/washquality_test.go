package api

import (
	"strings"
	"testing"

	"115-station/internal/model"
)

// 维护者 2026-10-20 现场的规则：分辨率写成「2160p,1080p」，DV 只出现在 REMUX 那条里
var userWashRules = []washRule{
	{ResourcePix: "2160p,1080p", ResourceType: "REMUX", ResourceEffect: "DV.HDR,DV,HDR10+,HDR"},
	{ResourcePix: "2160p,1080p", ResourceType: "REMUX"},
	{ResourcePix: "2160p,1080p", ResourceType: "WEB-DL"},
}

type washCase struct {
	a, b string
	want int // >0 a 更好，<0 b 更好，0 分不出
}

func checkWashCases(t *testing.T, rules []washRule, cases []washCase) {
	t.Helper()
	for _, c := range cases {
		got, why := washCompare(c.a, c.b, rules)
		if washSign(got) != washSign(c.want) {
			t.Errorf("washCompare(%q, %q) = %d（%s），want %d", c.a, c.b, got, why, c.want)
		}
		// 反过来比结果要相反，否则两份会来回互相顶
		if back, _ := washCompare(c.b, c.a, rules); washSign(back) != -washSign(c.want) {
			t.Errorf("washCompare(%q, %q) 反过来 = %d，不对称", c.b, c.a, back)
		}
	}
}

func washSign(n int) int {
	switch {
	case n > 0:
		return 1
	case n < 0:
		return -1
	}
	return 0
}

// 现场：两份都只命中第 3 条，原来平局不换，2160p DV 被移进「已存在」
func TestWashSameTierFallsBackToQuality(t *testing.T) {
	checkWashCases(t, userWashRules, []washCase{
		{"Cyberpunk.Edgerunners.S02E01.2022.Last.Chance.2160p.NF.WEB-DL.DDP5.1.DV.H.265-SalmonWeb.mkv",
			"赛博朋克：边缘行者.Cyberpunk： Edgerunners.S02E01.1080p.NF.WEB-DL.DDP5.1.H.264-FLUX.mkv", 1},
		// 同一条里同为 2160p：DV > HDR10+ > HDR10 > HDR > 没写
		{"片.2160p.WEB-DL.DV.H265.mkv", "片.2160p.WEB-DL.HDR10+.H265.mkv", 1},
		{"片.2160p.WEB-DL.HDR10+.H265.mkv", "片.2160p.WEB-DL.HDR10.H265.mkv", 1},
		{"片.2160p.WEB-DL.HDR10.H265.mkv", "片.2160p.WEB-DL.HDR.H265.mkv", 1},
		{"片.2160p.WEB-DL.HDR.H265.mkv", "片.2160p.WEB-DL.H265.mkv", 1},
		// 分辨率先于 HDR：1080p DV 比不过 2160p 没写 HDR 的
		{"片.1080p.WEB-DL.DV.H265.mkv", "片.2160p.WEB-DL.H264.mkv", -1},
		// 都一样：分不出，不换
		{"片.2160p.WEB-DL.DV.H265-A.mkv", "片.2160p.WEB-DL.DV.H265-B.mkv", 0},
	})
}

// 规则分高的赢，不看画质：命中第 2 条的 1080p REMUX 赢命中第 3 条的 2160p DV WEB-DL
func TestWashTierBeatsQuality(t *testing.T) {
	checkWashCases(t, userWashRules, []washCase{
		{"片.1080p.BluRay.REMUX.AVC.mkv", "片.2160p.WEB-DL.DV.H265.mkv", 1},
		{"片.2160p.BluRay.REMUX.DV.HEVC.mkv", "片.2160p.BluRay.REMUX.HEVC.mkv", 1}, // 第 1 条 > 第 2 条
		{"片.720p.WEB-DL.mkv", "片.1080p.WEB-DL.mkv", -1},                          // 720p 一条都没命中
		// 同在第 1 条：DV > HDR10（HDR10 靠「HDR」这个值命中第 1 条）
		{"片.2160p.BluRay.REMUX.DV.HDR.HEVC.mkv", "片.2160p.BluRay.REMUX.HDR10.HEVC.mkv", 1},
	})
}

// 规则里写了的字段，最多按命中的值算：没写 DV 就不让 DV 占便宜
func TestWashRuleCapsDimension(t *testing.T) {
	rules := []washRule{{ResourcePix: "2160p", ResourceEffect: "HDR10+,HDR"}}
	checkWashCases(t, rules, []washCase{
		// DV.HDR 只命中「HDR」，按 HDR 算，输给 HDR10+
		{"片.2160p.WEB-DL.HDR10+.mkv", "片.2160p.WEB-DL.DV.HDR.mkv", 1},
		// 纯 DV 一条规则都命中不了，输给任何命中的
		{"片.2160p.WEB-DL.HDR.mkv", "片.2160p.WEB-DL.DV.mkv", 1},
		// HDR10 也只命中「HDR」，与 DV.HDR 一样按 HDR 算，再往后比编码
		{"片.2160p.WEB-DL.HDR10.H265.mkv", "片.2160p.WEB-DL.DV.HDR.H264.mkv", 1},
		{"片.2160p.WEB-DL.HDR10.H265.mkv", "片.2160p.WEB-DL.DV.HDR.H265.mkv", 0},
	})
	// 规则里没写的字段照默认排序：片源 REMUX > BluRay > WEB-DL > WEBRip
	checkWashCases(t, []washRule{{ResourcePix: "2160p,1080p"}}, []washCase{
		{"片.1080p.BluRay.REMUX.mkv", "片.1080p.BluRay.mkv", 1},
		{"片.1080p.BluRay.mkv", "片.1080p.WEB-DL.mkv", 1},
		{"片.1080p.WEB-DL.mkv", "片.1080p.WEBRip.mkv", 1},
	})
	// 只写了排除词的字段也照默认排序
	checkWashCases(t, []washRule{{ResourcePix: "2160p", ResourceEffect: "!DV"}}, []washCase{
		{"片.2160p.HDR10+.mkv", "片.2160p.HDR.mkv", 1},
		{"片.2160p.HDR.mkv", "片.2160p.DV.mkv", 1}, // DV 被排除，命中不了规则
	})
	// 默认排序认不出的值（UHD）不设上限
	checkWashCases(t, []washRule{{ResourceType: "UHD,BluRay"}}, []washCase{
		{"片.2160p.UHD.BluRay.REMUX.mkv", "片.2160p.UHD.BluRay.mkv", 0}, // 上限是 BluRay：REMUX 不额外加分
		{"片.2160p.UHD.BluRay.DV.mkv", "片.2160p.UHD.BluRay.mkv", 1},
	})
}

// 逗号里的先后不参与比较
func TestWashCommaOrderIrrelevant(t *testing.T) {
	a, b := "片.2160p.WEB-DL.mkv", "片.1080p.WEB-DL.mkv"
	for _, pix := range []string{"2160p,1080p", "1080p,2160p"} {
		got, why := washCompare(a, b, []washRule{{ResourcePix: pix, ResourceType: "WEB-DL"}})
		if got <= 0 {
			t.Errorf("resource_pix=%q：2160p 应胜 1080p，得到 %d（%s）", pix, got, why)
		}
	}
}

// 用户把 1080p 那条排在上面，就是要 1080p：规则分优先于默认的 2160p > 1080p
func TestWashUserTierOrderWins(t *testing.T) {
	checkWashCases(t, []washRule{{ResourcePix: "1080p"}, {ResourcePix: "2160p"}}, []washCase{
		{"片.1080p.WEB-DL.mkv", "片.2160p.BluRay.DV.mkv", 1},
	})
}

// 没配优先级：只有默认画质这一层（MediaSync115 同序：分辨率 → 片源 → HDR → 编码）
func TestWashDefaultQualityOrder(t *testing.T) {
	checkWashCases(t, nil, []washCase{
		{"片.2160p.WEBRip.mkv", "片.1080p.BluRay.REMUX.DV.mkv", 1},
		{"片.4K.WEB-DL.mkv", "片.2160p.WEB-DL.mkv", 0}, // 4K 就是 2160p
		{"片.1080p.BluRay.mkv", "片.1080p.WEB-DL.DV.mkv", 1},
		{"片.1080p.WEB-DL.DoVi.mkv", "片.1080p.WEB-DL.HDR10+.mkv", 1}, // DoVi 也认作 DV
		{"片.1080p.WEB-DL.HDR.x265.mkv", "片.1080p.WEB-DL.HDR.x264.mkv", 1},
		{"片.1080p.WEB-DL.AV1.mkv", "片.1080p.WEB-DL.H264.mkv", 1},
		// 分辨率 / 片源 / 编码有一边没写：不知道，不算更差
		{"片.1080p.mkv", "片.mkv", 0},
		{"片.1080p.BluRay.REMUX.H264.mkv", "片.1080p.BluRay.REMUX.mkv", 0},
		{"片.2160p.mkv", "片.1080p.BluRay.mkv", 1}, // 片源没写不比，分辨率照比
		{"片.mkv", "另一个.mkv", 0},
	})
	// 有规则但两份都没命中：同样按默认画质比
	checkWashCases(t, []washRule{{ResourceTeam: "WiKi"}}, []washCase{
		{"片.2160p.WEB-DL.mkv", "片.1080p.WEB-DL.mkv", 1},
	})
}

// 日志里要说清楚为什么：用户看「不优于」时得知道卡在哪一项
func TestWashCompareReason(t *testing.T) {
	for _, c := range []struct {
		a, b  string
		rules []washRule
		want  []string
	}{
		{"片.2160p.WEB-DL.DV.mkv", "片.1080p.WEB-DL.mkv", userWashRules, []string{"都命中第 3 条规则", "分辨率", "2160p > 1080p"}},
		{"片.1080p.WEB-DL.mkv", "片.1080p.BluRay.REMUX.mkv", userWashRules, []string{"命中第 3 条规则", "对方命中第 2 条规则"}},
		{"片.720p.WEB-DL.mkv", "片.1080p.WEB-DL.mkv", userWashRules, []string{"没命中规则", "对方命中第 3 条规则"}},
		{"片.2160p.WEB-DL.mkv", "片.2160p.WEB-DL.mkv", userWashRules, []string{"都命中第 3 条规则", "画质也分不出"}},
		{"片.1080p.WEB-DL.mkv", "片.1080p.BluRay.mkv", nil, []string{"片源", "WEB-DL < BluRay"}},
		{"片.1080p.WEB-DL.mkv", "片.1080p.WEB-DL.HDR.mkv", nil, []string{"HDR", "未标明 < HDR"}},
	} {
		_, why := washCompare(c.a, c.b, c.rules)
		for _, w := range c.want {
			if !strings.Contains(why, w) {
				t.Errorf("washCompare(%q, %q) 理由 %q 缺少 %q", c.a, c.b, why, w)
			}
		}
	}
}

// 整条判定：现场那一集走 decideWash 应判洗版、让位库里的 1080p 与它的字幕，别的集不动
func TestDecideWashUserScenario(t *testing.T) {
	season := "动漫番剧/赛博朋克：边缘行者.2022.{tmdbid=105248}/Season 2/"
	lib := []model.SyncedFile{
		{FileID: "e1", Kind: "video", RelPath: season + "赛博朋克：边缘行者.Cyberpunk： Edgerunners.S02E01.1080p.NF.WEB-DL.DDP5.1.H.264-FLUX.strm"},
		{FileID: "s1", Kind: "asset", RelPath: season + "赛博朋克：边缘行者.Cyberpunk： Edgerunners.S02E01.1080p.NF.WEB-DL.DDP5.1.H.264-FLUX.chs.ass"},
		{FileID: "e3", Kind: "video", RelPath: season + "赛博朋克：边缘行者.Cyberpunk： Edgerunners.S02E03.1080p.NF.WEB-DL.DDP5.1.H.264-FLUX.strm"},
	}
	st := &washStrategy{Mode: "replace", Scope: "all", MediaType: "tv", OldVersionTarget: "delete", PriorityLevel: userWashRules}
	media := &TmdbMedia{MediaType: "tv", Title: "赛博朋克：边缘行者"}
	var logs []string
	plan := decideWash(media, "Cyberpunk.Edgerunners.S02E01.2022.Last.Chance.2160p.NF.WEB-DL.DDP5.1.DV.H.265-SalmonWeb.mkv",
		"", strings.TrimSuffix(season, "/"), st, lib, nil, func(s string) { logs = append(logs, s) })
	if plan.decision != washReplaced {
		t.Fatalf("应判洗版，得到 %s；日志 %v", plan.decision, logs)
	}
	var ids []string
	for _, v := range plan.victims {
		ids = append(ids, v.FileID)
	}
	if strings.Join(ids, ",") != "e1,s1" {
		t.Errorf("让位应只有第 1 集与它的字幕，得到 %v", ids)
	}
	if !strings.Contains(strings.Join(logs, "\n"), "2160p > 1080p") {
		t.Errorf("日志没写清理由：%v", logs)
	}

	// 反过来：库里是 2160p DV，新来 1080p，按已存在处理，日志说明卡在分辨率
	lib[0].RelPath = season + "赛博朋克：边缘行者.S02E01.2160p.NF.WEB-DL.DV.H.265-SalmonWeb.strm"
	logs = nil
	plan = decideWash(media, "Cyberpunk.Edgerunners.S02E01.1080p.NF.WEB-DL.H.264-FLUX.mkv",
		"", strings.TrimSuffix(season, "/"), st, lib, nil, func(s string) { logs = append(logs, s) })
	if plan.decision != washNotBetter {
		t.Fatalf("1080p 不该顶掉 2160p，得到 %s", plan.decision)
	}
	if !strings.Contains(strings.Join(logs, "\n"), "1080p < 2160p") {
		t.Errorf("日志没写清理由：%v", logs)
	}
}
