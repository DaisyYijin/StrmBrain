package api

import (
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"

	"115-station/internal/model"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"
)

// ==================== 多个 115 账号：账号层（2026-10-20，MULTI-ACCOUNT-PLAN.md §4，阶段 2） ====================
//
// 主号 = Storage 表里 type=115、Main=true 的那一行（老部署由 MigrateMainAccount 把原来那一行标上）。
// 其余 type=115 的行是附加账号：只登录、检测、浏览目录，阶段 3 才能把它的目录挂成「我自己整理」的库。
// 整理、转存、订阅、离线、机器人、签到一律只用主号，所以 get115Cookie / newPan115Ops 这些老入口不动，
// 只有要按账号办事的地方改走 cookieOf / opsFor。
//
// 账号 id：主号在代码里一律记 0（acctMain），存量数据（库、台账、记录）都不用改；附加账号用 Storage.ID。
//
// 这一阶段刻意不做的：
//   - 附加账号的 OpenAPI：只支持扫码 / 手动导入 Cookie（开放平台要每个账号各申请一个 AppID，阶段 3 再看要不要）；
//   - 按账号分节流队列：附加账号的请求照旧排进主号那条全局队列，只会更慢不会更快。拆不拆要等阶段 0 实测风控按账号还是按 IP 算；
//   - 写操作：附加账号的 pan115Ops 一律拒绝 move / rename / delete / mkdir（acctReadOnly），防止哪条路误拿它去改网盘；
//   - 路径缓存：附加账号的目录不写 PathCache（那张表按路径查时不分账号，两个账号都有「/影视」就会串）。
//
// 界面默认藏着（STATION_MULTI_ACCOUNT=1 才显示）：阶段 3 之前附加账号登录了也挂不了库，露出来只会让人困惑。

// acctMain 主号在代码里的账号 id
const acctMain uint = 0

// errAcctReadOnly 附加账号的通道不许写网盘
var errAcctReadOnly = errors.New("附加账号只能读，不能改动网盘上的文件")

// multiAccountUIEnabled 界面是否显示附加账号（阶段 3 完成前默认关）
func multiAccountUIEnabled() bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("STATION_MULTI_ACCOUNT")))
	return v == "1" || v == "true" || v == "yes"
}

// mainStorage115 主号那一行。一行 Main 都没有（MigrateMainAccount 跑之前）按老口径取 type=115 的第一行：
// 那时还不可能有附加账号（添加附加账号要求主号已存在，删主号在有附加账号时被拦）
func mainStorage115(db *gorm.DB) (model.Storage, error) {
	var s model.Storage
	if db == nil {
		return s, errors.New("数据库未就绪")
	}
	if err := db.Where("type = ? AND main = ?", "115", true).Order("id").First(&s).Error; err == nil {
		return s, nil
	}
	err := db.Where("type = ?", "115").Order("id").First(&s).Error
	return s, err
}

// MigrateMainAccount 启动时把主号标上（幂等）：没有任何 Main 行时，原来 First 取到的那一行就是主号
func (h *Handler) MigrateMainAccount() {
	if h.DB == nil {
		return
	}
	var n int64
	h.DB.Model(&model.Storage{}).Where("type = ? AND main = ?", "115", true).Count(&n)
	if n > 0 {
		return
	}
	var s model.Storage
	if err := h.DB.Where("type = ?", "115").Order("id").First(&s).Error; err != nil {
		return // 还没登录过 115：第一次扫码时 upsert115Storage 建出来就是主号
	}
	if err := h.DB.Model(&s).Update("main", true).Error; err != nil {
		log.Printf("[账号] ✗ 标记主账号失败（下次启动再试）: %v", err)
		return
	}
	log.Printf("[账号] ✓ 已把现有 115 账号（id=%d）标为主账号", s.ID)
}

// extraAccount 附加账号那一行（不是主号、类型是 115）
func (h *Handler) extraAccount(id uint) (model.Storage, error) {
	var s model.Storage
	if id == acctMain {
		return s, errors.New("这是主账号")
	}
	if err := h.DB.Where("id = ? AND type = ? AND main = ?", id, "115", false).First(&s).Error; err != nil {
		return s, fmt.Errorf("附加账号 id=%d 不存在", id)
	}
	if m, err := mainStorage115(h.DB); err == nil && m.ID == s.ID {
		return model.Storage{}, errors.New("这是主账号")
	}
	return s, nil
}

