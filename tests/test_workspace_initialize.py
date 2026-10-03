"""Verify project environments without network or container dependencies."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

SOURCE = Path(__file__).resolve().parents[1] / "workspace-runtime" / "initialize.py"
SPEC = importlib.util.spec_from_file_location("workspace_initialize", SOURCE)
assert SPEC is not None and SPEC.loader is not None
initialize = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(initialize)


class InitializeTest(unittest.TestCase):
    """Exercise actual Python environment publication and explicit settings."""

    def test_project_python_uses_shared_libraries_and_keeps_installations_local(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tinkerfin-environment-test-"
        ) as directory:
            root = Path(directory)
            dependencies = root / "dependencies"
            dependencies.mkdir()
            runtime = root / "runtime"
            shared = (
                runtime
                / "venv"
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            shared.mkdir(parents=True)
            (shared / "test_runtime_library.py").write_text(
                "VALUE = 'shared-readonly'\n"
            )
            session_id = str(uuid4())
            with (
                patch.object(initialize, "_DEPENDENCIES", dependencies),
                patch.object(initialize, "_RUNTIME", runtime),
            ):
                python_environment = initialize.prepare_python(session_id)
                self.assertEqual(
                    initialize.prepare_python(str(uuid4())), python_environment
                )
            response = subprocess.run(
                [
                    str(python_environment / "bin" / "python"),
                    "-I",
                    "-c",
                    "import sys, sysconfig, test_runtime_library; print(sys.prefix); print(sysconfig.get_path('purelib')); print(test_runtime_library.VALUE)",
                ],
                capture_output=True,
                text=True,
                check=True,
                env={"PATH": os.defpath},
            )
            result = response.stdout.splitlines()
            self.assertEqual(result[0], str(python_environment))
            self.assertTrue(result[1].startswith(str(python_environment)))
            self.assertEqual(result[2], "shared-readonly")
            stale = str(dependencies / f".python-{session_id}").encode()
            for launcher in (python_environment / "bin").iterdir():
                if launcher.is_file() and not launcher.is_symlink():
                    self.assertNotIn(stale, launcher.read_bytes())
            self.assertFalse((dependencies / f".python-{session_id}").exists())

    def test_explicit_environment_does_not_copy_parent_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "synthetic-secret",
                "HOME": "/parent/home",
                "HTTP_PROXY": "http://parent.invalid",
            },
        ):
            environment = initialize.project_environment()
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual(environment["HOME"], "/home/workspace")
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:18080")
        self.assertEqual(environment["NPM_CONFIG_PREFIX"], "/dependencies/node")
        self.assertTrue(environment["PATH"].startswith("/dependencies/python/bin:"))

    def test_concurrent_initialization_reuses_the_completely_published_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tinkerfin-environment-test-"
        ) as directory:
            dependencies = Path(directory)
            publish = threading.Barrier(2)
            original_rename = Path.rename

            def rename(source: Path, target: Path) -> Path:
                if target == dependencies / "python":
                    publish.wait()
                return original_rename(source, target)

            with (
                patch.object(initialize, "_DEPENDENCIES", dependencies),
                patch.object(Path, "rename", rename),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                results = tuple(
                    executor.map(
                        initialize.prepare_python, (str(uuid4()), str(uuid4()))
                    )
                )
            self.assertEqual(
                results, (dependencies / "python", dependencies / "python")
            )
            self.assertTrue((dependencies / "python" / "pyvenv.cfg").is_file())
            self.assertEqual(
                sorted(path.name for path in dependencies.iterdir()), ["python"]
            )

    def test_incomplete_existing_environment_is_not_silently_replaced(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tinkerfin-environment-test-"
        ) as directory:
            dependencies = Path(directory)
            (dependencies / "python").mkdir()
            sentinel = dependencies / "python" / "sentinel"
            sentinel.write_bytes(b"existing-project-data")
            with patch.object(initialize, "_DEPENDENCIES", dependencies):
                with self.assertRaises(RuntimeError):
                    initialize.prepare_python(str(uuid4()))
            self.assertEqual(sentinel.read_bytes(), b"existing-project-data")


if __name__ == "__main__":
    unittest.main()
