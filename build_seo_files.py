# -*- coding: utf-8 -*-
"""生成并上传站点级 SEO 文件（robots.txt / sitemap-data.xml）。

背景（2026-09-19 实测）：
  robots.txt 现在**是 WordPress 动态生成的**（站点根目录里没有物理文件），
  它输出的 Sitemap 指向 wp-sitemap.xml —— 那份永远不含 /football/ 与 /jc/。
  WordPress 只在「根目录没有物理 robots.txt」时才动态输出，所以上传一份物理文件
  就能覆盖它，不必动主题或 wp-config。

做法：
  1) 物理 robots.txt = WP 原版内容（Disallow /wp-admin/ + Allow admin-ajax）
     ＋ 追加一行 Sitemap: sitemap-data.xml
  2) sitemap-data.xml = 只含数据页（/football/、/jc/），lastmod 取北京时间当天。
     以后加「按天归档页」时，往 PAGES() 里追加即可。

用法：
  python build_seo_files.py             # 只生成到 seo/
  python build_seo_files.py --upload    # 生成后再 FTP 传到站点根
  python build_seo_files.py --check     # 只抓线上现状，不写不改
  python build_seo_files.py --verify <文件名或文件路径>
                                        # 传百度站点验证文件到站点根（支持两种输入）
                                        #   ① 给 `baidu_verify_codeva-XXXX.html`，自动生成
                                        #      内容 = 文件名去掉 .html 的那串 codeva-XXXX
                                        #   ② 给本地文件路径，原样上传
                                        # 也可用 --code <codeva-XXXX> 只给验证码
  python build_seo_files.py --verify-check <文件名>
                                        # 只探测 https://90qu.com/<文件名> 是否可访问
"""
import os
import io
import sys
import json
import time
import ftplib
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "seo")
CONF = os.path.join(ROOT, "publish.json")

SITE = "https://90qu.com"
WP_ROBOTS = """User-agent: *
Disallow: /wp-admin/
Allow: /wp-admin/admin-ajax.php
"""


def bj_day():
    """北京时间当天（不依赖本机时区）"""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


def pages():
    """要进 sitemap 的 URL。第三档的按天归档页将来追加在这里。"""
    return [
        ("%s/football/" % SITE, 0.9, "today 竞彩足球指数数据"),
        ("%s/jc/" % SITE, 0.9, "今日竞足赛事数据分析"),
    ]


def build_robots():
    return WP_ROBOTS + "\nSitemap: %s/wp-sitemap.xml\nSitemap: %s/sitemap-data.xml\n" % (SITE, SITE)


def build_sitemap():
    day = bj_day()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url, prio, _note in pages():
        parts.append("  <url>")
        parts.append("    <loc>%s</loc>" % url)
        parts.append("    <lastmod>%s</lastmod>" % day)
        parts.append("    <changefreq>daily</changefreq>")
        parts.append("    <priority>%.1f</priority>" % prio)
        parts.append("  </url>")
    parts.append("</urlset>")
    return "\n".join(parts) + "\n"


def write_files():
    if not os.path.isdir(OUT):
        os.makedirs(OUT)
    rp = os.path.join(OUT, "robots.txt")
    sp = os.path.join(OUT, "sitemap-data.xml")
    io.open(rp, "w", encoding="utf-8", newline="\n").write(build_robots())
    io.open(sp, "w", encoding="utf-8", newline="\n").write(build_sitemap())
    print("生成 %s" % rp)
    print("生成 %s" % sp)
    return [rp, sp]


def ftp_cfg():
    with io.open(CONF, encoding="utf-8") as f:
        c = json.load(f)["ftp"]
    remote = c["remote_dir"].rstrip("/")
    site_root = remote.rsplit("/", 1)[0]          # /domains/90qu.com/public_html
    return c, site_root


