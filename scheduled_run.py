"""计划任务专用入口：静默读 .env -> 跑一次同步 -> 结果追加到 sync.log

为什么单独写一个：
- run.cmd / run.ps1 是给人手动用的，会闪窗口、有回显；
- 计划任务每 5 分钟跑一次，需要完全静默 + 留日志，所以用 pythonw.exe 跑这个文件。

原版 dida_todo_sync.py 依然一行未改。
"""

import datetime as dt
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, ".env")
LOG_FILE = os.path.join(HERE, "sync.log")
STATE_FILE = os.path.join(HERE, "sync_state.json")
MAX_LOG_BYTES = 2 * 1024 * 1024


def load_env(path):
    """极简 .env 解析：忽略注释与空行，去掉值两端引号。"""
    data = {}
    if not os.path.exists(path):
        return data
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val:
                data[key] = val
    return data


def rotate_log(path):
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            os.remove(path)
    except OSError:
        pass


def python_exe():
    """pythonw 下 sys.executable 是 pythonw.exe，子进程统一用 python.exe。"""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        candidate = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(candidate):
            return candidate
    return exe


def main():
    env = os.environ.copy()
    env.update(load_env(ENV_FILE))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    missing = [k for k in ("CLOUD_API_KEY", "DEVICE_ID", "DIDA_ICS_URL") if not env.get(k)]
    if missing:
        out, code = "缺少必填配置: " + ", ".join(missing), 1
    else:
        try:
            r = subprocess.run(
                [python_exe(), os.path.join(HERE, "dida_todo_sync.py"),
                 "--once", "--state-file", STATE_FILE],
                cwd=HERE, env=env, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
            )
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            code = r.returncode
        except Exception as exc:  # noqa: BLE001
            out, code = f"启动失败: {exc}", -1

    rotate_log(LOG_FILE)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n===== {stamp} exit={code} =====\n")
            f.write(out + "\n")
    except OSError:
        pass

    # CI 环境（GitHub Actions 等）没有持久磁盘，同时打到 stdout 才能在运行日志里看到
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"):
        try:
            print(f"===== {stamp} exit={code} =====")
            print(out)
        except Exception:  # noqa: BLE001  pythonw 下 stdout 为 None
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
