package api

// 任务中心「分享记录」页签的接口（转存后分享到 RE0 / 癫影，见 shareout.go）。
// 只读 ShareOut 表和本地的整理记录 / 来源链接，不请求两个站；站上那条分享的地址按本地记账拼出来。

import (
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
)

// shareOutRecordDTO 一条分享记录
type shareOutRecordDTO struct {
	model.ShareOut
	Title     string `json:"title"`
	Year      string `json:"year"`
	Poster    string `json:"poster_path"`
	ShareName string `json:"share_name"` // 来源链接上记的分享标题
	LinkURL   string `json:"link_url"`
	Source    string `json:"source"` // 经谁提交（web / 机器人）
	// SiteURL 站上那条分享（发成的才有）：RE0 有 slug 时直达分享，否则打开这部片在站上的页面（分享列在里面）
	SiteURL string `json:"site_url,omitempty"`
	// NextRetry 失败了、还会自动重试的时间；次数用完的为空
	NextRetry *time.Time `json:"next_retry,omitempty"`
	CanRetry  bool       `json:"can_retry"`
}

var re0SlugLike = regexp.MustCompile(`^[A-Za-z0-9_-]{16,}$`)

// shareOutSiteURL 站上那条分享的地址（纯函数）
func shareOutSiteURL(o model.ShareOut, re0Base string) string {
	if o.Status != shareOutStatusDone {
		return ""
	}
	switch o.Site {
	case shareOutSiteRe0:
		if re0SlugLike.MatchString(o.Ref) {
			return re0Base + "/resource/115/" + o.Ref
		}
		if o.TmdbID > 0 && o.MediaType != "" {
			return fmt.Sprintf("%s/tmdb/%s/%d", re0Base, o.MediaType, o.TmdbID)
		}
	case shareOutSiteDian:
		// 站点的资源页是 /r/{key}，key 与列资源用的同一套编码（前端 An(mediaType, id)）
		if o.TmdbID > 0 && o.MediaType != "" {
			return dianBase + "/r/" + dianResourceKey(o.MediaType, o.TmdbID)
		}
	}
	return ""
}

// ShareOutRecords GET /shareout/records?status=done|skipped|failed&page=&size=
func (h *Handler) ShareOutRecords(c *gin.Context) {
	status := c.Query("status")
	page, _ := strconv.Atoi(c.DefaultQuery("page", "1"))
	size, _ := strconv.Atoi(c.DefaultQuery("size", "20"))
	if page < 1 {
		page = 1
	}
	if size < 1 || size > 100 {
		size = 20
	}

	counts := map[string]int64{}
	var byStatus []struct {
		Status string
		N      int64
	}
	h.DB.Model(&model.ShareOut{}).Select("status, count(*) as n").Group("status").Scan(&byStatus)
	for _, s := range byStatus {
		counts[s.Status] = s.N
		counts["all"] += s.N
	}

	q := h.DB.Model(&model.ShareOut{})
	if status == shareOutStatusDone || status == shareOutStatusSkipped || status == shareOutStatusFailed {
		q = q.Where("status = ?", status)
	}
	var total int64
	q.Count(&total)
	var rows []model.ShareOut
	q.Order("updated_at DESC, id DESC").Offset((page - 1) * size).Limit(size).Find(&rows)

	c.JSON(http.StatusOK, gin.H{"data": h.shareOutDTOs(rows, time.Now()), "total": total, "counts": counts, "page": page, "size": size})
}

