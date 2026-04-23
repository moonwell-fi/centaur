import http.server
import os

PORT = int(os.environ.get("PORT", 3000))
PREFIX = "/apps/usage"

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.dirname(os.path.abspath(__file__)), **kwargs)

    def translate_path(self, path):
        if path.startswith(PREFIX):
            path = path[len(PREFIX):] or "/"
        return super().translate_path(path)

if __name__ == "__main__":
    with http.server.HTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Serving on port {PORT}")
        httpd.serve_forever()
