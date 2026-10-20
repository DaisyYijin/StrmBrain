package api

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"115-station/internal/config"
	"115-station/internal/model"
)

// 附加账号的库（acctlibs.go，MULTI-ACCOUNT-PLAN.md §15）

func TestPlayIDRoundTrip(t *testing.T) {
	if encodePlayID(acctMain, "abc") != "abc" {
		t.Error("主号的直链不变")
	}
	id := encodePlayID(7, "abc")
	if id != "7~abc" {
		t.Fatalf("附加账号：%q", id)
	}
	if a, pc := decodePlayID(id); a != 7 || pc != "abc" {
		t.Fatalf("拆：%d %q", a, pc)
	}
	if a, pc := decodePlayID("abc"); a != acctMain || pc != "abc" {
		t.Fatalf("没有账号段 = 主号：%d %q", a, pc)
	}
	if _, pc := decodePlayID("x~abc"); pc != "" {
		t.Error("账号段不是数字应当无效")
	}

	// 302 / Emby 反代认标识时保留账号段（缓存键、台账都带着它）
	cases := map[string]string{
		"7~ABC.mkv":     "7~abc",
		"7~abc/文件名.mkv": "7~abc",
		"0~abc":         "abc",
		"007~abc":       "7~abc",
		"x~abc":         "",
		"~abc":          "",
		"7~ab-c":        "",
		"abc.iso":       "abc",
	}
	for in, want := range cases {
		if got := normalizePlaybackID(in); got != want {
			t.Errorf("normalizePlaybackID(%q) = %q，期望 %q", in, got, want)
		}
	}
}

func TestAcctLedgerIDs(t *testing.T) {
	if acctLedgerFid(acctMain, "123") != "123" || acctLedgerFid(3, "123") != "a3:123" {
		t.Fatal("台账 fid 前缀")
	}
	if !isAcctLedgerFid("a3:123") || isAcctLedgerFid("123") || isAcctLedgerFid("abc") {
		t.Fatal("认附加账号的台账行")
	}
	rows := []model.SyncedFile{{FileID: "1"}, {FileID: "a3:2"}, {FileID: "3"}}
	kept, n := dropAcctLedgerRows(rows)
	if n != 1 || len(kept) != 2 || kept[1].FileID != "3" {
		t.Fatalf("去掉附加账号的行：%v %d", kept, n)
	}
	if len(rows) != 3 || rows[1].FileID != "a3:2" {
		t.Fatal("不该改到传进来的切片")
	}
}

func TestAcctLibKeyAndForcedSelf(t *testing.T) {
	k := acctLibKey(4, " 999 ")
	if k != "a4:999" {
		t.Fatalf("key：%q", k)
	}
	if a, c, ok := splitAcctLibKey(k); !ok || a != 4 || c != "999" {
		t.Fatalf("拆 key：%d %q %v", a, c, ok)
	}
	for _, bad := range []string{"999", "a0:1", "a:1", "a4:", "ab:1"} {
		if isAcctLibKey(bad) {
			t.Errorf("%q 不是附加账号的库 key", bad)
		}
	}
	// 附加账号的库不管 lib_opts 里存了什么，都是自己整理
	o := libOpts{Libs: map[string]libOpt{k: {Mode: "organize", Depth: 1}}}
	if !o.of(k).self() || o.of(k).depth() != 1 {
		t.Fatalf("应强制自己整理：%+v", o.of(k))
	}
	if o.of("100").self() {
		t.Error("主号的库不受影响")
	}
}

func TestPanDirOfLocalSkipsAcctLib(t *testing.T) {
	roots := []libRoot{{Cid: "1", Name: "影视", Abs: "/影视"}}
	roots = append(roots, acctLibRoots([]acctLibEntry{{Account: 2, Cid: "9", Path: "/仓库/电影", Local: "副号电影"}})...)
	if got := panDirOfLocal(roots, "副号电影/某片"); got != "" {
		t.Fatalf("附加账号的库不该算出网盘目录（会往主号上传）：%q", got)
	}
	if got := panDirOfLocal(roots, "影视/某片"); got != "/影视/某片" {
		t.Fatalf("主号的库照旧：%q", got)
	}
	if r := roots[1]; !r.Self || r.Account != 2 || r.Abs != "" || r.Cid != "a2:9" {
		t.Fatalf("附加账号的库：%+v", r)
	}
}

