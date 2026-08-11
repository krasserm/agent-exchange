package main

import (
	"fmt"
	"os"
	"os/exec"
)

func openTerminalWithTmux(sessionName string) error {
	if err := exec.Command("tmux", "has-session", "-t", sessionName).Run(); err != nil {
		return fmt.Errorf("tmux session %q not found", sessionName)
	}

	switch os.Getenv("TERM_PROGRAM") {
	case "iTerm.app":
		// iTerm runs the command directly, with no persistent wrapping shell, so
		// the tab closes on its own when tmux attach exits (e.g. the session is
		// stopped). No exec needed.
		cmd := fmt.Sprintf("tmux attach -t %s", sessionName)
		script := fmt.Sprintf(
			`tell application "iTerm2" to tell current window to create tab with default profile command "%s"`,
			cmd)
		return exec.Command("osascript", "-e", script).Run()
	default:
		// Terminal.app's `do script` runs the command inside a persistent login
		// shell that survives the tmux session being killed, keeping the window
		// open. exec replaces that shell with tmux attach, so when the session is
		// stopped the window closes (per "close if the shell exited cleanly").
		cmd := fmt.Sprintf("exec tmux attach -t %s", sessionName)
		script := fmt.Sprintf(
			`tell application "Terminal" to do script "%s"`,
			cmd)
		return exec.Command("osascript", "-e", script).Run()
	}
}
