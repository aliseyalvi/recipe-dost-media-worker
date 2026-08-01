from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.media_resolvers.contracts import ResolverError
from app.media_resolvers.tiktok_input import (
    resolve_tiktok_canonical_input,
    resolve_tiktok_input,
)


def response(status: int, *, location: str | None = None, html: bytes = b"") -> MagicMock:
    value = MagicMock()
    value.status_code = status
    value.headers = {"Content-Type": "text/html"}
    if location is not None:
        value.headers["Location"] = location
    value.iter_content.return_value = [html] if html else []
    value.__enter__.return_value = value
    value.__exit__.return_value = False
    return value


class TikTokShortLinkTests(unittest.TestCase):
    def test_runtime_path_uses_dns_pinned_get_without_requests_session(self) -> None:
        responses = [
            response(
                302,
                location="https://www.tiktok.com/@fixture/video/7651218940919598000",
            ),
            response(403),
        ]
        with patch(
            "app.media_resolvers.tiktok_input.resolve_public_ips",
            return_value={"8.8.8.8"},
        ), patch(
            "app.media_resolvers.tiktok_input._pinned_tiktok_get",
            side_effect=responses,
        ) as pinned_get, patch(
            "app.media_resolvers.tiktok_input.connected_peer_ip",
            return_value="8.8.8.8",
        ):
            result = resolve_tiktok_input(
                "https://vt.tiktok.com/ZSFixture/",
                input_kind="short_url",
            )
        self.assertEqual(pinned_get.call_count, 2)
        self.assertEqual(result.redirect_count, 1)
        self.assertEqual(result.canonical_source, "redirect_path")

    def test_vt_short_link_expands_and_strips_tracking(self) -> None:
        session = MagicMock()
        session.cookies = MagicMock()
        session.get.side_effect = [
            response(
                302,
                location=(
                    "https://www.tiktok.com/@fixture/video/7651218940919598000"
                    "?tracking=removed#fragment"
                ),
            ),
            response(200),
        ]
        with patch(
            "app.media_resolvers.tiktok_input.resolve_public_ips",
            return_value={"8.8.8.8"},
        ), patch(
            "app.media_resolvers.tiktok_input.connected_peer_ip",
            return_value="8.8.8.8",
        ):
            result = resolve_tiktok_canonical_input(
                "https://vt.tiktok.com/ZSFixture/?tracking=removed#fragment",
                session=session,
            )
        self.assertEqual(result.original_input_kind, "short_url")
        self.assertEqual(result.redirect_count, 1)
        self.assertEqual(
            result.canonical_url,
            "https://www.tiktok.com/@fixture/video/7651218940919598000",
        )
        self.assertEqual(result.video_id, "7651218940919598000")
        self.assertEqual(session.get.call_args_list[0].kwargs["allow_redirects"], False)
        self.assertEqual(session.get.call_args_list[0].kwargs["headers"]["Accept"], "*/*")

    def test_curl_equivalent_chain_accepts_terminal_status_after_canonical_redirect(self) -> None:
        session = MagicMock()
        session.cookies = MagicMock()
        session.get.side_effect = [
            response(
                301,
                location="https://m.tiktok.com/@fixture/video/7651218940919598000?x=1",
            ),
            response(403),
        ]
        with patch(
            "app.media_resolvers.tiktok_input.resolve_public_ips",
            return_value={"8.8.8.8"},
        ), patch(
            "app.media_resolvers.tiktok_input.connected_peer_ip",
            return_value="8.8.8.8",
        ):
            result = resolve_tiktok_input(
                "https://vt.tiktok.com/ZSFixture/",
                input_kind="short_url",
                session=session,
            )
        self.assertEqual(result.redirect_count, 1)
        self.assertEqual(result.canonical_source, "redirect_path")

    def test_vm_short_link_uses_bounded_html_canonical(self) -> None:
        session = MagicMock()
        session.cookies = MagicMock()
        session.get.return_value = response(
            200,
            html=(
                b'<html><head><link rel="canonical" '
                b'href="https://www.tiktok.com/@fixture/video/7651218940919598000?x=1">'
                b"</head></html>"
            ),
        )
        with patch(
            "app.media_resolvers.tiktok_input.resolve_public_ips",
            return_value={"8.8.8.8"},
        ), patch(
            "app.media_resolvers.tiktok_input.connected_peer_ip",
            return_value="8.8.8.8",
        ):
            result = resolve_tiktok_canonical_input(
                "https://vm.tiktok.com/ZSFixture/",
                session=session,
            )
        self.assertEqual(result.canonical_source, "html_canonical")
        self.assertEqual(result.redirect_count, 0)

    def test_full_url_requires_no_network_and_is_canonical(self) -> None:
        session = MagicMock()
        result = resolve_tiktok_canonical_input(
            "https://m.tiktok.com/@fixture/video/7651218940919598000?tracking=removed#fragment",
            session=session,
        )
        session.get.assert_not_called()
        self.assertEqual(
            result.canonical_url,
            "https://www.tiktok.com/@fixture/video/7651218940919598000",
        )

    def test_explicit_input_modes_reject_mismatches(self) -> None:
        numeric = resolve_tiktok_input(
            "7651218940919598000",
            input_kind="numeric_id",
        )
        self.assertIsNone(numeric.canonical_url)
        self.assertEqual(numeric.original_input_kind, "numeric_id")
        cases = (
            ("https://vt.tiktok.com/ZSFixture/", "numeric_id"),
            ("7651218940919598000", "canonical_url"),
            ("https://www.tiktok.com/@fixture/video/7651218940919598000", "short_url"),
        )
        for value, kind in cases:
            with self.subTest(kind=kind), self.assertRaises(ResolverError) as captured:
                resolve_tiktok_input(value, input_kind=kind)  # type: ignore[arg-type]
            self.assertEqual(captured.exception.failure_stage, "input_validation")

    def test_legitimate_canonical_host_variants_are_narrowly_accepted(self) -> None:
        for host in ("tiktok.com", "www.tiktok.com", "m.tiktok.com"):
            with self.subTest(host=host):
                result = resolve_tiktok_input(
                    f"https://{host}/@fixture/video/7651218940919598000?tracking=removed",
                    input_kind="canonical_url",
                )
                self.assertTrue(result.safe_to_dict()["video_id_resolved"])
        with self.assertRaises(ResolverError):
            resolve_tiktok_input(
                "https://www.tiktok.com.attacker.invalid/@fixture/video/7651218940919598000",
                input_kind="canonical_url",
            )

    def test_non_tiktok_redirect_is_rejected(self) -> None:
        session = MagicMock()
        session.cookies = MagicMock()
        session.get.return_value = response(302, location="https://example.invalid/video")
        with patch(
            "app.media_resolvers.tiktok_input.resolve_public_ips",
            return_value={"8.8.8.8"},
        ), patch(
            "app.media_resolvers.tiktok_input.connected_peer_ip",
            return_value="8.8.8.8",
        ):
            with self.assertRaises(ResolverError) as captured:
                resolve_tiktok_canonical_input(
                    "https://vt.tiktok.com/ZSFixture/",
                    session=session,
                )
        self.assertEqual(captured.exception.failure_stage, "short_link_redirect_validation")

    def test_redirect_loop_is_stopped_at_bound(self) -> None:
        session = MagicMock()
        session.cookies = MagicMock()
        session.get.return_value = response(302, location="https://vt.tiktok.com/ZSFixture/")
        with patch(
            "app.media_resolvers.tiktok_input.resolve_public_ips",
            return_value={"8.8.8.8"},
        ), patch(
            "app.media_resolvers.tiktok_input.connected_peer_ip",
            return_value="8.8.8.8",
        ):
            with self.assertRaisesRegex(ResolverError, "media_unavailable"):
                resolve_tiktok_canonical_input(
                    "https://vt.tiktok.com/ZSFixture/",
                    max_redirects=2,
                    session=session,
                )
        self.assertEqual(session.get.call_count, 3)

    def test_private_or_unverified_destination_is_rejected(self) -> None:
        session = MagicMock()
        session.cookies = MagicMock()
        session.get.return_value = response(302, location="https://www.tiktok.com/")
        with patch(
            "app.media_resolvers.tiktok_input.resolve_public_ips",
            return_value={"127.0.0.1"},
        ), patch(
            "app.media_resolvers.tiktok_input.connected_peer_ip",
            return_value="127.0.0.1",
        ):
            with self.assertRaises(ResolverError):
                resolve_tiktok_canonical_input(
                    "https://vt.tiktok.com/ZSFixture/",
                    session=session,
                )

    def test_repr_never_contains_canonical_url_or_video_id(self) -> None:
        result = resolve_tiktok_canonical_input(
            "https://www.tiktok.com/@fixture/video/7651218940919598000"
        )
        rendered = repr(result)
        self.assertNotIn("tiktok.com", rendered)
        self.assertNotIn("7651218940919598000", rendered)


if __name__ == "__main__":
    unittest.main()
