export interface Session {
  session_key: string;
  tmux_session_name: string | null;
  claude_start_dir: string;
  /** ssh host for remote sessions; null when the session runs locally. */
  host: string | null;
  status: string | null;
  session_id: string | null;
  project: string;
  background_tasks: number;
  session_crons: number;
}

export interface SessionDetail extends Session {
  claude_project_dir: string | null;
  transcript_path: string | null;
}

export interface Message {
  message: string;
  timestamp: string;
}
