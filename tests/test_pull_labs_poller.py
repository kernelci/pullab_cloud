# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 Collabora Limited
# Author: Denys Fedoryshchenko <denys.f@collabora.com>

"""Unit tests for pull_labs_poller (no network, no AWS)."""

import json
import logging
import os
import tempfile
import urllib.error
from unittest.mock import patch

import pytest

from kernel_ci_cloud_labs import pull_labs_poller as poller_mod
from kernel_ci_cloud_labs.pull_labs_poller import (
    DEFAULT_FROM_TIMESTAMP,
    FileCursorStore,
    NodeOutcome,
    PullLabsPoller,
    _extract_test_results,
    _node_result_from_rows,
    _parse_kcidb_rest,
    _test_name_to_path,
)

_GET = "kernel_ci_cloud_labs.pull_labs_poller._http_get_json"
_PUT = "kernel_ci_cloud_labs.pull_labs_poller._http_put_json"

# Capture the real validators at import time so a specific test can call them
# after the autouse fixtures have stubbed them out.
_REAL_VALIDATE_DEFAULT_EXECUTOR_DEPS = poller_mod._validate_default_executor_deps
_REAL_VALIDATE_API_TOKEN = poller_mod._validate_api_token


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


@pytest.fixture(autouse=True)
def _skip_default_executor_deps_check(monkeypatch):
    """Bypass the boto3/AWS-package import check the poller runs at startup.

    The construction tests don't exercise the default executor and should not
    depend on boto3 being installed in the test environment. Dedicated tests
    for the validator itself temporarily restore the real function.
    """
    monkeypatch.setattr(poller_mod, "_validate_default_executor_deps", lambda: None)


@pytest.fixture(autouse=True)
def _skip_api_token_check(monkeypatch):
    """Bypass the startup /whoami token preflight (no network in unit tests).

    Dedicated tests call the real _validate_api_token via the captured
    reference with _http_get_json patched.
    """
    monkeypatch.setattr(poller_mod, "_validate_api_token", lambda *a, **k: None)


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
        ev = {"node": {"data": {"runtime": "pull-labs-aws-ec2"}}}
        assert p._matches_runtime(ev)

    def test_matches_runtime_false(self):
        p = PullLabsPoller(_minimal_kc())
        ev = {"node": {"data": {"runtime": "lava-collabora"}}}
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

    def test_boot_test_names_remapped_to_boot_path(self):
        # Boot tests must use the "boot" path so the dashboard's is_boot()
        # classifies them as boots rather than generic tests.
        summary = {"vms": {
            "test_names": ["baseline", "url-kernel-boot", "ltp"],
            "failed_by_test": {},
        }}
        rows, _ = _extract_test_results(summary)
        names = sorted(r["name"] for r in rows)
        assert names == ["boot", "boot", "ltp"]

    def test_boot_remap_preserves_failure_status(self):
        # The failed_by_test lookup must still use the original test name.
        summary = {"vms": {
            "test_names": ["baseline"],
            "failed_by_test": {"baseline": ["i-123"]},
        }}
        rows, _ = _extract_test_results(summary)
        assert rows == [{"name": "boot", "status": "FAIL"}]


