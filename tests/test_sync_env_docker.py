from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = ROOT / "scripts" / "sync-env-docker.py"


def load_sync_module():
    spec = importlib.util.spec_from_file_location("docker_claude_sync_env", SYNC_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load sync module from {SYNC_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SYNC_MODULE = load_sync_module()


class SyncEnvSecretRedactionTests(unittest.TestCase):
    def run_verify(self, actual_value: str, expected_pattern: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with (
            mock.patch.object(
                SYNC_MODULE,
                "load_env_example",
                return_value=({"API_SECRET": ""}, {"API_SECRET": expected_pattern}),
            ),
            mock.patch.object(
                SYNC_MODULE,
                "docker_env",
                return_value={"API_SECRET": actual_value},
            ),
            contextlib.redirect_stdout(stdout),
        ):
            result = SYNC_MODULE.cmd_verify("test-service", skip_infra=True)
        return result, stdout.getvalue()

    def test_mismatch_does_not_print_actual_secret(self) -> None:
        secret = "live-super-secret-value"

        result, output = self.run_verify(secret, r"^expected-format$")

        self.assertEqual(result, 1)
        self.assertIn("MISMATCH", output)
        self.assertIn("API_SECRET", output)
        self.assertIn(f"length={len(secret)}", output)
        self.assertNotIn(secret, output)

    def test_placeholder_does_not_print_actual_value(self) -> None:
        placeholder = "REPLACE_ME-super-secret-value"

        result, output = self.run_verify(placeholder, r"^REPLACE_ME-.+$")

        self.assertEqual(result, 1)
        self.assertIn("PLACEHOLDER", output)
        self.assertIn(f"length={len(placeholder)}", output)
        self.assertNotIn(placeholder, output)


if __name__ == "__main__":
    unittest.main()
