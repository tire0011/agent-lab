"""Regression tests for static assets (JavaScript syntax checking, etc.)."""

import subprocess
import unittest
from pathlib import Path


class TestStaticAssets(unittest.TestCase):
    """Tests for static/client-side assets."""

    def test_app_js_syntax_check(self):
        """Verify all static JavaScript files have valid syntax via node --check.
        
        This test is mandatory and cannot be skipped. Syntax errors will cause
        the test to fail with full stderr output.
        """
        static_dir = Path(__file__).parent.parent / "static"
        js_files = sorted(static_dir.glob("*.js"))
        
        if not js_files:
            self.fail(f"No JS files found at {static_dir}")
        
        for js_path in js_files:
            result = subprocess.run(
                ["node", "--check", str(js_path)],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                error_msg = (
                    f"JavaScript syntax error in {js_path}\n"
                    f"Exit code: {result.returncode}\n"
                    f"stderr:\n{result.stderr}\n"
                    f"stdout:\n{result.stdout}"
                )
                self.fail(error_msg)


if __name__ == "__main__":
    unittest.main()