class TestExtractTestResultsPerInstance:
    """When summary["vms"]["instances"] is present the extractor must emit
    one row per VM and join boot-log URLs from artifacts.json."""

    @staticmethod
    def _write_manifest(run_dir, artifacts):
        path = os.path.join(run_dir, "artifacts.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"artifacts": artifacts}, f)
        return path

    def test_one_row_per_instance_with_log_url(self, tmp_path):
        # Two VMs for the same test, both with a manifest entry.
        self._write_manifest(
            tmp_path,
            [
                {
                    "test": "baseline",
                    "instance_id": "i-aaa",
                    "log_url": "https://b.s3.eu-west-1.amazonaws.com/a.log",
                    "status": "ready",
                },
                {
                    "test": "baseline",
                    "instance_id": "i-bbb",
                    "log_url": "https://b.s3.eu-west-1.amazonaws.com/b.log",
                    "status": "ready",
                },
            ],
        )
        summary = {
            "run_directory": str(tmp_path),
            "vms": {
                "instances": [
                    {"test": "baseline", "instance_id": "i-aaa", "status": "PASS"},
                    {"test": "baseline", "instance_id": "i-bbb", "status": "FAIL"},
                ],
            },
        }
        rows, log = _extract_test_results(summary)
        assert log is None
        # boot remap applied; instance_id and log_url preserved per row.
        assert rows == [
            {
                "name": "boot",
                "status": "PASS",
                "instance_id": "i-aaa",
                "log_url": "https://b.s3.eu-west-1.amazonaws.com/a.log",
            },
            {
                "name": "boot",
                "status": "FAIL",
                "instance_id": "i-bbb",
                "log_url": "https://b.s3.eu-west-1.amazonaws.com/b.log",
            },
        ]

    def test_missing_manifest_entry_leaves_log_url_none(self, tmp_path):
        # i-bbb's console upload failed -> no manifest entry; the row is
        # still emitted with status from the summary, log_url=None.
        self._write_manifest(
            tmp_path,
            [
                {
                    "test": "baseline",
                    "instance_id": "i-aaa",
                    "log_url": "https://b.s3.eu-west-1.amazonaws.com/a.log",
                },
            ],
        )
        summary = {
            "run_directory": str(tmp_path),
            "vms": {
                "instances": [
                    {"test": "baseline", "instance_id": "i-aaa", "status": "PASS"},
                    {"test": "baseline", "instance_id": "i-bbb", "status": "FAIL"},
                ],
            },
        }
        rows, _ = _extract_test_results(summary)
        urls = {r["instance_id"]: r["log_url"] for r in rows}
        assert urls == {"i-aaa": "https://b.s3.eu-west-1.amazonaws.com/a.log",
                        "i-bbb": None}

    def test_no_artifacts_json_still_emits_per_instance_rows(self, tmp_path):
        # run_directory exists but artifacts.json never got written
        # (e.g. collect_run_artifacts failed). Each row has log_url=None.
        summary = {
            "run_directory": str(tmp_path),
            "vms": {
                "instances": [
                    {"test": "ltp", "instance_id": "i-aaa", "status": "PASS"},
                ],
            },
        }
        rows, _ = _extract_test_results(summary)
        assert rows == [
            {"name": "ltp", "status": "PASS",
             "instance_id": "i-aaa", "log_url": None},
        ]

    def test_corrupt_artifacts_json_is_non_fatal(self, tmp_path, caplog):
        (tmp_path / "artifacts.json").write_text("{not valid json")
        summary = {
            "run_directory": str(tmp_path),
            "vms": {
                "instances": [
                    {"test": "ltp", "instance_id": "i-x", "status": "PASS"},
                ],
            },
        }
        with caplog.at_level(logging.WARNING):
            rows, _ = _extract_test_results(summary)
        assert rows[0]["log_url"] is None
        assert any("artifacts.json" in r.message for r in caplog.records)

    def test_artifacts_join_requires_both_test_and_instance(self, tmp_path):
        # Manifest entries lacking test/instance_id/log_url are skipped
        # rather than producing partial matches.
        self._write_manifest(
            tmp_path,
            [
                {"test": "ltp", "instance_id": "i-a"},  # no log_url
                {"test": "ltp", "log_url": "https://x"},  # no instance_id
                {"instance_id": "i-b", "log_url": "https://y"},  # no test
            ],
        )
        summary = {
            "run_directory": str(tmp_path),
            "vms": {
                "instances": [
                    {"test": "ltp", "instance_id": "i-a", "status": "PASS"},
                    {"test": "ltp", "instance_id": "i-b", "status": "PASS"},
                ],
            },
        }
        rows, _ = _extract_test_results(summary)
        assert all(r["log_url"] is None for r in rows)


class TestTestNameToPath:
    """_test_name_to_path() remaps boot test names to the 'boot' path."""

    @pytest.mark.parametrize("name", ["baseline", "url-kernel-boot", "boot",
                                      "Baseline", "  BOOT  "])
    def test_boot_names_map_to_boot(self, name):
        assert _test_name_to_path(name) == "boot"

    @pytest.mark.parametrize("name", ["ltp", "unixbench", "kselftest", "a"])
    def test_other_names_pass_through(self, name):
        assert _test_name_to_path(name) == name


# ---------------------------------------------------------------------------
# Node state updates — claim (state=running) and finish (state=done + result)
# ---------------------------------------------------------------------------


class TestNodeResultFromRows:
    """_node_result_from_rows() maps KCIDB statuses to a node result.

    It never returns "incomplete" -- that is reserved for infrastructure
    failures and decided by the caller (see TestProcessEventNodeResult).
    """

    @pytest.mark.parametrize(
        "statuses,expected",
        [
            (["PASS"], "pass"),
            (["PASS", "PASS"], "pass"),
            (["DONE"], "pass"),
            (["PASS", "FAIL"], "fail"),
            (["FAIL", "ERROR"], "fail"),
            # ERROR/MISS in a job that ran fail the node -- they do NOT make
            # it "incomplete" (that is infrastructure-failure only).
            (["PASS", "ERROR"], "fail"),
            (["MISS"], "fail"),
            (["SKIP"], "skip"),
        ],
    )
    def test_result_mapping(self, statuses, expected):
        rows = [{"status": s} for s in statuses]
        assert _node_result_from_rows(rows) == expected

    def test_never_returns_incomplete(self):
        for statuses in (["PASS"], ["FAIL"], ["ERROR"], ["MISS"], ["SKIP"], []):
            rows = [{"status": s} for s in statuses]
            assert _node_result_from_rows(rows) != "incomplete"


class TestNodeStateUpdates:
    """_claim_node() records data.job_id; _finish_node() PUTs state=done."""

    def test_claim_available_node_records_job_id(self):
        # kernelci-api has no claimable *state*, so claiming writes the
        # node's data.job_id ("Runtime job ID") and leaves state=available.
        p = PullLabsPoller(_minimal_kc())
        puts = []
        with patch(_GET, return_value={"id": "n1", "state": "available", "data": {}}), \
             patch(_PUT, side_effect=lambda url, payload, **kw: puts.append((url, payload))):
            assert p._claim_node({"id": "n1"}) is True
        assert len(puts) == 1
        assert puts[0][0].endswith("/node/n1")
        # state untouched (available -> available is a no-op transition);
        # the claim lives in data.job_id.
        assert puts[0][1]["state"] == "available"
        assert puts[0][1]["data"]["job_id"]

    def test_claim_skips_node_already_claimed(self):
        # A node that already carries a data.job_id has been picked up.
        p = PullLabsPoller(_minimal_kc())
        with patch(_GET, return_value={
                   "id": "n1", "state": "available",
                   "data": {"job_id": "other-poller:abc123"}}), \
             patch(_PUT) as put:
            assert p._claim_node({"id": "n1"}) is False
        put.assert_not_called()

    def test_claim_skips_node_no_longer_available(self):
        # A node that has moved on from "available" (already finished by the
        # pipeline or another poller) is skipped -- without any PUT.
        p = PullLabsPoller(_minimal_kc())
        with patch(_GET, return_value={"id": "n1", "state": "done"}), \
             patch(_PUT) as put:
            assert p._claim_node({"id": "n1"}) is False
        put.assert_not_called()

    def test_claim_skips_on_get_error(self):
        p = PullLabsPoller(_minimal_kc())
        with patch(_GET, side_effect=urllib.error.URLError("boom")):
            assert p._claim_node({"id": "n1"}) is False

    def test_claim_skips_on_put_error(self):
        p = PullLabsPoller(_minimal_kc())
        with patch(_GET, return_value={"id": "n1", "state": "available", "data": {}}), \
             patch(_PUT, side_effect=urllib.error.URLError("boom")):
            assert p._claim_node({"id": "n1"}) is False

    def test_claim_skips_node_without_id(self):
        p = PullLabsPoller(_minimal_kc())
        assert p._claim_node({}) is False

    def test_finish_node_puts_done_and_result(self):
        p = PullLabsPoller(_minimal_kc())
        puts = []
        with patch(_GET, return_value={"id": "n1", "state": "running"}), \
             patch(_PUT, side_effect=lambda url, payload, **kw: puts.append(payload)):
            assert p._finish_node("n1", NodeOutcome("pass")) is True
        assert puts[0]["state"] == "done"
        assert puts[0]["result"] == "pass"
        # No error_code/error_msg for a clean (non-infra) result.
        assert "error_code" not in puts[0].get("data", {})

    def test_finish_node_sets_error_code_on_infra_failure(self):
        p = PullLabsPoller(_minimal_kc())
        puts = []
        with patch(_GET, return_value={"id": "n1", "state": "running", "data": {}}), \
             patch(_PUT, side_effect=lambda url, payload, **kw: puts.append(payload)):
            ok = p._finish_node(
                "n1", NodeOutcome("incomplete", "Infrastructure", "vm did not boot")
            )
        assert ok is True
        assert puts[0]["result"] == "incomplete"
        # error_code/error_msg go into node.data, not the top level.
        assert puts[0]["data"]["error_code"] == "Infrastructure"
        assert puts[0]["data"]["error_msg"] == "vm did not boot"

    def test_process_event_skips_unclaimable_job(self):
        # A job we cannot claim must not be run or submitted.
        executor_calls = []
        p = PullLabsPoller(
            _minimal_kc(),
            job_executor=lambda cfg: (executor_calls.append(cfg), ([], None))[1],
        )
        event = {
            "node": {
                "id": "n1",
                "data": {"runtime": "pull-labs-aws-ec2"},
                "artifacts": {"job_definition": "https://x/y.json"},
            }
        }
        with patch.object(p, "_claim_node", return_value=False), \
             patch.object(p, "_finish_node") as finish:
            assert p.process_event(event) is True
        assert executor_calls == []
        finish.assert_not_called()


def _job_event(node_id="n1"):
    """A minimal claimable job event whose node resolves its own build_id."""
    return {
        "node": {
            "id": node_id,
            "kind": "kbuild",  # resolve_build_id returns directly, no HTTP
            "data": {"runtime": "pull-labs-aws-ec2"},
            "artifacts": {"job_definition": "https://x/y.json"},
        }
    }


class TestProcessEventNodeResult:
    """process_event() finishes the node; "incomplete" means infra failure."""

    def _run(self, poller, event, translate=None):
        translate = translate or {"return_value": {}}
        captured = {}
        with patch.object(poller, "_claim_node", return_value=True), \
             patch.object(
                 poller, "_finish_node",
                 side_effect=lambda nid, outcome: captured.update(outcome=outcome),
             ), \
             patch(_GET, return_value={"artifacts": {}}), \
             patch("kernel_ci_cloud_labs.pull_labs_poller.translate_job", **translate), \
             patch("kernel_ci_cloud_labs.pull_labs_poller.submit_tests", return_value={}):
            poller.process_event(event)
        return captured["outcome"]

    def test_passing_run_finishes_pass(self):
        p = PullLabsPoller(
            _minimal_kc(),
            job_executor=lambda cfg: ([{"name": "ltp", "status": "PASS"}], None),
        )
        outcome = self._run(p, _job_event())
        assert outcome.result == "pass"
        assert outcome.error_code is None

    def test_failing_run_finishes_fail(self):
        p = PullLabsPoller(
            _minimal_kc(),
            job_executor=lambda cfg: ([{"name": "ltp", "status": "FAIL"}], None),
        )
        outcome = self._run(p, _job_event())
        assert outcome.result == "fail"
        assert outcome.error_code is None

    def test_executor_crash_finishes_incomplete_infrastructure(self):
        def _boom(cfg):
            raise RuntimeError("vm did not boot")

        p = PullLabsPoller(_minimal_kc(), job_executor=_boom)
        outcome = self._run(p, _job_event())
        assert outcome.result == "incomplete"
        assert outcome.error_code == "Infrastructure"
        assert "vm did not boot" in outcome.error_msg

    def test_no_results_finishes_incomplete_infrastructure(self):
        p = PullLabsPoller(_minimal_kc(), job_executor=lambda cfg: ([], None))
        outcome = self._run(p, _job_event())
        assert outcome.result == "incomplete"
        assert outcome.error_code == "Infrastructure"

    def test_translate_failure_finishes_invalid_job_params(self):
        p = PullLabsPoller(_minimal_kc(), job_executor=lambda cfg: ([], None))
        outcome = self._run(
            p, _job_event(),
            translate={"side_effect": ValueError("missing artifacts.kernel")},
        )
        assert outcome.result == "incomplete"
        assert outcome.error_code == "invalid_job_params"
        assert "missing artifacts.kernel" in outcome.error_msg

    def test_per_instance_rows_carry_log_url_and_stable_test_id(self):
        """When executor returns per-instance rows with log_url, the submitted
        KCIDB rows must each carry that URL and a test_id derived from the
        instance_id (not the positional index)."""
        per_test = [
            {"name": "boot", "status": "PASS", "instance_id": "i-aaaa1111",
             "log_url": "https://b.s3.eu-west-1.amazonaws.com/a.log"},
            {"name": "boot", "status": "FAIL", "instance_id": "i-bbbb2222",
             "log_url": "https://b.s3.eu-west-1.amazonaws.com/b.log"},
        ]
        p = PullLabsPoller(
            _minimal_kc(),
            job_executor=lambda cfg: (per_test, None),
        )
        seen = {}
        with patch.object(p, "_claim_node", return_value=True), \
             patch.object(p, "_finish_node"), \
             patch(_GET, return_value={"artifacts": {}}), \
             patch("kernel_ci_cloud_labs.pull_labs_poller.translate_job",
                   return_value={}), \
             patch(
                "kernel_ci_cloud_labs.pull_labs_poller.submit_tests",
                side_effect=lambda url, jwt, origin, build_id, rows: seen.update(rows=rows),
             ):
            p.process_event(_job_event(node_id="ndX"))

        rows = seen["rows"]
        assert len(rows) == 2
        by_id = {r["id"]: r for r in rows}
        # test_id derived from instance_id => stable across retries.
        assert set(by_id) == {"pullab_cloud_aws:ndX.i-aaaa1111", "pullab_cloud_aws:ndX.i-bbbb2222"}
        # Per-row log_url survives the build_test_row pass-through.
        assert by_id["pullab_cloud_aws:ndX.i-aaaa1111"]["log_url"] == \
            "https://b.s3.eu-west-1.amazonaws.com/a.log"
        assert by_id["pullab_cloud_aws:ndX.i-bbbb2222"]["log_url"] == \
            "https://b.s3.eu-west-1.amazonaws.com/b.log"
        # instance_id surfaces in misc for traceability.
        assert by_id["pullab_cloud_aws:ndX.i-aaaa1111"]["misc"]["instance_id"] == "i-aaaa1111"
        # Aggregated node outcome from per-instance statuses.
        # (one fail among two -> fail; verified indirectly via existing tests).


# ---------------------------------------------------------------------------
# Default-executor dependency validation
# ---------------------------------------------------------------------------


class TestDefaultExecutorDepsValidation:
    """Cover the startup check that runs when no custom job_executor is set."""

    def test_missing_boto3_raises_systemexit(self, monkeypatch):
        # Put back the real validator (autouse fixture stubbed it out).
        monkeypatch.setattr(
            poller_mod,
            "_validate_default_executor_deps",
            _REAL_VALIDATE_DEFAULT_EXECUTOR_DEPS,
        )
        # Force the boto3 import inside the validator to fail.
        import builtins
        real_import = builtins.__import__

        def _fail_boto3(name, *args, **kwargs):
            if name == "boto3":
                raise ImportError("boto3 not installed (simulated)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail_boto3)
        with pytest.raises(SystemExit) as ei:
            PullLabsPoller(_minimal_kc())
        assert "boto3" in str(ei.value)

    def test_custom_executor_skips_validation(self, monkeypatch):
        """Passing a custom executor must NOT trigger the boto3 check."""
        called = {"validator": False}

        def _fail_if_called():
            called["validator"] = True
            raise SystemExit("validator should not have been called")

        monkeypatch.setattr(poller_mod, "_validate_default_executor_deps", _fail_if_called)
        # Custom executor — validator must be skipped, no SystemExit.
        PullLabsPoller(_minimal_kc(), job_executor=lambda cfg: ([], None))
        assert called["validator"] is False


# ---------------------------------------------------------------------------
# Startup /whoami token preflight
# ---------------------------------------------------------------------------


class TestValidateApiToken:
    """_validate_api_token() -- never fatal, logs token validity and groups."""

    URI = "https://api.example/latest"
    RUNTIME = "pull-labs-aws-ec2"

    def test_no_token_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            _REAL_VALIDATE_API_TOKEN(self.URI, None, self.RUNTIME)
        assert "No kernelci-api token" in caplog.text

    def test_401_logs_error(self, caplog):
        err = urllib.error.HTTPError(self.URI, 401, "Unauthorized", {}, None)
        with patch(_GET, side_effect=err), caplog.at_level(logging.ERROR):
            _REAL_VALIDATE_API_TOKEN(self.URI, "bad-token", self.RUNTIME)
        assert "rejected" in caplog.text

    def test_network_error_is_not_fatal(self):
        # A transient API error must not raise -- it cannot block startup.
        with patch(_GET, side_effect=urllib.error.URLError("boom")):
            _REAL_VALIDATE_API_TOKEN(self.URI, "t", self.RUNTIME)

    def test_valid_token_with_editor_group(self, caplog):
        whoami = {
            "username": "pullbot",
            "groups": [{"name": "runtime:pull-labs-aws-ec2:node-editor"}],
        }
        with patch(_GET, return_value=whoami), caplog.at_level(logging.INFO):
            _REAL_VALIDATE_API_TOKEN(self.URI, "t", self.RUNTIME)
        assert "token OK" in caplog.text
        assert "cannot edit" not in caplog.text

    def test_superuser_token_ok(self, caplog):
        whoami = {"username": "root", "is_superuser": True, "groups": []}
        with patch(_GET, return_value=whoami), caplog.at_level(logging.INFO):
            _REAL_VALIDATE_API_TOKEN(self.URI, "t", self.RUNTIME)
        assert "cannot edit" not in caplog.text

    def test_valid_token_without_editor_group_warns(self, caplog):
        whoami = {"username": "pullbot", "groups": [{"name": "some-other-group"}]}
        with patch(_GET, return_value=whoami), caplog.at_level(logging.WARNING):
            _REAL_VALIDATE_API_TOKEN(self.URI, "t", self.RUNTIME)
        assert "cannot edit job nodes" in caplog.text
        # The required group is named in the hint.
        assert "runtime:pull-labs-aws-ec2:node-editor" in caplog.text
