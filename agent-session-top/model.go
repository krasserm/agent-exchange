package main

import (
	"fmt"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/bubbles/v2/spinner"
	"charm.land/bubbles/v2/textinput"
	"charm.land/bubbles/v2/viewport"
	"charm.land/lipgloss/v2"
)

type viewMode int

const (
	modeNormal viewMode = iota
	modeSend
	modeStopConfirm
)

const (
	focusList   = 0
	focusDetail = 1
)

// Tick messages.
type tickListMsg struct{}
type tickDetailMsg struct{}

// Result messages from daemon calls.
type listResultMsg struct{ sessions []ListSession }
type detailResultMsg struct {
	detail   *DetailSession
	messages []Message
}
type listErrMsg struct{ err error }
type detailErrMsg struct{ err error }
type sendResultMsg struct{ err error }
type stopResultMsg struct{ err error }
type openTerminalMsg struct{ err error }
type clearErrMsg struct{}

type model struct {
	client   *DaemonClient
	sessions []ListSession
	cursor   int

	detail   *DetailSession
	messages []Message

	viewport   viewport.Model
	focused    int
	prevCursor int

	width  int
	height int

	mode      viewMode
	sendInput textinput.Model
	spinner   spinner.Model

	err       string
	errExpiry time.Time

	lastStopTime time.Time
	connFailures int
}

func newModel(client *DaemonClient) model {
	ti := textinput.New()
	ti.Placeholder = "Type a message..."
	ti.CharLimit = 0

	sp := spinner.New()
	sp.Spinner = spinner.Dot

	vp := viewport.New(viewport.WithWidth(76), viewport.WithHeight(10))

	return model{
		client:     client,
		sendInput:  ti,
		spinner:    sp,
		viewport:   vp,
		focused:    focusList,
		prevCursor: -1,
		width:      80,
		height:     24,
	}
}

func (m model) Init() tea.Cmd {
	return tea.Batch(
		tea.Tick(time.Millisecond*100, func(t time.Time) tea.Msg { return tickListMsg{} }),
		tea.Tick(time.Millisecond*200, func(t time.Time) tea.Msg { return tickDetailMsg{} }),
		m.spinner.Tick,
	)
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var cmds []tea.Cmd

	switch msg := msg.(type) {

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.updateViewportSize()
		return m, nil

	case tickListMsg:
		return m, tea.Batch(
			m.pollList(),
			tea.Tick(time.Second, func(t time.Time) tea.Msg { return tickListMsg{} }),
		)

	case tickDetailMsg:
		return m, tea.Batch(
			m.pollDetail(),
			tea.Tick(2*time.Second, func(t time.Time) tea.Msg { return tickDetailMsg{} }),
		)

	case listResultMsg:
		m.sessions = msg.sessions
		m.connFailures = 0
		// A successful poll means the daemon is reachable, so clear any stale
		// "Daemon not running." error. Leave a still-active transient error
		// (which expires on its own via clearErrMsg) intact.
		if m.errExpiry.IsZero() || time.Now().After(m.errExpiry) {
			m.err = ""
		}
		if m.cursor >= len(m.sessions) {
			m.cursor = len(m.sessions) - 1
		}
		if m.cursor < 0 {
			m.cursor = 0
		}
		return m, nil

	case listErrMsg:
		m.connFailures++
		if time.Since(m.lastStopTime) < 5*time.Second {
			m.sessions = nil
			return m, nil
		}
		if m.connFailures >= 3 {
			m.err = fmt.Sprintf("Daemon not running.\n\nStart a session with: asn start <dir>\nOr start the daemon: asn daemon start\n\n(%v)", msg.err)
		}
		return m, nil

	case detailResultMsg:
		m.detail = msg.detail
		m.messages = msg.messages
		// Update viewport content.
		content := renderMessageContent(m.messages)
		m.viewport.SetContent(content)
		// Scroll to bottom when cursor hasn't changed (new data, same session).
		if m.cursor == m.prevCursor {
			m.viewport.GotoBottom()
		} else {
			m.viewport.GotoTop()
			m.prevCursor = m.cursor
		}
		return m, nil

	case detailErrMsg:
		m.detail = nil
		m.messages = nil
		m.viewport.SetContent(dimStyle.Render("Error loading details"))
		return m, nil

	case sendResultMsg:
		if msg.err != nil {
			m.err = fmt.Sprintf("Send failed: %v", msg.err)
			m.errExpiry = time.Now().Add(3 * time.Second)
			cmds = append(cmds, tea.Tick(3*time.Second, func(t time.Time) tea.Msg { return clearErrMsg{} }))
		}
		m.mode = modeNormal
		return m, tea.Batch(cmds...)

	case stopResultMsg:
		if msg.err != nil {
			m.err = fmt.Sprintf("Stop failed: %v", msg.err)
			m.errExpiry = time.Now().Add(3 * time.Second)
			cmds = append(cmds, tea.Tick(3*time.Second, func(t time.Time) tea.Msg { return clearErrMsg{} }))
		} else {
			m.lastStopTime = time.Now()
		}
		m.mode = modeNormal
		return m, tea.Batch(cmds...)

	case openTerminalMsg:
		if msg.err != nil {
			m.err = fmt.Sprintf("Open terminal: %v", msg.err)
			m.errExpiry = time.Now().Add(3 * time.Second)
			cmds = append(cmds, tea.Tick(3*time.Second, func(t time.Time) tea.Msg { return clearErrMsg{} }))
		}
		return m, tea.Batch(cmds...)

	case clearErrMsg:
		if time.Now().After(m.errExpiry) || m.errExpiry.IsZero() {
			m.err = ""
		}
		return m, nil

	case spinner.TickMsg:
		var cmd tea.Cmd
		m.spinner, cmd = m.spinner.Update(msg)
		return m, cmd

	case tea.KeyMsg:
		return m.handleKey(msg)
	}

	return m, nil
}

