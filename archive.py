# -*- coding: utf-8 -*-
"""按天归档页 —— /football/2026-09-19/ 这类「一天一个 URL」的快照页。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么要有它（第三档 SEO）

/football/ 是**同一个 URL 每天换内容**。搜索引擎只认它「当前那一份」，
昨天、前天的场次连同联赛名、队名、日期一起被覆盖掉了 ——
「2026年9月19日 竞彩 必发指数」「上海申花 必发指数」这类长尾词
一个都攒不下来。

归档页把每天的抓取结果固化成**独立 URL**，标题/正文里带日期、联赛与队名，
长尾词才有地方落脚，也才有内部链接把权重串起来。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

三个产物：
  1) /football/<date>/index.html    当天的数据快照（自包含：内联 CSS，不依赖外部文件）
  2) /football/archive/index.html   全部归档的索引页（按年月分组）
  3) /football/ 主页 .pageintro 里的「历史数据」内链（最近 LINKS_LIMIT 天）

清单状态：`.archive-index.json`（只有日期与场次数，不含任何凭据）。
CI 每次都是全新 checkout，不把这个文件落仓库，下一轮就不知道历史上出现过哪些日期。
它由 workflow 的「回写数据快照到仓库」步骤提交 —— 与 `.baidu-push-state` 同样的待遇。

⚠️ 四条硬约定（改这个文件前先读）：
  1) **只在 publish.py `--finalize`（归档定稿轮）里生成**，不在每轮抓取后生成。
     抓取失败时磁盘上那份是「保留的上一份」，拿它生成归档等于把旧内容重复写成
     某一天的归档（CI 上那份更是仓库里的旧快照）。
  2) **定稿时点统一为每天 16:30**（FINALIZE_HM）。竞彩必发只在北京 9:00-16:00 提供
     数据，16:00 之后**再也抓不到**（实测：DOM 抽到 0 场），所以「当日最终数据」
     只能用当天最后一次成功抓取、在 16:00 之后定稿。页面/描述/索引里的时间口径
     一律写「<日期> 16:30 定稿」，**不写每轮各不相同的抓取时刻**
     （2026-09-19 用户要求统一口径）。
  3) **上传成功之后才把日期写进 `.archive-index.json`**。先记账后上传的话，
     一次上传失败就会在索引里留下一个线上并不存在的死链。
  4) 归档页的日期取自数据里的 `fetchedAt[:10]`（北京时间），**不是**本机当天 ——
     页面显示的那份数据是哪天的，归档就挂在哪天，自洽。
"""
import datetime
import html
import io
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))

SITE = "https://90qu.com"
FOOTBALL = SITE + "/football"
ARCHIVE_SLUG = "archive"                    # /football/archive/ —— 索引页
ARCHIVE_URL = "%s/%s/" % (FOOTBALL, ARCHIVE_SLUG)

INDEX_PATH = os.path.join(ROOT, ".archive-index.json")
LINKS_LIMIT = 30                            # 主页内链列最近多少天
SITEMAP_DAYS = 1000                         # sitemap 最多列多少个归档日（防无限膨胀）

# 「定稿」时刻 —— 唯一的文案来源，改这里就够，别在别处再写一遍 "16:30"。
# 为什么是 16:30：数据源（竞彩必发）只在北京时间 9:00-16:00 提供数据，16:00 之后
# 抓不到（DOM 抽 0 场）。所以当天数据的「最终版」= 16:00 前最后一次成功抓取，
# 定稿动作安排在 16:30（CI 的 cron 也是这个点，见 spdex-update.yml）。
# ⚠️ 它只影响**页面怎么写**；真正的定稿护栏在 publish.py --finalize：
#    「数据日期 == 北京今天」+「已过 16:00」+「这天还没定稿过」。
FINALIZE_HM = "16:30"

WEEK_CN = "一二三四五六日"                    # weekday(): 周一 = 0

# 主页 .pageintro 里的内链锚点（常驻，不会被页面的 dropSeoBody 移除，访客也看得到）。
# ⚠️ 改名必须同步改 index.html 里的同名注释。
LINKS_START = "<!--ARCHIVE_LINKS_START-->"
LINKS_END = "<!--ARCHIVE_LINKS_END-->"

# ---------------------------------------------------------------- 小工具


