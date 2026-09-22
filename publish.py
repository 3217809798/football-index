# -*- coding: utf-8 -*-
"""
publish.py —— 把竞彩指数页面发布到静态虚拟主机（如 90qu.com 的子目录）

虚拟主机上跑不了常驻的 Python 服务，所以换成「本地抓取 → 上传静态文件」的模式：
    1. 抓最新数据（竞彩必发走 bifaw_fetch.py；超级指数已下线，见下面的开关）
    2. 生成发布包 dist/（index.html + bf.json + .htaccess + 内联快照）
    3. 通过 FTP/FTPS/SFTP 把文件传到站点子目录

用法：
    python publish.py --check           # 自检 FTP 配置（首次配置完先跑这个）
    python publish.py                   # 只抓取 + 生成 dist/，不联网上传
    python publish.py --upload          # 抓取 + 生成 + 上传（首次部署用全量）
    python publish.py --upload --only-data   # 日常更新用：只传数据文件
    python publish.py --no-fetch --upload    # 不抓取，用现有数据重新发布
    python publish.py --upload --page-only   # 只发页面文件，绝不碰数据（改版式立即上线用）
    python publish.py --finalize --upload    # 归档定稿（每天 16:30 那一轮，见下）
    python publish.py --archive-only         # 只补传归档页，不含定稿标记（手工维护入口）
    python publish.py --skip-bifaw      # 只跑超级指数（需配合 --with-spdex）
    python publish.py --with-spdex      # 临时恢复超级指数抓取
    python publish.py 20260913          # 指定期号（只对超级指数有意义）

⚠️ 服务时段（见 SERVICE_OPEN / SERVICE_CLOSE）：数据只在北京时间 9:00-16:00 提供。
   时段以外本脚本直接 RESULT: SKIP 退出（不抓取、不上传），线上因此停在
   当天最后一次成功更新上 —— 这就是「截止数据」。
   想手动强跑一次加 --ignore-window（多半仍会抓不到，因为源站那时确实不给数据）。

⚠️ 归档定稿（--finalize）：按天归档页 /football/<日期>/ 只在**每天 16:30 定稿一次**，
   服务时段内的各轮抓取**不再**生成归档。
   为什么：16:00 之后源站就不给数据了（实测 DOM 抽到 0 场），当天数据的「最终版」
   = 16:00 之前最后一次成功抓取 —— 所以把归档动作挪到截止之后一次做完，
   页面上的时间口径也统一写成「<日期> 16:30 定稿」，不再写每轮各不相同的抓取时刻。
   --finalize 的三条护栏：① 数据日期必须是北京今天；② 必须已过 16:00；
   ③ 这一天还没定稿过（清单里没有 fin 标记）。`--force` 可越过全部护栏，手工补历史时用。
   ⚠️ 它还会**顺带重建 /football/ 主页**（2026-09-20 加）：主页那条「历史数据」内链
      用的是归档清单，而白天各轮重建主页时当天还没归档，所以定稿轮必须在同一轮内
      用「含当天的清单」重建一次，否则主页上永远缺当天那条内链（次日才补上）。
   ⚠️ CI 跑 `--finalize --upload`；本地不加 --upload 只生成到 dist/，不联网。

首次运行会生成 publish.json 配置模板，填好 FTP 信息后再跑 --upload。
依赖：标准库（ftplib）；SFTP 需额外 pip install paramiko
     bifaw 抓取需要 playwright + ddddocr，且必须有显示环境（CI 用 xvfb）

⛔ 关于超级指数（c.spdex.com）：2026-09-16 起**默认下线**（见 SPDEX_ENABLED）。
   该站已整体改版成 Nuxt SPA + 会员登录墙，老的 ASP.NET 页面不复存在 ——
   任何路径（连 /robots.txt）都只返回同一个登录页，抓取脚本每轮「一场都没抓到」。
   留着只会让每轮白跑一次并报 WARN，所以默认不再跑它。

给「定时无人值守」准备的安全机制（都由本脚本内置，任务计划只需调用它）：
    · 加锁 —— 上一次没跑完就跳过本轮（电脑睡醒后任务计划可能补跑重叠）
    · 服务时段 —— 只在北京时间 9:00-16:00 干活，其余时间什么都不动（见 SERVICE_OPEN）
    · 不传半成品 —— 本轮没抓到数据就只传页面文件，绝不拿磁盘上的旧快照去覆盖线上
    · 回写仓库 —— 抓到新数据时写 .publish-state（data_ok=1），CI 据此把 bf.json /
      bf-inline.js 提交回仓库。这样「上一份」永远只是上一轮（约 20 分钟前），
      而不是几天前 —— 万一哪轮失败仍误传了数据，线上最多倒退一轮。
    · 归档定稿 —— 每天 16:30 那一轮不抓取，只用当天最后一份数据定稿归档页
      （--finalize），成功后把 archive_ok=1 写进同一个 .publish-state，CI 据此把
      .archive-index.json 提交回仓库（不提交的话，下一轮 / 次日就不知道今天已归档）。
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
        RESULT: SKIP  - 上一次还没跑完、或不在服务时段内（都无害）
      这一行在控制台上**不带时间戳**、以行首 "RESULT: " 开头，所以可以直接
        grep '^RESULT: ' 输出文件
      取到它（Windows 任务计划、GitHub Actions 都靠这个判读成败）。
      之所以用 ASCII 而不是中文：cmd 默认代码页会把 bat 里的中文显示成乱码，
      用户恰恰要靠这一行判断成败。
    · 退出码：0 = 成功 / 1 = 配置缺失或上传失败 / 2 = 数据不完整（已保留旧数据）
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys
import time

import archive          # 按天归档页（/football/2026-09-19/）+ sitemap URL 清单的唯一来源

# ---------------------------------------------------------------- 数据源开关
# ⛔ 超级指数（c.spdex.com）：2026-09-16 起下线。
#    该站已整体改版成 Nuxt SPA + 会员登录墙（任何路径，含 /robots.txt，都返回同一个
#    10652 字节登录页），老页面结构 `<h3 tid='...'>` + VIEWSTATE 分页不复存在，
#    spdex_fetch.py 每轮都「一场都没抓到」。留着它只会每轮白跑一次并报 WARN。
#    以后若拿到账号要改造登录抓取，用 --with-spdex 临时恢复（或把这里改成 True）。
SPDEX_ENABLED = False

# ---------------------------------------------------------------- 服务时段
# 免费账号只能在「北京时间 9:00-16:00」看到数据；过了 16:00 源站页面会直接返回
#   「你的会员服务是[免费版]，你能查看必发指数的时间为 9:00-16:00」
# 抓取因此必然失败（0 场）。
#
# ⚠️ 为什么必须在**这一层**把住：CI 每次运行都是全新 checkout，本机没有「上一份好数据」，
#    所谓「保留上一份」其实恢复的是**仓库里提交的那份快照**（可能好几天前）。
#    于是 16 点后每轮失败都会把那份旧快照上传、覆盖掉下午刚抓到的新数据 ——
#    2026-09-18 就踩了这个坑：下午 15:51 抓到 71 场并上传成功，
#    晚上每一轮失败又把仓库里的 09-17 13:26 那份传上去，页面因此显示「昨天的数据」。
#
# 所以规则改成：**服务时段以外，一个文件都不动**。线上就停在当天 16:00 前
# 最后一次成功更新的那份 —— 这正是「截止数据」想要的效果。
SERVICE_OPEN = (9, 0)     # 北京时间开服：09:00
SERVICE_CLOSE = (16, 0)   # 北京时间截止：16:00（到点即止，不再抓取/上传）

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DIST = os.path.join(ROOT, "dist")
CONF = os.path.join(ROOT, "publish.json")
# CI 用的机器可读状态：本轮是否抓到了「可上线的新数据」。
# 供 workflow 的后续步骤判断要不要把数据快照回写仓库（见下面 write_state()）。
STATE = os.path.join(ROOT, ".publish-state")

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

    抓一次约 10 秒、间隔 20 分钟，正常不会撞；但电脑睡醒后任务计划可能补跑，
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


def now_bj():
    """当前北京时间（aware datetime）。

    ⚠️ 刻意**不依赖本机时区**：CI runner 默认 UTC（虽然 workflow 里设了 TZ，
    但本地 Windows 也可能跑），用 UTC 现算 +8 才能保证「几点中的闭包」在任何
    机器上都一致。
    """
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)


def service_state(now=None):
    """相对服务时段的状态：before（还没开）/ open（服务中）/ closed（已截止）"""
    bj = now or now_bj()
    hm = bj.hour * 60 + bj.minute
    if hm < SERVICE_OPEN[0] * 60 + SERVICE_OPEN[1]:
        return "before"
    if hm >= SERVICE_CLOSE[0] * 60 + SERVICE_CLOSE[1]:
        return "closed"
    return "open"


def write_state(data_ok, why="", archive_ok=False):
    """把「本轮产出了什么」写成 .publish-state（写不进去也无所谓）。

    CI 里两个步骤认这个文件：
      · 「把数据快照回写仓库」只认 `data_ok=1`：只有真抓到新数据才允许把
        bf.json / bf-inline.js 提交回仓库 —— 否则会把「保留的上一份」
        （= 仓库里的旧快照）当成新数据又提交一遍。
      · 同一个步骤也认 `archive_ok=1`：归档定稿轮（--finalize）不抓数据，
        data_ok 恒为 0，但它产出的 `.archive-index.json` **必须**提交回仓库，
        否则下一轮 / 次日就不知道「今天已经归档过」。

    背景：CI 每次都是全新 checkout，仓库里那份数据快照就是「上一份」。
    它是几天前的，抓取失败时一旦被传上去，线上数据就被倒退（2026-09-18 踩过）。
    回写之后仓库快照最多只落后一轮（约 20 分钟）。
    """
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            f.write("data_ok=%d\n" % (1 if data_ok else 0))
            f.write("archive_ok=%d\n" % (1 if archive_ok else 0))
            f.write("why=%s\n" % (why or ""))
            f.write("at=%s\n" % now_bj().strftime("%Y-%m-%d %H:%M:%S"))
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
    「data.json + bf.json」而漏掉开关。
    """
    names = ["bf.json"]
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
        return False, "没有 bf.json"
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