func (m model) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch m.mode {

	case modeSend:
		switch msg.String() {
		case "esc":
			m.mode = modeNormal
			return m, nil
		case "enter":
			if m.cursor < len(m.sessions) {
				text := m.sendInput.Value()
				key := m.sessions[m.cursor].SessionKey
				m.sendInput.Reset()
				return m, m.doSend(key, text)
			}
			m.mode = modeNormal
			return m, nil
		default:
			var cmd tea.Cmd
			m.sendInput, cmd = m.sendInput.Update(msg)
			return m, cmd
		}

	case modeStopConfirm:
		switch msg.String() {
		case "y":
			if m.cursor < len(m.sessions) {
				key := m.sessions[m.cursor].SessionKey
				return m, m.doStop(key)
			}
			m.mode = modeNormal
			return m, nil
		case "n", "esc":
			m.mode = modeNormal
			return m, nil
		}
		return m, nil

	default: // modeNormal
		switch msg.String() {
		case "q", "ctrl+c":
			return m, tea.Quit
		case "tab":
			if m.focused == focusList {
				m.focused = focusDetail
			} else {
				m.focused = focusList
			}
			return m, nil
		case "j", "down":
			if m.focused == focusDetail {
				var cmd tea.Cmd
				m.viewport, cmd = m.viewport.Update(msg)
				return m, cmd
			}
			if m.cursor < len(m.sessions)-1 {
				m.cursor++
			}
			return m, nil
		case "k", "up":
			if m.focused == focusDetail {
				var cmd tea.Cmd
				m.viewport, cmd = m.viewport.Update(msg)
				return m, cmd
			}
			if m.cursor > 0 {
				m.cursor--
			}
			return m, nil
		case "pgdown", "ctrl+d":
			if m.focused == focusDetail {
				var cmd tea.Cmd
				m.viewport, cmd = m.viewport.Update(msg)
				return m, cmd
			}
			return m, nil
		case "pgup", "ctrl+u":
			if m.focused == focusDetail {
				var cmd tea.Cmd
				m.viewport, cmd = m.viewport.Update(msg)
				return m, cmd
			}
			return m, nil
		case "o":
			if len(m.sessions) > 0 && m.cursor < len(m.sessions) {
				tmuxName := m.sessions[m.cursor].TmuxName
				return m, m.doOpenTerminal(tmuxName)
			}
			return m, nil
		case "s":
			if len(m.sessions) > 0 && m.cursor < len(m.sessions) {
				if m.sessions[m.cursor].Status == nil {
					m.err = "Cannot send to ended session"
					m.errExpiry = time.Now().Add(3 * time.Second)
					return m, tea.Tick(3*time.Second, func(t time.Time) tea.Msg { return clearErrMsg{} })
				}
				m.mode = modeSend
				m.sendInput.Reset()
				return m, m.sendInput.Focus()
			}
			return m, nil
		case "x":
			if len(m.sessions) > 0 && m.cursor < len(m.sessions) {
				m.mode = modeStopConfirm
			}
			return m, nil
		case "r":
			m.connFailures = 0
			m.err = ""
			return m, m.pollList()
		}
	}
	return m, nil
}

