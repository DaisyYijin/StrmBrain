package api

import (
	"115-station/internal/config"
	"115-station/internal/model"

	"archive/zip"
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// 备份 → 改动 → 暂存恢复 → 启动时换进去，配置与数据库都回到备份时的样子，
// 恢复前的状态另存了一份
func TestBackupRestoreRoundTrip(t *testing.T) {
	root := t.TempDir()
	cfg := &config.Config{ConfigDir: filepath.Join(root, "config"), DataDir: filepath.Join(root, "data")}
	_ = os.MkdirAll(cfg.ConfigDir, 0o755)
	_ = os.MkdirAll(cfg.DataDir, 0o755)
	old := notifyConfigSource
	notifyConfigSource = cfg
	t.Cleanup(func() { notifyConfigSource = old })

	prevDB := model.DB
	t.Cleanup(func() { model.DB = prevDB })
	dbPath := filepath.Join(cfg.DataDir, backupDBName)
	db, err := model.InitDB(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	h := &Handler{DB: db, Config: cfg}
	if err := cfg.SaveSetting("demo", `{"v":1}`); err != nil {
		t.Fatal(err)
	}
	db.Create(&model.Setting{Key: "probe", Value: "before"})

	item, err := h.createBackupFile("manual")
	if err != nil {
		t.Fatal(err)
	}
	if !backupNameRe.MatchString(item.Name) {
		t.Fatalf("备份文件名 %q 认不出", item.Name)
	}

	// 备份之后又改了
	_ = cfg.SaveSetting("demo", `{"v":2}`)
	db.Model(&model.Setting{}).Where("key = ?", "probe").Update("value", "after")

	info, err := h.stageRestore(filepath.Join(h.backupDir(), item.Name), item.Name)
	if err != nil {
		t.Fatal(err)
	}
	if info.SafetyBak == "" || backupKindOf(info.SafetyBak) != "prerestore" {
		t.Fatalf("恢复前没有另存当前状态: %+v", info)
	}
	if readPendingRestore(cfg.DataDir) == nil {
		t.Fatal("暂存信息读不到")
	}

	// 模拟重启：关库 → 换文件 → 再开
	sqlDB, _ := db.DB()
	_ = sqlDB.Close()
	if !ApplyPendingRestore(cfg) {
		t.Fatal("恢复没有生效")
	}
	if _, err := os.Stat(filepath.Join(cfg.DataDir, restorePending)); !os.IsNotExist(err) {
		t.Fatal("恢复完暂存目录应删掉")
	}
	cfg2 := &config.Config{ConfigDir: cfg.ConfigDir, DataDir: cfg.DataDir}
	if v := cfg2.GetSetting("demo"); v != `{"v":1}` {
		t.Fatalf("setting.yaml 没恢复：%s", v)
	}
	db2, err := model.InitDB(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if s, err := db2.DB(); err == nil {
			_ = s.Close()
		}
	})
	var s model.Setting
	db2.Where("key = ?", "probe").First(&s)
	if s.Value != "before" {
		t.Fatalf("数据库没恢复：%q", s.Value)
	}
	if ApplyPendingRestore(cfg) {
		t.Fatal("没有暂存时不该再恢复")
	}
}

func zipOf(t *testing.T, files map[string]string) *zip.Reader {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for name, body := range files {
		w, _ := zw.Create(name)
		_, _ = w.Write([]byte(body))
	}
	_ = zw.Close()
	zr, err := zip.NewReader(bytes.NewReader(buf.Bytes()), int64(buf.Len()))
	if err != nil {
		t.Fatal(err)
	}
	return zr
}

// 只收本站自己的包：路径穿越、不认识的条目、假数据库一律拒收
func TestValidateBackupZip(t *testing.T) {
	cases := []struct {
		name  string
		files map[string]string
		bad   string
	}{
		{"老版本导出的包", map[string]string{"config/setting.yaml": "a: b"}, ""},
		{"路径穿越", map[string]string{"config/../../etc/passwd": "x"}, "不认识"},
		{"子目录", map[string]string{"config/sub/x": "x", "config/setting.yaml": "a"}, "不认识"},
		{"别的条目", map[string]string{"media/a.strm": "x", "config/setting.yaml": "a"}, "不认识"},
		{"假数据库", map[string]string{backupDBEntry: "not sqlite at all"}, "SQLite"},
		{"空包", map[string]string{"manifest.json": "{}"}, "不是 115-Station"},
	}
	for _, c := range cases {
		_, err := validateBackupZip(zipOf(t, c.files))
		if c.bad == "" {
			if err != nil {
				t.Errorf("%s: 不该拒收: %v", c.name, err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), c.bad) {
			t.Errorf("%s: 期望拒收（%s），得到 %v", c.name, c.bad, err)
		}
	}
}

// 定时备份只清定时备份的旧份，手动的不动
func TestPruneBackupsOnlyOwnKind(t *testing.T) {
	root := t.TempDir()
	cfg := &config.Config{ConfigDir: root, DataDir: root}
	old := notifyConfigSource
	notifyConfigSource = cfg
	t.Cleanup(func() { notifyConfigSource = old })
	h := &Handler{Config: cfg}
	dir := h.backupDir()
	_ = os.MkdirAll(dir, 0o755)
	names := []string{
		"115-station-backup-20261001-040000-auto.zip",
		"115-station-backup-20261002-040000-auto.zip",
		"115-station-backup-20261003-040000-auto.zip",
		"115-station-backup-20261001-120000.zip",
	}
	for _, n := range names {
		_ = os.WriteFile(filepath.Join(dir, n), []byte("x"), 0o644)
	}
	h.pruneBackups("auto", 2)
	left := map[string]bool{}
	for _, b := range h.listBackups() {
		left[b.Name] = true
	}
	if left[names[0]] || !left[names[1]] || !left[names[2]] || !left[names[3]] {
		t.Fatalf("清理结果不对：%v", left)
	}
}
