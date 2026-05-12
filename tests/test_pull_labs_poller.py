# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 Collabora Limited
# Author: Denys Fedoryshchenko <denys.f@collabora.com>

"""Unit tests for pull_labs_poller (no network, no AWS)."""

import json
import os
import tempfile
from unittest.mock import patch

import pytest

from kernel_ci_cloud_labs.pull_labs_poller import (
    DEFAULT_FROM_TIMESTAMP,
    FileCursorStore,
    PullLabsPoller,
    _extract_test_results,
    _parse_kcidb_rest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_kernelci_env(monkeypatch):
    """Make sure no stray env from the developer's shell leaks into tests."""
    for var in [
        "KERNELCI_API_BASE_URI", "KERNELCI_API_TOKEN", "KERNELCI_RUNTIME_NAME",
        "KCIDB_SUBMIT_URL", "KCIDB_JWT", "KCIDB_REST", "KCIDB_ORIGIN",
        "PULLAB_CURSOR_FILE", "PULLAB_POLL_INTERVAL_SEC", "PULLAB_BASE_CONFIG",
    ]:
        monkeypatch.delenv(var, raising=False)


def _minimal_kc(**overrides):
    base = {
        "api_base_uri": "https://api.example/latest",
        "runtime_name": "pull-labs-aws-ec2",
        "kcidb_submit_url": "https://kcidb.example/submit",
        "kcidb_jwt": "test-jwt",
        "kcidb_origin": "pullab_cloud_aws",
    }
    base.update(overrides)
    return {"kernelci": base}


# ---------------------------------------------------------------------------
# KCIDB_REST parsing
# ---------------------------------------------------------------------------


class TestParseKcidbRest:
    def test_full_url_with_token(self):
        url, tok = _parse_kcidb_rest("https://abc@kcidb.example/submit")
        assert url == "https://kcidb.example/submit"
        assert tok == "abc"

    def test_path_without_submit_gets_suffix(self):
        url, tok = _parse_kcidb_rest("https://abc@kcidb.example/")
        assert url == "https://kcidb.example/submit"
        assert tok == "abc"

    def test_port_preserved(self):
        url, tok = _parse_kcidb_rest("https://abc@host.example:9000/api")
        assert url == "https://host.example:9000/api/submit"
        assert tok == "abc"

    def test_missing_token_returns_none(self):
        assert _parse_kcidb_rest("https://kcidb.example/submit") == (None, None)

    def test_empty_returns_none(self):
        assert _parse_kcidb_rest("") == (None, None)


# ---------------------------------------------------------------------------
# Constructor / credential priority
# ---------------------------------------------------------------------------


class TestPollerConstruction:
    def test_minimal_config_constructs(self):
        p = PullLabsPoller(_minimal_kc())
        assert p.api_base_uri == "https://api.example/latest"
        assert p.runtime_name == "pull-labs-aws-ec2"
        assert p.kcidb_submit_url == "https://kcidb.example/submit"
        assert p.kcidb_jwt == "test-jwt"
        assert p.kcidb_origin == "pullab_cloud_aws"

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("KERNELCI_API_BASE_URI", "https://env-api/")
        monkeypatch.setenv("KCIDB_ORIGIN", "env-origin")
        p = PullLabsPoller(_minimal_kc())
        assert p.api_base_uri == "https://env-api/"
        assert p.kcidb_origin == "env-origin"

    def test_kcidb_rest_used_when_explicit_pair_absent(self, monkeypatch):
        monkeypatch.setenv("KCIDB_REST", "https://reststok@kcidb.example/submit")
        p = PullLabsPoller(_minimal_kc(kcidb_submit_url=None, kcidb_jwt=None))
        assert p.kcidb_submit_url == "https://kcidb.example/submit"
        assert p.kcidb_jwt == "reststok"

    def test_explicit_pair_wins_over_kcidb_rest(self, monkeypatch):
        monkeypatch.setenv("KCIDB_SUBMIT_URL", "https://explicit.example/submit")
        monkeypatch.setenv("KCIDB_JWT", "explicit-jwt")
        monkeypatch.setenv("KCIDB_REST", "https://other@kcidb.example/submit")
        p = PullLabsPoller(_minimal_kc(kcidb_submit_url=None, kcidb_jwt=None))
        assert p.kcidb_submit_url == "https://explicit.example/submit"
        assert p.kcidb_jwt == "explicit-jwt"

    def test_incomplete_explicit_falls_through_to_kcidb_rest(self, monkeypatch):
        # Only URL set, no JWT — must fall through, not raise prematurely.
        monkeypatch.setenv("KCIDB_SUBMIT_URL", "https://explicit.example/submit")
        monkeypatch.setenv("KCIDB_REST", "https://restok@kcidb.example/submit")
        p = PullLabsPoller(_minimal_kc(kcidb_submit_url=None, kcidb_jwt=None))
        assert p.kcidb_submit_url == "https://kcidb.example/submit"
        assert p.kcidb_jwt == "restok"

    def test_missing_required_field_exits(self):
        with pytest.raises(SystemExit) as exc:
            PullLabsPoller(_minimal_kc(kcidb_jwt=None))
        assert "kcidb_jwt" in str(exc.value)

    def test_invalid_poll_interval_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("PULLAB_POLL_INTERVAL_SEC", "not-an-int")
        p = PullLabsPoller(_minimal_kc())
        assert p.poll_interval_sec == 30


# ---------------------------------------------------------------------------
# Cursor store
# ---------------------------------------------------------------------------


class TestFileCursorStore:
    def test_default_when_file_missing(self, tmp_path):
        path = str(tmp_path / "absent.json")
        store = FileCursorStore(path)
        assert store.read() == DEFAULT_FROM_TIMESTAMP

    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "cursor.json")
        store = FileCursorStore(path)
        store.write("2026-05-12T10:00:00")
        assert store.read() == "2026-05-12T10:00:00"

    def test_corrupted_file_falls_back_to_default(self, tmp_path):
        path = str(tmp_path / "broken.json")
        with open(path, "w") as f:
            f.write("not json at all")
        store = FileCursorStore(path)
        assert store.read() == DEFAULT_FROM_TIMESTAMP

    def test_missing_timestamp_key_falls_back(self, tmp_path):
        path = str(tmp_path / "no-key.json")
        with open(path, "w") as f:
            json.dump({"other": 1}, f)
        store = FileCursorStore(path)
        assert store.read() == DEFAULT_FROM_TIMESTAMP


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


