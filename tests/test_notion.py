import io
import tempfile
import unittest
from pathlib import Path

import requests

from esa2notion.errors import MigrationError
from esa2notion.http import HTTPClient
from esa2notion.images import DownloadedImage
from esa2notion.notion import NotionClient, PART_SIZE, SINGLE_PART_LIMIT, plain_text, rich_text


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.data = data if data is not None else {}
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.uploads = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if "files" in kwargs:
            stream = kwargs["files"]["file"][1]
            content = stream.read()
            self.uploads.append((kwargs.get("data", {}).get("part_number"), len(content), content.count(b"\0")))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class NotionTests(unittest.TestCase):
    def client(self, session, **kwargs):
        options = dict(base_url="http://localhost/v1", session=session, min_interval=0)
        options.update(kwargs)
        return NotionClient("private-token", **options)

    def image(self, size):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "image.png"
        with path.open("wb") as output:
            output.truncate(size)
        return DownloadedImage(path, "image.png", "image/png", size)

    def upload_session(self, *, parts, final_status="uploaded", limit=5 * 1024**3):
        return Session(
            Response({"bot": {"workspace_limits": {"max_file_upload_size_in_bytes": limit}}}),
            Response({"id": "upload-id", "status": "pending"}),
            *(Response({"status": "pending"}) for _ in range(parts - 1)),
            Response({"status": final_status}),
        )

    def test_rich_text_roundtrip_and_exact_limits(self):
        for length in (0, 1, 2000, 2001, 200000):
            text = "日" * length
            items = rich_text(text)
            self.assertEqual(plain_text(items), text)
            self.assertLessEqual(len(items), 100)
            self.assertTrue(all(len(item["text"]["content"]) <= 2000 for item in items))
        self.assertEqual(plain_text([{"plain_text": "server"}, {"text": {"content": "fallback"}}]), "serverfallback")
        with self.assertRaises(MigrationError):
            rich_text("x" * 200001)

    def test_recursive_blocks_include_second_page_and_nested_images(self):
        session = Session(
            Response({"results": [{"id": "container", "has_children": True}], "has_more": True, "next_cursor": "second"}),
            Response({"results": [{"id": "nested-image", "type": "image"}], "has_more": False}),
            Response({"results": [{"id": "later-image", "type": "image"}], "has_more": False}),
        )
        blocks = list(self.client(session).iter_blocks("page"))
        self.assertEqual([block["id"] for block in blocks], ["container", "nested-image", "later-image"])
        self.assertEqual(session.calls[2][2]["params"]["start_cursor"], "second")
        self.assertIn("/blocks/container/children", session.calls[1][1])

    def test_malformed_pagination_is_not_silently_truncated(self):
        client = self.client(Session(Response({"results": [], "has_more": True, "next_cursor": None})))
        with self.assertRaisesRegex(MigrationError, "pagination cursor"):
            list(client.iter_blocks("page"))

    def test_workspace_limit_is_required_and_cached(self):
        for data in ({}, {"bot": {"workspace_limits": {"max_file_upload_size_in_bytes": True}}}):
            with self.subTest(data=data), self.assertRaises(MigrationError):
                self.client(Session(Response(data))).get_upload_limit()
        session = Session(Response({"bot": {"workspace_limits": {"max_file_upload_size_in_bytes": 5242880}}}))
        client = self.client(session)
        self.assertEqual(client.get_upload_limit(), 5242880)
        self.assertEqual(client.get_upload_limit(), 5242880)
        self.assertEqual(len(session.calls), 1)

    def test_twenty_mib_is_single_part_and_bytes_are_sent(self):
        session = self.upload_session(parts=1)
        result = self.client(session).upload_image(self.image(SINGLE_PART_LIMIT))
        self.assertEqual(result, "upload-id")
        self.assertEqual(session.calls[1][2]["json"]["mode"], "single_part")
        self.assertEqual(session.uploads, [(None, SINGLE_PART_LIMIT, SINGLE_PART_LIMIT)])
        self.assertFalse(any(url.endswith("/complete") for _, url, _ in session.calls))
        self.assertIsNone(session.calls[2][2]["headers"]["Content-Type"])

    def test_above_twenty_mib_has_ten_mib_parts_and_short_final_part(self):
        size = SINGLE_PART_LIMIT + 7
        session = self.upload_session(parts=4)
        result = self.client(session).upload_image(self.image(size))
        self.assertEqual(result, "upload-id")
        created = session.calls[1][2]["json"]
        self.assertEqual((created["mode"], created["number_of_parts"]), ("multi_part", 3))
        self.assertEqual(session.uploads, [("1", PART_SIZE, PART_SIZE), ("2", PART_SIZE, PART_SIZE), ("3", 7, 7)])
        self.assertTrue(session.calls[-1][1].endswith("/complete"))

    def test_pending_upload_is_failure_for_both_modes(self):
        for size, requests_after_create in ((1, 1), (SINGLE_PART_LIMIT + 1, 4)):
            session = self.upload_session(parts=requests_after_create, final_status="pending")
            with self.subTest(size=size), self.assertRaisesRegex(MigrationError, "uploaded status"):
                self.client(session).upload_image(self.image(size))

    def test_workspace_limit_prevents_upload_creation(self):
        session = Session(Response({"bot": {"workspace_limits": {"max_file_upload_size_in_bytes": 5}}}))
        with self.assertRaisesRegex(MigrationError, "workspace upload limit"):
            self.client(session).upload_image(self.image(6))
        self.assertEqual(len(session.calls), 1)

    def test_notion_rate_limit_applies_between_all_requests_and_retries(self):
        clock = Clock()
        session = Session(Response({}, 429, {"Retry-After": "0"}), Response({}), Response({}))
        client = self.client(session, min_interval=0.35, sleep=clock.sleep, monotonic=clock.monotonic)
        client.get_page("page")
        client.get_block("block")
        self.assertEqual(clock.waits, [0.0, 0.35, 0.35])


