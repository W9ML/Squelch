"use client";

/**
 * Thin wrapper around the Web Notifications API. The watchlist engine already
 * exists server-side; the reason alerts never actually fired is that nothing
 * ever *requested* permission. These helpers centralize that so the bell menu
 * and the star toggles can prompt at the right (user-gesture) moment.
 */

export function notifySupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export function notifyPermission(): NotificationPermission {
  return notifySupported() ? Notification.permission : "denied";
}

/** Must be called from a user gesture (click) to satisfy browser policy. */
export async function requestNotify(): Promise<NotificationPermission> {
  if (!notifySupported()) return "denied";
  try {
    return await Notification.requestPermission();
  } catch {
    return notifyPermission();
  }
}

export function showNotification(title: string, body: string, tag?: string): void {
  if (notifyPermission() !== "granted") return;
  try {
    const n = new Notification(title, { body, tag: tag || "squelch" });
    n.onclick = () => {
      window.focus();
      n.close();
    };
  } catch {
    /* some browsers throw if constructed outside a SW on mobile — ignore */
  }
}
