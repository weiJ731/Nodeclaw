from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .base import nodeclaw_tool
from ..config import OFFICE_DIR


SYS_OS = platform.system()
MAX_READ_CHARS = 10_000
MAX_WRITE_CHARS = 100_000
MAX_COMMAND_CHARS = 4_096
MAX_COMMAND_ARGS = 100
MAX_OUTPUT_CHARS = 2_000
SAFE_COMMANDS = {"echo", "ls", "pwd"}
SHELL_SYNTAX = re.compile(r"(?:&&|\|\||[|;<>`]|\$\(|\r|\n|\x00)")
WINDOWS_ABSOLUTE_PATH = re.compile(r"^[a-zA-Z]:[\\/]")


def _office_root() -> Path:
    return Path(OFFICE_DIR).expanduser().resolve(strict=True)


def _get_safe_path(relative_path: str, *, must_exist: bool = False) -> str:
    """Resolve an Office-relative path without permitting symlink or path escape."""
    if not isinstance(relative_path, str) or "\x00" in relative_path:
        raise PermissionError("越权拦截：文件路径无效。")

    raw_path = Path(relative_path)
    if (
        raw_path.is_absolute()
        or relative_path == "~"
        or relative_path.startswith(("~/", "~\\"))
        or WINDOWS_ABSOLUTE_PATH.match(relative_path)
    ):
        raise PermissionError("越权拦截：只能使用 office 工位内的相对路径。")
    if ".." in raw_path.parts or ".." in Path(relative_path.replace("\\", "/")).parts:
        raise PermissionError("越权拦截：禁止使用父目录跳转。")

    root = _office_root()
    lexical_target = root.joinpath(raw_path)

    cursor = root
    for part in raw_path.parts:
        if part in {"", "."}:
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            raise PermissionError(f"越权拦截：路径包含符号链接 '{relative_path}'。")

    target = lexical_target.resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError(
            f"越权拦截：路径 '{relative_path}' 超出 office 工位。"
        ) from exc

    if must_exist and not target.exists():
        raise FileNotFoundError(f"路径不存在：{relative_path}")
    return str(target)


def _open_text(path: str, mode: str):
    flags = os.O_RDONLY if mode == "r" else os.O_WRONLY | os.O_CREAT
    if mode == "w":
        flags |= os.O_TRUNC
    elif mode == "a":
        flags |= os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    return os.fdopen(descriptor, mode, encoding="utf-8")


def _contains_path_escape(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        normalized.startswith(("/", "~/"))
        or WINDOWS_ABSOLUTE_PATH.match(value) is not None
        or ".." in Path(normalized).parts
        or "\x00" in value
    )


def _parse_safe_command(command: str, *, allowed_skill: str | None = None) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise PermissionError("权限拒绝：命令不能为空。")
    if len(command) > MAX_COMMAND_CHARS:
        raise PermissionError("权限拒绝：命令长度超过安全限制。")
    if SHELL_SYNTAX.search(command):
        raise PermissionError("权限拒绝：不允许管道、重定向、命令拼接或命令替换。")

    try:
        arguments = shlex.split(command, posix=SYS_OS != "Windows")
    except ValueError as exc:
        raise PermissionError(f"权限拒绝：命令格式不合法：{exc}") from exc
    if not arguments or len(arguments) > MAX_COMMAND_ARGS:
        raise PermissionError("权限拒绝：命令参数数量不合法。")

    executable = arguments[0].lower()
    if "/" in executable or "\\" in executable:
        raise PermissionError("权限拒绝：不允许直接指定可执行文件路径。")

    if executable in {"python", "python3"}:
        if not allowed_skill:
            raise PermissionError("权限拒绝：Python 脚本只能通过对应的动态技能执行。")
        if len(arguments) < 2 or arguments[1].startswith("-"):
            raise PermissionError("权限拒绝：Python 只能运行 skills 目录中的脚本。")
        script = Path(_get_safe_path(arguments[1], must_exist=True))
        skills_root = Path(_get_safe_path("skills", must_exist=True))
        try:
            relative_script = script.relative_to(skills_root)
        except ValueError as exc:
            raise PermissionError("权限拒绝：Python 脚本必须位于 office/skills 目录中。") from exc
        if (
            len(relative_script.parts) != 3
            or relative_script.parts[0] != allowed_skill
            or relative_script.parts[1] != "scripts"
            or script.suffix.lower() != ".py"
            or not script.is_file()
        ):
            raise PermissionError(
                "权限拒绝：只能运行 skills/<技能名>/scripts 目录中的 Python 文件。"
            )
        if any(_contains_path_escape(value) for value in arguments[2:]):
            raise PermissionError("权限拒绝：脚本参数包含越界路径。")
        return [sys.executable, str(script), *arguments[2:]]

    if executable not in SAFE_COMMANDS:
        allowed = ", ".join(sorted(SAFE_COMMANDS))
        raise PermissionError(f"权限拒绝：命令 '{arguments[0]}' 不在白名单中。允许：{allowed}。")

    if executable == "pwd" and len(arguments) != 1:
        raise PermissionError("权限拒绝：pwd 不接受参数。")
    if executable == "ls":
        for value in arguments[1:]:
            if value.startswith("-"):
                if not re.fullmatch(r"-[1aAhl]+", value):
                    raise PermissionError(f"权限拒绝：ls 参数 '{value}' 不在白名单中。")
                continue
            _get_safe_path(value, must_exist=True)
    return arguments


