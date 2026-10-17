package api

import (
	"strings"
	"testing"
	"time"

	"115-station/internal/mediaprobe"
	"115-station/internal/model"
)

func useProbeCfg(t *testing.T, value string) {
	t.Helper()
	newTestDB(t, "probe.db")
	if err := model.DB.Create(&model.Setting{Key: "media-probe", Value: value}).Error; err != nil {
		t.Fatal(err)
	}
	prev := renameTpl
	renameTpl = defaultRenameConfig()
	t.Cleanup(func() { renameTpl = prev })
}

func uhdISOInfo() *mediaprobe.Info {
	return &mediaprobe.Info{
		Container: "bluray", Disc: "uhd-bluray",
		Video: &mediaprobe.Video{Codec: "hevc", Width: 3840, Height: 2160, BitDepth: 10, FPS: 23.976, Transfer: "pq", DV: true},
		Audios: []mediaprobe.Audio{
			{Codec: "lpcm", Channels: 2, Default: true},
			{Codec: "truehd"},
		},
	}
}

func TestProbeResourceMapping(t *testing.T) {
	ri := probeResource(uhdISOInfo())
	want := ResourceInfo{Type: "UHD.BluRay", Pix: "2160p", Effect: "DV.HDR10", VideoEncode: "H265.10bit", AudioEncode: "TrueHD", FPS: "23.976FPS"}
	if ri != want {
		t.Fatalf("光盘：主音轨挑最好的、声道看不出不写\n got %+v\nwant %+v", ri, want)
	}

	mkv := &mediaprobe.Info{Container: "mkv",
		Video: &mediaprobe.Video{Codec: "h264", Width: 1920, Height: 800, BitDepth: 8, FPS: 24},
		Audios: []mediaprobe.Audio{
			{Codec: "dtshd_ma", Channels: 8},
			{Codec: "ac3", Channels: 6, Default: true},
		},
	}
	ri = probeResource(mkv)
	want = ResourceInfo{Pix: "1080p", Effect: "SDR", VideoEncode: "H264", AudioEncode: "DD.5.1", FPS: "24FPS"}
	if ri != want {
		t.Fatalf("普通文件：裁过黑边按宽度算、主音轨按默认轨、8 bit 没写传输特性算 SDR\n got %+v\nwant %+v", ri, want)
	}

	dvd := &mediaprobe.Info{Container: "dvd", Disc: "dvd",
		Video: &mediaprobe.Video{Codec: "mpeg2", Width: 720, Height: 576, BitDepth: 8, FPS: 25, Transfer: "sdr"},
		Audios: []mediaprobe.Audio{{Codec: "ac3", Channels: 6, Default: true}, {Codec: "dts", Channels: 6}},
	}
	ri = probeResource(dvd)
	want = ResourceInfo{Pix: "576p", Effect: "SDR", VideoEncode: "MPEG2", AudioEncode: "DTS.5.1", FPS: "25FPS"}
	if ri != want {
		t.Fatalf("DVD：不写资源质量，主音轨挑最好的\n got %+v\nwant %+v", ri, want)
	}

	// 10 bit 没读到传输特性：不下结论
	if e := probeEffect(&mediaprobe.Video{Codec: "hevc", BitDepth: 10}); e != "" {
		t.Fatalf("10 bit 传输特性未知不该写特效: %q", e)
	}
	// 杜比视界档位 5 没有兼容层
	if e := probeEffect(&mediaprobe.Video{DV: true, DVProfile: 5}); e != "DV" {
		t.Fatalf("档位 5 只写 DV: %q", e)
	}
}