def esc(v):
    """HTML 转义。队名里有 [中超8] 这类方括号、联赛名可能带 &，不转义会破坏结构。"""
    return html.escape("" if v is None else str(v), quote=True)


def bj_today():
    """北京时间当天（不看本机时区）。"""
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")


def cn_date(day):
    """2026-09-19 → 2026年9月19日（周六）。解析不了就原样返回。"""
    try:
        d = datetime.datetime.strptime(str(day)[:10], "%Y-%m-%d")
    except Exception:
        return str(day)
    return "%d年%d月%d日（周%s）" % (d.year, d.month, d.day, WEEK_CN[d.weekday()])


def short_date(day):
    """2026-09-19 → 9月19日（主页内链用，省地方）。"""
    try:
        d = datetime.datetime.strptime(str(day)[:10], "%Y-%m-%d")
    except Exception:
        return str(day)
    return "%d月%d日" % (d.month, d.day)


def archive_url(day):
    return "%s/%s/" % (FOOTBALL, day)


def is_day_dir(name):
    """「2026-09-19」这种归档目录名。用来清理本地 dist/ 里的历史残留。"""
    s = str(name)
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return False
    try:
        datetime.datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False


def data_day(d):
    """从抓取结果里取归档日期。

    `fetchedAt` 是抓取脚本写的**北京时间**字符串（bifaw_fetch.build 里 strftime），
    取前 10 位就是数据日期。取不到就退回北京时间当天。
    """
    at = str((d or {}).get("fetchedAt") or "")
    day = at[:10]
    try:
        datetime.datetime.strptime(day, "%Y-%m-%d")
        return day
    except Exception:
        return bj_today()


# ---------------------------------------------------------------- 星期序号过滤
# ⚠️ 硬约定（需求 10，2026-09-19 定）：归档页只留「星期序号 == 归档日星期」的比赛。
#
# 竞彩必发源一次会吐出**多天**赛事：周六那天的抓取里往往混着「周日002-周日030」
# 这种次日的场次（序号挂在 leagueText 上，形如 `日职联(周六001)` / `英超(周日002)`）。
# 归档页按「哪天」挂 URL、标题/正文都写死当天日期，如果一页里塞两天比赛，
# 长尾词（"9月19日 竞彩 必发"）会对不上、还会把不属于这天的序号显示出来。
# 所以渲染归档页前，必须把非当天的星期序号场次删掉。
# 没有星期序号（tag=None）的比赛保守保留，避免误删真实数据。
def weekday_tag_of(day):
    """归档日期 -> 期望保留的星期标签，如 2026-09-19（周六）-> '周六'。"""
    try:
        d = datetime.datetime.strptime(str(day)[:10], "%Y-%m-%d")
        return "周" + WEEK_CN[d.weekday()]
    except Exception:
        return None


_WEEKDAY_RE = re.compile(r"周[一二三四五六日]")


def match_weekday_tag(m):
    """从一场比赛里抽星期序号标签：leagueText 形如 '日职联(周六001)' -> '周六'。
    抽不到返回 None（按「无星期序号」处理，不删）。"""
    s = str((m or {}).get("leagueText") or (m or {}).get("league") or "")
    hit = _WEEKDAY_RE.search(s)
    return hit.group(0) if hit else None


def keep_same_weekday(matches, day):
    """归档过滤：只留「星期序号 == 归档日星期」的比赛，删掉其它天的。"""
    tag = weekday_tag_of(day)
    if not tag:
        return list(matches or [])
    out = []
    for m in (matches or []):
        t = match_weekday_tag(m)
        if t is None or t == tag:
            out.append(m)
    return out


# ---------------------------------------------------------------- 清单读写


