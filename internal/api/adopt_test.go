package api

import (
	"os"
	"path/filepath"
	"testing"
)

// adoptTestIDBase 真实的文件 id 是十几位的数，太小的 id 编出来的 pickcode 过不了 validFilePickcode 的长度检查
const adoptTestIDBase = 3165030031459331000

// adoptTestPC 按本账号（realPickcode 的不动点）造一个文件 pickcode
func adoptTestPC(t *testing.T, id uint64, stable string) string {
	t.Helper()
	tab := pcEncTab["b"]
	id += adoptTestIDBase
	pc := "b" + pcTranslate(pcB36Encode(id), tab) + pcTranslate(stable, tab)
	if back, err := pickcodeToID115(pc); err != nil || back != id {
		t.Fatalf("造的 pickcode %s 解不回 %d: %v", pc, id, err)
	}
	return pc
}

func adoptTestStable(t *testing.T) string {
	t.Helper()
	sp, err := stablePoint115(realPickcode)
	if err != nil {
		t.Fatal(err)
	}
	return sp
}

// 各项目的写法按 ADOPT-STRM-PLAN.md §1 的出处造，别凭印象改
func TestParseForeignStrm(t *testing.T) {
	pc := realPickcode
	cases := []struct {
		name, content string
		want          foreignRef
	}{
		{"p115strmhelper 默认", "http://192.168.1.2:3000/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=" + pc,
			foreignRef{Kind: "pickcode", Format: strmFmtHelper, PickCode: pc}},
		{"p115strmhelper pickname", "http://mp:3000/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=" + pc + "&file_name=%E9%BE%99%E7%8F%A0.mkv",
			foreignRef{Kind: "pickcode", Format: strmFmtHelper, PickCode: pc}},
		{"p115strmhelper URL 模板只给 file_id", "http://mp:3000/api/v1/plugin/P115StrmHelper/redirect_url?file_id=3165030031459331046",
			foreignRef{Kind: "fileid", Format: strmFmtHelper, FileID: "3165030031459331046"}},
		{"p115strmhelper 分享", "http://mp:3000/api/v1/plugin/P115StrmHelper/redirect_url?share_code=sw1abc&receive_code=x1y2&id=123",
			foreignRef{Kind: "share", Format: strmFmtHelper}},
		{"qmediasync", "http://10.0.0.5:12333/115/url/video.mkv?pickcode=" + pc + "&userid=12345",
			foreignRef{Kind: "pickcode", Format: strmFmtQms, PickCode: pc}},
		{"qmediasync 带路径", "http://10.0.0.5:12333/115/url/video.mkv?pickcode=" + pc + "&userid=12345&path=%2F%E5%BD%B1%E8%A7%86%2F%E7%94%B5%E5%BD%B1%2FA.mkv",
			foreignRef{Kind: "pickcode", Format: strmFmtQms, PickCode: pc, PanPath: "/影视/电影/A.mkv"}},
		{"MediaSync115", "http://nas:8000/api/115/url/video.mp4?pickcode=" + pc,
			foreignRef{Kind: "pickcode", Format: strmFmtMs115, PickCode: pc}},
		{"本站 pick_code", "http://nas:6060/d/" + pc, foreignRef{Kind: "pickcode", Format: strmFmtStation, PickCode: pc}},
		{"本站带后缀与文件名", "http://nas:6060/d/" + pc + ".mkv?/龙珠 S01E01.mkv", foreignRef{Kind: "pickcode", Format: strmFmtStation, PickCode: pc}},
		{"OpenList 路径", "http://nas:5244/d/%E5%BD%B1%E8%A7%86/%E7%94%B5%E5%BD%B1/A%20(2020)/A.mkv",
			foreignRef{Kind: "path", Format: strmFmtOpenList, PanPath: "/影视/电影/A (2020)/A.mkv"}},
		{"OpenList 第二段像 pickcode 也不算", "http://nas:5244/d/" + pc + "/A.mkv",
			foreignRef{Kind: "path", Format: strmFmtOpenList, PanPath: "/" + pc + "/A.mkv"}},
		{"openStrm 自定义前缀", "https://h.example/strm/影视/剧集/B/Season 1/B S01E01.mkv",
			foreignRef{Kind: "path", Format: strmFmtOther, PanPath: "/strm/影视/剧集/B/Season 1/B S01E01.mkv"}},
		{"挂载路径", "/mnt/CloudNAS/115/影视/电影/A.mkv\n", foreignRef{Kind: "path", Format: strmFmtMount, PanPath: "/mnt/CloudNAS/115/影视/电影/A.mkv"}},
		{"前面有注释与 BOM", "\uFEFF#EXTM3U\n\nhttp://nas:6060/d/" + pc, foreignRef{Kind: "pickcode", Format: strmFmtStation, PickCode: pc}},
		{"目录 pickcode 不算", "http://nas:6060/d/" + realDirPickcode, foreignRef{Kind: "unknown", Format: strmFmtOpenList}},
		{"空文件", "  \n", foreignRef{Kind: "unknown", Format: strmFmtOther}},
		{"乱写的", "hello", foreignRef{Kind: "unknown", Format: strmFmtOther}},
	}
	for _, c := range cases {
		got := parseForeignStrm(c.content)
		if got != c.want {
			t.Errorf("%s:\n got  %+v\n want %+v", c.name, got, c.want)
		}
	}
}

