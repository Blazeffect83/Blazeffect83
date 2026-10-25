"use strict";
// Polymath's service worker: it only lets Chrome install the dashboard as an app window. It caches nothing,
// so the brain's data never sits on your PC's disk, and every request goes straight to the Pi.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
