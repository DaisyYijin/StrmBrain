package api

import (
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// ==================== 自己整理的库：片目层级预览（LIBRARY-ROLES-PLAN.md §2.2） ====================
//
// 片目在第几层是用户声明的，不推断。声明完马上按台账给预览：认出几部、各是什么、哪些看着不对，
// 用户一眼就知道选对了没有。只读台账，零 115 请求；还没同步过的库预览是空的，界面提示先跑一次全量。

// libPreviewTitle 预览里的一部
type libPreviewTitle struct {
	Key       string `json:"key"`   // 含库名的片目 key（同海报墙）
	Title     string `json:"title"` // 片目目录名（平铺片目是视频名）
	MediaType string `json:"media_type"`
	Videos    int    `json:"videos"`
	// Odd 看着不像一部片的原因：像季目录（层级选深了）、里面还有带年份的子目录（层级选浅了）
	Odd string `json:"odd,omitempty"`
}

// libPreview 一个库按某个层级认出来的片目
type libPreview struct {
	Lib    string            `json:"lib"`
	Depth  int               `json:"depth"`
	Total  int               `json:"total"`
	Titles []libPreviewTitle `json:"titles"` // 最多 libPreviewMax 部，看着不对的排前面
	Odd    int               `json:"odd"`    // 看着不对的部数
	// Loose 比片目层还浅、不属于任何片目的视频数（比如分类目录里直接放的剧集文件）
	Loose int `json:"loose"`
}

const libPreviewMax = 200

var (
	reSeasonLikeDir = regexp.MustCompile(`(?i)^(season\s*\d+|s\d{1,2}|第.{1,3}季|specials?|特别篇|extras?|featurettes?)$`)
	reYearInDir     = regexp.MustCompile(`(?:^|[^0-9])(?:19|20)\d{2}(?:[^0-9]|$)`)
)

// previewSelfTitles 纯函数：库 name 下的台账视频（rels 含库名前缀）按 depth 认片目
func previewSelfTitles(name string, depth int, rels []string) libPreview {
	layout := libCategoryLayout{self: map[string]int{name: depth}}
	out := libPreview{Lib: name, Depth: depth, Titles: []libPreviewTitle{}}
	byKey := map[string]*libPreviewTitle{}
	order := []string{}
	deeper := map[string]bool{} // 片目里还有像一部片的子目录
	for _, rel := range rels {
		key, titleDir, _, mt, ok := layout.titleOf(rel)
		if !ok {
			out.Loose++
			continue
		}
		t, seen := byKey[key]
		if !seen {
			t = &libPreviewTitle{Key: key, Title: titleDir, MediaType: mt}
			byKey[key] = t
			order = append(order, key)
		}
		t.Videos++
		if t.MediaType == "" && parseFileName(strings.TrimSuffix(lastSeg(rel), ".strm")).Episode > 0 {
			t.MediaType = "tv"
		}
		// 片目下一层的子目录名带年份、又不是季目录：多半是又一部片，层级选浅了
		if rest := strings.TrimPrefix(rel, key+"/"); rest != rel {
			if child, _, more := strings.Cut(rest, "/"); more && !reSeasonLikeDir.MatchString(child) && reYearInDir.MatchString(child) {
				deeper[key] = true
			}
		}
	}
	for _, k := range order {
		t := byKey[k]
		if t.MediaType == "" {
			t.MediaType = "movie"
		}
		switch {
		case reSeasonLikeDir.MatchString(t.Title):
			t.Odd = "像季目录，层级可能选深了"
		case deeper[k]:
			t.Odd = "里面像是还有好几部片，层级可能选浅了"
		}
		if t.Odd != "" {
			out.Odd++
		}
	}
	out.Total = len(order)
	sort.SliceStable(order, func(i, j int) bool {
		return (byKey[order[i]].Odd != "") && (byKey[order[j]].Odd == "")
	})
	for i, k := range order {
		if i >= libPreviewMax {
			break
		}
		out.Titles = append(out.Titles, *byKey[k])
	}
	return out
}

func lastSeg(p string) string {
	if i := strings.LastIndex(p, "/"); i >= 0 {
		return p[i+1:]
	}
	return p
}

// PreviewLibrary GET /libraries/preview?cid=&depth= ：按这个层级，这个库的台账能认出哪些片目
func (h *Handler) PreviewLibrary(c *gin.Context) {
	cid := strings.TrimSpace(c.Query("cid"))
	depth, _ := strconv.Atoi(c.Query("depth"))
	if depth < 1 || depth > libOptDepthMax {
		c.JSON(http.StatusBadRequest, gin.H{"error": "层级只能是 1–3"})
		return
	}
	name := ""
	for _, s := range libNameSources(h.getSettingValue("full")) {
		if s.Cid == cid {
			name = s.libRootOf().Name
		}
	}
	if name == "" {
		// 新加的库还没保存，或名字还没进缓存：按前端带来的名字查（只用来拼台账前缀，查不到就是空预览）
		name = strings.Trim(strings.TrimSpace(c.Query("name")), "/")
	}
	if name == "" || strings.Contains(name, "/") {
		c.JSON(http.StatusOK, libPreview{Depth: depth, Titles: []libPreviewTitle{}})
		return
	}
	var rels []string
	h.DB.Model(&model.SyncedFile{}).Where(`kind = ? AND rel_path LIKE ? ESCAPE '\'`, "video", likeEscape(name)+"/%").
		Pluck("rel_path", &rels)
	c.JSON(http.StatusOK, previewSelfTitles(name, depth, rels))
}
