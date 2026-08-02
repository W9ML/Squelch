"use client";

import { useEffect, useRef } from "react";
import type { WsEvent } from "./types";

/**
 * Live-feed WebSocket with exponential-backoff reconnect, ported from app.js.
 * In production the socket is same-origin (`/ws`); in dev it connects to
 * NEXT_PUBLIC_WS_BASE because Next's dev rewrites don't tunnel WebSockets.
 * Callbacks are held in refs so re-renders don't churn the connection.
 */
export function useWebSocket(
  onEvent: (e: WsEvent) => void,
  onOpen?: () => void,
  onStatus?: (connected: boolean) => void,
): void {
  const cbRef = useRef(onEvent);
  cbRef.current = onEvent;
  const openRef = useRef(onOpen);
  openRef.current = onOpen;
  const statusRef = useRef(onStatus);
  statusRef.current = onStatus;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 1000;
    let closed = false;
    let timer: ReturnType<typeof setTimeout>;

    const connect = () => {
      const base =
        process.env.NEXT_PUBLIC_WS_BASE ||
        `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
      ws = new WebSocket(`${base}/ws`);
      ws.onopen = () => {
        retry = 1000;
        statusRef.current?.(true);
        openRef.current?.();
      };
      ws.onmessage = (ev) => {
        try {
          cbRef.current(JSON.parse(ev.data) as WsEvent);
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        if (closed) return;              // unmounting — don't touch state
        statusRef.current?.(false);      // feed dropped -> "reconnecting"
        timer = setTimeout(connect, retry);
        retry = Math.min(retry * 2, 15000);
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          /* noop */
        }
      };
    };

    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, []);
}
