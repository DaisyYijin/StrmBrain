package api

import (
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// ==================== 移出配置的媒体库：本地残留（多个媒体库第 3 期，2026-10-12） ====================
//
// 附加媒体库从配置里删掉之后，它的本地 STRM 与台账行没人管了：全量只标还在配置里的库（失效 STRM
// 按库名前缀分区），增量也不认它的路径。它们会一直出现在海报墙上，Emby 里也一直有。
// 115 主库改过名同样如此（本地第一层还是旧名字）。
//
// 这里只做两件事：列出来、用户点了才清。清理只动本地（STRM / 附属 / 刮削产物 + 台账行），
// 不碰网盘 —— 想要回来，把库加回配置跑一次全量就行。所以不必像深度删除那样层层设防，
// 但「是不是残留」要认准：
//   - 台账路径第一段不是任何一个配置中的库名；
//   - 也不是分类目录的第一段：老台账可能不带库名（「电影/某片/…」），第一段是分类目录，不是库；
//   - 各库名字认不出来（连不上 115）时不下结论，宁可不列。

// libLeftover 一个残留的库目录
type libLeftover struct {
	Name   string `json:"name"`   // 本地第一层目录名
	Files  int64  `json:"files"`  // 台账行数
	Videos int64  `json:"videos"` // 其中视频
	Local  bool   `json:"local"`  // 本地目录还在
}

// ledgerTopCounts 台账按路径第一段计数（只算带 / 的行）
func ledgerTopCounts() (map[string][2]int64, error) {
	var rows []struct {
		Top    string
		Files  int64
		Videos int64
	}
	err := model.DB.Model(&model.SyncedFile{}).
		Select("substr(rel_path, 1, instr(rel_path, '/') - 1) AS top, count(*) AS files, " +
			"sum(CASE WHEN kind = 'video' THEN 1 ELSE 0 END) AS videos").
		Where("instr(rel_path, '/') > 1").Group("top").Scan(&rows).Error
	if err != nil {
		return nil, err
	}
	out := make(map[string][2]int64, len(rows))
	for _, r := range rows {
		out[r.Top] = [2]int64{r.Files, r.Videos}
	}
	return out, nil
}

// findLibLeftovers 哪些第一段是残留（纯函数）。libNames 是配置中各库的目录名，layout 是分类目录表
func findLibLeftovers(counts map[string][2]int64, libNames []string, layout libCategoryLayout) []libLeftover {
	keep := map[string]bool{}
	for _, n := range libNames {
		// 库的本地目录可以多级（电影/主号，libstore.go）：它的第一段整段都不算残留。
		// 代价是「电影/」下不属于任何库的残留这里看不出来 —— 宁可漏报，不能把别的库当残留清掉
		top, _, _ := strings.Cut(normLibLocal(n), "/")
		keep[top] = true
	}
	for dir := range layout.cats {
		top, _, _ := strings.Cut(dir, "/")
		keep[top] = true
	}
	var out []libLeftover
	for top, c := range counts {
		if top == "" || keep[top] {
			continue
		}
		out = append(out, libLeftover{Name: top, Files: c[0], Videos: c[1]})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// libLeftoversNow 当前的残留。认不出各库名字时报错（不下结论）
func (h *Handler) libLeftoversNow() ([]libLeftover, error) {
	if len(effectiveLibraries(h.getSettingValue)) == 0 {
		return nil, nil // 没配媒体库：谈不上残留
	}
	cookie, _ := h.get115Cookie()
	roots, err := h.resolveConfiguredLibRoots(cookie)
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(roots))
	for _, r := range roots {
		if r.Name == "" {
			return nil, errors.New("读不出媒体库目录名（115 暂时不可用？），稍后再看")
		}
		names = append(names, r.Name)
	}
	counts, err := ledgerTopCounts()
	if err != nil {
		return nil, err
	}
	out := findLibLeftovers(counts, names, loadLibCategoryLayout())
	root := localMediaRoot()
	for i := range out {
		if st, err := os.Stat(filepath.Join(root, out[i].Name)); err == nil && st.IsDir() {
			out[i].Local = true
		}
	}
	return out, nil
}

// ListLibLeftovers GET /library/leftovers
func (h *Handler) ListLibLeftovers(c *gin.Context) {
	out, err := h.libLeftoversNow()
	if err != nil {
		c.JSON(http.StatusOK, gin.H{"data": []libLeftover{}, "error": err.Error()})
		return
	}
	if out == nil {
		out = []libLeftover{}
	}
	c.JSON(http.StatusOK, gin.H{"data": out})
}

// CleanLibLeftover POST /library/leftovers/clean  body: {"name": "动漫库"} → 入队，202
func (h *Handler) CleanLibLeftover(c *gin.Context) {
	var req struct {
		Name string `json:"name"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || strings.TrimSpace(req.Name) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	name := strings.TrimSpace(req.Name)
	if _, err := h.checkLibLeftover(name); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	job, err := enqueueJob(h.DB, jobSpec{Kind: "libclean", Title: "清理已移除媒体库的本地残留「" + name + "」",
		DedupeKey: "libclean:" + name, Source: "web", Priority: jobPriorityManual, Params: jobParams{LibName: name}})
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	h.queuedReply(c, job, "清理")
}

// checkLibLeftover 入队时与执行时各认一次：排队期间用户可能把库加回去了
func (h *Handler) checkLibLeftover(name string) (libLeftover, error) {
	if strings.ContainsAny(name, `/\`) || name == "." || name == ".." {
		return libLeftover{}, fmt.Errorf("「%s」不是一个目录名", name)
	}
	list, err := h.libLeftoversNow()
	if err != nil {
		return libLeftover{}, err
	}
	for _, l := range list {
		if l.Name == name {
			return l, nil
		}
	}
	return libLeftover{}, fmt.Errorf("「%s」不是已移出配置的媒体库（它还在配置里、是分类目录，或台账里已经没有它了）", name)
}

func init() {
	jobExecutors["libclean"] = execLibCleanJob
}

// execLibCleanJob 删掉本地 <根>/<库名> 整棵树与它的台账行。只动本地，网盘一个字节都不碰
func execLibCleanJob(h *Handler, job *model.TaskJob) (jobOutcome, error) {
	name := decodeJobParams(job).LibName
	left, err := h.checkLibLeftover(name)
	if err != nil {
		return jobOutcome{}, err
	}
	root := localMediaRoot()
	if st, err := os.Stat(root); err != nil || !st.IsDir() {
		return jobOutcome{}, fmt.Errorf("本地媒体库目录不可访问：%s", root)
	}
	setJobProgress("清理本地残留", 0, 0, name)
	done, err := cleanLibLocal(root, name)
	if err != nil {
		return jobOutcome{}, err
	}
	invalidateLedgerTitles()
	forgetLocalTitles()
	log.Printf("[媒体库] ✓ 已清理移出配置的媒体库「%s」的本地残留：台账 %d 行（视频 %d），本地目录%s",
		name, left.Files, left.Videos, map[bool]string{true: "已删除", false: "本来就不在"}[done])
	return jobOutcome{Message: fmt.Sprintf("已清理「%s」：台账 %d 行（视频 %d 个）%s。Emby 里对应的媒体库请在 Emby 里删除或刷新",
		name, left.Files, left.Videos, map[bool]string{true: "，本地目录已删除", false: ""}[done])}, nil
}

// cleanLibLocal 删掉本地 <root>/<name> 整棵树与台账里这一段开头的行，返回本地目录是否存在过。
// 不用 removeSyncedDir：它按不转义的 LIKE 删台账，库名里带 _ 会顺带匹配到别的库
func cleanLibLocal(root, name string) (bool, error) {
	local := filepath.Join(root, name)
	done := false
	if st, err := os.Stat(local); err == nil && st.IsDir() {
		if err := os.RemoveAll(local); err != nil {
			return false, fmt.Errorf("删除本地目录 %s 失败: %w", local, err)
		}
		done = true
	}
	model.DB.Where(`rel_path LIKE ? ESCAPE '\'`, likeEscape(name)+"/%").Delete(&model.SyncedFile{})
	return done, nil
}
