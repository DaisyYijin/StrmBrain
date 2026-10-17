package api

// 订阅一轮里转存进来的包装目录先压着不整理，等这一轮提交完了再一次整理。
//
// 订阅一轮可能要试几十条资源、跑好几分钟（RE0 一条解锁就十几秒），每条分享 / 离线各进一个
// 包装目录。原来守望者每分钟一看、离线秒传 10 秒一试，各自入队「转存触发」整理，一轮下来
// 同一部剧被拆成四五次整理：入库通知四五条、刮削 / Emby 刷新 / 提前探测各跑四五遍
// （2026-10-17 现场：凡人修仙传缺 20 集，一轮推了 4 条入库通知）。
//
// 只在内存里记：重启就没了，等于放行，守望者照常接管，不会有东西永远卡在转存目录。
// 离线下载这一轮里没下完的不等它：任务结束就放行，下完了由离线监视器照常触发整理。

import "sync"

var subWrapHold = struct {
	sync.Mutex
	cids map[string]bool
}{cids: map[string]bool{}}

func holdSubWrapper(cid string) {
	if cid == "" {
		return
	}
	subWrapHold.Lock()
	subWrapHold.cids[cid] = true
	subWrapHold.Unlock()
}

func releaseSubWrappers(cids ...string) {
	subWrapHold.Lock()
	for _, c := range cids {
		delete(subWrapHold.cids, c)
	}
	subWrapHold.Unlock()
}

// subWrapperHeld 这个目录是不是正在跑的订阅任务刚建的包装目录
func subWrapperHeld(id string) bool {
	if id == "" {
		return false
	}
	subWrapHold.Lock()
	defer subWrapHold.Unlock()
	return subWrapHold.cids[id]
}
