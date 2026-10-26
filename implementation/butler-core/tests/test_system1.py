"""System 1 shadow (DEC-047, DEC-057): predicts locally in milliseconds, records next to the actual route, never acts."""
import unittest
from pathlib import Path

from butler_core.routing import Shadow
from butler_core.system1 import MODEL_DIR, RouterModel, load, softmax
from support import CoreCase


class SoftmaxTest(unittest.TestCase):
    def test_softmax_is_a_distribution_and_temperature_sharpens(self):
        plain = softmax([1.0, 2.0, 3.0])
        self.assertAlmostEqual(sum(plain), 1.0)
        sharp = softmax([1.0, 2.0, 3.0], temperature=0.5)
        self.assertGreater(sharp[2], plain[2])


def runtime_present():
    try:
        import onnxruntime, tokenizers  # noqa: F401
    except ImportError:
        return False
    return (MODEL_DIR / "model.gq.onnx").is_file()


@unittest.skipUnless(runtime_present(), "router model or its runtime not installed for this interpreter")
class RouterModelTest(unittest.TestCase):
    def test_installed_model_predicts_in_milliseconds(self):
        model = load()
        self.assertIsNotNone(model)
        label, confidence = model.predict("Батлер, напомни мне завтра позвонить Саше")
        category, _, difficulty = label.partition("/d")
        self.assertIn(category, ("coding", "agentic", "math", "knowledge", "long_context", "tool_use", "design", "summarisation", "general"))
        self.assertTrue(1 <= int(difficulty) <= 5)
        self.assertTrue(0 < confidence <= 1)
        self.assertLess(model.last_latency_ms, 200)
        self.assertEqual(model.health()["available"], True)

    def test_missing_model_directory_is_silent(self):
        model = RouterModel(Path("/nonexistent/s1"))
        self.assertFalse(model.available())
        self.assertIsNone(load(Path("/nonexistent/s1")))
        with self.assertRaises(RuntimeError):
            model.predict("x")


class ShadowRecordsTest(CoreCase):
    def test_shadow_records_prediction_next_to_the_actual_route_without_acting(self):
        class Fixed:
            name = "fixed"

            def available(self):
                return True

            def predict(self, text):
                return "general/d1", 0.9

        shadow = Shadow(self.core.store, self.core.config, model=Fixed(), clock=self.clock)
        shadow.observe("ev-1", "привет", "system2", background=False)
        rows = self.core.store.query("SELECT predicted, actual, confidence, model FROM shadow")
        self.assertEqual([tuple(r) for r in rows], [("general/d1", "system2", 0.9, "fixed")])
        report = shadow.report()
        self.assertTrue(report)
        self.assertEqual(self.core.operations.list(limit=5), [])            # a prediction never becomes an operation


if __name__ == "__main__":
    unittest.main()
