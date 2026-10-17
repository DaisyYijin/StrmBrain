package api

import (
	"encoding/json"
	"math/big"
	"strings"
)

// size115 是 index_info 里 space_info 的 size 字段。
// 115 并不总回整数：大容量账号常见带小数（155555555555555.25）、科学计数法（1.5e6），
// 有的还是字符串（"123.75"）。原来按 int64 解，一碰到这几种整段 Unmarshal 失败，
// 仪表盘读到 0 就显示「Cookie 已失效」，其实 Cookie 好好的（2026-10-17 用户反馈）。
// 认法参考 115driver pkg/driver/info.go 的 TotalSize：读成大浮点再向零取整。
type size115 int64

func (s *size115) UnmarshalJSON(b []byte) error {
	v := strings.TrimSpace(string(b))
	if v == "null" {
		*s = 0
		return nil
	}
	if strings.HasPrefix(v, `"`) {
		if err := json.Unmarshal(b, &v); err != nil {
			return err
		}
		v = strings.TrimSpace(v)
	}
	if v == "" {
		*s = 0
		return nil
	}
	f, _, err := big.ParseFloat(v, 10, 256, big.ToZero)
	if err != nil || f.IsInf() || f.Sign() < 0 {
		// 认不出（"Inf" 之类）按 0 算，别让一个字段拖垮整段解析
		*s = 0
		return nil
	}
	n, _ := f.Int(nil)
	if n == nil || !n.IsInt64() {
		*s = 0
		return nil
	}
	*s = size115(n.Int64())
	return nil
}

// spaceInfo115 解析 files/index_info 返回里的容量，ok=false 表示没读到（state=false 或总容量为 0）
func spaceInfo115(body []byte) (used, total int64, ok bool) {
	var info struct {
		State bool `json:"state"`
		Data  struct {
			SpaceInfo struct {
				AllTotal struct {
					Size size115 `json:"size"`
				} `json:"all_total"`
				AllUse struct {
					Size size115 `json:"size"`
				} `json:"all_use"`
			} `json:"space_info"`
		} `json:"data"`
	}
	if json.Unmarshal(body, &info) != nil || !info.State {
		return 0, 0, false
	}
	used, total = int64(info.Data.SpaceInfo.AllUse.Size), int64(info.Data.SpaceInfo.AllTotal.Size)
	return used, total, total > 0
}
