"use client";

/**
 * PWA service-worker registration + web-push subscription. The service worker
 * (public/sw.js) handles `push` events even with the app closed; here we
 * register it, then subscribe the browser to push using the server's VAPID
 * application key and hand the subscription back to the backend.
 */

import { api } from "./api";

function urlB64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export function pushSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    typeof window !== "undefined" &&
    "PushManager" in window
  );
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return null;
  try {
    return await navigator.serviceWorker.register("/sw.js");
  } catch {
    return null;
  }
}

// avoid re-POSTing the same subscription repeatedly within a session
let posted = false;

/** Ensure this browser is subscribed to push and the server has the record.
 *  Assumes notification permission is already granted; returns success. */
export async function subscribeToPush(): Promise<boolean> {
  if (!pushSupported()) return false;
  await registerServiceWorker(); // idempotent
  const reg = await navigator.serviceWorker.ready.catch(() => null);
  if (!reg) return false;
  try {
    const existing = await reg.pushManager.getSubscription();
    if (posted && existing) return true;

    const { key, enabled } = await api.pushVapid();
    if (!enabled || !key) return false;

    const sub =
      existing ||
      (await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(key),
      }));

    const j = sub.toJSON();
    if (!j.endpoint || !j.keys?.p256dh || !j.keys?.auth) return false;
    await api.pushSubscribe({
      endpoint: j.endpoint,
      keys: { p256dh: j.keys.p256dh, auth: j.keys.auth },
    });
    posted = true;
    return true;
  } catch {
    return false;
  }
}

export async function unsubscribeFromPush(): Promise<void> {
  if (!pushSupported()) return;
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await api.pushUnsubscribe(sub.endpoint).catch(() => {});
      await sub.unsubscribe().catch(() => {});
    }
    posted = false;
  } catch {
    /* ignore */
  }
}