func TestPlanAdoptBuckets(t *testing.T) {
	sp := adoptTestStable(t)
	pcA, pcB, pcC, pcD, pcE, pcF := adoptTestPC(t, 101, sp), adoptTestPC(t, 102, sp), adoptTestPC(t, 103, sp),
		adoptTestPC(t, 104, sp), adoptTestPC(t, 105, sp), adoptTestPC(t, 106, sp)
	other := adoptTestPC(t, 999, "0zzz")
	remote := []remoteFile{
		{Fid: "101", PickCode: pcA, Path: "影视/电影/A (2020)", Name: "A (2020).mkv"},
		{Fid: "102", PickCode: pcB, Path: "影视/电影/B (2021)", Name: "B (2021).iso"},
		{Fid: "103", PickCode: pcC, Path: "影视/剧集/C/Season 1", Name: "C S01E01.mkv"},
		{Fid: "104", PickCode: pcD, Path: "影视/电影/D (2019)", Name: "D: 导演剪辑版.mkv"},
		{Fid: "105", PickCode: pcE, Path: "影视/电影/E (2018)", Name: "E (2018).mp4"},
		{Fid: "106", PickCode: pcF, Path: "影视/电影/F (2017)", Name: "F (2017).mkv"},
		{Fid: "107", PickCode: adoptTestPC(t, 107, sp), Path: "影视/电影/G (2016)", Name: "G (2016).mkv"},
		{Fid: "108", PickCode: adoptTestPC(t, 108, sp), Path: "影视/电影/H (2015)", Name: "H (2015).mkv"},
	}
	helper := func(pc string) foreignRef {
		return parseForeignStrm("http://mp:3000/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=" + pc)
	}
	root := "/media"
	l := func(rel string, ref foreignRef) localStrm {
		abs := rel
		if rel[0] != '/' {
			abs = root + "/" + rel
		}
		return localStrm{Rel: rel, Abs: abs, Ref: ref}
	}
	locals := []localStrm{
		// p115strmhelper 默认写法 stem.strm，位置一致
		l("影视/电影/A (2020)/A (2020).strm", helper(pcA)),
		// ISO 写成 stem.iso.strm（本站旧写法），也算原样
		l("影视/电影/B (2021)/B (2021).iso.strm", helper(pcB)),
		// 已在台账
		l("影视/剧集/C/Season 1/C S01E01.strm", parseForeignStrm("http://nas:6060/d/"+pcC)),
		// Windows 上跑的 p115strmhelper 把冒号换成全角：名字不同
		l("影视/电影/D (2019)/D： 导演剪辑版.strm", helper(pcD)),
		// p115strmhelper 本地目录不带「影视」这一层：位置不同
		l("电影/E (2018)/E (2018).strm", helper(pcE)),
		// 同一个文件两份：原样那份留下，另一份算重复
		l("影视/电影/F (2017)/F (2017).strm", helper(pcF)),
		l("影视/电影/F (2017)/F (2017).mkv.strm", helper(pcF)),
		// 只有网盘路径（OpenList），按后缀对上
		l("影视/电影/G (2016)/G (2016).strm", parseForeignStrm("http://nas:5244/d/115/影视/电影/G (2016)/G (2016).mkv")),
		// 网盘上没有
		l("影视/电影/X/X.strm", helper(adoptTestPC(t, 555, sp))),
		// 另一个账号
		l("影视/电影/Y/Y.strm", helper(other)),
		// 分享与认不出
		l("影视/电影/S/S.strm", parseForeignStrm("http://mp:3000/api/v1/plugin/P115StrmHelper/redirect_url?share_code=a&receive_code=b&id=1")),
		l("影视/电影/U/U.strm", parseForeignStrm("hello")),
	}
	ledger := map[string]string{"103": "影视/剧集/C/Season 1/C S01E01.strm"}

	rep := planAdopt(locals, remote, ledger, root)
	want := map[string]int{
		adoptBucketAdopt: 4, adoptBucketRename: 1, adoptBucketAlready: 1, adoptBucketMoved: 1, adoptBucketDup: 1,
		adoptBucketMissing: 2, adoptBucketShare: 1, adoptBucketUnknown: 1, adoptBucketNotLocal: 1,
	}
	for k, n := range want {
		if got := rep.count(k); got != n {
			t.Errorf("桶 %s = %d，期望 %d；%+v", k, got, n, rep.bucket(k).Samples)
		}
	}
	if len(rep.Buckets) != len(adoptBucketOrder) {
		t.Errorf("桶数 %d", len(rep.Buckets))
	}
	for i, b := range rep.Buckets {
		if b.Key != adoptBucketOrder[i] {
			t.Errorf("第 %d 个桶是 %s，期望 %s", i, b.Key, adoptBucketOrder[i])
		}
	}
	if rep.OtherAccount != 1 {
		t.Errorf("OtherAccount = %d", rep.OtherAccount)
	}
	if dup := rep.bucket(adoptBucketDup).Samples[0]; dup.Local != "影视/电影/F (2017)/F (2017).mkv.strm" {
		t.Errorf("重复里留下的是原样那份，算重复的应是旧名那份：%+v", dup)
	}
	if rn := rep.bucket(adoptBucketRename).Samples[0]; rn.Expect != "影视/电影/D (2019)/D: 导演剪辑版.strm" {
		t.Errorf("名字不同的样例：%+v", rn)
	}
	if len(rep.Shifts) != 1 || rep.Shifts[0] != (adoptShift{From: "/media", To: "/media/影视", Count: 1}) {
		t.Errorf("前缀换算：%+v", rep.Shifts)
	}
	if rep.Verdict != "ok" {
		t.Errorf("Verdict = %s", rep.Verdict)
	}
	if rep.Formats[strmFmtHelper] != 9 || rep.Formats[strmFmtStation] != 1 || rep.Formats[strmFmtOpenList] != 1 {
		t.Errorf("Formats = %+v", rep.Formats)
	}
}

