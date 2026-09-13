# -*- coding: utf-8 -*-
"""
spdex_fetch.py —— 竞彩足球指数数据抓取器
把 c.spdex.com/spdexlehejc（超级指数网）的竞彩数据抓成 data.json，
供 index.html 前端直接渲染。

用法:
    python spdex_fetch.py                # 抓取当天默认期号
    python spdex_fetch.py 20260913       # 指定期号
    python spdex_fetch.py 20260913 out.json

依赖: 仅标准库 (urllib / re / json)
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://c.spdex.com/spdexlehejc"
IFRAME = "http://c.spdex.com/IFrame/IframeViewerQQ.aspx?id="
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 表格列（与页面一致，共 13 列）
COLUMNS = ["球队队名", "指数", "交易量", "比例", "赔付", "价位", "挂牌指数",
           "欧洲平均", "凯利方差", "大小交易量", "比例", "大小价位", "大小指数"]


def _get(url, data=None, referer=None):
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "ignore")


def _hidden(html, name):
    m = re.search(r'name="%s"[^>]*value="([^"]*)"' % re.escape(name), html)
    return m.group(1) if m else ""


def _strip(s):
    """去掉标签并还原常见实体，返回 (纯文本, 是否红色)"""
    red = "color:red" in s or "color: red" in s
    txt = re.sub(r"<[^>]+>", "", s)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").strip())
    return txt, red


def parse_matchlist(html):
    """解析列表页：返回 [(tid, 标题, 开赛时间), ...]"""
    out = []
    for m in re.finditer(
            r"<h3 tid='(\d+)'>(.*?)</h3><span class='matchtime'>开赛时间：([^<]*)</span>",
            html):
        out.append((m.group(1), m.group(2).strip(), m.group(3).strip()))
    return out


def fetch_list(date):
    """翻页抓取全部列表页，返回统一列表"""
    html = _get(BASE)
    pages = 1
    m = re.search(r"共\s*(\d+)\s*页", html)
    if m:
        pages = int(m.group(1))

    items = []
    seen = set()
    for t in parse_matchlist(html):
        if t[0] not in seen:
            seen.add(t[0]); items.append(t)

    for p in range(2, pages + 1):
        body = urllib.parse.urlencode({
            "__EVENTTARGET": "AspNetPager1",
            "__EVENTARGUMENT": str(p),
            "__LASTFOCUS": "",
            "__VIEWSTATE": _hidden(html, "__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": _hidden(html, "__VIEWSTATEGENERATOR"),
            "DropJcId": date,
            "AspNetPager1_input": str(p - 1),
        }).encode()
        html = _get(BASE, data=body, referer=BASE)
        for t in parse_matchlist(html):
            if t[0] not in seen:
                seen.add(t[0]); items.append(t)
        time.sleep(0.3)
    return pages, items


def fetch_detail(tid):
    """抓单场比赛的指数表格 + 底部数据条"""
    html = _get(IFRAME + str(tid), referer=BASE)

    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 13:
            continue
        cells = [_strip(c)[0] for c in tds]
        reds = [_strip(c)[1] for c in tds]
        rows.append({
            "name": cells[0], "idx": cells[1], "vol": cells[2], "pct": cells[3],
            "pay": cells[4], "payRed": reds[4], "price": cells[5],
            "listed": cells[6], "euro": cells[7], "kelly": cells[8],
            "ouVol": cells[9], "ouPct": cells[10], "ouPrice": cells[11],
            "ouIdx": cells[12],
        })

    total = ""
    m = re.search(r'<li class="cj">交易量总成交：([^<]*)</li>', html)
    if m:
        total = m.group(1).strip()

    upd = ""
    m = re.search(r"更新时间：([^<]*)", html)
    if m:
        upd = m.group(1).strip()

    return {"rows": rows, "total": total, "updateTime": upd}


def build(date, out_path):
    pages, items = fetch_list(date)
    matches = []
    for i, (tid, title, kickoff) in enumerate(items, 1):
        name = title.split("\\")[-1] if "\\" in title else title
        parts = re.split(r"\s+VS\s+", name)
        home = parts[0].strip() if parts else name
        away = parts[1].strip() if len(parts) > 1 else ""
        try:
            detail = fetch_detail(tid)
        except Exception as exc:                     # 单场失败不影响整体
            print("  ! %s 抓取失败: %s" % (tid, exc))
            detail = {"rows": [], "total": "", "updateTime": ""}
        matches.append({
            "id": tid,
            "no": "%03d" % i,
            "title": title,
            "home": home,
            "away": away,
            "kickoff": kickoff,
            "total": detail["total"],
            "updateTime": detail["updateTime"],
            "rows": detail["rows"],
        })
        print("  [%2d/%d] %s %s" % (i, len(items), detail.get("updateTime", ""), name))
        time.sleep(0.25)

    data = {
        "date": date,
        "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pages": pages,
        "columns": COLUMNS,
        "matches": matches,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 同步导出内联快照，保证 index.html 用 file:// 直接打开也能显示数据
    inline = out_path.replace(".json", "") + "-inline.js"
    if inline == out_path:
        inline = "data-inline.js"
    with open(inline, "w", encoding="utf-8") as f:
        f.write("/* 自动生成：由 spdex_fetch.py 从 %s 导出，供 file:// 直接打开时使用 */\n"
                % out_path)
        f.write("window.__DATA__ = %s;\n"
                % json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    print("\n完成: %d 场 -> %s" % (len(matches), out_path))
    print("内联快照 -> %s" % inline)


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y%m%d")
    out = sys.argv[2] if len(sys.argv) > 2 else "data.json"
    print("抓取期号 %s ..." % date)
    build(date, out)
