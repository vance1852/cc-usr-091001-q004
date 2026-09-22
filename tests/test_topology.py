import unittest
from pathlib import Path

from island_recovery.topology import load_topology

ROOT = Path(__file__).parents[1]
FIX = Path(__file__).parent / "fixtures" / "campus_topology.json"


class TopologyTest(unittest.TestCase):
    def setUp(self):
        self.topo = load_topology(FIX)

    def test_plan_respects_isolation_source_bus_load_order(self):
        ids = [n.id for n in self.topo.plan()]
        self.assertEqual(ids[0], "grid-breaker")
        self.assertLess(ids.index("grid-breaker"), ids.index("forming-pcs"))
        self.assertLess(ids.index("forming-pcs"), ids.index("critical-bus"))
        self.assertEqual(
            [n for n in ids if n in {
                "emergency-lighting", "cold-store", "process-line", "office-load"}],
            ["emergency-lighting", "cold-store", "process-line", "office-load"],
        )

    def test_non_restorable_diesel_excluded_from_plan(self):
        ids = [n.id for n in self.topo.plan()]
        self.assertNotIn("diesel-gen", ids)
        self.assertIn("diesel-gen", self.topo.nodes)

    def test_unknown_node_reference_rejected(self):
        with self.assertRaises(ValueError):
            load_topology({"nodes": [
                {"id": "a", "kind": "load", "requires": ["missing"]}]})

    def test_cyclic_dependency_rejected(self):
        with self.assertRaises(ValueError):
            load_topology({"nodes": [
                {"id": "a", "kind": "isolation", "requires": ["b"]},
                {"id": "b", "kind": "source", "requires": ["a"]},
            ]})

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            load_topology({"nodes": [{"id": "a", "kind": "flux-capacitor"}]})

    def test_restorable_node_depending_on_manual_only_node_rejected(self):
        with self.assertRaises(ValueError):
            load_topology({"nodes": [
                {"id": "iso", "kind": "isolation"},
                {"id": "diesel", "kind": "source", "restorable": False},
                {"id": "bus", "kind": "bus", "requires": ["diesel"]},
            ]})

    def test_load_without_bus_upstream_rejected(self):
        with self.assertRaises(ValueError):
            load_topology({"nodes": [
                {"id": "iso", "kind": "isolation"},
                {"id": "pcs", "kind": "source", "requires": ["iso"]},
                {"id": "load", "kind": "load", "requires": ["pcs"]},
            ]})

    def test_source_without_isolation_upstream_rejected(self):
        with self.assertRaises(ValueError):
            load_topology({"nodes": [
                {"id": "pcs", "kind": "source"},
            ]})

    def test_bus_without_source_upstream_rejected(self):
        with self.assertRaises(ValueError):
            load_topology({"nodes": [
                {"id": "iso", "kind": "isolation"},
                {"id": "bus", "kind": "bus", "requires": ["iso"]},
            ]})

    def test_reference_topology_loads(self):
        topo = load_topology(ROOT / "reference" / "recovery_drill.json")
        self.assertEqual([n.id for n in topo.plan()],
                         ["grid-breaker", "forming-pcs", "critical-bus", "cold-store"])


if __name__ == "__main__":
    unittest.main()
