package api

import (
	"net/http"
	"strconv"
	"time"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// ==================== 增量同步：最近事件 ====================
//
// 「Strm 管理 → 增量同步」的最近事件列表（2026-10-23，参考 openStrm 的网盘监控页）：
// 115 上发生了什么、本地处理了没有。只读 SyncEvent 表（已应用的留 30 天，见 pruneSyncEvents），
// 目录位置只查路径缓存，**零 115 请求**。
//
// 事件表本身不记「处理成了什么」（写了几个 STRM），只记消费时的去向 Note：
// 整理自己产生的（self）默认隐藏 —— 整理一部剧就是几十条移动 / 改名，会把外部变更淹掉

// incrEventGroups 界面上的四个筛选，按事件类型分组
var incrEventGroups = map[string][]string{
	"add":  {evUpload, evUploadImage, evReceive, evCopy, evNewFolder, evCopyFolder},
	"del":  {evDelete},
	"move": {evMove, evMoveImage, evRename, evFolderRename},
}

type incrEventRow struct {
	ID      uint   `json:"id"`
	At      string `json:"at"`
	Type    string `json:"type"`
	Dir     bool   `json:"is_dir"`
	Name    string `json:"name"`
	Path    string `json:"path,omitempty"` // 所在目录（路径缓存里有才给）
	Size    int64  `json:"size"`
	Account uint   `json:"account"`
	Status  string `json:"status"` // pending / applied
	Note    string `json:"note"`   // self / outside / 空
}

// IncrEvents 最近的增量事件
// GET /sync/incr-events?group=all|add|del|move&hide_self=1&page=1&size=20
func (h *Handler) IncrEvents(c *gin.Context) {
	page, _ := strconv.Atoi(c.DefaultQuery("page", "1"))
	size, _ := strconv.Atoi(c.DefaultQuery("size", "20"))
	if page < 1 {
		page = 1
	}
	if size < 1 || size > 100 {
		size = 20
	}
	group := c.DefaultQuery("group", "all")
	hideSelf := c.Query("hide_self") == "1"

	base := func() *gorm.DB {
		q := h.DB.Model(&model.SyncEvent{})
		if hideSelf {
			q = q.Where("COALESCE(note, '') <> ?", "self")
		}
		return q
	}

	counts := gin.H{}
	var all int64
	base().Count(&all)
	counts["all"] = all
	for g, types := range incrEventGroups {
		var n int64
		base().Where("type IN ?", types).Count(&n)
		counts[g] = n
	}
	var hidden int64
	if hideSelf {
		h.DB.Model(&model.SyncEvent{}).Where("note = ?", "self").Count(&hidden)
	}

	q := base()
	if types, ok := incrEventGroups[group]; ok {
		q = q.Where("type IN ?", types)
	}
	var total int64
	q.Count(&total)
	var rows []model.SyncEvent
	q.Order("event_time DESC, id DESC").Offset((page - 1) * size).Limit(size).Find(&rows)

	out := make([]incrEventRow, 0, len(rows))
	for _, r := range rows {
		row := incrEventRow{
			ID: r.ID, Type: r.Type, Dir: r.FileCat == "0", Name: r.FileName, Size: r.Size,
			Account: r.Account, Status: r.Status, Note: r.Note,
		}
		if r.EventTime > 0 {
			row.At = time.Unix(r.EventTime, 0).Format(time.RFC3339)
		} else {
			row.At = r.CreatedAt.Format(time.RFC3339)
		}
		// 路径缓存只记主号（别的账号不写缓存，按路径查会串，见 AGENTS.md「多个 115 账号」）
		if r.Account == acctMain {
			if p, ok := lookupDirAbsAnyAge(r.Cid); ok {
				row.Path = p
			}
		}
		out = append(out, row)
	}
	c.JSON(http.StatusOK, gin.H{"data": out, "total": total, "counts": counts, "hidden": hidden, "page": page, "size": size})
}
