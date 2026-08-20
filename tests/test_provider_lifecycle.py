"""Unit tests for provider lifecycle"""

__authors__ = ["Max Hubmann <mxhbm@amazon.de>", "Norbert Manthey <nmanthey@amazon.de>"]
__copyright__ = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


from types import SimpleNamespace
from unittest.mock import Mock, patch

import botocore.exceptions
import pytest

from kernel_ci_cloud_labs.providers.aws_provider import (
    AWSProvider,
    _scan_for_kernel_crash,
)


class TestAWSProviderLifecycle:
    """Tests for AWS provider lifecycle operations"""

    def test_spawn_container_returns_task_arn(self):
        """Test spawn_container returns valid task ARN"""
        mock_auth = Mock()
        mock_ecs = Mock()
        mock_auth.get_client.return_value = mock_ecs
        mock_auth.get_network_config.return_value = {
            "awsvpcConfiguration": {"subnets": ["subnet-1"], "securityGroups": ["sg-1"]}
        }
        mock_ecs.run_task.return_value = {
            "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/cluster/abc"}],
            "failures": [],
        }

        config = {"ecs": {"cluster_name": "test-cluster", "task_definition": {"family": "test-task"}}}

        provider = AWSProvider(mock_auth, config)
        provider.authenticate()
        task_arn = provider.spawn_container()

        assert "arn:aws:ecs" in task_arn
        mock_ecs.run_task.assert_called_once()

    def test_spawn_container_handles_failures(self):
        """Test spawn_container handles task launch failures"""
        mock_auth = Mock()
        mock_ecs = Mock()
        mock_auth.get_client.return_value = mock_ecs
        mock_auth.get_network_config.return_value = {
            "awsvpcConfiguration": {"subnets": ["subnet-1"], "securityGroups": ["sg-1"]}
        }
        mock_ecs.run_task.return_value = {"tasks": [], "failures": [{"reason": "RESOURCE:CPU"}]}

        config = {"ecs": {"cluster_name": "test-cluster", "task_definition": {"family": "test-task"}}}

        provider = AWSProvider(mock_auth, config)
        provider.authenticate()

        with pytest.raises(Exception):
            provider.spawn_container()

    def test_terminate_container_stops_task(self):
        """Test terminate_container stops specific task"""
        mock_auth = Mock()
        mock_ecs = Mock()
        mock_auth.get_client.return_value = mock_ecs

        config = {"ecs": {"cluster_name": "test-cluster", "task_definition": {"family": "test-task"}}}

        provider = AWSProvider(mock_auth, config)
        provider.authenticate()
        provider.terminate_container("arn:aws:ecs:us-west-2:123:task/cluster/abc")

        mock_ecs.stop_task.assert_called_once()

    def test_stop_all_tasks_stops_running_tasks(self):
        """Test stop_all_tasks stops all running tasks"""
        mock_auth = Mock()
        mock_ecs = Mock()
        mock_auth.get_client.return_value = mock_ecs
        mock_ecs.list_tasks.return_value = {"taskArns": ["task1", "task2"]}

        config = {"ecs": {"cluster_name": "test-cluster", "task_definition": {"family": "test-task"}}}

        provider = AWSProvider(mock_auth, config)
        provider.authenticate()
        provider.stop_all_tasks()

        assert mock_ecs.stop_task.call_count == 2


