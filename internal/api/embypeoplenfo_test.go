package api

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"115-station/internal/config"
	"115-station/internal/model"
)

func TestRewriteNFOPeople(t *testing.T) {
	src := `<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>阿甘正传</title>
  <director>Robert Zemeckis</director>
  <credits>Eric Roth</credits>
  <actor>
    <name>Tom Hanks</name>
    <role>Forrest</role>
    <type>Actor</type>
    <thumb>https://image.tmdb.org/t/p/h632/tom.jpg</thumb>
    <tmdbid>31</tmdbid>
  </actor>
  <actor><name>Sally &amp; Co</name><role>x</role></actor>
  <actor><name>巩俐</name><type>GuestStar</type></actor>
</movie>`
	zh := map[string]string{"Tom Hanks": "汤姆·汉克斯", "Robert Zemeckis": "罗伯特·泽米吉斯", "Sally & Co": "莎莉<和>朋友"}
	var asked []string
	out, actors := rewriteNFOPeople(src, func(id int, name, typ string) string {
		asked = append(asked, typ+":"+name)
		if name == "Tom Hanks" && id != 31 {
			t.Errorf("演员的 tmdbid 没读出来: %d", id)
		}
		return zh[name]
	})
	for _, want := range []string{
		"<name>汤姆·汉克斯</name>", "<director>罗伯特·泽米吉斯</director>", "<credits>Eric Roth</credits>",
		"<name>莎莉&lt;和&gt;朋友</name>", "<title>阿甘正传</title>", "<role>Forrest</role>",
		"<thumb>https://image.tmdb.org/t/p/h632/tom.jpg</thumb>", "<name>巩俐</name>",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("改写后缺 %s:\n%s", want, out)
		}
	}
	if len(actors) != 3 || actors[0].Name != "汤姆·汉克斯" || actors[0].Orig != "Tom Hanks" || actors[0].TmdbID != 31 ||
		actors[1].Orig != "Sally & Co" || actors[2].Name != "巩俐" {
		t.Fatalf("演员列表不对: %+v", actors)
	}
	if strings.Join(asked, ",") != "Actor:Tom Hanks,Actor:Sally & Co,GuestStar:巩俐,Director:Robert Zemeckis,Writer:Eric Roth" {
		t.Fatalf("问名字的顺序 / 类型不对: %v", asked)
	}
	// 没有要改的：原样返回
	if same, _ := rewriteNFOPeople(src, func(int, string, string) string { return "" }); same != src {
		t.Fatal("没有改名时 NFO 不该有任何变化")
	}
}

func TestActorImageNameAndThumb(t *testing.T) {
	cases := map[string]string{"Tom Hanks": "Tom_Hanks.jpg", "汤姆·汉克斯": "汤姆·汉克斯.jpg", `A/B:C?`: "ABC.jpg", " . ": ""}
	for in, want := range cases {
		if got := actorImageName(in); got != want {
			t.Errorf("actorImageName(%q) = %q，应为 %q", in, got, want)
		}
	}
	if p := tmdbProfileFromThumb("https://img.example.com/t/p/h632/abc.jpg"); p != "/abc.jpg" {
		t.Fatalf("thumb 里的图片路径: %q", p)
	}
	if p := tmdbProfileFromThumb("/media/.actors/a.jpg"); p != "" {
		t.Fatalf("不是 TMDB 链接应为空: %q", p)
	}
}

func TestAggregateCastOf(t *testing.T) {
	var in []tmdbAggregateCast
	_ = json.Unmarshal([]byte(`[
		{"id":2,"name":"B","order":1,"roles":[{"character":"小角色","episode_count":1},{"character":"主角","episode_count":30}]},
		{"id":1,"name":"A","order":0,"profile_path":"/a.jpg","roles":[{"character":"","episode_count":50}]}
	]`), &in)
	out := aggregateCastOf(in)
	if len(out) != 2 || out[0].ID != 1 || out[0].ProfilePath != "/a.jpg" || out[1].Character != "主角" {
		t.Fatalf("应按 order 排、角色取出场最多的: %+v", out)
	}
	many := make([]tmdbAggregateCast, nfoTVMaxCast+10)
	for i := range many {
		many[i] = tmdbAggregateCast{ID: i + 1, Name: "x", Order: i}
	}
	if n := len(aggregateCastOf(many)); n != nfoTVMaxCast {
		t.Fatalf("剧集演员应截到 %d 位: %d", nfoTVMaxCast, n)
	}
}