// 读到的值写进文件名之后，按文件名解析要得到同样的值：洗版、资源条件、库内体检都按文件名解析
func TestProbeResourceRoundTrip(t *testing.T) {
	audios := []mediaprobe.Audio{
		{Codec: "truehd", Channels: 8, Atmos: true}, {Codec: "dtshd_ma", Channels: 8}, {Codec: "dtshd", Channels: 6},
		{Codec: "dtsx", Channels: 8}, {Codec: "dts", Channels: 6}, {Codec: "eac3", Channels: 6, Atmos: true},
		{Codec: "ac3", Channels: 2}, {Codec: "aac", Channels: 2}, {Codec: "flac", Channels: 2}, {Codec: "lpcm", Channels: 8},
	}
	videos := []mediaprobe.Video{
		{Codec: "hevc", Width: 3840, BitDepth: 10, Transfer: "pq", DV: true, FPS: 23.976},
		{Codec: "hevc", Width: 3840, BitDepth: 10, HDR10Plus: true, FPS: 24},
		{Codec: "hevc", Width: 1920, BitDepth: 10, Transfer: "hlg", FPS: 50},
		{Codec: "h264", Width: 1280, BitDepth: 8, FPS: 29.97},
		{Codec: "av1", Width: 1920, BitDepth: 10, Transfer: "sdr", FPS: 25},
		{Codec: "vc1", Width: 1920, Transfer: "sdr", FPS: 23.976},
		{Codec: "mpeg2", Width: 720, Height: 576, Transfer: "sdr", FPS: 25},
	}
	for _, v := range videos {
		for _, a := range audios {
			v, a := v, a
			ri := probeResource(&mediaprobe.Info{Video: &v, Audios: []mediaprobe.Audio{a}})
			// 默认电影模板（特效里的点换成空格）
			ctx := buildRenameContext(&TmdbMedia{Title: "测试", OriginalTitle: "Test", Year: "2020", MediaType: "movie"},
				&ParsedName{Media: &mediaOverlay{Res: ri}}, "x.mkv")
			// 默认模板不带帧率，帧率与发布组另拼在结尾
			name := strings.TrimSuffix(ctx.ApplyTemplate(defaultRenameConfig().MovieFile), ".mkv") + "." + ri.FPS + "-GRP.mkv"
			got := ParseResourceInfo(name)
			if got.Pix != ri.Pix || got.Effect != ri.Effect || got.VideoEncode != ri.VideoEncode ||
				got.AudioEncode != ri.AudioEncode || got.FPS != ri.FPS {
				t.Errorf("%s\n写进去 %+v\n读回来 %+v", name, ri, got)
			}
		}
	}
	// UHD 蓝光写成 UHD.BluRay（维护者定的），读回来是来源 UHD + 质量 BluRay
	got := ParseResourceInfo("测试.2020.UHD.BluRay.2160p.mkv")
	if got.Type != "BluRay" || got.Source != "UHD" {
		t.Fatalf("UHD.BluRay 读回来: %+v", got)
	}
}

func TestResourceParseFixes(t *testing.T) {
	cases := map[string]string{
		"Movie.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.7.1-FGT.mkv": "DTS-HD.MA.7.1",
		"Movie.DTS-HD-MA.5.1.mkv":                             "DTS-HD.MA.5.1",
		"Movie.DTSHDMA.mkv":                                   "DTS-HD.MA",
		"Movie.DTS-HD.5.1.mkv":                                "DTS-HD.5.1",
		"Movie.DTS.X.mkv":                                     "DTS.X",
		"Movie.DTS-X.7.1.mkv":                                 "DTS.X",
		"Movie.DTS.5.1.mkv":                                   "DTS.5.1",
	}
	for name, want := range cases {
		if got := ParseResourceInfo(name).AudioEncode; got != want {
			t.Errorf("%s 音频 = %q，预期 %q", name, got, want)
		}
	}
	if got := ParseResourceInfo("Movie.23.976FPS.mkv").FPS; got != "23.976FPS" {
		t.Errorf("帧率前面不该带点: %q", got)
	}
	if got := ParseResourceInfo("Movie.2160p.HLG.mkv").Effect; got != "HLG" {
		t.Errorf("HLG 应认得: %q", got)
	}
}

