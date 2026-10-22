package api

// 订阅洗版（2026-10-22，设计见 docs/115-station-notes/SUB-WASH-PLAN.md §3.3）。
//
// 开了洗版的订阅，库里已有、没达标（没命中洗版策略第 1 条规则）的集，出现比它好的资源就转进来，
// 交给整理按洗版策略替换旧版。补缺集期间就洗；补齐之后再洗 WashDays 天（默认 30），全部达标或到期停止检查。
//
// 几条约束（维护者定的 / 踩坑预防）：
//   - 「更好」和整理时洗版同一套（subPriority.compare = washCompare）。所以只有「跟随洗版策略」能洗，
//     「基础」要整理的策略也没写规则；自定义不能洗：订阅觉得更好、整理觉得打平，换进来的被移进「已存在」。
//     整理的策略还得是 replace，否则新版不会顶掉旧版。
//   - 库里那份有多好不能看库里的文件名：整理按模板改了名，模板没写画质的话名字里什么都看不出，
//     任何资源都「更好」，每轮换一次。按整理记录里这个文件的原名（orgRecordFile.Orig）认，没有就用台账名，
//     读过媒体信息的叠上读到的画质（probeJudgeName）；连分辨率都认不出的那一集不洗。
//   - 只用 115 分享（能按集挑、不扣离线配额），不花积分。
//   - 洗版试过的资源单独记（SubAttempt.Wash）：补缺时「里面没有缺的集」的合集，正是洗版要看的东西。

import (
	"encoding/json"
	"fmt"
	"log"
	"path"
	"strings"
	"time"

	"115-station/internal/model"
)

// subSearchHit 一轮里搜过的结果（补缺集与洗版共用，不搜两遍）
type subSearchHit struct {
	items []ResourceItem
	note  string
}

func (r *subRunner) searchOnce(sub *model.Subscription) ([]ResourceItem, string) {
	if h, ok := r.searched[sub.ID]; ok {
		return h.items, h.note
	}
	items, note := r.search(sub)
	if r.searched == nil {
		r.searched = map[uint]subSearchHit{}
	}
	r.searched[sub.ID] = subSearchHit{items, note}
	return items, note
}

// washReady 这个订阅现在能不能洗版；不能时说清为什么（写进上一轮结果）
func (r *subRunner) washReady(sub *model.Subscription) (subPriority, string) {
	mode, _, _ := subPrioMode(r.cfg, sub)
	if mode == subPrioCustom {
		return subPriority{}, "资源优先级是自定义，和整理的洗版策略判得不一样，不能洗版；改成「跟随洗版策略」"
	}
	st := r.subWashStrategy(sub)
	if st == nil {
		return subPriority{}, "没配洗版策略，整理不会替换旧版"
	}
	if st.Mode != "replace" {
		return subPriority{}, fmt.Sprintf("整理用的洗版策略模式是 %s，不是 replace（替换），换进来的新版不会顶掉旧版", firstNonEmpty(st.Mode, "空"))
	}
	if mode == subPrioBasic && len(st.PriorityLevel) > 0 {
		return subPriority{}, "资源优先级是基础、整理的洗版策略却写了规则，两边判得不一样；改成「跟随洗版策略」"
	}
	return r.priorityOf(sub), ""
}

// subWashState 库里各集现有版本的画质（认画质用的名字）
type subWashState struct {
	Want    map[epKey]string // 没达标、认得出画质的集 → 现有版本的名字（洗版要换的）
	Top     int              // 已达标的集数
	Unknown int              // 认不出现有画质的集数（不洗）
}

// subExistingNames 库里每一集现有版本「认画质用的名字」：一集几份（分组洗版 / 共存）取最好的那份
func subExistingNames(sub *model.Subscription, have subHave, prio subPriority) map[epKey]string {
	db := model.DB
	if db == nil {
		return nil
	}
	orig := subOrigNames(sub)
	out := map[epKey]string{}
	for _, key := range have.TitleKeys {
		for _, sf := range subTitleVideos(db, key) {
			name := orig[sf.FileID]
			if name == "" {
				name = strings.TrimSuffix(path.Base(sf.RelPath), ".strm")
			}
			if info, ok := cachedProbe(probeKey(remoteFile{PickCode: sf.PickCode, Sha1: sf.Sha1})); ok && info != nil {
				name = probeJudgeName(name, &mediaOverlay{Res: probeResource(info)})
			}
			keys := []epKey{{}}
			if sub.MediaType == "tv" {
				keys = ledgerVideoEpisodes(key, sf.RelPath)
			}
			for _, k := range keys {
				if cur, ok := out[k]; !ok || prio.compare(name, cur) > 0 {
					out[k] = name
				}
			}
		}
	}
	return out
}

