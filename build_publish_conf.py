# -*- coding: utf-8 -*-
"""
build_publish_conf.py —— 在 GitHub Actions 里用环境变量拼出 publish.json

为什么需要它：
    publish.py 读的是仓库根目录的 publish.json，但 FTP 密码绝不能提交进 Git。
    所以本地那份 publish.json 被 .gitignore 挡在仓库外，CI 里改由本脚本
    从 GitHub Secrets 现场生成一份，任务结束后随 runner 一起销毁。

环境变量（对应仓库 Secrets，同名）：
    FTP_MODE         ftp / sftp，默认 ftp
    FTP_HOST         如 ftp.90qu.com
    FTP_PORT         默认 ftp=21，sftp=22
    FTP_USER
    FTP_PASSWORD
    FTP_REMOTE_DIR   如 /public_html/football
    FTP_USE_TLS      true/false，默认 false

安全约定：
    · 只打印「已生成」，任何情况下都不打印密码；
    · 缺哪一项就明确报出名字，方便对症去补 Secret。
"""
import json
import os
import sys


def env(name, default=""):
    """空字符串也当作没填，避免 Secrets 里留了个空值还以为生效了"""
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def need(name, value):
    if not value:
        print("::error::缺少 Secret：%s（仓库 Settings → Secrets and variables → Actions）" % name)
        sys.exit(1)
    return value


def main():
    mode = env("FTP_MODE", "ftp").strip().lower()
    if mode not in ("ftp", "sftp"):
        print("::error::FTP_MODE 只能是 ftp 或 sftp，当前是 %r" % mode)
        return 1

    # 诊断：打印每个 Secret 实际字节数（不打印内容，避免泄漏）。
    # 踩过的坑：本机 Git Bash 下 `printf '%s' x | gh secret set --body -` 会把
    # 字符串截成 1 个字符，肉眼很难看出；保留这段几乎零成本，万一日后又翻车能立刻发现。
    for n in ("FTP_MODE", "FTP_HOST", "FTP_PORT", "FTP_USER", "FTP_PASSWORD",
             "FTP_REMOTE_DIR", "FTP_USE_TLS"):
        raw = os.environ.get(n)
        print("Secret %-15s len=%-3d" % (n, len(raw or "")))

    host = need("FTP_HOST", env("FTP_HOST"))
    user = need("FTP_USER", env("FTP_USER"))
    password = need("FTP_PASSWORD", env("FTP_PASSWORD"))
    remote_dir = need("FTP_REMOTE_DIR", env("FTP_REMOTE_DIR"))

    default_port = 22 if mode == "sftp" else 21
    try:
        port = int(env("FTP_PORT", str(default_port)))
    except ValueError:
        print("::error::FTP_PORT 必须是数字")
        return 1

    use_tls = env("FTP_USE_TLS", "false").strip().lower() in ("1", "true", "yes", "y")

    # 两个分支都填上，这样以后想从 ftp 换成 sftp 只需改 FTP_MODE 一个 Secret
    conf = {
        "_说明": "由 GitHub Actions 从 Secrets 生成，请勿提交到仓库",
        "mode": mode,
        "ftp": {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "use_tls": use_tls,
            "remote_dir": remote_dir,
        },
        "sftp": {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "remote_dir": remote_dir,
        },
    }

    with open("publish.json", "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)

    print("已生成 publish.json：mode=%s，host=%s，remote_dir=%s（凭据来自 Secrets，未回显）"
          % (mode, host, remote_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
