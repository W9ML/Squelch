/* Squelch service worker — push notifications for watchlist alerts.
 * Kept intentionally tiny: no offline caching (the app needs a live backend),
 * only push + notification handling so alerts arrive with the app closed. */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { body: event.data ? event.data.text() : "" };
  }
  const title = data.title || "Squelch alert";
  const options = {
    body: data.body || "",
    tag: data.tag || "squelch-push",
    icon: "/icons/icon.svg",
    badge: "/icons/icon.svg",
    data: { url: data.url || "/" },
    renotify: true,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((wins) => {
        for (const w of wins) {
          if ("focus" in w) {
            if ("navigate" in w) w.navigate(url).catch(() => {});
            return w.focus();
          }
        }
        if (self.clients.openWindow) return self.clients.openWindow(url);
      }),
  );
});
