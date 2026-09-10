import json
import unittest
from pathlib import Path


class RecoveryReferenceTest(unittest.TestCase):
    def test_topology_dependencies_resolve(self):
        data = json.loads((Path(__file__).parents[1] / "reference" / "recovery_drill.json").read_text(encoding="utf-8"))
        node_ids = {node["id"] for node in data["nodes"]}
        dependencies = {item for node in data["nodes"] for item in node["requires"]}
        self.assertTrue(dependencies <= node_ids)
        self.assertIn("rejected", {item["status"] for item in data["responses"]})


if __name__ == "__main__":
    unittest.main()