def _safe_environment() -> dict[str, str]:
    root = _office_root()
    python_bin = str(Path(sys.executable).resolve().parent)
    system_paths = [python_bin, "/usr/bin", "/bin"] if os.name != "nt" else [python_bin]
    return {
        "HOME": str(root),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": os.pathsep.join(dict.fromkeys(system_paths)),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }


@nodeclaw_tool
def list_office_files(sub_dir: str = "") -> str:
    """列出 office 工位内指定相对目录的文件。"""
    try:
        target_dir = Path(_get_safe_path(sub_dir, must_exist=True))
        if not target_dir.is_dir():
            return f"不是目录：{sub_dir}"
        items = sorted(target_dir.iterdir(), key=lambda item: item.name.lower())
        if not items:
            return f"[{sub_dir if sub_dir else 'office 根目录'}] 是空的。"
        result = []
        for item in items[:500]:
            item_type = "链接" if item.is_symlink() else "目录" if item.is_dir() else "文件"
            result.append(f"[{item_type}] {item.name}")
        if len(items) > 500:
            result.append(f"...[另有 {len(items) - 500} 项未显示]")
        return "\n".join(result)
    except Exception as exc:
        return str(exc)


@nodeclaw_tool
def read_office_file(filepath: str) -> str:
    """读取 office 工位内的 UTF-8 文本文件，内容最多返回 10000 字符。"""
    try:
        target_path = _get_safe_path(filepath, must_exist=True)
        if not Path(target_path).is_file():
            return f"不是文件：{filepath}"
        with _open_text(target_path, "r") as file:
            content = file.read(MAX_READ_CHARS + 1)
        if len(content) > MAX_READ_CHARS:
            return content[:MAX_READ_CHARS] + "\n\n...[内容过长，已被安全截断]..."
        return content
    except Exception as exc:
        return str(exc)


@nodeclaw_tool
def write_office_file(filepath: str, content: str, mode: str = "w") -> str:
    """在 office 工位内新建、覆盖或追加 UTF-8 文本文件。"""
    try:
        if mode not in {"w", "a"}:
            return "错误：mode 参数必须是 'w'（覆盖）或 'a'（追加）。"
        if not isinstance(content, str) or len(content) > MAX_WRITE_CHARS:
            return f"权限拒绝：单次写入不能超过 {MAX_WRITE_CHARS} 字符。"

        target_path = Path(_get_safe_path(filepath))
        skills_root = _office_root() / "skills"
        try:
            target_path.relative_to(skills_root)
        except ValueError:
            pass
        else:
            return "权限拒绝：skills 是只读受信任目录，不能通过 Agent 修改。"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        parent_relative = target_path.parent.relative_to(_office_root()).as_posix()
        _get_safe_path(parent_relative, must_exist=True)
        if target_path.exists() and not target_path.is_file():
            return f"不是文件：{filepath}"

        with _open_text(str(target_path), mode) as file:
            if mode == "a" and content and not content.startswith("\n"):
                file.write("\n")
            file.write(content)
        action = "覆盖/新建" if mode == "w" else "追加"
        return f"成功以{action}模式写入文件：{filepath}（{len(content)} 字符）"
    except Exception as exc:
        return str(exc)


def _execute_command(command: str, *, allowed_skill: str | None = None) -> str:
    try:
        arguments = _parse_safe_command(command, allowed_skill=allowed_skill)
        result = subprocess.run(
            arguments,
            shell=False,
            cwd=str(_office_root()),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=_safe_environment(),
            check=False,
        )

        output = [
            f"当前系统：{SYS_OS}",
            f"执行命令：{command}",
            f"退出码：{result.returncode}",
        ]
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if stdout:
            output.append(f"[STDOUT]\n{stdout[-MAX_OUTPUT_CHARS:]}")
        if stderr:
            output.append(f"[STDERR]\n{stderr[-MAX_OUTPUT_CHARS:]}")
        if not stdout and not stderr:
            output.append("执行完毕，无终端输出。")
        return "\n".join(output)
    except PermissionError as exc:
        return str(exc)
    except subprocess.TimeoutExpired:
        return "执行超时：命令超过 60 秒，已终止。"
    except Exception as exc:
        return f"执行异常：{exc}"


def execute_dynamic_skill(command: str, skill_folder: str) -> str:
    """Execute a trusted script belonging to one specific dynamic skill."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", skill_folder):
        return "权限拒绝：动态技能目录名称无效。"
    return _execute_command(command, allowed_skill=skill_folder)


@nodeclaw_tool
def execute_office_shell(command: str) -> str:
    """
    在 office 工位中执行受限的只读辅助命令。

    仅允许 pwd、ls 和 echo。动态技能脚本只能经对应技能的 run 阶段执行。
    不支持管道、重定向、命令拼接、命令替换或任意解释器代码。
    """
    return _execute_command(command)
