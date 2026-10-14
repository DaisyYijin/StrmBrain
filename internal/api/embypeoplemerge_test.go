package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"115-station/internal/config"
	"115-station/internal/model"
)

func TestCleanPersonName(t *testing.T) {
	cases := map[string]string{
		"周 星驰":          "周星驰",
		"周　星驰":          "周星驰", // 全角空格
		" 刘德华 ":         "刘德华",
		"汤姆·汉克斯":        "汤姆·汉克斯",
		"Angelababy 杨颖": "Angelababy 杨颖", // 夹着拉丁字母的不动
		"Tom Hanks":     "Tom Hanks",
		"木村 拓哉":         "木村拓哉",
		"송 강호":          "송강호",
		"":              "",
	}
	for in, want := range cases {
		if got := cleanPersonName(in); got != want {
			t.Errorf("cleanPersonName(%q) = %q，预期 %q", in, got, want)
		}
	}
	// 中文名已带空白：去掉；被别的条目占了就记账暂缓
	cfg := defaultPersonFillCfg()
	if p := planPerson(cfg, "周 星驰", "演员", true, true, model.PersonMeta{TmdbID: 1}, false); p.newName != "周星驰" || p.state != "" {
		t.Fatalf("中文名带空格应去掉: %+v", p)
	}
	if p := planPerson(cfg, "周 星驰", "演员", true, true, model.PersonMeta{TmdbID: 1}, true); p.newName != "" || p.state != personStateNameTaken {
		t.Fatalf("去空格后的名字被占应记账: %+v", p)
	}
}

func TestFindDupPersons(t *testing.T) {
	all := []embyPersonBrief{
		{ID: "a", Name: "Tom Hanks", ProviderIds: map[string]string{"Tmdb": "31"}, DateCreated: "2020"},
		{ID: "b", Name: "汤姆·汉克斯", ProviderIds: map[string]string{"tmdb": "31"}, DateCreated: "2024"},
		{ID: "c", Name: "Tom Hanks", ProviderIds: map[string]string{"Tmdb": "31"}, ImageTags: map[string]string{"Primary": "x"}, DateCreated: "2019"},
		{ID: "d", Name: "Solo", ProviderIds: map[string]string{"Tmdb": "40"}},
		{ID: "e", Name: "No Id"},
		{ID: "f", Name: "No Id"},
	}
	gs := findDupPersons(all)
	if len(gs) != 1 || gs[0].TmdbID != 31 {
		t.Fatalf("只有 TMDB 31 是重复组: %+v", gs)
	}
	g := gs[0]
	// 中文名优先于有头像；剩下两个里有头像的排前面
	if g.Keep.ID != "b" || len(g.Drop) != 2 || g.Drop[0].ID != "c" || g.Drop[1].ID != "a" {
		t.Fatalf("留下的应是中文名那个: %+v", g)
	}
	if g.SameName {
		t.Fatal("名字都和留下的不同，不算同名")
	}
	if _, ok := dupNamesOK(g, "Tom Hanks", "汤姆·汉克斯"); !ok {
		t.Fatal("名字都是这个 TMDB 人物的，应通过")
	}
	if bad, ok := dupNamesOK(g, "Tom Hanks", ""); ok || bad != "汤姆·汉克斯" {
		t.Fatalf("中文名不认识时不合并: %q %v", bad, ok)
	}
	// 同名
	same := findDupPersons([]embyPersonBrief{
		{ID: "x", Name: "Tom Hanks", ProviderIds: map[string]string{"Tmdb": "31"}},
		{ID: "y", Name: "tom  hanks", ProviderIds: map[string]string{"Tmdb": "31"}},
	})
	if len(same) != 1 || !same[0].SameName {
		t.Fatalf("归一后同名应标出来: %+v", same)
	}
}

