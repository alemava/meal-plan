var CACHE = 'mesa-v27';
var SHELL = [
  './index.html',
  './manifest.json',
  './icon.svg',
  './logo.svg'
];
var SB_HOST = 'vcluruaueetktctdyplh.supabase.co';

self.addEventListener('install', function(e) {
  e.waitUntil(
    caches.open(CACHE).then(function(c) {
      return c.addAll(SHELL);
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function(e) {
  e.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(keys.filter(function(k) { return k !== CACHE; }).map(function(k) { return caches.delete(k); }));
    })
  );
  return self.clients.claim();
});

self.addEventListener('message', function(e) {
  if (!e.data || e.data.type !== 'TIMER_DONE') return;
  var label = e.data.label || 'Timer';
  self.clients.matchAll({ includeUncontrolled: true, type: 'window' }).then(function(clients) {
    var anyVisible = clients.some(function(c) { return c.visibilityState === 'visible'; });
    if (anyVisible) return; // app is in foreground — skip notification
    return self.registration.showNotification('\u23f1 ' + label, {
      body: 'Timer finished!',
      icon: './icon.svg',
      badge: './icon.svg',
      tag: 'timer-' + label,
      renotify: true,
      silent: false
    });
  });
});

self.addEventListener('fetch', function(e) {
  var url = e.request.url;

  // App shell: cache-first
  var isShell = SHELL.some(function(p) { return url.endsWith(p.replace('./', '')); });
  if (isShell) {
    e.respondWith(
      caches.match(e.request).then(function(cached) {
        if (cached) return cached;
        return fetch(e.request).then(function(res) {
          var clone = res.clone();
          caches.open(CACHE).then(function(c) { c.put(e.request, clone); });
          return res;
        });
      })
    );
    return;
  }

  // Supabase GET requests: network-first, fall back to cache by URL
  if (url.indexOf(SB_HOST) !== -1 && e.request.method === 'GET') {
    e.respondWith(
      fetch(e.request).then(function(res) {
        if (res.ok) {
          var clone = res.clone();
          caches.open(CACHE).then(function(c) {
            c.put(new Request(url), clone);
          });
        }
        return res;
      }).catch(function() {
        return caches.match(new Request(url));
      })
    );
    return;
  }
});
