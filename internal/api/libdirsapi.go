package api

import (
	"fmt"
	"log"
	"net/http"
	"path/filepath"
	"strings"
	"sync"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
	"gopkg.in/yaml.v3"
	"gorm.io/gorm"
)

// ==================== 媒体库目录表：启用（迁移）与接口（设计见 docs/115-station-notes/LIBRARY-DIRS-PLAN.md） ====================
//
// 启用 = 把老的分类规则与其他媒体库目录迁成目录表 + 打开 library_dirs_active。迁移只读老配置、不删不改（回滚到老版本时还在）。
// 要各库的目录名（识别按库名编译），拿不到（115 不可用、还没配媒体库）就不迁、不打开关，下次再试：
// 启动时试一次，打开「媒体库目录」卡片（GET /library/dirs）时再试。开关没打开前整理与识别都走老配置。

const libDirsMigratedKey = "migrate.library_dirs.v1"

var libDirsMigrateMu sync.Mutex

// libDirBoundLibs 来源定过库的媒体库（迁移给它们复制行、保存时要求它们有兜底）：
// 转存子目录绑定表里的库 + 按来源入库过的整理记录的库（重新整理会沿用，绑定事后解了也一样）
func (h *Handler) libDirBoundLibs() []string {
	main := h.loadFullSyncCfg().Cid
	seen := map[string]bool{}
	var out []string
	add := func(cid string) {
		if cid == "" {
			cid = main
		}
		if cid != "" && !seen[cid] {
			seen[cid] = true
			out = append(out, cid)
		}
	}
	for _, s := range loadStagingLibs() {
		if s.Lib != "" {
			add(s.Lib)
		}
	}
	if h.DB != nil {
		var cids []string
		h.DB.Model(&model.OrganizeRecord{}).Where("lib_by_source = ?", true).Distinct().Pluck("lib_cid", &cids)
		for _, c := range cids {
			add(c)
		}
	}
	return out
}

// ledgerVideoPaths 台账全部视频的相对路径
func ledgerVideoPaths() []string {
	var rels []string
	if model.DB != nil {
		model.DB.Model(&model.SyncedFile{}).Where("kind = ?", "video").Pluck("rel_path", &rels)
	}
	return rels
}

// ensureLibraryDirs 还没启用就迁移并启用。返回是否已启用，没启用时说明原因
func (h *Handler) ensureLibraryDirs() (bool, string) {
	if libraryDirsActive() {
		return true, ""
	}
	libDirsMigrateMu.Lock()
	defer libDirsMigrateMu.Unlock()
	forgetLibraryDirs()
	if libraryDirsActive() {
		return true, ""
	}
	if h.loadFullSyncCfg().Cid == "" {
		return false, "还没有配置 115 媒体库目录"
	}
	cookie, _ := h.get115Cookie()
	roots, err := h.resolveConfiguredLibRoots(cookie)
	if err != nil {
		return false, "读不出各媒体库的目录名（115 暂时不可用？）：" + err.Error()
	}
	return h.activateLibraryDirs(roots)
}

// activateLibraryDirs 迁移并打开开关（库名已拿到）。调用方持有 libDirsMigrateMu
func (h *Handler) activateLibraryDirs(roots []libRoot) (bool, string) {
	var rules []model.CategoryRule
	h.DB.Where("media_type IN ?", []string{"movie", "tv"}).Find(&rules)
	videos := ledgerVideoPaths()
	mig, err := planLibraryDirMigration(rules, loadLibExtraDirs(), roots, videos, h.libDirBoundLibs())
	if err != nil {
		return false, err.Error()
	}
	if err := replaceLibraryDirs(h.DB, mig.Rows); err != nil {
		return false, "写入媒体库目录表失败：" + err.Error()
	}
	if err := h.Config.SaveSetting(libraryDirsActiveKey, "1"); err != nil {
		return false, "打开开关失败：" + err.Error()
	}
	_ = h.Config.SaveSetting(libDirsMigratedKey, "1")
	forgetLibraryDirs()

	// 识别前后对一遍（设计文档 §6 G2）：对不上只告警，老配置还在，可人工处理
	old := loadRuleLayout().withExtraDirs(loadLibExtraDirs())
	neu := compileLibraryDirs(mig.Rows, libDirLibNames(mig.Rows))
	diff := 0
	for _, v := range videos {
		o, okO := old.titleAt(v)
		n, okN := neu.titleAt(v)
		if okO != okN || o != n {
			if diff < 5 {
				log.Printf("[媒体库目录] ⚠ 迁移前后识别不一致：%s（老 %v %s → 新 %v %s）", v, okO, o.Key, okN, n.Key)
			}
			diff++
		}
	}
	log.Printf("[媒体库目录] ✓ 已启用：%d 条分类规则、%d 个其他媒体库目录迁成 %d 行；识别不一致 %d 个视频",
		len(rules), len(loadLibExtraDirs()), len(mig.Rows), diff)
	for _, n := range mig.Notes {
		log.Printf("[媒体库目录] ○ %s", n)
	}
	return true, ""
}

