package main

import (
	"archive/zip"
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

const (
	fallbackVersion = "2.6.5"
	manifestURL     = "https://raw.githubusercontent.com/gggg456228-droid/THERE/main/update/latest.json"
)

type pythonCommand struct {
	path string
	args []string
}

type updateManifest struct {
	Version          string `json:"version"`
	WindowsBundleURL string `json:"windows_bundle_url"`
	SHA256           string `json:"sha256"`
}

func exists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func validPython(candidate pythonCommand) bool {
	args := append([]string{}, candidate.args...)
	args = append(args, "-c", "import sys;raise SystemExit(0 if sys.version_info >= (3,10) else 1)")
	cmd := exec.Command(candidate.path, args...)
	cmd.Stdout = nil
	cmd.Stderr = nil
	return cmd.Run() == nil
}

func findPython() (pythonCommand, bool) {
	var candidates []pythonCommand
	if path, err := exec.LookPath("py.exe"); err == nil {
		candidates = append(candidates, pythonCommand{path: path, args: []string{"-3"}})
	}
	if path, err := exec.LookPath("python.exe"); err == nil {
		candidates = append(candidates, pythonCommand{path: path})
	}

	local := os.Getenv("LOCALAPPDATA")
	programFiles := os.Getenv("ProgramFiles")
	versions := []string{"314", "313", "312", "311", "310"}
	for _, version := range versions {
		candidates = append(candidates,
			pythonCommand{path: filepath.Join(local, "Programs", "Python", "Python"+version, "python.exe")},
			pythonCommand{path: filepath.Join(programFiles, "Python"+version, "python.exe")},
			pythonCommand{path: filepath.Join(`C:\`, "Python"+version, "python.exe")},
		)
	}

	seen := map[string]bool{}
	for _, candidate := range candidates {
		key := strings.ToLower(candidate.path + "\x00" + strings.Join(candidate.args, "\x00"))
		if seen[key] || candidate.path == "" {
			continue
		}
		seen[key] = true
		if (strings.EqualFold(filepath.Base(candidate.path), "py.exe") || exists(candidate.path)) && validPython(candidate) {
			return candidate, true
		}
	}
	return pythonCommand{}, false
}

func waitForEnter() {
	fmt.Println()
	fmt.Print("Нажмите Enter для выхода.")
	_, _ = bufio.NewReader(os.Stdin).ReadString('\n')
}

func localVersion(root string) string {
	data, err := os.ReadFile(filepath.Join(root, "VERSION.txt"))
	if err != nil {
		return fallbackVersion
	}
	v := strings.TrimSpace(string(data))
	if v == "" {
		return fallbackVersion
	}
	return v
}

func numericVersion(v string) []int {
	v = strings.TrimSpace(v)
	if i := strings.IndexAny(v, "-+"); i >= 0 {
		v = v[:i]
	}
	parts := strings.Split(v, ".")
	out := make([]int, 0, len(parts))
	for _, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil {
			n = 0
		}
		out = append(out, n)
	}
	return out
}

func newer(remote, local string) bool {
	a, b := numericVersion(remote), numericVersion(local)
	n := len(a)
	if len(b) > n {
		n = len(b)
	}
	for i := 0; i < n; i++ {
		av, bv := 0, 0
		if i < len(a) {
			av = a[i]
		}
		if i < len(b) {
			bv = b[i]
		}
		if av != bv {
			return av > bv
		}
	}
	return false
}

func httpClient() *http.Client {
	return &http.Client{Timeout: 12 * time.Second}
}

func fetchManifest() (updateManifest, error) {
	var m updateManifest
	req, _ := http.NewRequest(http.MethodGet, manifestURL, nil)
	req.Header.Set("User-Agent", "THERE-Updater/1")
	resp, err := httpClient().Do(req)
	if err != nil {
		return m, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return m, fmt.Errorf("manifest HTTP %d", resp.StatusCode)
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64*1024)).Decode(&m); err != nil {
		return m, err
	}
	m.Version = strings.TrimSpace(m.Version)
	m.WindowsBundleURL = strings.TrimSpace(m.WindowsBundleURL)
	m.SHA256 = strings.ToLower(strings.TrimSpace(m.SHA256))
	if m.Version == "" || m.WindowsBundleURL == "" {
		return m, errors.New("incomplete update manifest")
	}
	return m, nil
}

func downloadFile(url, dst string) error {
	req, _ := http.NewRequest(http.MethodGet, url, nil)
	req.Header.Set("User-Agent", "THERE-Updater/1")
	resp, err := httpClient().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("download HTTP %d", resp.StatusCode)
	}
	f, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer f.Close()
	_, err = io.Copy(f, io.LimitReader(resp.Body, 300*1024*1024))
	return err
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func unzip(src, dst string) error {
	zr, err := zip.OpenReader(src)
	if err != nil {
		return err
	}
	defer zr.Close()
	cleanDst, err := filepath.Abs(dst)
	if err != nil {
		return err
	}

	for _, f := range zr.File {
		target := filepath.Join(cleanDst, filepath.Clean(f.Name))
		if target != cleanDst && !strings.HasPrefix(target, cleanDst+string(os.PathSeparator)) {
			return errors.New("unsafe path in update archive")
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
			return err
		}
		r, err := f.Open()
		if err != nil {
			return err
		}
		w, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, f.Mode())
		if err != nil {
			r.Close()
			return err
		}
		_, copyErr := io.Copy(w, r)
		closeErr := w.Close()
		r.Close()
		if copyErr != nil {
			return copyErr
		}
		if closeErr != nil {
			return closeErr
		}
	}
	return nil
}

func scheduleReplacement(root, extracted, tempRoot string) error {
	script := filepath.Join(os.TempDir(), fmt.Sprintf("there-update-%d.cmd", os.Getpid()))
	exe := filepath.Join(root, "THERE.exe")
	body := fmt.Sprintf(`@echo off
setlocal
set "OLDPID=%d"
:wait
tasklist /FI "PID eq %%OLDPID%%" 2>NUL | find "%%OLDPID%%" >NUL
if not errorlevel 1 (
  timeout /t 1 /nobreak >NUL
  goto wait
)
robocopy "%s" "%s" /E /COPY:DAT /R:3 /W:1 >NUL
start "" "%s" --skip-update-once
rmdir /S /Q "%s" 2>NUL
del "%%~f0"
`, os.Getpid(), extracted, root, exe, tempRoot)
	if err := os.WriteFile(script, []byte(body), 0600); err != nil {
		return err
	}
	cmd := exec.Command("cmd.exe", "/C", "start", "", "/min", script)
	return cmd.Start()
}

func maybeUpdate(root string) (bool, error) {
	for _, arg := range os.Args[1:] {
		if arg == "--skip-update-once" {
			return false, nil
		}
	}

	m, err := fetchManifest()
	if err != nil {
		return false, err
	}
	current := localVersion(root)
	if !newer(m.Version, current) {
		return false, nil
	}

	fmt.Printf("Доступно обновление THERE %s -> %s. Загружаю...\n", current, m.Version)
	tempRoot, err := os.MkdirTemp("", "there-update-")
	if err != nil {
		return false, err
	}
	archivePath := filepath.Join(tempRoot, "update.zip")
	extractPath := filepath.Join(tempRoot, "new")

	if err := downloadFile(m.WindowsBundleURL, archivePath); err != nil {
		os.RemoveAll(tempRoot)
		return false, err
	}
	if m.SHA256 != "" {
		got, err := fileSHA256(archivePath)
		if err != nil || got != m.SHA256 {
			os.RemoveAll(tempRoot)
			if err != nil {
				return false, err
			}
			return false, errors.New("update SHA256 mismatch")
		}
	}
	if err := unzip(archivePath, extractPath); err != nil {
		os.RemoveAll(tempRoot)
		return false, err
	}
	if !exists(filepath.Join(extractPath, "THERE.exe")) || !exists(filepath.Join(extractPath, "runtime", "app", "standalone_entry.py")) {
		os.RemoveAll(tempRoot)
		return false, errors.New("update bundle is incomplete")
	}
	if err := scheduleReplacement(root, extractPath, tempRoot); err != nil {
		os.RemoveAll(tempRoot)
		return false, err
	}
	fmt.Println("Обновление загружено. THERE перезапустится автоматически.")
	return true, nil
}

func main() {
	if runtime.GOOS != "windows" {
		fmt.Println("Этот запускатель предназначен для Windows.")
		return
	}

	exePath, err := os.Executable()
	if err != nil {
		fmt.Println("Не удалось определить папку THERE:", err)
		waitForEnter()
		return
	}
	root := filepath.Dir(exePath)

	if scheduled, err := maybeUpdate(root); scheduled {
		return
	} else if err != nil {
		fmt.Println("Проверка обновлений не удалась, запускаю текущую версию:", err)
	}

	appScript := filepath.Join(root, "runtime", "app", "standalone_entry.py")
	packages := filepath.Join(root, "runtime", "packages")
	appDir := filepath.Join(root, "runtime", "app")

	if !exists(appScript) {
		fmt.Println("Повреждена папка THERE. Не найден runtime\\app\\standalone_entry.py")
		waitForEnter()
		return
	}

	python, ok := findPython()
	if !ok {
		fmt.Println("THERE не нашёл Python 3.10 или новее.")
		fmt.Println("Установите Python с python.org и снова запустите THERE.exe.")
		waitForEnter()
		return
	}

	args := append([]string{}, python.args...)
	args = append(args, "-u", "-s", appScript)
	cmd := exec.Command(python.path, args...)
	cmd.Dir = root
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	pathValue := packages + string(os.PathListSeparator) + appDir
	env := os.Environ()
	env = append(env,
		"PYTHONPATH="+pathValue,
		"PYTHONNOUSERSITE=1",
		"PYTHONUTF8=1",
	)
	cmd.Env = env

	if err := cmd.Run(); err != nil {
		fmt.Println()
		fmt.Println("THERE завершился с ошибкой:", err)
		waitForEnter()
	}
}
