package api

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"os"
	"path/filepath"
	"testing"
)

func writeTarGz(t *testing.T, p string, files map[string]string) {
	t.Helper()
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	for name, body := range files {
		tw.WriteHeader(&tar.Header{Name: name, Mode: 0o755, Size: int64(len(body)), Typeflag: tar.TypeReg})
		tw.Write([]byte(body))
	}
	tw.Close()
	gz.Close()
	if err := os.WriteFile(p, buf.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
}

// 解压：单个顶层目录上提一层，带 .. 的条目跳过
func TestCloakExtractFlatten(t *testing.T) {
	dir := t.TempDir()
	arc := filepath.Join(dir, "a.tar.gz")
	writeTarGz(t, arc, map[string]string{
		"cloakbrowser-linux-x64/chrome":       "bin",
		"cloakbrowser-linux-x64/lib/x.so":     "so",
		"cloakbrowser-linux-x64/../../escape": "bad",
	})
	dst := filepath.Join(dir, "out")
	n, err := cloakExtract(arc, dst)
	if err != nil {
		t.Fatal(err)
	}
	cloakFlatten(dst)
	if n != 2 {
		t.Errorf("应当解出 2 个文件，得到 %d", n)
	}
	if _, err := os.Stat(filepath.Join(dst, "chrome")); err != nil {
		t.Error("上提后 chrome 应在根下")
	}
	if _, err := os.Stat(filepath.Join(dir, "escape")); err == nil {
		t.Error("带 .. 的条目不该写到外面")
	}
}

// 已装好 = 标记 + 可执行文件都在；只有一样不算
func TestCloakInstalled(t *testing.T) {
	old := cloakRoot
	defer func() { cloakRoot = old }()
	cloakRoot = t.TempDir()
	v := cloakVersion()
	if _, ok := cloakInstalled(); ok {
		t.Fatal("空目录不该算装好")
	}
	os.MkdirAll(cloakBinDir(v), 0o755)
	os.WriteFile(cloakBinPath(v), []byte("x"), 0o755)
	if _, ok := cloakInstalled(); ok {
		t.Fatal("没有校验标记不该算装好（可能是解压到一半）")
	}
	os.WriteFile(cloakMarker(v), []byte(v), 0o644)
	if _, ok := cloakInstalled(); !ok && filepath.Separator == '/' {
		t.Fatal("标记 + 可执行文件都在应当算装好")
	}
	// 种子持久化：两次一样
	if a, b := cloakSeed(), cloakSeed(); a != b || a == "" {
		t.Errorf("指纹种子应当固定：%q %q", a, b)
	}
}
