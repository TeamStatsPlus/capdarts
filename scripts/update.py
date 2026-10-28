#!/usr/bin/env python3
"""Download the LeagueLeader share reports and rebuild data.json."""
import datetime, json, pathlib, re, sys, time
import requests
from bs4 import BeautifulSoup, Comment

ROOT = pathlib.Path(__file__).resolve().parent.parent
HEADERS = {"User-Agent": "Mozilla/5.0 (dart-league-stats updater)"}


def clean(t):
    return re.sub(r"\s+", " ", t.replace("\xa0", " ")).strip()


def cells(tr):
    return [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]


def lines(cell):
    """Split a multi-line cell (text separated by <br> or runs of spaces)."""
    return [clean(x) for x in re.split(r"\n|\s{2,}", cell.get_text("\n")) if clean(x)]


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    if "Unable to display report" in soup.get_text():
        raise ValueError("LeagueLeader says this share link is invalid or expired")
    return soup


def table_after(soup, test):
    """First table that follows a piece of text (any tag) matching test()."""
    for node in soup.find_all(string=True):
        if isinstance(node, Comment):
            continue
        t = clean(str(node))
        if t and len(t) < 200 and test(t):
            tb = node.find_next("table")
            if tb:
                return tb
    return None


def table_with(soup, *words):
    for t in soup.find_all("table"):
        first = t.find("tr")
        if first and all(w in clean(first.get_text(" ")) for w in words):
            return t
    return None


def body(tbl):
    return tbl.find_all("tr")[1:]


def need(value, label):
    if not value:
        raise ValueError(f"nothing found for: {label}")
    return value


def parse_stats(soup):
    out = {}
    head = next((clean(h.get_text()) for h in soup.find_all(["h1", "h2"]) if "Report for" in clean(h.get_text())), "")
    m = re.search(r"Report for (\S+)\s*-\s*(.+)", head)
    if m:
        out["league"] = {"title": m.group(2).strip(), "location": m.group(1)}

    t = need(table_after(soup, lambda x: "Team Standings" in x), "Team Standings")
    out["standings"] = need([[r[0], int(r[3]), int(r[4])] for r in map(cells, body(t)) if len(r) >= 5 and r[3].isdigit()], "standings rows")

    t = table_after(soup, lambda x: "Last Match Results" in x)
    out["results"] = [[r[0], r[1], r[2], int(r[3]), int(r[5]), int(r[6])] for r in map(cells, body(t)) if len(r) >= 7 and r[3].isdigit()] if t else []

    out["improved"] = {}
    for key, word in (("c", "Cricket"), ("x", "X01")):
        t = table_after(soup, lambda x, w=word: "Most Improved" in x and w in x)
        out["improved"][key] = [[r[0], r[1], float(r[2]), float(r[3])] for r in map(cells, body(t)) if len(r) >= 4] if t else []

    t = need(table_after(soup, lambda x: "sorted by MPR" in x), "Cricket table")
    out["cricket"] = need([r[:2] + [float(r[2])] + [int(v) for v in r[3:13]] for r in map(cells, body(t)) if len(r) >= 13], "cricket rows")

    t = need(table_after(soup, lambda x: "sorted by PPD" in x), "X01 table")
    out["x01"] = need([r[:2] + [float(r[2])] + [int(v) for v in r[3:10]] for r in map(cells, body(t)) if len(r) >= 10], "x01 rows")
    return out