class HTTPTests(unittest.TestCase):
    def transport(self, session, **kwargs):
        options = dict(session=session, sleep=lambda _: None, jitter=lambda: 0, secrets=("private-token",))
        options.update(kwargs)
        return HTTPClient(**options)

    def test_retries_rewind_multipart_to_original_position(self):
        session = Session(Response({}, 429, {"Retry-After": "2"}), Response({"status": "uploaded"}))
        waits = []
        transport = self.transport(session, sleep=waits.append)
        source = io.BytesIO(b"prefix\0\0\0")
        source.seek(6)
        transport.request("POST", "http://local/send", files={"file": ("image.png", source, "image/png")})
        self.assertEqual(session.uploads, [(None, 3, 3), (None, 3, 3)])
        self.assertEqual(waits, [2.0])

    def test_retry_after_http_date(self):
        waits = []
        session = Session(Response({}, 529, {"Retry-After": "Thu, 01 Jan 1970 00:00:05 GMT"}), Response({}))
        self.transport(session, sleep=waits.append, wall_time=lambda: 2).request("GET", "http://local")
        self.assertEqual(waits, [3.0])

    def test_six_total_attempts_with_exponential_fallback(self):
        waits = []
        responses = [Response({}, 529) for _ in range(6)]
        session = Session(*responses)
        with self.assertRaises(MigrationError):
            self.transport(session, sleep=waits.append).request("POST", "http://local/pages")
        self.assertEqual(waits, [1, 2, 4, 8, 16])
        self.assertEqual(len(session.calls), 6)
        self.assertTrue(all(response.closed for response in responses))

    def test_blocked_429_never_retries(self):
        session = Session(Response({"code": "public_api_request_blocked"}, 429))
        waits = []
        with self.assertRaisesRegex(MigrationError, "public_api_request_blocked"):
            self.transport(session, sleep=waits.append).request("POST", "http://local/pages")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(waits, [])

    def test_network_failure_and_other_5xx_never_retry_writes(self):
        for result in (requests.Timeout("private-token"), requests.ConnectionError("secret"), Response({}, 500), Response({}, 503)):
            session = Session(result)
            with self.subTest(result=result), self.assertRaises(MigrationError) as caught:
                self.transport(session).request("POST", "http://local/pages?signature=private")
            self.assertEqual(len(session.calls), 1)
            self.assertNotIn("private-token", str(caught.exception))
            self.assertNotIn("signature=", str(caught.exception))

    def test_error_redacts_secrets_but_preserves_committed_resource(self):
        session = Session(Response({
            "code": "validation_error",
            "message": "bad private-token https://image.example/a.png?signature=secret",
            "additional_data": {"committed_resource_id": "12345678-1234-5678-1234-567812345678", "secret": "hidden"},
        }, 400))
        with self.assertRaises(MigrationError) as caught:
            self.transport(session).request("POST", "http://local/pages")
        message = str(caught.exception)
        self.assertIn("validation_error", message)
        self.assertIn("https://www.notion.so/12345678123456781234567812345678", message)
        for secret in ("private-token", "signature=", "hidden"):
            self.assertNotIn(secret, message)


if __name__ == "__main__":
    unittest.main()
