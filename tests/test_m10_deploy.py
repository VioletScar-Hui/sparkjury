import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sparkjury.api import create_app

ROOT = Path(__file__).resolve().parents[1]
DGX = ROOT / "deploy" / "dgx"
SCRIPTS = ["common.sh", "setup_node.sh", "download_models.sh", "start_judges.sh", "status.sh", "stop_all.sh", "run_tau2.sh", "make_demo_bundle.sh", "apply_prompt_fix.sh", "full_loop.sh"]


# ---- API token ---------------------------------------------------------------------------

def test_api_token_required_when_set(tmp_path, monkeypatch):
    monkeypatch.delenv("SPARKJURY_API_TOKEN", raising=False)
    app = create_app(tmp_path / "runs", probe_endpoints=False, token="s3cret")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200 and c.get("/health").json()["auth"] is True
        assert c.get("/runs").status_code == 401
        assert c.get("/").status_code == 401
        assert c.get("/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/runs", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/runs?token=s3cret").status_code == 200
        assert c.get("/?token=s3cret").status_code == 200
        assert c.post("/runs", json={"demo": True}).status_code == 401


def test_api_token_from_env_and_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARKJURY_API_TOKEN", "envtok")
    with TestClient(create_app(tmp_path / "runs", probe_endpoints=False)) as c:
        assert c.get("/runs").status_code == 401 and c.get("/runs?token=envtok").status_code == 200
    monkeypatch.delenv("SPARKJURY_API_TOKEN")
    with TestClient(create_app(tmp_path / "runs", probe_endpoints=False)) as c:
        assert c.get("/runs").status_code == 200 and c.get("/health").json()["auth"] is False


def test_cockpit_page_forwards_token():
    html = (ROOT / "src" / "sparkjury" / "api" / "static" / "index.html").read_text(encoding="utf-8")
    assert "sparkjury_token" in html and 'new EventSource(withTok(' in html and "401" in html


# ---- deploy scripts ----------------------------------------------------------------------------

def test_deploy_files_exist():
    for s in SCRIPTS:
        assert (DGX / s).exists(), s
    assert (DGX / "env.example").exists() and (ROOT / "deploy" / "README.md").exists()


def _usable_bash():
    """找一个真能跑的 bash，找不到返回 None。

    Windows 上 `shutil.which("bash")` 找到的往往是 C:\\Windows\\System32\\bash.exe——那是 WSL 的
    启动桩，机器没装 WSL 时它只会吐一句「要装 WSL」（UTF-16，别指望 stderr 里有）并以非 0 退出。
    GitHub 的 windows runner 就是这样：bash -n 全线报错，看着像仓库脚本有语法问题，其实是找错了 bash。
    所以这里真跑一句再认。
    """
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates += [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
        ]
    for exe in candidates:
        if not exe or not Path(exe).exists():
            continue
        try:
            probe = subprocess.run([exe, "-c", "echo sparkjury-bash-ok"], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and "sparkjury-bash-ok" in probe.stdout:
            return exe
    return None


BASH = _usable_bash()


@pytest.mark.skipif(BASH is None, reason="没有可用的 bash（Windows 上常见：只有 WSL 启动桩、没装 Git Bash）")
@pytest.mark.parametrize("script", [s for s in SCRIPTS if s != "common.sh"])
def test_scripts_pass_bash_syntax_check(script):
    r = subprocess.run([BASH, "-n", str(DGX / script)], capture_output=True, text=True)
    assert r.returncode == 0, f"{script} 语法检查没过：rc={r.returncode}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def test_env_example_covers_what_scripts_use():
    env = (DGX / "env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"^([A-Z_]+)=", env, re.M))
    for k in ["STEPFUN_API_KEY", "TYPESAFE_API_KEY", "SPARKJURY_API_TOKEN", "JUDGE_A_MODEL", "JUDGE_B_MODEL", "EMBED_MODEL", "AGENT_MODEL",
              "JUDGE_A_PORT", "JUDGE_B_PORT", "EMBED_PORT", "AGENT_PORT", "API_PORT", "JUDGE_A_MEM", "JUDGE_B_MEM", "EMBED_MEM", "AGENT_MEM"]:
        assert k in keys, k
    common = (DGX / "common.sh").read_text(encoding="utf-8")
    for k in keys - {"STEPFUN_API_KEY", "TYPESAFE_API_KEY", "SPARKJURY_API_TOKEN", "HF_ENDPOINT", "MODELS_DIR"}:
        assert k in common, f"{k} not defaulted in common.sh"


def test_ports_consistent_across_configs():
    env = (DGX / "env.example").read_text(encoding="utf-8")
    judges = (ROOT / "deploy" / "judges.example.toml").read_text(encoding="utf-8")
    run = (ROOT / "deploy" / "run.example.toml").read_text(encoding="utf-8")
    assert "JUDGE_A_PORT=8001" in env and "127.0.0.1:8001/v1" in judges
    assert "JUDGE_B_PORT=8002" in env and "127.0.0.1:8002/v1" in judges
    assert "EMBED_PORT=8003" in env and "127.0.0.1:8003/v1" in run
    assert "API_PORT=9000" in env
    # vLLM binds loopback only; the API is the only 0.0.0.0 listener
    start = (DGX / "start_judges.sh").read_text(encoding="utf-8")
    common = (DGX / "common.sh").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in common and "sparkjury serve --host 0.0.0.0" in start
    mems = [float(x) for x in re.findall(r"_MEM=([0-9.]+)", env)]
    assert sum(mems) < 0.85  # headroom on the 128 GB unified memory


def test_tau2_script_uses_different_models_for_agent_and_user():
    s = (DGX / "run_tau2.sh").read_text(encoding="utf-8")
    assert '--agent-llm "openai/$AGENT_MODEL"' in s and '--user-llm "openai/$JUDGE_A_MODEL"' in s
    assert "--num-trials" in s and "sparkjury ingest" in s


# ---- 公网服务必须有鉴权：从"警告一句"改成"拒绝启动" ------------------------------------------

def test_serve_refuses_public_bind_without_token(tmp_path, monkeypatch):
    """`serve --host 0.0.0.0` 没 token 时以前只打一句警告就照常起服务，日志里滚过去谁也没看见。"""
    import uvicorn
    from typer.testing import CliRunner

    from sparkjury.cli import app as cli_app

    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(a))
    monkeypatch.delenv("SPARKJURY_API_TOKEN", raising=False)
    runner = CliRunner(env={"COLUMNS": "200"})
    runs_dir = str(tmp_path / "runs")

    r = runner.invoke(cli_app, ["serve", "--host", "0.0.0.0", "--port", "9000", "--runs-dir", runs_dir])
    assert r.exit_code == 2, r.output
    assert "SPARKJURY_API_TOKEN" in r.output and "refusing" in r.output
    assert started == [], "拒绝启动时不该真的去 bind"

    r = runner.invoke(cli_app, ["serve", "--host", "0.0.0.0", "--port", "9000", "--token", "t", "--runs-dir", runs_dir])
    assert r.exit_code == 0 and len(started) == 1, r.output  # 给了 token 照常起

    started.clear()
    r = runner.invoke(cli_app, ["serve", "--host", "127.0.0.1", "--port", "9000", "--runs-dir", runs_dir])
    assert r.exit_code == 0 and len(started) == 1, r.output  # 本机回环不需要 token


@pytest.mark.skipif(BASH is None, reason="没有可用的 bash（Windows 上常见：只有 WSL 启动桩、没装 Git Bash）")
def test_start_judges_refuses_to_start_the_api_window_without_a_token(tmp_path):
    """部署脚本以前只 `warn` 一句就照常起 API 窗口。真跑一遍，看它是不是拦在动手之前。"""
    dgx = tmp_path / "deploy" / "dgx"
    dgx.mkdir(parents=True)
    for name in ("common.sh", "start_judges.sh"):
        shutil.copy(DGX / name, dgx / name)  # 复制到临时目录：那里没有 .env，结果不随开发机变化
    env = {k: v for k, v in os.environ.items() if k != "SPARKJURY_API_TOKEN"}
    env["SPARKJURY_API_TOKEN"] = ""
    env["HOME"] = str(tmp_path / "home")  # 免得命中开发机上的 ~/envs/vllm
    (tmp_path / "home").mkdir()

    # encoding 必须钉死成 UTF-8：Windows 上 text=True 会按 cp1252 解码，脚本里 die 的中文
    # 把读取线程打挂（UnicodeDecodeError → stdout 变 None），报出来的是无关的 TypeError。
    # 同一个坑本仓踩过，见 docs/ESSAY_十日谈.md 结尾。scripts/certificate.py 的 _run 也是这么写的。
    r = subprocess.run([BASH, str(dgx / "start_judges.sh")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=120)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "SPARKJURY_API_TOKEN" in out, out
    assert "using vLLM" not in out, "token 检查要拦在动任何东西之前（tmux、vLLM 都还没碰）"

    # --no-api 是留的正当出口：不起 API 就不要求 token，脚本继续往下走
    r = subprocess.run([BASH, str(dgx / "start_judges.sh"), "--no-api"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=120)
    out = r.stdout + r.stderr
    assert "SPARKJURY_API_TOKEN" not in out, out
    assert "vLLM not found" in out, out  # 走到下一步才停，说明上一条检查确实是条件性的
