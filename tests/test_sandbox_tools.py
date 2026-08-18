import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nodeclaw.core.tools import sandbox_tools


class TestSandboxTools(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.office = Path(self.temp_dir.name)
        self.skills = self.office / "skills"
        self.script = self.skills / "demo" / "scripts" / "demo.py"
        self.script.parent.mkdir(parents=True)
        self.script.write_text("print('skill ok')\n", encoding="utf-8")
        self.office_patch = patch.object(sandbox_tools, "OFFICE_DIR", str(self.office))
        self.office_patch.start()

    def tearDown(self):
        self.office_patch.stop()
        self.temp_dir.cleanup()

    def test_get_safe_path_accepts_office_relative_path(self):
        result = sandbox_tools._get_safe_path("skills/demo/scripts/demo.py", must_exist=True)
        self.assertEqual(Path(result), self.script.resolve())

    def test_get_safe_path_rejects_parent_and_absolute_paths(self):
        for value in ("../../forbidden.txt", "/etc/passwd", "~/secret", "C:\\secret.txt"):
            with self.subTest(value=value), self.assertRaises(PermissionError):
                sandbox_tools._get_safe_path(value)

    def test_get_safe_path_rejects_similar_prefix_escape(self):
        sibling = self.office.parent / f"{self.office.name}-outside"
        with self.assertRaises(PermissionError):
            sandbox_tools._get_safe_path(f"../{sibling.name}/secret.txt")

    @unittest.skipIf(os.name == "nt", "Symlink creation may require elevated Windows privileges")
    def test_get_safe_path_rejects_symlink_even_when_target_is_inside(self):
        real_dir = self.office / "real"
        real_dir.mkdir()
        (self.office / "linked").symlink_to(real_dir, target_is_directory=True)
        with self.assertRaises(PermissionError):
            sandbox_tools._get_safe_path("linked/file.txt")

    @unittest.skipIf(os.name == "nt", "Symlink creation may require elevated Windows privileges")
    def test_read_rejects_symlink_escape(self):
        outside = Path(self.temp_dir.name).parent / "nodeclaw-outside-secret.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.office / "secret-link.txt"
        link.symlink_to(outside)
        try:
            result = sandbox_tools.read_office_file.invoke({"filepath": "secret-link.txt"})
            self.assertIn("符号链接", result)
            self.assertNotIn("secret\n", result)
        finally:
            outside.unlink(missing_ok=True)

    def test_list_read_and_write_office_files(self):
        result = sandbox_tools.write_office_file.invoke({
            "filepath": "notes/test.txt",
            "content": "hello",
            "mode": "w",
        })
        self.assertIn("成功", result)
        self.assertEqual(
            sandbox_tools.read_office_file.invoke({"filepath": "notes/test.txt"}),
            "hello",
        )
        listing = sandbox_tools.list_office_files.invoke({"sub_dir": "notes"})
        self.assertIn("[文件] test.txt", listing)

    def test_write_rejects_invalid_mode_and_large_content(self):
        invalid = sandbox_tools.write_office_file.invoke({
            "filepath": "test.txt", "content": "text", "mode": "x"
        })
        self.assertIn("mode", invalid)
        oversized = sandbox_tools.write_office_file.invoke({
            "filepath": "test.txt",
            "content": "x" * (sandbox_tools.MAX_WRITE_CHARS + 1),
            "mode": "w",
        })
        self.assertIn("权限拒绝", oversized)
        self.assertFalse((self.office / "test.txt").exists())

    def test_write_cannot_modify_trusted_skills(self):
        result = sandbox_tools.write_office_file.invoke({
            "filepath": "skills/demo/scripts/injected.py",
            "content": "print('unsafe')",
            "mode": "w",
        })
        self.assertIn("只读受信任目录", result)
        self.assertFalse((self.script.parent / "injected.py").exists())

    @patch.object(sandbox_tools.subprocess, "run")
    def test_shell_uses_argument_list_and_sanitized_environment(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(["ls"], 0, "demo.py\n", "")
        result = sandbox_tools.execute_office_shell.invoke({"command": "ls -la skills"})

        self.assertIn("demo.py", result)
        arguments = mock_run.call_args.args[0]
        options = mock_run.call_args.kwargs
        self.assertEqual(arguments, ["ls", "-la", "skills"])
        self.assertIs(options["shell"], False)
        self.assertEqual(options["cwd"], str(self.office.resolve()))
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertEqual(options["env"]["HOME"], str(self.office.resolve()))

    def test_shell_rejects_non_allowlisted_and_shell_syntax(self):
        blocked = [
            "rm -rf notes",
            "cat /etc/passwd",
            "ls ~",
            "ls ../",
            "echo ok > output.txt",
            "echo ok | cat",
            "echo $(whoami)",
            "pwd; id",
            "bash -c 'id'",
            "python skills/demo/scripts/demo.py",
            "python -c 'import os'",
            "python -m http.server",
        ]
        with patch.object(sandbox_tools.subprocess, "run") as mock_run:
            for command in blocked:
                with self.subTest(command=command):
                    result = sandbox_tools.execute_office_shell.invoke({"command": command})
                    self.assertTrue(
                        "权限拒绝" in result or "越权拦截" in result,
                        result,
                    )
            mock_run.assert_not_called()

    @patch.object(sandbox_tools.subprocess, "run")
    def test_dynamic_skill_allows_its_own_python_script_only(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, "skill ok\n", "")
        command = "python skills/demo/scripts/demo.py --name test"
        result = sandbox_tools.execute_dynamic_skill(command, "demo")

        self.assertIn("skill ok", result)
        arguments = mock_run.call_args.args[0]
        self.assertEqual(arguments[0], sys.executable)
        self.assertEqual(Path(arguments[1]), self.script.resolve())
        self.assertEqual(arguments[2:], ["--name", "test"])

    def test_shell_rejects_python_outside_skills_and_escaping_arguments(self):
        outside_script = self.office / "outside.py"
        outside_script.write_text("print('outside')\n", encoding="utf-8")
        misplaced_script = self.skills / "demo" / "misplaced.py"
        misplaced_script.write_text("print('misplaced')\n", encoding="utf-8")
        with patch.object(sandbox_tools.subprocess, "run") as mock_run:
            outside_result = sandbox_tools.execute_dynamic_skill("python outside.py", "demo")
            escape_result = sandbox_tools.execute_dynamic_skill(
                "python skills/demo/scripts/demo.py --file ../../secret", "demo"
            )
            misplaced_result = sandbox_tools.execute_dynamic_skill(
                "python skills/demo/misplaced.py", "demo"
            )
            wrong_skill_result = sandbox_tools.execute_dynamic_skill(
                "python skills/demo/scripts/demo.py", "another-skill"
            )
            self.assertIn("权限拒绝", outside_result)
            self.assertIn("权限拒绝", escape_result)
            self.assertIn("权限拒绝", misplaced_result)
            self.assertIn("权限拒绝", wrong_skill_result)
            mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
