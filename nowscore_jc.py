# -*- coding: utf-8 -*-
"""
nowscore_jc.py —— 竞彩星期序号（周X0XX）补充源

背景
----
源站 bifaw（必发指数网）从 2026-09-21 起，不再在 leagueText 里带「周X序号」前缀
（leagueText 直接变空）。这让我们既没法在页面上显示「周二002」这种竞彩期号，
也丢了按星期区分竞彩期的能力。

nowscore 的竞彩销售页（buy/jingcai.aspx）仍然按竞彩期列出「周二001」这类序号，
可以作为**补充源**：把序号按（联赛 + 主客队 + 开赛时间）交叉匹配，回填进我们的比赛数据。

匹配键的设计（为什么不靠「按星期推算」）
----------------------------------------
- nowscore 行的 name="周二" 是「竞彩销售期所属日」，可能与真实开赛日历日差一天
  （凌晨 02:00 的比赛往往归到前一天的竞彩期），所以**绝不能**靠 kickoff 推出星期来匹配。
- 直接复制 nowscore 的 name+序号 即可拿到正确的竞彩期号；匹配本身只负责「找对那一行」。
- 匹配以（主队、客队）为主，联赛与开赛 HH:MM 作交叉校验：
  · 队名做过去 [排名] 后缀、去空格，并允许「互为前缀」容错
    （nowscore 常缩写队名，如「米尔顿」vs 我们侧的「米尔顿凯恩斯」）。
  · 只有「队名对上」且「联赛兼容 或 开赛时间一致」才补，避免纯队名巧合误补。

失败即跳过
----------
nowscore 抓不到 / 解析不出 / 网络异常 → 不动 leagueText（保持当前空值），绝不影响主流程。
本模块也不向任何发布物写入「来源」标识——补进去的只是中性的竞彩期号，不是源站名。
"""
import os
import re
import sys
import json
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
URL = "https://cp.nowscore.com/buy/jingcai.aspx?typeID=105&oddstype=2"

# 本机出网代理（仅作兜底；CI 上没这端口，不会误用）。
LOCAL_PROXY = "http://127.0.0.1:49886"


def _norm_team(s):
    """队名归一：去掉 [中超8] 这类排名后缀、去空格。"""
    s = str(s or "")
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = re.sub(r"\s+", "", s)
    return s.strip()


def _norm_league(s):
    return re.sub(r"\s+", "", str(s or "")).strip()


def _hm(kickoff):
    """bifaw kickoff '09-23 02:00' -> '02:00'。"""
    if not kickoff:
        return ""
    parts = str(kickoff).split()
    return (parts[-1] if parts else "").strip()


def fetch_rows():
    """抓取并解析 nowscore 竞彩页，返回 [{weekday, seq, league, home, away, hm}, ...]。"""
    req = urllib.request.Request(URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://cp.nowscore.com/",
    })
    # 1) 走环境代理（CI 若是 ubuntu 通常无，本机若设了 HTTP_PROXY 则自动生效）
    # 2) 直连  3) 本机代理兜底。任一种成功即用。
    html = None
    last_err = ""
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    attempts = []
    if env_proxy:
        attempts.append(("env", None))
    attempts.append(("direct", None))
    attempts.append(("localproxy", LOCAL_PROXY))
    for mode, proxy in attempts:
        try:
            if mode == "localproxy":
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
            else:
                opener = urllib.request.build_opener()
            with opener.open(req, timeout=40) as r:
                raw = r.read()
                enc = r.headers.get_content_charset() or "utf-8"
            html = raw.decode(enc, errors="replace")
            break
        except Exception as e:
            last_err = "%s:%s" % (mode, e)
            html = None
    if not html:
        raise RuntimeError("nowscore 抓取失败: " + last_err)
    return _parse(html)


def _clean(cell):
    return re.sub(r"<[^>]+>", "", str(cell or "")).strip()


def _parse(html):
    rows = []
    blocks = re.findall(r'<tr id="row_[0-9]+"[^>]*>[\s\S]*?</tr>', html)
    for b in blocks:
        m = re.search(r'id="row_([0-9]+)"\s+name="([^"]*)"', b)
        if not m:
            continue
        seq = m.group(1)[-3:]          # 行 id 末三位 = 竞彩序号
        weekday = m.group(2)           # 周二 / 周三 ...
        tds = re.findall(r"<td[^>]*>([\s\S]*?)</td>", b)
        if len(tds) < 8:
            continue
        # 联赛 / 开赛（HH:MM）
        league = _norm_league(_clean(tds[1]))
        hm = _clean(tds[2])
        # 主客队：找分隔符 '-'，取它前后最近的非空 td
        ttxt = [_clean(t) for t in tds]
        sep = next((i for i, t in enumerate(ttxt) if t == "-"), None)
        home = away = ""
        if sep is not None:
            for i in range(sep - 1, -1, -1):
                if ttxt[i]:
                    home = ttxt[i]
                    break
            for i in range(sep + 1, len(ttxt)):
                if ttxt[i]:
                    away = ttxt[i]
                    break
        if not (home and away):
            continue
        rows.append({
            "weekday": weekday,
            "seq": seq,
            "league": league,
            "home": _norm_team(home),
            "away": _norm_team(away),
            "hm": hm,
        })
    return rows


