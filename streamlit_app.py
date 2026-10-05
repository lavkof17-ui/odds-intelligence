import math
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Odds Intelligence V3", page_icon="🎯", layout="wide")

BASE = "https://api.oddspapi.io/v4"
BOOKMAKER = "1xbet"
FOOTBALL_DATA_BASE = "https://www.football-data.co.uk/mmz4281"

st.title("🎯 Odds Intelligence — V3 Analytique")
st.caption("Scanner 1xBet + modèle statistique indépendant + value + confiance + backtest")


class QuotaError(Exception):
    pass


# =========================
# OddsPapi
# =========================
def api_get(path, params=None, timeout=60):
    key = st.secrets.get("ODDS_API_KEY", "")
    if not key:
        raise RuntimeError("Clé OddsPapi absente. Ajoute ODDS_API_KEY dans Streamlit > Secrets.")
    q = dict(params or {})
    q["apiKey"] = key
    r = requests.get(f"{BASE}/{path}", params=q, timeout=timeout)
    if r.status_code == 429:
        raise QuotaError("HTTP 429")
    if not r.ok:
        raise RuntimeError(f"OddsPapi HTTP {r.status_code}: {r.text[:250]}")
    return r.json()


def account_info():
    # /account is unmetered according to OddsPapi documentation.
    return api_get("account")


def extract_account(data):
    root = data if isinstance(data, dict) else {}
    candidates = [root]
    for k in ("account", "data", "user"):
        if isinstance(root.get(k), dict):
            candidates.append(root[k])
    subs = root.get("subscriptions")
    if isinstance(subs, list):
        candidates.extend(x for x in subs if isinstance(x, dict))
    limit = count = valid_until = None
    for d in candidates:
        limit = limit if limit is not None else d.get("request_limit")
        count = count if count is not None else d.get("request_count")
        valid_until = valid_until if valid_until is not None else d.get("valid_until")
    remaining = None
    if limit is not None and count is not None:
        try:
            remaining = max(0, int(limit) - int(count))
        except Exception:
            pass
    return limit, count, remaining, valid_until


# =========================
# Generic helpers
# =========================
def sget(obj, *keys):
    if not isinstance(obj, dict):
        return ""
    for k in keys:
        v = obj.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("teamName") or v.get("shortName") or v.get("title")
        if v not in (None, ""):
            return str(v)
    return ""


def iso_dt(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def normalize_name(x):
    x = unicodedata.normalize("NFKD", str(x or "")).encode("ascii", "ignore").decode("ascii")
    x = x.lower().replace("&", "and")
    x = re.sub(r"\b(fc|cf|afc|sc|ac|rc|calcio|club|fk)\b", " ", x)
    x = re.sub(r"[^a-z0-9]+", " ", x)
    return re.sub(r"\s+", " ", x).strip()


def match_team(name, candidates):
    n = normalize_name(name)
    if not n:
        return None
    mapping = {normalize_name(c): c for c in candidates}
    if n in mapping:
        return mapping[n]
    # Conservative fuzzy match. Avoid matching very short names.
    if len(n) >= 5:
        close = get_close_matches(n, mapping.keys(), n=1, cutoff=0.88)
        if close:
            return mapping[close[0]]
    return None


# =========================
# Market catalogue / odds
# =========================
@st.cache_data(ttl=21600, show_spinner=False)
def load_markets():
    data = api_get("markets", {"language": "en"})
    result = {}
    if isinstance(data, list):
        for m in data:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("marketId", ""))
            if not mid:
                continue
            outs = {}
            for o in m.get("outcomes") or []:
                if isinstance(o, dict):
                    oid = str(o.get("outcomeId", ""))
                    if oid:
                        outs[oid] = o.get("outcomeName") or ""
            result[mid] = {
                "name": m.get("marketName") or "",
                "type": m.get("marketType") or "",
                "handicap": m.get("handicap"),
                "outcomes": outs,
            }
    return result