func TestPlanAcctLibs(t *testing.T) {
	deps, _ := fakeDeps(t)
	deps.ledgerHas = func(n string) bool { return n == "旧库" || n == "副号已有" }
	logged := errors.New("附加账号「x」还没登录 115")
	deps.acctOK = func(acct uint) error {
		if acct == 9 {
			return logged
		}
		return nil
	}
	in := planInput()

	// 不带 acct_libs：沿用
	keep, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll()}, deps)
	if err != nil || keep.AcctChanged {
		t.Fatalf("不带 acct_libs 应沿用：%v %+v", err, keep)
	}

	// 加一个附加账号的库：一律自己整理，lib_opts 按 a{账号}:{cid} 存
	add := []acctLibEntry{{Account: 2, Cid: "777", Path: "/仓库/电影", Local: "副号电影"}}
	plan, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll(), AcctLibs: add,
		Opts: map[string]libOpt{"a2:777": {Mode: "organize", Depth: 1, Scrape: "manual"}}}, deps)
	if err != nil {
		t.Fatal(err)
	}
	if !plan.AcctChanged || len(plan.AcctAdded) != 1 || len(parseAcctLibs(plan.AcctJSON)) != 1 {
		t.Fatalf("应加上：%+v", plan)
	}
	if o := parseLibOpts(plan.OptsJSON).of("a2:777"); !o.self() || o.depth() != 1 || o.Scrape != "manual" {
		t.Fatalf("附加账号的库的选项：%s", plan.OptsJSON)
	}
	if plan.FullChanged || len(plan.Removed) != 0 {
		t.Fatalf("不该动主号的库：%+v", plan)
	}

	bad := map[string][]acctLibEntry{
		"和主号的库撞名": {{Account: 2, Cid: "777", Local: "影视"}},
		"和残留撞名":   {{Account: 2, Cid: "777", Local: "旧库"}},
		"没有本地名":   {{Account: 2, Cid: "777"}},
		"网盘根":     {{Account: 2, Cid: "0", Local: "x"}},
		"同一目录两次":  {{Account: 2, Cid: "777", Local: "a"}, {Account: 2, Cid: "777", Local: "b"}},
		"两个库同名":   {{Account: 2, Cid: "1", Local: "a"}, {Account: 3, Cid: "2", Local: "A"}},
		"账号没登录":   {{Account: 9, Cid: "1", Local: "a"}},
		"没选账号":    {{Account: 0, Cid: "1", Local: "a"}},
	}
	for name, list := range bad {
		if _, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll(), AcctLibs: list}, deps); err == nil {
			t.Errorf("%s 应该拒绝", name)
		}
	}

	// 已有的库：有内容就不能改本地名；移除时可以清本地
	in.AcctRaw = `[{"account":2,"cid":"777","path":"/仓库/电影","local":"副号已有"}]`
	if _, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll(),
		AcctLibs: []acctLibEntry{{Account: 2, Cid: "777", Local: "改个名"}}}, deps); err == nil || !strings.Contains(err.Error(), "不能再改") {
		t.Fatalf("有内容的附加账号的库不能改名：%v", err)
	}
	rm, _, err := planLibrariesSave(in, libSaveReq{ExtraLibs: keepAll(), AcctLibs: []acctLibEntry{}, CleanLocal: []string{"a2:777"}}, deps)
	if err != nil || len(rm.AcctRemoved) != 1 || len(rm.Clean) != 1 || rm.Clean[0] != "副号已有" || rm.AcctJSON != "[]" {
		t.Fatalf("移除并清本地：%v %+v", err, rm)
	}
	if strings.Contains(rm.YAML, "副号") || len(rm.Removed) != 0 {
		t.Fatalf("移除附加账号的库不该碰主号的库与分类规则：%+v", rm)
	}
}

func TestApplySyncResultsAcctFiles(t *testing.T) {
	newTestDB(t, "acctsync.db")
	root := t.TempDir()
	videos := []remoteFile{
		{Fid: "11", Name: "主号片.mkv", Path: "影视/电影/主号片", PickCode: "pcmain"},
		{Fid: "11", Name: "副号片.mkv", Path: "副号电影/副号片", PickCode: "pcextra", Acct: 2},
	}
	st := applySyncResults(model.DB, &pan115Ops{}, videos, nil, root, "http://h:6086", "pick_code", false, false, "")
	if st.StrmCreated != 2 {
		t.Fatalf("应写两个 STRM：%+v", st)
	}
	main, _ := os.ReadFile(filepath.Join(root, "影视/电影/主号片/主号片.strm"))
	extra, _ := os.ReadFile(filepath.Join(root, "副号电影/副号片/副号片.strm"))
	if string(main) != "http://h:6086/d/pcmain" || string(extra) != "http://h:6086/d/2~pcextra" {
		t.Fatalf("直链：%q / %q", main, extra)
	}
	// 同一个 fid 在两个账号里也不打架：附加账号的台账行带前缀
	var rows []model.SyncedFile
	model.DB.Order("file_id").Find(&rows)
	if len(rows) != 2 || rows[0].FileID != "11" || rows[1].FileID != "a2:11" || rows[1].PickCode != "2~pcextra" {
		t.Fatalf("台账：%+v", rows)
	}
	// 洗版 / sha1 去重不看附加账号的行
	model.DB.Model(&model.SyncedFile{}).Where("1 = 1").Update("sha1", "S")
	w := newWashScanner(nil, &OrgConfig{})
	if got := w.sameFile("S"); len(got) != 1 || got[0].FileID != "11" {
		t.Fatalf("sha1 去重只看主号：%+v", got)
	}
}

func TestProxyDownloadAcctNeedsAccount(t *testing.T) {
	newTestDB(t, "acctproxy.db")
	cfg := &config.Config{DataDir: t.TempDir(), ConfigDir: t.TempDir()}
	_, _, err := proxyDownloadURLFull(model.DB, cfg, "5~pcx", "ua")
	if err == nil || !strings.Contains(err.Error(), "已删除") {
		t.Fatalf("账号不存在时应报错、不去请求 115：%v", err)
	}
	model.DB.Create(&model.Storage{Name: "副号", Type: "115"})
	var s model.Storage
	model.DB.Where("name = ?", "副号").First(&s)
	_, _, err = proxyDownloadURLFull(model.DB, cfg, encodePlayID(s.ID, "pcx"), "ua")
	if err == nil || !strings.Contains(err.Error(), "没有登录") {
		t.Fatalf("没登录的附加账号应报错：%v", err)
	}
}
