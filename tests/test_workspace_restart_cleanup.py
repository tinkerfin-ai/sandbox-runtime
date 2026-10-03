"""Keep Docker attachment failures from escaping their cleanup owner."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "workspace_restart_cleanup_target", Path(__file__).with_name("workspace-restart.py")
)
assert SPEC is not None and SPEC.loader is not None
restart = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = restart
SPEC.loader.exec_module(restart)


class AttachmentCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def test_log_failures_return_before_waiting_for_the_live_attachment(self) -> None:
        for stage in ("open", "write", "flush"):
            with self.subTest(stage=stage):
                failure = OSError(f"Controlled log {stage} failure")
                stream = io.StringIO('{"ready":true}\n')
                self.addCleanup(stream.close)
                process = mock.Mock(spec=subprocess.Popen)
                process.stdout = stream
                process.poll.return_value = None
                process.wait.side_effect = AssertionError(
                    "Cannot wait before the owner stops its running container"
                )
                target = mock.MagicMock(spec=io.TextIOWrapper)
                target.__enter__.return_value = target
                if stage != "open":
                    getattr(target, stage).side_effect = failure
                owner = restart.DockerRun(self.directory)
                with (
                    mock.patch.object(
                        restart.subprocess, "Popen", return_value=process
                    ),
                    mock.patch.object(
                        restart.Path,
                        "open",
                        side_effect=failure if stage == "open" else None,
                        return_value=target,
                    ),
                ):
                    try:
                        with self.assertRaisesRegex(
                            RuntimeError, "log collection"
                        ) as captured:
                            owner.start()
                        self.assertIs(captured.exception.__cause__, failure)
                        self.assertEqual(len(owner.attachments), 1)
                        process.wait.assert_not_called()
                    finally:
                        process.wait.side_effect = None
                        process.wait.return_value = 0
                        for attachment in owner.attachments:
                            with self.assertRaisesRegex(RuntimeError, "log collection"):
                                attachment.close()
                self.assertTrue(stream.closed)
                process.kill.assert_called_once()

    def test_eof_without_readiness_leaves_waiting_to_the_cleanup_owner(self) -> None:
        stream = io.StringIO("startup refused\n")
        self.addCleanup(stream.close)
        process = mock.Mock(spec=subprocess.Popen)
        process.stdout = stream
        process.poll.return_value = None
        process.wait.side_effect = AssertionError(
            "The attached CLI is still waiting for its container"
        )
        owner = restart.DockerRun(self.directory)
        with mock.patch.object(restart.subprocess, "Popen", return_value=process):
            try:
                with self.assertRaisesRegex(RuntimeError, "startup refused"):
                    owner.start()
                self.assertEqual(len(owner.attachments), 1)
                process.wait.assert_not_called()
            finally:
                process.wait.side_effect = None
                process.wait.return_value = 0
                for attachment in owner.attachments:
                    attachment.close()
        self.assertTrue(stream.closed)
        process.kill.assert_called_once()
        process.wait.assert_called_once()

    def test_thread_start_failure_reaps_the_unregistered_cli(self) -> None:
        failure = RuntimeError("Controlled thread start failure")
        stream = io.StringIO()
        self.addCleanup(stream.close)
        process = mock.Mock(spec=subprocess.Popen)
        process.stdout = stream
        process.poll.return_value = None
        owner = restart.DockerRun(self.directory)
        with (
            mock.patch.object(restart.subprocess, "Popen", return_value=process),
            mock.patch.object(restart.Thread, "start", side_effect=failure),
            self.assertRaisesRegex(RuntimeError, "thread start") as captured,
        ):
            owner.start()
        self.assertIs(captured.exception, failure)
        self.assertEqual(owner.attachments, [])
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        self.assertTrue(stream.closed)

    def test_started_reader_is_joined_when_start_reports_failure(self) -> None:
        failure = RuntimeError("Controlled partial thread start failure")
        stream = io.StringIO()
        self.addCleanup(stream.close)
        process = mock.Mock(spec=subprocess.Popen)
        process.stdout = stream
        process.poll.return_value = None
        original_start = Thread.start
        original_join = Thread.join
        started: list[Thread] = []

        def start_then_fail(reader: Thread) -> None:
            original_start(reader)
            started.append(reader)
            raise failure

        try:
            with (
                mock.patch.object(restart.subprocess, "Popen", return_value=process),
                mock.patch.object(
                    Thread, "start", autospec=True, side_effect=start_then_fail
                ),
                mock.patch.object(
                    Thread, "join", autospec=True, side_effect=original_join
                ) as joined,
            ):
                with self.assertRaisesRegex(RuntimeError, "partial thread start"):
                    restart.DockerRun(self.directory).start()
                joined.assert_called_once_with(started[0])
                process.kill.assert_called_once()
                process.wait.assert_called_once()
                self.assertTrue(stream.closed)
        finally:
            for reader in started:
                original_join(reader)


if __name__ == "__main__":
    unittest.main()