func TestRelinkPeople(t *testing.T) {
	keep := embyPersonBrief{ID: "k", Name: "汤姆·汉克斯"}
	people := []any{
		map[string]any{"Id": "o", "Name": "Other", "Type": "Actor"},
		map[string]any{"Id": "d", "Name": "Tom Hanks", "Type": "Actor", "Role": "Forrest", "PrimaryImageTag": "t"},
		map[string]any{"Id": "d", "Name": "Tom Hanks", "Type": "Producer"},
		map[string]any{"Id": "k", "Name": "汤姆·汉克斯", "Type": "Producer"},
	}
	out, changed := relinkPeople(people, "d", keep)
	if !changed || len(out) != 3 {
		t.Fatalf("应换掉演员那项、去掉重复的制片: changed=%v %v", changed, out)
	}
	m := out[1].(map[string]any)
	if m["Id"] != "k" || m["Name"] != "汤姆·汉克斯" || m["Role"] != "Forrest" || m["PrimaryImageTag"] != nil {
		t.Fatalf("换过去的那项不对: %v", m)
	}
	if people[1].(map[string]any)["Id"] != "d" {
		t.Fatal("不该改动传进来的表")
	}
	if _, changed := relinkPeople(people, "zzz", keep); changed {
		t.Fatal("表里没有的条目不算改动")
	}
}

func TestPersonPathSafe(t *testing.T) {
	all := []embyPersonBrief{
		{ID: "k", Path: "/config/metadata/people/汤/汤姆·汉克斯"},
		{ID: "d", Path: "/config/metadata/people/T/Tom Hanks"},
		{ID: "s", Path: `\config\metadata\people\S\Same`},
		{ID: "s2", Path: "/config/metadata/people/S/Same/"},
	}
	if !personPathSafe(all[1], all) {
		t.Fatal("各自的人物目录，可以删")
	}
	if personPathSafe(all[2], all) {
		t.Fatal("与别的人物共用目录（同名），不许删")
	}
	if personPathSafe(embyPersonBrief{ID: "x"}, all) || personPathSafe(embyPersonBrief{ID: "x", Path: "/movies/a"}, all) ||
		personPathSafe(embyPersonBrief{ID: "x", Path: "/config/metadata/people"}, all) {
		t.Fatal("路径为空、不像人物目录、是人物根目录，都不许删")
	}
}

// ---- 端到端 ----

type fakeMergeServer struct {
	mu       sync.Mutex
	persons  map[string]*embyPersonBrief
	titles   map[string][]map[string]any // 片目 id → People
	stubborn map[string]bool             // 写回时不改 People 的片目（模拟 Emby 没换过去）
	deleted  []string
	writes   map[string]int
	bad      []string // 不该发生的请求
}

func (f *fakeMergeServer) linked(pid string) []map[string]any {
	var out []map[string]any
	for id, ps := range f.titles {
		for _, p := range ps {
			if p["Id"] == pid {
				out = append(out, map[string]any{"Id": id})
				break
			}
		}
	}
	return out
}

