"""Static dev server for the onboarding shell.

Stock `python -m http.server` calls socket.getfqdn() while binding, which can
hang for a long time on some macOS resolvers. This skips that lookup.
"""
import argparse
import functools
import http.server
import pathlib
import socketserver


class Server(http.server.ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5173)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    root = pathlib.Path(__file__).parent
    handler = functools.partial(Handler, directory=str(root))
    with Server((args.host, args.port), handler) as httpd:
        print(f"onboarding shell → http://localhost:{args.port}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