def fetch(url):
    req = urllib.request.Request(url + ("&" if "?" in url else "?") + "t=%d" % int(time.time()),
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read().decode("utf-8", "replace")


def check():
    print("--- 线上现状 ---")
    for name in ("/robots.txt", "/sitemap-data.xml", "/wp-sitemap.xml"):
        try:
            st, body = fetch(SITE + name)
            head = body.strip().splitlines()[:3]
            print("%-20s HTTP %s  len=%d  首行=%s" % (name, st, len(body), head[0][:70] if head else ""))
        except Exception as e:
            print("%-20s ERR %s" % (name, e))


def upload():
    files = write_files()
    c, site_root = ftp_cfg()
    print("FTP %s:%s -> %s" % (c["host"], c["port"], site_root))
    f = ftplib.FTP(timeout=30)
    f.connect(c["host"], int(c["port"]))
    f.login(c["user"], c["password"])
    f.set_pasv(True)
    f.cwd(site_root)
    for p in files:
        with open(p, "rb") as fh:
            n = f.storbinary("STOR " + os.path.basename(p), fh)
        print("  已上传 %s  (%s)" % (os.path.basename(p), n))
    names = []
    f.retrlines("LIST", names.append)
    keep = [x for x in names if ("robots" in x or "sitemap" in x)]
    print("  远端含 robots/sitemap 的条目: %s" % ("; ".join(keep) or "（无）"))
    try:
        f.quit()
    except Exception:
        f.close()


def ftp_open_site_root():
    c, site_root = ftp_cfg()
    f = ftplib.FTP(timeout=30)
    f.connect(c["host"], int(c["port"]))
    f.login(c["user"], c["password"])
    f.set_pasv(True)
    f.cwd(site_root)
    return f, site_root


def verify_upload(arg):
    """把百度站点验证文件传到站点根。arg 可以是文件名，也可以是本地文件路径。"""
    code = None
    for i, a in enumerate(sys.argv):
        if a == "--code" and i + 1 < len(sys.argv):
            code = sys.argv[i + 1].strip()
    if arg is None and not code:
        print("用法: --verify <文件名|文件路径>  或  --code <codeva-XXXXXX>")
        return 1
    if arg and os.path.isfile(arg):
        local, name = arg, os.path.basename(arg)
        print("本地文件: %s" % local)
    else:
        if code:
            name = code if code.lower().endswith(".html") else "baidu_verify_%s.html" % code
        else:
            name = os.path.basename(arg)
        if not name.lower().endswith(".html"):
            name += ".html"
        # 百度验证文件内容 = 文件名去掉扩展名
        body = name[:-5] if name.lower().endswith(".html") else name
        if not os.path.isdir(OUT):
            os.makedirs(OUT)
        local = os.path.join(OUT, name)
        io.open(local, "w", encoding="utf-8", newline="\n").write(body)
        print("已生成验证文件: %s\n  内容: %s" % (local, body))
    f, site_root = ftp_open_site_root()
    print("FTP -> %s" % site_root)
    with open(local, "rb") as fh:
        f.storbinary("STOR " + name, fh)
    print("  已上传 %s" % name)
    names = []
    try:
        f.retrlines("LIST", names.append)
        print("  远端含 baidu 的条目: %s"
              % ("; ".join([x for x in names if "baidu" in x.lower()]) or "（无）"))
    except Exception as e:
        print("  LIST ERR %s" % e)
    try:
        f.quit()
    except Exception:
        f.close()
    verify_check(name)
    return 0


def verify_check(name):
    name = os.path.basename(name)
    if not name.lower().endswith(".html"):
        name += ".html"
    url = "%s/%s" % (SITE, name)
    print("--- 探测 %s ---" % url)
    try:
        st, body = fetch(url)
        print("  HTTP %s  len=%d  body=%r" % (st, len(body), body[:80]))
        print("  %s" % ("✅ 可被百度抓取到" if st == 200 else "❌ 返回 %s" % st))
    except urllib.error.HTTPError as e:
        print("  ❌ HTTP %s（文件没传成功，或被 WP 重写吃掉）" % e.code)
    except Exception as e:
        print("  ❌ ERR %s" % e)


def arg_after(flag):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    elif "--upload" in sys.argv:
        upload()
        check()
    elif "--verify-check" in sys.argv:
        verify_check(arg_after("--verify-check"))
    elif "--verify" in sys.argv or "--code" in sys.argv:
        sys.exit(verify_upload(arg_after("--verify")))
    else:
        write_files()
