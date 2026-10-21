# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Country codes on the models -cc- routing reads: a connector's declared countries and a proxy's exit country."""

import pytest

from api.models.connector import Connector
from api.models.credential import CredentialType
from api.models.location import normalize_country_list
from api.models.proxy import Proxy, ProxyCreate, ProxyUpdate


class TestConnectorCountries:
    """Connector.countries and normalize_country_list."""

    def _connector(self, **kwargs: object) -> Connector:
        return Connector(
            name="c",
            credential_id="cred",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="p",
            **kwargs,  # type: ignore[arg-type]
        )

    def test_empty_when_nothing_declared(self) -> None:
        assert self._connector().countries == []
        assert self._connector(config={"country_code": ""}).countries == []
        assert self._connector(config={"countries": []}).countries == []

    def test_static_and_cloud_countries_list(self) -> None:
        assert self._connector(config={"countries": ["gb", "IE"]}).countries == ["GB", "IE"]

    def test_provider_country_code_single_or_list(self) -> None:
        assert self._connector(config={"country_code": "de"}).countries == ["DE"]
        assert self._connector(config={"country_code": ["US", "de"]}).countries == ["US", "DE"]

    def test_uk_is_an_alias_of_gb(self) -> None:
        # Vendors, the IP databases and the map all say GB; a UK allow-list must still match -cc-gb.
        assert self._connector(config={"country_code": "uk"}).countries == ["GB"]
        assert normalize_country_list(["uk", "GB"]) == ["GB"]

    def test_normalize_country_list(self) -> None:
        assert normalize_country_list("us, gb,,US") == ["US", "GB"]
        assert normalize_country_list(["de"]) == ["DE"]
        assert normalize_country_list(None) == []
        with pytest.raises(ValueError):
            normalize_country_list(["USA"])


class TestManualProxyCountry:
    def test_uk_is_an_alias_of_gb(self) -> None:
        assert ProxyCreate(host="h", port=1, connector_id="c1", country="uk").country == "GB"
        assert ProxyUpdate(country="UK").country == "GB"
        # An empty string still means "clear".
        assert ProxyUpdate(country="").country == ""


class TestProxyCountry:
    def test_prefers_reported_country_over_geo(self) -> None:
        proxy = Proxy(host="h", port=1, connector_id="c", metadata={"country": "nl", "geo": "DE"})
        assert proxy.country == "NL"

    def test_falls_back_to_geo(self) -> None:
        assert Proxy(host="h", port=1, connector_id="c", metadata={"geo": "de"}).country == "DE"
        assert Proxy(host="h", port=1, connector_id="c").country is None