class TestEventHelpers:
    def test_matches_runtime_true(self):
        p = PullLabsPoller(_minimal_kc())
        ev = {"node": {"data": {"data": {"runtime": "pull-labs-aws-ec2"}}}}
        assert p._matches_runtime(ev)

    def test_matches_runtime_false(self):
        p = PullLabsPoller(_minimal_kc())
        ev = {"node": {"data": {"data": {"runtime": "lava-collabora"}}}}
        assert not p._matches_runtime(ev)

    def test_matches_runtime_missing_keys(self):
        p = PullLabsPoller(_minimal_kc())
        assert not p._matches_runtime({})
        assert not p._matches_runtime({"node": {}})

    def test_job_definition_url_extracted(self):
        p = PullLabsPoller(_minimal_kc())
        ev = {"node": {"artifacts": {"job_definition": "https://x/y.json"}}}
        assert p._job_definition_url(ev) == "https://x/y.json"

    def test_job_definition_url_rejects_non_http(self):
        p = PullLabsPoller(_minimal_kc())
        ev = {"node": {"artifacts": {"job_definition": "file:///etc/passwd"}}}
        assert p._job_definition_url(ev) is None

    def test_job_definition_url_missing(self):
        p = PullLabsPoller(_minimal_kc())
        assert p._job_definition_url({"node": {"artifacts": {}}}) is None

    def test_events_url_includes_state_available(self):
        p = PullLabsPoller(_minimal_kc())
        url = p._events_url("2026-01-01T00:00:00")
        assert "state=available" in url
        assert "kind=job" in url
        assert "recursive=true" in url
        assert "from=" in url


# ---------------------------------------------------------------------------
# Build ID resolution
# ---------------------------------------------------------------------------


class TestResolveBuildId:
    def test_direct_kbuild_node(self):
        p = PullLabsPoller(_minimal_kc())
        node = {"id": "build-abc", "kind": "kbuild"}
        assert p.resolve_build_id(node) == "pullab_cloud_aws:build-abc"

    def test_walks_one_parent(self):
        p = PullLabsPoller(_minimal_kc())
        parent_node = {"id": "build-abc", "kind": "kbuild"}
        child_node = {"id": "job-xyz", "kind": "job", "parent": "build-abc"}
        with patch(
            "kernel_ci_cloud_labs.pull_labs_poller._http_get_json",
            return_value=parent_node,
        ):
            result = p.resolve_build_id(child_node)
        assert result == "pullab_cloud_aws:build-abc"

    def test_no_kbuild_ancestor_returns_none(self):
        p = PullLabsPoller(_minimal_kc())
        node = {"id": "n1", "kind": "test"}
        assert p.resolve_build_id(node) is None


# ---------------------------------------------------------------------------
# _extract_test_results — the surface between run_pipeline and KCIDB
# ---------------------------------------------------------------------------


class TestExtractTestResults:
    def test_all_passing(self):
        summary = {"vms": {"test_names": ["a", "b"], "failed_by_test": {}}}
        rows, log = _extract_test_results(summary)
        assert log is None
        assert rows == [
            {"name": "a", "status": "PASS"},
            {"name": "b", "status": "PASS"},
        ]

    def test_some_failing(self):
        summary = {"vms": {
            "test_names": ["a", "b"],
            "failed_by_test": {"b": ["i-123"]},
        }}
        rows, _ = _extract_test_results(summary)
        statuses = {r["name"]: r["status"] for r in rows}
        assert statuses == {"a": "PASS", "b": "FAIL"}

    def test_empty_summary(self):
        rows, log = _extract_test_results({})
        assert rows == []
        assert log is None
