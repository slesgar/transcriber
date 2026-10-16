"""Установщики: префикс меток launchd и bundle id берутся из установки, а не из кода."""
import subprocess

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "install-lib.sh"


def resolve(tmp_path, env_prefix=None, file_line=None):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    if file_line is not None:
        (data / "install.env").write_text(f"# установка\n{file_line}\n", encoding="utf-8")
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "TRANSCRIBER_DATA": str(data)}
    if env_prefix is not None:
        env["TRANSCRIBER_LABEL_PREFIX"] = env_prefix
    return subprocess.run(["bash", "-c", f'set -euo pipefail; ROOT="{ROOT}"; source "{LIB}"; echo "$LABEL_PREFIX"'],
                          env=env, capture_output=True, text=True)


def test_default_prefix_is_neutral(tmp_path):
    assert resolve(tmp_path).stdout.strip() == "local.transcriber"


def test_prefix_from_install_env_in_data_dir(tmp_path):
    assert resolve(tmp_path, file_line='LABEL_PREFIX="org.example.transcriber"').stdout.strip() == "org.example.transcriber"


def test_environment_beats_install_env(tmp_path):
    out = resolve(tmp_path, env_prefix="com.acme.rec", file_line="LABEL_PREFIX=org.example.transcriber")
    assert out.stdout.strip() == "com.acme.rec"


def test_bad_prefix_is_refused(tmp_path):
    out = resolve(tmp_path, env_prefix="bad prefix;rm")
    assert out.returncode != 0 and "LABEL_PREFIX" in out.stderr


def test_templates_render_without_placeholders(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "TRANSCRIBER_DATA": str(data),
           "TRANSCRIBER_LABEL_PREFIX": "org.example.transcriber"}
    script = (f'set -euo pipefail; ROOT="{ROOT}"; source "{LIB}"; '
              f'render_template "$ROOT/launchd/app.plist" "{tmp_path}/a.plist"; '
              f'render_template "$ROOT/launchd/worker.plist" "{tmp_path}/w.plist"; '
              f'render_template "$ROOT/app/Resources/Info.plist" "{tmp_path}/i.plist"')
    subprocess.run(["bash", "-c", script], env=env, check=True)
    app, worker, info = ((tmp_path / n).read_text(encoding="utf-8") for n in ("a.plist", "w.plist", "i.plist"))
    assert "<string>org.example.transcriber.app</string>" in app
    assert "<string>org.example.transcriber.worker</string>" in worker
    assert "<key>CFBundleIdentifier</key><string>org.example.transcriber</string>" in info
    for text in (app, worker, info):
        assert "__" not in text
    assert f"<string>{ROOT}/worker</string>" in worker and f"{tmp_path}/Applications/Transcriber.app" in app


@pytest.mark.parametrize("line", [
    "LABEL_PREFIX=org.example.transcriber",
    'LABEL_PREFIX="org.example.transcriber"',
    "LABEL_PREFIX = 'org.example.transcriber'",
    "  export LABEL_PREFIX=org.example.transcriber  ",
    "LABEL_PREFIX=org.example.transcriber\r",
    "LABEL_PREFIX=com.old.one\nLABEL_PREFIX=org.example.transcriber",
])
def test_install_env_forms(tmp_path, line):
    out = resolve(tmp_path, file_line=line)
    assert out.returncode == 0 and out.stdout.strip() == "org.example.transcriber", out.stderr


def test_commented_line_is_ignored(tmp_path):
    assert resolve(tmp_path, file_line="# LABEL_PREFIX=org.example.x").stdout.strip() == "local.transcriber"


def test_warns_when_installed_agent_has_other_prefix(tmp_path):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.old.transcriber.app.plist").write_text("<plist/>", encoding="utf-8")
    out = resolve(tmp_path)
    assert out.returncode == 0 and "com.old.transcriber.app.plist" in out.stderr
    assert "внимание" not in resolve(tmp_path, env_prefix="com.old.transcriber").stderr


def test_render_escapes_home(tmp_path):
    home = tmp_path / "a&b|c"
    home.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TRANSCRIBER_DATA": str(data)}
    script = f'set -euo pipefail; ROOT="{ROOT}"; source "{LIB}"; render_template "$ROOT/launchd/app.plist" "{tmp_path}/a.plist"'
    subprocess.run(["bash", "-c", script], env=env, check=True)
    text = (tmp_path / "a.plist").read_text(encoding="utf-8")
    assert f"{tmp_path}/a&amp;b|c/Applications/Transcriber.app" in text and "__HOME__" not in text
