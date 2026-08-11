import type { Message, Session, SessionDetail } from "./types";

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText}`);
  }
  return (await resp.json()) as T;
}

export async function listSessions(): Promise<Session[]> {
  try {
    return await json<Session[]>(await fetch("/api/sessions"));
  } catch {
    return [];
  }
}

export async function getSession(key: string): Promise<SessionDetail> {
  return json<SessionDetail>(await fetch(`/api/sessions/${key}`));
}

export async function stopSession(key: string): Promise<void> {
  await fetch(`/api/sessions/${key}/stop`, { method: "POST" });
}

export async function getMessages(key: string, last = 5): Promise<Message[]> {
  try {
    return await json<Message[]>(
      await fetch(`/api/sessions/${key}/messages?last=${last}`),
    );
  } catch {
    return [];
  }
}

export async function sendMessage(key: string, message: string): Promise<void> {
  await fetch(`/api/sessions/${key}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
}

export function terminalSocketUrl(key: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/${key}/terminal`;
}
