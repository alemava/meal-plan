#!/usr/bin/env python3
"""Dev server — like http.server but sends Cache-Control: no-cache for sw.js
and manifest.json so iOS Safari picks up SW updates reliably instead of serving
a stale cached version and missing the update prompt."""
import http.server
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

class NoCacheForSW(http.server.SimpleHTTPRequestHandler):
    NO_CACHE_FILES = {'/sw.js', '/manifest.json'}

    def end_headers(self):
        path = self.path.split('?')[0]
        if path in self.NO_CACHE_FILES:
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('Pragma', 'no-cache')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # keep terminal clean

if __name__ == '__main__':
    with http.server.HTTPServer(('', PORT), NoCacheForSW) as httpd:
        print(f'Serving on http://localhost:{PORT}')
        httpd.serve_forever()
