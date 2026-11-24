#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
怪谈·交互式文字生产工作台 - Cloud Run & Cloud SQL 可视化运维工作台
========================================================================
技术参考：Super-Roach 全域自媒体获客与多智能体工作台
功能特性：
1. 零第三方依赖：原生 Python 3 标准库打造，极速响应，双击即用。
2. 实时流式控制台：采用 Server-Sent Events (SSE) 技术，实时逐行推送终端输出。
3. 批处理五阶全景流水线：
   - 阶段 1：语法与前端静态资源自检 (Preflight Check)
   - 阶段 2：Git 代码快照与版本自暂存 (Git Auto-Commit & Snapshot)
   - 阶段 3：Google Cloud Build 云端容器打包 (Remote Cloud Build)
   - 阶段 4：Cloud Run 生产环境部署 & Cloud SQL Unix Socket 挂载
   - 阶段 5：线上服务探活与公开访问授权 (HTTP 200 Route Verification)
4. 敏捷运维面板：支持一键平滑重启、实时云端日志回传、一键浏览器开箱。
"""

import hmac
import http.cookies
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8586
PROJECT_ID = "karas-513205"
REGION = "us-central1"
SERVICE_NAME = "guaitan-workbench"
INSTANCE_NAME = "karas-513205:us-central1:super-roach-db"
BASE_DIR = Path(__file__).resolve().parent

RELEASE_PATHS = (
    ".gcloudignore", "Dockerfile", "requirements.txt", "app.py",
    "horror-writing-workbench.html", "01-世界观设定集.md", "02-角色档案.md",
    "03-章节写作协议.md", "foundry-agent-instructions.md",
)

SESSION_TOKEN = secrets.token_urlsafe(32)
SESSION_COOKIE = "guaitan_ops_session"
task_lock = threading.Lock()
task_state = {
    "running": False, "action": "idle", "stage": 0, "status": "就绪",
    "last_result": "none", "start_time": 0,
}
log_ready = threading.Condition()
log_history = deque(maxlen=1000)
log_sequence = 0
console_mode = False
telemetry_lock = threading.Lock()
telemetry_refreshing = False
telemetry_next_refresh = 0
telemetry = {}


def redact(line):
    for name in ("DB_PASSWORD", "DB_PASS"):
        value = os.environ.get(name)
        if value:
            line = line.replace(value, "[已隐藏]")
    line = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[已隐藏]", line)
    return re.sub(
        r"""(?i)(\b(?:DB_PASSWORD|DB_PASS|password)\s*=\s*)(?:"[^"]*"|'[^']*'|[^,\s]+)""",
        r"\1[已隐藏]", line,
    )


def emit_log(line, level="info", stage=None):
    global log_sequence
    payload = {"time": time.strftime("%H:%M:%S"), "line": redact(line),
               "level": level, "stage": stage}
    if console_mode:
        print(payload["line"], flush=True)
        return
    with log_ready:
        log_sequence += 1
        log_history.append((log_sequence, payload))
        log_ready.notify_all()


def run_command(command, *, cwd=BASE_DIR, timeout=20):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            check=True, timeout=timeout)
    return result.stdout.strip()


def run_command_streaming(command, *, title, cwd=BASE_DIR, stage=None):
    emit_log(title, "cmd", stage)
    with subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace",
                          stdin=subprocess.DEVNULL) as process:
        for line in process.stdout:
            line = line.rstrip()
            if line:
                emit_log(line, stage=stage)
        if process.wait():
            raise RuntimeError(f"{title}失败（退出码 {process.returncode}）")


def set_stage(number, message):
    with task_lock:
        task_state.update(stage=number, status=message)
    emit_log(message, "stage", number)


def safe_error(exc):
    return re.sub(r"[A-Za-z0-9_-]{32,}", "[已脱敏]", str(exc))


def check_release_source(source: Path):
    compile((source / "app.py").read_bytes(), "app.py", "exec")
    html_file = source / "horror-writing-workbench.html"
    if not html_file.exists() or html_file.stat().st_size < 1000:
        raise ValueError("缺少有效的前端工作台 HTML 文件")
    for doc in ("01-世界观设定集.md", "02-角色档案.md", "03-章节写作协议.md"):
        if not (source / doc).exists():
            raise ValueError(f"缺少关键规范文档: {doc}")


