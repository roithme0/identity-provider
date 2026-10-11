from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from disposable import compose_arguments, require_disposable


class DisposableTargetsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.test_compose = self.root / "deployments/docker-compose-test.yml"

    def test_regular_deployments_are_rejected(self) -> None:
        for name in ("local", "staging", "prod"):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                compose_arguments(self.root / f"deployments/docker-compose-{name}.yml")

    def test_project_and_environment_are_explicit(self) -> None:
        arguments = compose_arguments(self.test_compose)
        self.assertEqual(arguments[arguments.index("--project-name") + 1], "identity-provider-test")
        self.assertEqual(Path(arguments[arguments.index("--env-file") + 1]), self.root / "deployments/.env.example")

    def test_regular_provider_url_is_rejected_before_http(self) -> None:
        with patch("disposable.configuration", return_value={}), self.assertRaises(RuntimeError):
            require_disposable(self.test_compose, "http://localhost:8006")


if __name__ == "__main__":
    unittest.main()
