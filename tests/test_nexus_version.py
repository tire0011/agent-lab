import unittest

from nexus_version import VERSION, version_tuple


class VersionTupleTests(unittest.TestCase):
    def test_canonical_version_parses(self):
        self.assertEqual(version_tuple(), tuple(int(part) for part in VERSION.split(".")))

    def test_versions_compare_numerically(self):
        self.assertLess(version_tuple("0.2.0"), version_tuple("0.10.0"))
        self.assertEqual(version_tuple(" 1.2.3 "), (1, 2, 3))

    def test_rejects_non_numeric_versions(self):
        for bad in ("", "1..2", "1.2.x", "v1.2.3", "1.-2"):
            with self.subTest(version=bad):
                with self.assertRaises(ValueError):
                    version_tuple(bad)


if __name__ == "__main__":
    unittest.main()
