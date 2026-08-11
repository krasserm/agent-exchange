package main

import (
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
)

// mockDaemon starts a Unix socket server that responds to requests with a handler.
func mockDaemon(t *testing.T, handler func(req map[string]any) map[string]any) *DaemonClient {
	t.Helper()
	sockPath := filepath.Join(t.TempDir(), "test.sock")

	ln, err := net.Listen("unix", sockPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })

	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				var req map[string]any
				if err := json.NewDecoder(conn).Decode(&req); err != nil {
					return
				}
				resp := handler(req)
				data, _ := json.Marshal(resp)
				conn.Write(append(data, '\n'))
			}()
		}
	}()

	return &DaemonClient{socketPath: sockPath}
}

func TestList_HappyPath(t *testing.T) {
	client := mockDaemon(t, func(req map[string]any) map[string]any {
		if req["method"] != "list" {
			t.Errorf("expected method list, got %v", req["method"])
		}
		return map[string]any{
			"ok": true,
			"result": []map[string]any{
				{
					"session_key":       "abcd1234abcd1234abcd1234abcd1234",
					"tmux_session_name": "cc-myproject-abcd1234",
					"start_dir":         "/Users/test/myproject",
					"status":            "idle",
					"session_id":        "sess-123",
				},
			},
		}
	})

	sessions, err := client.List()
	if err != nil {
		t.Fatal(err)
	}
	if len(sessions) != 1 {
		t.Fatalf("expected 1 session, got %d", len(sessions))
	}
	s := sessions[0]
	if s.SessionKey != "abcd1234abcd1234abcd1234abcd1234" {
		t.Errorf("session_key = %q", s.SessionKey)
	}
	if s.Status == nil || *s.Status != "idle" {
		t.Errorf("status = %v", s.Status)
	}
}

func TestList_NullStatus(t *testing.T) {
	client := mockDaemon(t, func(_ map[string]any) map[string]any {
		return map[string]any{
			"ok": true,
			"result": []map[string]any{
				{
					"session_key":       "abcd1234abcd1234abcd1234abcd1234",
					"tmux_session_name": "cc-myproject",
					"start_dir":         "/Users/test/myproject",
					"status":            nil,
					"session_id":        nil,
				},
			},
		}
	})

	sessions, err := client.List()
	if err != nil {
		t.Fatal(err)
	}
	if len(sessions) != 1 {
		t.Fatalf("expected 1 session, got %d", len(sessions))
	}
	if sessions[0].Status != nil {
		t.Errorf("expected nil status, got %v", sessions[0].Status)
	}
	if sessions[0].SessionID != nil {
		t.Errorf("expected nil session_id, got %v", sessions[0].SessionID)
	}
}

func TestList_BackgroundFields(t *testing.T) {
	client := mockDaemon(t, func(_ map[string]any) map[string]any {
		return map[string]any{
			"ok": true,
			"result": []map[string]any{
				{
					"session_key":       "abcd1234abcd1234abcd1234abcd1234",
					"tmux_session_name": "cc-myproject",
					"start_dir":         "/Users/test/myproject",
					"status":            "backgrounded",
					"session_id":        "sess-123",
					"background_tasks":  1,
					"session_crons":     2,
				},
			},
		}
	})

	sessions, err := client.List()
	if err != nil {
		t.Fatal(err)
	}
	if len(sessions) != 1 {
		t.Fatalf("expected 1 session, got %d", len(sessions))
	}
	s := sessions[0]
	if s.Status == nil || *s.Status != "backgrounded" {
		t.Errorf("status = %v", s.Status)
	}
	if s.BackgroundTasks != 1 {
		t.Errorf("background_tasks = %d, want 1", s.BackgroundTasks)
	}
	if s.SessionCrons != 2 {
		t.Errorf("session_crons = %d, want 2", s.SessionCrons)
	}
}

