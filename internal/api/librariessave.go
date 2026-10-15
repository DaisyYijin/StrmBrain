package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strings"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// ==================== 媒体库卡片保存：PUT /libraries（MULTI-LIB-SIMPLIFY-PLAN.md §3.2 / §3.6） ====================
//
// 一次保存三份配置：full.extra_libs、staging_libs（转存入口），移除库时还有分类 YAML 与订阅的转存子目录。
// 它们互相引用（入口指着库、规则的 library 指着库、订阅指着入口），分开存、中途失败会留下指向不存在的东西的配置，
// 整理到那一类就报错停下。所以：**先全部校验、再动网盘、最后一起写**。
//
// 设置在 setting.yaml（Config.SaveSettings 一次原子落盘），分类与订阅在 SQLite：
// 数据库写在事务里，设置文件写成功才提交，写失败回滚；提交失败时把设置文件改回去。
// 这一步之前新建的入口子目录在失败时删掉（进回收站）：留着的空子目录会被守望者当成待整理条目一路计数到熔断。
//
// 默认库（full.cid）不在这里改：它是「账号与媒体库」上原来那个必填项，走 SaveSetting。换默认库还会改变
// 整理记录 LibCid 的口径（主库记空），见设计文档 R9。

// libSaveReq PUT /libraries 请求：期望的全部附加库与全部入口（整份替换，不是增量）
type libSaveReq struct {
	ExtraLibs []libRootEntry `json:"extra_libs"`
	Inboxes   []libInboxReq  `json:"inboxes"`
	// CleanLocal 这次被移除的库里，要同时清理本地 STRM 与台账的（库 cid）。保存成功后入 libclean 队列
	CleanLocal []string `json:"clean_local"`
	// Opts 每个库让本站做什么（libopts.go，库 cid → 选项），整份替换；不带这个字段 = 沿用现在的
	Opts map[string]libOpt `json:"opts"`
	// Fallback 「默认放这里」的库 cid（阶段 3）。只在带 Opts 时生效；空或不是本站整理的库 → 第一个本站整理的库
	Fallback string `json:"fallback"`
}

// libInboxReq 一个转存入口：Cid 是已有的子目录；Cid 为空 = 按 Name 在转存目录下建（同名的已存在就复用）
type libInboxReq struct {
	Cid  string `json:"cid"`
	Name string `json:"name"`
	Lib  string `json:"lib"`
	// Confirm 同名子目录已存在且里面有东西时，用户确认过「里面的会整理进这个库」（R6）
	Confirm bool `json:"confirm"`
}

// libInboxConfirm 需要用户确认的入口：同名子目录已存在且不空
type libInboxConfirm struct {
	Name  string `json:"name"`
	Lib   string `json:"lib"`
	Items int    `json:"items"`
}

// libSaveDeps 规划时要问 115 的几件事（测试换成假的）。只在真用得上时调用
type libSaveDeps struct {
	// listRoot 转存目录下一层（只在有按名字新建的入口时调用一次）
	listRoot func() ([]dirEntry, error)
	// countItems 一个子目录里有几项（只对同名已存在的调用）
	countItems func(cid string) (int, error)
	// dirOf 查一个目录的父目录与名字（只对不在旧绑定表里的已有子目录调用）
	dirOf func(cid string) (dirInfo, error)
}

// libSaveInput 规划所需的现状
type libSaveInput struct {
	FullRaw      string
	OldLibs      []libRoot // 当前各库（主库在前），名字要认得出（移除库时按名字找 YAML 里的 library）
	StagingRoot  string
	OldBindings  []stagingLib
	CategoryYAML string // 没存过 YAML（规则来自种子）为空
	OptsRaw      string // 现在的 lib_opts 原文
}