// shareOutDTOs 给分享记录带上片名（整理记录上的）、来源链接与站上地址
func (h *Handler) shareOutDTOs(rows []model.ShareOut, now time.Time) []shareOutRecordDTO {
	linkIDs := make([]uint, 0, len(rows))
	for _, r := range rows {
		linkIDs = append(linkIDs, r.LinkID)
	}
	links := map[uint]model.DownloadLink{}
	type titleKey struct {
		link uint
		tmdb int
	}
	titles := map[titleKey]model.OrganizeRecord{}
	if len(linkIDs) > 0 {
		var ls []model.DownloadLink
		h.DB.Where("id IN ?", linkIDs).Find(&ls)
		for _, l := range ls {
			links[l.ID] = l
		}
		var recs []model.OrganizeRecord
		h.DB.Select("link_id, tmdb_id, title, year, poster_path").Where("link_id IN ? AND tmdb_id > 0", linkIDs).Find(&recs)
		for _, r := range recs {
			k := titleKey{r.LinkID, r.TmdbID}
			if _, ok := titles[k]; !ok {
				titles[k] = r
			}
		}
	}
	re0Base := re0NormalizeBase(loadRe0Cfg().BaseURL)
	out := make([]shareOutRecordDTO, 0, len(rows))
	for _, r := range rows {
		l := links[r.LinkID]
		d := shareOutRecordDTO{ShareOut: r, ShareName: l.Name, LinkURL: l.URL, Source: l.Source,
			SiteURL: shareOutSiteURL(r, re0Base), CanRetry: r.Status != shareOutStatusDone}
		if t, ok := titles[titleKey{r.LinkID, r.TmdbID}]; ok {
			d.Title, d.Year, d.Poster = t.Title, t.Year, t.PosterPath
		}
		if d.Title == "" {
			d.Title = l.Name // 没认出片目（跳过的多半是这种）：用分享标题
		}
		if r.Status == shareOutStatusFailed && r.Attempts < shareOutMaxAttempts {
			at := r.RetryAt
			if at.Before(now) {
				at = now
			}
			d.NextRetry = &at
		}
		out = append(out, d)
	}
	return out
}

var errShareOutBusy = errors.New("正在处理别的分享，稍后再试")

// ShareOutRetry POST /shareout/records/:id/retry：失败 / 跳过的手动重试一次，后台立刻发
func (h *Handler) ShareOutRetry(c *gin.Context) {
	var row model.ShareOut
	if h.DB.First(&row, c.Param("id")).Error != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "记录不存在"})
		return
	}
	// 跳过的也能重试：站点拒收的原因可能已经不在了（本站修了提交方式、用户在站上删了旧分享），
	// 原因还在的（从两站拿到的、站上已有）重试也只是再记一次跳过
	if row.Status == shareOutStatusDone {
		c.JSON(http.StatusConflict, gin.H{"error": "已经分享成功了"})
		return
	}
	if err := h.shareOutRetryNow(row); err != nil {
		code := http.StatusConflict
		if !errors.Is(err, errShareOutBusy) {
			code = http.StatusBadRequest
		}
		c.JSON(code, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusAccepted, gin.H{"message": fmt.Sprintf("已开始重新分享到%s，结果稍后刷新查看", shareOutSiteLabel(row.Site))})
}

// shareOutRetryNow 手动重试：判定照常（片目、合集），然后在后台对这一个站发一次。
// 不走定时那一轮：定时只看 8 天内提交的链接，老链接手动重试会一直等不到。
// 次数记成用完，再失败就停在失败、不再自动重试（手动点一次就是一次）
func (h *Handler) shareOutRetryNow(row model.ShareOut) error {
	var link model.DownloadLink
	if h.DB.First(&link, row.LinkID).Error != nil {
		return fmt.Errorf("来源链接已经被清理，没法重试")
	}
	var recs []model.OrganizeRecord
	h.DB.Where("link_id = ?", link.ID).Find(&recs)
	now := time.Now()
	v := shareOutJudge(link, recs, now)
	if v.Wait {
		return fmt.Errorf("这条链接的整理还没稳定（在等确认或刚整理完），稍后再试")
	}
	if !shareOutRunning.CompareAndSwap(false, true) {
		return errShareOutBusy
	}
	row.Attempts = shareOutMaxAttempts - 1
	go func() {
		defer shareOutRunning.Store(false)
		defer func() { _ = recover() }()
		h.shareOutLink(link, v, []string{row.Site}, map[string]model.ShareOut{row.Site: row}, now)
	}()
	return nil
}