// subOrigNames 整理记录里这部片各文件的原名（fid → 原名）。后整理的盖前面的：同一个 fid 重新整理过，以最近一次为准
func subOrigNames(sub *model.Subscription) map[string]string {
	out := map[string]string{}
	var recs []model.OrganizeRecord
	model.DB.Select("id", "files").Where("tmdb_id = ? AND media_type = ?", sub.TmdbID, sub.MediaType).Order("id ASC").Find(&recs)
	for _, rec := range recs {
		var files []orgRecordFile
		if json.Unmarshal([]byte(rec.Files), &files) != nil {
			continue
		}
		for _, f := range files {
			if f.Kind == "video" && f.Fid != "" {
				out[f.Fid] = firstNonEmpty(f.Orig, f.Name)
			}
		}
	}
	return out
}

// planSubWash 哪些集要洗（纯函数）：在范围里、不在路上、认得出画质、没达标的
func planSubWash(names map[epKey]string, inScope func(epKey) bool, inflight map[epKey]bool, prio subPriority) subWashState {
	st := subWashState{Want: map[epKey]string{}}
	for k, name := range names {
		if !inScope(k) || inflight[k] {
			continue
		}
		switch {
		case prio.top(name):
			st.Top++
		case !washQualityKnown(name, prio):
			st.Unknown++
		default:
			st.Want[k] = name
		}
	}
	return st
}

// washQualityKnown 现有版本的画质认不认得出：命中了某条规则，或者至少看得出分辨率
func washQualityKnown(name string, prio subPriority) bool {
	return washTier(name, prio.Rules) >= 0 || ParseResourceInfo(name).Pix != ""
}

// subWashInflight 洗版提交了、还没结算的集
func subWashInflight(subID uint) map[epKey]bool {
	out := map[epKey]bool{}
	var rows []model.SubAttempt
	model.DB.Where("sub_id = ? AND status = ? AND wash = ?", subID, subAttemptInflight, true).Find(&rows)
	for _, a := range rows {
		for _, k := range unmarshalEpKeys(a.Episodes) {
			out[k] = true
		}
		if a.Episodes == "" {
			out[epKey{}] = true // 电影
		}
	}
	return out
}

// subWashOutcome 这一轮洗版的结果，run 据此排期与写上一轮结果
type subWashOutcome struct {
	Active    bool   // 还在洗（有没达标的集、或有洗版在路上），订阅不能结束
	Submitted bool   // 这一轮提交了洗版
	Note      string // 上一轮结果里的一句
}

// washRound 洗版一轮。ev 是补缺盘点；done = 范围内已经齐了（补齐之后才有 30 天期限）
func (r *subRunner) washRound(sub *model.Subscription, ev subEval, item *subRunItem) subWashOutcome {
	now := r.now()
	var out subWashOutcome
	if !sub.Wash || model.DB == nil {
		return out
	}
	if ev.Done && sub.WashUntil != nil && !now.Before(*sub.WashUntil) {
		return out // 洗版期限到了
	}
	prio, why := r.washReady(sub)
	if why != "" {
		out.Note = "洗版没开始：" + why
		return out
	}
	have := subHaveOf(model.DB, scanLedgerTitlesCached(), sub.TmdbID, sub.MediaType)
	inflight := subWashInflight(sub.ID)
	inScope := func(k epKey) bool { return sub.MediaType != "tv" || subEpInScope(sub, k) }
	st := planSubWash(subExistingNames(sub, have, prio), inScope, inflight, prio)
	if len(inflight) > 0 {
		out.Active = true
	}
	if len(st.Want) == 0 {
		switch {
		case len(inflight) > 0:
			out.Note = fmt.Sprintf("洗版：%d 集在路上", len(inflight))
		case st.Unknown > 0:
			out.Note = fmt.Sprintf("洗版：%d 集认不出现有画质，不洗", st.Unknown)
		}
		return out
	}
	out.Active = true
	out.Submitted = r.searchAndWash(sub, st, prio, item)
	out.Note = fmt.Sprintf("洗版：%d 集没达标", len(st.Want))
	if st.Unknown > 0 {
		out.Note += fmt.Sprintf("，%d 集认不出画质", st.Unknown)
	}
	if out.Submitted {
		out.Note += "，提交了更好的版本"
	}
	return out
}