def parse_kickoff_day(text, today):
    """从形如 \"09-18 20:15\" 的开赛时间里取出日期（只要 MM-DD）。

    kickoff 里没有年份，所以按「离今天最近的那个年份」还原，
    避免跨年（12-31 → 次年 01-01）时把日期算反。
    """
    t = (text or "").strip()
    if len(t) < 5:
        return None
    try:
        mm, dd = int(t[0:2]), int(t[3:5])
    except ValueError:
        return None
    try:
        d = datetime.date(today.year, mm, dd)
    except ValueError:                      # 2-29 之类
        return None
    # 同一 MM-DD 在 ±1 年里也成立，取离今天更近的那个
    for delta in (-1, 1):
        try:
            cand = datetime.date(today.year + delta, mm, dd)
        except ValueError:
            continue
        if abs((cand - today).days) < abs((d - today).days):
            d = cand
    return d


def check_bifaw_day(path):
    """抓回来的是不是「今天或以后」的赛事列表。

    源站偶尔会留着昨天的列表（比如早上还没刷新），那种数据抽出来有场、有指数，
    常规校验全通过，传上去页面就显示成昨天的数据 —— 正是用户会立刻发现的那类问题。
    这里拦一道：所有能解析的开赛日期里，最晚的一天如果早于今天，就判不可用。

    ⚠️ 这个函数**只在刚抓完时调用**，不要塞进 check_bifaw()：
    后者还负责给「磁盘上已有的旧文件」做可上线校验，加进去会让历史快照也判失败。
    """
    try:
        with open(path, encoding="utf-8") as f:
            ms = json.load(f).get("matches") or []
    except Exception as exc:
        return False, "读取失败：%s" % exc
    today = now_bj().date()
    days = [d for d in (parse_kickoff_day(m.get("kickoff"), today) for m in ms) if d]
    if not days:
        return True, "没有可判断的开赛日期（放行）"
    latest = max(days)
    if latest < today:
        return False, "最晚开赛日是 %s，早于今天 %s（源站可能还没刷新到今天）" % (latest, today)
    return True, "最晚开赛日 %s" % latest