func (f *fakeMergeServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	p, q := r.URL.Path, r.URL.Query()
	js := func(v any) { _ = json.NewEncoder(w).Encode(v) }
	switch {
	case p == "/3/person/31":
		js(map[string]any{"id": 31, "name": "Tom Hanks", "translations": map[string]any{"translations": []map[string]any{
			{"iso_3166_1": "CN", "iso_639_1": "zh", "data": map[string]any{"name": "汤姆·汉克斯"}}}}})
	case p == "/3/person/32":
		js(map[string]any{"id": 32, "name": "Robin Wright"})
	case p == "/3/person/34":
		js(map[string]any{"id": 34, "name": "Gary Sinise"})
	case p == "/3/person/35":
		js(map[string]any{"id": 35, "name": "Sally Field"})
	case p == "/Persons":
		var items []any
		for _, ps := range f.persons {
			items = append(items, ps)
		}
		js(map[string]any{"Items": items, "TotalRecordCount": len(items)})
	case p == "/Users/Query":
		js(map[string]any{"Items": []any{
			map[string]any{"Id": "u0", "Policy": map[string]any{"IsAdministrator": false}},
			map[string]any{"Id": "u1", "Policy": map[string]any{"IsAdministrator": true}},
		}})
	case p == "/Items" && q.Get("PersonIds") != "":
		items := f.linked(q.Get("PersonIds"))
		js(map[string]any{"Items": items, "TotalRecordCount": len(items)})
	case p == "/Items" && q.Get("IncludeItemTypes") == "Movie,Series":
		js(map[string]any{"Items": []any{}, "TotalRecordCount": 0})
	case p == "/Items" && q.Get("Ids") != "":
		var items []any
		if ps := f.persons[q.Get("Ids")]; ps != nil {
			items = append(items, map[string]any{"Id": ps.ID, "Name": ps.Name, "Type": "Person",
				"ProviderIds": map[string]any{"Tmdb": ps.ProviderIds["Tmdb"]}})
		}
		js(map[string]any{"Items": items})
	case strings.HasPrefix(p, "/Users/u1/Items/"):
		id := strings.TrimPrefix(p, "/Users/u1/Items/")
		if f.persons[id] != nil {
			f.bad = append(f.bad, "按用户读人物条目 "+id)
		}
		ps, ok := f.titles[id]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		people := make([]any, len(ps))
		for i, x := range ps {
			people[i] = x
		}
		js(map[string]any{"Id": id, "Name": "片目" + id, "People": people, "SomeOtherField": "keep"})
	case r.Method == http.MethodPost && strings.HasPrefix(p, "/Items/"):
		id := strings.TrimPrefix(p, "/Items/")
		var it map[string]any
		_ = json.NewDecoder(r.Body).Decode(&it)
		if it["SomeOtherField"] != "keep" {
			f.bad = append(f.bad, "写回没带完整 DTO "+id)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		f.writes[id]++
		if f.stubborn[id] {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		var ps []map[string]any
		for _, v := range it["People"].([]any) {
			ps = append(ps, v.(map[string]any))
		}
		f.titles[id] = ps
		w.WriteHeader(http.StatusNoContent)
	case r.Method == http.MethodDelete && strings.HasPrefix(p, "/Items/"):
		id := strings.TrimPrefix(p, "/Items/")
		if f.persons[id] == nil {
			f.bad = append(f.bad, "删了不是人物的条目 "+id)
		}
		if len(f.linked(id)) > 0 {
			f.bad = append(f.bad, "删了还有片目挂着的人物 "+id)
		}
		f.deleted = append(f.deleted, id)
		delete(f.persons, id)
		w.WriteHeader(http.StatusNoContent)
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

func TestPersonMergeEndToEnd(t *testing.T) {
	newTestDB(t, "person-merge.db")
	old := personMergeWriteGap
	personMergeWriteGap = 0
	defer func() { personMergeWriteGap = old }()

	per := func(id, name, tmdb, path string) *embyPersonBrief {
		return &embyPersonBrief{ID: id, Name: name, Type: "Person", Path: path, ProviderIds: map[string]string{"Tmdb": tmdb}}
	}
	actor := func(id, name, typ string) map[string]any { return map[string]any{"Id": id, "Name": name, "Type": typ} }
	f := &fakeMergeServer{
		persons: map[string]*embyPersonBrief{
			// 31：正常合并，英文名那个挂在一部电影和一集上（集里留下的人物已经在了）
			"k31": per("k31", "汤姆·汉克斯", "31", "/config/metadata/people/汤/汤姆·汉克斯"),
			"d31": per("d31", "Tom Hanks", "31", "/config/metadata/people/T/Tom Hanks"),
			// 32：有个条目的名字不是这个 TMDB 人物的（编号认错了）
			"k32": per("k32", "Robin Wright", "32", "/config/metadata/people/R/Robin Wright"),
			"d32": per("d32", "Someone Else", "32", "/config/metadata/people/S/Someone Else"),
			// 33：同名
			"k33": per("k33", "Same Name", "33", "/config/metadata/people/S/Same Name"),
			"d33": per("d33", "same name", "33", "/config/metadata/people/S/same name"),
			// 34：Emby 写回后没换过去
			"k34": per("k34", "加里·西尼斯", "34", "/config/metadata/people/加/加里·西尼斯"),
			"d34": per("d34", "Gary Sinise", "34", "/config/metadata/people/G/Gary Sinise"),
			// 35：重复条目的目录与别的人物重叠
			"k35": per("k35", "莎莉·菲尔德", "35", "/config/metadata/people/莎/莎莉·菲尔德"),
			"d35": per("d35", "Sally Field", "35", "/config/metadata/people/X/Shared"),
			"o35": per("o35", "Other", "99", "/config/metadata/people/X/Shared"),
		},
		titles: map[string][]map[string]any{
			"m1": {actor("x1", "Other", "Actor"), actor("d31", "Tom Hanks", "Actor")},
			"e1": {actor("k31", "汤姆·汉克斯", "GuestStar"), actor("d31", "Tom Hanks", "GuestStar")},
			"m2": {actor("d32", "Someone Else", "Actor")},
			"m3": {actor("d33", "same name", "Actor")},
			"m4": {actor("d34", "Gary Sinise", "Actor")},
			"m5": {actor("d35", "Sally Field", "Actor")},
		},
		stubborn: map[string]bool{"m4": true},
		writes:   map[string]int{},
	}
	f.persons["k31"].ImageTags = map[string]string{"Primary": "x"}
	srv := httptest.NewServer(f)
	defer srv.Close()

	model.DB.Create(&model.TmdbConfig{ApiKey: "k", ApiUrl: srv.URL, ImageApiUrl: srv.URL, Language: "zh-CN"})
	model.DB.Create(&model.PersonMeta{TmdbID: 34, Name: "Gary Sinise", ZhName: "加里·西尼斯", FetchedAt: time.Now()})
	model.DB.Create(&model.PersonMeta{TmdbID: 35, Name: "Sally Field", ZhName: "莎莉·菲尔德", FetchedAt: time.Now()})
	dir := t.TempDir()
	h := &Handler{DB: model.DB, Config: &config.Config{ConfigDir: dir, DataDir: dir}}
	emby, _ := json.Marshal(map[string]string{"server_url": srv.URL, "api_key": "ek"})
	_ = h.Config.SaveSetting("emby", string(emby))

	// 开关关着：一个都不动
	if _, err := execPersonJob(h, &model.TaskJob{Kind: jobKindPerson, Priority: jobPriorityManual}); err != nil {
		t.Fatal(err)
	}
	if len(f.writes)+len(f.deleted) != 0 {
		t.Fatalf("没开合并不该改写也不该删: writes=%v deleted=%v", f.writes, f.deleted)
	}

	cfg := defaultPersonFillCfg()
	cfg.MergeDups = true
	b, _ := json.Marshal(cfg)
	_ = h.Config.SaveSetting(personFillSetting, string(b))
	out, err := execPersonJob(h, &model.TaskJob{Kind: jobKindPerson, Priority: jobPriorityManual})
	if err != nil {
		t.Fatal(err)
	}
	if len(f.bad) > 0 {
		t.Fatalf("出现了不该有的请求: %v", f.bad)
	}
	if strings.Join(f.deleted, ",") != "d31" {
		t.Fatalf("只该删 d31: %v", f.deleted)
	}
	if ps := f.titles["m1"]; len(ps) != 2 || ps[1]["Id"] != "k31" || ps[1]["Name"] != "汤姆·汉克斯" {
		t.Fatalf("m1 应换成留下的人物: %v", ps)
	}
	if ps := f.titles["e1"]; len(ps) != 1 || ps[0]["Id"] != "k31" {
		t.Fatalf("e1 已有留下的人物，重复那项应去掉: %v", ps)
	}
	if f.writes["m2"]+f.writes["m3"] != 0 {
		t.Fatal("名字对不上、同名的组不该改写片目")
	}
	if f.persons["d34"] == nil || f.writes["m4"] != 1 {
		t.Fatal("Emby 没换过去时不许删")
	}
	if f.persons["d35"] == nil || f.titles["m5"][0]["Id"] != "k35" {
		t.Fatalf("目录重叠：片目照换、条目不删: %v", f.titles["m5"])
	}
	res := out.Result.(personJobResult)
	if res.Merged != 1 || res.Relinked != 4 {
		t.Fatalf("统计不对: %+v", res)
	}
	joined := strings.Join(res.Problems, "\n")
	for _, want := range []string{"Someone Else", "同名", "没换过去", "重叠"} {
		if !strings.Contains(joined, want) {
			t.Errorf("问题清单缺「%s」: %s", want, joined)
		}
	}
	if !strings.Contains(out.Message, "重复人物 5 组，合并 1 个条目") {
		t.Fatalf("消息不对: %s", out.Message)
	}
}