// 多数对不上位置时判 move；一个都没对上判 empty
func TestPlanAdoptVerdict(t *testing.T) {
	sp := adoptTestStable(t)
	pcA, pcB := adoptTestPC(t, 201, sp), adoptTestPC(t, 202, sp)
	remote := []remoteFile{
		{Fid: "201", PickCode: pcA, Path: "影视/电影/A", Name: "A.mkv"},
		{Fid: "202", PickCode: pcB, Path: "影视/电影/B", Name: "B.mkv"},
	}
	ref := func(pc string) foreignRef { return parseForeignStrm("http://q:1/115/url/video.mkv?pickcode=" + pc) }
	// 旧目录在本地根外面（扫描目录另指）：Rel 是绝对路径，一律算位置不同
	locals := []localStrm{
		{Rel: "/old/strm/电影/A/A.strm", Abs: "/old/strm/电影/A/A.strm", Ref: ref(pcA)},
		{Rel: "/old/strm/电影/B/B.strm", Abs: "/old/strm/电影/B/B.strm", Ref: ref(pcB)},
	}
	rep := planAdopt(locals, remote, nil, "/media/")
	if rep.Verdict != "move" || rep.count(adoptBucketMoved) != 2 {
		t.Fatalf("Verdict=%s moved=%d", rep.Verdict, rep.count(adoptBucketMoved))
	}
	if len(rep.Shifts) != 1 || rep.Shifts[0] != (adoptShift{From: "/old/strm", To: "/media/影视", Count: 2}) {
		t.Errorf("前缀换算：%+v", rep.Shifts)
	}

	rep = planAdopt([]localStrm{{Rel: "a/b.strm", Abs: "/media/a/b.strm", Ref: parseForeignStrm("hello")}}, remote, nil, "/media")
	if rep.Verdict != "empty" {
		t.Errorf("Verdict=%s", rep.Verdict)
	}
}

// 路径对号取最长能对上的后缀：挂载前缀、OpenList 的存储名都能被跳过
func TestAdoptMatchPathSuffix(t *testing.T) {
	byPath := map[string]int{"影视/电影/A/A.mkv": 0, "电影/A/A.mkv": 1}
	for _, p := range []string{"/mnt/115/影视/电影/A/A.mkv", "/影视/电影/A/A.mkv"} {
		if i, ok := adoptMatch(foreignRef{PanPath: p}, nil, nil, byPath); !ok || i != 0 {
			t.Errorf("%s → %d %v", p, i, ok)
		}
	}
	if _, ok := adoptMatch(foreignRef{PanPath: "/影视/电影/A/B.mkv"}, nil, nil, byPath); ok {
		t.Error("文件名不同不该对上")
	}
}

func TestScanLocalStrms(t *testing.T) {
	root := t.TempDir()
	write := func(rel, content string) {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("影视/电影/A/A.strm", "http://nas:6060/d/"+realPickcode)
	write("影视/电影/A/A.nfo", "<movie/>")
	write("影视/电影/A/.actors/x.strm", "隐藏目录里的不算")
	write("影视/@eaDir/y.strm", "群晖缩略图目录不算")
	write("影视/电影/B/B.STRM", "/mnt/115/影视/电影/B/B.mkv")

	got, err := scanLocalStrms(root, root, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("扫到 %d 个：%+v", len(got), got)
	}
	rels := map[string]string{}
	for _, s := range got {
		rels[s.Rel] = s.Ref.Kind
	}
	if rels["影视/电影/A/A.strm"] != "pickcode" || rels["影视/电影/B/B.STRM"] != "path" {
		t.Errorf("扫描结果：%+v", rels)
	}

	// 扫描目录在本地根外面：Rel 用绝对路径
	other := t.TempDir()
	got, err = scanLocalStrms(root, other, nil)
	if err != nil || len(got) != 2 || !filepath.IsAbs(filepath.FromSlash(got[0].Rel)) {
		t.Errorf("根外扫描：%+v %v", got, err)
	}
}
