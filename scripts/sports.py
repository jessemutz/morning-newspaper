#!/usr/bin/env python3
"""The sports page: your teams, drivers and players, from free public data (no API keys).

config.json:
  "sports": [
    {"league": "NHL", "follow": ["Avalanche"]},
    {"league": "F1",  "follow": ["Max Verstappen", "Kimi Antonelli", "Ferrari"]}
  ]
`follow` names are matched loosely ("Avalanche" finds "Colorado Avalanche"; a constructor such as
"Ferrari" covers both its drivers). Team-sport leagues come from ESPN's public site API (see
LEAGUES; any other ESPN league works with "espn": "<sport>/<league>", e.g. "soccer/ger.1").
F1 results and standings come from the Jolpica (Ergast) API, F1 news from ESPN.

Writes one article, pinned near the back of the edition (before recommendations).
"""
import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kobo_config import CFG, DATA_DIR  # noqa: E402

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
JOLPICA = "https://api.jolpi.ca/ergast/f1"
LEAGUES = {
    "NHL": "hockey/nhl", "NFL": "football/nfl", "NBA": "basketball/nba", "WNBA": "basketball/wnba",
    "MLB": "baseball/mlb", "MLS": "soccer/usa.1", "EPL": "soccer/eng.1", "PREMIER LEAGUE": "soccer/eng.1",
    "LA LIGA": "soccer/esp.1", "SERIE A": "soccer/ita.1", "BUNDESLIGA": "soccer/ger.1",
    "CHAMPIONS LEAGUE": "soccer/uefa.champions", "NWSL": "soccer/usa.nwsl",
    "NCAAF": "football/college-football", "NCAAM": "basketball/mens-college-basketball",
    "NCAAW": "basketball/womens-college-basketball",
}
UA = {"User-Agent": "Mozilla/5.0 (compatible; morning-paper/1.0)"}
e = html.escape


def log(msg):
    print(f"{datetime.now():%F %T} sports: {msg}", flush=True)


