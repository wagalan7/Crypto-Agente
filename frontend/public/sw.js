/* Service Worker — Crypto AI Agent
 * Recebe push notifications e gerencia clicks.
 * Sem cache offline (PWA leve — apenas vehicle pra notificações).
 *
 * IMPORTANTE: bump CACHE_VERSION sempre que mudar este arquivo OU quando
 * quiser forçar revalidação de assets nos clientes (útil quando o usuário
 * vê a UI desatualizada). O activate handler limpa TODOS os caches antigos.
 */

const CACHE_VERSION = 'crypto-ai-v6';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    Promise.all([
      // Limpa qualquer Cache Storage antigo (caso versões futuras adicionem cache)
      caches.keys().then(keys => Promise.all(
        keys.filter(k => k !== CACHE_VERSION).map(k => caches.delete(k))
      )),
      // Toma controle imediato de todas as abas abertas
      self.clients.claim(),
    ])
  );
});

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    payload = { title: 'Crypto AI', body: event.data ? event.data.text() : 'Nova recomendação' };
  }

  const title = payload.title || 'Crypto AI Agent';
  const options = {
    body: payload.body || 'Nova recomendação disponível',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: payload.tag || 'crypto-ai-rec',
    renotify: true,
    requireInteraction: false,
    vibrate: [200, 100, 200],
    data: payload.data || { url: '/' },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      // Se já tem aba aberta, foca nela
      for (const client of clientList) {
        if ('focus' in client) {
          client.postMessage({ type: 'push-click', data: event.notification.data });
          return client.focus();
        }
      }
      // Caso contrário, abre nova
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});

self.addEventListener('pushsubscriptionchange', (event) => {
  // Se a subscription expirar, browser dispara isto.
  // App.tsx vai re-registrar na próxima abertura.
  console.log('[SW] pushsubscriptionchange', event);
});
