"""Destination address policy of the bookmark fetcher.

Covers the notations that reach a private destination through an address that
``ipaddress.is_global`` still accepts, and the address pinning that keeps the
validated answer and the connected address the same.
"""

from __future__ import annotations

import ipaddress
import socket
import unittest
from unittest import mock

from noetrail import bookmark_fetcher as FETCHER

# Every entry is one URL notation for a destination that must never be reached.
BLOCKED_URLS: tuple[tuple[str, str], ...] = (
    ("loopback", "http://127.0.0.1/x"),
    ("loopback shorthand", "http://127.1/x"),
    ("link-local metadata", "http://169.254.169.254/latest/meta-data/"),
    ("private class A", "http://10.0.0.1/x"),
    ("private class B", "http://172.16.0.1/x"),
    ("private class C", "http://192.168.1.1/x"),
    ("carrier-grade NAT", "http://100.64.0.1/x"),
    ("unspecified", "http://0.0.0.0/x"),
    ("reserved", "http://240.0.0.1/x"),
    ("multicast base", "http://224.0.0.1/x"),
    ("multicast SSDP", "http://239.255.255.250/x"),
    ("IPv6 loopback", "http://[::1]/x"),
    ("IPv6 unique local", "http://[fd00::1]/x"),
    ("IPv6 link-local", "http://[fe80::1]/x"),
    ("IPv6 multicast", "http://[ff02::1]/x"),
    ("NAT64 metadata", "http://[64:ff9b::a9fe:a9fe]/latest/meta-data/"),
    ("NAT64 metadata dotted", "http://[64:ff9b::169.254.169.254]/x"),
    ("NAT64 loopback", "http://[64:ff9b::7f00:1]/x"),
    ("NAT64 private", "http://[64:ff9b::10.0.0.1]/x"),
    ("NAT64 local-use prefix", "http://[64:ff9b:1::a9fe:a9fe]/x"),
    ("6to4 loopback", "http://[2002:7f00:1::]/x"),
    ("6to4 metadata", "http://[2002:a9fe:a9fe::]/x"),
    ("6to4 private", "http://[2002:c0a8:101::]/x"),
    ("IPv4-mapped metadata", "http://[::ffff:169.254.169.254]/x"),
    ("IPv4-mapped loopback", "http://[::ffff:7f00:1]/x"),
)


def dns_answer(address: str, port: int = 80) -> list[tuple[object, ...]]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]


class BlockedNotationTest(unittest.TestCase):
    def test_every_blocked_notation_fails_closed(self) -> None:
        for label, url in BLOCKED_URLS:
            with self.subTest(notation=label):
                result = FETCHER.fetch_bookmark(url, timeout_seconds=1)
                bookmark: dict[str, object] = result["bookmark"]  # type: ignore[assignment]
                self.assertEqual(bookmark["fetch_status"], "blocked")
                self.assertEqual(
                    result["warnings"],
                    ["non_public_destination_blocked"],
                )

    def test_addresses_that_is_global_accepts_are_still_rejected(self) -> None:
        """The concrete gap the address policy exists for."""
        for address in ("64:ff9b::a9fe:a9fe", "224.0.0.1", "239.255.255.250"):
            with self.subTest(address=address):
                self.assertTrue(ipaddress.ip_address(address).is_global)
                self.assertFalse(
                    FETCHER.is_public_address(ipaddress.ip_address(address))
                )

    def test_translation_prefixes_are_rejected_independently_of_the_stdlib(
        self,
    ) -> None:
        # Whether ``is_global`` covers 2002::/16 and 64:ff9b:1::/48 has changed
        # between CPython patch releases, so the policy names them itself.
        for address in ("2002:7f00:1::", "2002:a9fe:a9fe::", "64:ff9b:1::a9fe:a9fe"):
            with self.subTest(address=address):
                self.assertFalse(
                    FETCHER.is_public_address(ipaddress.ip_address(address))
                )

    def test_nat64_prefix_is_blocked_even_with_a_public_embedded_address(self) -> None:
        # 64:ff9b::8.8.8.8 embeds a public address, but the packet still goes to
        # a NAT64 gateway whose translation the fetcher cannot see.
        self.assertFalse(
            FETCHER.is_public_address(ipaddress.ip_address("64:ff9b::808:808"))
        )

    def test_public_addresses_stay_reachable(self) -> None:
        for address in ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"):
            with self.subTest(address=address):
                self.assertTrue(
                    FETCHER.is_public_address(ipaddress.ip_address(address))
                )

    def test_integer_host_notation_is_resolved_and_blocked(self) -> None:
        try:
            socket.getaddrinfo("2130706433", 80, type=socket.SOCK_STREAM)
        except socket.gaierror:  # pragma: no cover - resolver dependent
            self.skipTest("platform resolver does not decode integer hosts")
        result = FETCHER.fetch_bookmark("http://2130706433/x", timeout_seconds=1)

        self.assertEqual(result["warnings"], ["non_public_destination_blocked"])


