package api

import (
	"bytes"
	"errors"
	"fmt"
	"strings"

	"115-station/internal/model"

	"gopkg.in/yaml.v3"
)

// ==================== 后端改分类 YAML 的 library（2026-10-15） ====================
//
// 分类归属只在二级分类里改（MULTI-LIB-SIMPLIFY-PLAN.md §0.5），后端只在两个收尾动作里碰 YAML：
//   - 移除一个媒体库：指向它的规则删掉 library（回默认库），不删的话整理到这一类会报错停下；
//   - 库在 115 上改了名：library 里的旧名换成新名。
//
// YAML 是分类的唯一事实来源（SyncCategoryRulesFromYAML 启动时按它重建规则表），所以必须改文本本身，
// 只改 CategoryRule 表的话下次启动就被覆盖回去。
//
// 用 yaml.v3 的节点树只动 library 这一个键，注释与其他键的引号风格都留着；
// 空行会丢、缩进按原文第一处缩进统一（yaml.v3 只支持一种缩进）。
// 改完拿 parseCategoryYAML 前后对比，除 Library 以外有任何一处不同就拒绝 —— 宁可让用户手改，
// 也不能悄悄把条件改坏（整理会把片子放错库、错分类）。

// errCategoryYAMLExotic YAML 用了锚点 / 别名，节点树改写后展开不回原样，不碰
var errCategoryYAMLExotic = errors.New("分类 YAML 用了锚点或别名（& / *），没法自动修改，请在「自动整理 → 二级分类」里手动改")

// categoryLibFn 决定一条规则的 library 怎么改：返回 (新值, true) 写回新值，("", false) 删掉这个键。
// 只对写了 library 的规则调用；返回的新值等于 old 就是不改
type categoryLibFn func(mediaType, name, old string) (string, bool)

// rewriteCategoryLibrary 按 fn 改写每条规则的 library，返回新文本与是否有改动。
// 没有改动时原样返回 src（不重新编码，免得只是打开看看就把用户的空行、缩进改掉）
func rewriteCategoryLibrary(src string, fn categoryLibFn) (string, bool, error) {
	var root yaml.Node
	if err := yaml.Unmarshal([]byte(src), &root); err != nil {
		return "", false, fmt.Errorf("分类 YAML 解析失败: %w", err)
	}
	if len(root.Content) == 0 {
		return src, false, nil
	}
	if yamlHasAnchors(&root) {
		return "", false, errCategoryYAMLExotic
	}
	top := root.Content[0]
	if top.Kind != yaml.MappingNode {
		return src, false, nil
	}
	changed := false
	for i := 0; i+1 < len(top.Content); i += 2 {
		mediaType := top.Content[i].Value
		section := top.Content[i+1]
		if (mediaType != "movie" && mediaType != "tv") || section.Kind != yaml.MappingNode {
			continue
		}
		for j := 0; j+1 < len(section.Content); j += 2 {
			name := libSubPath(section.Content[j].Value)
			fields := section.Content[j+1]
			if name == "" || fields.Kind != yaml.MappingNode {
				continue // 没写条件的规则不可能有 library
			}
			for k := 0; k+1 < len(fields.Content); k += 2 {
				if fields.Content[k].Value != "library" {
					continue
				}
				valNode := fields.Content[k+1]
				old, ok := categoryYAMLValue(valNode)
				old = strings.TrimSpace(old)
				if !ok || old == "" {
					break
				}
				next, keep := fn(mediaType, name, old)
				switch {
				case !keep:
					fields.Content = append(fields.Content[:k], fields.Content[k+2:]...)
					if len(fields.Content) == 0 {
						// 只剩 library 的规则（兜底）删完变回「名字:」，和用户手写的兜底一个样
						*fields = yaml.Node{Kind: yaml.ScalarNode, Tag: "!!null"}
					}
					changed = true
				case strings.TrimSpace(next) != old:
					valNode.Value = strings.TrimSpace(next)
					valNode.Tag = "!!str"
					if valNode.Style&yaml.TaggedStyle != 0 {
						valNode.Style = 0
					}
					changed = true
				}
				break
			}
		}
	}
	if !changed {
		return src, false, nil
	}
	var buf bytes.Buffer
	enc := yaml.NewEncoder(&buf)
	enc.SetIndent(yamlIndentOf(src))
	if err := enc.Encode(&root); err != nil {
		return "", false, fmt.Errorf("分类 YAML 编码失败: %w", err)
	}
	_ = enc.Close()
	out := buf.String()
	if err := sameCategoryRulesButLibrary(src, out); err != nil {
		return "", false, err
	}
	return out, true, nil
}