// cookieOf 某个账号的 Cookie。0 = 主号（get115Cookie 老逻辑）
func (h *Handler) cookieOf(acct uint) (string, error) {
	if acct == acctMain {
		return h.get115Cookie()
	}
	s, err := h.extraAccount(acct)
	if err != nil {
		return "", err
	}
	if ck, err := h.Config.LoadCookieOf(acct); err == nil && ck != "" {
		return ck, nil
	}
	if s.Cookie != "" {
		return s.Cookie, nil
	}
	return "", fmt.Errorf("附加账号「%s」还没登录 115", s.Name)
}

// opsFor 某个账号的操作通道。0 = 主号（newPan115Ops，OpenAPI 优先）；附加账号只走 Cookie、只读
func (h *Handler) opsFor(acct uint) (*pan115Ops, error) {
	if acct == acctMain {
		return h.newPan115Ops()
	}
	cookie, err := h.cookieOf(acct)
	if err != nil {
		return nil, err
	}
	return &pan115Ops{cookie: cookie, acct: acct}, nil
}

// acctOfQuery 请求里的 ?account=（缺省 / 0 = 主号）
func acctOfQuery(c *gin.Context) (uint, error) {
	raw := strings.TrimSpace(c.Query("account"))
	if raw == "" || raw == "0" {
		return acctMain, nil
	}
	n, err := strconv.ParseUint(raw, 10, 32)
	if err != nil {
		return 0, fmt.Errorf("账号参数不对：%s", raw)
	}
	return uint(n), nil
}

// acctDupOf 这份 Cookie 是不是已经登录在别的账号上（主号或别的附加账号）。返回那个账号的名字
func (h *Handler) acctDupOf(self uint, cookie string) string {
	uid := cookieUserID(cookie) // upload115.go：UID=12345_A1_xxx 的第一段，认不出为 0
	if uid == 0 {
		return ""
	}
	if self != acctMain {
		if ck, err := h.get115Cookie(); err == nil && cookieUserID(ck) == uid {
			return "主账号"
		}
	}
	var rows []model.Storage
	h.DB.Where("type = ? AND main = ?", "115", false).Find(&rows)
	for _, r := range rows {
		if r.ID == self {
			continue
		}
		ck, err := h.Config.LoadCookieOf(r.ID)
		if err != nil || ck == "" {
			ck = r.Cookie
		}
		if ck != "" && cookieUserID(ck) == uid {
			return "附加账号「" + r.Name + "」"
		}
	}
	return ""
}

// saveExtraCookie 附加账号登录成功：落盘 Cookie、记下 115 用户名。
// 同一个 115 账号已经登录在别处时拒绝（同一份网盘挂两遍，阶段 3 会生成两份一模一样的 STRM）
func (h *Handler) saveExtraCookie(acct uint, cookie, device, username string) (warning string, err error) {
	s, err := h.extraAccount(acct)
	if err != nil {
		return "", err
	}
	if dup := h.acctDupOf(acct, cookie); dup != "" {
		return "", fmt.Errorf("这个 115 账号已经登录在%s上了，不用再加一次", dup)
	}
	if old := s.Cookie; old != "" {
		if ck, e := h.Config.LoadCookieOf(acct); e == nil && ck != "" {
			old = ck
		}
		if a, b := cookieUserID(old), cookieUserID(cookie); a != 0 && b != 0 && a != b {
			// 阶段 3 起这个账号下的库要重新全量（pickcode 不跨账号），这里先提醒
			warning = fmt.Sprintf("这次登录的是另一个 115 用户（原来是「%s」）", firstNonEmpty(s.UserName, "未知"))
		}
	}
	if err := h.Config.SaveCookieOf(acct, cookie); err != nil {
		return "", fmt.Errorf("保存 Cookie 失败: %w", err)
	}
	updates := map[string]any{"cookie": cookie, "device": device, "status": "online"}
	if username != "" {
		updates["user_name"] = username
	}
	h.DB.Model(&model.Storage{}).Where("id = ?", acct).Updates(updates)
	log.Printf("[账号] ✓ 附加账号「%s」已登录 115（%s）", s.Name, firstNonEmpty(username, "用户名未知"))
	return warning, nil
}