def get(url, **params):
    r = requests.get(url, params=params, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def local(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()


def when(dt: datetime) -> str:
    today = datetime.now().astimezone().date()
    day = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get((dt.date() - today).days, f"{dt:%a %b %-d}")
    return f"{day}, {dt:%-I:%M %p}".replace(":00 ", " ")


def matches(name: str, follows: list[str]) -> bool:
    """A followed name appears in `name` ("Avalanche" in "Colorado Avalanche")."""
    n = name.lower()
    return bool(n) and any(f.strip().lower() in n for f in follows if f.strip())


# --- team sports (ESPN) -----------------------------------------------------------------------

def _score(c):
    s = c.get("score")
    return s.get("displayValue") if isinstance(s, dict) else s


def team_block(path: str, team: dict) -> str:
    tid, name = team["id"], team["displayName"]
    sched = get(f"{ESPN}/{path}/teams/{tid}/schedule")
    info = sched.get("team", {})
    now = datetime.now(timezone.utc)
    events = sorted(sched.get("events", []), key=lambda ev: ev["date"])
    done = [ev for ev in events if ev["competitions"][0]["status"]["type"].get("completed")]
    upcoming = [ev for ev in events if not ev["competitions"][0]["status"]["type"].get("completed")
                and local(ev["date"]) >= now - timedelta(hours=4)]
    if not upcoming:  # the schedule endpoint can lag; the team endpoint knows the next game
        upcoming = get(f"{ESPN}/{path}/teams/{tid}").get("team", {}).get("nextEvent", [])
    lines = []
    rec = " &middot; ".join(e(x) for x in (info.get("recordSummary"), info.get("standingSummary")) if x)
    if done:
        ev = done[-1]
        comp = ev["competitions"][0]
        us = next((c for c in comp["competitors"] if c["team"]["id"] == tid), None)
        them = next((c for c in comp["competitors"] if c["team"]["id"] != tid), None)
        if us and them:
            res = "W" if us.get("winner") else ("L" if them.get("winner") else "T")
            where = "vs" if us.get("homeAway") == "home" else "at"
            lines.append(f"<b>Last:</b> {res} {_score(us)}-{_score(them)} {where} "
                         f"{e(them['team'].get('displayName', ''))} ({local(ev['date']):%a %b %-d})")
    if upcoming:
        ev = upcoming[0]
        comp = ev["competitions"][0]
        us = next((c for c in comp["competitors"] if c["team"]["id"] == tid), {})
        them = next((c for c in comp["competitors"] if c["team"]["id"] != tid), {})
        where = "vs" if us.get("homeAway") == "home" else "at"
        tv = ", ".join(b.get("media", {}).get("shortName", "") for b in comp.get("broadcasts", [])
                       if b.get("media", {}).get("shortName"))
        lines.append(f"<b>Next:</b> {where} {e(them.get('team', {}).get('displayName', ''))}, "
                     f"{when(local(ev['date']))}{f' ({e(tv)})' if tv else ''}")
    return (f"<h3>{e(name)}</h3>" + (f"<p>{rec}</p>" if rec else "") +
            "".join(f"<p>{ln}</p>" for ln in lines))


def news_block(path: str, follows: list[str], team_ids: set, limit: int = 3) -> str:
    try:
        arts = get(f"{ESPN}/{path}/news", limit=40).get("articles", [])
    except requests.RequestException:
        return ""
    named, tagged_only = [], []
    for a in arts:
        tagged = {str(c.get("teamId")) for c in a.get("categories", []) if c.get("teamId")}
        text = f"{a.get('headline', '')} {a.get('description', '')}".lower()
        if any(f.lower() in text for f in follows) or any(
                w in text for f in follows for w in f.lower().split() if len(w) > 4):
            named.append(a)
        elif tagged & team_ids and len(tagged) <= 2:  # tagged with your team, not a league roundup
            tagged_only.append(a)
    mine = named + tagged_only
    if not mine:
        return ""
    return "<h3>Headlines</h3>" + "".join(
        f"<p><b>{e(a['headline'])}</b><br/>{e(a.get('description', ''))}</p>" for a in mine[:limit])


def team_league(entry: dict) -> str:
    league = entry["league"]
    path = entry.get("espn") or LEAGUES.get(league.upper())
    if not path:
        return f"<h2>{e(league)}</h2><p>Unknown league; add \"espn\": \"sport/league\" to its entry.</p>"
    follows = entry.get("follow", [])
    teams = [t["team"] for t in get(f"{ESPN}/{path}/teams")["sports"][0]["leagues"][0]["teams"]]
    abbrevs = {f.strip().upper() for f in follows}
    picked = [t for t in teams if t.get("displayName") and (
        any(matches(t.get(k, ""), follows) for k in ("displayName", "shortDisplayName", "name"))
        or t.get("abbreviation", "").upper() in abbrevs)]
    out = [f"<h2>{e(league)}</h2>"]
    for t in picked:
        try:
            out.append(team_block(path, t))
        except (requests.RequestException, KeyError, ValueError) as ex:
            log(f"{t['displayName']}: {ex}")
    out.append(news_block(path, follows, {t["id"] for t in picked}))
    return "".join(out)


# --- F1 (Jolpica + ESPN news) -----------------------------------------------------------------------

def f1(entry: dict) -> str:
    follows = entry.get("follow", [])
    out = ["<h2>Formula 1</h2>"]
    last = get(f"{JOLPICA}/current/last/results.json")["MRData"]["RaceTable"]["Races"]
    if last:
        r = last[0]
        res = r["Results"]

        def drv(x):
            return f"{x['Driver']['givenName']} {x['Driver']['familyName']}"
        mine = [x for x in res if matches(drv(x), follows) or matches(x["Constructor"]["name"], follows)]
        out.append(f"<h3>{e(r['raceName'])} ({datetime.fromisoformat(r['date']):%b %-d})</h3>")
        out.append("<p><b>Podium:</b> " + ", ".join(
            f"{x['position']}. {e(drv(x))} ({e(x['Constructor']['name'])})" for x in res[:3]) + "</p>")
        if mine:
            out.append("<p><b>Your drivers:</b> " + ", ".join(
                f"{e(drv(x))} P{x['position']}" + (f" ({e(x['status'])})" if not x["status"].startswith(("Finished", "+"))
                                                   else "") for x in mine) + "</p>")
    ds = get(f"{JOLPICA}/current/driverStandings.json")["MRData"]["StandingsTable"]["StandingsLists"]
    cs = get(f"{JOLPICA}/current/constructorStandings.json")["MRData"]["StandingsTable"]["StandingsLists"]
    if ds:
        rows = ds[0]["DriverStandings"]
        leader = float(rows[0]["points"])
        mine = [x for x in rows if matches(f"{x['Driver']['givenName']} {x['Driver']['familyName']}", follows)
                or any(matches(c["name"], follows) for c in x["Constructors"])]
        out.append("<p><b>Drivers' standings:</b> " + "; ".join(
            f"P{x['position']} {e(x['Driver']['familyName'])} {x['points']} pts"
            + (f" (-{leader - float(x['points']):g})" if x["position"] != "1" else "")
            for x in (rows[:1] + [m for m in mine if m["position"] != "1"])) + "</p>")
    if cs:
        rows = cs[0]["ConstructorStandings"]
        mine = [x for x in rows if matches(x["Constructor"]["name"], follows)]
        if mine:
            out.append("<p><b>Constructors:</b> " + "; ".join(
                f"P{x['position']} {e(x['Constructor']['name'])} {x['points']} pts" for x in mine) + "</p>")
    nxt = get(f"{JOLPICA}/current/next.json")["MRData"]["RaceTable"]["Races"]
    if nxt:
        r = nxt[0]
        start = local(f"{r['date']}T{r.get('time', '12:00:00Z')}")
        out.append(f"<p><b>Next:</b> {e(r['raceName'])}, {e(r['Circuit']['Location']['locality'])}: "
                   f"{when(start)}</p>")
    out.append(news_block("racing/f1", follows, set()))
    return "".join(out)


# --- main -----------------------------------------------------------------------------------------

def build_page(entries: list[dict]) -> str:
    parts = []
    for entry in entries:
        try:
            parts.append(f1(entry) if entry["league"].upper() in ("F1", "FORMULA 1") else team_league(entry))
        except Exception as ex:  # one league failing must not cost the others
            log(f"{entry.get('league')}: {type(ex).__name__}: {ex}")
    return "".join(p for p in parts if p)


def main() -> int:
    entries = CFG.get("sports") or []
    if not entries:
        return 0
    body = build_page(entries)
    if not body:
        log("nothing to report")
        return 0
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    art = {"url": f"morning-paper://sports/{day}", "title": "Sports", "domain": "Sports desk",
           "published": day, "saved": now.isoformat(), "image": None,
           "pin_bottom": True, "pin_order": 0, "html": body}
    (DATA_DIR / "articles").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "articles" / f"sports-{day}.json").write_text(json.dumps(art, ensure_ascii=False))
    log(f"wrote sports page ({len(entries)} league(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
