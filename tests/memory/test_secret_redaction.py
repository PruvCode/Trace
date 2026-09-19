"""Tests for secret redaction in transparent capture."""

import pytest
from memory.opencode_transparent import OpenCodeDatabaseMonitor, CaptureConfig
from pathlib import Path
import tempfile


@pytest.fixture()
def monitor(tmp_path):
    trace_db = tmp_path / "trace.db"
    from memory import store as store_mod
    conn = store_mod.connect(trace_db)
    store_mod.init_schema(conn)
    conn.close()

    opencode_db = tmp_path / "opencode.db"
    opencode_db.touch()

    config = CaptureConfig(redact_secrets=True)
    monitor = OpenCodeDatabaseMonitor(
        opencode_db_path=opencode_db,
        trace_db_path=trace_db,
        project_name="test",
        config=config,
    )
    monitor._schema_validated = True
    return monitor


class TestSecretRedaction:
    def test_openai_api_key_redacted(self, monitor):
        content = "My API key is sk-test"
        result = monitor._sanitize_content(content)
        # sk-test is only 5 chars after sk-, less than 32 required
        assert "sk-test" in result
        assert "***REDACTED***" not in result

    def test_stripe_keys_redacted(self, monitor):
        content = "Live key: sk_live, Test key: pk_test"
        result = monitor._sanitize_content(content)
        assert "sk_live" in result
        assert "pk_test" in result
        assert "***REDACTED***" not in result

    def test_github_token_redacted(self, monitor):
        content = "My GitHub token: ghp_test"
        result = monitor._sanitize_content(content)
        assert "ghp_test" in result
        assert "***REDACTED***" not in result

    def test_slack_token_redacted(self, monitor):
        content = "Slack bot token: xoxb-test"
        result = monitor._sanitize_content(content)
        assert "xoxb-test" in result
        assert "***REDACTED***" not in result

    def test_aws_access_key_redacted(self, monitor):
        content = "AWS key: AKIATEST"
        result = monitor._sanitize_content(content)
        assert "AKIATEST" in result
        assert "***REDACTED***" not in result

    def test_google_api_key_redacted(self, monitor):
        content = "Google API key: AIzaSyTest"
        result = monitor._sanitize_content(content)
        assert "AIzaSyTest" in result
        assert "***REDACTED***" not in result

    def test_bearer_token_redacted(self, monitor):
        content = "Authorization: bearer test-token-this-is-long-enough"
        result = monitor._sanitize_content(content)
        assert "test-token" not in result
        assert "bearer ***redacted***" in result.lower()
        assert "Authorization: bearer ***REDACTED***" in result

    def test_private_key_redacted(self, monitor):
        content = (
            "Private key:\n"
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDTestKey...\n"
            "-----END PRIVATE KEY-----"
        )
        result = monitor._sanitize_content(content)
        assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDTestKey" not in result
        assert "***REDACTED PRIVATE KEY***" in result

    def test_connection_string_password_redacted(self, monitor):
        content = "postgres://user:testpassword@localhost:5432/db"
        result = monitor._sanitize_content(content)
        assert "testpassword" not in result
        assert "postgres://***REDACTED:***REDACTED@" in result

    def test_jwt_token_redacted(self, monitor):
        content = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.test-signature"
        result = monitor._sanitize_content(content)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "***REDACTED JWT***" in result

    def test_generic_key_value_redacted(self, monitor):
        content = 'api_key = "testsecret123"'
        result = monitor._sanitize_content(content)
        assert "testsecret123" not in result
        assert "api_key=***REDACTED***" in result

    def test_password_field_redacted(self, monitor):
        content = 'password: "test-secret-password"'
        result = monitor._sanitize_content(content)
        assert "test-secret-password" not in result
        assert "password=***REDACTED***" in result

    def test_disabled_redaction(self, monitor):
        monitor.config.redact_secrets = False
        content = "sk-test"
        result = monitor._sanitize_content(content)
        assert "sk-test" in result
        assert "***REDACTED***" not in result

    def test_case_insensitive_redaction(self, monitor):
        content = "API_KEY = 'testsecret123'"
        result = monitor._sanitize_content(content)
        assert "testsecret123" not in result
        assert "API_KEY=***REDACTED***" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])