// libSavePlan 规划结果：要写什么
type libSavePlan struct {
	FullJSON    string
	FullChanged bool
	Bindings    []stagingLib // 最终绑定表；Cid 为空的是要新建的
	Removed     []libRoot
	YAML        string
	YAMLChanged bool
	Unbound     map[string]bool // 这次解绑的子目录 cid（订阅里引用它们的要退回）
	Clean       []string        // 要清本地的库名
	OptsJSON    string          // 要写的 lib_opts
	OptsChanged bool
	// ToSelf 这次改成自己整理的库（原来是本站整理）：指向它的规则回默认库、入口解绑，与移除库同一套收尾
	ToSelf []libRoot
	// Fallback 保存后「默认放这里」的库（空 = 一个本站整理的库都没有）
	Fallback        string
	FallbackChanged bool
}

// planLibrariesSave 校验并算出要写的东西。不写任何东西；要用户确认时返回 confirms（不报错）
func planLibrariesSave(in libSaveInput, req libSaveReq, deps libSaveDeps) (libSavePlan, []libInboxConfirm, error) {
	var plan libSavePlan
	cur := parseFullCfg(in.FullRaw)
	primary := strings.TrimSpace(cur.Cid)
	if primary == "" {
		return plan, nil, errors.New("先设置媒体库目录")
	}

	// ---- 附加库 ----
	extras := make([]libRootEntry, 0, len(req.ExtraLibs))
	for i, e := range req.ExtraLibs {
		e.Cid, e.Path = strings.TrimSpace(e.Cid), strings.TrimSpace(e.Path)
		if e.Cid == "" {
			return plan, nil, fmt.Errorf("第 %d 个媒体库没有选目录", i+2)
		}
		if e.Cid == "0" {
			return plan, nil, errors.New("媒体库不能选网盘根目录，请选一个具体的文件夹")
		}
		if e.Cid == primary {
			return plan, nil, errors.New("附加的媒体库和「默认放这里」的那个库是同一个目录")
		}
		extras = append(extras, e)
	}
	full := map[string]any{}
	if in.FullRaw != "" {
		if err := json.Unmarshal([]byte(in.FullRaw), &full); err != nil || full == nil {
			return plan, nil, fmt.Errorf("媒体库配置解析失败: %v", err)
		}
	}
	full["extra_libs"] = extras
	b, _ := json.Marshal(full)
	plan.FullJSON = string(b)
	next := libCidsOf(primary, extras)
	plan.FullChanged = strings.Join(next, ",") != strings.Join(cur.libCids(), ",")
	nextSet := map[string]bool{}
	for _, c := range next {
		nextSet[c] = true
	}
	for _, l := range in.OldLibs {
		if !nextSet[l.Cid] {
			plan.Removed = append(plan.Removed, l)
		}
	}

	// ---- 整理方式 ----
	oldOpts := parseLibOpts(in.OptsRaw)
	opts := libOpts{Libs: map[string]libOpt{}}
	if req.Opts == nil {
		opts = oldOpts // 老客户端不带：沿用（marshalLibOpts 会去掉已移除的库）
	}
	for cid, o := range req.Opts {
		cid = strings.TrimSpace(cid)
		if !nextSet[cid] {
			continue
		}
		if !validLibScrape(o.Scrape) {
			return plan, nil, fmt.Errorf("不认识的刮削方式「%s」", o.Scrape)
		}
		switch o.Mode {
		case "", "organize":
			opts.Libs[cid] = libOpt{Scrape: o.Scrape}
			continue
		case libModeSelf:
		default:
			return plan, nil, fmt.Errorf("不认识的整理方式「%s」", o.Mode)
		}
		if o.Depth < 1 || o.Depth > libOptDepthMax {
			return plan, nil, errors.New("自己整理的库要选片目在第几层（1–3 层）")
		}
		opts.Libs[cid] = o
	}
	for _, l := range in.OldLibs {
		if nextSet[l.Cid] && opts.of(l.Cid).self() && !oldOpts.of(l.Cid).self() {
			plan.ToSelf = append(plan.ToSelf, l)
		}
	}
	if req.Opts != nil {
		opts.Fallback = strings.TrimSpace(req.Fallback)
	}
	// 没指定、指的库被移除了或改成自己整理了：落到第一个本站整理的库；一个都没有就空着（整理线停用，界面上说清楚）
	opts.Fallback = opts.fallbackOf(next)
	plan.Fallback = opts.Fallback
	plan.FallbackChanged = opts.Fallback != oldOpts.fallbackOf(cur.libCids())
	plan.OptsJSON = marshalLibOpts(opts, next)
	plan.OptsChanged = plan.OptsJSON != strings.TrimSpace(in.OptsRaw) && !(plan.OptsJSON == "{}" && strings.TrimSpace(in.OptsRaw) == "")

	// ---- 转存入口 ----
	if len(req.Inboxes) > 0 && in.StagingRoot == "" {
		return plan, nil, errors.New("先设置转存目录，才能给媒体库开转存入口")
	}
	var existing []stagingLib // 有 cid 的，交给 normStagingLibs 统一校验
	var creates []stagingLib
	var confirms []libInboxConfirm
	var rootDirs map[string]dirEntry
	taken := map[string]bool{}
	for _, ib := range req.Inboxes {
		ib.Cid, ib.Name, ib.Lib = strings.TrimSpace(ib.Cid), strings.TrimSpace(ib.Name), strings.TrimSpace(ib.Lib)
		if !nextSet[ib.Lib] {
			return plan, nil, fmt.Errorf("转存入口「%s」指向的媒体库不在这次保存的媒体库里", ib.Name)
		}
		if opts.of(ib.Lib).self() {
			return plan, nil, fmt.Errorf("转存入口「%s」指向的是自己整理的库，本站不往里整理", ib.Name)
		}
		if ib.Cid != "" {
			if taken[ib.Cid] {
				return plan, nil, fmt.Errorf("子目录「%s」绑定了两次", ib.Name)
			}
			taken[ib.Cid] = true
			existing = append(existing, stagingLib{Cid: ib.Cid, Name: ib.Name, Lib: ib.Lib})
			continue
		}
		if ib.Name == "" || strings.ContainsAny(ib.Name, `/\`) || ib.Name == "." || ib.Name == ".." {
			return plan, nil, fmt.Errorf("转存入口的名字「%s」不能为空，也不能带斜杠", ib.Name)
		}
		if rootDirs == nil {
			list, err := deps.listRoot()
			if err != nil {
				return plan, nil, fmt.Errorf("读取转存目录失败: %w", err)
			}
			rootDirs = map[string]dirEntry{}
			for _, e := range list {
				if e.IsDir {
					rootDirs[e.Name] = e
				}
			}
		}
		if hit, ok := rootDirs[ib.Name]; ok {
			if taken[hit.Cid] {
				return plan, nil, fmt.Errorf("转存目录里的「%s」已经是别的入口了", ib.Name)
			}
			taken[hit.Cid] = true
			n, err := deps.countItems(hit.Cid)
			if err != nil {
				return plan, nil, fmt.Errorf("读取子目录「%s」失败: %w", ib.Name, err)
			}
			if n > 0 && !ib.Confirm {
				confirms = append(confirms, libInboxConfirm{Name: ib.Name, Lib: ib.Lib, Items: n})
			}
			existing = append(existing, stagingLib{Cid: hit.Cid, Name: hit.Name, Lib: ib.Lib})
			continue
		}
		for _, c := range creates {
			if c.Name == ib.Name {
				return plan, nil, fmt.Errorf("两个转存入口都叫「%s」", ib.Name)
			}
		}
		creates = append(creates, stagingLib{Name: ib.Name, Lib: ib.Lib})
	}
	if len(confirms) > 0 {
		return plan, confirms, nil
	}
	if len(existing) > 0 {
		norm, err := normStagingLibs(existing, in.StagingRoot, next, stagingLibMap(in.OldBindings), deps.dirOf)
		if err != nil {
			return plan, nil, err
		}
		existing = norm
	}
	plan.Bindings = append(existing, creates...)
	plan.Unbound = map[string]bool{}
	for _, o := range in.OldBindings {
		if !taken[o.Cid] {
			plan.Unbound[o.Cid] = true
		}
	}

	// ---- 移除库、改成自己整理：指向它的规则回默认库 ----
	plan.YAML = in.CategoryYAML
	drop := append(append([]libRoot{}, plan.Removed...), plan.ToSelf...)
	if len(drop) > 0 && strings.TrimSpace(in.CategoryYAML) != "" {
		for _, r := range drop {
			if r.Name == "" {
				return plan, nil, fmt.Errorf("读不出媒体库（cid=%s）的名字，没法确认分类规则里有没有指向它：稍后再试", r.Cid)
			}
		}
		out, changed, err := rewriteCategoryLibrary(in.CategoryYAML, func(_, _, old string) (string, bool) {
			for _, r := range drop {
				if libValueMatches(old, r) {
					return "", false
				}
			}
			return old, true
		})
		if err != nil {
			return plan, nil, fmt.Errorf("要把指向这个库的分类规则改回默认库，但%v", err)
		}
		plan.YAML, plan.YAMLChanged = out, changed
	}

	// ---- 清本地 ----
	removedByCid := map[string]libRoot{}
	for _, r := range plan.Removed {
		removedByCid[r.Cid] = r
	}
	for _, cid := range req.CleanLocal {
		r, ok := removedByCid[strings.TrimSpace(cid)]
		if !ok {
			return plan, nil, errors.New("只能清理这次移除的媒体库的本地文件")
		}
		if r.Name == "" {
			return plan, nil, fmt.Errorf("读不出媒体库（cid=%s）的名字，没法清理本地", r.Cid)
		}
		plan.Clean = append(plan.Clean, r.Name)
	}
	return plan, nil, nil
}

// subscribeCfgWithoutSubdir 订阅全局默认的转存子目录被解绑时清空它（空 = 转存目录本身，运行时也是这么退的）
func subscribeCfgWithoutSubdir(raw string, unbound map[string]bool) (string, bool) {
	if raw == "" || len(unbound) == 0 {
		return raw, false
	}
	m := map[string]any{}
	if json.Unmarshal([]byte(raw), &m) != nil || m == nil {
		return raw, false
	}
	sd, _ := m["subdir"].(string)
	if !unbound[strings.TrimSpace(sd)] {
		return raw, false
	}
	m["subdir"] = ""
	b, _ := json.Marshal(m)
	return string(b), true
}

// SaveLibraries PUT /libraries
func (h *Handler) SaveLibraries(c *gin.Context) {
	var req libSaveReq
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	fullRaw := h.getSettingValue("full")
	cookie, _ := h.get115Cookie()

	// 现有各库的名字：移除库时要按名字找 YAML 里的 library。缓存里没有才问一次 115
	sources := libNameSources(fullRaw)
	oldLibs := make([]libRoot, len(sources))
	for i, s := range sources {
		oldLibs[i] = s.libRootOf()
		if oldLibs[i].Name == "" && cookie != "" {
			if info, err := get115DirInfo(cookie, s.Cid); err == nil {
				oldLibs[i].Name = info.n
			}
		}
	}
	var catRule model.ScrapeRule
	h.DB.Where("type = ?", "category_config").First(&catRule)

	var ops *pan115Ops
	needOps := func() (*pan115Ops, error) {
		if ops != nil {
			return ops, nil
		}
		o, err := h.newPan115Ops()
		if err != nil {
			return nil, err
		}
		ops = o
		return ops, nil
	}
	stagingRoot := h.shareFolderCid()
	plan, confirms, err := planLibrariesSave(libSaveInput{
		FullRaw: fullRaw, OldLibs: oldLibs, StagingRoot: stagingRoot,
		OldBindings: loadStagingLibs(), CategoryYAML: catRule.Config, OptsRaw: h.getSettingValue(libOptsKey),
	}, req, libSaveDeps{
		listRoot: func() ([]dirEntry, error) {
			o, err := needOps()
			if err != nil {
				return nil, err
			}
			return listPendingTopLevel(o, stagingRoot)
		},
		countItems: func(cid string) (int, error) {
			o, err := needOps()
			if err != nil {
				return 0, err
			}
			_, n, err := o.listEntries(cid, 0)
			return n, err
		},
		dirOf: func(cid string) (dirInfo, error) {
			if cookie == "" {
				return dirInfo{}, errors.New("未登录 115")
			}
			return get115DirInfo(cookie, cid)
		},
	})
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if len(confirms) > 0 {
		c.JSON(http.StatusConflict, gin.H{"error": "转存目录里已有同名子目录，里面有东西", "confirm": confirms})
		return
	}
	if plan.FullChanged {
		// 互不包含、库目录不重名（workspace.go），与 SaveSetting 同一道校验
		if err := h.guardWorkspaceSetting("full", plan.FullJSON); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
	}

	// ---- 动网盘：新建入口子目录 ----
	var created []string
	dropCreated := func() {
		if len(created) == 0 || ops == nil {
			return
		}
		if err := ops.deleteFiles(created); err != nil {
			log.Printf("[媒体库] ✗ 保存失败后删除刚建的转存子目录失败（请到转存目录里手动删掉空目录）: %v", err)
		}
	}
	for i := range plan.Bindings {
		if plan.Bindings[i].Cid != "" {
			continue
		}
		o, err := needOps()
		if err != nil {
			dropCreated()
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		cid, err := o.mkdir(stagingRoot, plan.Bindings[i].Name)
		if err != nil {
			dropCreated()
			c.JSON(http.StatusBadGateway, gin.H{"error": fmt.Sprintf("新建转存子目录「%s」失败: %v", plan.Bindings[i].Name, err)})
			return
		}
		plan.Bindings[i].Cid = cid
		created = append(created, cid)
	}

	// ---- 一起写 ----
	settings := map[string]string{}
	if plan.FullChanged {
		settings["full"] = plan.FullJSON
	}
	bindingsJSON, _ := json.Marshal(plan.Bindings)
	if string(bindingsJSON) != h.getSettingValue(stagingLibsKey) {
		settings[stagingLibsKey] = string(bindingsJSON)
	}
	if plan.OptsChanged {
		settings[libOptsKey] = plan.OptsJSON
	}
	if v, ok := subscribeCfgWithoutSubdir(h.getSettingValue("subscribe"), plan.Unbound); ok {
		settings["subscribe"] = v
	}
	old := map[string]string{}
	for k := range settings {
		old[k] = h.getSettingValue(k)
	}
	var newRows []model.CategoryRule
	if plan.YAMLChanged {
		if newRows, err = parseCategoryYAML(plan.YAML); err != nil || len(newRows) == 0 {
			dropCreated()
			c.JSON(http.StatusBadRequest, gin.H{"error": "改写后的分类 YAML 解析失败，已放弃保存"})
			return
		}
	}
	unbound := make([]string, 0, len(plan.Unbound))
	for cid := range plan.Unbound {
		unbound = append(unbound, cid)
	}
	tx := h.DB.Begin()
	fail := func(status int, msg string) {
		tx.Rollback()
		dropCreated()
		c.JSON(status, gin.H{"error": msg})
	}
	if plan.YAMLChanged {
		catRule.Type, catRule.Enabled, catRule.Config = "category_config", true, plan.YAML
		if err := tx.Save(&catRule).Error; err != nil {
			fail(http.StatusInternalServerError, "保存分类 YAML 失败: "+err.Error())
			return
		}
		if err := tx.Where("media_type IN ?", []string{"movie", "tv"}).Delete(&model.CategoryRule{}).Error; err != nil {
			fail(http.StatusInternalServerError, "重建分类规则失败: "+err.Error())
			return
		}
		if err := tx.Create(&newRows).Error; err != nil {
			fail(http.StatusInternalServerError, "重建分类规则失败: "+err.Error())
			return
		}
	}
	subsMoved := int64(0)
	if len(unbound) > 0 {
		// 指定了被解绑子目录的订阅改成「转存目录本身」：运行时（subRunner.targetOf）本来就是这么退的，这里只是落到配置上
		res := tx.Model(&model.Subscription{}).Where("subdir IN ?", unbound).Update("subdir", "root")
		if res.Error != nil {
			fail(http.StatusInternalServerError, "更新订阅失败: "+res.Error.Error())
			return
		}
		subsMoved = res.RowsAffected
	}
	if err := h.Config.SaveSettings(settings); err != nil {
		fail(http.StatusInternalServerError, "保存配置失败: "+err.Error())
		return
	}
	if err := tx.Commit().Error; err != nil {
		if rerr := h.Config.SaveSettings(old); rerr != nil {
			log.Printf("[媒体库] ✗ 数据库提交失败后还原设置也失败了，媒体库与分类配置可能对不上: %v", rerr)
		}
		dropCreated()
		c.JSON(http.StatusInternalServerError, gin.H{"error": "保存失败: " + err.Error()})
		return
	}

	// ---- 收尾 ----
	forgetClassifyRules()
	if plan.OptsChanged || plan.FullChanged {
		// 片目认在哪一层变了：海报墙、刮削、对账读的台账聚合都得重算
		forgetLedgerTitles()
		forgetLocalTitles()
	}
	if plan.FallbackChanged {
		if plan.Fallback == "" {
			log.Printf("[媒体库] ⚠ 现在没有本站整理的媒体库：自动整理、转存后整理、订阅都停着")
		} else {
			log.Printf("[媒体库] ✓ 「默认放这里」改到 cid=%s：二级分类里没写 library 的规则以后整理进它", plan.Fallback)
		}
	}
	for _, r := range plan.ToSelf {
		log.Printf("[媒体库] ✓ 「%s」改成自己整理：本站不再往里整理，指向它的分类规则回默认库、转存入口解绑", r.Name)
	}
	for _, r := range plan.Removed {
		log.Printf("[媒体库] ✓ 已移除媒体库「%s」（cid=%s），网盘上的目录不动", r.Name, r.Cid)
	}
	if plan.YAMLChanged {
		log.Printf("[媒体库] ✓ 指向已移除媒体库的分类规则已改回默认库")
	}
	if subsMoved > 0 {
		log.Printf("[媒体库] ✓ %d 个订阅原来转存到已解绑的子目录，改为转存目录本身", subsMoved)
	}
	log.Printf("[媒体库] ✓ 媒体库已保存：%d 个库、%d 个转存入口（新建 %d 个）", len(parseFullCfg(plan.FullJSON).libCids()), len(plan.Bindings), len(created))
	var jobs []uint
	for _, name := range plan.Clean {
		job, err := enqueueJob(h.DB, jobSpec{Kind: "libclean", Title: "清理已移除媒体库的本地残留「" + name + "」",
			DedupeKey: "libclean:" + name, Source: "web", Priority: jobPriorityManual, Params: jobParams{LibName: name}})
		if err != nil {
			log.Printf("[媒体库] ✗ 清理本地「%s」入队失败（可在「媒体库」的残留提示里再点一次）: %v", name, err)
			continue
		}
		jobs = append(jobs, job.ID)
	}
	c.JSON(http.StatusOK, gin.H{"message": "已保存", "jobs": jobs, "subs_moved": subsMoved, "data": h.librariesNow()})
}