// 端到端：补全跑完，本地 NFO 的演员名换成中文、头像存进 .actors；第二轮没事可做
func TestPersonFillSyncsLocalNFO(t *testing.T) {
	newTestDB(t, "person-nfo-sync.db")
	root := t.TempDir()
	model.DB.Create(&model.Setting{Key: "full", Value: `{"local_path":"` + filepath.ToSlash(root) + `"}`})
	movieDir := filepath.Join(root, "电影", "阿甘正传 (1994) {tmdb-13}")
	if err := os.MkdirAll(movieDir, 0o755); err != nil {
		t.Fatal(err)
	}
	strm := filepath.Join(movieDir, "阿甘正传 (1994).strm")
	nfo := filepath.Join(movieDir, "阿甘正传 (1994).nfo")
	_ = os.WriteFile(strm, []byte("http://x"), 0o644)
	// 老 NFO：Tom Hanks 没有 tmdbid（要靠本轮取过的演职员表认），Robin Wright 有
	_ = os.WriteFile(nfo, []byte(`<movie>
  <title>阿甘正传</title>
  <director>Robert Zemeckis</director>
  <actor><name>Tom Hanks</name><role>Forrest</role></actor>
  <actor><name>Robin Wright</name><role>Jenny</role><thumb>https://image.tmdb.org/t/p/h632/robin.jpg</thumb><tmdbid>32</tmdbid></actor>
  <actor><name>Nobody</name><role>x</role></actor>
</movie>`), 0o644)

	f := &fakePeopleServer{
		persons: map[string]*fakePerson{
			"p1": {Name: "Tom Hanks"}, "p2": {Name: "Robin Wright", Tmdb: "32", Image: "old"}, "p3": {Name: "Robert Zemeckis"},
			"p4": {Name: "Nobody"}, "p5": {Name: "刘德华", Image: "x", Overview: "香港演员、歌手。"}, "w1": {Name: "Jane Doe"},
		},
		uploads: map[string]int{}, updates: map[string]int{}, tmdbHit: map[string]int{},
		paths:   map[string]string{"m1": filepath.ToSlash(strm)}, imgGets: map[string]int{},
	}
	srv := httptest.NewServer(f)
	defer srv.Close()
	model.DB.Create(&model.TmdbConfig{ApiKey: "k", ApiUrl: srv.URL, ImageApiUrl: srv.URL, Language: "zh-CN"})
	dir := t.TempDir()
	h := &Handler{DB: model.DB, Config: &config.Config{ConfigDir: dir, DataDir: dir}}
	emby, _ := json.Marshal(map[string]string{"server_url": srv.URL, "api_key": "ek"})
	_ = h.Config.SaveSetting("emby", string(emby))

	out, err := execPersonJob(h, &model.TaskJob{Kind: jobKindPerson, Priority: jobPriorityManual})
	if err != nil {
		t.Fatal(err)
	}
	res := out.Result.(personJobResult)
	b, _ := os.ReadFile(nfo)
	s := string(b)
	// Robin Wright 的中文名在 Emby 里被别人占着没改，NFO 里照样写中文名：同一个人，Emby 按名字认到那个中文条目上
	for _, want := range []string{"<name>汤姆·汉克斯</name>", "<name>罗宾·怀特</name>", "<name>Nobody</name>",
		"<director>Robert Zemeckis</director>", "<title>阿甘正传</title>", "<tmdbid>32</tmdbid>"} {
		if !strings.Contains(s, want) {
			t.Errorf("NFO 缺 %s:\n%s", want, s)
		}
	}
	for _, name := range []string{"汤姆·汉克斯.jpg", "罗宾·怀特.jpg"} {
		if !localMetaExists(filepath.Join(movieDir, ".actors"), name) {
			t.Errorf(".actors 里缺 %s", name)
		}
	}
	if f.imgGets["p4"] != 0 {
		t.Error("Emby 里没有头像的人物不该去取")
	}
	if f.imgGets["p1"] != 1 || f.imgGets["p2"] != 1 {
		t.Errorf("头像应从 Emby 取（p1 本轮刚补上）: %v", f.imgGets)
	}
	if res.NFOs != 1 || res.ActorImages != 2 {
		t.Fatalf("NFO 同步计数不对: %+v", res)
	}

	// 第二轮：NFO 已是中文、头像已在，什么都不做，按空转处理
	out, err = execPersonJob(h, &model.TaskJob{Kind: jobKindPerson, Priority: jobPriorityBackground})
	if err != nil {
		t.Fatal(err)
	}
	if r2 := out.Result.(personJobResult); r2.NFOs != 0 || r2.ActorImages != 0 || !out.Idle {
		t.Fatalf("第二轮不该再改: %+v idle=%v", r2, out.Idle)
	}
}

// 网盘镜像的 NFO、刮削方式为 Emby 时都不改写
func TestPersonNFOSyncGuards(t *testing.T) {
	newTestDB(t, "person-nfo-guard.db")
	root := t.TempDir()
	dir := filepath.Join(root, "剧集", "某剧")
	_ = os.MkdirAll(dir, 0o755)
	nfo := filepath.Join(dir, "tvshow.nfo")
	orig := `<tvshow><actor><name>Tom Hanks</name><tmdbid>31</tmdbid></actor></tvshow>`
	_ = os.WriteFile(nfo, []byte(orig), 0o644)
	model.DB.Create(&model.PersonMeta{TmdbID: 31, ZhName: "汤姆·汉克斯", FetchedAt: time.Now()})

	r := &personRunner{cfg: defaultPersonFillCfg(), nfoSync: true, root: root, credits: map[string]*titleCredits{},
		imaged: map[string]bool{}, mapPath: func(p string) string { return p }}
	r.cfg.Image = false
	item := embyTitleItem{ID: "s1", Name: "某剧", Type: "Series", Path: filepath.ToSlash(dir)}

	model.DB.Create(&model.SyncedFile{RelPath: "库/剧集/某剧/tvshow.nfo"})
	r.syncTitleNFO(item)
	if b, _ := os.ReadFile(nfo); string(b) != orig {
		t.Fatal("台账里有的 NFO（网盘镜像）不该改写")
	}
	model.DB.Where("1 = 1").Delete(&model.SyncedFile{})

	// 媒体库外的路径（路径映射没对上）不碰
	outside := embyTitleItem{Type: "Series", Path: filepath.ToSlash(t.TempDir())}
	if n, _ := r.titleNFOPath(outside); n != "" {
		t.Fatalf("媒体库外的路径不该认: %s", n)
	}

	r.syncTitleNFO(item)
	if b, _ := os.ReadFile(nfo); !strings.Contains(string(b), "<name>汤姆·汉克斯</name>") {
		t.Fatalf("应改写成中文名: %s", b)
	}
}