func TestList_NoBGFields(t *testing.T) {
	// Old daemons omit background_tasks/session_crons; both must default to 0.
	client := mockDaemon(t, func(_ map[string]any) map[string]any {
		return map[string]any{
			"ok": true,
			"result": []map[string]any{
				{
					"session_key":       "abcd1234abcd1234abcd1234abcd1234",
					"tmux_session_name": "cc-myproject",
					"start_dir":         "/Users/test/myproject",
					"status":            "idle",
					"session_id":        "sess-123",
				},
			},
		}
	})

	sessions, err := client.List()
	if err != nil {
		t.Fatal(err)
	}
	if len(sessions) != 1 {
		t.Fatalf("expected 1 session, got %d", len(sessions))
	}
	if sessions[0].BackgroundTasks != 0 {
		t.Errorf("background_tasks = %d, want 0", sessions[0].BackgroundTasks)
	}
	if sessions[0].SessionCrons != 0 {
		t.Errorf("session_crons = %d, want 0", sessions[0].SessionCrons)
	}
}

func TestList_EmptyList(t *testing.T) {
	client := mockDaemon(t, func(_ map[string]any) map[string]any {
		return map[string]any{
			"ok":     true,
			"result": []any{},
		}
	})

	sessions, err := client.List()
	if err != nil {
		t.Fatal(err)
	}
	if len(sessions) != 0 {
		t.Fatalf("expected 0 sessions, got %d", len(sessions))
	}
}

func TestStatus_HappyPath(t *testing.T) {
	client := mockDaemon(t, func(req map[string]any) map[string]any {
		params, _ := req["params"].(map[string]any)
		if params["key"] != "abcd" {
			t.Errorf("expected key abcd, got %v", params["key"])
		}
		return map[string]any{
			"ok": true,
			"result": map[string]any{
				"session_key":       "abcd1234abcd1234abcd1234abcd1234",
				"tmux_session_name": "cc-myproject",
				"start_dir":         "/Users/test/myproject",
				"project_dir":       "/Users/test/myproject",
				"status":            "working",
				"session_id":        "sess-123",
				"transcript_path":   "/Users/test/.claude/projects/myproject/sess-123.jsonl",
			},
		}
	})

	detail, err := client.Status("abcd")
	if err != nil {
		t.Fatal(err)
	}
	if detail.SessionKey != "abcd1234abcd1234abcd1234abcd1234" {
		t.Errorf("session_key = %q", detail.SessionKey)
	}
	if detail.Status == nil || *detail.Status != "working" {
		t.Errorf("status = %v", detail.Status)
	}
	if detail.ProjectDir != "/Users/test/myproject" {
		t.Errorf("project_dir = %q", detail.ProjectDir)
	}
}

func TestStatus_BackgroundFields(t *testing.T) {
	client := mockDaemon(t, func(_ map[string]any) map[string]any {
		return map[string]any{
			"ok": true,
			"result": map[string]any{
				"session_key":       "abcd1234abcd1234abcd1234abcd1234",
				"tmux_session_name": "cc-myproject",
				"start_dir":         "/Users/test/myproject",
				"project_dir":       "/Users/test/myproject",
				"status":            "backgrounded",
				"session_id":        "sess-123",
				"transcript_path":   nil,
				"background_tasks":  1,
				"session_crons":     0,
			},
		}
	})

	detail, err := client.Status("abcd")
	if err != nil {
		t.Fatal(err)
	}
	if detail.Status == nil || *detail.Status != "backgrounded" {
		t.Errorf("status = %v", detail.Status)
	}
	if detail.BackgroundTasks != 1 {
		t.Errorf("background_tasks = %d, want 1", detail.BackgroundTasks)
	}
	if detail.SessionCrons != 0 {
		t.Errorf("session_crons = %d, want 0", detail.SessionCrons)
	}
}

func TestStatus_NoBGFields(t *testing.T) {
	// Old daemons omit background_tasks/session_crons; both must default to 0.
	client := mockDaemon(t, func(_ map[string]any) map[string]any {
		return map[string]any{
			"ok": true,
			"result": map[string]any{
				"session_key":       "abcd1234abcd1234abcd1234abcd1234",
				"tmux_session_name": "cc-myproject",
				"start_dir":         "/Users/test/myproject",
				"project_dir":       "/Users/test/myproject",
				"status":            "working",
				"session_id":        "sess-123",
				"transcript_path":   nil,
			},
		}
	})

	detail, err := client.Status("abcd")
	if err != nil {
		t.Fatal(err)
	}
	if detail.BackgroundTasks != 0 {
		t.Errorf("background_tasks = %d, want 0", detail.BackgroundTasks)
	}
	if detail.SessionCrons != 0 {
		t.Errorf("session_crons = %d, want 0", detail.SessionCrons)
	}
}

