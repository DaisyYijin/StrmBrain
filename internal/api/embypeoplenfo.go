package api

import (
	"bytes"
	"encoding/xml"
	"fmt"
	"html"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"115-station/internal/model"
)

// ==================== 演职人员补全 → 本地 NFO ====================
//
// 补全只改 Emby 的数据库（人物头像在 Emby 自己的元数据目录，中文名是条目字段），本地 NFO 一直是刮削当时的样子。
// 直接读媒体目录的播放器（Kodi 一系、VidHub、Fileball 等）看的是 NFO：Emby 里中文名带头像，
// 那边是英文名（刮削在补全之前，中文名还没进缓存），头像只是一个 TMDB 链接（国内常连不上）。
// 2026-10-13 用户反馈「第三方软件打开和 Emby 不一致，缺图非常多」。
//
// 所以每部片目补完后顺手对齐这一部的 NFO（电影：STRM 同名 .nfo；剧集：tvshow.nfo）：
//  1. <actor> 的 <name>、<director>、<credits> 换成缓存里的中文名。演员按 <tmdbid> 认；
//     老 NFO 没有 tmdbid 的、以及导演 / 编剧，只用这一轮手上已有的数据认（本轮改名的对照、
//     本轮已经取过的片目演职员表），不为此多发 TMDB 请求 —— 定时任务每次扫全库，一部一次就是几千次
//  2. 演员头像存成 .actors/<名字>.jpg（Kodi 的约定，tinyMediaManager 同款），优先取 Emby 里那张
//     （与 Emby 显示的一致，也不用连 TMDB），Emby 没有再从 TMDB 拉。已有的不重下。
//     <thumb> 的 TMDB 链接保留：Kodi 先找 .actors，连得上 TMDB 的播放器照样能用链接
//
// 只改这三处的文本，NFO 其余内容原样不动（可能是 Emby 或用户改过的）。不碰：
//   - 刮削方式为 Emby：本站一样元数据都不写（scrapeprovider.go）
//   - 网盘镜像的 NFO（台账里有）：那是 115 上的文件同步下来的，本地改了与网盘就是两份
//   - 媒体库目录外的路径（路径映射没对上时 Emby 给的路径原样返回，不能拿去写）
// 改写之前已经由监控上传传过网盘的 NFO，改写后记成「已上传」：115 允许同名文件并存，再传一次网盘上就是两份。

// personNFOImageBudget 一次任务最多下载多少张演员头像。第一次跑时全库每部片都没有 .actors，
// 不设上限一次任务就是几万张图；没下的下次扫到再补
const personNFOImageBudget = 2000

// personNFOStat 这一轮同步本地 NFO 的计数
type personNFOStat struct {
	NFOs   int // 改写的 NFO
	Images int // 新存的演员头像
}

var (
	reNFOActor      = regexp.MustCompile(`(?s)<actor>.*?</actor>`)
	reNFOActorName  = regexp.MustCompile(`(?s)<name>(.*?)</name>`)
	reNFOActorID    = regexp.MustCompile(`<tmdbid>\s*(\d+)\s*</tmdbid>`)
	reNFOActorType  = regexp.MustCompile(`(?s)<type>(.*?)</type>`)
	reNFOActorThumb = regexp.MustCompile(`(?s)<thumb[^>]*>(.*?)</thumb>`)
	reNFOCrew       = regexp.MustCompile(`(?s)<(director|credits)>(.*?)</(?:director|credits)>`)
)

// nfoPerson NFO 里的一位演员（改名之后）
type nfoPerson struct {
	Name   string // 改写后的名字
	Orig   string // NFO 里原来的名字
	TmdbID int
	Thumb  string
}

// nfoNamer 给 NFO 里的一个人定新名字：返回空 = 不改。typ：Actor / Director / Writer
type nfoNamer func(tmdbID int, name, typ string) string

func xmlText(s string) string {
	var b bytes.Buffer
	_ = xml.EscapeText(&b, []byte(s))
	return b.String()
}