// searchAndWash 在搜到的资源里找比库里更好的 115 分享，逐条试
func (r *subRunner) searchAndWash(sub *model.Subscription, st subWashState, prio subPriority, item *subRunItem) bool {
	db := model.DB
	items, _ := r.searchOnce(sub)
	if len(items) == 0 {
		return false
	}
	tried := map[string]subTried{}
	var rows []model.SubAttempt
	db.Where("sub_id = ? AND wash = ?", sub.ID, true).Order("id ASC").Find(&rows)
	for _, a := range rows {
		tried[a.Hash] = subTried{Status: a.Status, RetryAt: a.RetryAt}
	}
	want := make([]epKey, 0, len(st.Want))
	missing := map[epKey]bool{}
	for k := range st.Want {
		want = append(want, k)
		missing[k] = true
	}
	sortEpKeys(want)
	pctx := subPickCtx{
		Sub: sub, Missing: want, Tried: tried, Now: r.now(),
		Exclude: append(splitKeywords(r.cfg.ExcludeDefault), splitKeywords(sub.Exclude)...),
		Include: splitKeywords(sub.Include),
		Cond:    subCondOf(r.cfg, sub.Cond),
		// 只用分享、不花积分
		OfflineMode: subOfflineShareOnly, Re0Left: 0, DianLeft: 0,
		Skipped: map[string]int{}, Prio: prio,
	}
	cands, _ := planSubCandidates(items, pctx)
	// 标题写明了画质、却一集都比不过库里的，不用去列
	kept := cands[:0]
	for _, c := range cands {
		if c.Paid > 0 || c.Item.offline() {
			continue
		}
		if subWashTitleHopeless(c.Item.Title, st.Want, prio) {
			continue
		}
		kept = append(kept, c)
	}
	if len(kept) == 0 {
		return false
	}

	r.washSub = sub.ID
	r.washBeats = func(k epKey, name string) bool {
		cur, ok := st.Want[k]
		return ok && prio.compare(name, cur) > 0
	}
	defer func() { r.washSub, r.washBeats = 0, nil }()

	kept, ok := r.previewShares(sub, kept, missing)
	if !ok {
		return false
	}
	submitted, tries, got := false, 0, 0
	for i, c := range kept {
		if len(missing) == 0 || subLane.stopRequested() || got >= r.cfg.MaxEpsPerSub || tries >= r.cfg.MaxResPerSub {
			break
		}
		if !c.coversAnyMissing(missing, sub.MediaType == "movie") {
			continue
		}
		if tries > 0 && !r.cooldown() {
			break
		}
		tries++
		subLane.setSub("洗版：尝试资源", i+1, len(kept), truncateStr(c.Item.Title, 40))
		att, covered := r.tryOne(sub, c, missing, r.cfg.MaxEpsPerSub-got)
		if att.Status != subAttemptInflight {
			continue
		}
		submitted = true
		got += max(len(covered), 1)
		for _, k := range covered {
			delete(missing, k)
		}
		item.Submitted = append(item.Submitted, fmt.Sprintf("洗版 · %s · %s：%s", botSourceLabel(c.Item.Source), truncateStr(c.Item.Title, 50), subEpisodesText(sub, covered)))
		log.Printf("[订阅] ♻ 《%s》洗版：从「%s」转存更好的版本（%s）", sub.Title, truncateStr(c.Item.Title, 50), subEpisodesText(sub, covered))
	}
	return submitted
}

// subWashTitleHopeless 资源标题明确写了画质，而且对要洗的每一集都不比库里的好。
// 标题没写分辨率的不下结论（「剧名 全集」得列开看文件）
func subWashTitleHopeless(title string, want map[epKey]string, prio subPriority) bool {
	if ParseResourceInfo(title).Pix == "" && washTier(title, prio.Rules) < 0 {
		return false
	}
	for _, cur := range want {
		if prio.compare(title, cur) > 0 {
			return false
		}
	}
	return true
}

// washUntilFor 补齐那一轮定下洗版截止时间
func washUntilFor(now time.Time, days int) time.Time {
	return now.Add(time.Duration(days) * 24 * time.Hour)
}