// MigrateLibraryDirs 启动时试一次（routes.go）：拿不到库名就等下次
func (h *Handler) MigrateLibraryDirs() {
	if h.Config == nil || h.DB == nil {
		return
	}
	if ok, why := h.ensureLibraryDirs(); !ok && why != "" {
		log.Printf("[媒体库目录] ○ 暂不启用，继续用分类规则：%s", why)
	}
}

// replaceLibraryDirs 整表替换（事务）
func replaceLibraryDirs(db *gorm.DB, rows []model.LibraryDir) error {
	return db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Where("1 = 1").Delete(&model.LibraryDir{}).Error; err != nil {
			return err
		}
		for i := range rows {
			rows[i].ID = 0
			rows[i].Priority = i + 1
		}
		if len(rows) == 0 {
			return nil
		}
		return tx.Create(&rows).Error
	})
}

// libDirLib 界面上的一个库
type libDirLib struct {
	Cid  string `json:"cid"`
	Name string `json:"name"`
	Abs  string `json:"abs"`
	Main bool   `json:"main"`
}

// libDirView 界面上的一行：表里的字段 + 片目数、本地目录、库是否还在配置里
type libDirView struct {
	model.LibraryDir
	Titles     int    `json:"titles"`
	Local      string `json:"local"`
	LibMissing bool   `json:"lib_missing"`
}

// refreshLibDirNames 库名快照跟着 115 走（设计文档 §4 R6）：库改了名、或原来认不出的库加回了配置
func refreshLibDirNames(db *gorm.DB, roots []libRoot) int {
	var rows []model.LibraryDir
	db.Find(&rows)
	n := 0
	for _, r := range rows {
		name := rootName(roots, r.LibCid)
		if name != "" && name != r.LibName {
			db.Model(&model.LibraryDir{}).Where("id = ?", r.ID).Update("lib_name", name)
			n++
		}
	}
	if n > 0 {
		forgetLibraryDirs()
	}
	return n
}

// libDirViews 行 → 界面行（片目数按台账算，零 115 请求）
func libDirViews(rows []model.LibraryDir, roots []libRoot, titles map[string]*ledgerTitleEntry, localRoot string) []libDirView {
	count := map[string]int{}
	for _, e := range titles {
		count[e.LibName+"\x00"+e.Category]++
	}
	out := make([]libDirView, 0, len(rows))
	for _, r := range rows {
		v := libDirView{LibraryDir: r, Titles: count[r.LibName+"\x00"+r.Path], LibMissing: rootName(roots, r.LibCid) == ""}
		if localRoot != "" && r.LibName != "" {
			v.Local = filepath.ToSlash(filepath.Join(localRoot, r.LibName, filepath.FromSlash(r.Path)))
		}
		out = append(out, v)
	}
	return out
}

func (h *Handler) libDirRoots() ([]libRoot, error) {
	cookie, _ := h.get115Cookie()
	return h.resolveConfiguredLibRoots(cookie)
}

func libDirLibsOf(roots []libRoot) []libDirLib {
	out := make([]libDirLib, 0, len(roots))
	for i, r := range roots {
		out = append(out, libDirLib{Cid: r.Cid, Name: r.Name, Abs: r.Abs, Main: i == 0})
	}
	return out
}

// GetLibraryDirs GET /library/dirs
func (h *Handler) GetLibraryDirs(c *gin.Context) {
	if ok, why := h.ensureLibraryDirs(); !ok {
		c.JSON(http.StatusOK, gin.H{"active": false, "reason": why})
		return
	}
	roots, err := h.libDirRoots()
	if err != nil {
		roots = nil // 认不出库名时照样列出来，库名用快照
	} else {
		refreshLibDirNames(h.DB, roots)
	}
	var rows []model.LibraryDir
	h.DB.Order("priority ASC, id ASC").Find(&rows)
	c.JSON(http.StatusOK, gin.H{
		"active": true, "rows": libDirViews(rows, roots, scanLedgerTitlesCached(), localMediaRoot()),
		"libs": libDirLibsOf(roots), "check": validateLibraryDirs(rows, h.libDirBoundLibs()),
		"unclaimed": ledgerUnclaimedCached(),
	})
}