func TestMediaOverlayApply(t *testing.T) {
	base := ParseResourceInfo("Movie.1080p.x264-GRP.mkv")
	ov := &mediaOverlay{Res: ResourceInfo{Pix: "2160p", VideoEncode: "H265.10bit", Effect: "HDR10"}}
	got := ov.apply(base)
	if got.Pix != "1080p" || got.VideoEncode != "x264" || got.Effect != "HDR10" {
		t.Fatalf("补全模式只补空字段: %+v", got)
	}
	ov.Overwrite = true
	got = ov.apply(base)
	if got.Pix != "2160p" || got.VideoEncode != "H265.10bit" || got.Team != "GRP" {
		t.Fatalf("覆盖模式读到的盖过文件名，读不到的（发布组）不动: %+v", got)
	}
}

func TestNeedProbe(t *testing.T) {
	useProbeCfg(t, `{"enabled":true}`)
	cfg := loadMediaProbeCfg()
	if !needProbe("Movie.2020.mkv", "movie", cfg) {
		t.Fatal("模板用到画质变量、文件名没写：要读")
	}
	full := "Movie.2020.BluRay.DV.HDR10.2160p.H265.10bit.TrueHD.7.1.mkv"
	if needProbe(full, "movie", cfg) {
		t.Fatal("文件名写全了（资源质量只对 ISO 有意义）：不读")
	}
	if needProbe(strings.Replace(full, ".mkv", ".iso", 1), "movie", cfg) {
		t.Fatal("ISO 文件名写了 BluRay 也写全了：不读")
	}
	if !needProbe("Movie.2020.DV.HDR10.2160p.H265.10bit.TrueHD.7.1.iso", "movie", cfg) {
		t.Fatal("ISO 文件名没写资源质量：要读")
	}
	if needProbe("Movie.2020.avi", "movie", cfg) {
		t.Fatal("AVI 不读")
	}
	if !needProbe("Movie.2020.m2ts", "movie", cfg) || !needProbe("Movie.2020.ts", "movie", cfg) {
		t.Fatal("TS / M2TS 要读")
	}
	if !needProbe(full, "movie", mediaProbeCfg{Enabled: true, Overwrite: true}) {
		t.Fatal("覆盖模式只看模板")
	}
	if needProbe("Movie.2020.mkv", "movie", mediaProbeCfg{}) {
		t.Fatal("没开不读")
	}
	renameTpl = &RenameConfig{MovieFolder: "{title}", MovieFile: "{title}.{year}{ext}"}
	if needProbe("Movie.2020.mkv", "movie", cfg) {
		t.Fatal("模板没用画质变量：不读")
	}
}

func TestProbeJudgeName(t *testing.T) {
	ov := &mediaOverlay{Res: ResourceInfo{Pix: "2160p", VideoEncode: "H265.10bit", Effect: "DV.HDR10", AudioEncode: "TrueHD.7.1.ATMOS"}}
	j := probeJudgeName("Movie.2016.BluRay-GRP.mkv", ov)
	ri := ParseResourceInfo(j)
	if ri.Pix != "2160p" || ri.VideoEncode != "H265.10bit" || ri.Effect != "DV.HDR10" || ri.AudioEncode != "TrueHD.7.1.ATMOS" || ri.Team != "GRP" || ri.Type != "BluRay" {
		t.Fatalf("判定名 %q 解析出 %+v", j, ri)
	}
	if !strings.HasSuffix(j, "-GRP.mkv") {
		t.Fatalf("发布组要留在结尾: %q", j)
	}
	ov.Overwrite = true
	j = probeJudgeName("Movie.1080p.x264-GRP.mkv", ov)
	if ri := ParseResourceInfo(j); ri.Pix != "2160p" || ri.VideoEncode != "H265.10bit" {
		t.Fatalf("覆盖模式判定名 %q 解析出 %+v", j, ri)
	}
	if probeJudgeName("A.mkv", nil) != "A.mkv" {
		t.Fatal("没读到时原样返回")
	}
	ep := probeJudgeName("Show.S01E05.WEB-DL-GRP.mkv", ov)
	if p := parseFileName(ep); p.Season != 1 || p.Episode != 5 {
		t.Fatalf("判定名不能弄坏季集: %q → %+v", ep, p)
	}
}

