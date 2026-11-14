/* Сервис-воркер «Мой дневник»: показывает уведомления, которые прислал роутер (веб-пуш), и открывает дневник по нажатию.
   Ничего не кеширует и в сети ничего не перехватывает: страница работает как раньше. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { body: event.data ? event.data.text() : "" }; }
  // Каждое сообщение обязано показать уведомление (так требуют браузеры, особенно Safari на iPhone), даже если текст не разобрался.
  event.waitUntil(self.registration.showNotification(d.title || "Мой дневник", {
    body: d.body || "В дневнике что-то новое.",
    tag: d.tag || undefined,
    icon: "icon.png",
    badge: "icon.png",
    lang: "ru",
    data: { url: d.url || "./" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "./";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    for (const w of windows) if ("focus" in w) return w.focus();       // дневник уже открыт: просто показываем его
    return self.clients.openWindow ? self.clients.openWindow(url) : undefined;
  }));
});
