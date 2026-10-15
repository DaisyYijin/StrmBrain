package api

import (
	"encoding/json"
	"strings"
)

// ==================== 每个库让本站做什么（2026-10-15，LIBRARY-ROLES-PLAN.md） ====================
//
// 库卡片只管「做不做」，功能页只管「怎么做」。阶段 1 只有一项：**谁来整理**。
//
//   - 本站整理（organize，默认）：分类规则 library / 转存入口 / 默认库把内容整理进来，目录结构是 库/分类/片目，
//     片目按二级分类认（libCategoryLayout 的分类表）。现有的库都是这种，所以没写过 lib_opts 的老部署零变化。
//   - 我自己整理（self）：本站**不往里放任何东西**（规则、转存入口都不能指向它，网盘文件页里不能整理 / 移动），
//     只同步 STRM、海报墙、刮削、探测。目录是用户自己起的，片目在第几层由用户**声明**（Depth），不推断：
//     平铺与多级分类混在一起时推断认不准（海报墙那条约束）。
//
// 存在单独的 setting key 里，**不塞进 full**：「账号与媒体库」整存整取 full，塞进去两边会互相冲掉
// （onLibsSaved 已经踩过一次）。按 cid 记：库在 115 上改名不影响它。
//
// 首个媒体库（full.cid）阶段 1 只能是本站整理：它还兼着「默认放这里」，拆开要等阶段 3（设计文档 §2.3）。

const libOptsKey = "lib_opts"

// libModeSelf 我自己整理
const libModeSelf = "self"

// libOpt 一个库的选项
type libOpt struct {
	Mode  string `json:"mode,omitempty"`  // 空 / organize = 本站整理；self = 我自己整理
	Depth int    `json:"depth,omitempty"` // 自己整理的库：片目在库下第几层（1 = 库/片目，2 = 库/分类/片目，3 = 再多一层分类）
}

// libOptDepthMax 片目层级最多三层：再深基本是选错了，界面也只给这三档
const libOptDepthMax = 3

// self 是不是自己整理的库
func (o libOpt) self() bool { return o.Mode == libModeSelf }

// depth 归一过的层级：没写或越界按 2（本站整理出来的就是 库/分类/片目，改成自己整理时最常见）
func (o libOpt) depth() int {
	if o.Depth < 1 || o.Depth > libOptDepthMax {
		return 2
	}
	return o.Depth
}

// libOpts setting lib_opts 的全部内容
type libOpts struct {
	Libs map[string]libOpt `json:"libs,omitempty"` // 库 cid → 选项；没有的库按默认（本站整理）
}

func parseLibOpts(raw string) libOpts {
	var o libOpts
	_ = json.Unmarshal([]byte(raw), &o)
	return o
}

func loadLibOpts() libOpts { return parseLibOpts(settingValueCompat(libOptsKey)) }

// of 某个库的选项（没写过的库返回零值 = 本站整理）
func (o libOpts) of(cid string) libOpt { return o.Libs[strings.TrimSpace(cid)] }

// selfCids 自己整理的库（cid 集合）
func (o libOpts) selfCids() map[string]bool {
	out := map[string]bool{}
	for cid, v := range o.Libs {
		if v.self() {
			out[cid] = true
		}
	}
	return out
}

// selfLibDepths 自己整理的库：库名 → 片目层级。名字只读路径缓存与配置里的显示路径（零 115 请求），
// 认不出名字的库这里没有它 —— 它的片目暂时按分类规则认，等名字进了缓存（打开一次网盘文件页 / 跑一次同步）就对了
func selfLibDepths(fullRaw string, opts libOpts) map[string]int {
	if len(opts.Libs) == 0 {
		return nil
	}
	var out map[string]int
	for _, s := range libNameSources(fullRaw) {
		o := opts.of(s.Cid)
		if !o.self() {
			continue
		}
		if name := s.libRootOf().Name; name != "" {
			if out == nil {
				out = map[string]int{}
			}
			out[name] = o.depth()
		}
	}
	return out
}

// marshalLibOpts 只留还在配置里、且不是默认值的库。一个都没有时写 {} 而不是空串：
// 空串在 getSettingValue 里会退回去读数据库的老值
func marshalLibOpts(o libOpts, libCids []string) string {
	keep := map[string]bool{}
	for _, c := range libCids {
		keep[c] = true
	}
	out := libOpts{Libs: map[string]libOpt{}}
	for cid, v := range o.Libs {
		if !keep[cid] || !v.self() {
			continue
		}
		out.Libs[cid] = libOpt{Mode: libModeSelf, Depth: v.depth()}
	}
	if len(out.Libs) == 0 {
		return "{}"
	}
	b, _ := json.Marshal(out)
	return string(b)
}
