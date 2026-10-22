package api

import (
	"reflect"
	"testing"
	"time"

	"115-station/internal/model"
)

// 规则里的词按词匹配：DV 不命中 DVDRip、!TS 不排掉 Ghosts；归一值里有的照样命中（WEB → WEBRip、HDR → HDR10）
func TestRuleMatchToken(t *testing.T) {
	cases := []struct {
		name string
		rule washRule
		want bool
	}{
		{"Movie.2010.1080p.DVDRip.x264.mkv", washRule{ResourceEffect: "DV"}, false},
		{"Movie.2010.2160p.WEB-DL.DV.H265.mkv", washRule{ResourceEffect: "DV"}, true},
		{"Ghosts.S01E01.1080p.WEB-DL.mkv", washRule{ResourceType: "!TS"}, true},
		{"Movie.2010.TS.mkv", washRule{ResourceType: "!TS"}, false},
		{"Show.S01E01.1080p.WEBRip.mkv", washRule{ResourceType: "WEB"}, true},
		{"Movie.2010.2160p.WEB-DL.HDR10.mkv", washRule{ResourceEffect: "HDR"}, true},
		{"Movie.2010.2160p.WEB-DL.DV.HDR.mkv", washRule{ResourceEffect: "!DV.HDR,!DV"}, false},
		{"三体 2023 4K 杜比视界", washRule{ResourcePix: "2160p"}, true},
	}
	for _, c := range cases {
		if got := ruleMatch(c.name, c.rule); got != c.want {
			t.Errorf("%s / %+v: got %v", c.name, c.rule, got)
		}
	}
}

func TestSubPriority(t *testing.T) {
	p := subPriority{Rules: []washRule{{ResourcePix: "2160p", ResourceEffect: "DV"}, {ResourcePix: "2160p,1080p"}}}
	if !p.top("剧.S01E01.2160p.WEB-DL.DV.H265.mkv") || p.top("剧.S01E01.2160p.WEB-DL.H265.mkv") {
		t.Fatal("达标 = 命中第 1 条")
	}
	// 同命中第 2 条：再按默认画质比，2160p 赢
	if p.compare("剧.S01E01.2160p.WEB-DL.mkv", "剧.S01E01.1080p.WEB-DL.mkv") <= 0 {
		t.Fatal("同一条规则里 2160p 应更好")
	}
	// 没有规则：只按默认画质，也没有达标一说
	basic := subPriority{}
	if basic.top("剧.2160p.REMUX.DV.H265.mkv") {
		t.Fatal("没有规则不该达标")
	}
	if basic.compare("剧.1080p.BluRay.mkv", "剧.1080p.WEB-DL.mkv") <= 0 {
		t.Fatal("基础：BluRay 应好过 WEB-DL")
	}
}

// 都不达标：先补集数、再看画质；有达标的排最前
func TestSortSubCandidatesTop(t *testing.T) {
	now := time.Date(2026, 10, 7, 12, 0, 0, 0, time.Local)
	sub := &model.Subscription{ID: 1, MediaType: "tv"}
	share := func(title, code string) ResourceItem {
		return ResourceItem{Source: "pansou", Kind: "share115", Action: "transfer", Title: title, URL: "https://115.com/s/" + code, Relevant: true}
	}
	items := []ResourceItem{
		share("剧 S02E05 1080p WEB-DL", "a"),
		share("剧 S02E05-E06 1080p WEBRip", "b"),
		share("剧 S02E05 2160p WEB-DL", "c"),
		share("剧 S02E06 1080p BluRay", "d"),
	}
	prio := subPriority{Rules: []washRule{{ResourcePix: "2160p"}, {ResourcePix: "1080p"}}}
	cands, _ := planSubCandidates(items, subPickCtx{Sub: sub, Missing: epKeys("S02E05", "S02E06"), Now: now, Re0Left: -1, Prio: prio})
	var got []string
	for _, c := range cands {
		got = append(got, c.Item.URL[len("https://115.com/s/"):])
	}
	// c 达标排第一；b 补两集；a / d 都补一集、同命中第 2 条，按片源 BluRay > WEB-DL
	if want := []string{"c", "b", "d", "a"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("顺序 %v, want %v", got, want)
	}
}

// 分享里同一集几份：同命中一条规则时按默认画质挑，不再只看命中第几条
func TestPickShareEpisodesCompare(t *testing.T) {
	entries := []shareEntry{
		fileE("lo", "剧.S01E01.1080p.WEB-DL.mkv", 900),
		fileE("hi", "剧.S01E01.2160p.WEB-DL.mkv", 500),
	}
	prio := subPriority{Rules: []washRule{{ResourceType: "WEB-DL"}}}
	p := pickShareEpisodes(entries, sharePickOpts{MediaType: "tv", Missing: map[epKey]bool{{1, 1}: true}, Compare: prio.compare})
	if got := pickIDs(p); !reflect.DeepEqual(got, []string{"hi"}) {
		t.Fatalf("挑中 %v", got)
	}
}
