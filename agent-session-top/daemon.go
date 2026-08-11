package main

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"time"
)

const dialTimeout = 5 * time.Second

// DaemonClient connects to the agent-session daemon over a Unix socket.
type DaemonClient struct {
	socketPath string
}

// NewClient creates a client that auto-detects the socket path from the current UID.
func NewClient() *DaemonClient {
	return &DaemonClient{
		socketPath: fmt.Sprintf("/tmp/agent-session-%d.sock", os.Getuid()),
	}
}

type request struct {
	Method string `json:"method"`
	Params any        `json:"params"`
}

type response struct {
	OK     bool            `json:"ok"`
	Result json.RawMessage `json:"result"`
	Error  string          `json:"error"`
}

// DaemonError is returned when the daemon responds with ok=false.
type DaemonError struct {
	Message string
}

func (e *DaemonError) Error() string { return e.Message }

// Call sends a request to the daemon and returns the raw result.
func (c *DaemonClient) Call(method string, params any) (json.RawMessage, error) {
	conn, err := net.DialTimeout("unix", c.socketPath, dialTimeout)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	defer conn.Close()

	if err := conn.SetDeadline(time.Now().Add(dialTimeout)); err != nil {
		return nil, fmt.Errorf("set deadline: %w", err)
	}

	req := request{Method: method, Params: params}
	data, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("marshal: %w", err)
	}
	data = append(data, '\n')

	if _, err := conn.Write(data); err != nil {
		return nil, fmt.Errorf("write: %w", err)
	}

	var resp response
	decoder := json.NewDecoder(conn)
	if err := decoder.Decode(&resp); err != nil {
		return nil, fmt.Errorf("read: %w", err)
	}

	if !resp.OK {
		return nil, &DaemonError{Message: resp.Error}
	}
	return resp.Result, nil
}

// Session types matching the daemon's JSON responses.

type ListSession struct {
	SessionKey      string  `json:"session_key"`
	Agent           string  `json:"agent"`
	TmuxName        string  `json:"tmux_session_name"`
	StartDir        string  `json:"start_dir"`
	Status          *string `json:"status"`
	SessionID       *string `json:"session_id"`
	BackgroundTasks int     `json:"background_tasks"`
	SessionCrons    int     `json:"session_crons"`
}

type DetailSession struct {
	SessionKey      string  `json:"session_key"`
	Agent           string  `json:"agent"`
	TmuxName        string  `json:"tmux_session_name"`
	StartDir        string  `json:"start_dir"`
	Status          *string `json:"status"`
	SessionID       *string `json:"session_id"`
	ProjectDir      string  `json:"project_dir"`
	TranscriptPath  *string `json:"transcript_path"`
	BackgroundTasks int     `json:"background_tasks"`
	SessionCrons    int     `json:"session_crons"`
}

type Message struct {
	Text      string `json:"message"`
	Timestamp string `json:"timestamp"`
}

type messagesResponse struct {
	Messages []Message `json:"messages"`
}

// Convenience methods.

func (c *DaemonClient) List() ([]ListSession, error) {
	raw, err := c.Call("list", map[string]any{})
	if err != nil {
		return nil, err
	}
	var sessions []ListSession
	if err := json.Unmarshal(raw, &sessions); err != nil {
		return nil, fmt.Errorf("parse list: %w", err)
	}
	return sessions, nil
}

func (c *DaemonClient) Status(key string) (*DetailSession, error) {
	raw, err := c.Call("status", map[string]any{"key": key})
	if err != nil {
		return nil, err
	}
	var detail DetailSession
	if err := json.Unmarshal(raw, &detail); err != nil {
		return nil, fmt.Errorf("parse status: %w", err)
	}
	return &detail, nil
}

func (c *DaemonClient) Send(key string, message string) error {
	_, err := c.Call("send", map[string]any{"key": key, "message": message})
	return err
}

func (c *DaemonClient) Stop(key string) error {
	_, err := c.Call("stop", map[string]any{"key": key})
	return err
}

func (c *DaemonClient) Messages(key string, last int) ([]Message, error) {
	raw, err := c.Call("messages", map[string]any{"key": key, "last": last})
	if err != nil {
		return nil, err
	}
	var resp messagesResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, fmt.Errorf("parse messages: %w", err)
	}
	return resp.Messages, nil
}

