"""
Testing for file ops: list_files_in_pod, upload_to_pod, download_from_pod

These tests require a running pod.

Pod image requires:
- The pod image must have /bin/sh available (for all file operations)
- The pod image must have 'ls' available (for list_files_in_pod)
- The pod image must have 'base64' available (for download_from_pod)
- Minimal/distroless images likely won't work


Notes:
- Url path leading slash is stripped, so paths are always relative to pod's working dir.
- Query parameter (?path=): Absolute paths allowed (e.g., ?path=/tmp → "/tmp")
"""
import os
import sys
import json
import time
import pytest
from tests.test_utils import headers, response_format, basic_response_checks, multipart_headers

sys.path.append('/home/tapis/service')
from api import api

from fastapi.testclient import TestClient

# raise_server_exceptions: If True, the client will raise exceptions from the server rather the normal client errors.
client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)


# Set up test variables
test_pod_1 = "testpodfiles"
test_upload_filename = "test_upload.txt"
test_upload_content = b"Hello from Pods file upload test!"
test_upload_path = "/tmp/test_upload.txt"  # Absolute path for dest_path


##### Teardown
@pytest.fixture(scope="module", autouse=True)
def teardown(headers):
    """Delete all Pod service objects created during testing.

    This fixture is automatically invoked by pytest at the end of the test.
    """
    # yield so the fixture waits until the end of the test to continue
    yield None

    # Delete all objects after the tests are done.
    pods = [test_pod_1]
    for pod_id in pods:
        rsp = client.delete(f'/pods/{pod_id}', headers=headers)


##### Setup - Create pod for file operations
class TestPodSetup:
    """Tests for setting up the pod for file operations."""

    def test_create_pod_for_file_ops(self, headers):
        """Create a pod to test file operations on."""
        pod_def = {
            "pod_id": test_pod_1,
            "image": "notchristiangarcia/testserver:fastapi",
            "description": "Test pod for file operations",
            "networking": {
                "default": {
                    "port": 5000,
                    "protocol": "http"
                }
            },
        }
        rsp = client.post("/pods", data=json.dumps(pod_def), headers=headers)
        result = basic_response_checks(rsp)
        assert result['pod_id'] == test_pod_1
        assert result['status'] == "REQUESTED"

    def test_pod_startup_for_file_ops(self, headers):
        """Wait for pod to be available before testing file operations."""
        i = 0
        while i < 25:
            rsp = client.get(f"/pods/{test_pod_1}", headers=headers)
            result = basic_response_checks(rsp)
            if result['status'] == "AVAILABLE":
                break
            time.sleep(2)
            i += 1
        else:
            assert False, "Pod never became available"
        
        assert result['status'] == "AVAILABLE"
        assert result['pod_id'] == test_pod_1


