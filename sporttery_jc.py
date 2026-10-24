# -*- coding: utf-8 -*-
"""sporttery_jc.py —— 竞彩期号（周X0XX）官方补充源

为什么需要它
------------
nowscore 的竞彩销售页（buy/jingcai.aspx）只列**在售**场次，状态为「待定/未开售」
（Define）的场次在它那里根本没有行，于是这些场次永远补不到周X序号：
  · 2026-10-24 周六007（英冠 查尔顿-诺维奇）、周六018（挪超 博德闪耀-瓦勒伦加）、
    周六030（巴甲 桑托斯-巴伊亚）三场在 bifaw 数据里 leagueText 全空。
官方赛程接口 getMatchListV1 返回**全量**（在售 + 待定，63 场），它的 matchNumStr
就是权威期号，正好补上这个缺口。

与 nowscore_jc 的分工
---------------------
· 本模块**优先**跑（官方源、含待定场次、且带真实开赛日期）。
· nowscore_jc 随后跑，只补本模块没覆盖到的场次（它覆盖的缩写/译名容错更宽）。
· 两者都「失败即跳过」，绝不打断必发主流程。

匹配为什么比 nowscore 更可靠
----------------------------
官方数据有 businessDate + matchTime，可还原**真实开赛日期时间**：
凌晨/上午场（matchTime < 12:00）属于下一自然日，与 bifaw 的 kickoff（MM-DD HH:MM）
可直接对到秒级分钟。于是匹配分三层：
  Tier1  主客队都匹配（互为前缀 / 编辑相似度 ≥0.85）→ 直接认。
  Tier2  真实开赛「日期+时间」完全一致 → 认；同时刻同联赛多场时挑队名相似度严格最优。
  Tier3  仅（联赛 + HH:MM）唯一 且 两队相似度之和 ≥0.6 → 认（容错极端缩写）。
Tier3 的相似度门槛是必须的：否则同一联赛同一时刻只列了一场时，会把另一场
「待定」比赛错认成它（2026-10-24 就出现过 桑德兰-利兹联 被标成「周六023 切尔西-热刺」）。
"""
import datetime
import difflib
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
URL = ("https://webapi.sporttery.cn/gateway/uniform/football/"
       "getMatchListV1.qry?clientCode=3001")

# 本机出网代理（仅兜底；CI 上没这端口，不会误用）。
LOCAL_PROXY = "http://127.0.0.1:49886"

# matchTime 小于该小时数视为「次日凌晨场」（竞彩一期从中午开到次日中午）
NOON = 12


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


def _real_date(business_date, hm):
    """businessDate + matchTime -> 真实开赛日期（凌晨/上午场 +1 天）。

    '2026-10-24' + '00:00' -> '2026-10-25'
    '2026-10-24' + '13:00' -> '2026-10-24'
    """
    try:
        d = datetime.date.fromisoformat(str(business_date)[:10])
    except Exception:
        return ""
    if not hm:
        return d.isoformat()
    try:
        hh = int(str(hm).split(":")[0])
    except Exception:
        return d.isoformat()
    if hh < NOON:
        d += datetime.timedelta(days=1)
    return d.isoformat()


def _fetch():
    req = urllib.request.Request(URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.sporttery.cn/jc/zqszsc/",
    })
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    attempts = []
    if env_proxy:
        attempts.append(("env", None))
    attempts.append(("direct", None))
    attempts.append(("localproxy", LOCAL_PROXY))
    raw = None
    last_err = ""
    for mode, proxy in attempts:
        try:
            if mode == "localproxy":
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
            else:
                opener = urllib.request.build_opener()
            with opener.open(req, timeout=40) as r:
                raw = r.read()
            break
        except Exception as e:
            last_err = "%s:%s" % (mode, e)
            raw = None
    if not raw:
        raise RuntimeError("官方赛程接口抓取失败: " + last_err)
    return json.loads(raw.decode("utf-8", errors="replace"))