// yamlHasAnchors 节点树里有没有锚点 / 别名
func yamlHasAnchors(n *yaml.Node) bool {
	if n.Anchor != "" || n.Kind == yaml.AliasNode {
		return true
	}
	for _, c := range n.Content {
		if yamlHasAnchors(c) {
			return true
		}
	}
	return false
}

// yamlIndentOf 原文第一处缩进的空格数（2–8），认不出按 2
func yamlIndentOf(src string) int {
	for _, line := range strings.Split(src, "\n") {
		trimmed := strings.TrimLeft(line, " ")
		if trimmed == "" || strings.HasPrefix(trimmed, "#") || len(trimmed) == len(line) {
			continue
		}
		if n := len(line) - len(trimmed); n >= 2 && n <= 8 {
			return n
		}
		break
	}
	return 2
}

// sameCategoryRulesButLibrary 改写前后解析出的规则除 Library 外逐条相等，否则报错
func sameCategoryRulesButLibrary(before, after string) error {
	a, err := parseCategoryYAML(before)
	if err != nil {
		return fmt.Errorf("分类 YAML 解析失败: %w", err)
	}
	b, err := parseCategoryYAML(after)
	if err != nil || len(a) != len(b) {
		return errors.New("自动修改分类 YAML 后规则对不上，已放弃修改，请在「自动整理 → 二级分类」里手动改")
	}
	norm := func(r model.CategoryRule) model.CategoryRule {
		r.Library = ""
		r.ID = 0
		return r
	}
	for i := range a {
		if norm(a[i]) != norm(b[i]) {
			return fmt.Errorf("自动修改分类 YAML 后规则「%s」的条件变了，已放弃修改，请在「自动整理 → 二级分类」里手动改", a[i].Name)
		}
	}
	return nil
}

// ---- 规则 → 库 ----

// ruleLibRef 一条分类规则（界面显示与改库确认用）
type ruleLibRef struct {
	MediaType string `json:"media_type"`
	Name      string `json:"name"`
	// Library YAML 里写的原值（认不出的库才带，界面原样显示「认不出『X』」）
	Library string `json:"library,omitempty"`
}

// ruleLibOfRow 一条规则落进哪个库（cid）。与整理选库同口径（pickRuleLib）：空 = 主库。
// 认不出返回 ok=false。libs 第一个是主库；名字为空的库（缓存里没有）按名字认不出
func ruleLibOfRow(r model.CategoryRule, libs []libRoot) (string, bool) {
	cid, err := pickRuleLib(r.Library, libs)
	return cid, err == nil
}

// rulesByLib 把规则按落进的库分组，认不出库的另列。一个分类名（同类型）只算一次：
// YAML 里同一段下键不能重复，重复只会出现在两种类型同名时，那是两条规则
func rulesByLib(rows []model.CategoryRule, libs []libRoot) (map[string][]ruleLibRef, []ruleLibRef) {
	by := map[string][]ruleLibRef{}
	var orphans []ruleLibRef
	for _, r := range rows {
		if r.MediaType != "movie" && r.MediaType != "tv" {
			continue
		}
		ref := ruleLibRef{MediaType: r.MediaType, Name: libSubPath(r.Name)}
		cid, ok := ruleLibOfRow(r, libs)
		if !ok {
			ref.Library = strings.TrimSpace(r.Library)
			orphans = append(orphans, ref)
			continue
		}
		by[cid] = append(by[cid], ref)
	}
	return by, orphans
}

// libValueMatches YAML 里的库值是不是指这个库（按 cid 或目录名，同 pickRuleLib）
func libValueMatches(val string, lib libRoot) bool {
	val = strings.TrimSpace(val)
	if val == "" {
		return false
	}
	return val == lib.Cid || (lib.Name != "" && strings.Trim(val, "/") == lib.Name)
}

// ---- 保存二级分类时的改库确认（§3.4） ----