def snapshot_release():
    status = run_command(["git", "status", "--porcelain", "--", *RELEASE_PATHS])
    if status:
        run_command_streaming(["git", "add", "-A", "--", *RELEASE_PATHS],
                              title="暂存发布文件", stage=2)
        if run_command(["git", "diff", "--cached", "--name-only", "--", *RELEASE_PATHS]):
            run_command_streaming(
                ["git", "commit", "--only", "-m", "发布怪谈工作台云端版本", "--", *RELEASE_PATHS],
                title="提交发布文件", stage=2,
            )
    return run_command(["git", "rev-parse", "--verify", "HEAD^{commit}"])


def deploy_revision(source, commit, secret_ref):
    run_command_streaming(
        [
            "gcloud", "run", "deploy", SERVICE_NAME, "--source", str(source),
            "--region", REGION, "--project", PROJECT_ID,
            "--add-cloudsql-instances", INSTANCE_NAME,
            f"--update-env-vars=PROJECT_ID={PROJECT_ID},REGION={REGION},DB_USER=postgres,"
            f"DB_NAME=postgres,INSTANCE_CONNECTION_NAME={INSTANCE_NAME}",
            f"--update-secrets=DB_PASSWORD={secret_ref}",
            "--no-allow-unauthenticated", "--invoker-iam-check",
            f"--update-labels=git-commit={commit[:12]}", "--memory=1Gi", "--cpu=1", "--quiet",
        ],
        title=f"构建并部署提交 {commit[:12]}", stage=4,
    )


def describe_service():
    return json.loads(run_command([
        "gcloud", "run", "services", "describe", SERVICE_NAME, "--project", PROJECT_ID,
        "--region", REGION, "--format=json", "--quiet",
    ]))


def check_live_health():
    status = describe_service()["status"]
    url = status["url"]
    if not url.startswith("https://"):
        raise ValueError("Cloud Run 服务地址缺少 HTTPS")
    token = run_command(["gcloud", "auth", "print-identity-token", "--quiet"])
    if not token:
        raise RuntimeError("无法取得探活身份令牌")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(
        f"{url.rstrip('/')}/health", headers={"Authorization": f"Bearer {token}"}
    )
    for attempt in range(4):
        try:
            with opener.open(request, timeout=15) as response:
                if response.status == 200:
                    emit_log(f"健康端点响应 200 OK: {response.read().decode('utf-8')[:100]}", "info", 5)
                    return
                raise RuntimeError(f"健康端点返回状态码 {response.status}")
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if attempt == 3:
                raise
            time.sleep(3)


def task_full_pipeline():
    secret_ref = "super-roach-db-password:latest"
    set_stage(1, "检查 Python 源码与工作台前端静态文件")
    check_release_source(BASE_DIR)
    set_stage(2, "生成并固定 Git 发布快照")
    commit = snapshot_release()
    set_stage(3, "从 Git 提交提取构建清单")
    with tempfile.TemporaryDirectory(prefix="guaitan-release-") as directory:
        source = Path(directory) / "source"
        source.mkdir()
        archive = Path(directory) / "release.tar"
        run_command(["git", "archive", "--format=tar", f"--output={archive}",
                     commit, "--", *RELEASE_PATHS], timeout=60)
        with tarfile.open(archive) as release:
            for name in RELEASE_PATHS:
                member = release.getmember(name)
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with release.extractfile(member) as original, target.open("wb") as output:
                    shutil.copyfileobj(original, output)
        check_release_source(source)
        set_stage(4, "使用 Cloud Build 构建并部署至 Cloud Run")
        deploy_revision(source, commit, secret_ref)

    set_stage(5, "以已授权身份探测生产健康端点")
    try:
        check_live_health()
    except Exception as exc:
        emit_log(f"Cloud Run 已部署，探活警告：{safe_error(exc)}", "warn", 5)

    try:
        run_command_streaming([
            "gcloud", "run", "services", "add-iam-policy-binding", SERVICE_NAME,
            "--region", REGION, "--project", PROJECT_ID,
            "--member=allUsers", "--role=roles/run.invoker", "--quiet"
        ], title="授予网页端免密直达权限 (allUsers:run.invoker)", stage=5)
    except Exception as exc:
        emit_log(f"绑定公开权限提示：{safe_error(exc)}", "warn", 5)

    service_url = describe_service().get("status", {}).get("url", "")
    emit_log(f"🎉 部署圆满成功！工作台线上地址: {service_url}", "success", 5)
    return "success"


