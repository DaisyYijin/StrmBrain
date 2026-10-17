package api

import "testing"

// index_info 的 size 可能是整数、小数、科学计数法或字符串，都要读得出来
func TestSpaceInfo115(t *testing.T) {
	cases := []struct {
		name        string
		body        string
		used, total int64
		ok          bool
	}{
		{"整数", `{"state":true,"data":{"space_info":{"all_total":{"size":16492674416640},"all_use":{"size":42}}}}`, 42, 16492674416640, true},
		{"小数", `{"state":true,"data":{"space_info":{"all_total":{"size":155555555555555.25,"size_format":"141.49TB"},"all_use":{"size":1234.9}}}}`, 1234, 155555555555555, true},
		{"科学计数法", `{"state":true,"data":{"space_info":{"all_total":{"size":1.5e6},"all_use":{"size":"2.5e3"}}}}`, 2500, 1500000, true},
		{"字符串", `{"state":true,"data":{"space_info":{"all_total":{"size":"123.75"},"all_use":{"size":"7"}}}}`, 7, 123, true},
		{"Inf 不拖垮别的字段", `{"state":true,"data":{"space_info":{"all_total":{"size":100},"all_use":{"size":"Inf"}}}}`, 0, 100, true},
		{"state=false", `{"state":false,"error":"x"}`, 0, 0, false},
		{"没有容量", `{"state":true,"data":{}}`, 0, 0, false},
	}
	for _, c := range cases {
		u, tot, ok := spaceInfo115([]byte(c.body))
		if u != c.used || tot != c.total || ok != c.ok {
			t.Errorf("%s: 得到 used=%d total=%d ok=%v，期望 %d %d %v", c.name, u, tot, ok, c.used, c.total, c.ok)
		}
	}
}