// rewriteNFOPeople 换掉 NFO 里演员、导演、编剧的名字（纯函数）。返回新文本与演员列表（按 NFO 里的顺序）
func rewriteNFOPeople(src string, namer nfoNamer) (string, []nfoPerson) {
	var actors []nfoPerson
	out := reNFOActor.ReplaceAllStringFunc(src, func(block string) string {
		m := reNFOActorName.FindStringSubmatchIndex(block)
		if m == nil {
			return block
		}
		orig := strings.TrimSpace(html.UnescapeString(block[m[2]:m[3]]))
		p := nfoPerson{Name: orig, Orig: orig}
		if id := reNFOActorID.FindStringSubmatch(block); id != nil {
			p.TmdbID, _ = strconv.Atoi(id[1])
		}
		if th := reNFOActorThumb.FindStringSubmatch(block); th != nil {
			p.Thumb = strings.TrimSpace(html.UnescapeString(th[1]))
		}
		typ := "Actor"
		if ty := reNFOActorType.FindStringSubmatch(block); ty != nil && strings.TrimSpace(ty[1]) != "" {
			typ = strings.TrimSpace(ty[1])
		}
		// NFO 的 <actor> 里本站只写演员；别的工具写进来的导演之类不进 .actors，名字照样换
		if n := namer(p.TmdbID, orig, typ); n != "" && n != orig {
			p.Name = n
			block = block[:m[2]] + xmlText(n) + block[m[3]:]
		}
		if orig != "" && (typ == "Actor" || typ == "GuestStar") {
			actors = append(actors, p)
		}
		return block
	})
	out = reNFOCrew.ReplaceAllStringFunc(out, func(s string) string {
		m := reNFOCrew.FindStringSubmatch(s)
		orig := strings.TrimSpace(html.UnescapeString(m[2]))
		typ := "Director"
		if m[1] == "credits" {
			typ = "Writer"
		}
		n := namer(0, orig, typ)
		if n == "" || n == orig {
			return s
		}
		return "<" + m[1] + ">" + xmlText(n) + "</" + m[1] + ">"
	})
	return out, actors
}

// actorImageName .actors 下的文件名：Kodi 的规则是名字里的空格换成下划线、去掉文件名非法字符
func actorImageName(name string) string {
	n := strings.ReplaceAll(strings.TrimSpace(name), " ", "_")
	n = strings.Map(func(r rune) rune {
		if r < 32 || strings.ContainsRune(`\/:*?"<>|`, r) {
			return -1
		}
		return r
	}, n)
	n = strings.Trim(n, ". ")
	if n == "" {
		return ""
	}
	return n + ".jpg"
}

// tmdbProfileFromThumb NFO <thumb> 里的 TMDB 链接 → 图片路径（/xxx.jpg）；不是 TMDB 形态的返回空
func tmdbProfileFromThumb(thumb string) string {
	i := strings.Index(thumb, "/t/p/")
	if i < 0 {
		return ""
	}
	rest := thumb[i+len("/t/p/"):] // h632/xxx.jpg
	j := strings.Index(rest, "/")
	if j < 0 || j == len(rest)-1 {
		return ""
	}
	return rest[j:]
}

// embyPersonImage Emby 里人物的头像（转成 jpg）
func embyPersonImage(base, key, pid string) ([]byte, error) {
	resp, err := embyRequest(http.MethodGet, base, key, "/Items/"+url.PathEscape(pid)+"/Images/Primary",
		url.Values{"format": {"jpg"}, "maxWidth": {"600"}}, nil)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return readImageBody(resp.Body, 10<<20)
}

// ledgerHasLocal 本地文件在不在台账里（是不是网盘镜像）。台账路径可能多一截前缀，与 scrapeDirVideoRows 同样两级认
func ledgerHasLocal(root, p string) bool {
	rel := localRelOrEmpty(root, p)
	if rel == "" || model.DB == nil {
		return false
	}
	var n int64
	model.DB.Model(&model.SyncedFile{}).
		Where(`rel_path = ? OR rel_path LIKE ? ESCAPE '\'`, rel, "%/"+likeEscape(rel)).Count(&n)
	return n > 0
}

// titleNFOPath Emby 条目 → 本地 NFO 与 .actors 所在目录。认不出（路径映射没对上、光盘目录等）返回空
func (r *personRunner) titleNFOPath(t embyTitleItem) (nfo, dir string) {
	if t.Path == "" || r.root == "" || r.mapPath == nil {
		return "", ""
	}
	p := filepath.FromSlash(r.mapPath(t.Path))
	switch t.Type {
	case "Movie":
		if !strings.EqualFold(path.Ext(t.Path), ".strm") {
			return "", ""
		}
		dir = filepath.Dir(p)
		nfo = strings.TrimSuffix(p, filepath.Ext(p)) + ".nfo"
	case "Series":
		dir = p
		nfo = filepath.Join(p, "tvshow.nfo")
	default:
		return "", ""
	}
	if rel := localRelOrEmpty(r.root, dir); rel == "" || rel == "." {
		return "", ""
	}
	return nfo, dir
}

