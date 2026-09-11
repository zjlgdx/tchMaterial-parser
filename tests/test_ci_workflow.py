# -*- coding: utf-8 -*-
"""CI 配置与 README 的承诺对齐（C7）。

不引入 PyYAML（依赖白名单之外），按行做结构性检查——这些断言要挡的是
「矩阵漏了某个版本」「打包 job 忘了装 PyInstaller」这类遗漏。
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "python-app.yml")


def text():
    return open(WORKFLOW, encoding="utf-8").read()


def jobs():
    """按缩进切出各个 job 的正文。"""
    lines = text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("jobs:"))
    found, current = {}, None
    for line in lines[start + 1:]:
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            found[current] = []
        elif current:
            found[current].append(line)
    return {name: "\n".join(body) for name, body in found.items()}


def test_three_jobs_plus_release():
    names = set(jobs())
    assert {"lint-and-test", "build-windows", "build-linux", "release"} <= names, names


def test_matrix_covers_every_supported_version():
    body = jobs()["lint-and-test"]
    for version in ("3.10", "3.11", "3.12", "3.13"):
        assert '"%s"' % version in body, version


def test_lint_gate_is_blocking():
    body = jobs()["lint-and-test"]
    assert "--select=E9,F63,F7,F82" in body
    assert "--exit-zero" in body, "风格检查应当是非阻塞的"
    # 阻塞的那条不能带 --exit-zero
    blocking = [line for line in body.splitlines() if "--select=E9" in line]
    assert blocking and all("--exit-zero" not in line for line in blocking), blocking


def test_tests_and_version_check_run_in_ci():
    body = jobs()["lint-and-test"]
    assert "pytest -q" in body
    assert "gen_version_file.py --check" in body


def test_build_jobs_install_pyinstaller():
    """打包工具不在 requirements-dev.txt 里，两个 build job 必须各自安装。"""
    for name in ("build-windows", "build-linux"):
        body = jobs()[name]
        assert "pip install pyinstaller==" in body, name
        assert "pyinstaller tchMaterial-parser.spec" in body, name


def test_pyinstaller_version_is_pinned():
    match = re.search(r'PYINSTALLER_VERSION:\s*"(\d+\.\d+(\.\d+)?)"', text())
    assert match, "PyInstaller 版本没有钉死"


def install_lines(body):
    """只看真正会执行的安装命令，注释里提到什么不算数。"""
    return [line for line in body.splitlines()
            if "install" in line and not line.lstrip().startswith("#")]


def test_linux_build_installs_tk():
    assert any("python3-tk" in line for line in install_lines(jobs()["build-linux"]))


def test_lint_job_does_not_install_tk():
    """核心模块必须在没有 Tk 的环境里跑得通。"""
    lines = install_lines(jobs()["lint-and-test"])
    assert not any("python3-tk" in line for line in lines), lines


def test_both_platforms_upload_artifacts():
    for name, artifact in (("build-windows", "windows"), ("build-linux", "linux")):
        body = jobs()[name]
        assert "upload-artifact" in body, name
        assert artifact in body, name


def test_pyinstaller_is_not_a_dev_dependency():
    dev = open(os.path.join(REPO_ROOT, "requirements-dev.txt"), encoding="utf-8").read()
    assert "pyinstaller" not in dev.lower()
    assert "pytest" in dev and "flake8" in dev
