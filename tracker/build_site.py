#!/usr/bin/env python3
"""Turn web/index.html into the public site: index.html plus a service worker and app manifest so phones keep
the last loaded results for offline viewing. usage: build_site.py OUTDIR"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.abspath(sys.argv[1])
os.makedirs(OUT, exist_ok=True)
page = open(os.path.join(HERE, "web", "index.html")).read()
head = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="robots" content="noindex,nofollow"><meta name="theme-color" content="#13201a">'
        '<link rel="manifest" href="manifest.webmanifest"><link rel="icon" href="icon.svg" type="image/svg+xml">'
        '<link rel="apple-touch-icon" href="icon.svg"><meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-title" content="My Tickets">'
        '<style>:root{color-scheme:light dark;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
        'body{margin:0;font:14px system-ui,-apple-system,"Segoe UI",sans-serif}img{max-width:100%}[hidden]{display:none!important}'
        '#offline{display:none;background:#a87a0c;color:#fff;text-align:center;padding:6px 12px;font:600 13px system-ui,sans-serif}</style>'
        '</head><body><div id="offline">You are offline - showing the results saved on this device.</div>')
tail = ('<script>if("serviceWorker"in navigator)navigator.serviceWorker.register("sw.js");'
        'function o(){document.getElementById("offline").style.display=navigator.onLine?"none":"block"}'
        'addEventListener("online",o);addEventListener("offline",o);o();</script></body></html>\n')
open(os.path.join(OUT, "index.html"), "w").write(head + page + tail)
open(os.path.join(OUT, "sw.js"), "w").write("""const C='tickets-v1';
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(C).then(c=>c.addAll(['./','./index.html','./manifest.webmanifest','./icon.svg'])))});
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
// network first, so an online visit always gets the newest results; the saved copy is used when offline
self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;
  e.respondWith(fetch(e.request).then(r=>{if(r.ok&&new URL(e.request.url).origin===location.origin){const k=r.clone();caches.open(C).then(c=>c.put(e.request,k))}return r})
  .catch(()=>caches.match(e.request,{ignoreSearch:true}).then(r=>r||caches.match('./index.html'))))});
""")
open(os.path.join(OUT, "manifest.webmanifest"), "w").write('{"name":"My Ticket Results","short_name":"My Tickets","start_url":"./","scope":"./","display":"standalone","background_color":"#edf1ef","theme_color":"#13201a","icons":[{"src":"icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any"}]}\n')
open(os.path.join(OUT, "icon.svg"), "w").write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96"><rect width="96" height="96" rx="20" fill="#13201a"/><rect x="18" y="26" width="60" height="44" rx="6" fill="#ffe58a"/><path d="M30 48l9 9 18-20" stroke="#16804a" stroke-width="8" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>\n')
open(os.path.join(OUT, ".nojekyll"), "w").write("")
print("site built in", OUT)