def fetch_bifaw():
    """抓竞彩必发数据。失败时**保留上一份 bifaw.json 继续发布**，但必须把失败
    往外传（调用方据此报 WARN）—— 否则抓取明明挂了、日志却一路 OK，
    页面悄悄停在旧数据上，正是「数据几天没更新却没人发现」的成因。

    该站要登录 + 过验证码，且必须在有界面浏览器里跑（纯 headless 会被识别，
    页面直接报「undefined，请点击此处重新获取!」），所以 CI 里需要 xvfb。

    返回 (ok, why)：ok=False 表示本轮没拿到新数据（数据已回滚为上一份）。
    """
    path = os.path.join(ROOT, "bf.json")
    script = os.path.join(ROOT, "bifaw_fetch.py")
    if not os.path.exists(script):
        return False, "缺少 bifaw_fetch.py"
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
    if ok:
        # 再验一遍是不是「今天」的列表，挡住源站没刷新时留下的昨日残留
        ok, why = check_bifaw_day(path)
    if not ok:
        log("竞彩必发数据不可用（%s）→ 保留上一份 bf.json" % why)
        if had and os.path.exists(backup):
            shutil.copy2(backup, path)
    else:
        log("竞彩必发数据正常：%s" % why)
    if os.path.exists(backup):
        os.remove(backup)
    return ok, why


# ---------------------------------------------------------------- 发布前净化
# 数据 JSON 与内联快照都会原样发布到公网，任何"从哪儿抓的"痕迹都不能带出去。
# 页面本身不读这些字段，它们纯粹是发布物上的指纹，所以在打包这一步直接摘掉。
_PRIVATE_KEYS = ("source", "sourceName", "sourceUrl", "url", "referer", "referrer")


def sanitize(d):
    """摘掉顶层的源站标识字段（只动顶层，不动 matches 里的业务字段）"""
    if isinstance(d, dict):
        for k in _PRIVATE_KEYS:
            d.pop(k, None)
    return d


def write_clean_json(src, dst, indent=2):
    """读 src → 净化 → 写 dst（不直接 copy，避免把指纹一起发布出去）"""
    with open(src, encoding="utf-8") as f:
        d = sanitize(json.load(f))
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=indent)
    return d


def sync_inline(use_spdex=True):
    """由各源的 json 重新生成内联快照（bifaw-inline.js / data-inline.js）。

    快照只是 file:// 双击打开时的兜底，必须和主数据始终一致。放在打包前统一重算，
    就不会出现「json 已经还原成旧的好数据、快照却还是刚写坏的 0 场空壳」这种半截状态
    —— 只还原 json 不还原快照，会把一份空壳打进发布包（踩过）。
    """
    pairs = [("bf.json", "bf-inline.js", "__BF__", "供 file:// 打开时使用")]
    if use_spdex:
        pairs.insert(0, ("data.json", "data-inline.js", "__DATA__",
                         "供 file:// 直接打开时使用"))
    for js, out, key, note in pairs:
        src = os.path.join(ROOT, js)
        if not os.path.exists(src):
            continue
        try:
            with open(src, encoding="utf-8") as f:
                d = sanitize(json.load(f))
            with open(os.path.join(ROOT, out), "w", encoding="utf-8") as f:
                f.write("/* 自动生成：数据快照，%s */\n" % note)
                f.write("window.%s = %s;\n"
                        % (key, json.dumps(d, ensure_ascii=False, separators=(",", ":"))))
        except Exception as exc:
            log("内联快照生成失败 %s：%s" % (out, exc))


# ---------------------------------------------------------------- 静态正文（给搜索引擎）
# 搜索引擎的抓取程序不执行 JS，只能读到静态 HTML。本页的数据全部由脚本渲染，
# 剥掉 <script> 后整页只剩 100 来字（实测），于是被判定成「空页」而收不进去。
# 所以在打包这一步把当天数据直接渲染成真实的 <table> 写进发布包：爬虫读到完整表格，
# 访客那边由页面脚本渲染出列表后把这块整块移除（见 index.html 的 dropSeoBody）。
# 两边看到的内容互不重复，也不需要各自维护一份模板。
#
# ⚠️ 下面的注释标记是 index.html 里的替换锚点，改名必须两边同步改。
STATIC_START = "<!--SEO_BODY_START-->"
STATIC_END = "<!--SEO_BODY_END-->"


