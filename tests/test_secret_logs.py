"""
Tests for the Pods Secret Logging functionality.

Tests cover:
- log_secret_event function with various event types
- Proper log entry formatting
- Default value handling for actor, tenant_id, site_id
- Logging levels based on event type (info vs warning for FAILED/ERROR events)
"""
import sys
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

# Allows us to import pods's modules.
sys.path.append('/home/tapis/service')

from models_secret_logs import log_secret_event


class MockG:
    """Mock for tapisservice.tapisfastapi.utils.g context object."""
    username = "testuser"
    request_tenant_id = "testtenant"
    site_id = "testsite"


##### Testing log_secret_event function

class TestLogSecretEvent:
    """Tests for the log_secret_event function."""

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_secret_created_event(self, mock_logger):
        """Test logging a SECRET_CREATED event."""
        log_secret_event(
            event_type="SECRET_CREATED",
            secret_id="test_secret",
            actor="creator_user",
            tenant_id="dev",
            site_id="tacc"
        )
        
        # Should log at info level (not a FAILED/ERROR event)
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        assert "SECRET_CREATED" in call_args
        assert "test_secret" in call_args
        assert "creator_user" in call_args
        assert "dev" in call_args
        assert "tacc" in call_args

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_secret_read_event(self, mock_logger):
        """Test logging a SECRET_READ event."""
        log_secret_event(
            event_type="SECRET_READ",
            secret_id="my_secret",
            actor="reader_user"
        )
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        assert "SECRET_READ" in call_args
        assert "my_secret" in call_args
        assert "reader_user" in call_args

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_secret_updated_event(self, mock_logger):
        """Test logging a SECRET_UPDATED event."""
        log_secret_event(
            event_type="SECRET_UPDATED",
            secret_id="updated_secret",
            actor="updater_user",
            details={"updated_value": True}
        )
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        assert "SECRET_UPDATED" in call_args
        assert "updated_secret" in call_args
        assert "updated_value" in call_args

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_secret_deleted_event(self, mock_logger):
        """Test logging a SECRET_DELETED event."""
        log_secret_event(
            event_type="SECRET_DELETED",
            secret_id="deleted_secret"
        )
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        assert "SECRET_DELETED" in call_args
        assert "deleted_secret" in call_args

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_failed_event_uses_warning(self, mock_logger):
        """Test that FAILED events are logged at warning level."""
        log_secret_event(
            event_type="SECRET_VALIDATION_FAILED",
            secret_id="failed_secret",
            details={"reason": "ownership mismatch"}
        )
        
        # Should log at warning level for FAILED events
        mock_logger.warning.assert_called_once()
        mock_logger.info.assert_not_called()
        
        call_args = mock_logger.warning.call_args[0][0]
        assert "SECRET_VALIDATION_FAILED" in call_args
        assert "failed_secret" in call_args

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_error_event_uses_warning(self, mock_logger):
        """Test that ERROR events are logged at warning level."""
        log_secret_event(
            event_type="SECRET_RESOLUTION_ERROR",
            secret_id="error_secret",
            details={"error": "SK connection failed"}
        )
        
        # Should log at warning level for ERROR events
        mock_logger.warning.assert_called_once()
        mock_logger.info.assert_not_called()

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_with_pod_id(self, mock_logger):
        """Test logging an event with pod_id."""
        log_secret_event(
            event_type="SECRET_INJECTED",
            secret_id="injected_secret",
            pod_id="my_pod_123"
        )
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        assert "my_pod_123" in call_args

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_with_details(self, mock_logger):
        """Test logging an event with additional details."""
        log_secret_event(
            event_type="SECRET_CREATED",
            secret_id="detailed_secret",
            details={
                "scope": "pod",
                "pod_id": "target_pod",
                "custom_field": "custom_value"
            }
        )
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        assert "scope" in call_args
        assert "pod" in call_args
        assert "custom_field" in call_args

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_defaults_from_g_context(self, mock_logger):
        """Test that actor, tenant_id, site_id default from g context."""
        log_secret_event(
            event_type="SECRET_READ",
            secret_id="context_secret"
            # Not providing actor, tenant_id, site_id - should use g defaults
        )
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        # Should use values from MockG
        assert "testuser" in call_args
        assert "testtenant" in call_args
        assert "testsite" in call_args

    @patch('models_secret_logs.logger')
    def test_log_with_missing_g_context(self, mock_logger):
        """Test logging when g context attributes are missing."""
        # Create a mock g without the expected attributes
        mock_g = MagicMock()
        del mock_g.username
        del mock_g.request_tenant_id
        del mock_g.site_id
        
        with patch('models_secret_logs.g', mock_g):
            log_secret_event(
                event_type="SECRET_READ",
                secret_id="no_context_secret"
            )
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        # Should fallback to 'unknown' when g attributes missing
        assert "unknown" in call_args

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_entry_contains_timestamp(self, mock_logger):
        """Test that log entries contain a timestamp."""
        before_call = datetime.utcnow()
        
        log_secret_event(
            event_type="SECRET_CREATED",
            secret_id="timestamp_secret"
        )
        
        after_call = datetime.utcnow()
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        # The timestamp should be in ISO format
        # Just verify the year is present (basic check)
        assert str(before_call.year) in call_args

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_empty_details_dict(self, mock_logger):
        """Test that empty details dict doesn't cause issues."""
        log_secret_event(
            event_type="SECRET_READ",
            secret_id="empty_details_secret",
            details={}
        )
        
        mock_logger.info.assert_called_once()
        # Should complete without error

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_none_details(self, mock_logger):
        """Test that None details defaults to empty dict."""
        log_secret_event(
            event_type="SECRET_READ",
            secret_id="none_details_secret",
            details=None
        )
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        # Should show empty dict for details
        assert "'details': {}" in call_args

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_log_sk_secret_name(self, mock_logger):
        """Test logging with sk_secret_name parameter."""
        log_secret_event(
            event_type="SECRET_CREATED",
            secret_id="sk_test_secret",
            sk_secret_name="pods_dev_user_testuser_sk_test_secret",
            actor="testuser"
        )
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        assert "sk_secret_name" in call_args
        assert "pods_dev_user_testuser_sk_test_secret" in call_args


