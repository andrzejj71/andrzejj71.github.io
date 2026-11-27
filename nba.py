#!/usr/bin/env python3
"""Aktualizuje wszystkie dane NBA jednym poleceniem i zapisuje je w nba_data.json.

Pobiera: graczy, bilanse drużyn, wyniki, H2H, składy, terminarz, kontuzje oraz zdjęcia i loga
(do folderu img). Pliki tymczasowe są usuwane automatycznie.

Uruchom:  python nba.py          (wymaga: pip install nba_api)
"""
import json
import pandas as pd
import re
import requests
import sys
import time
import unicodedata
import urllib.request
import warnings
from collections import defaultdict
from datetime import datetime, timedelta
from nba_api.stats.endpoints import gamerotation, leaguegamelog
from nba_api.stats.endpoints import leaguegamelog, leaguestandingsv3, playerindex
from nba_api.stats.endpoints import leaguestandingsv3
from nba_api.stats.static import teams as static_teams
from pathlib import Path
import os
import shutil
import tempfile

# ---- USTAWIENIA ----
SEASON = "2026-27"
ALWAYS_INCLUDE = ["Ausar Thompson"]   # gracze dodawani zawsze (np. bez trafionej trójki); dopisz kolejnych po przecinku


def dunkapi_request(endpoint, payload):
    """Opcjonalny zapasowy odczyt DunkAPI. Klucz wyłącznie ze zmiennej środowiskowej."""
    api_key = os.getenv("DUNKAPI_API_KEY")
    if not api_key:
        return None
    try:
        response = requests.post(
            f"https://mcp.dunkapi.com/v1/{endpoint}",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=45,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise RuntimeError(body.get("message") or body["error"])
        return body.get("data", body)
    except Exception as exc:
        print(f"DunkAPI {endpoint} niedostępne: {exc}")
        return None

def step_players():
    """Gracze i ranking obrony przeciw pozycji."""
    LAST_N = 10      # ile ostatnich meczów na wykresie
    MIN_EACH = 1     # minimalna suma sezonowa każdej z wymaganych statystyk
    REQUIRED = ("PTS", "REB", "AST")   # wymagane statystyki (trójki nie są wymagane)
    KEYS = {"pts": "PTS", "reb": "REB", "ast": "AST", "tpm": "FG3M"}


    def fetch(endpoint, **kwargs):
        for _ in range(3):
            try:
                return endpoint(timeout=60, **kwargs).get_data_frames()[0]
            except Exception:
                time.sleep(3)
        raise SystemExit("Nie udało się pobrać danych z nba.com. Spróbuj ponownie za chwilę.")


    log = fetch(leaguegamelog.LeagueGameLog, season=SEASON, player_or_team_abbreviation="P")
    standings = fetch(leaguestandingsv3.LeagueStandingsV3, season=SEASON)
    index = fetch(playerindex.PlayerIndex, season=SEASON)

    records = {int(r.TeamID): [int(r.WINS), int(r.LOSSES)] for r in standings.itertuples()}
    positions = {int(r.PERSON_ID): r.POSITION for r in index.itertuples()}


    def norm(text):
        return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn").lower()


    always = {norm(n) for n in ALWAYS_INCLUDE}
    found = set()


    def default_line(values):
        return int(sum(values) / len(values)) + 0.5


    players = []
    for pid, games in log.groupby("PLAYER_ID"):
        games = games.sort_values("GAME_DATE")
        key_name = norm(games.iloc[-1].PLAYER_NAME)
        forced = key_name in always
        if forced:
            found.add(key_name)
        if not forced and any(games[col].sum() < MIN_EACH for col in REQUIRED):
            continue

        last = games.tail(LAST_N)
        latest = last.iloc[-1]
        stats = {}
        for key, col in KEYS.items():
            values = [int(x) for x in last[col]]
            stats[key] = [default_line(values), max(max(values) + 2, 6), values]

        name = latest.PLAYER_NAME
        record = records.get(int(latest.TEAM_ID))
        players.append({
            "id": int(pid),
            "n": name,
            "ini": "".join(w[0] for w in name.split()[:2]).upper(),
            "team": latest.TEAM_ABBREVIATION,
            "pos": positions.get(int(pid), "") or "NBA",
            "opp": None,
            "time": "",
            "home": False,
            "real": "nba.com",
            "rec": record,
            "recNote": "Brak bilansu drużyny w danych.",
            "o": [("@" if "@" in m else "") + m.split()[-1] for m in last.MATCHUP],
            "s": stats,
        })

    for wanted in ALWAYS_INCLUDE:
        if norm(wanted) not in found:
            print(f"Uwaga: nie znaleziono meczów gracza {wanted} w sezonie {SEASON} (może jeszcze nie zagrał albo nazwa jest inna).")

    # Ranking obrony przeciw pozycji: ile średnio tracą drużyny graczom danej pozycji (1 = tracą najwięcej)
    def pos_group(pid):
        p = positions.get(int(pid))
        return p[:1] if isinstance(p, str) and p else "F"


    log["pos"] = log.PLAYER_ID.map(pos_group)
    log["opp"] = log.MATCHUP.str.split().str[-1]
    cols = list(KEYS.values())
    per_game = log.groupby(["opp", "pos", "GAME_ID"])[cols].sum().reset_index()
    avg = per_game.groupby(["opp", "pos"])[cols].mean().reset_index()
    dvp = {}
    for pos, sub in avg.groupby("pos"):
        for key, col in KEYS.items():
            ranks = sub[col].rank(ascending=False, method="min")
            for team, rk in zip(sub["opp"], ranks):
                dvp.setdefault(team, {}).setdefault(pos, {})[key] = int(rk)
    with open("dvp.json", "w", encoding="utf-8") as f:
        json.dump(dvp, f, ensure_ascii=False, separators=(",", ":"))

    players.sort(key=lambda p: p["n"])
    with open("players.json", "w", encoding="utf-8") as f:
        json.dump(players, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Zapisano players.json: {len(players)} graczy")


def step_teams():
    """Bilanse drużyn."""
    def fetch():
        for _ in range(3):
            try:
                return leaguestandingsv3.LeagueStandingsV3(season=SEASON, timeout=60).get_data_frames()[0]
            except Exception:
                time.sleep(3)
        raise SystemExit("Nie udało się pobrać danych z nba.com. Spróbuj ponownie za chwilę.")


    abbreviations = {t["id"]: t["abbreviation"] for t in static_teams.get_teams()}
    result = []
    try:
        rows = fetch().to_dict("records")
        for row in rows:
            wins, losses = int(row["WINS"]), int(row["LOSSES"])
            games = wins + losses
            result.append({
                "team": abbreviations.get(int(row["TeamID"]), ""),
                "name": f'{row.get("TeamCity", "")} {row.get("TeamName", "")}'.strip(),
                "w": wins,
                "l": losses,
                "pct": round(wins / games * 100, 1) if games else 0,
                "home": row.get("HOME", ""),
                "road": row.get("ROAD", ""),
                "l10": row.get("L10", ""),
                "streak": row.get("strCurrentStreak", ""),
            })
    except SystemExit:
        print("NBA Stats nie zwróciło tabeli. Próbuję zapasowego DunkAPI...")
        backup = dunkapi_request("get_standings", {"season": 2027})
        standings = backup.get("standings", []) if backup else []
        if len(standings) != 30:
            raise SystemExit("Nie udało się pobrać kompletnych tabel z NBA API ani DunkAPI.")
        for row in standings:
            wins, losses = int(row["wins"]), int(row["losses"])
            games = wins + losses
            result.append({
                "team": row.get("abbrev", ""),
                "name": row.get("display_name", ""),
                "w": wins,
                "l": losses,
                "pct": round(float(row.get("win_pct", wins / games if games else 0)) * 100, 1),
                "home": f'{row.get("home_wins", 0)}-{row.get("home_losses", 0)}',
                "road": f'{row.get("away_wins", 0)}-{row.get("away_losses", 0)}',
                "l10": f'{row.get("last10_wins", 0)}-{10 - int(row.get("last10_wins", 0))}',
                "streak": "",
            })

    result.sort(key=lambda t: -t["pct"])
    with open("teams.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Zapisano teams.json: {len(result)} drużyn")
    for t in result[:5]:
        print(t["team"], f'{t["w"]}-{t["l"]}', f'{t["pct"]}%')


def step_detail():
    """Wyniki, H2H, składy, terminarz."""
    warnings.filterwarnings("ignore", category=DeprecationWarning)

    SEASONS = ["2026-27", "2025-26", "2024-25"]   # pierwszy = bieżący sezon; reszta tylko do H2H
    LAST_RESULTS = 10   # ile ostatnich wyników na wykresach
    LAST_LINEUP = 12    # z ilu ostatnich meczów liczymy starty i minuty
    TENTHS = 10         # GameRotation podaje czas w dziesiątych częściach sekundy
    QUARTER = 7200      # 12 minut w dziesiątych częściach sekundy


    SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"


    UPCOMING = []


    def next_games():
        """Najbliższy mecz każdej drużyny oraz terminarz na kolejne dni
        (oficjalny terminarz nba.com, a gdy się nie uda, tablice wyników)."""
        out = {}

        def add(home, away, ts="", label="", day=""):
            for team, opp, is_home in ((home, away, True), (away, home, False)):
                out.setdefault(team, {"opp": opp, "home": is_home, "ts": ts, "label": label, "date": day})
            UPCOMING.append({"home": home, "away": away, "ts": ts, "date": day})

        try:
            import requests
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com"}
            data = requests.get(SCHEDULE_URL, headers=headers, timeout=60).json()
            games = [g for d in data["leagueSchedule"]["gameDates"] for g in d["games"]
                     if g.get("gameStatus") == 1 and g["gameId"][:3] in ("002", "004")]
            for g in sorted(games, key=lambda g: g["gameDateTimeUTC"]):
                ts = g["gameDateTimeUTC"]
                day = (datetime.fromisoformat(ts.replace("Z", "+00:00")) - timedelta(hours=5)).strftime("%Y-%m-%d")
                add(g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"], ts, "", day)
            if out:
                days = sorted({g["date"] for g in UPCOMING})[:6]
                UPCOMING[:] = [g for g in UPCOMING if g["date"] in days]
                return out
        except Exception as e:
            print(f"Terminarz z cdn.nba.com niedostępny ({e}). Próbuję tablic wyników...")
        out.clear()
        UPCOMING.clear()

        try:
            from datetime import date as today_date
            from nba_api.stats.endpoints import scoreboardv2
            ids = {t["id"]: t["abbreviation"] for t in static_teams.get_teams()}
            for i in range(21):
                day = today_date.today() + timedelta(days=i)
                res = fetch(scoreboardv2.ScoreboardV2, game_date=day.strftime("%m/%d/%Y"))
                if res is None:
                    continue
                for r in res.get_data_frames()[0].itertuples():
                    if int(r.GAME_STATUS_ID) == 1 and str(r.GAME_ID)[:3] in ("002", "004"):
                        add(ids[int(r.HOME_TEAM_ID)], ids[int(r.VISITOR_TEAM_ID)], "", day.strftime("%d.%m"), day.strftime("%Y-%m-%d"))
                if len(out) >= 30 and i >= 6:
                    break
                time.sleep(0.6)
        except Exception as e:
            print(f"Uwaga: nie udało się pobrać terminarza ({e}), rywale nie będą wybierani automatycznie.")
        return out


    def fetch(fn, **kw):
        for _ in range(3):
            try:
                return fn(timeout=60, **kw)
            except Exception:
                time.sleep(3)
        return None


    logs = []
    for season in SEASONS:
        res = fetch(leaguegamelog.LeagueGameLog, season=season)
        if res is None:
            raise SystemExit("Nie udało się pobrać danych z nba.com. Spróbuj za chwilę.")
        logs.append(res.get_data_frames()[0])
    cur, everything = logs[0], pd.concat(logs)

    # H2H: ostatnie 3 spotkania każdej pary (we wszystkich pobranych sezonach)
    h2h = defaultdict(list)
    for gid, g in everything.groupby("GAME_ID"):
        if len(g) != 2:
            continue
        a, b = sorted(g.itertuples(), key=lambda r: r.TEAM_ABBREVIATION)
        h2h[f"{a.TEAM_ABBREVIATION}-{b.TEAM_ABBREVIATION}"].append(
            (a.GAME_DATE, a.TEAM_ABBREVIATION, int(a.PTS), b.TEAM_ABBREVIATION, int(b.PTS), gid))
    h2h_out = {}
    for key, games in h2h.items():
        games.sort(reverse=True)
        h2h_out[key] = [[f"{d[8:]}.{d[5:7]}.{d[:4]}", a, ap, b, bp] for d, a, ap, b, bp, _gid in games[:3]]

    # Top 3 (punkty, zbiórki, asysty) w ostatnich H2H: tylko gracze obecnych składów obu drużyn
    plogs = []
    for season in SEASONS:
        res = fetch(leaguegamelog.LeagueGameLog, season=season, player_or_team_abbreviation="P")
        if res is not None:
            plogs.append(res.get_data_frames()[0])
    h2h_top = {}
    if plogs:
        pall = pd.concat(plogs)
        current = plogs[0].sort_values("GAME_DATE").groupby("PLAYER_ID")["TEAM_ABBREVIATION"].last().to_dict()
        pall["cur"] = pall.PLAYER_ID.map(current)
        pall = pall[pall.cur == pall.TEAM_ABBREVIATION]
        for key, games in h2h.items():
            recent = games[:3]
            sub = pall[pall.GAME_ID.isin([g[5] for g in recent])]
            entry = {}
            for tm in key.split("-"):
                pts = [g[2] if g[1] == tm else g[4] for g in recent]
                part = sub[sub.TEAM_ABBREVIATION == tm].groupby("PLAYER_NAME")[["PTS", "REB", "AST"]].mean()
                tops = {k.lower(): [[nm, round(float(v), 1)] for nm, v in part[k].nlargest(3).items()] for k in ("PTS", "REB", "AST")}
                tops["avg"] = round(sum(pts) / len(pts), 1) if pts else None
                entry[tm] = tops
            h2h_top[key] = entry

    # Wyniki i bilans drużyn
    game_total = cur.groupby("GAME_ID")["PTS"].sum()
    teams, windows = {}, {}
    for tid, g in cur.groupby("TEAM_ID"):
        g = g.sort_values("GAME_DATE")
        last = g.tail(LAST_RESULTS)
        abbr = g.iloc[-1].TEAM_ABBREVIATION
        teams[abbr] = {
            "id": int(tid),
            "last": g.iloc[-1].GAME_DATE,   # data ostatniego meczu (do liczenia dni odpoczynku)
            "n": g.iloc[-1].TEAM_NAME,
            "rec": [int((g.WL == "W").sum()), int((g.WL == "L").sum())],
            "p": [int(x) for x in last.PTS],
            "o": [int(game_total[r.GAME_ID] - r.PTS) for r in last.itertuples()],
            "pa": [int(x) for x in g.PTS],   # punkty drużyny we wszystkich meczach sezonu
            "oa": [int(game_total[r.GAME_ID] - r.PTS) for r in g.itertuples()],   # punkty rywali w tych meczach
            "pl": [],
        }
        windows[int(tid)] = (abbr, list(g.tail(LAST_LINEUP).GAME_ID))

    # Skład: starty, minuty i kwartały z rotacji w każdym meczu
    needed = sorted({gid for _, ids in windows.values() for gid in ids})
    stats = defaultdict(list)   # (team_id, player_id) -> [(game_id, min, starter, full4, name)]
    for i, gid in enumerate(needed, 1):
        res = fetch(gamerotation.GameRotation, game_id=gid)
        if res is None:
            continue
        for df in res.get_data_frames():
            for (tid, pid), rows in df.groupby(["TEAM_ID", "PERSON_ID"]):
                tid, pid = int(tid), int(pid)
                if tid not in windows or gid not in windows[tid][1]:
                    continue
                quarters, minutes, starter = set(), 0.0, False
                for r in rows.itertuples():
                    start, end = float(r.IN_TIME_REAL), float(r.OUT_TIME_REAL)
                    minutes += (end - start) / TENTHS / 60
                    starter = starter or start == 0
                    for q in range(4):
                        if start < (q + 1) * QUARTER and end > q * QUARTER:
                            quarters.add(q)
                name = f"{rows.iloc[0].PLAYER_FIRST} {rows.iloc[0].PLAYER_LAST}"
                stats[(tid, pid)].append((gid, minutes, starter, len(quarters) == 4, name))
        print(f"\rMecze: {i}/{len(needed)}", end="", flush=True)
        time.sleep(0.6)
    print()

    for (tid, pid), rows in stats.items():
        n = len(rows)
        teams[windows[tid][0]]["pl"].append([
            rows[0][4],
            sum(1 for r in rows if r[2]),
            round(sum(r[1] for r in rows) / n, 1),
            round(100 * sum(1 for r in rows if r[3]) / n),
            n,
            1 if any(r[0] == windows[tid][1][-1] for r in rows) else 0,   # czy zagrał w ostatnim meczu drużyny
            pid,                                                           # numer gracza (do zdjęcia)
        ])
    for t in teams.values():
        t["pl"].sort(key=lambda p: (-p[1], -p[2]))
        t["pl"] = t["pl"][:8]

    upcoming = next_games()
    print(f"Terminarz: najbliższe mecze dla {len(upcoming)} drużyn.")
    for abbr, nxt in upcoming.items():
        if abbr in teams:
            teams[abbr]["next"] = nxt

    h2h_out["_schedule"] = UPCOMING   # terminarz na kolejne dni (dla bloku "Mecze następnego dnia")
    h2h_out["_top"] = h2h_top

    with open("teams_detail.json", "w", encoding="utf-8") as f:
        json.dump({"teams": teams, "h2h": h2h_out}, f, ensure_ascii=False, separators=(",", ":"))
    total_min = [sum(p[2] for p in t["pl"]) for t in teams.values() if t["pl"]]
    print(f"Zapisano teams_detail.json: {len(teams)} drużyn")
    print(f"Kontrola: suma średnich minut najczęściej grających ok. {sum(total_min) / len(total_min):.0f} (powinno być blisko 200-240)")


def step_injuries():
    """Kontuzje."""
    URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"


    def key(name):
        """To samo uproszczenie nazwy robi strona: małe litery, bez akcentów i znaków specjalnych."""
        plain = "".join(c for c in unicodedata.normalize("NFD", name) if unicodedata.category(c) != "Mn")
        return re.sub(r"[^a-z ]", "", plain.lower()).strip()


    try:
        data = requests.get(URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=60).json()
    except Exception as e:
        sys.exit(f"Nie udało się pobrać kontuzji ({e}).")

    out = {}
    for team in data.get("injuries", []):
        for item in team.get("injuries", []):
            name = (item.get("athlete") or {}).get("displayName") or ""
            if not name:
                continue
            details = item.get("details") or {}
            out[key(name)] = {
                "s": item.get("status", ""),                                   # np. Out, Day-To-Day
                "r": str(details.get("returnDate") or "")[:10],                # przewidywany powrót (RRRR-MM-DD)
                "d": details.get("type") or details.get("detail") or "",       # rodzaj urazu
                "t": team.get("displayName", ""),
            }

    if not out:
        sys.exit("API zwróciło pustą listę kontuzji lub zmieniło format. Nic nie zapisano.")

    with open("injuries.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    with_date = sum(1 for v in out.values() if v["r"])
    print(f"Zapisano injuries.json: {len(out)} graczy, w tym z datą powrotu: {with_date}")


def step_images():
    """Zdjęcia graczy i loga drużyn."""
    here = Path(__file__).resolve().parent
    data = json.loads((here / "nba_data.json").read_text(encoding="utf-8"))

    players, teams = set(), set()
    for p in data.get("players", []):
        if p.get("id"):
            players.add(int(p["id"]))
    for t in (data.get("detail") or {}).get("teams", {}).values():
        if t.get("id"):
            teams.add(int(t["id"]))
        for row in t.get("pl", []):
            if len(row) > 6:
                players.add(int(row[6]))

    HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nba.com/"}
    JOBS = [
        ("img/players", "png", "https://cdn.nba.com/headshots/nba/latest/260x190/{}.png", sorted(players)),
        ("img/teams", "svg", "https://cdn.nba.com/logos/nba/{}/global/L/logo.svg", sorted(teams)),
    ]

    failed = 0
    for folder, ext, url, ids in JOBS:
        out = here / folder
        out.mkdir(parents=True, exist_ok=True)
        for i, pid in enumerate(ids, 1):
            target = out / f"{pid}.{ext}"
            if target.exists() and target.stat().st_size > 0:
                continue
            try:
                r = requests.get(url.format(pid), headers=HEADERS, timeout=30)
                if r.status_code == 200 and r.content:
                    target.write_bytes(r.content)
                else:
                    failed += 1
            except Exception:
                failed += 1
            print(f"\r{folder}: {i}/{len(ids)}", end="", flush=True)
            time.sleep(0.1)
        print()
    print(f"Gotowe. Graczy: {len(players)}, drużyn: {len(teams)}, niepobranych: {failed}.")

def main():
    here = Path(__file__).resolve().parent
    work = Path(tempfile.mkdtemp(prefix="nba_"))
    os.chdir(work)
    ok = {}
    for name, fn, required in (("players", step_players, True), ("teams", step_teams, True),
                               ("detail", step_detail, False), ("injuries", step_injuries, False)):
        print(f"\n=== {fn.__doc__} ===")
        try:
            fn()
            ok[name] = True
        except SystemExit as e:
            print(f"Błąd: {e}")
            ok[name] = False
            if required:
                os.chdir(here)
                sys.exit("Nie udało się pobrać wymaganych danych. Plik nba_data.json nie został zapisany.")
        except Exception as e:
            print(f"Błąd: {e}")
            ok[name] = False
            if required:
                os.chdir(here)
                raise

    def load(fname):
        return json.loads((work / fname).read_text(encoding="utf-8"))

    data = {"players": load("players.json"), "teams": load("teams.json")}
    if (work / "dvp.json").exists():
        data["dvp"] = load("dvp.json")
    if ok.get("detail"):
        data["detail"] = load("teams_detail.json")
    if ok.get("injuries"):
        data["injuries"] = load("injuries.json")

    # Zachowaj ostatnie poprawne dane opcjonalne, gdy źródło chwilowo nie odpowiada.
    previous_path = here / "nba_data.json"
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            for key in ("dvp", "detail", "injuries"):
                if key not in data and key in previous:
                    data[key] = previous[key]
                    print(f"Zachowano poprzednią poprawną sekcję: {key}")
        except Exception as exc:
            print(f"Nie udało się odczytać poprzedniego JSON: {exc}")

    # Walidacja przed zapisem: nie publikuj pustych lub niepełnych danych podstawowych.
    if not isinstance(data.get("players"), list) or len(data["players"]) < 100:
        os.chdir(here)
        sys.exit("Walidacja nieudana: zbyt mało graczy. Poprzedni nba_data.json pozostaje bez zmian.")
    if not isinstance(data.get("teams"), list) or len(data["teams"]) != 30:
        os.chdir(here)
        sys.exit("Walidacja nieudana: tabela musi zawierać 30 drużyn. Poprzedni nba_data.json pozostaje bez zmian.")

    os.chdir(here)
    output = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    temp_json = here / "nba_data.json.tmp"
    temp_json.write_text(output, encoding="utf-8")
    temp_json.replace(here / "nba_data.json")
    print(f"\nZapisano nba_data.json (graczy: {len(data['players'])}, drużyn: {len(data['teams'])}).")

    print("\n=== Zdjęcia i loga ===")
    try:
        step_images()
    except BaseException as e:
        print(f"Uwaga: nie udało się pobrać zdjęć ({e}).")
    shutil.rmtree(work, ignore_errors=True)
    print("\nGotowe.")


if __name__ == "__main__":
    main()