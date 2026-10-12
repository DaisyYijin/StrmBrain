package api

import (
	"encoding/json"
	"log"
	"strings"
)

// ==================== 转存/待整理：合成一个目录（2026-10-12） ====================
//
// 原来转存（share.folder）与待整理（org-basic.pending）是两个目录，各有一条整理路径：
// 转存触发 / 守望者扫转存，定时整理扫待整理（再顺带扫转存）。两条路扫的东西本质一样，
// 维护者决定只留一个设置项「转存/待整理」，存在 share.folder，loadOrgConfig 拿它当扫描根。
//
// 老配置两个都配了、又不是同一个时以转存为准（维护者定的）：还没下完的离线任务、
// 订阅的包装目录都已经指向转存目录，换成待整理它们就落空了。旧待整理目录**不动网盘**，
// 只把它记进 setting `staging_legacy`，工作目录卡上提示一次，里面剩下的东西由用户自己处理。

// stagingLegacyKey 合并时被弃用的旧待整理目录（{cid, path}；用户点「知道了」后清成 {}）
const stagingLegacyKey = "staging_legacy"

// stagingLegacy 被弃用的旧待整理目录
type stagingLegacy struct {
	Cid  string `json:"cid"`
	Path string `json:"path"`
}

// stripOrgPending 去掉 org-basic 里的 pending / pending_path。
// 老前端（浏览器缓存）整存 org-basic 时还会带着它们，留着不读也会让人误以为还生效。
// 解析不了的原样返回，交给后面的保存逻辑照旧处理
func stripOrgPending(raw string) (string, bool) {
	var m map[string]any
	if strings.TrimSpace(raw) == "" || json.Unmarshal([]byte(raw), &m) != nil || m == nil {
		return raw, false
	}
	_, a := m["pending"]
	_, b := m["pending_path"]
	if !a && !b {
		return raw, false
	}
	delete(m, "pending")
	delete(m, "pending_path")
	out, err := json.Marshal(m)
	if err != nil {
		return raw, false
	}
	return string(out), true
}

// stagingMerge 合并的结果（纯函数产出，MigrateStagingDir 负责落库）
type stagingMerge struct {
	Share  string         // 新的 share 原文；空 = 不改
	Org    string         // 新的 org-basic 原文；空 = 不改
	Legacy *stagingLegacy // 被弃用的旧待整理目录；nil = 没有
}

// mergeStagingSettings 按 share / org-basic 原文算出合并后的配置：
//   - 只配了待整理 → 搬进 share.folder（连同显示路径）；
//   - 两个都配了且不同 → 以转存为准，旧待整理记进 Legacy；
//   - 不管哪种，org-basic 里的 pending 字段都去掉。
//
// org-basic 里没有 pending 字段时什么都不做，所以每次启动都跑也是幂等的
func mergeStagingSettings(shareRaw, orgRaw string) stagingMerge {
	var res stagingMerge
	pending := jsonStrField(orgRaw, "pending")
	pendingPath := jsonStrField(orgRaw, "pending_path")
	org, stripped := stripOrgPending(orgRaw)
	if !stripped {
		return res
	}
	res.Org = org
	if pending == "" {
		return res
	}
	share := jsonStrField(shareRaw, "folder")
	switch {
	case share == "":
		m := map[string]any{}
		if strings.TrimSpace(shareRaw) != "" {
			_ = json.Unmarshal([]byte(shareRaw), &m)
			if m == nil {
				m = map[string]any{}
			}
		}
		m["folder"] = pending
		m["folder_path"] = pendingPath
		b, _ := json.Marshal(m)
		res.Share = string(b)
	case share != pending:
		res.Legacy = &stagingLegacy{Cid: pending, Path: pendingPath}
	}
	return res
}

// MigrateStagingDir 启动时把老配置的待整理目录合进转存（见文件头）。
// 必须排在任何后台任务之前：loadOrgConfig 已经只认 share.folder，
// 「只配了待整理」的老用户不先搬过去，第一轮整理就报未配置
func (h *Handler) MigrateStagingDir() {
	if h.Config == nil {
		return
	}
	r := mergeStagingSettings(h.getSettingValue("share"), h.getSettingValue("org-basic"))
	if r.Org == "" {
		return
	}
	// 先写 share 再改 org-basic：中途失败时 pending 还在，下次启动重来
	if r.Share != "" {
		if err := h.Config.SaveSetting("share", r.Share); err != nil {
			log.Printf("[迁移] ✗ 转存/待整理：写入转存目录失败，下次启动再试: %v", err)
			return
		}
		log.Printf("[迁移] ✓ 转存/待整理：原待整理目录改作「转存/待整理」目录")
	}
	if r.Legacy != nil {
		b, _ := json.Marshal(r.Legacy)
		if err := h.Config.SaveSetting(stagingLegacyKey, string(b)); err != nil {
			log.Printf("[迁移] ✗ 转存/待整理：记录旧待整理目录失败，下次启动再试: %v", err)
			return
		}
		where := r.Legacy.Path
		if where == "" {
			where = "cid=" + r.Legacy.Cid
		}
		log.Printf("[迁移] ⚠ 转存/待整理：转存与待整理原是两个目录，已统一用转存目录；"+
			"原待整理目录（%s）不再自动整理，里面剩下的内容请移到转存/待整理目录", where)
	}
	if err := h.Config.SaveSetting("org-basic", r.Org); err != nil {
		log.Printf("[迁移] ✗ 转存/待整理：清理整理配置失败，下次启动再试: %v", err)
	}
}
