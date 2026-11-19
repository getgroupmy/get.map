"""Call a WSGI app once, without a server."""
import io
from urllib.parse import urlsplit


def call(app, url):
    parts = urlsplit(url)
    env = {"REQUEST_METHOD": "GET", "PATH_INFO": parts.path, "QUERY_STRING": parts.query,
           "SERVER_NAME": "test", "SERVER_PORT": "80", "wsgi.input": io.BytesIO(), "wsgi.url_scheme": "http",
           "wsgi.errors": io.StringIO()}
    got = {}

    def start(status, headers, exc_info=None):
        got["status"] = status
    body = b"".join(app(env, start))
    return got["status"], body.decode()
