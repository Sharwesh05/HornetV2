import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import type { LiveEvent } from "./types";

export type LiveStatus = "connecting" | "live" | "closed" | "unauthorized";
const MAX_EVENTS = 300;

export interface LiveState {
  status: LiveStatus;
  events: LiveEvent[]; // newest first
}

/** Exported for tests, which supply events without a WebSocket. */
export const LiveContext = createContext<LiveState>({ status: "closed", events: [] });

/**
 * One WebSocket to /api/v1/ws/events for the whole app. The admin key is sent as the first message (browsers
 * cannot set headers on a WebSocket, and a key in the URL would end up in logs). Reconnects with backoff, except
 * after 4401/4403 (wrong key / foreign origin).
 */
export function LiveProvider({ apiKey, children }: { apiKey: string; children: ReactNode }) {
  const [state, setState] = useState<LiveState>({ status: "connecting", events: [] });
  const stopped = useRef(false);

  useEffect(() => {
    stopped.current = false;
    let socket: WebSocket | null = null;
    let retry = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/api/v1/ws/events`);
      setState((s) => ({ ...s, status: "connecting" }));
      socket.onopen = () => socket?.send(JSON.stringify({ api_key: apiKey }));
      socket.onmessage = (msg) => {
        let data: unknown;
        try {
          data = JSON.parse(String(msg.data));
        } catch {
          return;
        }
        if (!data || typeof data !== "object") return;
        const ev = data as { type?: unknown };
        if (ev.type === "ready") {
          retry = 0;
          setState((s) => ({ ...s, status: "live" }));
        } else if (ev.type === "session_event" || ev.type === "gateway_event") {
          const event = data as LiveEvent;
          setState((s) => ({ ...s, events: [event, ...s.events].slice(0, MAX_EVENTS) }));
        }
      };
      socket.onclose = (e) => {
        if (stopped.current) return;
        if (e.code === 4401 || e.code === 4403) {
          setState((s) => ({ ...s, status: "unauthorized" }));
          return;
        }
        setState((s) => ({ ...s, status: "closed" }));
        timer = setTimeout(connect, Math.min(1000 * 2 ** retry++, 15000));
      };
    };
    connect();
    return () => {
      stopped.current = true;
      if (timer) clearTimeout(timer);
      socket?.close();
    };
  }, [apiKey]);

  return <LiveContext.Provider value={state}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveState {
  return useContext(LiveContext);
}