// ---- 接口 ----

// acctView 账号列表的一行（零 115 请求：状态与用户名都是上次登录 / 检测时记下的）
type acctView struct {
	ID       uint   `json:"id"`
	Name     string `json:"name"`
	UserName string `json:"user_name"`
	Main     bool   `json:"main"`
	Status   string `json:"status"` // online / offline / invalid
	Device   string `json:"device"`
	LoggedIn bool   `json:"logged_in"`
}

// ListAccounts115 GET /storage/115/accounts
func (h *Handler) ListAccounts115(c *gin.Context) {
	out := []acctView{}
	if m, err := mainStorage115(h.DB); err == nil {
		ck, _ := h.get115Cookie()
		out = append(out, acctView{ID: acctMain, Name: m.Name, UserName: firstNonEmpty(m.UserName, m.Name), Main: true,
			Status: m.Status, Device: m.Device, LoggedIn: ck != "" || m.OpenapiEnabled})
	}
	var rows []model.Storage
	h.DB.Where("type = ? AND main = ?", "115", false).Order("id").Find(&rows)
	mainID := uint(0)
	if m, err := mainStorage115(h.DB); err == nil {
		mainID = m.ID
	}
	for _, r := range rows {
		if r.ID == mainID {
			continue
		}
		ck, _ := h.Config.LoadCookieOf(r.ID)
		out = append(out, acctView{ID: r.ID, Name: r.Name, UserName: r.UserName, Status: r.Status, Device: r.Device,
			LoggedIn: ck != "" || r.Cookie != ""})
	}
	c.JSON(http.StatusOK, gin.H{"data": out, "enabled": multiAccountUIEnabled()})
}

// acctNameOK 附加账号的名字：给人看的标签，不能空、不能太长
func acctNameOK(name string) (string, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		return "", errors.New("给这个账号起个名字（比如「副号」「老婆的号」）")
	}
	if len([]rune(name)) > 30 {
		return "", errors.New("名字最多 30 个字")
	}
	return name, nil
}

// CreateAccount115 POST /storage/115/accounts {name}：只建一行，登录走扫码或导入 Cookie
func (h *Handler) CreateAccount115(c *gin.Context) {
	var req struct {
		Name string `json:"name"`
	}
	_ = c.ShouldBindJSON(&req)
	name, err := acctNameOK(req.Name)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	if _, err := mainStorage115(h.DB); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "先登录主账号，再添加附加账号"})
		return
	}
	var n int64
	h.DB.Model(&model.Storage{}).Where("type = ? AND main = ? AND name = ?", "115", false, name).Count(&n)
	if n > 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "已经有一个叫「" + name + "」的账号了"})
		return
	}
	s := model.Storage{Name: name, Type: "115", Device: "web", Status: "offline", Interval: 3}
	if err := h.DB.Create(&s).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "保存失败: " + err.Error()})
		return
	}
	log.Printf("[账号] ✓ 已添加附加账号「%s」（id=%d），等登录", name, s.ID)
	c.JSON(http.StatusOK, gin.H{"data": acctView{ID: s.ID, Name: s.Name, Status: s.Status, Device: s.Device}})
}

// paramAcct 路由参数 :id → 附加账号
func (h *Handler) paramAcct(c *gin.Context) (model.Storage, bool) {
	n, err := strconv.ParseUint(c.Param("id"), 10, 32)
	if err != nil || n == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "账号参数不对"})
		return model.Storage{}, false
	}
	s, err := h.extraAccount(uint(n))
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return model.Storage{}, false
	}
	return s, true
}

// UpdateAccount115 PUT /storage/115/accounts/:id {name}：改名字（主号的名字就是 115 用户名，不在这里改）
func (h *Handler) UpdateAccount115(c *gin.Context) {
	s, ok := h.paramAcct(c)
	if !ok {
		return
	}
	var req struct {
		Name string `json:"name"`
	}
	_ = c.ShouldBindJSON(&req)
	name, err := acctNameOK(req.Name)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}
	var n int64
	h.DB.Model(&model.Storage{}).Where("type = ? AND main = ? AND name = ? AND id <> ?", "115", false, name, s.ID).Count(&n)
	if n > 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "已经有一个叫「" + name + "」的账号了"})
		return
	}
	h.DB.Model(&model.Storage{}).Where("id = ?", s.ID).Update("name", name)
	c.JSON(http.StatusOK, gin.H{"message": "已保存"})
}

