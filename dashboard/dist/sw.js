/**
 * sw.js — Energy Tracker Service Worker
 * オフライン対応 + プッシュ通知（リカバリースコア）
 */

const CACHE_NAME = "energy-tracker-v2";
const STATIC_CACHE = "energy-tracker-static-v2";

// ── Install: ルートのみキャッシュ（Vite ビルドでファイル名がハッシュ化されるため最小限に）
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(["/"])).catch(() => {})
  );
  self.skipWaiting();
});

// ── Activate: 古いキャッシュを削除
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME && k !== STATIC_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── Fetch: Network-first (API) / Cache-first (静的アセット) / Navigate fallback
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // API / auth リクエストは Network-first（オフライン時はエラーレスポンス）
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/auth/")) {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(
          JSON.stringify({ detail: "オフラインです。接続を確認してください。" }),
          { status: 503, headers: { "Content-Type": "application/json" } }
        )
      )
    );
    return;
  }

  // ナビゲーションリクエスト（SPA ルーティング）: キャッシュの / にフォールバック
  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(() =>
        caches.match("/").then((r) => r || fetch("/"))
      )
    );
    return;
  }

  // 静的アセット: Cache-first、キャッシュになければネットワークから取得してキャッシュ
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((resp) => {
        if (!resp || resp.status !== 200 || resp.type !== "basic") return resp;
        const clone = resp.clone();
        caches.open(STATIC_CACHE).then((cache) => cache.put(event.request, clone));
        return resp;
      }).catch(() => cached || new Response("", { status: 408 }));
    })
  );
});

// ── Push: リカバリースコア通知
self.addEventListener("push", (event) => {
  let data = { title: "Energy Tracker", body: "今日のリカバリースコアを確認してください" };
  if (event.data) {
    try { data = event.data.json(); } catch (_) { data.body = event.data.text(); }
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Energy Tracker", {
      body:     data.body || "コンディションを確認してください",
      icon:     "/favicon.ico",
      badge:    "/favicon.ico",
      tag:      "recovery-score",
      renotify: true,
      data:     { url: "/?tab=dashboard" },
    })
  );
});

// ── Notification click: アプリを開く
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = event.notification.data?.url || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
      const existing = cs.find((c) => c.url.includes(self.location.origin));
      if (existing) return existing.focus();
      return clients.openWindow(targetUrl);
    })
  );
});