def iso(d):
    try:
        return datetime.datetime.strptime(d, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return d


def parse_schedule(soup):
    teams = []
    roster = need(table_with(soup, "Team", "Site"), "team roster table")
    head = [clean(c.get_text(" ")) for c in roster.find("tr").find_all(["th", "td"])]
    has_captain = any("Captain" in h for h in head)  # some divisions list no captains
    for tr in body(roster):
        td = tr.find_all("td")
        if len(td) < 3:
            continue
        team = lines(td[2])
        cap = lines(td[3]) if has_captain and len(td) > 3 else []
        ph = lines(td[4]) if has_captain and len(td) > 4 else []
        if team:
            teams.append({"name": team[0], "site": team[1] if len(team) > 1 else "", "captain": cap[0] if cap else "", "phone": ph[0] if ph else ""})
    need(teams, "teams")

    sched, cur = [], None
    tbl = need(table_with(soup, "Home", "Away"), "schedule table")
    for tr in body(tbl):
        c = cells(tr)
        if len(c) < 5:
            continue
        if c[0].isdigit():
            cur = {"week": int(c[0]), "date": iso(c[1]), "note": c[5] if len(c) > 5 else "", "matches": []}
            sched.append(cur)
        elif cur and c[2] and c[3] and "BYE" not in (c[2], c[3]):
            cur["matches"].append([c[2], c[3]])
    return {"teams": teams, "schedule": need(sched, "schedule weeks")}


def parse_locations(soup):
    tbl = need(table_with(soup, "Location", "Machines"), "locations table")
    sites, cur = [], None
    for tr in body(tbl):
        c = cells(tr)
        if len(c) < 5:
            continue
        if c[0]:
            cur = {"name": c[0], "machines": int(c[1]) if c[1].isdigit() else 0, "phone": re.sub(r"^\w+:\s*", "", c[3]), "parts": [c[4]] if c[4] else []}
            sites.append(cur)
        elif cur and c[4]:
            cur["parts"].append(c[4])
    return {"sites": need([{"name": s["name"], "machines": s["machines"], "phone": s["phone"], "address": ", ".join(s["parts"])} for s in sites], "sites")}


JOBS = [("stats", parse_stats), ("schedule", parse_schedule), ("locations", parse_locations)]
DATA = ROOT / "data"


def slug(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def div_id(d):
    return slug(f"{d['season']} {d['day']} {d['name']}")


def build_catalog(divs, default):
    seasons = []
    for d in divs:
        s = next((x for x in seasons if x["name"] == d["season"]), None)
        if not s:
            s = {"name": d["season"], "days": []}
            seasons.append(s)
        dy = next((x for x in s["days"] if x["name"] == d["day"]), None)
        if not dy:
            dy = {"name": d["day"], "divisions": []}
            s["days"].append(dy)
        dy["divisions"].append({"name": d["name"], "id": div_id(d)})
    return {"default": default or div_id(divs[0]), "seasons": seasons}


def write_if_changed(path, new, stamp):
    old = json.loads(path.read_text()) if path.exists() else {}
    strip = lambda d: {k: v for k, v in d.items() if k != "updated"}
    if strip(new) == strip(old):
        return False
    if stamp:
        new["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(new, indent=1))
    return True


def build_players(divs):
    """One combined list of every player (Cricket + 01 stats) across all divisions."""
    out = []
    for d in divs:
        path = DATA / f"{div_id(d)}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        rec = {}
        for row in data.get("cricket", []):
            rec[(row[0].lower(), row[1])] = [row[0], row[1], div_id(d), row[2:], None]
        for row in data.get("x01", []):
            k = (row[0].lower(), row[1])
            if k in rec:
                rec[k][4] = row[2:]
            else:
                rec[k] = [row[0], row[1], div_id(d), None, row[2:]]
        out.extend(rec.values())
    return {"players": out}


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    divs = cfg["divisions"]
    DATA.mkdir(exist_ok=True)
    errors = []
    for d in divs:
        did = div_id(d)
        path = DATA / f"{did}.json"
        new = json.loads(path.read_text()) if path.exists() else {}
        for name, fn in JOBS:
            url = d.get(name)
            if not url:
                continue
            try:
                new.update(fn(fetch(url)))
                print(f"OK    {did} / {name}")
            except Exception as e:  # keep the old data for this report
                errors.append(f"{did}/{name}")
                print(f"FAIL  {did} / {name}: {e}")
            time.sleep(1)
        print(f"{did}: " + ("data changed" if write_if_changed(path, new, True) else "no changes"))
    print("players.json: " + ("changed" if write_if_changed(ROOT / "players.json", build_players(divs), True) else "no changes"))
    cat = build_catalog(divs, cfg.get("default"))
    print("catalog.json: " + ("changed" if write_if_changed(ROOT / "catalog.json", cat, False) else "no changes"))
    if errors:
        print("Problems with:", ", ".join(errors))
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
