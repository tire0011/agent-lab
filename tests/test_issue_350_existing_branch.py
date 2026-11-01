"""An issue branch that already exists is reused, not a failure.

The #350 experiment ended ``failed`` with GitHub HTTP 422 "Reference already
exists" while ensuring its issue branch.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modules.github_work_tracking import GitHubWorkTrackingError, GitHubWorkTrackingService


class EnsureBranchTests(unittest.TestCase):
    def _service(self, post_error):
        calls = []

        def transport(method, path, payload):
            calls.append((method, path))
            if method == "GET" and "/git/ref/heads/nexus" in path:
                raise GitHubWorkTrackingError("GitHub HTTP 404: Not Found")
            if method == "GET":
                return {"object": {"sha": "abc"}}
            if post_error:
                raise GitHubWorkTrackingError(post_error)
            return {}

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = GitHubWorkTrackingService(
            db_path=str(Path(tmp.name) / "agent.db"), repo_root=tmp.name,
            repository="o/r", token="t", transport=transport,
        )
        return service, calls

    def test_an_existing_branch_is_reused(self):
        service, calls = self._service(
            'GitHub HTTP 422: {"message":"Reference already exists"}'
        )
        service._ensure_branch("nexus/350-report")
        self.assertEqual(calls[-1][0], "POST")

    def test_other_errors_still_raise(self):
        service, _ = self._service("GitHub HTTP 403: Forbidden")
        with self.assertRaises(GitHubWorkTrackingError):
            service._ensure_branch("nexus/350-report")


if __name__ == "__main__":
    unittest.main()
