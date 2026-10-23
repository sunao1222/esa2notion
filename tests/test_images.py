import base64
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import requests

from esa2notion.errors import MigrationError
from esa2notion.images import download_image


class Response:
    def __init__(self, *, status=200, headers=None, chunks=(), data=None):
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks
        self.data = data
        self.closed = False
        self.consumed = 0

    def json(self):
        if self.data is None:
            raise ValueError("not JSON")
        return self.data

    def iter_content(self, chunk_size):
        self.chunk_size = chunk_size
        for chunk in self.chunks:
            self.consumed += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.auth = ("unwanted", "credentials")

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def download(self, url="https://img.esa.io/a.png", **kwargs):
        options = dict(esa_team=None, esa_token=None, directory=self.directory, max_bytes=20)
        options.update(kwargs)
        return download_image(url, **options)

    def test_streamed_bytes_mime_parameters_and_credential_isolation(self):
        response = Response(headers={"Content-Type": "image/png; charset=binary"}, chunks=[b"abc", b"", b"def"])
        session = Session(response)
        image = self.download(session=session, esa_token="must-not-forward")
        self.assertEqual(image.path.read_bytes(), b"abcdef")
        self.assertEqual((image.filename, image.content_type, image.size), ("a.png", "image/png", 6))
        self.assertTrue(response.closed)
        self.assertEqual(response.chunk_size, 64 * 1024)
        self.assertIsNone(session.auth)
        request = session.calls[0][2]
        self.assertIsNone(request["headers"]["Authorization"])
        self.assertIsNone(request["headers"]["Notion-Version"])
        self.assertEqual(request["timeout"], (10, 120))
        self.assertTrue(request["stream"])

    def test_public_http_and_cross_origin_redirect_without_api_credentials(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
        )
        requests_seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests_seen.append((self.server.server_port, self.path, self.headers))
                # Some image CDNs reject the generic requests client identity.
                user_agent = self.headers.get("User-Agent", "")
                if not user_agent or user_agent.lower().startswith("python-requests/"):
                    self.send_response(403)
                    self.end_headers()
                    return
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", target + "/redirected.png")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(png)))
                    self.end_headers()
                    self.wfile.write(png)

            def log_message(self, *args):
                pass

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        threads = [Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()

        def stop_servers():
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()

        self.addCleanup(stop_servers)
        origin, target = [f"http://127.0.0.1:{server.server_port}" for server in servers]
        with requests.Session() as session:
            session.headers.update({"Authorization": "Bearer must-not-leak", "Notion-Version": "secret-version"})
            session.auth = ("unwanted", "credentials")
            for path in ("/direct.png", "/redirect"):
                with self.subTest(path=path):
                    image = self.download(origin + path, session=session, max_bytes=len(png))
                    self.assertEqual(image.path.read_bytes(), png)
        self.assertEqual(
            [(port, path) for port, path, _ in requests_seen],
            [(servers[0].server_port, "/direct.png"), (servers[0].server_port, "/redirect"),
             (servers[1].server_port, "/redirected.png")],
        )
        for _, _, headers in requests_seen:
            self.assertIsNone(headers.get("Authorization"))
            self.assertIsNone(headers.get("Notion-Version"))

    def test_secure_signing_matches_original_and_never_sends_token_to_image(self):
        source = "https://files.esa.io/private/a.png"
        signed = "https://storage.example/image.png?signature=super-secret"
        api = Session(Response(data={"signed_urls": [["other", "https://wrong.example/a"], [source, signed]]}))
        image_session = Session(Response(headers={"Content-Type": "image/png"}, chunks=[b"png"]))
        image = self.download(source, esa_team="my/team", esa_token="esa-secret", esa_base_url="http://localhost/esa/v1", esa_session=api, session=image_session)
        self.assertEqual(image.path.read_bytes(), b"png")
        self.assertEqual(api.calls[0][1], "http://localhost/esa/v1/teams/my%2Fteam/signed_urls")
        self.assertEqual(api.calls[0][2]["json"], {"urls": [source], "expires_in": 3600})
        self.assertEqual(api.calls[0][2]["headers"]["Authorization"], "Bearer esa-secret")
        self.assertEqual(image_session.calls[0][1], signed)
        self.assertIsNone(image_session.calls[0][2]["headers"]["Authorization"])

    def test_secure_configuration_and_malformed_signing_fail_before_get(self):
        image_session = Session()
        with self.assertRaisesRegex(MigrationError, "ESA_TOKEN"):
            self.download("https://dl.esa.io/a.png", session=image_session)
        self.assertEqual(image_session.calls, [])
        for data in ({}, {"signed_urls": [["other", "https://x/a"]]}, {"signed_urls": [["https://dl.esa.io/a.png", "relative"]]}):
            with self.subTest(data=data), self.assertRaises(MigrationError):
                self.download("https://dl.esa.io/a.png", esa_team="team", esa_token="token", esa_session=Session(Response(data=data)), session=image_session)
        self.assertEqual(image_session.calls, [])

    def test_declared_limit_rejected_before_stream_or_file_creation(self):
        response = Response(headers={"Content-Type": "image/png", "Content-Length": "21"}, chunks=[b"ignored"])
        with self.assertRaisesRegex(MigrationError, "Content-Length exceeds"):
            self.download(session=Session(response))
        self.assertEqual(response.consumed, 0)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertTrue(response.closed)

    def test_stream_limit_stops_and_removes_partial_file(self):
        response = Response(headers={"Content-Type": "image/png"}, chunks=[b"a" * 12, b"b" * 9, b"not consumed"])
        with self.assertRaisesRegex(MigrationError, "streamed image exceeds"):
            self.download(session=Session(response))
        self.assertEqual(response.consumed, 2)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertTrue(response.closed)

    def test_exact_limit_is_accepted_and_octet_stream_uses_extension(self):
        for mime in ("application/octet-stream", ""):
            with self.subTest(mime=mime):
                image = self.download(session=Session(Response(headers={"Content-Type": mime}, chunks=[b"x" * 20])))
                self.assertEqual(image.size, 20)
                self.assertEqual(image.content_type, "image/png")

    def test_html_unsupported_empty_and_interrupted_stream_are_failures(self):
        for response in (
            Response(headers={"Content-Type": "text/html"}, chunks=[b"login"]),
            Response(headers={"Content-Type": "image/png"}, chunks=[b" <ht", b"ml>login"]),
            Response(headers={"Content-Type": "image/not-supported"}, chunks=[b"x"]),
            Response(headers={"Content-Type": "image/png"}, chunks=[]),
            Response(headers={"Content-Type": "image/png"}, chunks=[b"partial", requests.ConnectionError("secret-url")]),
        ):
            with self.subTest(response=response), self.assertRaises(MigrationError):
                self.download(session=Session(response))
            self.assertTrue(response.closed)
            self.assertEqual(list(self.directory.iterdir()), [])

    def test_signed_download_failure_redacts_target_and_query(self):
        source = "https://files.esa.io/a.png"
        api = Session(Response(data={"signed_urls": [[source, "https://storage.example/secret-path?signature=private"]]}))
        with self.assertRaises(MigrationError) as caught:
            self.download(source, esa_team="team", esa_token="token", esa_session=api,
                          session=Session(Response(status=404, data={"message": "private"})))
        self.assertIn("files.esa.io", str(caught.exception))
        self.assertNotIn("storage.example", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    def test_invalid_urls_never_request_network(self):
        for url in ("relative.png", "data:image/png;base64,eA==", "https://user:password@example.com/a.png"):
            session = Session()
            with self.subTest(url=url), self.assertRaises(MigrationError):
                self.download(url, session=session)
            self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