func (m model) View() tea.View {
	var content string

	if m.width < 80 || m.height < 24 {
		content = renderTooSmall()
	} else if m.connFailures >= 3 && m.err != "" {
		content = renderError(m.err, m.width, m.height)
	} else {
		var b strings.Builder

		// Header.
		b.WriteString(renderHeader(m.width))
		b.WriteByte('\n')

		// Calculate space.
		listHeight := len(m.sessions) + 2
		if listHeight < 3 {
			listHeight = 3
		}
		maxListHeight := m.height / 2
		if listHeight > maxListHeight {
			listHeight = maxListHeight
		}
		detailHeight := m.height - listHeight - 4

		// List pane.
		listContent := renderList(m.sessions, m.cursor, m.width-4, listHeight)
		listBorder := inactiveBorder
		if m.focused == focusList {
			listBorder = activeBorder
		}
		b.WriteString(listBorder.Width(m.width - 4).Render(listContent))
		b.WriteByte('\n')

		// Detail pane.
		var detailContent string
		if m.detail != nil {
			statusLine := renderDetailStatus(m.detail)
			m.updateViewportSize()
			vpHeight := detailHeight - 4 // borders + status line + padding
			if vpHeight < 3 {
				vpHeight = 3
			}
			m.viewport.SetHeight(vpHeight)
			m.viewport.SetWidth(m.width - 8)
			detailContent = statusLine + "\n" + m.viewport.View()
		} else {
			detailContent = dimStyle.Render("Select a session to view details")
		}
		detailBorderStyle := inactiveBorder
		if m.focused == focusDetail {
			detailBorderStyle = activeBorder
		}
		b.WriteString(detailBorderStyle.Width(m.width - 4).Render(detailContent))

		// Inline error.
		if m.err != "" && m.connFailures < 3 {
			b.WriteByte('\n')
			b.WriteString(errorStyle.Render(m.err))
		}

		content = b.String()

		// Modal overlay.
		switch m.mode {
		case modeSend:
			if m.cursor < len(m.sessions) {
				modal := renderSendModal(m.sendInput.View(), m.sessions[m.cursor].SessionKey, m.width)
				content = placeModal(content, modal, m.width, m.height)
			}
		case modeStopConfirm:
			if m.cursor < len(m.sessions) {
				modal := renderStopConfirm(m.sessions[m.cursor].SessionKey, m.width)
				content = placeModal(content, modal, m.width, m.height)
			}
		}
	}

	v := tea.NewView(content)
	v.AltScreen = true
	return v
}

func placeModal(background string, modal string, width int, height int) string {
	return lipgloss.Place(width, height, lipgloss.Center, lipgloss.Center, modal)
}

func (m *model) updateViewportSize() {
	listHeight := len(m.sessions) + 2
	if listHeight < 3 {
		listHeight = 3
	}
	maxListHeight := m.height / 2
	if listHeight > maxListHeight {
		listHeight = maxListHeight
	}
	detailHeight := m.height - listHeight - 4
	vpHeight := detailHeight - 4
	if vpHeight < 3 {
		vpHeight = 3
	}
	m.viewport.SetHeight(vpHeight)
	m.viewport.SetWidth(m.width - 8)
}

// Commands.

func (m model) pollList() tea.Cmd {
	return func() tea.Msg {
		sessions, err := m.client.List()
		if err != nil {
			return listErrMsg{err: err}
		}
		return listResultMsg{sessions: sessions}
	}
}

func (m model) pollDetail() tea.Cmd {
	return func() tea.Msg {
		if len(m.sessions) == 0 || m.cursor >= len(m.sessions) {
			return detailResultMsg{}
		}
		key := m.sessions[m.cursor].SessionKey

		detail, err := m.client.Status(key)
		if err != nil {
			return detailErrMsg{err: err}
		}

		messages, _ := m.client.Messages(key, 1)

		return detailResultMsg{
			detail:   detail,
			messages: messages,
		}
	}
}

func (m model) doSend(key string, text string) tea.Cmd {
	return func() tea.Msg {
		err := m.client.Send(key, text)
		return sendResultMsg{err: err}
	}
}

func (m model) doStop(key string) tea.Cmd {
	return func() tea.Msg {
		err := m.client.Stop(key)
		return stopResultMsg{err: err}
	}
}

func (m model) doOpenTerminal(tmuxName string) tea.Cmd {
	return func() tea.Msg {
		err := openTerminalWithTmux(tmuxName)
		return openTerminalMsg{err: err}
	}
}
