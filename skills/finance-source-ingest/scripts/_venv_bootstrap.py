"""
首次运行 ingest 时：若无 .venv 或当前非 venv 内解释器，则自动建 venv、pip install -r，再 os.execv 到 venv 的 python。
仅依赖 stdlib。可通过环境变量关闭（见 ensure_venv_and_reexec  docstring）。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ENV_SKIP = "FINANCE_INGEST_SKIP_VENV"
_ENV_DISABLE = "FINANCE_INGEST_NO_AUTO_VENV"


def _finance_root(ingest_py: Path) -> Path:
    return ingest_py.resolve().parent.parent


def _venv_python(finance_root: Path) -> Path:
    if sys.platform == "win32":
        p = finance_root / ".venv" / "Scripts" / "python.exe"
    else:
        p = finance_root / ".venv" / "bin" / "python"
    return p


def ensure_venv_and_reexec(ingest_py: Path) -> None:
    """
    在导入 pipeline/akshare 之前调用。若已设置 FINANCE_INGEST_SKIP_VENV=1 则 no-op（防止 exec 后循环）。

    关闭自动建环境：export FINANCE_INGEST_NO_AUTO_VENV=1
    """
    if os.environ.get(_ENV_DISABLE) == "1":
        return
    if os.environ.get(_ENV_SKIP) == "1":
        return

    fin = _finance_root(ingest_py)
    vpy = _venv_python(fin)
    req = fin / "requirements.txt"

    try:
        this = Path(sys.executable).resolve()
    except (OSError, ValueError):
        this = Path(sys.executable)
    if vpy.is_file() and this == vpy.resolve():
        return

    if not vpy.is_file():
        if not req.is_file():
            print(
                f"无法自举 venv：缺少 {req}",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            subprocess.check_call(
                [sys.executable, "-m", "venv", str(fin / ".venv")],
                cwd=str(fin),
            )
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"[finance-source-ingest] 创建 venv 失败: {e}", file=sys.stderr)
            sys.exit(2)
        if not vpy.is_file():
            print("[finance-source-ingest] 创建 venv 后未找到解释器: " + str(vpy), file=sys.stderr)
            sys.exit(2)
        try:
            subprocess.check_call(
                [str(vpy), "-m", "pip", "install", "-U", "pip", "setuptools", "wheel"],
                cwd=str(fin),
            )
            subprocess.check_call(
                [str(vpy), "-m", "pip", "install", "-r", str(req)],
                cwd=str(fin),
            )
        except subprocess.CalledProcessError as e:
            print(f"[finance-source-ingest] pip install 失败: {e}", file=sys.stderr)
            sys.exit(2)

    # 已存在 venv 但未用其解释器：直接切换
    env = {**os.environ, _ENV_SKIP: "1"}
    script = str(ingest_py.resolve())
    argv = [str(vpy), script, *sys.argv[1:]]
    os.execve(str(vpy), argv, env)