def static_body(d):
    """把一份抓取结果渲染成静态正文（每场一个小标题 + 完整表格）。

    ⚠️ 渲染本体 2026-09-19 搬到了 archive.py（见 archive.match_articles）：
       归档页要用**同一份**表格模板，两处各写一份迟早走样。
       主页注入的 markup 与归档页逐字节一致，index.html 的 .seoblk / .datatable
       样式两边通用，这里只留一个转发。
    """
    return archive.seo_block(d)


def inject_static(page, block):
    """把静态正文填进页面里的锚点。锚点缺失时原样返回，并给出提示。"""
    a, b = page.find(STATIC_START), page.find(STATIC_END)
    if a < 0 or b < 0 or b < a:
        log("  提示：index.html 里没找到 %s / %s 锚点，静态正文未注入" % (STATIC_START, STATIC_END))
        return page
    return page[:a + len(STATIC_START)] + block + page[b:]


def render_home(links_days=None):
    """生成主页 HTML（静态正文 + 历史归档内链），返回字符串；读不到模板时返回 None。

    ⚠️ 抽成独立函数是为了让**定稿轮也能重建主页**（见 build_archive_files(also_home)）：
       白天各轮重建主页时，当天的归档链接还不在清单里（链路是「先上传归档、
       成功后才 save_days」）。所以 16:30 定稿之后必须在**同一轮内**用「含当天的清单」
       再重建一次主页 —— 否则 /football/ 上永远缺当天那条内链，
       要等到次日第一轮抓取才补上（2026-09-20 实测：主页只链到 09-19 与 09-17，
       而 /football/archive/ 索引页里 09-20 早就有了）。
    ⚠️ links_days 传入时用**它**算内链（定稿轮传 days_new），不传则读磁盘清单。
    ⚠️ 静态正文取自 bf.json（磁盘上当天最后一份数据），与白天最后一轮上传的内容一致。
    """
    page_src = os.path.join(ROOT, "index.html")
    if not os.path.exists(page_src):
        log("缺少 index.html")
        return None
    # 静态发布包：关掉「探测本地服务」的分支，否则每个访客都会白打一次 /api/status 404
    with open(page_src, encoding="utf-8") as f:
        page = f.read()
    flag = "var LIVE_PROBE = true;"
    if flag in page:
        page = page.replace(flag, "var LIVE_PROBE = false;", 1)
    else:
        log("  提示：index.html 里没找到 %s，静态包仍会探测本地服务" % flag)

    # 注入静态正文 —— 爬虫不执行 JS，没有这块页面在它们眼里就是空的（见 static_body）
    seo_src = os.path.join(ROOT, "bf.json")
    try:
        with open(seo_src, encoding="utf-8") as f:
            seo_d = sanitize(json.load(f))
        block = static_body(seo_d)
        page = inject_static(page, block)
        if block:
            log("  静态正文已注入（%d 场，%d 字节）"
                % (len(seo_d.get("matches") or []), len(block)))
    except Exception as exc:
        log("  提示：静态正文生成失败（%s），本轮页面不含静态表格" % exc)

    # 历史归档内链（第三档）：常驻在 .pageintro 里，**不会**被页面的 dropSeoBody 移除，
    # 所以访客也能点进去 —— 这是爬虫发现历史归档页的主入口。
    # 它只是导航、不依赖今天的抓取结果，所以抓取失败的那几轮照样注入。
    try:
        links = archive.links_html(links_days)
        if links:
            page, hit = archive.inject_links(page, links)
            if hit:
                log("  历史归档内链已注入（共 %d 天归档）" % len(archive.sorted_days(links_days)))
            else:
                log("  提示：index.html 里没找到 %s / %s 锚点，历史内链未注入"
                    % (archive.LINKS_START, archive.LINKS_END))
    except Exception as exc:
        log("  提示：历史内链生成失败（%s）" % exc)
    return page


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
    page = render_home()
    if page is None:
        return False

    with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(page)

    for p in data_files:
        # 不直接 copy：先把源站标识摘掉再落进发布包（见 sanitize）
        write_clean_json(p, os.path.join(DIST, os.path.basename(p)))

    # 内联快照（file:// 兜底）。下线源的不再带
    for name in ("data-inline.js", "bf-inline.js"):
        if name == "data-inline.js" and not use_spdex:
            continue
        src = os.path.join(ROOT, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DIST, name))

    # 清掉 dist/ 里的历史文件名 —— dist/ 是跨轮累积的，改过名的旧文件不清掉
    # 会一直躺在里面（线上的那份还得另外删一次）
    for legacy in ("bifaw.json", "bifaw-inline.js"):
        p = os.path.join(DIST, legacy)
        if os.path.exists(p):
            os.remove(p)

    # 清掉 dist/ 里已下线源的残留 —— dist/ 是跨轮累积的，
    # 不清理的话线上会一直留着一份几天前（甚至更早）的 data.json
    if not use_spdex:
        for name in ("data.json", "data-inline.js"):
            p = os.path.join(DIST, name)
            if os.path.exists(p):
                os.remove(p)

    with open(os.path.join(DIST, ".htaccess"), "w", encoding="utf-8", newline="\n") as f:
        f.write(HTACCESS)

    # dist/ 是跨轮累积的：清掉过期的归档日期目录，免得本地无限长胖。
    # 服务器上的历史归档不在 dist/ 里生成，所以这一句删不到它们。
    for _n in os.listdir(DIST):
        _p = os.path.join(DIST, _n)
        if os.path.isdir(_p) and archive.is_day_dir(_n):
            shutil.rmtree(_p, ignore_errors=True)

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


