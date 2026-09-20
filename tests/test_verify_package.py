"""Package-check tests. Fake .whl zips and build doubles are not product builds."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
CHECK_WHEEL_PATH = ROOT / "scripts" / "check_wheel.py"
VERIFY_SH = ROOT / "scripts" / "verify.sh"


def load_check_wheel() -> object:
    spec = importlib.util.spec_from_file_location("office_check_wheel", CHECK_WHEEL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load scripts/check_wheel.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["office_check_wheel"] = module
    spec.loader.exec_module(module)
    return module


check_wheel = load_check_wheel()


def write_fake_wheel(
    path: Path, *, control_html: bool, extra: dict[str, str] | None = None
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = {"jarvis_office-0.0.dist-info/METADATA": "Name: fake\n"}
    if control_html:
        names["jarvis_office/control.html"] = "<html></html>"
    if extra:
        names.update(extra)
    with ZipFile(path, "w") as archive:
        for name, body in names.items():
            archive.writestr(name, body)
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_builder(directory: Path, source: str) -> Path:
    path = directory / "fake_build.py"
    path.write_text(f"#!{sys.executable}\n{source}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class LegacyPickerTests(unittest.TestCase):
    def test_next_glob_can_accept_stale_conforming_wheel(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="legacy-glob-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        good = write_fake_wheel(folder / "aaaa_old-0-py3-none-any.whl", control_html=True)
        write_fake_wheel(
            folder / "zzzz_new-0-py3-none-any.whl",
            control_html=False,
            extra={"jarvis_office/secret.env": "nope\n"},
        )
        ordered = [good, folder / "zzzz_new-0-py3-none-any.whl"]
        picked = next(iter(ordered))
        self.assertEqual(picked, good)
        with ZipFile(picked) as archive:
            names = archive.namelist()
        self.assertIn("jarvis_office/control.html", names)


class PackageCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="verify-package-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.historical = self.root / "dist"
        self.out = self.root / "invocation"
        self.historical.mkdir()
        self.out.mkdir()

    def builder_from_payload(self, payload: Path) -> Path:
        builder = write_builder(
            self.root,
            "\n".join(
                [
                    "import argparse, shutil",
                    "from pathlib import Path",
                    f"payload = Path({str(payload)!r})",
                    "parser = argparse.ArgumentParser()",
                    'parser.add_argument("--out-dir", required=True)',
                    "args, _unknown = parser.parse_known_args()",
                    "out = Path(args.out_dir)",
                    "out.mkdir(parents=True, exist_ok=True)",
                    "shutil.copy(payload, out / payload.name)",
                ]
            ),
        )
        return builder

    def check_with_builder(self, builder: Path) -> tuple[int, str, str]:
        env = os.environ.copy()
        env["VERIFY_BUILD_CMD"] = f"{sys.executable} {builder}"
        completed = subprocess.run(
            [sys.executable, str(CHECK_WHEEL_PATH), "--out-dir", str(self.out), "--keep"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def test_a_new_nonconforming_wheel_is_not_hidden_by_old_conforming(self) -> None:
        old = write_fake_wheel(self.historical / "old-good-0-py3-none-any.whl", control_html=True)
        new = write_fake_wheel(
            self.root / "new-bad-0-py3-none-any.whl",
            control_html=False,
            extra={"jarvis_office/secret.env": "nope\n"},
        )
        code, _out, err = self.check_with_builder(self.builder_from_payload(new))
        self.assertNotEqual(code, 0)
        self.assertIn("wheel_fail", err)
        self.assertTrue(old.is_file())
        self.assertEqual(list(self.historical.glob("*.whl")), [old])

    def test_b_new_conforming_wheel_ignores_old_nonconforming(self) -> None:
        old = write_fake_wheel(
            self.historical / "old-bad-0-py3-none-any.whl",
            control_html=False,
            extra={"jarvis_office/secret.env": "nope\n"},
        )
        new = write_fake_wheel(self.root / "new-good-0-py3-none-any.whl", control_html=True)
        code, out, err = self.check_with_builder(self.builder_from_payload(new))
        self.assertEqual(code, 0, err)
        self.assertIn("wheel_ok new-good-0-py3-none-any.whl", out)
        self.assertIn(f"sha256:{file_sha256(self.out / new.name)}", out)
        self.assertTrue(old.is_file())
        self.assertIn("jarvis_office/secret.env", ZipFile(old).namelist())

    def test_c_build_failure_does_not_fall_back_to_old_wheel(self) -> None:
        old = write_fake_wheel(self.historical / "old-good-0-py3-none-any.whl", control_html=True)
        builder = write_builder(self.root, "import sys\nsys.exit(7)\n")
        code, _out, err = self.check_with_builder(builder)
        self.assertNotEqual(code, 0)
        self.assertIn("wheel_fail: build exited 7", err)
        self.assertTrue(old.is_file())
        self.assertEqual(list(self.out.glob("*.whl")), [])

    def test_d_successful_build_without_wheel_fails_explicitly(self) -> None:
        builder = write_builder(
            self.root,
            "\n".join(
                [
                    "import argparse",
                    "from pathlib import Path",
                    "parser = argparse.ArgumentParser()",
                    'parser.add_argument("--out-dir", required=True)',
                    "args, _unknown = parser.parse_known_args()",
                    "Path(args.out_dir).mkdir(parents=True, exist_ok=True)",
                    'Path(args.out_dir, "readme.txt").write_text("no wheel")',
                ]
            ),
        )
        code, _out, err = self.check_with_builder(builder)
        self.assertNotEqual(code, 0)
        self.assertIn("wheel_fail: no wheel in build output", err)

    def test_e_multiple_wheels_in_invocation_output_fail_without_picking(self) -> None:
        first = write_fake_wheel(self.root / "one-0-py3-none-any.whl", control_html=True)
        second = write_fake_wheel(self.root / "two-0-py3-none-any.whl", control_html=True)
        builder = write_builder(
            self.root,
            "\n".join(
                [
                    "import argparse, shutil",
                    "from pathlib import Path",
                    f"one = Path({str(first)!r})",
                    f"two = Path({str(second)!r})",
                    "parser = argparse.ArgumentParser()",
                    'parser.add_argument("--out-dir", required=True)',
                    "args, _unknown = parser.parse_known_args()",
                    "out = Path(args.out_dir)",
                    "out.mkdir(parents=True, exist_ok=True)",
                    "shutil.copy(one, out / one.name)",
                    "shutil.copy(two, out / two.name)",
                ]
            ),
        )
        code, _out, err = self.check_with_builder(builder)
        self.assertNotEqual(code, 0)
        self.assertIn("wheel_fail: expected 1 wheel, found 2:", err)
        self.assertIn("one-0-py3-none-any.whl", err)
        self.assertIn("two-0-py3-none-any.whl", err)

    def test_f_single_new_conforming_wheel_reports_name_and_sha256(self) -> None:
        new = write_fake_wheel(self.root / "only-0-py3-none-any.whl", control_html=True)
        code, out, err = self.check_with_builder(self.builder_from_payload(new))
        self.assertEqual(code, 0, err)
        digest = file_sha256(self.out / new.name)
        self.assertEqual(out.strip(), f"wheel_ok only-0-py3-none-any.whl sha256:{digest}")
        result = check_wheel.check_output_dir(self.out)
        self.assertEqual(result.sha256, digest)
        self.assertEqual(result.path.name, new.name)


class VerifyScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="verify-script-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_package_command_exits_nonzero_on_failure(self) -> None:
        builder = write_builder(self.root, "import sys\nsys.exit(3)\n")
        out = self.root / "out"
        out.mkdir()
        env = os.environ.copy()
        env["VERIFY_BUILD_CMD"] = f"{sys.executable} {builder}"
        env["VERIFY_OUT_DIR"] = str(out)
        completed = subprocess.run(
            [str(VERIFY_SH), "package"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("wheel_fail: build exited 3", completed.stderr)

    def test_all_stays_nonzero_but_runs_other_families(self) -> None:
        marker = self.root / "families.log"
        builder = write_builder(self.root, "import sys\nsys.exit(9)\n")
        out = self.root / "out"
        out.mkdir()
        env = os.environ.copy()
        env["VERIFY_BUILD_CMD"] = f"{sys.executable} {builder}"
        env["VERIFY_OUT_DIR"] = str(out)
        env["VERIFY_LINT_FORMAT_CMD"] = f"printf 'lint\\n' >> '{marker}'"
        env["VERIFY_TYPECHECK_CMD"] = f"printf 'types\\n' >> '{marker}'"
        env["VERIFY_TESTS_PYTHON_CMD"] = f"printf 'py\\n' >> '{marker}'"
        env["VERIFY_TESTS_JAVASCRIPT_CMD"] = f"printf 'js\\n' >> '{marker}'"
        completed = subprocess.run(
            [str(VERIFY_SH), "all"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        logged = marker.read_text().splitlines()
        self.assertEqual(logged, ["lint", "types", "py", "js"])
        self.assertIn("RESULT package FAIL", completed.stdout)
        self.assertIn("RESULT lint-format PASS", completed.stdout)
        self.assertIn("RESULT typecheck PASS", completed.stdout)
        self.assertIn("RESULT tests-python PASS", completed.stdout)
        self.assertIn("RESULT tests-javascript PASS", completed.stdout)