def translate_market(name):
    n = (name or "").strip()
    low = n.lower()
    if "full time result" in low or low in {"1x2", "match result"}:
        return "Resultat final (1X2)"
    if "double chance" in low:
        return "Double chance"
    if "both teams" in low and "score" in low:
        return "Les deux equipes marquent"
    if "over" in low and "under" in low:
        return "Total buts - Plus/Moins"
    if "total" in low and "goal" in low:
        return "Total buts - Plus/Moins"
    if "asian handicap" in low or "handicap" in low:
        return "Handicap asiatique"
    return n or "Marche inconnu"


def parse_line(text):
    if text in (None, ""):
        return None
    # Preserve sign where supplied. Examples: -1.5/home, 2.5/over.
    for p in [x.strip() for x in str(text).split("/")]:
        try:
            x = float(p)
            if -20 <= x <= 100:
                return x
        except Exception:
            pass
    return None


def outcome_display(raw, market_name, home, away, line):
    o = str(raw or "").strip()
    low = o.lower()
    ml = (market_name or "").lower()
    if o == "1" and ("result" in ml or "1x2" in ml):
        return f"1 - {home}"
    if o.upper() == "X" and ("result" in ml or "1x2" in ml):
        return "X - Match nul"
    if o == "2" and ("result" in ml or "1x2" in ml):
        return f"2 - {away}"
    if low in ("over", "o", "plus"):
        return f"Plus de {line:g}" if isinstance(line, float) else "Plus de"
    if low in ("under", "u", "moins"):
        return f"Moins de {line:g}" if isinstance(line, float) else "Moins de"
    if low == "yes":
        return "Oui"
    if low == "no":
        return "Non"
    if low in ("home", "team 1"):
        return home
    if low in ("away", "team 2"):
        return away
    return o


def fixture_names(f):
    home = sget(f, "participant1Name", "homeTeamName", "homeName", "home")
    away = sget(f, "participant2Name", "awayTeamName", "awayName", "away")
    if not home or not away:
        ps = f.get("participants") or f.get("teams") or []
        if isinstance(ps, list):
            names = [sget(x, "name", "teamName", "shortName", "title") for x in ps if isinstance(x, dict)]
            names = [x for x in names if x]
            if len(names) >= 2:
                home, away = home or names[0], away or names[1]
    return home, away


def parse_bookmaker_fixture(item, catalog):
    bo = item.get("bookmakerOdds") or {}
    board = bo.get(BOOKMAKER)
    if not board and bo:
        board = next(iter(bo.values()))
    if not isinstance(board, dict):
        return []
    markets = board.get("markets") or {}
    rows = []
    for market_id, mdata in markets.items():
        if not isinstance(mdata, dict):
            continue
        mid = str(market_id)
        meta = catalog.get(mid, {})
        raw_market = meta.get("name") or mdata.get("marketName") or f"Marche {mid}"
        market = translate_market(raw_market)
        outs_meta = meta.get("outcomes", {})
        base_line = meta.get("handicap")
        outcomes = mdata.get("outcomes") or {}
        for oid, odata in outcomes.items():
            if not isinstance(odata, dict):
                continue
            players = odata.get("players")
            plist = list(players.values()) if isinstance(players, dict) else (players if isinstance(players, list) else [odata])
            for p in plist:
                if not isinstance(p, dict) or p.get("active", True) is False:
                    continue
                try:
                    price = float(p.get("price"))
                except Exception:
                    continue
                if price <= 1:
                    continue
                boid = p.get("bookmakerOutcomeId") or ""
                line = parse_line(boid)
                if line is None:
                    try:
                        line = float(base_line) if base_line not in (None, "") else None
                    except Exception:
                        line = None
                raw = p.get("outcomeName") or p.get("name") or p.get("label") or outs_meta.get(str(oid)) or str(oid)
                low_boid = str(boid).lower()
                if "/" in low_boid:
                    tail = low_boid.split("/")[-1]
                    if tail in ("over", "under", "yes", "no", "home", "away", "1", "x", "2"):
                        raw = tail
                rows.append({
                    "market_id": mid,
                    "market": market,
                    "raw_market": raw_market,
                    "outcome_id": str(oid),
                    "outcome_raw": str(raw),
                    "line": line,
                    "price": price,
                    "boid": str(boid),
                })
    return rows