##### Testing various event types that might be used

class TestSecretEventTypes:
    """Tests for various secret event types used throughout the codebase."""

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_secret_injected_event(self, mock_logger):
        """Test SECRET_INJECTED event type."""
        log_secret_event(
            event_type="SECRET_INJECTED",
            secret_id="injected_secret",
            pod_id="target_pod",
            details={"env_var": "DATABASE_PASSWORD"}
        )
        
        mock_logger.info.assert_called_once()
        assert "SECRET_INJECTED" in mock_logger.info.call_args[0][0]

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_secret_resolution_failed_event(self, mock_logger):
        """Test SECRET_RESOLUTION_FAILED event type."""
        log_secret_event(
            event_type="SECRET_RESOLUTION_FAILED",
            secret_id="missing_secret",
            pod_id="failing_pod",
            details={"error": "Secret not found in SK"}
        )
        
        # FAILED should trigger warning
        mock_logger.warning.assert_called_once()
        assert "SECRET_RESOLUTION_FAILED" in mock_logger.warning.call_args[0][0]

    @patch('models_secret_logs.logger')
    @patch('models_secret_logs.g', MockG())
    def test_secret_permission_denied_event(self, mock_logger):
        """Test logging when permission is denied."""
        log_secret_event(
            event_type="SECRET_PERMISSION_DENIED",
            secret_id="protected_secret",
            actor="unauthorized_user",
            details={"required_level": "ADMIN", "user_level": "READ"}
        )
        
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args[0][0]
        
        assert "SECRET_PERMISSION_DENIED" in call_args
        assert "unauthorized_user" in call_args