def _team_match(a, b):
    """队名对上：完全相同，或互为前缀（容错缩写）。"""
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _score(row, bl, bh, ba, bhm):
    """匹配打分：联赛兼容 +2，开赛时间一致 +3。0 分视为不可信。"""
    s = 0
    if bl and row["league"] and (bl == row["league"]
                                 or bl in row["league"] or row["league"] in bl):
        s += 2
    if bhm and row["hm"] and bhm == row["hm"]:
        s += 3
    return s


def _league_ok(row_league, bl):
    return bool(bl and row_league and
                (bl == row_league or bl in row_league or row_league in bl))


def enrich(matches):
    """把周X序号补进 matches（原地写 leagueText）。返回成功补充的场次数。

    匹配分两层，目的都是「找对 nowscore 那一行」，匹配到就直接抄它的
    竞彩期号（name+序号），不自己推算星期：
      Tier1  以（主队、客队）为主，联赛 / 开赛 HH:MM 作交叉校验；队名允许互为前缀
             （容错 nowscore 的常见缩写，如「米尔顿」vs 我们的「米尔顿凯恩斯」）。
      Tier2  队名对不上（如亚运队被缩成「韩国亚/沙特亚」）时，退回（联赛 + 开赛 HH:MM）
             精确匹配——同一场真实比赛两边联赛与开赛时间必然一致，且要求全局唯一，
             避免同名不同场的误补。
    """
    try:
        rows = fetch_rows()
    except Exception as e:
        sys.stderr.write("[nowscore] 序号补充跳过：%s\n" % e)
        return 0
    if not rows:
        return 0
    done = 0
    for m in matches:
        if not isinstance(m, dict):
            continue
        bl = _norm_league(m.get("league"))
        bh = _norm_team(m.get("home"))
        ba = _norm_team(m.get("away"))
        bhm = _hm(m.get("kickoff"))
        chosen = None
        # ---- Tier1：队名匹配 ----
        cands = [r for r in rows
                 if _team_match(r["home"], bh) and _team_match(r["away"], ba)]
        if cands:
            best = max(cands, key=lambda r: _score(r, bl, bh, ba, bhm))
            if _score(best, bl, bh, ba, bhm) > 0:   # 须有联赛或时间支撑
                chosen = best
        # ---- Tier2：联赛 + 开赛时间 兜底（须全局唯一）----
        if chosen is None:
            lh = [r for r in rows
                  if _league_ok(r["league"], bl) and bhm and r["hm"] and bhm == r["hm"]]
            if len(lh) == 1:
                chosen = lh[0]
        if chosen is None:
            continue
        tag = "%s%s" % (chosen["weekday"], chosen["seq"])
        league = m.get("league") or ""
        m["leagueText"] = "%s(%s)" % (league, tag)
        done += 1
    return done


# --------------------------- 自测（python nowscore_jc.py）---------------------------
if __name__ == "__main__":
    try:
        rows = fetch_rows()
    except Exception as e:
        print("fetch_rows ERR:", e)
        sys.exit(1)
    print("解析到 %d 行 nowscore 竞彩：" % len(rows))
    for r in rows:
        print("  ", r["weekday"], r["seq"], r["league"], r["home"], "VS", r["away"], r["hm"])
    # 用本地抓取到的样本页测一下匹配（若仓库里没样本就跳过）
    sample = os.path.join(ROOT, "_nowscore_sample.html")
    if os.path.exists(sample):
        html = open(sample, encoding="utf-8").read()
        rows2 = _parse(html)
        print("样本页解析 %d 行" % len(rows2))
        # 造几条「带 [排名] 后缀 + 缩写」的 bifaw 风格比赛，验证容错
        synth = [
            {"league": "英锦赛", "home": "米尔顿凯恩斯[英甲4]", "away": "克劳利[英甲19]", "kickoff": "2026-09-23 02:00"},
            # 亚运队被 nowscore 缩成「韩国亚/沙特亚」，队名对不上 → 走 Tier2 (联赛+时间)
            {"league": "亚运男足", "home": "韩国[U23]", "away": "沙特阿拉伯", "kickoff": "2026-09-22 18:00"},
            # 样本里没有的西甲，应当匹配不到（保持 None）
            {"league": "西甲", "home": "皇家贝蒂斯[3]", "away": "赫塔费[15]", "kickoff": "2026-09-24 01:00"},
        ]
        n = enrich(synth)
        print("合成匹配补充 %d 场：" % n)
        for m in synth:
            print("  ", m.get("leagueText"), "|", m.get("home"), "VS", m.get("away"))