@st.cache_data(ttl=30, show_spinner=False)
def get_1xbet_fixtures(hours):
    acc = account_info()
    limit, count, remaining, valid_until = extract_account(acc)
    if limit is not None and count is not None and int(count) >= int(limit):
        raise QuotaError(f"QUOTA:{count}:{limit}:{valid_until or ''}")

    tournaments = api_get("tournaments", {"sportId": 10})
    tids = []
    tmeta = {}
    if isinstance(tournaments, list):
        for t in tournaments:
            if isinstance(t, dict) and t.get("tournamentId") is not None:
                if t.get("futureFixtures", 1) != 0 or t.get("upcomingFixtures", 1) != 0:
                    tid = str(t["tournamentId"])
                    tids.append(tid)
                    tmeta[tid] = t
    if not tids:
        return [], {"limit": limit, "count": count, "remaining": remaining, "valid_until": valid_until}

    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)
    raw_items = []
    for i in range(0, len(tids), 100):
        chunk = tids[i:i + 100]
        data = api_get("odds-by-tournaments", {
            "tournamentIds": ",".join(chunk),
            "bookmakers": BOOKMAKER,
            "language": "en",
            "verbosity": 3,
        })
        if isinstance(data, list):
            raw_items.extend(data)
        elif isinstance(data, dict):
            raw_items.extend(data.get("data") or data.get("fixtures") or [data])

    catalog = load_markets()
    fixtures = []
    seen = set()
    for item in raw_items:
        if not isinstance(item, dict) or item.get("statusId") not in (None, 0) or not item.get("hasOdds", True):
            continue
        dt = iso_dt(item.get("startTime"))
        if not dt or not (now <= dt <= end):
            continue
        home, away = fixture_names(item)
        if not home or not away:
            continue
        tid = str(item.get("tournamentId", ""))
        league = item.get("tournamentName") or item.get("leagueName") or item.get("competitionName") or tmeta.get(tid, {}).get("tournamentName") or ""
        key = str(item.get("fixtureId"))
        if key in seen:
            continue
        seen.add(key)
        quotes = parse_bookmaker_fixture(item, catalog)
        for q in quotes:
            q.update({
                "home": home,
                "away": away,
                "league": league,
                "start": item.get("startTime"),
                "fixture_id": item.get("fixtureId"),
            })
        fixtures.append({
            "fixture_id": item.get("fixtureId"),
            "home": home,
            "away": away,
            "league": league,
            "start": item.get("startTime"),
            "quotes": quotes,
        })
    return fixtures, {"limit": limit, "count": count, "remaining": remaining, "valid_until": valid_until, "checked": len(raw_items)}


# =========================
# Independent football model
# =========================
LEAGUE_CODES = {
    "premier league": "E0", "championship": "E1", "league one": "E2", "league two": "E3",
    "national league": "E4", "la liga": "SP1", "primera division": "SP1", "segunda": "SP2",
    "bundesliga": "D1", "2 bundesliga": "D2", "serie a": "I1", "serie b": "I2",
    "ligue 1": "F1", "ligue 2": "F2", "eredivisie": "N1", "premier division": "N1",
    "primeira liga": "P1", "liga portugal": "P1", "jupiler": "B1", "pro league": "B1",
    "premiership": "SC0", "scottish premiership": "SC0", "super league": "G1",
    "super lig": "T1", "super ligue": "T1",
}


def league_code(name):
    n = normalize_name(name)
    for key, code in LEAGUE_CODES.items():
        if key in n:
            return code
    return None


