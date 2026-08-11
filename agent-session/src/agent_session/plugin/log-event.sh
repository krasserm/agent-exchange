#!/bin/bash
# Logs coding-agent hook events as JSON lines to an events.jsonl file.
# Shared by all agents (claude, codex, ...); their hooks fire this script
# with a hook payload (JSON) on stdin and the event name as $1.
#
# Usage: log-event.sh <event_name>
#
# Output: Appends one JSON line per event to events.jsonl.
#   Only writes when AGENT_SESSION_KEY is set (managed by agent-session).

# Only log when running under agent-session.
[ -z "$AGENT_SESSION_KEY" ] && exit 0

EVENT="$1"
INPUT=$(cat)

# Extract fields from the hook payload (provided by the agent via stdin).
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // "n/a"')
AGENT_ID=$(echo "$INPUT" | jq -r '.agent_id // "n/a"')
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // "n/a"')
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path // ""')

# In-flight background work at event time. Claude Code (>= 2.1.145) sends
# these on Stop/SubagentStop; older versions and codex omit them, so both
# counts default to 0 and status mapping degrades to the pre-field behavior.
BG_TASKS=$(echo "$INPUT" | jq -r '(.background_tasks // []) | length')
SESSION_CRONS=$(echo "$INPUT" | jq -r '(.session_crons // []) | length')
# A jq failure (malformed payload) yields empty strings, which would make the
# --argjson calls below fail and silently drop the whole event.
[ -n "$BG_TASKS" ] || BG_TASKS=0
[ -n "$SESSION_CRONS" ] || SESSION_CRONS=0

# Map event names to a simplified status for downstream consumers. Covers
# both Claude Code and codex event sets (codex has no SessionEnd).
case "$EVENT" in
  SessionEnd)
    STATUS="ended"
    ;;
  Stop|StopFailure)
    # The turn ended, but backgrounded work (background shells, subagents,
    # workflows) may still be running -- that is not the same as idle.
    if [ "$BG_TASKS" -gt 0 ]; then
      STATUS="backgrounded"
    else
      STATUS="idle"
    fi
    ;;
  TeammateIdle|TaskCompleted|SessionStart)
    STATUS="idle"
    ;;
  Elicitation|PermissionRequest)
    STATUS="waiting_for_input"
    ;;
  *)
    STATUS="working"
    ;;
esac

# Build the base JSON log entry.
LOG_ENTRY=$(jq -nc \
  --arg ts "$(date +%Y-%m-%dT%H:%M:%S)" \
  --arg event "$EVENT" \
  --arg status "$STATUS" \
  --arg session_id "$SESSION_ID" \
  --arg agent_id "$AGENT_ID" \
  --arg agent_type "$AGENT_TYPE" \
  --argjson background_tasks "$BG_TASKS" \
  --argjson session_crons "$SESSION_CRONS" \
  '{timestamp: $ts, event: $event, status: $status, session_id: $session_id, agent_id: $agent_id, agent_type: $agent_type, background_tasks: $background_tasks, session_crons: $session_crons}')

# Attach the transcript path when the agent reports one.
[ -n "$TRANSCRIPT" ] && LOG_ENTRY=$(echo "$LOG_ENTRY" | jq -c --arg p "$TRANSCRIPT" '. + {transcript_path: $p}')

# Attach full messages for specific events.
if [ "$EVENT" = "Stop" ]; then
  LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message // ""')
  [ -n "$LAST_MSG" ] && LOG_ENTRY=$(echo "$LOG_ENTRY" | jq -c --arg msg "$LAST_MSG" '. + {assistant_message: $msg}')
elif [ "$EVENT" = "UserPromptSubmit" ]; then
  PROMPT=$(echo "$INPUT" | jq -r '.prompt // ""')
  [ -n "$PROMPT" ] && LOG_ENTRY=$(echo "$LOG_ENTRY" | jq -c --arg msg "$PROMPT" '. + {user_message: $msg}')
fi

EVENTS_DIR="${ASN_HOME:-$HOME/.agent-session}/sessions/$AGENT_SESSION_KEY"
mkdir -p "$EVENTS_DIR"
echo "$LOG_ENTRY" >> "$EVENTS_DIR/events.jsonl"