// nfoNamer 这一部片目的改名规则：缓存里的中文名（按 TMDB id）→ 本轮的改名对照 → 本轮已取过的演职员表
func (r *personRunner) nfoNamerFor(t embyTitleItem) nfoNamer {
	renamed := map[string]string{}
	for _, res := range r.results {
		if res.Renamed != "" {
			renamed[personNameKey(res.Name)] = res.Renamed
		}
	}
	var cred *titleCredits
	if id, _ := strconv.Atoi(providerID(t.ProviderIds, "Tmdb")); id > 0 {
		cred = r.credits[t.Type+":"+strconv.Itoa(id)] // 只用已经取过的，不为改 NFO 多发请求
	}
	return func(tmdbID int, name, typ string) string {
		if !r.cfg.ZhName || name == "" || hasHan(name) {
			return ""
		}
		if tmdbID <= 0 && cred != nil {
			tmdbID = matchCredit(cred, name, typ)
		}
		if tmdbID > 0 {
			if zh := cachedZhNames([]int{tmdbID})[tmdbID]; zh != "" {
				return zh
			}
		}
		return renamed[personNameKey(name)]
	}
}

// syncTitleNFO 把这一部片目的本地 NFO 与 Emby 对齐（见文件头）
func (r *personRunner) syncTitleNFO(t embyTitleItem) {
	if !r.nfoSync {
		return
	}
	nfo, dir := r.titleNFOPath(t)
	if nfo == "" {
		return
	}
	// 「不刮（交给 Emby）」的库本站一样元数据都不写，NFO 也不改（libopts.go）
	if rel, err := filepath.Rel(r.root, nfo); err == nil && r.scrapePolicy.of(filepath.ToSlash(rel)) == libScrapeOff {
		return
	}
	raw, err := os.ReadFile(nfo)
	if err != nil {
		return // 没有 NFO（Emby 自己刮的、还没刮）就没有可对齐的
	}
	if ledgerHasLocal(r.root, nfo) {
		vlog("[演职人员] ○ 《%s》的 NFO 是网盘镜像，不改写", t.Name)
		return
	}
	text, actors := rewriteNFOPeople(string(raw), r.nfoNamerFor(t))
	if text != string(raw) {
		st0, had := stampOfPath(nfo)
		uploaded := had && model.DB != nil && uploadStampDone(model.DB, nfo, st0)
		if err := writeFileAtomic(nfo, []byte(text)); err != nil {
			vlog("[演职人员] ✗ 《%s》改写 NFO 失败: %v", t.Name, err)
			return
		}
		if uploaded {
			if st, ok := stampOfPath(nfo); ok {
				markUploadedStamp(model.DB, nfo, st)
			}
		}
		r.nfoStat.NFOs++
		vlog("[演职人员] ✓ 《%s》NFO 演职人员名已换成中文", t.Name)
	}
	if r.cfg.Image {
		r.saveActorImages(t, dir, actors)
	}
}

// saveActorImages 演员头像存进 .actors（只看前 MaxCast 位，与补全同一个范围）
func (r *personRunner) saveActorImages(t embyTitleItem, dir string, actors []nfoPerson) {
	if r.cfg.MaxCast > 0 && len(actors) > r.cfg.MaxCast {
		actors = actors[:r.cfg.MaxCast]
	}
	// Emby 里这部片的人物：t.People 是补全之前读的，名字可能还是 NFO 里的原名，两个名字都认
	refs := map[string]embyPersonRef{}
	for _, p := range t.People {
		if p.Type == "Actor" || p.Type == "GuestStar" {
			refs[personNameKey(p.Name)] = p
		}
	}
	actorsDir := filepath.Join(dir, ".actors")
	for _, a := range actors {
		name := actorImageName(a.Name)
		if name == "" || localMetaExists(actorsDir, name) {
			continue
		}
		if r.nfoImgLeft <= 0 {
			return
		}
		var data []byte
		ref, ok := refs[personNameKey(a.Name)]
		if !ok {
			ref, ok = refs[personNameKey(a.Orig)]
		}
		// 只在 Emby 那边确实有头像时去取：没有头像的人每轮都去问一次，白占下载名额
		if ok && (ref.PrimaryImageTag != "" || r.imaged[ref.ID]) {
			r.nfoImgLeft--
			data, _ = embyPersonImage(r.base, r.key, ref.ID)
		}
		if data == nil {
			prof := tmdbProfileFromThumb(a.Thumb)
			if prof == "" && a.TmdbID > 0 {
				var m model.PersonMeta
				if model.DB.First(&m, "tmdb_id = ?", a.TmdbID).Error == nil {
					prof = m.ProfilePath
				}
			}
			if prof == "" {
				continue // Emby 与 TMDB 都没有头像
			}
			r.nfoImgLeft--
			if data, _ = fetchTMDBImage("h632", prof); data == nil {
				continue
			}
		}
		if err := os.MkdirAll(actorsDir, 0o755); err != nil {
			vlog("[演职人员] ✗ 《%s》建 .actors 失败: %v", t.Name, err)
			return
		}
		if err := writeFileAtomic(filepath.Join(actorsDir, name), data); err != nil {
			vlog("[演职人员] ✗ 《%s》存头像 %s 失败: %v", t.Name, name, err)
			continue
		}
		r.nfoStat.Images++
	}
}