# ---------------------------------------------------------------- 按天归档页
def site_root_of(conf):
    """发布目录的上一级（也就是站点根），sitemap 要放那儿。

    归档页在 remote_dir/football/<date>/ 下面，而 sitemap-data.xml 必须在站点根
    （robots.txt 里写的是 https://90qu.com/sitemap-data.xml），所以得往上走一层。
    """
    c = conf.get("sftp" if conf.get("mode", "ftp") == "sftp" else "ftp") or {}
    rd = str(c.get("remote_dir") or "").rstrip("/")
    return rd.rsplit("/", 1)[0] if "/" in rd else ""


def build_archive_files(conf, d, final=False, also_home=False):
    """生成当天归档页 + 归档索引页 + sitemap，返回 (上传清单, 新清单, 归档日期)。

    ⚠️ 只在「数据确实是当天的」时候调用（--finalize 已把这件事校验过一遍）。
       抓取失败时磁盘上的 bf.json 是「保留的上一份」，CI 上那份更是仓库里的旧快照 ——
       拿它生成归档，等于把旧内容重复写成某一天的归档页，还会把过期日期塞进 sitemap。
    ⚠️ 返回的 days_new **不要在这里落盘**：要等上传成功之后再写（见 run()），
       否则一次上传失败就会在归档索引里留下线上并不存在的死链。

    final=True 表示这是 16:30 的定稿轮：给这一天的清单项打上 `fin` 标记。
    标记的作用是「这一天已经定稿过了」—— 16:50 的重试轮、以及之后任何轮次
    看到它就直接跳过，同一份内容不会被反复上传。

    also_home=True 时**顺带重建主页**（dist/index.html）并把它加进上传清单。
    ⚠️ 这件事必须在本函数里做、不能放到 run() 里另起一次：主页内链要用
       **含当天的 days_new**，而 days_new 只在本函数内构造、且要等上传成功才落盘。
       放到函数外就只能读到磁盘上那份「还没有当天」的清单，等于没修。

    典型调用（run()）：
        files, days, day = build_archive_files(conf, bf_data, final=True, also_home=True)
    """
    days = archive.load_days()
    days_new = dict(days)
    day = archive.data_day(d)
    # 合并而不是整条替换：这天可能已经有 fin 标记（比如 16:50 的重试轮），
    # 直接覆盖会把标记抹掉，于是又「没定稿」了。
    entry = dict(days.get(day) or {})
    # 星期序号过滤（archive.keep_same_weekday）：归档索引里记录的场次数 / 联赛，
    # 必须与当天归档页一致 —— 只算归档日当天的比赛，不算源站一次吐出的其它天场次。
    _kept = archive.keep_same_weekday(d.get("matches") or [], day)
    entry.update(archive.stat_of({"matches": _kept}))
    if final:
        entry["fin"] = True
    days_new[day] = entry

    _, pages = archive.write_archive(d, days_new, DIST)
    files = archive.archive_files(pages)

    # 定稿轮补上「当天的内链」：白天各轮重建主页时这天还没归档，
    # 不在这里重建的话 /football/ 要等到次日第一轮抓取才链上它（2026-09-20 修）。
    if also_home:
        home = render_home(days_new)
        if home:
            with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8", newline="\n") as f:
                f.write(home)
            files.append((os.path.join(DIST, "index.html"), "index.html"))
            log("  主页已重建：%s 的历史内链已补进 /football/（清单共 %d 天）"
                % (day, len(days_new)))
        else:
            log("  提示：主页模板缺失，本轮不重建主页（归档页与 sitemap 照常上传）")

    # sitemap 也在这里刷新：归档日一变、sitemap 不跟着变，百度就永远发现不了新归档页。
    # 它挂在站点根（比 remote_dir 高一层），所以用 abs: 规格把它送出 football/ 目录。
    seo_dir = os.path.join(DIST, "seo")
    os.makedirs(seo_dir, exist_ok=True)
    sp = os.path.join(seo_dir, "sitemap-data.xml")
    with open(sp, "w", encoding="utf-8", newline="\n") as f:
        f.write(archive.sitemap_xml(days_new))
    root = site_root_of(conf)
    if root:
        files.append((sp, "sitemap-data.xml", "abs:" + root))
    else:
        log("  提示：publish.json 里的 remote_dir 看不出站点根，本轮不更新 sitemap")

    fetched = str((d or {}).get("fetchedAt") or "")
    if fetched[:10] != day:
        log("  ⚠️ 数据日期(%s)与归档目录(%s)不一致，已按数据自己的日期归档" % (fetched[:10], day))
    n = len(d.get("matches") or [])
    log("归档页已生成：/football/%s/（%d 场，涉及 %d 个联赛）；归档索引共 %d 天；sitemap 已刷新"
        % (day, n, len(days_new[day].get("lg") or []), len(days_new)))
    return files, days_new, day


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


# ---------------------------------------------------------------- 上传目录规格
# 2026-09-19 起 files 里的每一项可以是两元组或三元组：
#   (本地, 远程文件名)                 → 传进 remote_dir（老行为，不变）
#   (本地, 远程文件名, "2026-09-19")   → 传进 remote_dir/2026-09-19/（归档页）
#   (本地, 远程文件名, "abs:/a/b")     → 传进该**绝对**目录（sitemap 要放站点根，
#                                        比 remote_dir 高一层，用相对路径表达不了）
# 归档页与 sitemap 都靠这个机制，所以 upload_ftp / upload_sftp 都要认三元组。
def norm_files(files):
    """统一成 (local, name, 目录规格) 三元组。"""
    out = []
    for item in files:
        if len(item) == 2:
            out.append((item[0], item[1], ""))
        else:
            out.append((item[0], item[1], item[2] or ""))
    return out