// normLibDirRow 规范化界面传来的一行；库要在配置里（库名取当前的）。原来就认不出库的行原样保留（不让用户的修改因此存不进去）
func normLibDirRow(r model.LibraryDir, roots []libRoot) (model.LibraryDir, error) {
	parts, err := splitExtraPath(r.Path)
	if err != nil {
		return r, err
	}
	r.Path = strings.Join(parts, "/")
	switch r.MediaType {
	case "movie", "tv", libDirTypeAll:
	default:
		return r, fmt.Errorf("目录「%s」的类型不认识：%s", r.Path, r.MediaType)
	}
	r.LibCid = strings.TrimSpace(r.LibCid)
	if name := rootName(roots, r.LibCid); name != "" {
		r.LibName = name
	} else if r.LibCid == "" {
		return r, fmt.Errorf("目录「%s」没选媒体库", r.Path)
	}
	// 库不在配置里的行（迁移来的、库后来移出了配置）原样留着：校验会提示，整理选中它时报错，
	// 不能因为它让整张表存不进去
	for _, f := range []*string{&r.GenreIds, &r.OriginalLanguage, &r.OriginCountry, &r.ProductionCountries, &r.ReleaseYear, &r.Note} {
		*f = strings.TrimSpace(*f)
	}
	if !r.Organize {
		r.Fallback = false // 仅识别的行不参与整理，兜底没有意义
	}
	return r, nil
}

// SaveLibraryDirs PUT /library/dirs {rows}：整表替换，顺序就是优先级
func (h *Handler) SaveLibraryDirs(c *gin.Context) {
	if !libraryDirsActive() {
		c.JSON(http.StatusConflict, gin.H{"error": "媒体库目录表还没启用"})
		return
	}
	var req struct {
		Rows []model.LibraryDir `json:"rows"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	roots, err := h.libDirRoots()
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "读不出各媒体库（115 暂时不可用？），稍后再存：" + err.Error()})
		return
	}
	rows := make([]model.LibraryDir, 0, len(req.Rows))
	for i, r := range req.Rows {
		r, err := normLibDirRow(r, roots)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": fmt.Sprintf("第 %d 行：%v", i+1, err)})
			return
		}
		r.Priority = i + 1
		rows = append(rows, r)
	}
	chk := validateLibraryDirs(rows, h.libDirBoundLibs())
	if len(chk.Errors) > 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": strings.Join(chk.Errors, "；"), "check": chk})
		return
	}
	if err := replaceLibraryDirs(h.DB, rows); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "保存失败: " + err.Error()})
		return
	}
	forgetLibraryDirs()
	log.Printf("[媒体库目录] ✓ 已保存 %d 行", len(rows))
	var saved []model.LibraryDir
	h.DB.Order("priority ASC, id ASC").Find(&saved)
	c.JSON(http.StatusOK, gin.H{"rows": libDirViews(saved, roots, scanLedgerTitlesCached(), localMediaRoot()), "check": chk})
}

// AddLibraryDir POST /library/dirs/add {lib, path, type}：追加一行仅识别（对账「加入」、海报墙提示用）。
// lib 是库目录名或 cid。同库同路径已有行时什么都不做
func (h *Handler) AddLibraryDir(c *gin.Context) {
	if !libraryDirsActive() {
		c.JSON(http.StatusConflict, gin.H{"error": "媒体库目录表还没启用"})
		return
	}
	var req struct {
		Lib  string `json:"lib"`
		Path string `json:"path"`
		Type string `json:"type"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || strings.TrimSpace(req.Lib) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	roots, err := h.libDirRoots()
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "读不出各媒体库：" + err.Error()})
		return
	}
	cid, err := pickRuleLib(req.Lib, roots)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	mt := req.Type
	if mt != "movie" && mt != "tv" {
		mt = libDirTypeAll
	}
	row, err := normLibDirRow(model.LibraryDir{LibCid: cid, Path: req.Path, MediaType: mt}, roots)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	var n int64
	h.DB.Model(&model.LibraryDir{}).Where("lib_cid = ? AND path = ?", row.LibCid, row.Path).Count(&n)
	if n == 0 {
		var last model.LibraryDir
		h.DB.Order("priority DESC").First(&last)
		row.Priority = last.Priority + 1
		if err := h.DB.Create(&row).Error; err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "保存失败: " + err.Error()})
			return
		}
		forgetLibraryDirs()
		log.Printf("[媒体库目录] ✓ 加入仅识别的目录：%s", libDirLabel(&row))
	}
	c.JSON(http.StatusOK, gin.H{"ok": true, "added": n == 0})
}

