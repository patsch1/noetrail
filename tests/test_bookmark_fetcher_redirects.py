"""Redirect handling of the bookmark fetcher against a real loopback server.

Every test starts an ``http.server`` and drives the production opener through
``DestinationGuard(test_targets=...)``, so the redirect policy, the redirect
budget and the address pinning are exercised as shipped. The server records
every request it receives; a blocked redirect must leave that record at the
single request that produced the redirect.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import unittest
from unittest import mock

from noetrail import bookmark_fetcher as FETCHER

PAGE = (
    b"<html lang='en'><head><title>Target page</title></head>"
    b"<body>body must not be returned</body></html>"
)
TEST_HOST = "fetch-test.example"
OTHER_HOST = "other-test.example"


def fake_dns(answers: dict[str, object]) -> object:
    """Patch resolution for the listed hostnames only.

    ``socket.getaddrinfo`` is also what the loopback connection itself uses, so
    a blanket patch would break the test server rather than the test host.
    """
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host: object, port: object, *args: object, **kwargs: object) -> object:
        answer = answers.get(str(host))
        if isinstance(answer, Exception):
            raise answer
        if answer is not None:
            return answer
        return real_getaddrinfo(host, port, *args, **kwargs)  # type: ignore[arg-type]

    return mock.patch.object(FETCHER.socket, "getaddrinfo", resolve)


class RouteHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        server: RouteServer = self.server  # type: ignore[assignment]
        server.requests.append(self.path)
        server.host_headers.append(self.headers.get("Host", ""))
        status, headers, body = server.routes.get(
            self.path,
            (404, {}, b"missing"),
        )
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class RouteServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), RouteHandler)
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[str] = []
        self.host_headers: list[str] = []


class RedirectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = RouteServer()
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def guard(self, *hosts: str) -> FETCHER.DestinationGuard:
        target = ("127.0.0.1", self.port)
        return FETCHER.DestinationGuard(
            test_targets={host: target for host in hosts or (TEST_HOST,)}
        )

    def route(
        self,
        path: str,
        status: int,
        headers: dict[str, str],
        body: bytes = b"",
    ) -> None:
        self.server.routes[path] = (status, headers, body)

    def redirect(self, path: str, location: str, status: int = 302) -> None:
        self.route(path, status, {"Location": location})

    def page(self, path: str, body: bytes = PAGE) -> None:
        self.route(path, 200, {"Content-Type": "text/html; charset=utf-8"}, body)

    def fetch(
        self,
        path: str = "/start",
        *,
        guard: FETCHER.DestinationGuard | None = None,
        host: str = TEST_HOST,
    ) -> dict[str, object]:
        return FETCHER.fetch_bookmark(
            f"http://{host}{path}",
            timeout_seconds=5,
            max_response_bytes=100_000,
            guard=guard if guard is not None else self.guard(),
        )

    def assertBlocked(self, result: dict[str, object], warning: str) -> None:
        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "blocked")
        self.assertEqual(result["warnings"], [warning])


class BlockedRedirectTargetTest(RedirectTestCase):
    def test_redirect_to_loopback_is_blocked_before_a_second_request(self) -> None:
        self.redirect("/start", f"http://127.0.0.1:{self.port}/secret")
        self.page("/secret")

        self.assertBlocked(self.fetch(), "invalid_redirect_blocked")
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_loopback_on_the_default_port_is_blocked(self) -> None:
        self.redirect("/start", "http://127.0.0.1/secret")

        self.assertBlocked(self.fetch(), "non_public_destination_blocked")
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_cloud_metadata_service_is_blocked(self) -> None:
        self.redirect("/start", "http://169.254.169.254/latest/meta-data/")

        self.assertBlocked(self.fetch(), "non_public_destination_blocked")
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_private_network_is_blocked(self) -> None:
        self.redirect("/start", "http://10.11.12.13/admin")

        self.assertBlocked(self.fetch(), "non_public_destination_blocked")
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_nat64_metadata_address_is_blocked(self) -> None:
        self.redirect("/start", "http://[64:ff9b::a9fe:a9fe]/latest/meta-data/")

        self.assertBlocked(self.fetch(), "non_public_destination_blocked")
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_a_host_resolving_to_a_private_address_is_blocked(self) -> None:
        self.redirect("/start", "http://internal.example/admin")
        answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 80))]

        with fake_dns({"internal.example": answer}):
            result = self.fetch()

        self.assertBlocked(result, "non_public_destination_blocked")
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_an_unresolvable_host_fails_closed(self) -> None:
        self.redirect("/start", "http://internal.example/admin")

        with fake_dns({"internal.example": socket.gaierror("no such host")}):
            result = self.fetch()

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "failed")
        self.assertEqual(result["warnings"], ["dns_resolution_failed"])
        self.assertEqual(self.server.requests, ["/start"])


class RejectedRedirectSchemeTest(RedirectTestCase):
    def test_redirect_to_file_scheme_is_blocked(self) -> None:
        self.redirect("/start", "file:///etc/passwd")

        self.assertBlocked(self.fetch(), "invalid_redirect_blocked")
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_ftp_scheme_is_blocked(self) -> None:
        # urllib itself would still follow ftp://.
        self.redirect("/start", "ftp://ftp.example.com/pub/secret")

        self.assertBlocked(self.fetch(), "invalid_redirect_blocked")
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_gopher_scheme_is_blocked(self) -> None:
        self.redirect("/start", "gopher://example.com:70/1")

        self.assertBlocked(self.fetch(), "invalid_redirect_blocked")
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_an_unparsable_url_is_blocked(self) -> None:
        # An unterminated IPv6 bracket makes urlsplit raise instead of
        # returning a scheme, which must not escape as an unhandled error.
        self.redirect("/start", "http://[::1/secret")

        self.assertBlocked(self.fetch(), "invalid_redirect_blocked")
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_data_scheme_is_blocked(self) -> None:
        self.redirect("/start", "data:text/html,<title>inline</title>")

        self.assertBlocked(self.fetch(), "invalid_redirect_blocked")
        self.assertEqual(self.server.requests, ["/start"])


class RejectedRedirectUrlShapeTest(RedirectTestCase):
    def test_redirect_with_embedded_credentials_is_blocked(self) -> None:
        self.redirect("/start", f"http://user:pass@{OTHER_HOST}/page")
        self.page("/page")

        self.assertBlocked(
            self.fetch(guard=self.guard(TEST_HOST, OTHER_HOST)),
            "invalid_redirect_blocked",
        )
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_a_non_standard_port_is_blocked(self) -> None:
        self.redirect("/start", f"http://{OTHER_HOST}:8443/page")
        self.page("/page")

        self.assertBlocked(
            self.fetch(guard=self.guard(TEST_HOST, OTHER_HOST)),
            "invalid_redirect_blocked",
        )
        self.assertEqual(self.server.requests, ["/start"])

    def test_redirect_to_an_unpinned_public_host_is_not_reached(self) -> None:
        # The guard trusts only the test host, so the redirect target has to go
        # through real resolution and the address policy.
        self.redirect("/start", "http://192.0.2.10/page")

        self.assertBlocked(self.fetch(), "non_public_destination_blocked")
        self.assertEqual(self.server.requests, ["/start"])


class RedirectBudgetTest(RedirectTestCase):
    def test_chain_longer_than_the_budget_fails_with_too_many_redirects(self) -> None:
        for hop in range(FETCHER.MAX_REDIRECTS + 2):
            self.redirect(f"/hop{hop}", f"/hop{hop + 1}")
        self.page(f"/hop{FETCHER.MAX_REDIRECTS + 2}")

        result = self.fetch("/hop0")

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "failed")
        self.assertEqual(result["warnings"], ["too_many_redirects"])
        # The budget allows MAX_REDIRECTS hops, so the request that returns the
        # rejected redirect is the last one that leaves the process.
        self.assertEqual(
            self.server.requests,
            [f"/hop{hop}" for hop in range(FETCHER.MAX_REDIRECTS + 1)],
        )

    def test_chain_at_the_budget_still_succeeds(self) -> None:
        for hop in range(FETCHER.MAX_REDIRECTS):
            self.redirect(f"/hop{hop}", f"/hop{hop + 1}")
        self.page(f"/hop{FETCHER.MAX_REDIRECTS}")

        result = self.fetch("/hop0")

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "complete")
        self.assertEqual(
            bookmark["url"],
            f"http://{TEST_HOST}/hop{FETCHER.MAX_REDIRECTS}",
        )
        self.assertEqual(len(self.server.requests), FETCHER.MAX_REDIRECTS + 1)


class AllowedRedirectTest(RedirectTestCase):
    def test_relative_redirect_is_resolved_and_revalidated(self) -> None:
        self.redirect("/start", "/other")
        self.page("/other")

        result = self.fetch()

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "complete")
        self.assertEqual(bookmark["url"], f"http://{TEST_HOST}/other")
        self.assertEqual(bookmark["title"], "Target page")
        self.assertEqual(self.server.requests, ["/start", "/other"])
        self.assertNotIn("body must not be returned", str(result))

    def test_absolute_redirect_to_another_allowed_host_is_followed(self) -> None:
        self.redirect("/start", f"http://{OTHER_HOST}/page", status=301)
        self.page("/page")

        result = self.fetch(guard=self.guard(TEST_HOST, OTHER_HOST))

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "complete")
        self.assertEqual(bookmark["url"], f"http://{OTHER_HOST}/page")
        retrieval: dict[str, object] = result["retrieval"]  # type: ignore[assignment]
        self.assertEqual(retrieval["requested_url"], f"http://{TEST_HOST}/start")
        self.assertEqual(retrieval["http_status"], 200)
        self.assertEqual(self.server.requests, ["/start", "/page"])

    def test_response_without_a_location_header_is_returned_as_is(self) -> None:
        self.route("/start", 302, {"Content-Type": "text/html"}, PAGE)

        result = self.fetch()

        retrieval: dict[str, object] = result["retrieval"]  # type: ignore[assignment]
        self.assertEqual(retrieval["http_status"], 302)


class AddressPinningTest(RedirectTestCase):
    def test_connection_uses_the_validated_address_not_a_second_lookup(self) -> None:
        """The validated address is pinned, so DNS cannot change under us.

        ``public_url`` used to return the hostname after validating it, and
        urllib resolved that name again when it opened the socket. A name
        server that answers with a public address first and a private one
        second therefore reached the private host.
        """
        self.page("/start")
        looked_up: list[object] = []
        real_getaddrinfo = socket.getaddrinfo

        def spy(host: object, port: object, *args: object, **kwargs: object) -> object:
            looked_up.append(host)
            return real_getaddrinfo(host, port, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(FETCHER.socket, "getaddrinfo", spy):
            result = self.fetch()

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "complete")
        # Only the pinned literal is looked up; the hostname never reaches the
        # resolver a second time.
        self.assertNotIn(TEST_HOST, looked_up)

    def test_request_keeps_the_hostname_in_the_host_header(self) -> None:
        # Connecting to the pinned address must not turn into a request for the
        # address; virtual hosts and the audit trail depend on the name.
        self.redirect("/start", "/other")
        self.page("/other")

        self.fetch()

        self.assertEqual(self.server.host_headers, [TEST_HOST, TEST_HOST])

    def test_unpinned_host_cannot_be_dialled(self) -> None:
        guard = FETCHER.DestinationGuard()
        connection = FETCHER.PinnedHTTPConnection("example.com", guard=guard)

        with self.assertRaises(FETCHER.FetchIssue) as blocked:
            connection.connect()

        self.assertEqual(blocked.exception.warning, "unvalidated_destination_blocked")

    def test_pinned_rejects_a_url_that_was_never_validated(self) -> None:
        guard = FETCHER.DestinationGuard()

        with self.assertRaises(FETCHER.FetchIssue) as blocked:
            guard.pinned("https://example.com/never-checked")

        self.assertEqual(blocked.exception.warning, "unvalidated_destination_blocked")

    def test_every_validated_address_is_tried_before_failing(self) -> None:
        guard = self.guard()
        guard.check(f"http://{TEST_HOST}/start")
        # A dead first target must not hide a working second one.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        guard._pins[(TEST_HOST, 80)] = (
            ("127.0.0.1", dead_port),
            ("127.0.0.1", self.port),
        )
        self.page("/start")

        result = FETCHER.fetch_bookmark(
            f"http://{TEST_HOST}/start",
            timeout_seconds=5,
            guard=guard,
        )

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "complete")


class ResponseHandlingTest(RedirectTestCase):
    def test_non_html_content_type_returns_metadata_free_fallback(self) -> None:
        self.route("/start", 200, {"Content-Type": "application/pdf"}, b"%PDF-1.7")

        result = self.fetch()

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "partial")
        self.assertEqual(result["warnings"], ["unsupported_content_type"])

    def test_oversized_response_is_truncated_and_flagged(self) -> None:
        self.page("/start", PAGE + b"<p>" + b"x" * 5_000 + b"</p>")

        result = FETCHER.fetch_bookmark(
            f"http://{TEST_HOST}/start",
            timeout_seconds=5,
            max_response_bytes=200,
            guard=self.guard(),
        )

        self.assertIn("response_truncated", result["warnings"])

    def test_unknown_charset_falls_back_to_utf8(self) -> None:
        self.route(
            "/start",
            200,
            {"Content-Type": "text/html; charset=definitely-not-a-charset"},
            "<html><head><title>Grüße</title></head></html>".encode(),
        )

        result = self.fetch()

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["title"], "Grüße")

    def test_http_error_status_becomes_a_failed_result(self) -> None:
        self.route("/start", 500, {"Content-Type": "text/html"}, b"boom")

        result = self.fetch()

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "failed")
        self.assertEqual(result["warnings"], ["http_error"])
        retrieval: dict[str, object] = result["retrieval"]  # type: ignore[assignment]
        self.assertEqual(retrieval["http_status"], 500)

    def test_refused_connection_becomes_a_network_failure(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        guard = FETCHER.DestinationGuard(
            test_targets={TEST_HOST: ("127.0.0.1", dead_port)}
        )

        result = FETCHER.fetch_bookmark(
            f"http://{TEST_HOST}/start",
            timeout_seconds=5,
            guard=guard,
        )

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "failed")
        self.assertEqual(result["warnings"], ["network_fetch_failed"])


class TlsPinningTest(unittest.TestCase):
    """The pinned HTTPS connection must still authenticate the hostname."""

    def setUp(self) -> None:
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.addCleanup(self.listener.close)
        self.client_hello = bytearray()

        def accept_once() -> None:
            try:
                connection, _ = self.listener.accept()
            except OSError:  # pragma: no cover - listener closed first
                return
            with connection:
                self.client_hello.extend(connection.recv(4096))

        self.thread = threading.Thread(target=accept_once, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)

    def test_https_fetch_uses_the_pinned_handler_and_fails_closed(self) -> None:
        # The listener answers no TLS, so the fetch can only fail; what matters
        # is that the HTTPS path is the pinned one and reports a bounded error.
        host = "secure.example"
        guard = FETCHER.DestinationGuard(
            test_targets={host: ("127.0.0.1", self.listener.getsockname()[1])}
        )

        result = FETCHER.fetch_bookmark(
            f"https://{host}/page",
            timeout_seconds=5,
            guard=guard,
        )

        bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
        self.assertEqual(bookmark["fetch_status"], "failed")
        self.assertEqual(result["warnings"], ["network_fetch_failed"])
        self.thread.join(5)
        self.assertIn(host.encode(), bytes(self.client_hello))

    def test_tls_handshake_goes_to_the_pinned_address_and_uses_the_name(self) -> None:
        host = "secure.example"
        guard = FETCHER.DestinationGuard(
            test_targets={host: ("127.0.0.1", self.listener.getsockname()[1])}
        )
        guard.check(f"https://{host}/page")
        connection = FETCHER.PinnedHTTPSConnection(
            host,
            guard=guard,
            context=FETCHER.ssl.create_default_context(),
            timeout=5,
        )

        # The listener speaks no TLS, so the handshake fails after the client
        # has sent its ClientHello to the pinned address.
        with self.assertRaises(OSError):
            connection.connect()
        self.thread.join(5)

        self.assertEqual(connection.host, host)
        self.assertTrue(connection._context.check_hostname)
        # The certificate is checked against the name, not the pinned address,
        # so the name has to be on the wire as SNI.
        self.assertIn(host.encode(), bytes(self.client_hello))
        self.assertNotIn(b"127.0.0.1", bytes(self.client_hello))


if __name__ == "__main__":
    unittest.main()