class TestAWSProviderWaitLoop:
    """wait_for_task_completion: crash / stall / overall-timeout detection."""

    @staticmethod
    def _make_provider(monkeypatch, env=None):
        # Deterministic clock: time advances only when sleep is called. This
        # lets the wait loop spin synchronously while elapsed time advances
        # in fixed steps -- a hang threshold of 5s reliably fires in ~6 polls.
        clock = {"t": 1000.0}

        def fake_time():
            return clock["t"]

        def fake_sleep(_):
            clock["t"] += 1.0

        fake_time_mod = SimpleNamespace(time=fake_time, sleep=fake_sleep)
        monkeypatch.setattr(
            "kernel_ci_cloud_labs.providers.aws_provider.time", fake_time_mod,
        )
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)

        mock_auth = Mock()
        mock_ecs = Mock()
        mock_auth.get_client.return_value = mock_ecs
        config = {
            "ecs": {"cluster_name": "c", "task_definition": {"family": "t"}},
        }
        provider = AWSProvider(mock_auth, config)
        provider.authenticate()
        provider.task_arn = "arn:aws:ecs:::task/c/abc"
        return provider, mock_ecs

    def test_clean_run_returns_when_stopped(self, monkeypatch):
        p, mock_ecs = self._make_provider(monkeypatch, env={
            "PULLAB_TASK_POLL_INTERVAL_SEC": "0",
            "PULLAB_TASK_PROGRESS_LOG_SEC": "9999",
            "PULLAB_TASK_HANG_THRESHOLD_SEC": "9999",
            "PULLAB_TASK_WAIT_TIMEOUT_SEC": "9999",
        })
        # Two RUNNING then STOPPED inside the loop + one STOPPED for the
        # post-loop final_status call.
        statuses = iter([
            {"status": "RUNNING", "containers": []},
            {"status": "RUNNING", "containers": []},
            {"status": "STOPPED", "containers": []},
            {"status": "STOPPED", "containers": []},
        ])
        with patch.object(p, "get_task_status", side_effect=lambda: next(statuses)), \
             patch.object(p, "_build_vm_log_manager", return_value=None):
            result = p.wait_for_task_completion()
        assert result["status"] == "STOPPED"
        mock_ecs.stop_task.assert_not_called()

    def test_kernel_crash_terminates_and_raises(self, monkeypatch):
        p, mock_ecs = self._make_provider(monkeypatch, env={
            "PULLAB_TASK_POLL_INTERVAL_SEC": "0",
            "PULLAB_TASK_HANG_THRESHOLD_SEC": "9999",
            "PULLAB_TASK_WAIT_TIMEOUT_SEC": "9999",
        })
        cw_manager = Mock()
        cw_manager.get_logs_with_filter.return_value = [
            {
                "timestamp": 1001000,
                "message": "Kernel panic - not syncing: VFS: Unable to mount root fs",
                "logStreamName": "cmd-id/i-12345/stdout",
            },
        ]
        with patch.object(p, "get_task_status", return_value={"status": "RUNNING", "containers": []}), \
             patch.object(p, "_build_vm_log_manager", return_value=cw_manager):
            with pytest.raises(RuntimeError, match="kernel crash detected"):
                p.wait_for_task_completion()
        mock_ecs.stop_task.assert_called_once()

    def test_hang_threshold_terminates_and_raises(self, monkeypatch):
        # fake_sleep advances 1s/iter; 5s threshold fires after ~6 polls.
        p, mock_ecs = self._make_provider(monkeypatch, env={
            "PULLAB_TASK_POLL_INTERVAL_SEC": "0",
            "PULLAB_TASK_HANG_THRESHOLD_SEC": "5",
            "PULLAB_TASK_WAIT_TIMEOUT_SEC": "9999",
        })
        cw_manager = Mock()
        cw_manager.get_logs_with_filter.return_value = []
        with patch.object(p, "get_task_status", return_value={"status": "RUNNING", "containers": []}), \
             patch.object(p, "_build_vm_log_manager", return_value=cw_manager):
            with pytest.raises(RuntimeError, match="no VM console output"):
                p.wait_for_task_completion()
        mock_ecs.stop_task.assert_called_once()

    def test_transient_log_error_does_not_trip_hang_timeout(self, monkeypatch):
        # When log retrieval keeps failing (e.g. ExpiredTokenException while
        # credentials refresh), the hang timer must NOT advance — the run
        # should keep going and only stop on the overall timeout, not the
        # (much smaller) hang threshold.
        p, mock_ecs = self._make_provider(monkeypatch, env={
            "PULLAB_TASK_POLL_INTERVAL_SEC": "0",
            "PULLAB_TASK_HANG_THRESHOLD_SEC": "3",
            "PULLAB_TASK_WAIT_TIMEOUT_SEC": "20",
        })
        cw_manager = Mock()
        expired = botocore.exceptions.ClientError(
            {"Error": {"Code": "ExpiredTokenException", "Message": "expired"}},
            "FilterLogEvents",
        )
        cw_manager.get_logs_with_filter.side_effect = expired
        with patch.object(p, "get_task_status", return_value={"status": "RUNNING", "containers": []}), \
             patch.object(p, "_build_vm_log_manager", return_value=cw_manager):
            # Must NOT raise the hang error (3s); must hit the overall timeout (20s).
            with pytest.raises(RuntimeError, match="task wait timeout exceeded"):
                p.wait_for_task_completion()
        # The logs client was refreshed on the transient error.
        assert mock_ecs.stop_task.called  # terminated on overall timeout

    def test_overall_timeout_terminates_and_raises(self, monkeypatch):
        # No log manager so the only abort path is the overall-timeout cap.
        p, mock_ecs = self._make_provider(monkeypatch, env={
            "PULLAB_TASK_POLL_INTERVAL_SEC": "0",
            "PULLAB_TASK_HANG_THRESHOLD_SEC": "9999",
            "PULLAB_TASK_WAIT_TIMEOUT_SEC": "3",
        })
        with patch.object(p, "get_task_status", return_value={"status": "RUNNING", "containers": []}), \
             patch.object(p, "_build_vm_log_manager", return_value=None):
            with pytest.raises(RuntimeError, match="task wait timeout exceeded"):
                p.wait_for_task_completion()
        mock_ecs.stop_task.assert_called_once()


class TestScanForKernelCrash:
    """Pure-helper matcher: kernel-side crash / stall patterns."""

    @pytest.mark.parametrize("message", [
        "Kernel panic - not syncing: VFS: Unable to mount root fs",
        "Oops: 0000 [#1] SMP PTI",
        "BUG: kernel NULL pointer dereference, address: 0000000000000000",
        "watchdog: BUG: soft lockup - CPU#0 stuck for 22s!",
        "soft lockup - CPU#3 stuck",
        "INFO: rcu_sched detected stalls on CPUs/tasks:",
        "INFO: task kworker/0:1:42 blocked for more than 120 seconds.",
        "general protection fault: 0000 [#1] PREEMPT SMP",
        "unable to handle kernel paging request at ffff800010000000",
        "Internal error: Oops: 96000005 [#1] SMP",
    ])
    def test_matches_known_crash_patterns(self, message):
        hit = _scan_for_kernel_crash([{"message": message}])
        assert hit is not None
        assert hit["message"] == message

    def test_no_match_returns_none(self):
        # Bare "Call Trace:" is too noisy to be a crash on its own -- we
        # deliberately don't match it; verify it doesn't trip the matcher.
        assert _scan_for_kernel_crash([
            {"message": "Booting Linux..."},
            {"message": "systemd: started kernel-ci-runner"},
            {"message": "Call Trace:"},
        ]) is None

    def test_returns_first_hit(self):
        events = [
            {"message": "Booting"},
            {"message": "Kernel panic - not syncing"},
            {"message": "Oops:"},
        ]
        hit = _scan_for_kernel_crash(events)
        assert hit["message"] == "Kernel panic - not syncing"

    def test_handles_missing_or_none_message(self):
        # Real CloudWatch events sometimes carry no message field.
        assert _scan_for_kernel_crash(
            [{}, {"message": None}, {"message": ""}]
        ) is None