@st.cache_data(ttl=21600, show_spinner=False)
def load_league_history(code, seasons=3):
    # Current season 2026/27 -> 2627, then 2526, 2425.
    frames = []
    for start_year in range(26, 26 - seasons, -1):
        suffix = f"{start_year:02d}{(start_year + 1):02d}"
        url = f"{FOOTBALL_DATA_BASE}/{suffix}/{code}.csv"
        try:
            df = pd.read_csv(url)
        except Exception:
            continue
        needed = {"HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"}
        if not needed.issubset(df.columns):
            continue
        df = df[list(needed)].copy()
        for c in ("FTHG", "FTAG"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"])
        if df.empty:
            continue
        df["FTHG"] = df["FTHG"].astype(float)
        df["FTAG"] = df["FTAG"].astype(float)
        df["season"] = suffix
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR", "season"])
    return pd.concat(frames, ignore_index=True)


def team_strengths(df):
    if df.empty:
        return None
    # Give recent seasons more weight while retaining history for early-season teams.
    season_order = {s: i for i, s in enumerate(sorted(df["season"].unique(), reverse=True))}
    work = df.copy()
    work["w"] = work["season"].map(lambda s: 1.0 / (1.0 + 0.55 * season_order.get(s, 2)))
    lh = (work["FTHG"] * work["w"]).sum() / work["w"].sum()
    la = (work["FTAG"] * work["w"]).sum() / work["w"].sum()
    stats = {}
    teams = sorted(set(work["HomeTeam"]).union(work["AwayTeam"]))
    for team in teams:
        h = work[work["HomeTeam"] == team]
        a = work[work["AwayTeam"] == team]
        wh = h["w"].sum(); wa = a["w"].sum()
        # Shrinkage prevents tiny samples from producing extreme ratings.
        hs = ((h["FTHG"] * h["w"]).sum() + 4 * lh) / (wh + 4)
        hc = ((h["FTAG"] * h["w"]).sum() + 4 * la) / (wh + 4)
        as_ = ((a["FTAG"] * a["w"]).sum() + 4 * la) / (wa + 4)
        ac = ((a["FTHG"] * a["w"]).sum() + 4 * lh) / (wa + 4)
        stats[team] = {
            "home_attack": hs / lh if lh else 1.0,
            "home_def": hc / la if la else 1.0,
            "away_attack": as_ / la if la else 1.0,
            "away_def": ac / lh if lh else 1.0,
        }
    return {"league_home": lh, "league_away": la, "teams": stats}


def poisson_pmf(lam, k):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def score_matrix(lh, la, max_goals=9):
    ph = [poisson_pmf(lh, i) for i in range(max_goals + 1)]
    pa = [poisson_pmf(la, i) for i in range(max_goals + 1)]
    matrix = [[ph[i] * pa[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    # Renormalize truncated tail.
    z = sum(map(sum, matrix))
    return [[x / z for x in row] for row in matrix]


def model_probs(strength, home, away):
    teams = strength["teams"]
    hm = match_team(home, teams.keys())
    am = match_team(away, teams.keys())
    hs = teams.get(hm, {"home_attack": 1, "home_def": 1, "away_attack": 1, "away_def": 1})
    ass = teams.get(am, {"home_attack": 1, "home_def": 1, "away_attack": 1, "away_def": 1})
    # Poisson expected goals.
    xh = max(0.15, strength["league_home"] * hs["home_attack"] * ass["away_def"])
    xa = max(0.10, strength["league_away"] * ass["away_attack"] * hs["home_def"])
    m = score_matrix(xh, xa)
    p_home = sum(m[i][j] for i in range(10) for j in range(10) if i > j)
    p_draw = sum(m[i][j] for i in range(10) for j in range(10) if i == j)
    p_away = sum(m[i][j] for i in range(10) for j in range(10) if i < j)
    p_btts = 1 - sum(m[0][j] for j in range(10)) - sum(m[i][0] for i in range(1, 10))
    return {
        "home": p_home,
        "draw": p_draw,
        "away": p_away,
        "btts_yes": max(0, min(1, p_btts)),
        "matrix": m,
        "lambda_home": xh,
        "lambda_away": xa,
        "home_match": hm,
        "away_match": am,
    }


def total_prob(matrix, line, over=True):
    if line is None:
        return None
    # For standard .5 lines, exact push is impossible. For integer lines, treat as strict over/under.
    if over:
        return sum(matrix[i][j] for i in range(10) for j in range(10) if (i + j) > line)
    return sum(matrix[i][j] for i in range(10) for j in range(10) if (i + j) < line)


def asian_handicap_prob(matrix, line, side):
    # Simple win-probability interpretation for common half-goal Asian lines.
    if line is None or abs(line * 2 - round(line * 2)) > 1e-8:
        return None
    p = 0.0
    for i in range(10):
        for j in range(10):
            diff = i - j
            if side == "home" and diff + line > 0:
                p += matrix[i][j]
            elif side == "away" and -diff + line > 0:
                p += matrix[i][j]
    return p


def quote_model_probability(q, probs):
    market = q["market"]
    raw = q["outcome_raw"].lower()
    line = q.get("line")
    if market == "Resultat final (1X2)":
        if raw in ("1", "home"):
            return probs["home"]
        if raw in ("x", "draw"):
            return probs["draw"]
        if raw in ("2", "away"):
            return probs["away"]
    if market == "Les deux equipes marquent":
        if raw in ("yes", "oui"):
            return probs["btts_yes"]
        if raw in ("no", "non"):
            return 1 - probs["btts_yes"]
    if market == "Total buts - Plus/Moins":
        if raw in ("over", "o", "plus"):
            return total_prob(probs["matrix"], line, True)
        if raw in ("under", "u", "moins"):
            return total_prob(probs["matrix"], line, False)
    if market == "Double chance":
        if raw in ("1x", "home/draw", "1x/draw"):
            return probs["home"] + probs["draw"]
        if raw in ("x2", "draw/away"):
            return probs["draw"] + probs["away"]
        if raw in ("12", "home/away"):
            return probs["home"] + probs["away"]
    if market == "Handicap asiatique":
        side = "home" if raw in ("home", "1") else "away" if raw in ("away", "2") else None
        if side:
            return asian_handicap_prob(probs["matrix"], line, side)
    return None


def confidence_score(model_p, data_quality, value):
    # Transparent score, not a guarantee of success.
    base = 55 + min(25, max(0, data_quality) * 20)
    value_bonus = min(15, max(0, value) * 100 * 0.8)
    uncertainty_penalty = min(20, abs(model_p - 0.5) * 0)  # explicit placeholder; no hidden adjustment
    score = base + value_bonus - uncertainty_penalty
    return int(max(1, min(99, round(score))))


def grade(value, confidence):
    if value >= 0.10 and confidence >= 75:
        return "🔥 PREMIUM"
    if value >= 0.06 and confidence >= 68:
        return "🟢 FORTE"
    if value >= 0.03:
        return "🟡 INTERESSANTE"
    if value >= 0:
        return "⚪ FAIBLE"
    return "🔴 EVITER"


@st.cache_data(ttl=21600, show_spinner=False)
def build_models():
    models = {}
    for code in sorted(set(LEAGUE_CODES.values())):
        df = load_league_history(code, 3)
        if not df.empty:
            s = team_strengths(df)
            if s:
                models[code] = {"strength": s, "rows": len(df), "teams": len(s["teams"])}
    return models


def enrich_quotes(fixtures):
    models = build_models()
    output = []
    for fx in fixtures:
        code = league_code(fx["league"])
        model_info = models.get(code)
        if not model_info:
            continue
        probs = model_probs(model_info["strength"], fx["home"], fx["away"])
        matched = int(bool(probs["home_match"])) + int(bool(probs["away_match"]))
        data_quality = 0.9 if matched == 2 else 0.55 if matched == 1 else 0.25
        for q in fx["quotes"]:
            p = quote_model_probability(q, probs)
            if p is None:
                continue
            implied = 1 / q["price"]
            value = p * q["price"] - 1
            confidence = confidence_score(p, data_quality, value)
            q2 = dict(q)
            q2.update({
                "home": fx["home"], "away": fx["away"], "league": fx["league"],
                "start": fx["start"], "fixture_id": fx["fixture_id"],
                "selection": outcome_display(q["outcome_raw"], q["raw_market"], fx["home"], fx["away"], q.get("line")),
                "implied": implied,
                "model_prob": p,
                "value": value,
                "confidence": confidence,
                "grade": grade(value, confidence),
                "model_code": code,
                "lambda_home": probs["lambda_home"], "lambda_away": probs["lambda_away"],
                "team_match_quality": matched,
            })
            output.append(q2)
    return output, models


# =========================
# Backtest
# =========================
def backtest_1x2(df):
    if df.empty or len(df) < 100:
        return None
    # Chronological walk-forward on a capped sample for speed.
    work = df.copy().reset_index(drop=True)
    work = work.tail(min(len(work), 700)).reset_index(drop=True)
    preds = []
    outcomes = []
    for i in range(60, len(work)):
        hist = work.iloc[:i]
        s = team_strengths(hist)
        if not s:
            continue
        row = work.iloc[i]
        p = model_probs(s, row["HomeTeam"], row["AwayTeam"])
        pred = [p["home"], p["draw"], p["away"]]
        y = {"H": 0, "D": 1, "A": 2}.get(str(row["FTR"]))
        if y is None:
            continue
        preds.append(pred)
        outcomes.append(y)
    if not preds:
        return None
    brier = sum(sum((p[j] - (1 if y == j else 0)) ** 2 for j in range(3)) for p, y in zip(preds, outcomes)) / len(preds)
    logloss = -sum(math.log(max(1e-9, p[y])) for p, y in zip(preds, outcomes)) / len(preds)
    accuracy = sum(int(max(range(3), key=lambda j: p[j]) == y) for p, y in zip(preds, outcomes)) / len(preds)
    return {"n": len(preds), "brier": brier, "logloss": logloss, "accuracy": accuracy}


# =========================
# UI
# =========================
with st.sidebar:
    st.header("Parametres")
    min_odds = st.number_input("Cote min", 1.01, 100.0, 1.40, 0.01)
    max_odds = st.number_input("Cote max", 1.01, 100.0, 3.50, 0.01)
    min_prob = st.number_input("Probabilite modele min (%)", 0.0, 99.9, 60.0, 1.0)
    min_value = st.number_input("Value min (%)", -50.0, 100.0, 3.0, 1.0)
    min_conf = st.number_input("Confiance min", 1, 99, 60, 1)
    hours = st.selectbox("Fenetre", [6, 12, 24, 36, 48], index=2)
    market_filter = st.selectbox("Marche", [
        "Tous", "Resultat final (1X2)", "Total buts - Plus/Moins",
        "Les deux equipes marquent", "Double chance", "Handicap asiatique"
    ])
    only_value = st.checkbox("Afficher uniquement les values positives", value=True)

    st.divider()
    st.subheader("Quota OddsPapi")
    if st.button("Verifier le quota", use_container_width=True):
        try:
            a = account_info()
            lim, cnt, rem, valid = extract_account(a)
            st.session_state.quota = (lim, cnt, rem, valid)
        except Exception as e:
            st.error(str(e))
    if "quota" in st.session_state:
        lim, cnt, rem, valid = st.session_state.quota
        st.metric("Restant", rem if rem is not None else "?")
        st.caption(f"Utilise: {cnt if cnt is not None else '?'} / {lim if lim is not None else '?'}")
        if valid:
            st.caption(f"Validite abonnement: {valid}")

scan_col, demo_col = st.columns(2)
scan = scan_col.button("🔎 SCANNER 1xBET", type="primary", use_container_width=True)
demo = demo_col.button("🧪 DEMO", use_container_width=True)

if demo:
    st.session_state.demo = True

if scan:
    st.session_state.demo = False
    if max_odds < min_odds:
        st.error("La cote maximum doit etre superieure a la cote minimum.")
    else:
        try:
            with st.spinner("Recuperation des matchs 1xBet puis analyse statistique..."):
                fixtures, info = get_1xbet_fixtures(hours)
                rows, models = enrich_quotes(fixtures)
            filtered = []
            for r in rows:
                if not (min_odds <= r["price"] <= max_odds):
                    continue
                if r["model_prob"] * 100 < min_prob:
                    continue
                if r["value"] * 100 < min_value:
                    continue
                if r["confidence"] < min_conf:
                    continue
                if market_filter != "Tous" and r["market"] != market_filter:
                    continue
                if only_value and r["value"] <= 0:
                    continue
                filtered.append(r)
            filtered.sort(key=lambda x: (x["value"], x["confidence"], x["model_prob"]), reverse=True)
            st.session_state.results = filtered
            st.session_state.info = info
            st.success(f"{len(fixtures)} matchs recuperes → {len(filtered)} opportunites analytiques.")
        except QuotaError as e:
            msg = str(e)
            if msg.startswith("QUOTA:"):
                p = msg.split(":")
                st.error(f"Quota epuise: {p[1]} / {p[2]}. Aucune requete supplementaire ne sera tentee.")
            else:
                st.error("OddsPapi a renvoye HTTP 429. Verifie le quota dans la barre laterale.")
        except Exception as e:
            st.error(str(e))


# Demo / result display
if st.session_state.get("demo"):
    demo_rows = [
        {"home":"Japon","away":"Nouvelle-Zelande","league":"Demo","market":"Resultat final (1X2)","selection":"1 - Japon","price":1.72,"implied":1/1.72,"model_prob":0.69,"value":0.1868,"confidence":82,"grade":"🔥 PREMIUM","start":None,"model_code":"DEMO"},
        {"home":"France","away":"Italie","league":"Demo","market":"Total buts - Plus/Moins","selection":"Plus de 1.5","price":1.55,"implied":1/1.55,"model_prob":0.78,"value":0.209,"confidence":79,"grade":"🔥 PREMIUM","start":None,"model_code":"DEMO"},
        {"home":"Allemagne","away":"Espagne","league":"Demo","market":"Les deux equipes marquent","selection":"Oui","price":1.80,"implied":1/1.80,"model_prob":0.61,"value":0.098,"confidence":73,"grade":"🟢 FORTE","start":None,"model_code":"DEMO"},
    ]
    st.info("DEMO: les chiffres ci-dessous sont fictifs. Ils servent uniquement a verifier l'interface.")
    rows = demo_rows
elif st.session_state.get("results") is not None:
    rows = st.session_state.get("results", [])
else:
    rows = []

if rows:
    st.subheader("Opportunites")
    display = []
    for r in rows:
        dt = iso_dt(r.get("start"))
        kickoff = dt.strftime("%d/%m %H:%M UTC") if dt else "-"
        display.append({
            "Match": f"{r['home']} — {r['away']}",
            "Competition": r.get("league", ""),
            "Coup d'envoi": kickoff,
            "Marche": r["market"],
            "Pari": r["selection"],
            "Cote 1xBet": round(r["price"], 2),
            "Prob. modele": f"{r['model_prob']*100:.1f}%",
            "Prob. implicite": f"{r['implied']*100:.1f}%",
            "Value": f"{r['value']*100:+.1f}%",
            "Confiance": f"{r['confidence']}/100",
            "Verdict": r["grade"],
        })
    st.dataframe(pd.DataFrame(display), use_container_width=True, hide_index=True)

    st.subheader("Lecture rapide")
    best = rows[0]
    st.write(f"**Top opportunite : {best['home']} — {best['away']} | {best['selection']} @ {best['price']:.2f}**")
    st.write(f"Modele {best['model_prob']*100:.1f}% vs implicite {best['implied']*100:.1f}% → **value {best['value']*100:+.1f}%**. Confiance: **{best['confidence']}/100**.")
    st.caption("La value est mathematique: P(modele) × cote − 1. Ce n'est pas une garantie de gain.")
else:
    st.info("Lance un scan ou le mode DEMO. Le scanner analytique privilegie les championnats pour lesquels le modele statistique dispose d'historique exploitable.")


# =========================
# Backtest tab
# =========================
st.divider()
st.subheader("Validation du modele")
col1, col2 = st.columns([2, 1])
with col1:
    bt_league = st.selectbox("Championnat a backtester", sorted(LEAGUE_CODES.keys()), index=0)
with col2:
    run_bt = st.button("📊 BACKTEST", use_container_width=True)

if run_bt:
    code = LEAGUE_CODES[bt_league]
    with st.spinner("Backtest walk-forward..."):
        df = load_league_history(code, 3)
        res = backtest_1x2(df)
    if not res:
        st.warning("Pas assez de donnees pour ce championnat.")
    else:
        a, b, c, d = st.columns(4)
        a.metric("Matchs testes", res["n"])
        b.metric("Accuracy", f"{res['accuracy']*100:.1f}%")
        c.metric("Brier", f"{res['brier']:.3f}")
        d.metric("Log loss", f"{res['logloss']:.3f}")
        st.caption("Backtest walk-forward: chaque prediction utilise uniquement les matchs precedents dans l'echantillon. Ce resultat mesure la calibration historique, pas une garantie future.")

st.divider()
st.caption("V3: la probabilite modele est independante des cotes 1xBet lorsqu'un championnat supporte dispose de donnees Football-Data.co.uk. Pour les competitions sans historique reconnu, le pari est volontairement exclu plutot que d'inventer une probabilite.")
