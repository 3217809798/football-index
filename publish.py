# -*- coding: utf-8 -*-
"""
publish.py —— 把竞彩指数页面发布到静态虚拟主机（如 90qu.com 的子目录）

虚拟主机上跑不了常驻的 Python 服务，所以换成「本地抓取 → 上传静态文件」的模式：
    1. 抓最新数据（复用 spdex_fetch.py）
    2. 生成发布包 dist/（index.html + data.json + .htaccess）
    3. 通过 FTP/FTPS/SFTP 把 data.json 传到站点子目录（页面文件只需首次传一次）

用法：
    python publish.py --check           # 自检 FTP 配置（首次配置完先跑这个）
    python publish.py                   # 只抓取 + 生成 dist/，不联网上传
    python publish.py --upload          # 抓取 + 生成 + 上传（首次部署用全量）
    python publish.py --upload --only-data   # 日常更新用：只传 data.json
    python publish.py --no-fetch --upload    # 不抓取，用现有 data.json 重新发布
    python publish.py 20260913          # 指定期号

首次运行会生成 publish.json 配置模板，填好 FTP 信息后再跑 --upload。
依赖：标准库（ftplib）；SFTP 需额外 pip install paramiko

给「定时无人值守」准备的安全机制（都由本脚本内置，任务计划只需调用它）：
    · 加锁 —— 上一次没跑完就跳过本轮（电脑睡醒后任务计划可能补跑重叠）
    · 数据校验 —— 0 场或全部场次没解析出表格时，保留上一份数据、不上传，
      避免一次抓取失败就把线上页面清空
    · 数据指纹 —— 数据没变化就不重复上传（每 15 分钟一次，多数轮次数据是不变的）
    · 日志 —— 同时写 logs/publish-YYYYMMDD.log，无人值守时靠它排查
    · **每次运行的最后一行输出一个纯 ASCII 结果标记**，供 bat / CI / 用户快速判读：
        RESULT: OK    - uploaded N file(s) / data unchanged / package built
        RESULT: FAIL  - 抓取不可用 / 配置缺失 / 上传失败（都会在下一轮自动重试）
        RESULT: SKIP  - 上一次还没跑完（无害）
      这一行在控制台上**不带时间戳**、以行首 "RESULT: " 开头，所以可以直接
        grep '^RESULT: ' 输出文件
      取到它（Windows 任务计划、GitHub Actions 都靠这个判读成败）。
      之所以用 ASCII 而不是中文：cmd 默认代码页会把 bat 里的中文显示成乱码，
      用户恰恰要靠这一行判断成败。
    · 退出码：0 = 成功 / 1 = 配置缺失或上传失败 / 2 = 抓取结果不可用（保留旧数据）
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DIST = os.path.join(ROOT, "dist")
CONF = os.path.join(ROOT, "publish.json")

CONF_TEMPLATE = {
    "_说明": "填好 host/user/password/remote_dir 后用 python publish.py --upload 上传",
    "mode": "ftp",
    "ftp": {
        "host": "ftp.90qu.com",
        "port": 21,
        "user": "你的FTP用户名",
        "password": "你的FTP密码",
        "use_tls": False,
        "remote_dir": "/public_html/football"
    },
    "sftp": {
        "host": "90qu.com",
        "port": 22,
        "user": "你的SSH用户名",
        "password": "你的SSH密码",
        "remote_dir": "/www/wwwroot/90qu.com/football"
    }
}

HTACCESS = """# ---- 足球指数子栏目（静态） ----
DirectoryIndex index.html
Options -Indexes
AddDefaultCharset UTF-8

# 数据文件不要缓存，保证访客看到的是最新一份
<FilesMatch "\\.json$">
  <IfModule mod_headers.c>
    Header set Cache-Control "no-cache, no-store, must-revalidate"
    Header set Pragma "no-cache"
  </IfModule>
  <IfModule mod_expires.c>
    ExpiresActive On
    ExpiresByType application/json "access plus 0 seconds"
  </IfModule>
</FilesMatch>

# 页面本体缓存 5 分钟
<FilesMatch "\\.html$">
  <IfModule mod_expires.c>
    ExpiresActive On
    ExpiresByType text/html "access plus 5 minutes"
  </IfModule>
</FilesMatch>

