import { writable } from "svelte/store";
import type { Session } from "./types";

export const sessions = writable<Session[]>([]);
export const selectedKey = writable<string | null>(null);