def resolve_remote(rd, spec):
    """把目录规格解析成 FTP/SFTP 上的目标目录（绝对路径）。"""
    if not spec:
        return rd.rstrip("/")
    spec = str(spec)
    if spec.startswith("abs:"):
        return spec[4:].rstrip("/")
    return rd.rstrip("/") + "/" + spec.strip("/")


def ftp_ensure_subdir(ftp, base, sub):
    """确保 base 下的子目录 sub 存在，返回可写的目录路径。

    ⚠️ DirectAdmin / cPanel 这类虚拟主机的 FTP 服务端对「绝对路径 MKD」支持不一致，
    所以先试绝对路径，失败再 cwd(base) 用相对名建一遍 —— 谁通用谁。
    """
    sub = sub.strip("/")
    d = base.rstrip("/") + "/" + sub
    try:
        ftp.mkd(d)
    except Exception:
        pass
    try:
        ftp.cwd(d)
        return d
    except Exception:
        pass
    ftp.cwd(base)
    try:
        ftp.mkd(sub)
    except Exception:
        pass
    ftp.cwd(sub)
    return ftp.pwd()


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
        missing = [n for n in ("index.html", "bf.json") if n not in names]
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

    rd = cfg["remote_dir"].rstrip("/")
    ftp_ensure_dir(ftp, rd)
    ftp.cwd(rd)
    log("进入远程目录：%s" % ftp.pwd())

    try:
        dirs = {}
        for local, name, spec in norm_files(files):
            if not spec:
                target = rd                              # 老行为：直接进 remote_dir
            else:
                target = dirs.get(spec)
                if target is None:
                    if str(spec).startswith("abs:"):
                        target = resolve_remote(rd, spec)
                        ftp_ensure_dir(ftp, target)      # 目标一般已存在，MKD 失败被忽略
                        ftp.cwd(target)                  # 走一遍确认这目录真能用
                    else:
                        target = ftp_ensure_subdir(ftp, rd, spec)
                    dirs[spec] = target
                    ftp.cwd(rd)                          # 恢复当前目录，下次从 rd 出发
            ftp_put(ftp, local, target, name)
            log("  已上传 %s (%.1f KB)%s"
                % (name, os.path.getsize(local) / 1024.0,
                   ("  -> %s/" % str(spec).replace("abs:", "")) if spec else ""))
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
def sftp_ensure_dir(sf, path):
    """逐级确保 SFTP 目录存在（与 ftp_ensure_dir 同义，SFTP 的 API 不一样）。"""
    cur = ""
    for p in [x for x in path.strip("/").split("/") if x]:
        cur += "/" + p
        try:
            sf.stat(cur)
        except IOError:
            sf.mkdir(cur)


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
    sftp_ensure_dir(sf, rd)

    try:
        dirs = {}
        for local, name, spec in norm_files(files):
            if not spec:
                target = rd
            else:
                target = dirs.get(spec)
                if target is None:
                    target = resolve_remote(rd, spec)
                    sftp_ensure_dir(sf, target)
                    dirs[spec] = target
            tmp = target + "/" + name + ".tmp"
            sf.put(local, tmp)
            try:
                sf.remove(target + "/" + name)
            except IOError:
                pass
            sf.rename(tmp, target + "/" + name)
            log("  已上传 %s (%.1f KB)%s"
                % (name, os.path.getsize(local) / 1024.0,
                   ("  -> %s/" % str(spec).replace("abs:", "")) if spec else ""))
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

    # ---------- -1. --archive-only：只补传归档页，绝不碰主页与数据文件 ----------
    # 刻意排在时段闸门**之前**：它不抓取、也不改主页，是纯手工的维护入口。
    # 两个用途：
    #   ① 首次上线 / 换主机后，单独把归档页铺上去验证 FTP 能不能建子目录；
    #   ② 某天归档那一步上传失败了，单独补一次 —— 不会碰到线上正在展示的当日数据。
    # ⚠️ 归档日期取自磁盘上 bf.json 的 fetchedAt，所以**先确认那份数据是哪天的**再跑。
    if args.archive_only:
        conf = load_conf()
        if not conf:
            log("RESULT: FAIL - publish.json is missing")
            return 1
        with open(os.path.join(ROOT, "bf.json"), encoding="utf-8") as f:
            d = sanitize(json.load(f))
        log("--archive-only：按磁盘上 bf.json（数据时间 %s）生成归档，只传归档相关文件"
            % (d.get("fetchedAt") or "未知"))
        files, days_new, day = build_archive_files(conf, d)
        mode = conf.get("mode", "ftp")
        ok = upload_sftp(conf["sftp"], files) if mode == "sftp" else upload_ftp(conf["ftp"], files)
        if not ok:
            log("RESULT: FAIL - archive upload failed")
            return 1
        # 这一条路径是手工触发的、且已经传成功，所以直接落清单
        archive.save_days(days_new)
        log("归档清单已更新：%s（共 %d 天）"
            % (os.path.basename(archive.INDEX_PATH), len(days_new)))
        log("RESULT: OK - uploaded %d archive file(s) for %s" % (len(files), day))
        return 0

    # ---------- -1b. --finalize：归档定稿（每天 16:30 那一轮） ----------
    # 它和 --archive-only 一样排在服务时段闸门**之前** —— 因为它本来就只能在窗口外跑。
    # 为什么单独一条路径：数据源 16:00 之后就不给数据了（实测 DOM 抽到 0 场），
    # 当天数据的「最终版」只能是 16:00 之前最后一次成功抓取，所以归档不在服务时段内
    # 零散地写，而是等到截止之后一次性定稿（页面上的时间口径也统一成 16:30）。
    if args.finalize:
        conf = load_conf()
        if not conf:
            log("RESULT: FAIL - publish.json is missing")
            return 1
        with open(os.path.join(ROOT, "bf.json"), encoding="utf-8") as f:
            d = sanitize(json.load(f))
        day = archive.data_day(d)
        today = archive.bj_today()
        days = archive.load_days()

        # 三道护栏。不满足就什么都不做 —— 节假日没有赛事、当天源站一直没给数据，
        # 都会走到这里，那是「无事可做」而不是故障，所以 SKIP 不影响 CI 判绿。
        # --force 越过全部护栏：手工补历史归档时用（比如把旧版式的归档页按新口径重刷）。
        if args.force:
            log("--force：越过「必须是今天 / 已过 16:00 / 尚未定稿」三道护栏")
        else:
            if day != today:
                log("磁盘上的 bf.json 是 %s 的数据，不是今天（%s）的 → 不归档"
                    % (day, today))
                log("RESULT: WARN - today's data was never captured; nothing to finalize")
                return 2
            if service_state() != "closed":
                log("现在北京时间 %s，还没过数据截止（%02d:%02d）→ 当天数据还会变，不提前定稿"
                    % (now_bj().strftime("%Y-%m-%d %H:%M"),
                       SERVICE_CLOSE[0], SERVICE_CLOSE[1]))
                log("RESULT: SKIP - before service close; not final yet")
                return 0
            if archive.is_final(days, day):
                log("%s 的归档已经定稿过了 → 跳过（同一份内容不重复上传）" % day)
                log("RESULT: SKIP - %s already finalized" % day)
                return 0

        log("归档定稿：数据采集于 %s → 按 %s %s 定稿"
            % (d.get("fetchedAt") or "未知", day, archive.FINALIZE_HM))
        files, days_new, day = build_archive_files(conf, d, final=True, also_home=True)

        if not args.upload:
            log("未加 --upload：归档页已生成到 dist/，没有联网上传")
            log("RESULT: OK - built archive for %s (no --upload)" % day)
            return 0

        mode = conf.get("mode", "ftp")
        ok = upload_sftp(conf["sftp"], files) if mode == "sftp" else upload_ftp(conf["ftp"], files)
        if not ok:
            log("RESULT: FAIL - archive upload failed")
            return 1
        # 上传成功之后才落清单：先记账后上传的话，一次失败就会在索引里
        # 留下一个线上并不存在的死链。
        archive.save_days(days_new)
        # archive_ok=1 让 CI 的回写步骤把 .archive-index.json 提交回仓库。
        # 这一步不抓数据，所以 data_ok 恒为 0（不能让 CI 以为抓到了新数据）。
        write_state(False, "归档定稿 %s" % day, archive_ok=True)
        log("归档清单已更新：%s（共 %d 天）"
            % (os.path.basename(archive.INDEX_PATH), len(days_new)))
        log("RESULT: OK - finalized %s: uploaded %d file(s)" % (day, len(files)))
        return 0

    # ---------- 0. 服务时段闸门 ----------
    # 免费账号只在北京时间 9:00-16:00 能看到数据，其余时间源站直接回绝 → 抓取必失败。
    # 关键：CI 是全新 checkout，「保留上一份」恢复的其实是仓库里那份旧快照，
    # 所以在时段外**一个文件都不能传**，让线上停在当天最后一次成功更新上。
    if not args.no_fetch and not args.ignore_window:
        st = service_state()
        if st != "open":
            bj = now_bj()
            log("现在北京时间 %s（更新时间 %02d:%02d-%02d:%02d）→ 本轮不抓取、不上传，"
                "线上保持当天最后一次有效更新"
                % (bj.strftime("%Y-%m-%d %H:%M"),
                   SERVICE_OPEN[0], SERVICE_OPEN[1],
                   SERVICE_CLOSE[0], SERVICE_CLOSE[1]))
            log("RESULT: SKIP - outside service window (09:00-16:00 Beijing)")
            return 0
        log("北京时间 %s，服务时段内，开始抓取" % now_bj().strftime("%Y-%m-%d %H:%M"))

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

    # ---------- 1b. 抓取竞彩必发（失败则沿用上一份，但必须报 WARN） ----------
    bifaw_ok, bifaw_why = True, "未抓取"
    if not args.no_fetch and not args.skip_bifaw:
        bifaw_ok, bifaw_why = fetch_bifaw()
    bifaw_stale = not bifaw_ok

    # ---------- 2. 校验「当前启用的源」+ 生成发布包 ----------
    if use_spdex:
        ok, why = check_data(data_path)
        if not ok:
            log("data.json 不可用（%s），中止" % why)
            log("RESULT: FAIL - local data.json unusable")
            return 2
    ok, why = check_bifaw(os.path.join(ROOT, "bf.json"))
    if not ok:
        log("bf.json 不可用（%s），中止" % why)
        log("RESULT: FAIL - local bf.json unusable")
        return 2

    if not build_dist(use_spdex):
        log("RESULT: FAIL - could not build dist/")
        return 1

    if not args.upload:
        log("未加 --upload：发布包已生成，可手动上传 dist/")
        if bifaw_stale:
            log("RESULT: WARN - package built; bifaw fetch failed (%s)" % bifaw_why)
            return 2
        if stale:
            log("RESULT: WARN - package built (spdex data stale)")
            return 2
        log("RESULT: OK - package built (no --upload flag)")
        return 0

    conf = load_conf()
    if not conf:
        log("RESULT: FAIL - publish.json is missing")
        return 1

    # ---------- 3. 决定本轮传什么 ----------
    # ⚠️ 关键规则：**只有本轮真抓到新数据，才传数据文件**。
    #    抓取失败时磁盘上的 bf.json 是「保留的上一份」，而 CI 每次都是全新 checkout，
    #    这份「上一份」其实是仓库里提交的旧快照 —— 传上去等于把线上刚更新的数据
    #    倒退回好几天前（2026-09-18 踩过：下午 71 场被晚间的旧快照覆盖）。
    #    页面文件（index.html）不受影响，改了页面照样能上线。
    data_files = active_data_files(use_spdex)
    data_ok = bifaw_ok and not stale
    fp = fingerprint(*data_files)
    # 记下「本轮有没有抓到可上线的新数据」，供 CI 决定要不要回写仓库快照。
    # --no-fetch 时磁盘上的数据不是本轮抓的，不算新数据（否则会把旧快照又提交一遍）。
    write_state(data_ok and not args.no_fetch and not args.page_only, bifaw_why)

    if data_ok and args.only_data and not args.force and fp and fp == read_fp():
        log("数据与上次上传一致，跳过上传（要强制上传加 --force）")
        log("RESULT: OK - data unchanged, nothing uploaded")
        return 0

    # ---------- 3b. 按天归档页：本轮**不**生成 ----------
    # 归档统一挪到每天 16:30 的定稿轮（--finalize，见上面 -1b）。
    # 原因：数据源 16:00 之后就抓不到了，服务时段内任何时刻生成的归档都只是
    # 「当日半成品」——改到截止后一次性定稿，页面上的时间口径才能统一成 16:30，
    # 归档页也才真正代表「这一天最终长什么样」。
    files = []
    if not args.only_data:
        files.append((os.path.join(DIST, "index.html"), "index.html"))
        files.append((os.path.join(DIST, ".htaccess"), ".htaccess"))
    if args.page_only:
        # 只发页面：数据文件一律不碰（本地磁盘上的数据不一定是当天的，传上去就可能倒退）
        log("--page-only：只传页面文件，数据文件保持线上不变")
    elif data_ok:
        if not args.only_data:
            snaps = ["bf-inline.js"] + (["data-inline.js"] if use_spdex else [])
            for snap in snaps:
                p = os.path.join(DIST, snap)
                if os.path.exists(p):
                    files.append((p, snap))
        for p in data_files:
            name = os.path.basename(p)
            packed = os.path.join(DIST, name)
            if os.path.exists(packed):
                files.append((packed, name))
    else:
        log("本轮没抓到可用数据（bifaw: %s%s）→ 只传页面文件，数据文件保持线上不变"
            % (bifaw_why, "，spdex 陈旧" if stale else ""))

    if not files:
        log("没有需要上传的文件")
        log("RESULT: WARN - nothing uploaded; bifaw fetch failed (%s)" % bifaw_why)
        return 2

    # ---------- 4. 上传 ----------
    mode = conf.get("mode", "ftp")
    ok = upload_sftp(conf["sftp"], files) if mode == "sftp" else upload_ftp(conf["ftp"], files)
    if ok:
        if data_ok:
            write_fp(fp)                   # 只有传了数据才记指纹（且必须是上传成功之后）
        log("上传完成 ✓  共 %d 个文件" % len(files))
        if bifaw_stale:
            log("RESULT: WARN - uploaded %d file(s); bifaw fetch failed (%s), kept previous data"
                % (len(files), bifaw_why))
            return 2                        # 2 = 数据不完整但不算故障（CI 出黄灯不出红灯）
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
    ap.add_argument("--page-only", action="store_true",
                    help="只上传页面文件（index.html/.htaccess），绝不动数据文件。"
                         "改了版式想立刻生效、又不在服务时段时用 —— 页面文件不含数据，"
                         "随时传都安全；数据文件则必须等窗口内抓到新数据才允许传")
    ap.add_argument("--ignore-window", action="store_true",
                    help="忽略 9:00-16:00 服务时段限制（想手动补一次时用，注意此时通常抓不到）")
    ap.add_argument("--archive-only", action="store_true",
                    help="只上传按天归档页 / 归档索引 / sitemap，绝不碰主页与数据文件。"
                         "归档日期取自磁盘上 bf.json 的 fetchedAt —— 先确认那份数据是哪天的再跑。"
                         "首次上线验证 FTP 子目录、或某天归档上传失败后单独补一次时用")
    ap.add_argument("--finalize", action="store_true",
                    help="归档定稿（CI 每天 16:30 那一轮）：不抓取，用磁盘上 bf.json 生成归档页、"
                         "归档索引与 sitemap 并上传，同时给这天打上「已定稿」标记。"
                         "三条护栏：数据日期必须是北京今天、必须已过 16:00、这天还没定稿过；"
                         "--force 越过全部护栏（手工补历史归档时用）。"
                         "不加 --upload 只生成到 dist/，不联网")
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

    # 先落一个「没抓到」的默认状态：run() 中途 return（例如服务时段 SKIP）
    # 时，CI 的回写步骤看到 data_ok=0 就会跳过提交。
    write_state(False, "未进入抓取流程")
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
