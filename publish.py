# -*- coding: utf-8 -*-
"""
publish.py —— 把竞彩指数页面发布到静态虚拟主机（如 90qu.com 的子目录）

虚拟主机上跑不了常驻的 Python 服务，所以换成「本地抓取 → 上传静态文件」的模式：
    1. 抓最新数据（竞彩必发走 bifaw_fetch.py；超级指数已下线，见下面的开关）
    2. 生成发布包 dist/（index.html + bifaw.json + .htaccess + 内联快照）
    3. 通过 FTP/FTPS/SFTP 把文件传到站点子目录

用法：
    python publish.py --check           # 自检 FTP 配置（首次配置完先跑这个）
    python publish.py                   # 只抓取 + 生成 dist/，不联网上传
    python publish.py --upload          # 抓取 + 生成 + 上传（首次部署用全量）
    python publish.py --upload --only-data   # 日常更新用：只传数据文件
    python publish.py --no-fetch --upload    # 不抓取，用现有数据重新发布
    python publish.py --skip-bifaw      # 只跑超级指数（需配合 --with-spdex）
    python publish.py --with-spdex      # 临时恢复超级指数抓取
    python publish.py 20260913          # 指定期号（只对超级指数有意义）

首次运行会生成 publish.json 配置模板，填好 FTP 信息后再跑 --upload。
依赖：标准库（ftplib）；SFTP 需额外 pip install paramiko
     bifaw 抓取需要 playwright + ddddocr，且必须有显示环境（CI 用 xvfb）

⛔ 关于超级指数（c.spdex.com）：2026-09-16 起**默认下线**（见 SPDEX_ENABLED）。
   该站已整体改版成 Nuxt SPA + 会员登录墙，老的 ASP.NET 页面不复存在 ——
   任何路径（连 /robots.txt）都只返回同一个登录页，抓取脚本每轮「一场都没抓到」。
   留着只会让每轮白跑一次并报 WARN，所以默认不再跑它。

给「定时无人值守」准备的安全机制（都由本脚本内置，任务计划只需调用它）：
    · 加锁 —— 上一次没跑完就跳过本轮（电脑睡醒后任务计划可能补跑重叠）
    · 数据校验 —— 0 场或全部场次没解析出表格时，保留上一份数据不上传，
      避免一次抓取失败就把线上页面清空。
      校验以「当前启用的源」为准：竞彩必发失败就只保留它自己的上一份。
      （启用超级指数时，它校验不过**不提前退出**，只标 WARN，免得拖累另一个源。）
    · 数据指纹 —— 只算启用中的源，没变化就不重复上传
    · 日志 —— 同时写 logs/publish-YYYYMMDD.log，无人值守时靠它排查
    · **每次运行的最后一行输出一个纯 ASCII 结果标记**，供 bat / CI / 用户快速判读：
        RESULT: OK    - uploaded N file(s) / data unchanged / package built
        RESULT: WARN  - 上传成功但超级指数数据是上一轮的（CI 出黄灯，不算故障）
        RESULT: FAIL  - 抓取不可用 / 配置缺失 / 上传失败（都会在下一轮自动重试）
        RESULT: SKIP  - 上一次还没跑完（无害）
      这一行在控制台上**不带时间戳**、以行首 "RESULT: " 开头，所以可以直接
        grep '^RESULT: ' 输出文件
      取到它（Windows 任务计划、GitHub Actions 都靠这个判读成败）。
      之所以用 ASCII 而不是中文：cmd 默认代码页会把 bat 里的中文显示成乱码，
      用户恰恰要靠这一行判断成败。
    · 退出码：0 = 成功 / 1 = 配置缺失或上传失败 / 2 = 数据不完整（已保留旧数据）
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time

# ---------------------------------------------------------------- 数据源开关
# ⛔ 超级指数（c.spdex.com）：2026-09-16 起下线。
#    该站已整体改版成 Nuxt SPA + 会员登录墙（任何路径，含 /robots.txt，都返回同一个
#    10652 字节登录页），老页面结构 `<h3 tid='...'>` + VIEWSTATE 分页不复存在，
#    spdex_fetch.py 每轮都「一场都没抓到」。留着它只会每轮白跑一次并报 WARN。
#    以后若拿到账号要改造登录抓取，用 --with-spdex 临时恢复（或把这里改成 True）。
SPDEX_ENABLED = False

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


def active_data_files(use_spdex=True):
    """当前启用中的源对应的数据文件（绝对路径），顺序即加载顺序。

    发布时间/指纹/校验/上传都从这里取，避免代码各处再写死一次
    「data.json + bifaw.json」而漏掉开关。
    """
    names = ["bifaw.json"]
    if use_spdex:
        names.insert(0, "data.json")
    return [os.path.join(ROOT, n) for n in names]


def fingerprint(*paths):
    """数据指纹：只看各源的 matches，不含抓取时间 —— 用来判断数据是否真的变了。

    ⚠️ 只算**传进来的**（也就是当前启用的）源。早期版本在这里固定拼上 data.json，
    超级指数一下线、data.json 不再更新甚至被删掉，指纹就会算成空串，
    「数据没变就跳过上传」这个优化会静默失效，变成每轮都白传一遍。
    """
    payload = []
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                payload.append(json.dumps(json.load(f).get("matches") or [],
                                          ensure_ascii=False, sort_keys=True))
        except Exception:
            return ""
    if not payload:
        return ""
    return hashlib.md5("|".join(payload).encode("utf-8")).hexdigest()


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
def check_bifaw(path):
    """竞彩必发数据的可上线校验（与 check_data 同思路，但字段是 rows）"""
    if not os.path.exists(path):
        return False, "没有 bifaw.json"
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as exc:
        return False, "读取失败：%s" % exc
    ms = d.get("matches") or []
    if not ms:
        return False, "一场都没抓到"
    blank = [m.get("id") for m in ms if not m.get("rows")]
    if len(blank) == len(ms):
        return False, "%d 场全部没有指数行" % len(ms)
    note = "%d 场" % len(ms)
    if blank:
        note += "，其中 %d 场缺指数行" % len(blank)
    return True, note


def fetch_bifaw():
    """抓竞彩必发数据。**失败不影响 spdex 上线** —— 保留上一份 bifaw.json 继续发布。

    该站要登录 + 过验证码，且必须在有界面浏览器里跑（纯 headless 会被识别，
    页面直接报「undefined，请点击此处重新获取!」），所以 CI 里需要 xvfb。
    """
    path = os.path.join(ROOT, "bifaw.json")
    script = os.path.join(ROOT, "bifaw_fetch.py")
    if not os.path.exists(script):
        return
    import subprocess
    backup = path + ".bak"
    had = os.path.exists(path)
    if had:
        shutil.copy2(path, backup)
    log("抓取竞彩必发（bifaw）…")
    try:
        p = subprocess.run([sys.executable, script, "--out", path],
                           cwd=ROOT, capture_output=True, timeout=300)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        for line in (out + err).strip().splitlines():
            log("  bifaw | %s" % line)
        rc = p.returncode
    except Exception as exc:
        log("  bifaw 抓取异常：%s" % exc)
        rc = 1

    ok, why = check_bifaw(path) if rc == 0 else (False, "抓取退出码 %s" % rc)
    if not ok:
        log("竞彩必发数据不可用（%s）→ 保留上一份 bifaw.json" % why)
        if had and os.path.exists(backup):
            shutil.copy2(backup, path)
    else:
        log("竞彩必发数据正常：%s" % why)
    if os.path.exists(backup):
        os.remove(backup)


def sync_inline(use_spdex=True):
    """由各源的 json 重新生成内联快照（bifaw-inline.js / data-inline.js）。

    快照只是 file:// 双击打开时的兜底，必须和主数据始终一致。放在打包前统一重算，
    就不会出现「json 已经还原成旧的好数据、快照却还是刚写坏的 0 场空壳」这种半截状态
    —— 只还原 json 不还原快照，会把一份空壳打进发布包（踩过）。
    """
    pairs = [("bifaw.json", "bifaw-inline.js", "__BIFAW__", "供 file:// 打开时使用")]
    if use_spdex:
        pairs.insert(0, ("data.json", "data-inline.js", "__DATA__",
                         "供 file:// 直接打开时使用"))
    for js, out, key, note in pairs:
        src = os.path.join(ROOT, js)
        if not os.path.exists(src):
            continue
        try:
            with open(src, encoding="utf-8") as f:
                d = json.load(f)
            with open(os.path.join(ROOT, out), "w", encoding="utf-8") as f:
                f.write("/* 自动生成：由 %s 导出，%s */\n" % (js, note))
                f.write("window.%s = %s;\n"
                        % (key, json.dumps(d, ensure_ascii=False, separators=(",", ":"))))
        except Exception as exc:
            log("内联快照生成失败 %s：%s" % (out, exc))


def build_dist(use_spdex=True):
    sync_inline(use_spdex)

    page_src = os.path.join(ROOT, "index.html")
    if not os.path.exists(page_src):
        log("缺少 index.html")
        return False

    # 只要求「启用的源」有数据；已下线的源不再进包
    data_files = []
    for p in active_data_files(use_spdex):
        if not os.path.exists(p):
            log("缺少 %s，先运行抓取" % os.path.basename(p))
            return False
        data_files.append(p)

    os.makedirs(DIST, exist_ok=True)
    # 静态发布包：关掉「探测本地服务」的分支，否则每个访客都会白打一次 /api/status 404
    with open(page_src, encoding="utf-8") as f:
        page = f.read()
    flag = "var LIVE_PROBE = true;"
    if flag in page:
        page = page.replace(flag, "var LIVE_PROBE = false;", 1)
    else:
        log("  提示：index.html 里没找到 %s，静态包仍会探测本地服务" % flag)
    with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(page)

    for p in data_files:
        shutil.copy2(p, os.path.join(DIST, os.path.basename(p)))

    # 内联快照（file:// 兜底）。下线源的不再带
    for name in ("data-inline.js", "bifaw-inline.js"):
        if name == "data-inline.js" and not use_spdex:
            continue
        src = os.path.join(ROOT, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DIST, name))

    # 清掉 dist/ 里已下线源的残留 —— dist/ 是跨轮累积的，
    # 不清理的话线上会一直留着一份几天前（甚至更早）的 data.json
    if not use_spdex:
        for name in ("data.json", "data-inline.js"):
            p = os.path.join(DIST, name)
            if os.path.exists(p):
                os.remove(p)

    with open(os.path.join(DIST, ".htaccess"), "w", encoding="utf-8", newline="\n") as f:
        f.write(HTACCESS)

    size = sum(os.path.getsize(os.path.join(DIST, n)) for n in os.listdir(DIST))
    bits = []
    for p in data_files:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            bits.append("%s %d 场" % (os.path.basename(p), len(d.get("matches") or [])))
        except Exception:
            bits.append("%s 读取失败" % os.path.basename(p))
    log("发布包已生成 -> dist/  （%.1f KB，%s）" % (size / 1024.0, " + ".join(bits)))
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
        missing = [n for n in ("index.html", "bifaw.json") if n not in names]
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
    use_spdex = bool(args.with_spdex or SPDEX_ENABLED)
    stale = False       # 本轮 spdex 没抓到可用的，线上保留的是上一份

    # ---------- 1. 抓取超级指数（已下线，默认跳过） ----------
    if not use_spdex:
        log("超级指数（spdex）已下线，本轮跳过该源")
    elif not args.no_fetch:
        import spdex_fetch
        log("抓取期号 %s …" % date)
        # ⚠️ 要备份两个文件：spdex_fetch.build() 会同时写 data.json 和
        # data-inline.js。只还原 json 的话，内联快照会留下一份「0 场」的空壳，
        # 被 build_dist 一起打进发布包（踩过）。
        backup = data_path + ".bak"
        inline_path = os.path.join(ROOT, "data-inline.js")
        inline_backup = inline_path + ".bak"
        had = os.path.exists(data_path)
        had_inline = os.path.exists(inline_path)
        if had:
            shutil.copy2(data_path, backup)
        if had_inline:
            shutil.copy2(inline_path, inline_backup)
        try:
            spdex_fetch.build(date, data_path)
        except Exception as exc:
            log("抓取过程异常：%s" % exc)

        ok, why = check_data(data_path)
        if not ok:
            # 注意：这里**不再提前 return**。抓取偶发失败是常态，
            # 若直接退出，另一个数据源（竞彩必发）就会跟着一起停更 ——
            # 所以改成「还原旧数据 + 继续跑」，最后用 WARN 标记本轮 spdex 是旧的。
            log("抓取结果不可用（%s）→ 保留上一份数据" % why)
            if had and os.path.exists(backup):
                shutil.copy2(backup, data_path)
            if had_inline and os.path.exists(inline_backup):
                shutil.copy2(inline_backup, inline_path)
            stale = True
        else:
            log("抓取正常：%s" % why)
        for p in (backup, inline_backup):
            if os.path.exists(p):
                os.remove(p)

    # ---------- 1b. 抓取第二个数据源：竞彩必发（失败不影响 spdex 上线） ----------
    if not args.no_fetch and not args.skip_bifaw:
        fetch_bifaw()

    # ---------- 2. 校验「当前启用的源」+ 生成发布包 ----------
    if use_spdex:
        ok, why = check_data(data_path)
        if not ok:
            log("data.json 不可用（%s），中止" % why)
            log("RESULT: FAIL - local data.json unusable")
            return 2
    ok, why = check_bifaw(os.path.join(ROOT, "bifaw.json"))
    if not ok:
        log("bifaw.json 不可用（%s），中止" % why)
        log("RESULT: FAIL - local bifaw.json unusable")
        return 2

    if not build_dist(use_spdex):
        log("RESULT: FAIL - could not build dist/")
        return 1

    if not args.upload:
        log("未加 --upload：发布包已生成，可手动上传 dist/")
        log("RESULT: %s" % ("WARN - package built (spdex data stale)"
                            if stale else "OK - package built (no --upload flag)"))
        return 2 if stale else 0

    conf = load_conf()
    if not conf:
        log("RESULT: FAIL - publish.json is missing")
        return 1

    # ---------- 3. 数据没变化就不重复上传 ----------
    data_files = active_data_files(use_spdex)
    fp = fingerprint(*data_files)
    if args.only_data and not args.force and fp and fp == read_fp():
        log("数据与上次上传一致，跳过上传（要强制上传加 --force）")
        log("RESULT: OK - data unchanged, nothing uploaded")
        return 0

    files = []
    if not args.only_data:
        files.append((os.path.join(DIST, "index.html"), "index.html"))
        files.append((os.path.join(DIST, ".htaccess"), ".htaccess"))
        snaps = ["bifaw-inline.js"] + (["data-inline.js"] if use_spdex else [])
        for snap in snaps:
            p = os.path.join(DIST, snap)
            if os.path.exists(p):
                files.append((p, snap))
    for p in data_files:
        name = os.path.basename(p)
        packed = os.path.join(DIST, name)
        if os.path.exists(packed):
            files.append((packed, name))

    # ---------- 4. 上传 ----------
    mode = conf.get("mode", "ftp")
    ok = upload_sftp(conf["sftp"], files) if mode == "sftp" else upload_ftp(conf["ftp"], files)
    if ok:
        write_fp(fp)                       # 只有上传成功才记指纹
        log("上传完成 ✓  共 %d 个文件" % len(files))
        if stale:
            log("RESULT: WARN - uploaded %d file(s); spdex fetch unusable, kept previous data"
                % len(files))
            return 2                        # 2 = 数据不完整但不算故障（CI 出黄灯不出红灯）
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
                    help="只上传数据文件（页面文件已就位时日常更新用）")
    ap.add_argument("--no-fetch", action="store_true", help="跳过抓取，用现有数据文件")
    ap.add_argument("--skip-bifaw", action="store_true",
                    help="跳过竞彩必发（bifaw）抓取，只跑超级指数")
    ap.add_argument("--with-spdex", action="store_true",
                    help="临时恢复超级指数（c.spdex.com）抓取（该站已改版为会员站，默认关闭）")
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
