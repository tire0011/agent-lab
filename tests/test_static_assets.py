"""Regression tests for static assets (JavaScript syntax checking, etc.)."""

import subprocess
import unittest
from pathlib import Path


class TestStaticAssets(unittest.TestCase):
    """Tests for static/client-side assets."""

    def test_app_js_syntax_check(self):
        """Verify app.js has valid JavaScript syntax via node --check.
        
        This test is mandatory and cannot be skipped. Syntax errors will cause
        the test to fail with full stderr output.
        """
        app_js_path = Path(__file__).parent.parent / "static" / "app.js"
        
        if not app_js_path.exists():
            self.fail(f"app.js not found at {app_js_path}")
        
        # Run node --check to verify syntax
        result = subprocess.run(
            ["node", "--check", str(app_js_path)],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            error_msg = (
                f"JavaScript syntax error in {app_js_path}\n"
                f"Exit code: {result.returncode}\n"
                f"stderr:\n{result.stderr}\n"
                f"stdout:\n{result.stdout}"
            )
            self.fail(error_msg)


if __name__ == "__main__":
    unittest.main()