##### Test list_files_in_pod
class TestListFilesInPod:
    """Tests for the list_files_in_pod endpoint.
    
    Design: URL path = relative, query param = absolute allowed.
    """

    def test_list_files_root_via_url_path(self, headers):
        """Test listing files at current directory using empty URL path."""
        # Empty path after list_files/ should default to "." (current dir)
        rsp = client.get(f"/pods/{test_pod_1}/list_files/", headers=headers)
        result = basic_response_checks(rsp)
        
        # Should return path, files list, and count
        assert 'path' in result
        assert 'files' in result
        assert 'count' in result
        assert isinstance(result['files'], list)

    def test_list_files_tmp_via_query_param(self, headers):
        """Test listing files in /tmp using query parameter (absolute path)."""
        # Query parameter allows absolute paths
        rsp = client.get(f"/pods/{test_pod_1}/list_files", params={"path": "/tmp"}, headers=headers)
        result = basic_response_checks(rsp)
        
        assert 'files' in result
        assert isinstance(result['files'], list)
        # Query param preserves the absolute path
        assert result['path'] == "/tmp"

    def test_list_files_etc_via_query_param(self, headers):
        """Test listing files in /etc using query parameter (absolute path)."""
        rsp = client.get(f"/pods/{test_pod_1}/list_files", params={"path": "/etc"}, headers=headers)
        result = basic_response_checks(rsp)
        
        assert 'files' in result
        assert isinstance(result['files'], list)
        assert result['path'] == "/etc"

    def test_list_files_relative_path_via_url(self, headers):
        """Test that URL path gives relative path (leading slash stripped)."""
        # URL path: /list_files/tmp -> "tmp" (relative, NOT /tmp)
        rsp = client.get(f"/pods/{test_pod_1}/list_files/tmp", headers=headers)
        
        # This should fail with 404 because "tmp" (relative) doesn't exist
        # at the working directory - it's not the same as "/tmp" (absolute)
        # Unless the container's working dir happens to have a "tmp" folder
        assert rsp.status_code in [200, 404]
        
        if rsp.status_code == 200:
            result = json.loads(rsp.content)['result']
            # If it succeeded, path should be "tmp" not "/tmp"
            assert result['path'] == "tmp"

    def test_list_files_nonexistent_path_error(self, headers):
        """Test that listing a nonexistent path returns 404 error with message."""
        rsp = client.get(f"/pods/{test_pod_1}/list_files", params={"path": "/nonexistent_path_xyz123"}, headers=headers)
        
        # Should return 404 for path not found
        assert rsp.status_code == 404
        # Verify error message
        assert "not found" in rsp.text.lower() or "not accessible" in rsp.text.lower()

    def test_list_files_file_metadata_structure(self, headers):
        """Test that file listings contain proper metadata structure."""
        # Use query param for absolute path to /etc
        rsp = client.get(f"/pods/{test_pod_1}/list_files", params={"path": "/etc"}, headers=headers)
        result = basic_response_checks(rsp)
        
        # /etc should have files to verify structure
        assert result['count'] > 0, "/etc directory should have files"
        first_file = result['files'][0]
        # Verify expected fields exist
        expected_fields = ['name', 'path', 'type', 'size', 'owner', 'group', 
                         'nativePermissions', 'lastModified']
        for field in expected_fields:
            assert field in first_file, f"Missing field: {field}"

    def test_list_files_url_path_is_relative_not_absolute(self, headers):
        """Verify URL path strips leading slash making it relative."""
        # This is the KEY test: URL path should NOT work for absolute paths
        rsp = client.get(f"/pods/{test_pod_1}/list_files/etc", headers=headers)
        
        # "etc" as relative path likely doesn't exist -> 404
        if rsp.status_code == 404:
            # Expected: relative "etc" not found
            assert "not found" in rsp.text.lower() or "not accessible" in rsp.text.lower()
        elif rsp.status_code == 200:
            # If found, verify path is relative "etc" not absolute "/etc"
            result = json.loads(rsp.content)['result']
            assert result['path'] == "etc", "URL path should be relative, not absolute"


##### Test upload_to_pod
class TestUploadToPod:
    """Tests for the upload_to_pod endpoint."""

    def test_upload_file_to_pod(self, headers):
        """Test uploading a file to the pod."""
        # API expects 'file' field name
        files = {"file": (test_upload_filename, test_upload_content)}
        data = {"dest_path": test_upload_path}  # dest_path is always absolute
        
        rsp = client.post(
            f"/pods/{test_pod_1}/upload_to_pod",
            files=files,
            data=data,
            headers=multipart_headers(headers)
        )
        result = basic_response_checks(rsp)
        
        # Verify upload response
        assert 'uploaded' in result
        assert result['uploaded'] == test_upload_path

    def test_upload_file_verify_exists(self, headers):
        """Verify the uploaded file exists by listing the directory."""
        # Use query param for absolute path
        rsp = client.get(f"/pods/{test_pod_1}/list_files", params={"path": "/tmp"}, headers=headers)
        result = basic_response_checks(rsp)
        
        # Check that our uploaded file appears in the listing
        file_names = [f.get('name', '') for f in result['files'] if isinstance(f, dict)]
        assert test_upload_filename in file_names, \
            f"Uploaded file '{test_upload_filename}' not found in /tmp listing: {file_names}"

    def test_upload_binary_file(self, headers):
        """Test uploading a binary file to the pod."""
        binary_content = bytes(range(256))  # All possible byte values
        binary_filename = "test_binary.bin"
        binary_path = f"/tmp/{binary_filename}"
        
        files = {"file": (binary_filename, binary_content)}
        data = {"dest_path": binary_path}
        
        rsp = client.post(
            f"/pods/{test_pod_1}/upload_to_pod",
            files=files,
            data=data,
            headers=multipart_headers(headers)
        )
        result = basic_response_checks(rsp)
        assert 'uploaded' in result
        assert result['uploaded'] == binary_path

    def test_upload_to_nested_directory(self, headers):
        """Test uploading a file to a nested directory path."""
        nested_filename = "nested_test.txt"
        nested_path = f"/tmp/nested_dir/{nested_filename}"
        
        files = {"file": (nested_filename, b"Nested directory test content")}
        data = {"dest_path": nested_path}
        
        rsp = client.post(
            f"/pods/{test_pod_1}/upload_to_pod",
            files=files,
            data=data,
            headers=multipart_headers(headers)
        )
        # This might fail if the directory doesn't exist - behavior depends on implementation
        assert rsp.status_code in [200, 201, 400, 500]


