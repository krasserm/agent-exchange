package main

import (
	"fmt"

	"charm.land/lipgloss/v2"
)

var (
	// Status colors.
	statusIdle         = lipgloss.NewStyle().Foreground(lipgloss.Color("2")) // green
	statusWorking      = lipgloss.NewStyle().Foreground(lipgloss.Color("3")) // yellow
	statusWaiting      = lipgloss.NewStyle().Foreground(lipgloss.Color("1")) // red
	statusBackgrounded = lipgloss.NewStyle().Foreground(lipgloss.Color("5")) // magenta/violet = alive, paused on background work
	statusDisconnected = lipgloss.NewStyle().Foreground(lipgloss.Color("9")) // bright red/rose = proxy lost, session may still be alive
	statusEnded        = lipgloss.NewStyle().Foreground(lipgloss.Color("8")) // gray

	// List.
	selectedRow = lipgloss.NewStyle().Bold(true)
	normalRow   = lipgloss.NewStyle()
	cursor      = lipgloss.NewStyle().Foreground(lipgloss.Color("6")) // cyan

	// Header / footer.
	headerStyle = lipgloss.NewStyle().Bold(true)
	helpStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("8"))

	// Pane borders (focus indicator).
	activeBorder = lipgloss.NewStyle().
			Border(lipgloss.DoubleBorder()).
			BorderForeground(lipgloss.Color("6")).
			Padding(0, 1)

	inactiveBorder = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("8")).
			Padding(0, 1)

	// Send modal.
	modalStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("6")).
			Padding(0, 1)

	// Errors.
	errorStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("1"))
	dimStyle     = lipgloss.NewStyle().Foreground(lipgloss.Color("8"))
)

func styleStatus(status *string) lipgloss.Style {
	if status == nil {
		return statusEnded
	}
	switch *status {
	case "idle":
		return statusIdle
	case "working":
		return statusWorking
	case "waiting":
		return statusWaiting
	case "backgrounded":
		return statusBackgrounded
	case "disconnected":
		return statusDisconnected
	default:
		return statusEnded
	}
}

func statusText(status *string) string {
	if status == nil {
		return "ENDED"
	}
	switch *status {
	case "idle":
		return "IDLE"
	case "working":
		return "WORKING"
	case "waiting":
		return "WAITING"
	case "backgrounded":
		return "BACKGROUNDED"
	case "disconnected":
		return "DISCONNECTED"
	default:
		return "UNKNOWN"
	}
}

// statusLabel appends the pending background-work count (background_tasks +
// session_crons) to the status text when it is nonzero, e.g. "BACKGROUNDED (1)".
func statusLabel(status *string, backgroundCount int) string {
	label := statusText(status)
	if backgroundCount > 0 {
		return fmt.Sprintf("%s (%d)", label, backgroundCount)
	}
	return label
}
