"""
Unit tests for the `pvc_default_storage_size` config knob (kubernetes_utils.create_pvc).

The knob replaced a hardcoded PVC storage request. These tests verify the storage
size on a newly-created pod PVC is driven by config (default "70Gi"), and that an
already-existing PVC is reused without building a new (resizing) request.
"""
import sys
from unittest.mock import patch, MagicMock

sys.path.append('/home/tapis/service')

from kubernetes import client as k8s_client
import kubernetes_utils


def _mock_k8_capturing():
    """Mock k8 client: PVC not found (404) so the create path runs; capture the created body."""
    mock_k8 = MagicMock()
    mock_k8.read_namespaced_persistent_volume_claim.side_effect = \
        k8s_client.exceptions.ApiException(status=404)
    captured = {}

    def _capture(namespace, body):
        captured['body'] = body
        return body

    mock_k8.create_namespaced_persistent_volume_claim.side_effect = _capture
    return mock_k8, captured


def test_create_pvc_uses_configured_storage_size():
    """A configured pvc_default_storage_size is applied to the new PVC's storage request."""
    mock_k8, captured = _mock_k8_capturing()
    mock_conf = MagicMock()
    mock_conf.get.return_value = "123Gi"
    with patch.object(kubernetes_utils, 'k8', mock_k8), \
         patch.object(kubernetes_utils, 'conf', mock_conf):
        kubernetes_utils.create_pvc(name="test-pvc-configured")
    # knob is read by the exact key with the 70Gi fallback
    mock_conf.get.assert_any_call("pvc_default_storage_size", "70Gi")
    assert captured['body'].spec.resources.requests["storage"] == "123Gi"


def test_create_pvc_defaults_to_70gi_when_unset():
    """With pvc_default_storage_size unset, create_pvc falls back to the 70Gi default."""
    mock_k8, captured = _mock_k8_capturing()
    mock_conf = MagicMock()
    # simulate an unset config key: conf.get returns the caller-provided default
    mock_conf.get.side_effect = lambda key, default=None: default
    with patch.object(kubernetes_utils, 'k8', mock_k8), \
         patch.object(kubernetes_utils, 'conf', mock_conf):
        kubernetes_utils.create_pvc(name="test-pvc-default")
    assert captured['body'].spec.resources.requests["storage"] == "70Gi"


def test_create_pvc_reuses_existing_without_resizing():
    """An existing PVC is returned as-is; no new (resizing) storage request is built."""
    mock_k8 = MagicMock()
    existing = object()
    mock_k8.read_namespaced_persistent_volume_claim.return_value = existing
    mock_conf = MagicMock()
    with patch.object(kubernetes_utils, 'k8', mock_k8), \
         patch.object(kubernetes_utils, 'conf', mock_conf):
        result = kubernetes_utils.create_pvc(name="test-pvc-existing")
    assert result is existing
    mock_k8.create_namespaced_persistent_volume_claim.assert_not_called()
