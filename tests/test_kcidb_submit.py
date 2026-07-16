# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 Collabora Limited
# Author: Denys Fedoryshchenko <denys.f@collabora.com>

"""Unit tests for kcidb_submit."""

import json
from unittest.mock import patch

import pytest

from kernel_ci_cloud_labs.kcidb_submit import (
    KCIDB_SCHEMA_VERSION,
    STATUS_MAP,
    build_revision,
    build_test_row,
    submit_revision,
    submit_tests,
    to_kcidb_status,
    validate_origin,
    validate_test_path,
)


class TestStatusMapping:
    """Mapping from PULL_LABS / pullab statuses to KCIDB enum."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("pass", "PASS"),
            ("PASS", "PASS"),
            ("ok", "PASS"),
            ("success", "PASS"),
            ("fail", "FAIL"),
            ("FAILED", "FAIL"),
            ("skip", "SKIP"),
            ("skipped", "SKIP"),
            ("error", "ERROR"),
            ("incomplete", "ERROR"),
            ("miss", "MISS"),
            ("done", "DONE"),
        ],
    )
    def test_known_statuses(self, raw, expected):
        assert to_kcidb_status(raw) == expected

    def test_none_maps_to_error(self):
        assert to_kcidb_status(None) == "ERROR"

    def test_unknown_string_maps_to_error(self):
        assert to_kcidb_status("definitely-not-a-status") == "ERROR"

    def test_whitespace_tolerated(self):
        assert to_kcidb_status("  pass  ") == "PASS"

    def test_status_map_keys_are_lowercase(self):
        # Convention: STATUS_MAP keys must be lowercase so to_kcidb_status
        # normalises with .lower() and finds them.
        for k in STATUS_MAP:
            assert k == k.lower()


class TestValidation:
    """validate_test_path() / validate_origin() verify, never rewrite."""

    @pytest.mark.parametrize(
        "path",
        ["", "boot", "boot.infrastructure", "ltp.syscalls", "kunit-test_03"],
    )
    def test_valid_paths_pass_through(self, path):
        assert validate_test_path(path) == path

    @pytest.mark.parametrize(
        "path",
        ["boot test", "ltp/syscalls", "fs\\ext4", "100% pass", "café",
         "a..b", ".leading", "trailing.", "tab\there"],
    )
    def test_invalid_paths_raise(self, path):
        with pytest.raises(ValueError, match="invalid KCIDB test path"):
            validate_test_path(path)

    @pytest.mark.parametrize("origin", ["o", "pullab_cloud_aws", "lab1", "x_y_2"])
    def test_valid_origins_pass_through(self, origin):
        assert validate_origin(origin) == origin

    @pytest.mark.parametrize(
        "origin", ["pullab-cloud", "PullLab", "lab 1", "café", ""],
    )
    def test_invalid_origins_raise(self, origin):
        with pytest.raises(ValueError, match="invalid KCIDB origin"):
            validate_origin(origin)


class TestBuildTestRow:
    """build_test_row() shape."""

    def test_required_fields(self):
        row = build_test_row(
            origin="pullab_cloud_aws",
            build_id="maestro:b1",
            test_id="node-1.0",
            path="ltp.syscalls",
            status="pass",
        )
        assert row == {
            "id": "pullab_cloud_aws:node-1.0",
            "build_id": "maestro:b1",
            "origin": "pullab_cloud_aws",
            "path": "ltp.syscalls",
            "status": "PASS",
        }

    def test_rejects_invalid_path(self):
        # An invalid test name fails loudly here, not silently at the ingester.
        with pytest.raises(ValueError, match="invalid KCIDB test path"):
            build_test_row(
                origin="o", build_id="b", test_id="t1",
                path="boot test", status="pass",
            )

    def test_rejects_invalid_origin(self):
        with pytest.raises(ValueError, match="invalid KCIDB origin"):
            build_test_row(
                origin="bad-origin", build_id="b", test_id="t1",
                path="boot", status="pass",
            )

    def test_optional_duration_converted_to_seconds(self):
        row = build_test_row(
            origin="o", build_id="b", test_id="t1",
            path="p", status="pass", duration_ms=2500,
        )
        assert row["duration"] == 2.5

    def test_optional_log_and_misc(self):
        row = build_test_row(
            origin="o", build_id="b", test_id="t1",
            path="p", status="fail",
            log_url="https://logs/foo.txt",
            misc={"kernelci_node_id": "n1"},
        )
        assert row["log_url"] == "https://logs/foo.txt"
        assert row["misc"] == {"kernelci_node_id": "n1"}

    def test_unknown_status_becomes_error(self):
        row = build_test_row(
            origin="o", build_id="b", test_id="t1", path="p",
            status="bizarre",
        )
        assert row["status"] == "ERROR"

    def test_no_optional_keys_when_not_provided(self):
        row = build_test_row(
            origin="o", build_id="b", test_id="t1", path="p", status="pass",
        )
        assert "duration" not in row
        assert "log_url" not in row
        assert "misc" not in row
        assert "output_files" not in row


class TestBuildRevision:
    def test_tests_only_revision(self):
        rows = [
            build_test_row(origin="o", build_id="b", test_id="t1", path="p1", status="pass"),
            build_test_row(origin="o", build_id="b", test_id="t2", path="p2", status="fail"),
        ]
        rev = build_revision(rows)
        assert rev["version"] == KCIDB_SCHEMA_VERSION
        assert rev["checkouts"] == []
        assert rev["builds"] == []
        assert len(rev["tests"]) == 2

    def test_schema_version_matches_pipeline(self):
        # send_kcidb.py uses version {"major": 5, "minor": 3}
        assert KCIDB_SCHEMA_VERSION == {"major": 5, "minor": 3}


class TestSubmitRevision:
    """Submitter HTTP layer — patched so no network is involved."""

    def test_submit_revision_posts_with_bearer_auth(self):
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"status":"ok","id":"sub-1"}'

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.header_items())
            captured["body"] = req.data
            captured["timeout"] = timeout
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            resp = submit_revision(
                "https://kcidb.example/submit",
                "my-jwt",
                {"tests": [], "checkouts": [], "builds": [], "version": KCIDB_SCHEMA_VERSION},
                timeout=12.0,
            )
        assert resp == {"status": "ok", "id": "sub-1"}
        assert captured["method"] == "POST"
        assert captured["url"] == "https://kcidb.example/submit"
        # Header keys come back capitalised
        assert captured["headers"]["Authorization"] == "Bearer my-jwt"
        assert captured["headers"]["Content-type"] == "application/json"
        body = json.loads(captured["body"].decode("utf-8"))
        assert body["version"] == KCIDB_SCHEMA_VERSION
        assert captured["timeout"] == 12.0

    def test_submit_revision_missing_url_raises(self):
        with pytest.raises(ValueError, match="submit_url"):
            submit_revision("", "jwt", {})

    def test_submit_revision_missing_jwt_raises(self):
        with pytest.raises(ValueError, match="jwt_token"):
            submit_revision("https://x", "", {})


class TestSubmitTestsWrapper:
    """submit_tests() fills in build_id/origin on rows that omit them."""

    def test_wrapper_fills_missing_build_id_and_origin(self):
        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{}'

        def fake_urlopen(req, timeout=None):  # pylint: disable=unused-argument
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            submit_tests(
                "https://kcidb.example/submit",
                "jwt-x",
                origin="pullab_cloud_aws",
                build_id="maestro:b9",
                test_rows=[
                    # No build_id / no origin — wrapper must inject them
                    {"id": "x:1", "path": "p1", "status": "PASS"},
                ],
            )
        row = captured["body"]["tests"][0]
        assert row["build_id"] == "maestro:b9"
        assert row["origin"] == "pullab_cloud_aws"

    def test_wrapper_preserves_explicit_values(self):
        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{}'

        def fake_urlopen(req, timeout=None):  # pylint: disable=unused-argument
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            submit_tests(
                "https://kcidb.example/submit", "jwt-x",
                origin="default-origin", build_id="default:b",
                test_rows=[{
                    "id": "x:1", "path": "p", "status": "FAIL",
                    "build_id": "explicit:override", "origin": "explicit-origin",
                }],
            )
        row = captured["body"]["tests"][0]
        assert row["build_id"] == "explicit:override"
        assert row["origin"] == "explicit-origin"
