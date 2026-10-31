//go:build windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
	"unsafe"
)

func showError(message string) {
	// Ошибка запуска видна и без консольного окна.
	user32 := syscall.NewLazyDLL("user32.dll")
	text, _ := syscall.UTF16PtrFromString(message)
	title, _ := syscall.UTF16PtrFromString("Пульсар — ошибка запуска")
	user32.NewProc("MessageBoxW").Call(0, uintptr(unsafe.Pointer(text)), uintptr(unsafe.Pointer(title)), 0x10)
}

func main() {
	executable, err := os.Executable()
	if err != nil {
		showError(err.Error())
		return
	}
	base := filepath.Dir(executable)
	logRoot, err := os.UserCacheDir()
	if err != nil {
		logRoot = os.TempDir()
	}
	logRoot = filepath.Join(logRoot, "PulsarLoader")
	if err = os.MkdirAll(logRoot, 0755); err != nil {
		showError(err.Error())
		return
	}
	logPath := filepath.Join(logRoot, "startup.log")
	log, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644)
	if err != nil {
		showError(err.Error())
		return
	}
	defer log.Close()
	fmt.Fprintln(log, "\nПуск:", time.Now().Format(time.RFC3339))
	command := exec.Command(filepath.Join(base, "runtime", "pythonw.exe"), "-B", filepath.Join(base, "app", "launch.pyw"))
	command.Dir = base
	command.Env = append(os.Environ(), "PYTHONUTF8=1")
	command.Stdout, command.Stderr = log, log
	command.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	if err = command.Run(); err != nil {
		fmt.Fprintln(log, err)
		showError("Не удалось открыть приложение.\n\nРаспакуйте весь ZIP в отдельную папку. Папки app и runtime должны лежать рядом с PulsarLoader.exe.\n\nЖурнал: " + logPath + "\n\n" + err.Error())
	}
}
