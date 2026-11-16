# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Sites and IPs: a site is the exact host a request went to, a report's site lands where the request's does, and an
IP has one text."""

import pytest

from api.reputation.site import (
    MAX_HOST_TEXT,
    MAX_SITE_LENGTH,
    _site_of,
    canonical_ip,
    counted_site,
    report_site,
    site_of,
)


class TestSiteOf:
    @pytest.mark.parametrize(
        "host",
        [
            "www.google.com",
            "maps.google.com",
            "google.com",
            "a.b.c.google.com",
            "news.bbc.co.uk",
            "docs.octoprox.github.io",
            "app.corp.internal",
        ],
    )
    def test_each_host_is_a_site_of_its_own(self, host: str) -> None:
        """www.google.com, maps.google.com and google.com may each judge an exit on its own."""
        assert site_of(host) == host

    @pytest.mark.parametrize(
        ("host", "site"),
        [
            ("WWW.Google.COM", "www.google.com"),
            ("google.com.", "google.com"),
            ("www.google.com:443", "www.google.com"),
            ("maps.google.com:443", "maps.google.com"),
            ("www.google.com.:443", "www.google.com"),
            ("  www.google.com  ", "www.google.com"),
        ],
    )
    def test_case_a_trailing_dot_a_port_and_spaces_are_dropped(self, host: str, site: str) -> None:
        assert site_of(host) == site

    @pytest.mark.parametrize(
        ("host", "site"),
        [
            ("203.0.113.7", "203.0.113.7"),
            ("203.0.113.7:8080", "203.0.113.7"),
            ("[2001:DB8::0001]", "2001:db8::1"),
            ("[2001:db8::1]:443", "2001:db8::1"),
            ("2001:db8::1", "2001:db8::1"),
            ("[::ffff:203.0.113.7]:443", "203.0.113.7"),
        ],
    )
    def test_an_ip_literal_is_its_own_site_in_its_canonical_text(
        self, host: str, site: str
    ) -> None:
        assert site_of(host) == site

    def test_a_unicode_host_is_its_ascii_form(self) -> None:
        """A CONNECT carries the punycode a browser sends, and a report may name either form."""
        assert site_of("www.bücher.de") == "www.xn--bcher-kva.de"
        assert site_of("xn--bcher-kva.de") == "xn--bcher-kva.de"
        assert site_of("пример.рф") == "xn--e1afmkfd.xn--p1ai"

    @pytest.mark.parametrize("host", ["localhost", "co.uk", "com"])
    def test_a_public_suffix_or_a_single_label_is_the_host_itself(self, host: str) -> None:
        assert site_of(host) == host

    @pytest.mark.parametrize("host", ["", "   ", "[]", ":443"])
    def test_no_host_is_no_site(self, host: str) -> None:
        assert site_of(host) is None

    def test_a_site_longer_than_a_host_name_is_none(self) -> None:
        """It could not be stored; such a host is not a name DNS resolves anyway."""
        assert site_of("x" * MAX_SITE_LENGTH + ".com") is None

    def test_text_longer_than_any_host_is_none_and_never_cached(self) -> None:
        """A CONNECT target or a report can carry kilobytes; the cache would keep each one whole."""
        cached = _site_of.cache_info().currsize
        assert site_of("a" * 60_000 + ".example.com") is None
        assert _site_of.cache_info().currsize == cached
        # The longest a host can be given as still has a site, the longest a site can be.
        longest = "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 57 + ".com.:65535"
        assert len(longest) == MAX_HOST_TEXT
        assert site_of(longest) == longest.removesuffix(".:65535")
        assert len(longest.removesuffix(".:65535")) == MAX_SITE_LENGTH


class TestReportSite:
    @pytest.mark.parametrize(
        ("text", "site"),
        [
            ("google.com", "google.com"),
            ("www.google.com", "www.google.com"),
            ("maps.google.com", "maps.google.com"),
            ("https://www.google.com/search?q=octoprox", "www.google.com"),
            ("http://user:secret@Maps.Google.com:8080/x", "maps.google.com"),
            ("//www.google.com/x", "www.google.com"),
            ("www.google.com/search", "www.google.com"),
            ("google.com?q=1", "google.com"),
            ("google.com#top", "google.com"),
            (".google.com", "google.com"),
            ("google.com.", "google.com"),
        ],
    )
    def test_a_host_or_a_url_names_its_exact_host(self, text: str, site: str) -> None:
        """A report naming google.com counts for google.com alone, not for www.google.com."""
        assert report_site(text) == site

    def test_it_lands_where_the_requests_site_does(self) -> None:
        for host in ("www.bbc.co.uk", "www.bücher.de", "[2001:db8::1]:443", "203.0.113.7:8080"):
            assert report_site(host) == site_of(host)

    @pytest.mark.parametrize(
        "text", ["co.uk", "https://co.uk/", "uk", "com", "github.io", "localhost"]
    )
    def test_a_public_suffix_is_refused(self, text: str) -> None:
        with pytest.raises(ValueError, match=r"not a site: .+ is a public suffix"):
            report_site(text)

    def test_the_refusal_names_the_suffix(self) -> None:
        with pytest.raises(ValueError, match="^not a site: co.uk is a public suffix$"):
            report_site("CO.UK")

    @pytest.mark.parametrize("text", ["", "   ", "https://", "https://[::1"])
    def test_an_empty_site_is_refused(self, text: str) -> None:
        with pytest.raises(ValueError, match="site is empty"):
            report_site(text)

    @pytest.mark.parametrize(
        "text", ["a..b.com", "goo gle.com", "*.google.com", "user@google.com", "google!.com", "goo\tgle.com"]
    )
    def test_a_name_that_is_no_host_is_refused(self, text: str) -> None:
        """Only letters, digits, hyphens, underscores and dots make a host name once in ASCII: no request goes to a
        wildcard, and a record kept under one would never be read."""
        with pytest.raises(ValueError, match="is not a host name"):
            report_site(text)

    def test_the_refusal_names_what_was_given(self) -> None:
        with pytest.raises(ValueError, match=r"^not a site: \*\.google\.com is not a host name$"):
            report_site("*.google.com")

    def test_underscores_and_unicode_names_are_host_names(self) -> None:
        assert report_site("_dmarc.example.com") == "_dmarc.example.com"
        assert report_site("www.bücher.de") == "www.xn--bcher-kva.de"

    def test_an_over_long_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="longer than"):
            report_site("x" * MAX_SITE_LENGTH + ".com")

    @pytest.mark.parametrize(
        ("text", "site"),
        [
            ("203.0.113.7", "203.0.113.7"),
            ("https://[2001:db8::1]:443/x", "2001:db8::1"),
            ("[::1]", "::1"),
            ("::FFFF:203.0.113.7", "203.0.113.7"),
        ],
    )
    def test_an_ip_is_a_site(self, text: str, site: str) -> None:
        assert report_site(text) == site


class TestCanonicalIp:
    @pytest.mark.parametrize(
        ("text", "ip"),
        [
            ("203.0.113.7", "203.0.113.7"),
            (" 203.0.113.7 ", "203.0.113.7"),
            ("2001:0DB8:0000::0001", "2001:db8::1"),
            ("::ffff:203.0.113.7", "203.0.113.7"),
            ("::FFFF:CB00:7107", "203.0.113.7"),
        ],
    )
    def test_one_text_per_address(self, text: str, ip: str) -> None:
        assert canonical_ip(text) == ip

    @pytest.mark.parametrize(
        "text", ["", "google.com", "203.0.113", "203.0.113.7:80", "[2001:db8::1]", "fe80::1%eth0"]
    )
    def test_anything_else_is_none(self, text: str) -> None:
        assert canonical_ip(text) is None


class TestCountedSite:
    """What a request is counted under for a connector's record: its site, unless the host could not be one."""

    @pytest.mark.parametrize(
        ("host", "site"),
        [
            ("www.Google.com.", "www.google.com"),
            ("[2001:db8::1]", "2001:db8::1"),
            (None, None),
            ("", None),
            ("evil\x00host", None),
            ("two words", None),
        ],
    )
    def test_a_host_a_record_cannot_hold_counts_nowhere(self, host: str | None, site: str | None) -> None:
        assert counted_site(host) == site
