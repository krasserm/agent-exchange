package main

import (
	"errors"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
)

// applyMsg feeds a message through Update and returns the updated model.
func applyMsg(t *testing.T, m model, msg tea.Msg) model {
	t.Helper()
	next, _ := m.Update(msg)
	nm, ok := next.(model)
	if !ok {
		t.Fatalf("Update returned %T, want model", next)
	}
	return nm
}

// TestListResult_ClearsStaleDaemonError reproduces the bug where a transient
// connection failure sets the "Daemon not running." error and a later
// successful poll resets connFailures but leaves the stale error showing.
func TestListResult_ClearsStaleDaemonError(t *testing.T) {
	m := newModel(nil)

	// Three consecutive list failures raise connFailures to the threshold and
	// set the "Daemon not running." error.
	for i := 0; i < 3; i++ {
		m = applyMsg(t, m, listErrMsg{err: errors.New("connection refused")})
	}
	if m.connFailures < 3 {
		t.Fatalf("connFailures = %d, want >= 3", m.connFailures)
	}
	if m.err == "" {
		t.Fatalf("expected daemon error to be set after repeated failures")
	}

	// A successful poll means the daemon is reachable again; the stale error
	// must be cleared so it no longer renders beneath the live session list.
	m = applyMsg(t, m, listResultMsg{sessions: []ListSession{{SessionKey: "abcd"}}})
	if m.connFailures != 0 {
		t.Errorf("connFailures = %d, want 0", m.connFailures)
	}
	if m.err != "" {
		t.Errorf("stale daemon error not cleared after successful poll: %q", m.err)
	}
}

// TestListResult_KeepsActiveTransientError ensures a successful list poll does
// not prematurely wipe an active transient error (e.g. a failed send), which
// expires on its own via clearErrMsg.
func TestListResult_KeepsActiveTransientError(t *testing.T) {
	m := newModel(nil)

	m.err = "Send failed: boom"
	m.errExpiry = time.Now().Add(3 * time.Second)

	m = applyMsg(t, m, listResultMsg{sessions: []ListSession{{SessionKey: "abcd"}}})
	if m.err != "Send failed: boom" {
		t.Errorf("active transient error should be preserved, got %q", m.err)
	}
}

func TestStyleStatus_BackgroundedAndDisconnected(t *testing.T) {
	backgrounded := "backgrounded"
	disconnected := "disconnected"

	if got := styleStatus(&backgrounded); got.GetForeground() != statusBackgrounded.GetForeground() {
		t.Errorf("styleStatus(backgrounded) foreground = %v, want %v",
			got.GetForeground(), statusBackgrounded.GetForeground())
	}
	if got := styleStatus(&disconnected); got.GetForeground() != statusDisconnected.GetForeground() {
		t.Errorf("styleStatus(disconnected) foreground = %v, want %v",
			got.GetForeground(), statusDisconnected.GetForeground())
	}
	// The two new statuses must be distinct from each other and from ended.
	if statusBackgrounded.GetForeground() == statusDisconnected.GetForeground() {
		t.Error("backgrounded and disconnected share a foreground color")
	}
	if statusBackgrounded.GetForeground() == statusEnded.GetForeground() {
		t.Error("backgrounded shares the ended foreground color")
	}
}

func TestStatusText_NewStatuses(t *testing.T) {
	backgrounded := "backgrounded"
	disconnected := "disconnected"

	if got := statusText(&backgrounded); got != "BACKGROUNDED" {
		t.Errorf("statusText(backgrounded) = %q", got)
	}
	if got := statusText(&disconnected); got != "DISCONNECTED" {
		t.Errorf("statusText(disconnected) = %q", got)
	}
}

func TestStatusLabel_BackgroundCounts(t *testing.T) {
	backgrounded := "backgrounded"
	idle := "idle"

	if got := statusLabel(&backgrounded, 1); got != "BACKGROUNDED (1)" {
		t.Errorf("statusLabel(backgrounded, 1) = %q", got)
	}
	if got := statusLabel(&backgrounded, 0); got != "BACKGROUNDED" {
		t.Errorf("statusLabel(backgrounded, 0) = %q", got)
	}
	if got := statusLabel(&idle, 0); got != "IDLE" {
		t.Errorf("statusLabel(idle, 0) = %q", got)
	}
	if got := statusLabel(nil, 0); got != "ENDED" {
		t.Errorf("statusLabel(nil, 0) = %q", got)
	}
}

func TestRenderList_BackgroundedLabel(t *testing.T) {
	backgrounded := "backgrounded"
	sessions := []ListSession{{
		SessionKey:      "abcd1234abcd1234",
		TmuxName:        "cc-myproject-abcd1234",
		StartDir:        "/Users/test/myproject",
		Status:          &backgrounded,
		BackgroundTasks: 1,
		SessionCrons:    0,
	}}

	out := renderList(sessions, 0, 100, 10)
	if !strings.Contains(out, "BACKGROUNDED (1)") {
		t.Errorf("rendered list missing \"BACKGROUNDED (1)\":\n%s", out)
	}
}