// ---- YAML 导入 / 导出（老分类规则的格式，参与整理的行；仅识别的行导出成注释）----

// exportLibraryDirsYAML 行 → 老分类规则 YAML。all 类型两节各写一份；带条件的兜底 YAML 表达不了，写成注释提醒
func exportLibraryDirsYAML(rows []model.LibraryDir, mainCid string) string {
	sec := map[string]*yaml.Node{"movie": {Kind: yaml.MappingNode}, "tv": {Kind: yaml.MappingNode}}
	var notes []string
	str := func(v string) *yaml.Node { return &yaml.Node{Kind: yaml.ScalarNode, Value: v} }
	for _, r := range rows {
		if !r.Organize {
			notes = append(notes, fmt.Sprintf("# 仅识别：%s（%s）", libDirLabel(&r), r.MediaType))
			continue
		}
		fields := &yaml.Node{Kind: yaml.MappingNode}
		for _, kv := range [][2]string{
			{"genre_ids", r.GenreIds}, {"original_language", r.OriginalLanguage}, {"origin_country", r.OriginCountry},
			{"production_countries", r.ProductionCountries}, {"release_year", r.ReleaseYear}, {"custom_regex", r.CustomRegex},
		} {
			if kv[1] != "" {
				fields.Content = append(fields.Content, str(kv[0]), str(kv[1]))
			}
		}
		if r.LibCid != "" && r.LibCid != mainCid {
			lib := r.LibName
			if lib == "" {
				lib = r.LibCid
			}
			fields.Content = append(fields.Content, str("library"), str(lib))
		}
		if r.Fallback && hasCond(r) {
			notes = append(notes, fmt.Sprintf("# 带条件的兜底：%s —— YAML 表达不了「兜底」，导入后要重新勾上", libDirLabel(&r)))
		}
		var val *yaml.Node = fields
		if len(fields.Content) == 0 {
			val = &yaml.Node{Kind: yaml.ScalarNode, Tag: "!!null", Value: ""}
		}
		for _, mt := range []string{"movie", "tv"} {
			if r.MediaType == mt || r.MediaType == libDirTypeAll {
				sec[mt].Content = append(sec[mt].Content, str(r.Path), val)
			}
		}
	}
	root := &yaml.Node{Kind: yaml.MappingNode}
	for _, mt := range []string{"movie", "tv"} {
		root.Content = append(root.Content, str(mt), sec[mt])
	}
	b, _ := yaml.Marshal(root)
	out := "# 媒体库目录表导出（老分类规则格式）。顺序即优先级；没写条件的视为兜底\n" + string(b)
	if len(notes) > 0 {
		out += "\n" + strings.Join(notes, "\n") + "\n"
	}
	return out
}

// ExportLibraryDirs GET /library/dirs/export
func (h *Handler) ExportLibraryDirs(c *gin.Context) {
	var rows []model.LibraryDir
	h.DB.Order("priority ASC, id ASC").Find(&rows)
	c.JSON(http.StatusOK, gin.H{"yaml": exportLibraryDirsYAML(rows, h.loadFullSyncCfg().Cid)})
}

// ImportLibraryDirs POST /library/dirs/import {yaml}：只转换不保存，界面拿去替换「参与整理」的行、用户看过再存
func (h *Handler) ImportLibraryDirs(c *gin.Context) {
	var req struct {
		Yaml string `json:"yaml"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	rules, err := parseCategoryYAML(req.Yaml)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "YAML 解析失败: " + err.Error()})
		return
	}
	roots, err := h.libDirRoots()
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "读不出各媒体库：" + err.Error()})
		return
	}
	mig, err := planLibraryDirMigration(rules, nil, roots, nil, h.libDirBoundLibs())
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"rows": libDirViews(mig.Rows, roots, scanLedgerTitlesCached(), localMediaRoot()), "notes": mig.Notes})
}

// libDirsLockedErr 老的分类规则 / 其他媒体库目录写接口：启用目录表后不再生效，直接拒绝，免得用户以为改了
func libDirsLocked(c *gin.Context) bool {
	if !libraryDirsActive() {
		return false
	}
	c.JSON(http.StatusConflict, gin.H{"error": "已改用「账号与媒体库 → 媒体库目录」，分类规则与其他媒体库目录在那里改"})
	return true
}