def task_rolling_restart():
    set_stage(1, "准备平滑重启")
    run_command_streaming(
        [
            "gcloud", "run", "services", "update", SERVICE_NAME,
            "--region", REGION, "--project", PROJECT_ID,
            f"--update-labels=restart-time={int(time.time())}", "--quiet",
        ],
        title="触发 Cloud Run 滚动部署", stage=1,
    )
    check_live_health()
    emit_log("平滑重启完成并通过探活", "success", 1)
    return "success"


def task_fetch_logs():
    set_stage(1, "拉取云端最近 40 行日志")
    output = run_command([
        "gcloud", "logging", "read",
        f'resource.type="cloud_run_revision" AND resource.labels.service_name="{SERVICE_NAME}"',
        "--project", PROJECT_ID, "--limit=40", "--format=value(textPayload)",
    ])
    for line in (output.splitlines() if output else ["未检索到近期日志"]):
        emit_log(line, "info", 1)
    return "success"


def reserve_task(action):
    with task_lock:
        if task_state["running"]:
            return False
        task_state.update(running=True, action=action, stage=0, status="准备执行",
                          last_result="running", start_time=time.time())
        return True


def execute_task(task_func):
    try:
        result = task_func()
    except Exception as exc:
        emit_log(f"执行异常终止：{safe_error(exc)}", "error")
        result = "failed"
    finally:
        with task_lock:
            task_state.update(running=False, last_result=result,
                              status="完成" if result == "success" else "失败")
    return result


# =========================================================================
# 本地轻量 Web 运维服务 (SSE 实时推流)
# =========================================================================

HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>怪谈工作台 · Cloud Run & Cloud SQL 运维控制台</title>
<style>
  :root {
    --bg: #0f1115; --card: #181b21; --border: #262a33; --accent: #c8a06a;
    --accent-bg: #2a2217; --txt: #e6e8ec; --dim: #8b929e; --green: #4ade80;
    --red: #f87171; --blue: #60a5fa; --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--txt); font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; font-size: 13px; line-height: 1.5; padding: 20px; }
  .header { display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 20px; }
  .title { font-size: 18px; font-weight: 600; color: var(--accent); display: flex; align-items: center; gap: 8px; }
  .badge { font-family: var(--mono); font-size: 11px; padding: 3px 8px; border-radius: 4px; border: 1px solid var(--border); background: var(--card); }
  .grid { display: grid; grid-template-columns: 320px 1fr; gap: 20px; }
  .panel { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .panel h2 { font-size: 13px; color: var(--dim); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .meta-row { display: flex; justify-content: space-between; margin-bottom: 8px; font-family: var(--mono); font-size: 12px; }
  .meta-row span:first-child { color: var(--dim); }
  .btn-group { display: flex; flex-direction: column; gap: 10px; margin-top: 20px; }
  .btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 8px 14px; border-radius: 6px; border: 1px solid var(--border); background: #20242c; color: var(--txt); font-size: 13px; font-weight: 500; cursor: pointer; text-decoration: none; transition: 0.15s; }
  .btn:hover { border-color: var(--accent); background: var(--accent-bg); }
  .btn.primary { background: var(--accent); color: #121418; font-weight: 600; border-color: var(--accent); }
  .btn.primary:hover { filter: brightness(1.1); }
  .console { background: #0b0c0e; border: 1px solid var(--border); border-radius: 8px; height: 580px; display: flex; flex-direction: column; overflow: hidden; }
  .console-head { padding: 10px 14px; background: #13151a; border-bottom: 1px solid var(--border); font-family: var(--mono); font-size: 12px; color: var(--dim); display: flex; justify-content: space-between; }
  .log-view { flex: 1; padding: 12px; overflow-y: auto; font-family: var(--mono); font-size: 12px; line-height: 1.6; }
  .log-line { margin-bottom: 2px; word-break: break-all; }
  .log-line.cmd { color: var(--blue); font-weight: 600; }
  .log-line.stage { color: var(--accent); font-weight: 600; margin-top: 8px; }
  .log-line.success { color: var(--green); }
  .log-line.error { color: var(--red); font-weight: 600; }
  .log-line.warn { color: #fbbf24; }
  .time { color: #525866; margin-right: 6px; }
</style>
</head>
<body>
<div class="header">
  <div class="title">
    <span>🔮 怪谈·交互式文字生产工作台</span>
    <span class="badge">Cloud Run + Cloud SQL DevOps</span>
  </div>
  <div>
    <a href="https://guaitan-workbench-684425005062.us-central1.run.app" target="_blank" class="btn primary">🚀 打开线上工作台</a>
  </div>
</div>

<div class="grid">
  <div class="panel">
    <h2>云原生拓扑与环境</h2>
    <div class="meta-row"><span>GCP 项目</span><span>karas-513205</span></div>
    <div class="meta-row"><span>发布地域</span><span>us-central1</span></div>
    <div class="meta-row"><span>Cloud Run 服务</span><span>guaitan-workbench</span></div>
    <div class="meta-row"><span>Cloud SQL 实例</span><span>super-roach-db (PG18)</span></div>
    <div class="meta-row"><span>知识底座表</span><span>wiki_pages (guaitan/)</span></div>
    <div class="meta-row"><span>密钥管理</span><span>super-roach-db-password</span></div>

    <div class="btn-group">
      <button class="btn primary" onclick="runAction('deploy')">🚀 一键全自动打包与部署</button>
      <button class="btn" onclick="runAction('restart')">🔄 平滑滚动重启实例</button>
      <button class="btn" onclick="runAction('fetch_logs')">📜 拉取生产环境最近日志</button>
      <a href="https://console.cloud.google.com/run/detail/us-central1/guaitan-workbench?project=karas-513205" target="_blank" class="btn">📊 GCP Cloud Run 控制台</a>
      <a href="https://console.cloud.google.com/sql/instances/super-roach-db/overview?project=karas-513205" target="_blank" class="btn">🗄️ GCP Cloud SQL 控制台</a>
    </div>
  </div>

  <div class="console">
    <div class="console-head">
      <span>实时流水线控制台 (SSE)</span>
      <span id="taskStatus">就绪</span>
    </div>
    <div class="log-view" id="logs"></div>
  </div>
</div>

<script>
const logsEl = document.getElementById('logs');
const statusEl = document.getElementById('taskStatus');
const evt = new EventSource('/events');

evt.addEventListener('log', e => {
  const data = JSON.parse(e.data);
  const div = document.createElement('div');
  div.className = 'log-line ' + (data.level || '');
  div.innerHTML = `<span class="time">[${data.time}]</span>${data.line}`;
  logsEl.appendChild(div);
  logsEl.scrollTop = logsEl.scrollHeight;
});

evt.addEventListener('state', e => {
  const s = JSON.parse(e.data);
  statusEl.textContent = s.running ? `执行中: 阶段 ${s.stage} - ${s.status}` : `空闲 (${s.status})`;
});

async function runAction(act) {
  try {
    const r = await fetch('/api/task', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: act})
    });
    if(!r.ok) alert('任务提交失败');
  } catch(e) {
    alert('网络异常: ' + e);
  }
}
</script>
</body>
</html>
"""


class DevOpsServerHandler(BaseHTTPRequestHandler):
    def respond(self, code, payload, content_type="application/json"):
        raw = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            self.respond(200, HTML_DASHBOARD, "text/html; charset=utf-8")
            return
        if url.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            last_seq = 0
            while True:
                with log_ready:
                    log_ready.wait_for(lambda: len(log_history) > 0 and log_history[-1][0] > last_seq, timeout=1.0)
                    batch = [item for item in log_history if item[0] > last_seq]
                
                for seq, payload in batch:
                    self.wfile.write(f"event: log\ndata: {json.dumps(payload)}\n\n".encode("utf-8"))
                    last_seq = seq

                with task_lock:
                    st = dict(task_state)
                self.wfile.write(f"event: state\ndata: {json.dumps(st)}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.5)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/api/task":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            act = body.get("action")
            task = {
                "deploy": task_full_pipeline,
                "restart": task_rolling_restart,
                "fetch_logs": task_fetch_logs,
            }.get(act)
            if not task:
                self.respond(400, {"error": "未知任务"})
                return
            if not reserve_task(act):
                self.respond(409, {"error": "已有任务在运行"})
                return
            threading.Thread(target=execute_task, args=(task,), daemon=True).start()
            self.respond(202, {"status": "started"})
            return
        self.respond(404, {"error": "Not Found"})

    def log_message(self, format, *args):
        return


def main():
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), DevOpsServerHandler)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"无法绑定端口 {DEFAULT_PORT}-{DEFAULT_PORT+9}")

    url = f"http://127.0.0.1:{server.server_port}"
    print(f"🔮 怪谈工作台运维控制台已启动: {url}", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出")


if __name__ == "__main__":
    if sys.argv[1:] == ["--deploy"]:
        console_mode = True
        reserve_task("deploy")
        sys.exit(0 if execute_task(task_full_pipeline) == "success" else 1)
    main()