// DeleteAccount115 DELETE /storage/115/accounts/:id：删行、删 Cookie 文件。
// 阶段 3 起挂着库的账号要先移除库（这里再加一道拦）
func (h *Handler) DeleteAccount115(c *gin.Context) {
	s, ok := h.paramAcct(c)
	if !ok {
		return
	}
	if err := h.Config.RemoveCookieOf(s.ID); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "删除 Cookie 文件失败: " + err.Error()})
		return
	}
	if err := h.DB.Delete(&model.Storage{}, s.ID).Error; err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "删除失败: " + err.Error()})
		return
	}
	dirCacheForgetAcct(s.ID)
	log.Printf("[账号] ✓ 已删除附加账号「%s」（id=%d），网盘上的东西不动", s.Name, s.ID)
	c.JSON(http.StatusOK, gin.H{"message": "已删除"})
}

// CheckAccount115 POST /storage/115/accounts/:id/check {cookie?}：检测附加账号；带 cookie 就是手动导入，有效才保存
func (h *Handler) CheckAccount115(c *gin.Context) {
	s, ok := h.paramAcct(c)
	if !ok {
		return
	}
	var req struct {
		Cookie string `json:"cookie"`
	}
	_ = c.ShouldBindJSON(&req)
	cookie := strings.TrimSpace(req.Cookie)
	manual := cookie != ""
	if !manual {
		var err error
		if cookie, err = h.cookieOf(s.ID); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
	} else if !strings.Contains(cookie, "UID=") || !strings.Contains(cookie, "SEID=") {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Cookie 格式不正确，需要包含 UID=...;CID=...;SEID=... 字段"})
		return
	}
	resp, userName := probe115Cookie(cookie)
	if userName == "" {
		if !manual {
			h.DB.Model(&model.Storage{}).Where("id = ?", s.ID).Update("status", "invalid")
		}
		c.JSON(http.StatusOK, resp)
		return
	}
	if manual {
		warning, err := h.saveExtraCookie(s.ID, cookie, "web", userName)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		if warning != "" {
			resp["warning"] = warning
		}
	} else {
		h.DB.Model(&model.Storage{}).Where("id = ?", s.ID).Updates(map[string]any{"status": "online", "user_name": userName})
	}
	c.JSON(http.StatusOK, resp)
}

// ---- 附加账号的目录浏览（不碰路径缓存，见文件头） ----

// dirCacheKey 目录选择器缓存的键：主号沿用裸 cid（老行为），附加账号加前缀，免得两个账号的同一个 cid 串
func dirCacheKey(acct uint, cid string) string {
	if acct == acctMain {
		return cid
	}
	return fmt.Sprintf("a%d:%s", acct, cid)
}

// dirCacheForgetAcct 删账号时清掉它的目录缓存
func dirCacheForgetAcct(acct uint) {
	prefix := fmt.Sprintf("a%d:", acct)
	dirCacheMu.Lock()
	defer dirCacheMu.Unlock()
	for k := range dirCache {
		if strings.HasPrefix(k, prefix) {
			delete(dirCache, k)
		}
	}
}

// acctDirAbs 附加账号里一个目录的网盘绝对路径：每次现查祖先链，不写路径缓存
func acctDirAbs(cookie, cid string) (string, error) {
	if cid == "" || cid == "0" {
		return "/", nil
	}
	chain, err := fetch115Ancestors(cookie, cid)
	if err != nil {
		return "", err
	}
	if ancestorChainGone(chain, cid) {
		return "", errDirGone
	}
	rows := chainToRows(chain)
	if len(rows) == 0 {
		return "/", nil
	}
	return rows[len(rows)-1].Path, nil
}

// loadMainStorage115 把主号那一行读进 out（原来各处的 Where("type = ?", "115").First(&storage) 换成它：
// 有了附加账号之后「type=115 的第一行」不一定是主号）
func loadMainStorage115(db *gorm.DB, out *model.Storage) error {
	s, err := mainStorage115(db)
	if err == nil {
		*out = s
	}
	return err
}