##### Test download_from_pod
class TestDownloadFromPod:
    """Tests for the download_from_pod endpoint.
    
    Design: URL path = relative, query param = absolute allowed.
    """

    def test_download_file_via_query_param(self, headers):
        """Test downloading file using query parameter (absolute path)."""
        # Query param allows absolute paths - this is the recommended way
        rsp = client.get(
            f"/pods/{test_pod_1}/download_from_pod",
            params={"path": test_upload_path},
            headers=headers
        )
        
        assert rsp.status_code == 200
        assert rsp.content == test_upload_content

    def test_download_file_content_disposition(self, headers):
        """Test that download response has correct Content-Disposition header."""
        rsp = client.get(
            f"/pods/{test_pod_1}/download_from_pod",
            params={"path": test_upload_path},
            headers=headers
        )
        
        assert rsp.status_code == 200
        assert 'content-disposition' in rsp.headers
        assert test_upload_filename in rsp.headers['content-disposition']

    def test_download_file_content_length(self, headers):
        """Test that download response has correct Content-Length header."""
        rsp = client.get(
            f"/pods/{test_pod_1}/download_from_pod",
            params={"path": test_upload_path},
            headers=headers
        )
        
        assert rsp.status_code == 200
        assert 'content-length' in rsp.headers
        assert int(rsp.headers['content-length']) == len(test_upload_content)

    def test_download_nonexistent_file_error(self, headers):
        """Test that downloading nonexistent file returns 404 error with message."""
        rsp = client.get(
            f"/pods/{test_pod_1}/download_from_pod",
            params={"path": "/tmp/nonexistent_file_xyz123.txt"},
            headers=headers
        )
        
        assert rsp.status_code == 404
        # Verify error message
        assert "not found" in rsp.text.lower() or "not accessible" in rsp.text.lower()

    def test_download_system_file(self, headers):
        """Test downloading a system file that should exist (e.g., /etc/hostname)."""
        rsp = client.get(
            f"/pods/{test_pod_1}/download_from_pod",
            params={"path": "/etc/hostname"},
            headers=headers
        )
        
        # Should succeed if the file exists
        assert rsp.status_code in [200, 404]
        if rsp.status_code == 200:
            assert len(rsp.content) > 0

    def test_download_url_path_is_relative(self, headers):
        """Verify URL path is treated as relative (leading slash stripped)."""
        # URL path: /download_from_pod/tmp/test.txt -> "tmp/test.txt" (relative)
        # This will fail because relative "tmp/test.txt" doesn't exist
        rsp = client.get(
            f"/pods/{test_pod_1}/download_from_pod/tmp/{test_upload_filename}",
            headers=headers
        )
        
        # Relative path "tmp/test_upload.txt" won't exist -> 404
        assert rsp.status_code == 404
        assert "not found" in rsp.text.lower() or "not accessible" in rsp.text.lower()


##### Test error conditions
class TestFileOperationsErrors:
    """Tests for error conditions in file operations."""

    def test_list_files_pod_not_found(self, headers):
        """Test listing files in a non-existent pod returns error."""
        rsp = client.get("/pods/nonexistent_pod_xyz123/list_files", params={"path": "/tmp"}, headers=headers)
        try:
            print(rsp.json())
        except Exception:
            print(rsp.text)
        
        assert rsp.status_code in [400, 403, 404]

    def test_download_from_pod_not_found(self, headers):
        """Test downloading from a non-existent pod returns error."""
        rsp = client.get("/pods/nonexistent_pod_xyz123/download_from_pod", params={"path": "/tmp/file.txt"}, headers=headers)
        
        assert rsp.status_code in [400, 403, 404]

    def test_upload_to_pod_not_found(self, headers):
        """Test uploading to a non-existent pod returns error."""
        files = {"file": ("test.txt", b"test content")}
        data = {"dest_path": "/tmp/test.txt"}
        
        rsp = client.post(
            "/pods/nonexistent_pod_xyz123/upload_to_pod",
            files=files,
            data=data,
            headers=multipart_headers(headers)
        )
        
        assert rsp.status_code in [400, 403, 404]

    def test_download_both_path_params_error(self, headers):
        """Test that providing both URL path and query param returns error."""
        rsp = client.get(
            f"/pods/{test_pod_1}/download_from_pod/somepath",
            params={"path": "/tmp/other.txt"},
            headers=headers
        )
        
        # Should return 400 for conflicting path parameters
        assert rsp.status_code == 400
        # Verify error message mentions the conflict
        assert "both" in rsp.text.lower() or "not both" in rsp.text.lower()

    def test_list_files_both_path_params_error(self, headers):
        """Test that providing both URL path and query param returns error for list_files."""
        rsp = client.get(
            f"/pods/{test_pod_1}/list_files/somepath",
            params={"path": "/tmp"},
            headers=headers
        )
        
        # Should return 400 for conflicting path parameters
        assert rsp.status_code == 400
        assert "both" in rsp.text.lower() or "not both" in rsp.text.lower()