def fetch_rows():
    """返回 [{weekday, seq, league, home, away, hm, date, status}, ...]"""
    data = _fetch()
    value = (data or {}).get("value") or {}
    out = []
    for day in value.get("matchInfoList") or []:
        bdate = day.get("businessDate") or ""
        for m in day.get("subMatchList") or []:
            numstr = str(m.get("matchNumStr") or "")
            mm = re.match(r"^周([一二三四五六日])(\d{3})$", numstr)
            if not mm:
                continue
            hm = str(m.get("matchTime") or "")[:5]
            out.append({
                "weekday": "周" + mm.group(1),
                "seq": mm.group(2),
                "league": _norm_league(m.get("leagueAbbName")),
                "home": _norm_team(m.get("homeTeamAbbName")),
                "away": _norm_team(m.get("awayTeamAbbName")),
                "hm": hm,
                "date": _real_date(bdate or m.get("businessDate"), hm),
                "status": m.get("matchStatus") or "",
            })
    return out


def _team_match(a, b):
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _league_ok(row_league, bl):
    return bool(bl and row_league and
                (bl == row_league or bl in row_league or row_league in bl))


def _sim(a, b):
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def _sim2(row, bh, ba):
    return _sim(row["home"], bh) + _sim(row["away"], ba)


def enrich(matches):
    """把官方期号补进 matches（原地写 leagueText）。返回成功补充场次数。

    只补 leagueText 里还没有「周X0XX」的场次，已被 nowscore 等补过的不动。
    """
    try:
        rows = fetch_rows()
    except Exception as e:
        sys.stderr.write("[sporttery] 期号补充跳过：%s\n" % e)
        return 0
    if not rows:
        return 0
    done = 0
    for m in matches:
        if not isinstance(m, dict):
            continue
        if re.search(r"周[一二三四五六日]\d{3}", m.get("leagueText") or ""):
            continue                      # 已有期号，不覆盖
        bl = _norm_league(m.get("league"))
        bh = _norm_team(m.get("home"))
        ba = _norm_team(m.get("away"))
        ko = str(m.get("kickoff") or "").strip()
        bhm = _hm(ko)
        bdate = ""
        if re.match(r"^\d{2}-\d{2}\s", ko):
            bdate = "2026-" + ko[:5]      # bifaw kickoff 不带年份，按当年补
        chosen = None

        # ---- Tier1：主客队都对上 ----
        t1 = [r for r in rows if _team_match(r["home"], bh) and _team_match(r["away"], ba)]
        if t1:
            def _s1(r):
                s = 0
                if _league_ok(r["league"], bl):
                    s += 2
                if bhm and r["hm"] == bhm:
                    s += 3
                if bdate and r["date"] == bdate:
                    s += 3
                return s
            best = max(t1, key=_s1)
            if _s1(best) > 0:
                chosen = best

        # ---- Tier2：真实开赛「日期 + 时间」完全一致 ----
        if chosen is None and bdate and bhm:
            t2 = [r for r in rows if r["date"] == bdate and r["hm"] == bhm]
            if len(t2) == 1:
                chosen = t2[0]
            elif len(t2) > 1:
                lg = [r for r in t2 if _league_ok(r["league"], bl)] or t2
                if len(lg) == 1:
                    chosen = lg[0]
                else:
                    sc = sorted(lg, key=lambda r: _sim2(r, bh, ba), reverse=True)
                    if len(sc) == 1 or _sim2(sc[0], bh, ba) - _sim2(sc[1], bh, ba) > 0.3:
                        if _sim2(sc[0], bh, ba) >= 0.6:
                            chosen = sc[0]

        # ---- Tier3：联赛 + HH:MM 唯一，且队名相似度达标 ----
        if chosen is None and bhm:
            t3 = [r for r in rows
                  if _league_ok(r["league"], bl) and r["hm"] == bhm]
            if t3:
                sc = sorted(t3, key=lambda r: _sim2(r, bh, ba), reverse=True)
                ok_len = (len(sc) == 1) or (_sim2(sc[0], bh, ba) - _sim2(sc[1], bh, ba) > 0.3)
                if ok_len and _sim2(sc[0], bh, ba) >= 0.6:
                    chosen = sc[0]

        if chosen is None:
            continue
        tag = "%s%s" % (chosen["weekday"], chosen["seq"])
        league = m.get("league") or ""
        m["leagueText"] = "%s(%s)" % (league, tag)
        done += 1
    return done


if __name__ == "__main__":
    try:
        rows = fetch_rows()
    except Exception as e:
        print("fetch_rows ERR:", e)
        sys.exit(1)
    print("官方赛程解析到 %d 场：" % len(rows))
    for r in rows:
        print("  %s%s %-6s %-10s VS %-10s %s %s %s"
              % (r["weekday"], r["seq"], r["league"], r["home"], r["away"],
                 r["date"], r["hm"], r["status"]))
