package main

import (
	"fmt"
	"os"
	"strings"
	"time"

	"charm.land/lipgloss/v2"
)

func renderHeader(width int) string {
	title := headerStyle.Render("agent-session-top")
	keys := helpStyle.Render("[tab]focus  [o]pen  [s]end  [x]stop  [q]uit")
	gap := width - lipgloss.Width(title) - lipgloss.Width(keys)
	if gap < 1 {
		gap = 1
	}
	return title + strings.Repeat(" ", gap) + keys
}

func renderList(sessions []ListSession, cursorIdx int, width int, height int) string {
	if len(sessions) == 0 {
		return dimStyle.Render("\n  No sessions running. Start one with: asn start <dir>\n")
	}

	// Column widths.
	keyW := 8

	// Status column: size to fit the longest label (e.g. "BACKGROUNDED (1)").
	statusW := 9
	for _, s := range sessions {
		if w := len(statusLabel(s.Status, s.BackgroundTasks+s.SessionCrons)); w > statusW {
			statusW = w
		}
	}

	remaining := width - keyW - statusW - 8 // padding/separators

	// Tmux column: size to fit the longest name (priority -- never truncate).
	tmuxW := lipgloss.Width("TMUX SESSION")
	for _, s := range sessions {
		if w := lipgloss.Width(s.TmuxName); w > tmuxW {
			tmuxW = w
		}
	}

	// Project column: take what's left, but never below a usable floor.
	const projFloor = 10
	projW := remaining - tmuxW
	if projW < projFloor {
		projW = projFloor
	}

	header := fmt.Sprintf("  %-*s %-*s %-*s %s",
		keyW, "KEY", projW, "PROJECT", statusW, "STATUS", "TMUX SESSION")

	var b strings.Builder
	b.WriteString(dimStyle.Render(header))
	b.WriteByte('\n')

	for i, s := range sessions {
		if i >= height-2 { // leave room for header
			b.WriteString(dimStyle.Render(fmt.Sprintf("  ... and %d more", len(sessions)-i)))
			break
		}

		prefix := "  "
		style := normalRow
		if i == cursorIdx {
			prefix = cursor.Render("> ")
			style = selectedRow
		}

		key := s.SessionKey
		if len(key) > keyW {
			key = key[:keyW]
		}

		proj := truncate(projectName(s.StartDir), projW)
		st := styleStatus(s.Status).Render(fmt.Sprintf("%-*s", statusW, statusLabel(s.Status, s.BackgroundTasks+s.SessionCrons)))
		tmux := s.TmuxName

		line := fmt.Sprintf("%-*s %-*s %s %s", keyW, key, projW, proj, st, tmux)
		b.WriteString(prefix + style.Render(line) + "\n")
	}
	return b.String()
}

func renderDetailStatus(detail *DetailSession) string {
	if detail == nil {
		return ""
	}
	key := detail.SessionKey
	if len(key) > 8 {
		key = key[:8]
	}
	st := styleStatus(detail.Status).Render(statusLabel(detail.Status, detail.BackgroundTasks+detail.SessionCrons))
	return fmt.Sprintf("SESSION: %s  |  STATUS: %s  |  DIR: %s", key, st, detail.StartDir)
}

func renderMessageContent(messages []Message) string {
	if len(messages) == 0 {
		return dimStyle.Render("No messages yet")
	}
	// Show the last message in full, no truncation.
	last := messages[len(messages)-1]
	ts := formatTimestamp(last.Timestamp)
	return fmt.Sprintf("[%s]\n%s", ts, last.Text)
}

func renderSendModal(inputView string, sessionKey string, width int) string {
	key := sessionKey
	if len(key) > 8 {
		key = key[:8]
	}
	content := fmt.Sprintf("Send to %s:\n%s\n%s",
		key, inputView, dimStyle.Render("Enter to send, Esc to cancel"))
	w := width / 2
	if w < 40 {
		w = 40
	}
	return modalStyle.Width(w).Render(content)
}

func renderStopConfirm(sessionKey string, width int) string {
	key := sessionKey
	if len(key) > 8 {
		key = key[:8]
	}
	content := fmt.Sprintf("Stop session %s? [y/n]", key)
	w := width / 3
	if w < 30 {
		w = 30
	}
	return modalStyle.Width(w).Render(content)
}

func renderError(msg string, width int, height int) string {
	content := errorStyle.Render(msg) + "\n\n" + dimStyle.Render("Press r to retry, q to quit")
	pad := height / 3
	return strings.Repeat("\n", pad) + lipgloss.PlaceHorizontal(width, lipgloss.Center, content)
}

func renderTooSmall() string {
	return "Terminal too small. Minimum 80x24."
}

// Helpers.

func truncate(s string, max int) string {
	if len(s) <= max {
		return s
	}
	if max < 4 {
		return s[:max]
	}
	return s[:max-3] + "..."
}

func projectName(startDir string) string {
	// Format the start dir as a home-relative path.
	// e.g., /Users/martin/Development/agent-exchange/agent-coord -> ~/Development/agent-exchange/agent-coord
	home, err := os.UserHomeDir()
	if err != nil {
		return startDir
	}
	if startDir == home {
		return "~"
	}
	if rel, ok := strings.CutPrefix(startDir, home+"/"); ok {
		return "~/" + rel
	}
	return startDir
}

func formatTimestamp(ts string) string {
	t, err := time.Parse(time.RFC3339, ts)
	if err != nil {
		// Try without timezone.
		t, err = time.Parse("2006-01-02T15:04:05", ts)
		if err != nil {
			return ts
		}
	}
	return t.Local().Format("15:04:05")
}
