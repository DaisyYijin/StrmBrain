package api

import (
	"os"
	"testing"
)

// TestMain 老测试按 full.local_path 把本地根指到临时目录（mediaroot.go 的 mediaRootLegacyForTests）
func TestMain(m *testing.M) {
	mediaRootLegacyForTests = true
	os.Exit(m.Run())
}
