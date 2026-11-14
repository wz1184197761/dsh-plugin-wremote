"""幂等启动 server.py：端口已在听就跳过；否则以脱离会话的独立进程拉起，并等待就绪。

用法：python launch.py [--port 8098] [--fps 20] [--quality 70] [--show]
"""
import argparse
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")
LOG = os.path.join(HERE, "server.log")


def port_open(port, host="127.0.0.1", timeout=0.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--quality", type=int, default=70)
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--follow", choices=("off", "weixin", "all"), default="weixin",
                    help="跟随前台窗口：weixin=只跟微信系进程（默认），all=跟任意程序，off=只投初始窗口")
    a = ap.parse_args()

    if port_open(a.port):
        print(f"[launch] port {a.port} already listening, skip")
        return 0

    cmd = [sys.executable, SERVER, "--port", str(a.port),
           "--fps", str(a.fps), "--quality", str(a.quality),
           "--follow", a.follow]
    if a.show:
        cmd.append("--show")

    flags = 0
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with open(LOG, "ab") as lf:
        p = subprocess.Popen(cmd, cwd=HERE, stdout=lf, stderr=lf, env=env,
                             stdin=subprocess.DEVNULL, creationflags=flags,
                             close_fds=True)

    for _ in range(100):
        if port_open(a.port):
            print(f"[launch] started pid={p.pid} port={a.port} log={LOG}")
            return 0
        if p.poll() is not None:
            print(f"[launch] server exited rc={p.returncode}, see {LOG}")
            return 1
        time.sleep(0.1)
    print(f"[launch] timeout waiting for port {a.port}, see {LOG}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