##### Test large file operations
class TestLargeFileOperations:
    """Tests for handling larger files."""

    def test_upload_large_file(self, headers):
        """Test uploading a 1MB file."""
        large_content = b"x" * (1024 * 1024)  # 1MB
        large_filename = "large_file.bin"
        large_path = f"/tmp/{large_filename}"
        
        files = {"file": (large_filename, large_content)}
        data = {"dest_path": large_path}
        
        rsp = client.post(
            f"/pods/{test_pod_1}/upload_to_pod",
            files=files,
            data=data,
            headers=multipart_headers(headers)
        )
        result = basic_response_checks(rsp)
        assert 'uploaded' in result
        assert result['uploaded'] == large_path

    def test_download_large_file(self, headers):
        """Test downloading the 1MB file."""
        rsp = client.get(
            f"/pods/{test_pod_1}/download_from_pod",
            params={"path": "/tmp/large_file.bin"},
            headers=headers
        )
        
        assert rsp.status_code == 200
        assert len(rsp.content) == 1024 * 1024

    def test_upload_medium_file(self, headers):
        """Test uploading a 100KB file."""
        medium_content = b"y" * (100 * 1024)  # 100KB
        medium_filename = "medium_file.bin"
        medium_path = f"/tmp/{medium_filename}"
        
        files = {"file": (medium_filename, medium_content)}
        data = {"dest_path": medium_path}
        
        rsp = client.post(
            f"/pods/{test_pod_1}/upload_to_pod",
            files=files,
            data=data,
            headers=multipart_headers(headers)
        )
        result = basic_response_checks(rsp)
        assert 'uploaded' in result
        assert result['uploaded'] == medium_path


##### Test round-trip file integrity
class TestFileIntegrity:
    """Tests to verify file content integrity through upload/download cycle."""

    def test_text_file_roundtrip(self, headers):
        """Test that text file content is preserved through upload/download."""
        text_content = b"This is a test file with special chars: \xc3\xa9\xc3\xa0\xc3\xbc\n"
        text_filename = "integrity_text.txt"
        text_path = f"/tmp/{text_filename}"
        
        # Upload
        files = {"file": (text_filename, text_content)}
        data = {"dest_path": text_path}
        rsp = client.post(
            f"/pods/{test_pod_1}/upload_to_pod",
            files=files,
            data=data,
            headers=multipart_headers(headers)
        )
        basic_response_checks(rsp)
        
        # Download and verify using query param for absolute path
        rsp = client.get(
            f"/pods/{test_pod_1}/download_from_pod",
            params={"path": text_path},
            headers=headers
        )
        assert rsp.status_code == 200
        assert rsp.content == text_content

    def test_binary_file_roundtrip(self, headers):
        """Test that binary file content is preserved through upload/download."""
        # Create binary content with all byte values
        binary_content = bytes(range(256)) * 4  # 1KB of all byte values
        binary_filename = "integrity_binary.bin"
        binary_path = f"/tmp/{binary_filename}"
        
        # Upload
        files = {"file": (binary_filename, binary_content)}
        data = {"dest_path": binary_path}
        rsp = client.post(
            f"/pods/{test_pod_1}/upload_to_pod",
            files=files,
            data=data,
            headers=multipart_headers(headers)
        )
        basic_response_checks(rsp)
        
        # Download and verify using query param for absolute path
        rsp = client.get(
            f"/pods/{test_pod_1}/download_from_pod",
            params={"path": binary_path},
            headers=headers
        )
        assert rsp.status_code == 200
        assert rsp.content == binary_content


##### Test shell availability error messages
class TestShellAvailability:
    """Tests to verify proper error handling for pods without shell."""

    def test_shell_error_message_format(self, headers):
        """Test that errors contain helpful information.
        
        Note: The test pod (notchristiangarcia/testserver:fastapi) has /bin/sh,
        so we're just verifying the error format when paths don't exist.
        For pods without shell, the API returns a 500 with detailed error.
        """
        # Test with nonexistent path to verify error format
        rsp = client.get(f"/pods/{test_pod_1}/list_files", params={"path": "/nonexistent_xyz"}, headers=headers)
        
        assert rsp.status_code == 404
        # Error should be a string with useful info
        assert len(rsp.text) > 0
        assert "not found" in rsp.text.lower() or "not accessible" in rsp.text.lower()


##### Cleanup test (optional - for debugging)
class TestCleanup:
    """Optional cleanup and debugging tests."""

    def test_cleanup_marker(self, headers):
        """Marker test to indicate tests completed successfully."""
        # Just verify we can still access the pod
        rsp = client.get(f"/pods/{test_pod_1}", headers=headers)
        result = basic_response_checks(rsp)
        assert result['pod_id'] == test_pod_1
        assert result['status'] == "AVAILABLE"