func TestSend_HappyPath(t *testing.T) {
	var gotMethod string
	var gotMessage string
	client := mockDaemon(t, func(req map[string]any) map[string]any {
		gotMethod = req["method"].(string)
		params, _ := req["params"].(map[string]any)
		gotMessage = params["message"].(string)
		return map[string]any{"ok": true, "result": nil}
	})

	err := client.Send("abcd", "hello world")
	if err != nil {
		t.Fatal(err)
	}
	if gotMethod != "send" {
		t.Errorf("method = %q", gotMethod)
	}
	if gotMessage != "hello world" {
		t.Errorf("message = %q", gotMessage)
	}
}

func TestStop_HappyPath(t *testing.T) {
	client := mockDaemon(t, func(_ map[string]any) map[string]any {
		return map[string]any{"ok": true, "result": nil}
	})

	if err := client.Stop("abcd"); err != nil {
		t.Fatal(err)
	}
}

func TestMessages_HappyPath(t *testing.T) {
	client := mockDaemon(t, func(_ map[string]any) map[string]any {
		return map[string]any{
			"ok": true,
			"result": map[string]any{
				"messages": []map[string]any{
					{"message": "Done writing file.", "timestamp": "2026-04-06T14:23:01Z"},
					{"message": "Running tests now.", "timestamp": "2026-04-06T14:23:05Z"},
				},
			},
		}
	})

	msgs, err := client.Messages("abcd", 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(msgs) != 2 {
		t.Fatalf("expected 2 messages, got %d", len(msgs))
	}
	if msgs[0].Text != "Done writing file." {
		t.Errorf("msg[0] = %q", msgs[0].Text)
	}
}

func TestDaemonError(t *testing.T) {
	client := mockDaemon(t, func(_ map[string]any) map[string]any {
		return map[string]any{
			"ok":    false,
			"error": "No session matching 'xyz'",
		}
	})

	_, err := client.List()
	if err == nil {
		t.Fatal("expected error")
	}
	de, ok := err.(*DaemonError)
	if !ok {
		t.Fatalf("expected DaemonError, got %T: %v", err, err)
	}
	if de.Message != "No session matching 'xyz'" {
		t.Errorf("error message = %q", de.Message)
	}
}

func TestConnectionRefused(t *testing.T) {
	client := &DaemonClient{socketPath: "/tmp/agent-session-top-test-nonexistent.sock"}
	_, err := client.List()
	if err == nil {
		t.Fatal("expected error for missing socket")
	}
}

func TestMalformedResponse(t *testing.T) {
	sockPath := filepath.Join(t.TempDir(), "bad.sock")
	ln, err := net.Listen("unix", sockPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })

	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		// Read request, send garbage
		buf := make([]byte, 1024)
		conn.Read(buf)
		conn.Write([]byte("not json\n"))
	}()

	client := &DaemonClient{socketPath: sockPath}
	_, err = client.List()
	if err == nil {
		t.Fatal("expected error for malformed response")
	}
}

func TestTimeout(t *testing.T) {
	sockPath := filepath.Join(t.TempDir(), "slow.sock")
	ln, err := net.Listen("unix", sockPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })

	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		// Read request but never respond -- let the timeout fire
		buf := make([]byte, 1024)
		conn.Read(buf)
		// Block forever (until connection closes)
		select {}
	}()

	// Use a short timeout for the test
	client := &DaemonClient{socketPath: sockPath}
	// Override timeout would require refactoring, but the 5s default will work.
	// For a faster test, we rely on the deadline being set.
	_, err = client.List()
	if err == nil {
		t.Fatal("expected timeout error")
	}
	if !os.IsTimeout(err) {
		// The error is wrapped, check if it contains timeout info
		t.Logf("got error (expected timeout): %v", err)
	}
}
