package api

import "testing"

// Emby 替认不出的集按文件名写的 NFO 算空壳；本站写的、Emby 刮到的、读不懂的都不算
func TestPlaceholderEpisodeNFO(t *testing.T) {
	emby := `<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails>
  <plot />
  <lockdata>false</lockdata>
  <dateadded>2026-10-17 19:24:10</dateadded>
  <title>.WEB-DL.HDR.H265.AAC</title>
  <episode>195</episode>
  <season>1</season>
</episodedetails>`
	if !placeholderEpisodeNFO([]byte(emby)) {
		t.Fatal("Emby 按文件名写的应算空壳")
	}
	ours, err := marshalEpisodeNFO([]nfoEpisode{{Season: 1, Episode: 195, Title: "慕兰之战19",
		UniqueIDs: []nfoUniqueID{{Type: "tmdb", Default: true, Value: "106449"}}}})
	if err != nil || placeholderEpisodeNFO(ours) {
		t.Fatalf("本站写的（哪怕没简介）不算空壳: %v", err)
	}
	embyScraped := `<episodedetails><plot>韩立……</plot><title>慕兰之战19</title><uniqueid type="tmdb">6543210</uniqueid></episodedetails>`
	if placeholderEpisodeNFO([]byte(embyScraped)) {
		t.Fatal("Emby 刮到了的不算空壳")
	}
	for _, s := range []string{"不是 XML", `<tvshow><title>x</title></tvshow>`} {
		if placeholderEpisodeNFO([]byte(s)) {
			t.Fatalf("读不懂 / 不是集 NFO 的不动: %s", s)
		}
	}
}
