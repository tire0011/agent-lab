import subprocess
import tempfile
import unittest
from unittest import mock

from modules.verification_runner import run_verification_test


class VerificationRunnerTests(unittest.TestCase):
    def test_success_uses_supplied_timeout(self):
        proc = subprocess.CompletedProcess(['x'], 0, stdout='ok', stderr='')
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            'modules.verification_runner.subprocess.run', return_value=proc
        ) as run:
            result = run_verification_test(['x'], cwd=tmp, timeout_seconds=450)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, 'ok')
        self.assertFalse(result.timed_out)
        self.assertEqual(run.call_args.kwargs['timeout'], 450)
        self.assertFalse(run.call_args.kwargs['shell'])

    def test_timeout_is_returned_to_caller_for_shared_backoff_policy(self):
        exc = subprocess.TimeoutExpired(['x'], 300, output='partial')
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            'modules.verification_runner.subprocess.run', side_effect=exc
        ):
            result = run_verification_test(['x'], cwd=tmp, timeout_seconds=300)
        self.assertIsNone(result.exit_code)
        self.assertTrue(result.timed_out)
        self.assertIn('partial', result.output)
        self.assertIn('timed out', result.output)