class UrlShapeTest(unittest.TestCase):
    def test_malformed_urls_are_rejected_before_any_lookup(self) -> None:
        for label, url in (
            ("empty", ""),
            ("oversized", "http://example.com/" + "a" * FETCHER.MAX_URL_CHARS),
            ("header injection", "http://example.com/\r\nX-Injected: 1"),
            ("invalid port", "http://example.com:notaport/x"),
            ("empty label", "http://example..com/x"),
            ("no host", "http:///x"),
            ("wrong scheme", "ftp://example.com/x"),
        ):
            with self.subTest(url=label), self.assertRaises(FETCHER.ToolFailure):
                FETCHER.normalize_url(url)


class ResolvedHostPolicyTest(unittest.TestCase):
    def test_hostname_resolving_to_nat64_metadata_is_blocked(self) -> None:
        with mock.patch.object(
            FETCHER.socket,
            "getaddrinfo",
            return_value=dns_answer("64:ff9b::a9fe:a9fe"),
        ):
            result = FETCHER.fetch_bookmark(
                "http://nat64.example/x",
                timeout_seconds=1,
            )

        self.assertEqual(result["warnings"], ["non_public_destination_blocked"])

    def test_one_private_answer_blocks_the_whole_name(self) -> None:
        answers = dns_answer("93.184.216.34") + dns_answer("64:ff9b::7f00:1")
        with mock.patch.object(FETCHER.socket, "getaddrinfo", return_value=answers):
            result = FETCHER.fetch_bookmark(
                "http://mixed.example/x",
                timeout_seconds=1,
            )

        self.assertEqual(result["warnings"], ["non_public_destination_blocked"])

    def test_empty_answer_is_a_resolution_failure(self) -> None:
        with mock.patch.object(FETCHER.socket, "getaddrinfo", return_value=[]):
            result = FETCHER.fetch_bookmark(
                "http://empty.example/x",
                timeout_seconds=1,
            )

        self.assertEqual(result["warnings"], ["dns_resolution_failed"])


class DestinationGuardTest(unittest.TestCase):
    def test_check_pins_every_resolved_address(self) -> None:
        guard = FETCHER.DestinationGuard()
        answers = dns_answer("93.184.216.34", 80) + dns_answer("93.184.216.35", 80)

        with mock.patch.object(FETCHER.socket, "getaddrinfo", return_value=answers):
            normalized = guard.check("http://example.com/page")

        self.assertEqual(normalized, "http://example.com/page")
        self.assertEqual(
            guard.targets_for("example.com", 80),
            (("93.184.216.34", 80), ("93.184.216.35", 80)),
        )

    def test_https_defaults_to_port_443(self) -> None:
        guard = FETCHER.DestinationGuard()
        with mock.patch.object(
            FETCHER.socket,
            "getaddrinfo",
            return_value=dns_answer("93.184.216.34", 443),
        ):
            guard.check("https://example.com/page")

        self.assertEqual(
            guard.targets_for("example.com", 443),
            (("93.184.216.34", 443),),
        )
        self.assertEqual(guard.targets_for("example.com", 80), ())

    def test_pinned_accepts_only_an_already_checked_endpoint(self) -> None:
        guard = FETCHER.DestinationGuard(
            test_targets={"pinned.example": ("127.0.0.1", 9)}
        )
        guard.check("http://pinned.example/first")

        self.assertEqual(
            guard.pinned("http://pinned.example/second"),
            "http://pinned.example/second",
        )
        with self.assertRaises(FETCHER.FetchIssue):
            guard.pinned("http://other.example/first")

    def test_url_shape_rules_still_apply_to_test_targets(self) -> None:
        guard = FETCHER.DestinationGuard(
            test_targets={"pinned.example": ("127.0.0.1", 9)}
        )

        for url in (
            "http://user:pass@pinned.example/x",
            "http://pinned.example:8080/x",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url), self.assertRaises(FETCHER.ToolFailure):
                guard.check(url)


if __name__ == "__main__":
    unittest.main()
