"""Keep the deployed Azure VM SKU consistent across operational docs."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VM_SIZE_PATTERN = re.compile(r"\bStandard_[A-Za-z0-9_]+\b")
VM_SIZE_DOCS = (
    "README.md",
    "AGENTS.md",
    "OPERATIONS.md",
    "ARCHITECTURE.md",
    "COSTS.md",
)
CONFIRMED_VM_SIZE = "Standard_D2ds_v5"


class DocumentedVmSizeTests(unittest.TestCase):
    def test_operational_docs_name_the_confirmed_vm_size(self):
        documented_sizes = {}
        for filename in VM_SIZE_DOCS:
            text = (REPO_ROOT / filename).read_text(encoding="utf-8")
            documented_sizes[filename] = set(VM_SIZE_PATTERN.findall(text))

        self.assertEqual(
            documented_sizes,
            {filename: {CONFIRMED_VM_SIZE} for filename in VM_SIZE_DOCS},
            "update the deployed VM SKU in all five operational docs together",
        )


if __name__ == "__main__":
    unittest.main()