// categoryLibConfirm 保存二级分类时，改了库且这一类已有片目就先返回 409 让用户确认。
// 2026-10-15 阶段 2 起打开（前端 YamlRuleTab 认这个 409、弹确认后带 confirm_lib_change 重发）。
// 留着开关是给测试与回退用的：前端要是又不认了，关掉它比让用户永远存不了强
var categoryLibConfirm = true

// categoryLibChange 一条规则换了库
type categoryLibChange struct {
	MediaType string `json:"media_type"`
	Name      string `json:"name"`
	From      string `json:"from"` // 库名（认不出时是 YAML 原值）
	To        string `json:"to"`
	Titles    int    `json:"titles"` // 原来那个库的这个分类目录下已有的片目数：它们留在原库
}

// libLabel 库值 → 界面上的库名
func libLabel(val string, libs []libRoot) string {
	cid, err := pickRuleLib(val, libs)
	if err != nil {
		return strings.TrimSpace(val)
	}
	for _, l := range libs {
		if l.Cid == cid {
			if l.Name != "" {
				return l.Name
			}
			return cid
		}
	}
	return cid
}

// categoryLibChanges 新旧规则按「类型 + 分类名」对齐，找出换了库、且原来的库里这个分类下已有片目的。
// 分类名改了的不算（等于新规则，原来那类的片也不会被新名字的规则管到）。
// titles(类型, 分类目录, 原库 cid) 返回已有片目数，零 115 请求（台账）
func categoryLibChanges(oldRows, newRows []model.CategoryRule, libs []libRoot,
	titles func(mediaType, category, libCid string) int) []categoryLibChange {
	type key struct{ mt, name string }
	before := map[key]string{}
	for _, r := range oldRows {
		before[key{r.MediaType, libSubPath(r.Name)}] = r.Library
	}
	var out []categoryLibChange
	for _, r := range newRows {
		k := key{r.MediaType, libSubPath(r.Name)}
		prev, ok := before[k]
		if !ok {
			continue
		}
		fromCid, errA := pickRuleLib(prev, libs)
		toCid, errB := pickRuleLib(r.Library, libs)
		if errA == nil && errB == nil && fromCid == toCid {
			continue
		}
		if errA != nil && strings.TrimSpace(prev) == strings.TrimSpace(r.Library) {
			continue // 两边写的是同一个认不出的值：没改
		}
		n := 0
		if errA == nil {
			n = titles(r.MediaType, k.name, fromCid)
		}
		if n == 0 {
			continue // 原来的库里没有这类的片：改了也不会分家，不打扰
		}
		out = append(out, categoryLibChange{MediaType: r.MediaType, Name: k.name,
			From: libLabel(prev, libs), To: libLabel(r.Library, libs), Titles: n})
	}
	return out
}

// ledgerCategoryTitles 台账里某个库的某个分类目录下有几个片目（老台账不带库名的两段式路径算在主库）
func ledgerCategoryTitles(entries map[string]*ledgerTitleEntry, libs []libRoot) func(mediaType, category, libCid string) int {
	nameOf := map[string]string{}
	for _, l := range libs {
		nameOf[l.Cid] = l.Name
	}
	primary := ""
	if len(libs) > 0 {
		primary = libs[0].Cid
	}
	return func(mediaType, category, libCid string) int {
		n := 0
		for _, e := range entries {
			if e.Category != category || (e.MediaType != "" && e.MediaType != mediaType) {
				continue
			}
			if e.LibName == nameOf[libCid] || (e.LibName == "" && libCid == primary) {
				n++
			}
		}
		return n
	}
}

// categoryLibChangesNow 拿当前规则表比要保存的规则。零 115 请求：库名读缓存与配置，认不全就不比（不打扰）
func (h *Handler) categoryLibChangesNow(newRows []model.CategoryRule) []categoryLibChange {
	sources := libNameSources(h.getSettingValue("full"))
	if len(sources) < 2 {
		return nil // 只有一个库：谈不上换库
	}
	libs := make([]libRoot, len(sources))
	for i, s := range sources {
		if libs[i] = s.libRootOf(); libs[i].Name == "" {
			return nil
		}
	}
	var oldRows []model.CategoryRule
	h.DB.Where("media_type IN ?", []string{"movie", "tv"}).Find(&oldRows)
	return categoryLibChanges(oldRows, newRows, libs, ledgerCategoryTitles(scanLedgerTitlesCached(), libs))
}