// 剧集同一个版本只读一集；体积差太多的（同目录里另一个版本）单独读；读失败的同版本也不再读
func TestProbeSessionGroups(t *testing.T) {
	useProbeCfg(t, `{"enabled":true}`)
	calls := map[string]int{}
	prev := probeFileFn
	t.Cleanup(func() { probeFileFn = prev })
	probeFileFn = func(_ *pan115Ops, f remoteFile, name string) (*mediaprobe.Info, error) {
		calls[f.Fid]++
		w := 1920
		if f.Size > 3<<30 {
			w = 3840
		}
		return &mediaprobe.Info{Video: &mediaprobe.Video{Codec: "hevc", Width: w, BitDepth: 10, Transfer: "pq"}}, nil
	}
	s := newProbeSession(&pan115Ops{}, "tv", nil)
	if s == nil {
		t.Fatal("开着应有会话")
	}
	mk := func(fid, name string, size int64) remoteFile {
		return remoteFile{Fid: fid, Name: name, Path: "Show/Season 01", PickCode: "pc" + fid, Size: size}
	}
	e1 := s.overlayFor(mk("1", "Show.S01E01.mkv", 1<<30), "Show.S01E01.mkv")
	e2 := s.overlayFor(mk("2", "Show.S01E02.mkv", 1<<30+100<<20), "Show.S01E02.mkv")
	big := s.overlayFor(mk("3", "Show.S01E01.mkv", 5<<30), "Show.S01E01.mkv")
	again := s.overlayFor(mk("1", "Show.S01E01.mkv", 1<<30), "Show.S01E01.mkv")
	if e1 == nil || e2 != e1 || again != e1 {
		t.Fatal("同版本应沿用第一集读到的")
	}
	if big == nil || big.Res.Pix != "2160p" {
		t.Fatalf("体积差太多的另读: %+v", big)
	}
	if calls["1"] != 1 || calls["2"] != 0 || calls["3"] != 1 {
		t.Fatalf("读取次数不对: %v", calls)
	}

	// 逐集
	model.DB.Model(&model.Setting{}).Where("key = ?", "media-probe").Update("value", `{"enabled":true,"each_episode":true}`)
	s = newProbeSession(&pan115Ops{}, "tv", nil)
	s.overlayFor(mk("4", "Show.S01E04.mkv", 1<<30), "Show.S01E04.mkv")
	s.overlayFor(mk("5", "Show.S01E05.mkv", 1<<30), "Show.S01E05.mkv")
	if calls["4"] != 1 || calls["5"] != 1 {
		t.Fatalf("逐集应每集都读: %v", calls)
	}

	// 没开：没有会话，nil 会话什么都不做
	model.DB.Model(&model.Setting{}).Where("key = ?", "media-probe").Update("value", `{"enabled":false}`)
	if s := newProbeSession(&pan115Ops{}, "tv", nil); s != nil || s.overlayFor(mk("6", "x.mkv", 1), "x.mkv") != nil {
		t.Fatal("没开不该读")
	}
}

// 缓存：读过的（成功或近期失败）不再读
func TestProbeFileCache(t *testing.T) {
	useProbeCfg(t, `{"enabled":true}`)
	model.DB.Create(&model.MediaProbe{Key: "ABC", Info: `{"container":"mkv","video":{"codec":"hevc","width":3840}}`})
	info, err := probeFile(nil, remoteFile{Sha1: "abc", PickCode: "x"}, "x.mkv")
	if err != nil || info.Video.Width != 3840 {
		t.Fatalf("应命中缓存: %v %+v", err, info)
	}
	model.DB.Create(&model.MediaProbe{Key: "pc:bad", Err: "读取量超过上限", ProbedAt: time.Now()})
	if _, err := probeFile(nil, remoteFile{PickCode: "bad"}, "x.mkv"); err == nil {
		t.Fatal("近期失败的不重试")
	}
	hook := cachedProbeHook("movie")
	if ov := hook(orgRecordFile{Sha1: "abc"}, "Movie.2020.mkv"); ov == nil || ov.Res.Pix != "2160p" {
		t.Fatalf("只查缓存的版本: %+v", ov)
	}
	if ov := hook(orgRecordFile{Sha1: "none"}, "Movie.2020.mkv"); ov != nil {
		t.Fatal("没缓存的不读")
	}
}