# 压缩
<IfModule mod_deflate.c>
  AddOutputFilterByType DEFLATE text/html text/css application/javascript application/json
</IfModule>
"""


# ---------------------------------------------------------------- 日志 / 锁 / 校验
# 定时任务跑起来后是没人盯着控制台的，所以：日志落盘、加锁防重叠、上线前校验数据
LOGDIR = os.path.join(ROOT, "logs")
LOCK = os.path.join(ROOT, ".publish.lock")
FPFILE = os.path.join(ROOT, ".publish-fingerprint")
LOCK_STALE = 20 * 60          # 锁超过 20 分钟视为陈旧（上次进程被强杀留下的）


RESULT_PREFIX = "RESULT: "


def log(msg):
    """同时写控制台和 logs/publish-YYYYMMDD.log

    带 RESULT: 前缀的行是「机器可读的结果标记」：控制台上**不带时间戳**原样输出，
    好让定时任务 / CI 能用 `grep '^RESULT: '` 精确判读成败；写进日志文件时仍然
    带时间戳，方便人工回溯。
    """
    is_result = msg.startswith(RESULT_PREFIX)
    print(msg if is_result else
          "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(os.path.join(LOGDIR, "publish-%s.log" % time.strftime("%Y%m%d")),
                  "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def acquire_lock():
    """防止两次任务重叠运行。

    抓一次约 10 秒、间隔 15 分钟，正常不会撞；但电脑睡醒后任务计划可能补跑，
    两次同时改 data.json / 同时连 FTP 会互相打架，所以还是加一道锁。
    """
    if os.path.exists(LOCK):
        age = time.time() - os.path.getmtime(LOCK)
        if age < LOCK_STALE:
            return False
        log("发现 %.0f 分钟前的陈旧锁，忽略它" % (age / 60.0))
    try:
        with open(LOCK, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except Exception as exc:
        log("锁文件创建失败（%s），继续执行" % exc)
        return True


def release_lock():
    try:
        os.remove(LOCK)
    except Exception:
        pass


def check_data(path):
    """校验数据能不能上线，返回 (ok, 说明)。

    只拦「彻底失败」：一场都没抓到、或所有场次都没解析出表格 ——
    这种数据传上去线上页面会变空白，宁可保留上一份旧数据、等下一轮再试。
    **不按场次数量判断**：小期次只有两三场、跨天时段少，都是正常的。
    """
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as exc:
        return False, "数据文件读取失败：%s" % exc

    ms = d.get("matches") or []
    if not ms:
        return False, "一场都没抓到"
    blank = [str(m.get("no")) for m in ms if not m.get("rows")]
    if len(blank) == len(ms):
        return False, "%d 场全部没有表格数据（详情接口可能挂了）" % len(ms)

    note = "%d 场" % len(ms)
    if blank:
        note += "，其中 %d 场缺表格数据（%s）" % (len(blank), ",".join(blank[:8]))
    return True, note


def fingerprint(path):
    """数据指纹：只看 matches，不含抓取时间 —— 用来判断数据是否真的变了"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        payload = json.dumps(d.get("matches") or [], ensure_ascii=False, sort_keys=True)
        return hashlib.md5(payload.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def read_fp():
    try:
        with open(FPFILE, encoding="utf-8") as f:
            return json.load(f).get("fp", "")
    except Exception:
        return ""


def write_fp(fp):
    """只在上传成功后才写，避免上传失败却记下指纹，导致下一轮误判为「没变化」跳过"""
    try:
        with open(FPFILE, "w", encoding="utf-8") as f:
            json.dump({"fp": fp, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------- 发布包
def build_dist():
    for name in ("index.html", "data.json"):
        src = os.path.join(ROOT, name)
        if not os.path.exists(src):
            log("缺少 %s，先运行抓取" % name)
            return False
    os.makedirs(DIST, exist_ok=True)
    # 静态发布包：关掉「探测本地服务」的分支，否则每个访客都会白打一次 /api/status 404
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        page = f.read()
    flag = "var LIVE_PROBE = true;"
    if flag in page:
        page = page.replace(flag, "var LIVE_PROBE = false;", 1)
    else:
        log("  提示：index.html 里没找到 %s，静态包仍会探测本地服务" % flag)
    with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    shutil.copy2(os.path.join(ROOT, "data.json"), os.path.join(DIST, "data.json"))
    # 若源目录里存在内联快照，也一并带上（线上用不到，纯属兜底）
    snap = os.path.join(ROOT, "data-inline.js")
    if os.path.exists(snap):
        shutil.copy2(snap, os.path.join(DIST, "data-inline.js"))
    with open(os.path.join(DIST, ".htaccess"), "w", encoding="utf-8", newline="\n") as f:
        f.write(HTACCESS)

    size = sum(os.path.getsize(os.path.join(DIST, n)) for n in os.listdir(DIST))
    with open(os.path.join(DIST, "data.json"), encoding="utf-8") as f:
        d = json.load(f)
    log("发布包已生成 -> dist/  （%.1f KB，%s 期 %d 场）"
        % (size / 1024.0, d.get("date"), len(d.get("matches", []))))
    return True


# ---------------------------------------------------------------- 配置
def load_conf():
    if not os.path.exists(CONF):
        with open(CONF, "w", encoding="utf-8") as f:
            json.dump(CONF_TEMPLATE, f, ensure_ascii=False, indent=2)
        log("已生成配置模板 %s —— 请填写 FTP 信息后重新运行 --upload" % CONF)
        return None
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- FTP
def ftp_ensure_dir(ftp, path):
    parts = [p for p in path.strip("/").split("/") if p]
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            ftp.mkd(cur)
        except Exception:
            pass        # 已存在


def ftp_put(ftp, local, remote_dir, name):
    """先传成 .tmp 再改名，避免访客下到写了一半的文件"""
    tmp = remote_dir.rstrip("/") + "/" + name + ".tmp"
    final = remote_dir.rstrip("/") + "/" + name
    with open(local, "rb") as f:
        ftp.storbinary("STOR " + tmp, f)
    try:
        ftp.delete(final)
    except Exception:
        pass
    ftp.rename(tmp, final)


def check_ftp(cfg):
    """只做一次「连接 + 进入目标目录 + 试写」自检，不上传任何数据文件。

    首次配置最容易填错的是 remote_dir：cPanel 多为 /public_html/<子目录>，
    宝塔多为 /www/wwwroot/<域名>/<子目录>。这个自检能直接说明问题出在哪。
    """
    import ftplib
    import io

    host, port = cfg["host"], int(cfg.get("port", 21))
    log("连接 FTP %s:%d …" % (host, port))
    ftp = ftplib.FTP_TLS() if cfg.get("use_tls") else ftplib.FTP()
    try:
        ftp.connect(host, port, timeout=30)
    except Exception as exc:
        log("[X] 连不上 %s:%d ：%s" % (host, port, exc))
        log("    检查 host / port 是否正确；有些主机要求用 ftp.<域名> 或指定端口")
        return False
    try:
        ftp.login(cfg.get("user", ""), cfg.get("password", ""))
    except Exception as exc:
        log("[X] 登录失败：%s" % exc)
        log("    检查 user / password（多数主机要求用 FTP 专用账号，不是面板登录密码）")
        return False
    if cfg.get("use_tls"):
        ftp.prot_p()
    ftp.set_pasv(True)
    log("登录成功，FTP 起始目录：%s" % ftp.pwd())

    rd = cfg["remote_dir"]
    try:
        ftp.cwd(rd)
    except Exception as exc:
        log("[X] 进不去远程目录 %s ：%s" % (rd, exc))
        log("    FTP 账号的起始目录是 %s，remote_dir 要写成它下面的路径" % ftp.pwd())
        try:
            log("    该目录下现有：%s" % ", ".join(ftp.nlst()[:20]))
        except Exception:
            pass
        return False

    log("远程目录存在：%s" % ftp.pwd())
    try:
        names = ftp.nlst()
        log("该目录现有文件：%s" % (", ".join(names[:20]) if names else "（空）"))
        missing = [n for n in ("index.html", "data.json") if n not in names]
        if missing:
            log("    注意：缺少 %s —— 如果这是第一次部署，先做一次全量上传（去掉 --only-data）"
                % "、".join(missing))
    except Exception:
        pass

    try:                                    # 试写一个空文件再删掉，确认有写权限
        ftp.storbinary("STOR .write-test", io.BytesIO(b"ok"))
        ftp.delete(".write-test")
        log("[OK] 目录可读可写，配置没问题")
        return True
    except Exception as exc:
        log("[X] 目录不可写：%s（检查 FTP 账号的写权限/磁盘配额）" % exc)
        return False
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def check_sftp(cfg):
    try:
        import paramiko
    except ImportError:
        log("SFTP 需要 paramiko：pip install paramiko")
        return False
    import io as _io

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    log("连接 SFTP %s:%s …" % (cfg["host"], cfg.get("port", 22)))
    try:
        cli.connect(cfg["host"], port=int(cfg.get("port", 22)),
                    username=cfg.get("user", ""), password=cfg.get("password", ""), timeout=30)
    except Exception as exc:
        log("[X] 连接/登录失败：%s" % exc)
        return False
    sf = cli.open_sftp()
    rd = cfg["remote_dir"].rstrip("/")
    try:
        log("远程目录内容：%s" % ", ".join(sf.listdir(rd)[:20]))
    except IOError as exc:
        log("[X] 进不去远程目录 %s ：%s" % (rd, exc))
        sf.close(); cli.close()
        return False
    try:
        sf.putfo(_io.BytesIO(b"ok"), rd + "/.write-test")
        sf.remove(rd + "/.write-test")
        log("[OK] 目录可读可写，配置没问题")
        ok = True
    except Exception as exc:
        log("[X] 目录不可写：%s" % exc)
        ok = False
    sf.close(); cli.close()
    return ok


def upload_ftp(cfg, files):
    import ftplib

    host, port = cfg["host"], int(cfg.get("port", 21))
    log("连接 FTP %s:%d …" % (host, port))
    ftp = ftplib.FTP_TLS() if cfg.get("use_tls") else ftplib.FTP()
    # 连接/登录失败是定时任务里最常见的错误，要给一行说明而不是 traceback
    try:
        ftp.connect(host, port, timeout=30)
        ftp.login(cfg.get("user", ""), cfg.get("password", ""))
    except Exception as exc:
        log("FTP 连接/登录失败：%s" % exc)
        log("    （先用 python publish.py --check 定位是 host、密码还是目录的问题）")
        return False
    if cfg.get("use_tls"):
        ftp.prot_p()
    ftp.set_pasv(True)
    log("登录成功，当前目录：%s" % ftp.pwd())

    rd = cfg["remote_dir"]
    ftp_ensure_dir(ftp, rd)
    ftp.cwd(rd)
    log("进入远程目录：%s" % ftp.pwd())

    try:
        for local, name in files:
            ftp_put(ftp, local, rd, name)
            log("  已上传 %s (%.1f KB)" % (name, os.path.getsize(local) / 1024.0))
    except Exception as exc:
        log("上传中断：%s" % exc)
        return False
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()
    return True


# ---------------------------------------------------------------- SFTP
def upload_sftp(cfg, files):
    try:
        import paramiko
    except ImportError:
        log("SFTP 需要 paramiko：pip install paramiko")
        return False

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    log("连接 SFTP %s:%s …" % (cfg["host"], cfg.get("port", 22)))
    try:
        cli.connect(cfg["host"], port=int(cfg.get("port", 22)),
                    username=cfg.get("user", ""), password=cfg.get("password", ""), timeout=30)
    except Exception as exc:
        log("SFTP 连接/登录失败：%s" % exc)
        log("    （先用 python publish.py --check 定位问题）")
        return False
    sf = cli.open_sftp()

    rd = cfg["remote_dir"].rstrip("/")
    cur = ""
    for p in [x for x in rd.split("/") if x]:
        cur += "/" + p
        try:
            sf.stat(cur)
        except IOError:
            sf.mkdir(cur)

    try:
        for local, name in files:
            tmp = rd + "/" + name + ".tmp"
            sf.put(local, tmp)
            try:
                sf.remove(rd + "/" + name)
            except IOError:
                pass
            sf.rename(tmp, rd + "/" + name)
            log("  已上传 %s (%.1f KB)" % (name, os.path.getsize(local) / 1024.0))
    except Exception as exc:
        log("上传中断：%s" % exc)
        return False
    finally:
        sf.close()
        cli.close()
    return True


# ---------------------------------------------------------------- main
def run(args):
    date = args.date or time.strftime("%Y%m%d")
    data_path = os.path.join(ROOT, "data.json")

    # ---------- 1. 抓取（结果不可用就还原，绝不用坏数据覆盖好数据） ----------
    if not args.no_fetch:
        import spdex_fetch
        log("抓取期号 %s …" % date)
        backup = data_path + ".bak"
        had = os.path.exists(data_path)
        if had:
            shutil.copy2(data_path, backup)
        try:
            spdex_fetch.build(date, data_path)
        except Exception as exc:
            log("抓取过程异常：%s" % exc)

        ok, why = check_data(data_path)
        if not ok:
            log("抓取结果不可用（%s）→ 保留上一份数据，本轮不上传" % why)
            if had and os.path.exists(backup):
                shutil.copy2(backup, data_path)
            log("RESULT: FAIL - fetch unusable, kept previous data")
            return 2
        log("抓取正常：%s" % why)
        if os.path.exists(backup):
            os.remove(backup)

    # ---------- 2. 校验 + 生成发布包 ----------
    ok, why = check_data(data_path)
    if not ok:
        log("data.json 不可用（%s），中止" % why)
        log("RESULT: FAIL - local data.json unusable")
        return 2

    if not build_dist():
        log("RESULT: FAIL - could not build dist/")
        return 1

    if not args.upload:
        log("未加 --upload：发布包已生成，可手动上传 dist/")
        log("RESULT: OK - package built (no --upload flag)")
        return 0

    conf = load_conf()
    if not conf:
        log("RESULT: FAIL - publish.json is missing")
        return 1

    # ---------- 3. 数据没变化就不重复上传 ----------
    fp = fingerprint(data_path)
    if args.only_data and not args.force and fp and fp == read_fp():
        log("数据与上次上传一致，跳过上传（要强制上传加 --force）")
        log("RESULT: OK - data unchanged, nothing uploaded")
        return 0

    files = [(os.path.join(DIST, "data.json"), "data.json")]
    if not args.only_data:
        files = [(os.path.join(DIST, "index.html"), "index.html"),
                 (os.path.join(DIST, "data.json"), "data.json"),
                 (os.path.join(DIST, ".htaccess"), ".htaccess")]
        snap = os.path.join(DIST, "data-inline.js")
        if os.path.exists(snap):
            files.append((snap, "data-inline.js"))

    # ---------- 4. 上传 ----------
    mode = conf.get("mode", "ftp")
    ok = upload_sftp(conf["sftp"], files) if mode == "sftp" else upload_ftp(conf["ftp"], files)
    if ok:
        write_fp(fp)                       # 只有上传成功才记指纹
        log("上传完成 ✓  共 %d 个文件" % len(files))
        log("RESULT: OK - uploaded %d file(s)" % len(files))
        return 0
    log("上传失败（下一轮会自动重试）")
    log("RESULT: FAIL - upload failed, will retry next round")
    return 1


def main():
    ap = argparse.ArgumentParser(description="发布竞彩指数页面到静态虚拟主机")
    ap.add_argument("date", nargs="?", default=None, help="期号，默认当天")
    ap.add_argument("--upload", action="store_true", help="上传到远程主机")
    ap.add_argument("--only-data", action="store_true",
                    help="只上传 data.json（页面文件已就位时日常更新用）")
    ap.add_argument("--no-fetch", action="store_true", help="跳过抓取，用现有 data.json")
    ap.add_argument("--force", action="store_true", help="数据没变化也强制上传")
    ap.add_argument("--check", action="store_true",
                    help="只自检 FTP/SFTP 配置（连接+进目录+试写），不上传数据")
    args = ap.parse_args()

    # --check 是只读操作，不参与加锁
    if args.check:
        conf = load_conf()
        if not conf:
            return 1
        mode = conf.get("mode", "ftp")
        ok = check_sftp(conf.get("sftp") or {}) if mode == "sftp" \
            else check_ftp(conf.get("ftp") or {})
        log("RESULT: OK - ftp config verified" if ok else "RESULT: FAIL - ftp config problem")
        return 0 if ok else 1

    if not acquire_lock():
        log("上一次发布还没结束，本轮跳过")
        log("RESULT: SKIP - another run still in progress")
        return 0
    try:
        return run(args)
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
