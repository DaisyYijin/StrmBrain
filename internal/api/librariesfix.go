package api

import (
	"encoding/json"
	"log"
	"net/http"
	"strings"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// ==================== 库在 115 上改了名：一键把分类规则里的旧名换成新名（MULTI-LIB-SIMPLIFY-PLAN.md R4） ====================
//
// 二级分类的 library 写的是库目录名（YAML 给人看）。库在 115 上改名后，这几条规则认不出库，整理到这一类就报错停下。
// 2026-10-20 起库有了自己的本地目录名（liblocal.go），规则认本地名，定过本地名的库不会再走到这里（staleName 返回空）；
// 留着给还没定下本地名的老库，等启动时都定下来以后可以删。
// 能认出是哪个库改的名（配置里存的旧显示路径的目录名 == 规则里写的值，缓存里是新名字，见 libRenameFixes），
// 就给一个按钮把这几处 library 换成新名。认不出的不猜，引导用户去二级分类自己改。
//
// 只信服务端算出来的修复（请求只带旧名），不接受前端传「改成什么」：改错一次就是一整类片进错库。
// 顺手把配置里这个库的显示路径刷成新路径，否则下次还会被认成「改过名」。

// renameFixYAML 规则里写的旧库名换成新名（纯函数）
func renameFixYAML(src, from, to string) (string, bool, error) {
	return rewriteCategoryLibrary(src, func(_, _, old string) (string, bool) {
		if strings.Trim(strings.TrimSpace(old), "/") == from {
			return to, true
		}
		return old, true
	})
}

// fullWithFreshPath full 里这个库存的显示路径（主库 cid_path / 附加库 extra_libs[].path）换成 115 现在的路径
func fullWithFreshPath(raw, cid, path string) (string, bool) {
	m := map[string]any{}
	if raw == "" || json.Unmarshal([]byte(raw), &m) != nil || m == nil || path == "" {
		return raw, false
	}
	changed := false
	if c, _ := m["cid"].(string); strings.TrimSpace(c) == cid {
		if old, _ := m["cid_path"].(string); old != path {
			m["cid_path"] = path
			changed = true
		}
	}
	if list, ok := m["extra_libs"].([]any); ok {
		for _, it := range list {
			e, ok := it.(map[string]any)
			if !ok {
				continue
			}
			if c, _ := e["cid"].(string); strings.TrimSpace(c) == cid {
				if old, _ := e["path"].(string); old != path {
					e["path"] = path
					changed = true
				}
			}
		}
	}
	if !changed {
		return raw, false
	}
	b, _ := json.Marshal(m)
	return string(b), true
}

// FixLibraryRename POST /libraries/fix-rename  body: {"from": "动画"}
func (h *Handler) FixLibraryRename(c *gin.Context) {
	var req struct {
		From string `json:"from"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || strings.TrimSpace(req.From) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "参数错误"})
		return
	}
	from := strings.Trim(strings.TrimSpace(req.From), "/")

	// 现场重算一遍：规则、各库名字都可能在页面打开之后变过
	view := h.librariesNow()
	var fix *libRenameFix
	for i := range view.RenameFixes {
		if view.RenameFixes[i].From == from {
			fix = &view.RenameFixes[i]
			break
		}
	}
	if fix == nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "认不出「" + from + "」是哪个库改的名，请到「自动整理 → 二级分类」里手动改"})
		return
	}
	var catRule model.ScrapeRule
	h.DB.Where("type = ?", "category_config").First(&catRule)
	out, changed, err := renameFixYAML(catRule.Config, fix.From, fix.To)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if !changed {
		c.JSON(http.StatusBadRequest, gin.H{"error": "分类规则里已经没有写「" + from + "」的了"})
		return
	}
	rows, err := parseCategoryYAML(out)
	if err != nil || len(rows) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "改写后的分类 YAML 解析失败，已放弃修改"})
		return
	}
	catRule.Config = out
	sid, err := ensureDefaultScheme(h.DB) // 老的单份分类就是方案「默认」（catscheme.go）
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "准备分类方案失败: " + err.Error()})
		return
	}
	tx := h.DB.Begin()
	if err := tx.Save(&catRule).Error; err != nil {
		tx.Rollback()
		c.JSON(http.StatusInternalServerError, gin.H{"error": "保存分类 YAML 失败: " + err.Error()})
		return
	}
	if err := writeSchemeTx(tx, sid, out, rows); err != nil {
		tx.Rollback()
		c.JSON(http.StatusInternalServerError, gin.H{"error": "重建分类规则失败: " + err.Error()})
		return
	}
	if err := tx.Commit().Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "保存失败: " + err.Error()})
		return
	}
	forgetClassifyRules()
	log.Printf("[媒体库] ✓ 分类规则里的库名「%s」已改成「%s」（库在 115 上改过名）", fix.From, fix.To)

	// 显示路径刷新只影响下次还会不会被认成「改过名」，失败不算这次修复失败
	for _, s := range legacyLibNameSources(h.getSettingValue("full")) {
		if s.Cid != fix.Lib || !s.CachedOK {
			continue
		}
		if v, ok := fullWithFreshPath(h.getSettingValue("full"), s.Cid, s.Cached.Path); ok {
			if err := h.Config.SaveSetting("full", v); err != nil {
				log.Printf("[媒体库] ○ 刷新库「%s」的显示路径失败（不影响整理）: %v", fix.To, err)
			}
		}
	}
	c.JSON(http.StatusOK, gin.H{"message": "已把分类规则里的「" + fix.From + "」改成「" + fix.To + "」", "data": h.librariesNow()})
}