def load_days():
    """返回 {日期: {"n": 场次数, "lg": [联赛…]}}。

    文件缺失/损坏一律当空 —— 宁可少列几个历史链接，也不能让发布流程卡住。
    兼容早期只存 ["2026-09-19", …] 数组的格式。
    """
    try:
        with io.open(INDEX_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        days = raw.get("days") or {}
        if isinstance(days, list):
            days = dict((str(d), {}) for d in days)
        if not isinstance(days, dict):
            return {}
        out = {}
        for k, v in days.items():
            k = str(k)[:10]
            try:
                datetime.datetime.strptime(k, "%Y-%m-%d")
            except Exception:
                continue                        # 脏数据直接丢，别污染 sitemap
            out[k] = v if isinstance(v, dict) else {}
        return out
    except Exception:
        return {}


def save_days(days):
    """原子写（先写 .tmp 再 os.replace），避免 CI/本地同时跑时读到半截文件。"""
    tmp = INDEX_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(
            {"days": days,
             "updated": (datetime.datetime.now(datetime.timezone.utc)
                         + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=1, sort_keys=True))
        f.write("\n")
    os.replace(tmp, INDEX_PATH)
    return INDEX_PATH


def sorted_days(days=None):
    """全部日期，新的在前。"""
    days = load_days() if days is None else days
    return sorted(days.keys(), reverse=True)


def is_final(days, day):
    """这一天是否已经「定稿」（publish.py --finalize 成功传过）。

    定稿之后就不再重写：16:50 的重试轮、以及之后任何轮次看到它都会直接跳过 ——
    免得同一份内容反复上传、页面上的时间口径来回变。
    """
    return bool((days or {}).get(day, {}).get("fin"))


def stat_of(d):
    """从一份抓取结果里抽出归档页要用的统计（场次数 + 涉及哪些联赛）。"""
    lg = []
    for m in (d.get("matches") or []):
        name = str(m.get("league") or "").strip()
        if name and name not in lg:
            lg.append(name)
    return {"n": len(d.get("matches") or []), "lg": lg}


def sitemap_entries(days=None):
    """sitemap 要用到的 URL 清单 -> [(url, priority, changefreq, lastmod)]。

    ⚠️ `build_seo_files.py` 与 `baidu_push.py` 都从这里取，别在别处另抄一份
       （两处不一致 = sitemap 说有的页推不到，或者反过来）。
    """
    days = load_days() if days is None else days
    today = bj_today()
    ents = [
        ("%s/" % FOOTBALL, 0.9, "daily", today),
        ("%s/jc/" % SITE, 0.9, "daily", today),
        (ARCHIVE_URL, 0.5, "daily", today),
    ]
    for day in sorted_days(days)[:SITEMAP_DAYS]:
        ents.append((archive_url(day), 0.6, "never", day))
    return ents


def sitemap_xml(days=None):
    """sitemap-data.xml 的正文。

    ⚠️ 归档页的 lastmod 用**日期本身**而不是抓取时刻：归档页内容在当天会反复刷新，
       把 lastmod 写成分钟级的时刻只会天天抖动，告诉搜索引擎「这页一直在变」，
       反而降低抓取优先级。写日期才稳定。
    """
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url, prio, freq, lastmod in sitemap_entries(days):
        parts.append("  <url>")
        parts.append("    <loc>%s</loc>" % url)
        parts.append("    <lastmod>%s</lastmod>" % lastmod)
        parts.append("    <changefreq>%s</changefreq>" % freq)
        parts.append("    <priority>%.1f</priority>" % prio)
        parts.append("  </url>")
    parts.append("</urlset>")
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- 表格渲染
# 从 publish.py 搬过来的（原 static_body）。主页注入和归档页共用同一份渲染，
# 改字段/改样式只需要改这一处。
def match_articles(d):
    """把一份抓取结果渲染成「每场一个小标题 + 完整表格」。返回 (html, 场次数)。

    刻意不分页、不折叠、不排序：对爬虫来说，把后面的场次藏到别的 URL 上等于没收。
    """
    matches = d.get("matches") or []
    cols = d.get("columns") or []
    if not matches or not cols:
        return "", 0

    head = ('<tr><th class="itemcol">项</th>'
            + "".join("<th>%s</th>" % esc(c) for c in cols) + "</tr>")

    def row(r, extra=""):
        tds = "".join("<td>%s</td>" % esc(v) for v in (r.get("v") or []))
        return ('<tr><td class="teamname itemcell%s">%s</td>%s</tr>'
                % (extra, esc(r.get("item")), tds))

    parts = []
    for m in matches:
        body = "".join(row(r) for r in (m.get("rows") or []))
        ou = m.get("ou") or []
        if ou:                                     # 大小球盘：先一行分隔说明，再是它的三行
            sep = "大小球"
            if m.get("ouLine"):
                sep += " " + str(m["ouLine"])
            if m.get("ouTotal"):
                sep += "　总成交量 " + str(m["ouTotal"])
            body += ('<tr class="ou-sep"><td colspan="%d">%s</td></tr>'
                     % (len(cols) + 1, esc(sep)))
            body += "".join(row(r, " ou") for r in ou)

        title = esc(m.get("leagueText") or m.get("league") or "")
        if m.get("home"):
            title += "　%s VS %s" % (esc(m.get("home")), esc(m.get("away")))
        tail = []
        if m.get("kickoff"):
            tail.append("开赛时间 " + str(m["kickoff"]))
        if m.get("total"):
            tail.append("总成交量 " + str(m["total"]))
        parts.append(
            '<article class="seo-match">'
            '<h3>%s<span class="t">%s</span></h3>'
            '<div class="tablescroll"><table class="datatable">'
            "<thead>%s</thead><tbody>%s</tbody></table></div>"
            "</article>" % (title, esc(" · ".join(tail)), head, body))

    return "".join(parts), len(matches)


def seo_block(d):
    """主页 #seoBody 里那块全量表格副本（打包时注入，脚本渲染后由 dropSeoBody 移除）。"""
    arts, n = match_articles(d)
    if not n:
        return ""
    lead = "下表为当日全部 %d 场赛事的必发指数明细，数据更新于 %s。" % (
        n, esc(d.get("fetchedAt") or "-"))
    return ('<div class="seoblk" id="seoBody">'
            "<h2>今日竞彩必发指数（共 %d 场）</h2>"
            '<p class="seo-lead">%s</p>%s</div>' % (n, lead, arts))


# ---------------------------------------------------------------- 归档页


# 站点导航条：与 90qu.com 首页一致（菜单项 / 顺序 / 链接与首页 zibll 主题对齐）。
# ⚠️ 这是 index.html 里那条导航条的副本 —— 改了那边（菜单项、logo、配色）
#    记得同步改这里；反向同理。归档页不做 sticky，所以不需要 --navh。
NAV_CSS = """
.sitenav{background:#216d01;color:#fff;font-size:15px;
  box-shadow:0 1px 0 rgba(0,0,0,.12),0 2px 8px rgba(0,0,0,.14)}
.sitenav-in{max-width:986px;margin:0 auto;padding:0 12px;height:64px;
  display:flex;align-items:center}
.sn-brand{display:flex;align-items:center;flex:0 0 auto;margin-right:10px}
.sn-brand img{display:block;height:34px;width:auto}
.sn-menu{display:flex;align-items:center;gap:2px;flex:1 1 auto;min-width:0;
  list-style:none;margin:0;padding:0}
.sn-menu a{display:block;padding:15px 10px;line-height:20px;border-radius:4px;
  color:#fff;font-size:15px;white-space:nowrap;text-decoration:none;transition:background .15s}
.sn-menu a:hover{background:rgba(255,255,255,.16);color:#fff}
.sn-menu li.on a{background:rgba(0,0,0,.18);font-weight:700}
@media (max-width:760px){
  .sitenav-in{max-width:none;height:auto;flex-wrap:wrap;padding:6px 8px 0}
  .sn-menu{flex:1 1 100%;overflow-x:auto}
  .sn-menu a{padding:10px 8px;font-size:14px}
}
"""

NAV_HTML = """<nav class="sitenav">
  <div class="sitenav-in">
    <a class="sn-brand" href="https://90qu.com/" title="90qu-足球数据网">
      <img src="https://90qu.com/wp-content/uploads/2023/03/1.png" alt="90qu-足球数据网" />
    </a>
    <ul class="sn-menu">
      <li><a href="https://90qu.com/archives/category/vip">今日概览</a></li>
      <li><a href="https://90qu.com/jc/">竞足数据</a></li>
      <li class="on"><a href="https://90qu.com/football/">必发数据</a></li>
      <li><a href="https://90qu.com/xg/">xG积分榜</a></li>
      <li><a href="https://90qu.com/archives/category/%e6%95%99%e7%a8%8b">数据资源</a></li>
    </ul>
  </div>
</nav>"""

# 归档页自包含样式：刻意只写这一页要用的部分（表格 + 排版 + 导航条），
# 不引外部 css 文件 —— 少一个可能 404 的依赖，爬虫也只发一次请求。
PAGE_CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --brand:#ff9900; --head:#ffe6c0; --row:#f0f0f0; --row-team:#fff7ea;
  --ink:#2b3440; --ink-2:#4a5563; --muted:#5b6673; --line:#e6e9ee; --cream-l:#ffe6c0;
}
body{margin:0;background:#eef1f5;color:var(--ink);font-size:14px;line-height:1.6;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
  "Hiragino Sans GB","Microsoft YaHei",sans-serif}
.wrap{max-width:986px;margin:12px auto 40px;background:#fff;border-radius:4px;
  box-shadow:0 1px 3px rgba(0,0,0,.08);padding:18px 0 8px}
.crumb{margin:0 12px 12px;color:var(--muted);font-size:12.5px}
.crumb a{color:#216d01;text-decoration:none}
.crumb a:hover{text-decoration:underline}
h1{margin:0 12px 10px;font-size:20px;line-height:1.4;color:var(--ink)}
h2{margin:22px 12px 10px;padding-bottom:6px;border-bottom:1px solid var(--line);
  font-size:15px;color:var(--ink)}
.lead{margin:0 12px 14px;color:var(--muted);font-size:12.5px;line-height:1.9}
.lead b{color:var(--ink-2)}
.daynav{margin:0 12px 16px;padding:8px 10px;background:#f7f9fb;border:1px solid var(--line);
  border-radius:4px;font-size:12.5px;color:var(--muted);display:flex;flex-wrap:wrap;gap:6px 14px}
.daynav a{color:#216d01;text-decoration:none}
.daynav a:hover{text-decoration:underline}
.daynav .sep{color:#c6ccd4}
.lgsum{margin:0 12px 6px;font-size:12.5px;color:var(--muted)}
.lgsum ul{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:6px 8px}
.lgsum li{padding:2px 8px;border:1px solid var(--line);border-radius:12px;background:#fafbfc}
.seoblk{margin:0 12px}
.seoblk .seo-lead{margin:0 0 14px;color:var(--muted);font-size:12.5px;line-height:1.8}
.seoblk .seo-match{margin:0 0 16px}
.seoblk .seo-match h3{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  margin:0;height:30px;padding:0 10px;background:var(--brand);color:#fff;
  font-size:13px;font-weight:700}
.seoblk .seo-match h3 .t{margin-left:auto;font-size:12px;font-weight:400;color:var(--cream-l)}
.tablescroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.datatable{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}
.datatable th,.datatable td{border:1px solid #e3e6eb;padding:4px 6px;
  text-align:center;white-space:nowrap}
.datatable th{background:var(--head);font-weight:700}
.datatable td{background:var(--row)}
.datatable td.teamname{text-align:left;background:var(--row-team);white-space:normal;min-width:150px}
.datatable th.itemcol{width:52px}
.datatable td.itemcell{width:52px;text-align:center;font-weight:700;color:var(--ink)}
.datatable td.itemcell.ou{color:#2f6b1e}
.datatable tbody tr.ou-sep td{background:#fff7ea;color:#a06a2c;font-size:12px;
  text-align:left;padding-left:10px;height:24px}
/* 小屏：比赛标题条要允许换行。定高 30px 只适合桌面 —— 队名长的时候
   标题会折成两行，被 30px 的固定高度切掉半截、压在表格上。 */
@media (max-width:760px){
  .seoblk .seo-match h3{height:auto;min-height:30px;padding:6px 10px;line-height:1.5}
  .seoblk .seo-match h3 .t{margin-left:0}
  h1{font-size:18px}
}
.foot{margin:26px 12px 0;padding-top:14px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12.5px;line-height:1.9}
.foot p{margin:0 0 8px}
.sitefoot{max-width:986px;margin:0 auto 30px;padding:0 12px;color:#8b95a1;font-size:12px;
  text-align:center}
.sitefoot a{color:#216d01;text-decoration:none}
.sitefoot a:hover{text-decoration:underline}
"""


def _daynav(days, day, cls="daynav"):
    """前一天 / 返回今日 / 全部归档 / 后一天。days 已按倒序排列。"""
    ordered = sorted(days)                       # 升序，好取相邻
    prev_d = next_d = None
    if day in ordered:
        i = ordered.index(day)
        prev_d = ordered[i - 1] if i > 0 else None
        next_d = ordered[i + 1] if i + 1 < len(ordered) else None
    bits = []
    if prev_d:
        bits.append('<a href="%s">← %s</a>' % (archive_url(prev_d), cn_date(prev_d)))
    else:
        bits.append("<span>← 已是最早一天</span>")
    bits.append('<a href="%s/">今日数据</a>' % FOOTBALL)
    bits.append('<a href="%s">全部归档（%d 天）</a>' % (ARCHIVE_URL, len(days)))
    if next_d:
        bits.append('<a href="%s">%s →</a>' % (archive_url(next_d), cn_date(next_d)))
    else:
        bits.append("<span>已是最新一天 →</span>")
    sep = ' <span class="sep">|</span> '
    return '<div class="%s">%s</div>' % (cls, sep.join(bits))


def archive_page(d, day, days):
    """单日归档页。返回完整 HTML 字符串。

    ⚠️ 页面上的时间口径**一律是 FINALIZE_HM（16:30 定稿）**，不写实际抓取时刻：
       抓取时刻每轮都不同（15:40 / 15:50 / 15:53…），写进页面既杂乱、又会让同一页
       在 sitemap/百度眼里「一直在变」。归档页要表达的是「这一天最终长什么样」。
    """
    # 星期序号过滤（硬约定）：只留归档日当天的比赛，删掉其它天的。
    # 拷一份 d 把 matches 换成过滤后的，match_articles / stat_of 都读它。
    kept = keep_same_weekday(d.get("matches") or [], day)
    dd = dict(d)
    dd["matches"] = kept
    arts, n = match_articles(dd)
    stat = stat_of(dd)
    leagues = stat["lg"]
    cn = cn_date(day)

    title = "%s竞彩必发指数数据（共 %d 场） - 足球指数中心" % (cn, n)
    desc = ("%s竞彩足球必发指数归档数据：共 %d 场，逐场列出主胜、平局、客胜的买家挂牌、"
            "卖家挂牌、成交量、成交比例、赔付率、必发指数、平均欧指初终盘与凯利方差，"
            "并附大小球指数。本页为该日数据的最终留档，%s %s 定稿。" %
            (cn, n, day, FINALIZE_HM))
    lg_txt = "、".join(leagues[:8]) if leagues else ""

    parts = []
    parts.append('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>')
    parts.append('<meta charset="utf-8" />')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1" />')
    parts.append("<title>%s</title>" % esc(title))
    parts.append('<meta name="description" content="%s" />' % esc(desc))
    parts.append("<!-- 自指 canonical：归档页与 /football/ 是两份不同内容的页面，各自声明自己 -->")
    parts.append('<link rel="canonical" href="%s" />' % archive_url(day))
    parts.append('<meta property="og:type" content="article" />')
    parts.append('<meta property="og:title" content="%s" />' % esc(title))
    parts.append('<meta property="og:description" content="%s" />' % esc(desc[:120]))
    parts.append('<style>%s%s</style>' % (NAV_CSS, PAGE_CSS))
    parts.append("</head>\n<body>")
    parts.append(NAV_HTML)
    parts.append('<div class="wrap">')
    parts.append('<p class="crumb"><a href="https://90qu.com/">首页</a> › '
                 '<a href="%s/">竞彩必发指数数据</a> › '
                 '<a href="%s">历史归档</a> › <span>%s</span></p>'
                 % (FOOTBALL, ARCHIVE_URL, esc(day)))
    parts.append("<h1>%s 竞彩必发指数数据</h1>" % esc(cn))
    lead = ("本页是 <b>%s</b> 这一天采集到的竞彩足球必发（交易所）指数<b>归档快照</b>："
            "共 <b>%d 场</b>赛事%s。每场列出主胜、平局、客胜三项的买家挂牌、卖家挂牌、"
            "成交量、成交比例、赔付率、必发指数、赔指、盈亏，以及多家主流机构平均欧指的"
            "初盘与终盘、凯利指数、凯利方差、热度指数，并附大小球指数。"
            % (esc(day), n, ("，涉及%s" % esc(lg_txt)) if lg_txt else ""))
    lead += ("本页为该日数据的<b>最终留档</b>，于 %s %s 定稿，此后不再随后续交易日变化。"
             "想看过往其它日期，见页面底部的历史归档列表。" % (esc(day), FINALIZE_HM))
    parts.append('<p class="lead">%s</p>' % lead)
    parts.append(_daynav(days, day))

    if leagues:
        parts.append('<div class="lgsum"><h2>本日涉及的联赛</h2><ul>%s</ul></div>'
                     % "".join("<li>%s</li>" % esc(x) for x in leagues))

    parts.append("<h2>全部 %d 场赛事明细</h2>" % n)
    parts.append('<div class="seoblk">%s</div>' % arts)
    parts.append(_daynav(days, day))

    parts.append('<div class="foot">'
                 "<p>字段说明：「买家挂牌」「卖家挂牌」为交易所买方、卖方的挂单量；"
                 "「价位」为当前成交价位；「成交量」为已成交金额；「比例」为该赛果成交量"
                 "占全场成交的比重；「必指」为必发指数，即成交量归一后的强度值；"
                 "「赔指」为价位归一后的指数；「赔付率」为该赛果当前的赔付比率；"
                 "「欧初」「欧终」为多家主流机构平均欧指的初盘与终盘；"
                 "「凯指」「凯差」为凯利指数与凯利方差，衡量欧指相对真实概率的偏离程度；"
                 "「热指」为热度指数。</p>"
                 "<p>本页为历史归档，内容不再随后续交易日变化。"
                 '当日最新数据请看 <a href="%s/">竞彩必发指数数据</a>。</p>'
                 "</div>" % FOOTBALL)
    parts.append("</div>")
    parts.append('<div class="sitefoot">数据由本站自动采集整理，仅供研究与参考，不构成任何建议。<br />'
                 '<a href="https://90qu.com/">90qu-足球数据网</a> · '
                 '<a href="%s/">今日数据</a> · '
                 '<a href="%s">全部归档</a></div>' % (FOOTBALL, ARCHIVE_URL))
    parts.append("</body>\n</html>")
    return "\n".join(parts) + "\n"


def index_page(days):
    """归档索引页 /football/archive/ ：按年月分组列出全部归档日。"""
    ds = sorted_days(days)
    title = "竞彩必发指数历史数据归档 - 足球指数中心"
    desc = ("竞彩足球必发指数历史数据归档目录：按日期保存每日数据的最终留档（每天 %s 定稿），"
            "可回看任意一天的成交量、比例、赔付率、必发指数与凯利方差等数据。"
            "共收录 %d 天。" % (FINALIZE_HM, len(ds)))

    parts = []
    parts.append('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>')
    parts.append('<meta charset="utf-8" />')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1" />')
    parts.append("<title>%s</title>" % esc(title))
    parts.append('<meta name="description" content="%s" />' % esc(desc))
    parts.append('<link rel="canonical" href="%s" />' % ARCHIVE_URL)
    parts.append('<style>%s%s</style>' % (NAV_CSS, PAGE_CSS + """
.ylist{list-style:none;margin:0 12px;padding:0;display:flex;flex-wrap:wrap;gap:8px}
.ylist li{border:1px solid var(--line);border-radius:14px;background:#fafbfc;
  padding:3px 10px;font-size:12.5px}
.ylist a{color:#216d01;text-decoration:none}
.ylist a:hover{text-decoration:underline}
.ylist em{font-style:normal;color:#9aa3ae;margin-left:4px}
"""))
    parts.append("</head>\n<body>")
    parts.append(NAV_HTML)
    parts.append('<div class="wrap">')
    parts.append('<p class="crumb"><a href="https://90qu.com/">首页</a> › '
                 '<a href="%s/">竞彩必发指数数据</a> › <span>历史归档</span></p>' % FOOTBALL)
    parts.append("<h1>竞彩必发指数历史数据归档</h1>")
    parts.append('<p class="lead">本站每天采集竞彩足球必发（交易所）指数，'
                 "并在当日数据定版后（每天 %s）把它固化成一份当天独立的页面留档。"
                 "下面是全部归档日期，"
                 "点任意一天可以回看那天的全部场次明细（逐场给出主胜、平局、客胜的挂牌量、"
                 "成交量、成交比例、赔付率、必发指数、平均欧指与凯利方差，"
                 "并附大小球指数）。当前共收录 <b>%d</b> 天%s。</p>"
                 % (FINALIZE_HM, len(ds),
                    ("，最早到 %s" % cn_date(ds[-1])) if ds else ""))
    parts.append('<div class="daynav"><a href="%s/">← 返回今日数据</a></div>' % FOOTBALL)

    if not ds:
        parts.append("<h2>还没有归档</h2>")
        parts.append('<p class="lead">归档页会在每天 %s 归档定稿后自动生成，'
                     "现在还没有任何一天的数据被归档。请稍后再来，"
                     '或先看 <a href="%s/">今日竞彩必发指数数据</a>。</p>'
                     % (FINALIZE_HM, FOOTBALL))
    else:
        # 按「年-月」分组
        groups = {}
        for day in ds:
            groups.setdefault(day[:7], []).append(day)
        for ym in sorted(groups, reverse=True):
            y, m = ym.split("-")
            parts.append("<h2>%s 年 %d 月</h2>" % (y, int(m)))
            items = []
            for day in groups[ym]:
                st = days.get(day) or {}
                n = st.get("n")
                lg = st.get("lg") or []
                meta = ""
                if n:
                    meta = "<em>%d 场</em>" % n
                tip = (" · ".join(lg[:6])) if lg else ""
                items.append('<li><a href="%s" title="%s%s">%s</a>%s</li>'
                             % (archive_url(day), esc(cn_date(day)),
                                ("　" + esc(tip)) if tip else "", esc(short_date(day)), meta))
            parts.append('<ul class="ylist">%s</ul>' % "".join(items))

    parts.append('<div class="foot"><p>归档页内容为该日数据的最终留档（每天 %s 定稿），'
                 '不随后续交易日变化。'
                 '当日最新数据请看 <a href="%s/">竞彩必发指数数据</a>。</p></div>'
                 % (FINALIZE_HM, FOOTBALL))
    parts.append("</div>")
    parts.append('<div class="sitefoot">数据由本站自动采集整理，仅供研究与参考，不构成任何建议。<br />'
                 '<a href="https://90qu.com/">90qu-足球数据网</a></div>')
    parts.append("</body>\n</html>")
    return "\n".join(parts) + "\n"


def links_html(days=None, limit=LINKS_LIMIT):
    """主页 .pageintro 里的「历史数据」内链块。没有归档时返回空串（锚点保持干净）。"""
    days = load_days() if days is None else days
    ds = sorted_days(days)
    if not ds:
        return ""
    shown = ds[:limit]
    items = []
    for day in shown:
        n = (days.get(day) or {}).get("n")
        items.append('<li><a href="%s">%s%s</a></li>'
                     % (archive_url(day), esc(short_date(day)),
                        ("（%d 场）" % n) if n else ""))
    more = ""
    if len(ds) > len(shown):
        more = '<p class="archmore">更早的数据见 <a href="%s">历史数据归档（共 %d 天）</a>。</p>' \
               % (ARCHIVE_URL, len(ds))
    else:
        more = '<p class="archmore">全部归档见 <a href="%s">历史数据归档</a>。</p>' % ARCHIVE_URL
    return ("<h2>历史数据归档</h2>"
            '<p class="archlead">每天的数据都会在当日定版后单独留档一份（每天 %s 定稿），'
            "可按日期查看当天的全部场次明细（最近 %d 天）：</p>"
            '<ul class="archlist">%s</ul>%s'
            % (FINALIZE_HM, len(shown), "".join(items), more))


def inject_links(page, block):
    """把内链块填进主页锚点。锚点缺失时原样返回（老页面/忘了加锚点都不至于崩）。"""
    a, b = page.find(LINKS_START), page.find(LINKS_END)
    if a < 0 or b < 0 or b < a:
        return page, False
    return page[:a + len(LINKS_START)] + block + page[b:], True


# ---------------------------------------------------------------- 落盘


def write_archive(d, days, out_root):
    """把归档页 + 索引页写到 out_root 下。返回 (日期, 归档页路径, 索引页路径)。

    out_root 就是 dist/，所以最终结构是 dist/<date>/index.html 与 dist/archive/index.html。
    """
    day = data_day(d)
    arch_dir = os.path.join(out_root, day)
    idx_dir = os.path.join(out_root, ARCHIVE_SLUG)
    os.makedirs(arch_dir, exist_ok=True)
    os.makedirs(idx_dir, exist_ok=True)

    pages = {}
    p = os.path.join(arch_dir, "index.html")
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(archive_page(d, day, days))
    pages["archive"] = p

    p = os.path.join(idx_dir, "index.html")
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(index_page(days))
    pages["index"] = p
    return day, pages


def archive_files(pages):
    """转成 upload 用的 (local, name, remote_dir_spec) 三元组。"""
    return [
        (pages["archive"], "index.html", os.path.basename(os.path.dirname(pages["archive"]))),
        (pages["index"], "index.html", ARCHIVE_SLUG),
    ]
