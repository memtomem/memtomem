"""Tests for SSRF protection in URL fetcher."""

import pytest
from memtomem.indexing.url_fetcher import _validate_url


class TestValidateUrl:
    def test_valid_https(self):
        assert _validate_url("https://example.com") == "https://example.com"

    def test_valid_http(self):
        assert _validate_url("http://example.com") == "http://example.com"

    def test_block_ftp(self):
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            _validate_url("ftp://example.com")

    def test_block_file(self):
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            _validate_url("file:///etc/passwd")

    def test_block_localhost(self):
        with pytest.raises(ValueError, match="Blocked host"):
            _validate_url("http://localhost/test")

    def test_block_127(self):
        with pytest.raises(ValueError, match="Blocked host"):
            _validate_url("http://127.0.0.1:8080/api")

    def test_block_ipv6_loopback(self):
        with pytest.raises(ValueError, match="Blocked host"):
            _validate_url("http://[::1]/test")

    def test_block_private_10(self):
        with pytest.raises(ValueError, match="Blocked private"):
            _validate_url("http://10.0.0.1/internal")

    def test_block_private_192(self):
        with pytest.raises(ValueError, match="Blocked private"):
            _validate_url("http://192.168.1.1/admin")

    def test_block_private_172(self):
        with pytest.raises(ValueError, match="Blocked private"):
            _validate_url("http://172.16.0.1/")

    def test_block_link_local(self):
        with pytest.raises(ValueError, match="Blocked private"):
            _validate_url("http://169.254.169.254/latest/meta-data/")

    def test_block_dot_local(self):
        with pytest.raises(ValueError, match="Blocked internal host"):
            _validate_url("http://myserver.local/api")

    def test_block_dot_internal(self):
        with pytest.raises(ValueError, match="Blocked internal host"):
            _validate_url("http://db.internal/query")

    def test_no_hostname(self):
        with pytest.raises(ValueError, match="hostname"):
            _validate_url("http:///path")

    def test_block_zero_ip(self):
        with pytest.raises(ValueError, match="Blocked host"):
            _validate_url("http://0.0.0.0/")
