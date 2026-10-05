import re
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import requests
import streamlit as st

st.set_page_config(page_title="Odds Intelligence", page_icon="🎯", layout="centered")

BASE = "https://api.oddspapi.io/v4"
BOOKMAKER = "1xbet"
SPORT_ID = 10

st.title("🎯 Odds Intelligence")
st.caption("Scanner 1xBet • toutes les options disponibles • analyse et filtrage")


class QuotaError(Exception):
    pass


@st.cache_data(ttl=30, show_spinner=False)
def api_get(path, params_tuple):
    key = st.secrets.get("ODDS_API_KEY", "")
    if not key:
        raise RuntimeError("Clé OddsPapi absente. Ajoute ODDS_API_KEY dans Streamlit → Secrets.")
    params = dict(params_tuple)
    params["apiKey"] = key
    r = requests.get(f"{BASE}/{path}", params=params, timeout=60)
    if r.status_code == 429:
        raise QuotaError("Quota OddsPapi atteint (HTTP 429).")
    if not r.ok:
        raise RuntimeError(f"OddsPapi HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def get_api(path, params=None):
    return api_get(path, tuple(sorted((params or {}).items())))


def text_value(obj, *keys):
    if not isinstance(obj, dict):
        return ""
    for key in keys:
        value = obj.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("teamName") or value.get("shortName") or value.get("title")
        if value not in (None, ""):
            return str(value)
    return ""


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def display_time(value):
    dt = parse_dt(value)
    if not dt:
        return str(value or "")
    return dt.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")


def line_from(value):
    if value in (None, ""):
        return None
    for part in str(value).split("/"):
        try:
            number = float(part)
            if -100 <= number <= 100:
                return number
        except Exception:
            continue
    return None


def fmt_line(value):
    if value is None:
        return ""
    try:
        f = float(value)
        return str(int(f)) if f.is_integer() else str(f).rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def translate_market(raw):
    n = str(raw or "").strip()
    low = n.lower()
    rules = [
        ("full time result", "Résultat final (1X2)"),
        ("match result", "Résultat final (1X2)"),
        ("double chance", "Double chance"),
        ("both teams to score", "Les deux équipes marquent"),
        ("over under", "Total / Plus-Moins"),
        ("over/under", "Total / Plus-Moins"),
        ("total goals", "Total buts"),
        ("asian handicap", "Handicap asiatique"),
        ("european handicap", "Handicap européen"),
        ("draw no bet", "Remboursé si nul"),
        ("half time result", "Résultat mi-temps"),
        ("correct score", "Score exact"),
        ("first half", "1re mi-temps"),
        ("second half", "2e mi-temps"),
    ]
    for needle, label in rules:
        if needle in low:
            return label
    return n or "Marché inconnu"


def outcome_label(raw, raw_market, home, away, line=None, bookmaker_outcome_id=""):
    value = str(raw or "").strip()
    low = value.lower()
    market_low = str(raw_market or "").lower()
    boid = str(bookmaker_outcome_id or "").lower()
    tail = boid.split("/")[-1] if "/" in boid else ""
    token = tail if tail in {"over", "under", "yes", "no", "home", "away", "1", "x", "2"} else low

    if token == "1" and ("result" in market_low or "1x2" in market_low):
        return f"1 — {home}"
    if token == "x" and ("result" in market_low or "1x2" in market_low):
        return "X — Match nul"
    if token == "2" and ("result" in market_low or "1x2" in market_low):
        return f"2 — {away}"
    if token == "over":
        return f"Plus de {fmt_line(line)}" if line is not None else "Plus de"
    if token == "under":
        return f"Moins de {fmt_line(line)}" if line is not None else "Moins de"
    if token == "yes":
        return "Oui"
    if token == "no":
        return "Non"
    if token == "home":
        return home
    if token == "away":
        return away
    return value or str(bookmaker_outcome_id or "Sélection")


@st.cache_data(ttl=21600, show_spinner=False)
def market_catalog():
    data = get_api("markets", {"language": "en"})
    result = {}
    if not isinstance(data, list):
        return result
    for market in data:
        if not isinstance(market, dict):
            continue
        mid = str(market.get("marketId", ""))
        if not mid:
            continue
        outcomes = {}
        for outcome in market.get("outcomes") or []:
            if isinstance(outcome, dict):
                oid = str(outcome.get("outcomeId", ""))
                if oid:
                    outcomes[oid] = outcome.get("outcomeName") or ""
        result[mid] = {
            "name": market.get("marketName") or "",
            "outcomes": outcomes,
            "handicap": market.get("handicap"),
        }
    return result


def extract_fixtures(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "fixtures", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []


def fixture_names(item):
    home = text_value(item, "participant1Name", "homeTeamName", "homeName", "home")
    away = text_value(item, "participant2Name", "awayTeamName", "awayName", "away")
    participants = item.get("participants") or item.get("teams") or []
    if isinstance(participants, list):
        vals = [text_value(p, "name", "teamName", "shortName", "title") for p in participants if isinstance(p, dict)]
        vals = [v for v in vals if v]
        if len(vals) >= 2:
            home = home or vals[0]
            away = away or vals[1]
    return home, away


def parse_quotes(item, catalog):
    bookmaker_odds = item.get("bookmakerOdds") or {}
    board = bookmaker_odds.get(BOOKMAKER)
    if not isinstance(board, dict) and bookmaker_odds:
        board = next(iter(bookmaker_odds.values()))
    if not isinstance(board, dict):
        return []

    markets = board.get("markets") or {}
    market_items = markets.items() if isinstance(markets, dict) else enumerate(markets)
    rows = []

    for market_id, market_data in market_items:
        if not isinstance(market_data, dict):
            continue
        mid = str(market_id)
        meta = catalog.get(mid, {})
        raw_market = meta.get("name") or market_data.get("marketName") or f"Marché {mid}"
        market_name = translate_market(raw_market)
        base_line = meta.get("handicap")
        outcomes = market_data.get("outcomes") or {}
        outcome_items = outcomes.items() if isinstance(outcomes, dict) else enumerate(outcomes)

        for outcome_id, outcome_data in outcome_items:
            if not isinstance(outcome_data, dict):
                continue
            players = outcome_data.get("players")
            legs = list(players.values()) if isinstance(players, dict) else (players if isinstance(players, list) else [outcome_data])
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                if leg.get("active", outcome_data.get("active", True)) is False:
                    continue
                try:
                    odds = float(leg.get("price", outcome_data.get("price")))
                except Exception:
                    continue
                if odds <= 1:
                    continue
                boid = str(leg.get("bookmakerOutcomeId") or "")
                line = leg.get("line") or outcome_data.get("line")
                if line in (None, ""):
                    line = line_from(boid)
                if line in (None, "") and base_line not in (None, ""):
                    line = line_from(base_line)
                raw_outcome = (
                    leg.get("outcomeName") or leg.get("name") or leg.get("label")
                    or catalog.get(mid, {}).get("outcomes", {}).get(str(outcome_id))
                    or outcome_data.get("outcomeName") or str(outcome_id)
                )
                rows.append({
                    "market_id": mid,
                    "outcome_id": str(outcome_id),
                    "market": market_name,
                    "raw_market": raw_market,
                    "selection": outcome_label(raw_outcome, raw_market, "", "", line, boid),
                    "raw_selection": str(raw_outcome),
                    "line": line,
                    "odds": odds,
                    "boid": boid,
                })
    return rows


def market_probability(rows):
    """Normalize inverse odds within comparable market/line groups.
    This is a market-adjusted probability, not an independent prediction.
    """
    groups = defaultdict(list)
    for row in rows:
        key = (row["market_id"], fmt_line(row.get("line")))
        groups[key].append(row)
    for group in groups.values():
        denom = sum(1.0 / r["odds"] for r in group if r["odds"] > 1)
        for r in group:
            r["implied"] = 100.0 / r["odds"]
            r["estimated"] = 100.0 * (1.0 / r["odds"]) / denom if denom else r["implied"]
            r["edge"] = r["estimated"] - r["implied"]
            r["market_margin"] = sum(1.0 / x["odds"] for x in group if x["odds"] > 1) * 100 - 100
    return rows


def build_fixtures(hours):
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)

    # One scoped fixture call first. This identifies only tournaments that actually
    # have matches in the requested window, then odds are retrieved in bulk.
    fixtures_data = get_api("fixtures", {
        "sportId": SPORT_ID,
        "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "statusId": 0,
        "hasOdds": "true",
        "bookmakers": BOOKMAKER,
        "language": "en",
    })
    fixture_seed = extract_fixtures(fixtures_data)
    tournament_ids = []
    seed_by_id = {}
    for item in fixture_seed:
        if not isinstance(item, dict):
            continue
        fid = item.get("fixtureId")
        tid = item.get("tournamentId")
        dt = parse_dt(item.get("startTime"))
        if not fid or tid is None or not dt or not (now <= dt <= end):
            continue
        seed_by_id[str(fid)] = item
        if str(tid) not in tournament_ids:
            tournament_ids.append(str(tid))

    if not tournament_ids:
        return [], {"seed": len(fixture_seed), "tournaments": 0}

    catalog = market_catalog()
    raw = []
    for i in range(0, len(tournament_ids), 100):
        chunk = tournament_ids[i:i + 100]
        data = get_api("odds-by-tournaments", {
            "tournamentIds": ",".join(chunk),
            "bookmakers": BOOKMAKER,
            "language": "en",
            "verbosity": 3,
        })
        raw.extend(extract_fixtures(data))

    fixtures = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("fixtureId") or "")
        if not fid or fid in seen:
            continue
        seed = seed_by_id.get(fid, {})
        dt = parse_dt(item.get("startTime") or seed.get("startTime"))
        if not dt or not (now <= dt <= end):
            continue
        home, away = fixture_names(item)
        sh, sa = fixture_names(seed)
        home, away = home or sh, away or sa
        if not home or not away:
            continue
        league = (
            item.get("tournamentName") or item.get("leagueName") or item.get("competitionName")
            or seed.get("tournamentName") or seed.get("leagueName") or seed.get("competitionName") or ""
        )
        quotes = parse_quotes(item, catalog)
        for q in quotes:
            q["home"] = home
            q["away"] = away
            q["league"] = league
            q["start"] = dt.isoformat()
            # Rebuild the human-readable selection now that team names are known.
            q["selection"] = outcome_label(q.get("raw_selection"), q.get("raw_market"), home, away, q.get("line"), q.get("boid"))
            q["fixture_id"] = fid
        quotes = market_probability(quotes)
        fixtures.append({
            "fixture_id": fid,
            "home": home,
            "away": away,
            "league": league,
            "start": dt.isoformat(),
            "quotes": quotes,
        })
        seen.add(fid)
    return fixtures, {"seed": len(fixture_seed), "tournaments": len(tournament_ids), "fixtures": len(fixtures)}


def account_status():
    data = get_api("account")
    root = data if isinstance(data, dict) else {}
    candidates = [root]
    for key in ("account", "data", "user"):
        if isinstance(root.get(key), dict):
            candidates.append(root[key])
    limit = count = valid_until = None
    for d in candidates:
        if limit is None: limit = d.get("request_limit")
        if count is None: count = d.get("request_count")
        if valid_until is None: valid_until = d.get("valid_until")
    remaining = None
    try:
        if limit is not None and count is not None:
            remaining = max(0, int(limit) - int(count))
    except Exception:
        pass
    return limit, count, remaining, valid_until


# ---------------- UI ----------------
with st.container(border=True):
    st.subheader("🔎 Recherche")
    c1, c2 = st.columns(2)
    min_odds = c1.number_input("Cote minimum", min_value=1.01, max_value=100.0, value=1.50, step=0.01)
    max_odds = c2.number_input("Cote maximum", min_value=1.01, max_value=100.0, value=2.00, step=0.01)
    c3, c4 = st.columns(2)
    min_prob = c3.number_input("Probabilité estimée minimum (%)", min_value=0.0, max_value=99.9, value=60.0, step=1.0)
    hours = c4.selectbox("Fenêtre", [6, 12, 24, 36, 48], index=2, format_func=lambda x: f"{x} h")

    # We can only populate the exact bookmaker market list after the catalogue is loaded.
    api_key_present = bool(st.secrets.get("ODDS_API_KEY", ""))
    market_options = ["Tous les marchés"]
    if api_key_present:
        try:
            cat = market_catalog()
            market_options += sorted({translate_market(v.get("name")) for v in cat.values() if v.get("name")})
        except Exception:
            pass
    market = st.selectbox("Marché", list(dict.fromkeys(market_options)))

    c5, c6, c7 = st.columns(3)
    sort_mode = c5.selectbox("Trier par", ["Probabilité décroissante", "Cote croissante", "Value marché décroissante", "Coup d'envoi"])
    only_positive_edge = c6.checkbox("Afficher seulement l'écart positif", value=False)
    c7.write("Toutes les options 1xBet : **OUI**")

    b1, b2, b3 = st.columns(3)
    scan = b1.button("🔎 SCANNER 1xBET", use_container_width=True, type="primary")
    demo = b2.button("🧪 DÉMO", use_container_width=True)
    quota_btn = b3.button("📊 QUOTA", use_container_width=True)

if quota_btn:
    try:
        limit, count, remaining, valid = account_status()
        if limit is None:
            st.info("Quota : OddsPapi n'a pas renvoyé les champs de quota sur ce compte.")
        else:
            st.info(f"Quota : {count}/{limit} utilisé(s) • reste {remaining} • validité : {valid or 'non indiquée'}")
    except Exception as exc:
        st.error(str(exc))

if demo:
    demo_rows = [
        {"home":"Japon", "away":"Nouvelle-Zélande", "league":"Démo", "market":"Résultat final (1X2)", "selection":"1 — Japon", "odds":1.72, "estimated":64.7, "implied":58.1, "edge":6.6, "line":None},
        {"home":"Japon", "away":"Nouvelle-Zélande", "league":"Démo", "market":"Total / Plus-Moins", "selection":"Plus de 1.5", "odds":1.55, "estimated":78.1, "implied":64.5, "edge":13.6, "line":1.5},
        {"home":"Japon", "away":"Nouvelle-Zélande", "league":"Démo", "market":"Les deux équipes marquent", "selection":"Oui", "odds":1.80, "estimated":58.0, "implied":55.6, "edge":2.4, "line":None},
    ]
    rows = [r for r in demo_rows if min_odds <= r["odds"] <= max_odds and r["estimated"] >= min_prob and (market == "Tous les marchés" or r["market"] == market)]
    if only_positive_edge:
        rows = [r for r in rows if r["edge"] > 0]
    st.session_state.rows = rows
    st.session_state.note = "Mode démo — chiffres illustratifs"

if scan:
    st.session_state.rows = []
    st.session_state.note = ""
    if max_odds < min_odds:
        st.error("La cote maximum doit être supérieure ou égale à la cote minimum.")
    elif not api_key_present:
        st.error("Clé OddsPapi absente dans Streamlit → Secrets.")
    else:
        try:
            with st.spinner("Récupération des matchs et de toutes les options 1xBet…"):
                fixtures, meta = build_fixtures(hours)
            rows = []
            for fixture in fixtures:
                for quote in fixture["quotes"]:
                    if not (min_odds <= quote["odds"] <= max_odds):
                        continue
                    if quote["estimated"] < min_prob:
                        continue
                    if market != "Tous les marchés" and quote["market"] != market:
                        continue
                    if only_positive_edge and quote["edge"] <= 0:
                        continue
                    rows.append(quote)
            if sort_mode == "Probabilité décroissante":
                rows.sort(key=lambda x: (x["estimated"], x["edge"]), reverse=True)
            elif sort_mode == "Cote croissante":
                rows.sort(key=lambda x: x["odds"])
            elif sort_mode == "Value marché décroissante":
                rows.sort(key=lambda x: x["edge"], reverse=True)
            else:
                rows.sort(key=lambda x: parse_dt(x.get("start")) or datetime.max.replace(tzinfo=timezone.utc))
            st.session_state.rows = rows
            st.session_state.note = f"{meta.get('fixtures', 0)} match(s) • {len(rows)} sélection(s) correspondant à tes critères • tous les marchés 1xBet récupérés pour ces matchs"
        except QuotaError:
            st.error("Quota OddsPapi atteint (HTTP 429). Le scanner ne peut pas récupérer de nouvelles cotes tant que le quota n'est pas renouvelé ou augmenté.")
        except Exception as exc:
            st.error(str(exc))

rows = st.session_state.get("rows", [])
note = st.session_state.get("note", "")
if note:
    st.info(note)

if rows:
    st.success(f"{len(rows)} pari(s) trouvé(s)")
    for r in rows:
        est = float(r.get("estimated", 0))
        imp = float(r.get("implied", 0))
        edge = float(r.get("edge", 0))
        if est >= 70:
            badge = "🔥 FORTE"
        elif est >= 60:
            badge = "🟢 INTÉRESSANTE"
        else:
            badge = "⚪ STANDARD"
        with st.container(border=True):
            st.subheader(f"{r.get('home', '')} — {r.get('away', '')}")
            st.caption(f"{r.get('league', '')} • {display_time(r.get('start'))}")
            st.write(f"**Marché :** {r.get('market', '')}")
            st.write(f"**Pari :** {r.get('selection', '')}")
            if r.get("line") not in (None, ""):
                st.caption(f"Ligne : {fmt_line(r['line'])}")
            a, b, c = st.columns(3)
            a.metric("Cote 1xBet", f"{r['odds']:.2f}")
            b.metric("Prob. estimée", f"{est:.1f}%")
            c.metric("Écart", f"{edge:+.1f} pt")
            st.write(f"**Probabilité implicite :** {imp:.1f}%  •  **{badge}**")
            st.caption("Analyse actuelle = probabilité du marché corrigée de la marge des issues comparables. Elle n'est pas une garantie et ne doit pas être présentée comme une prédiction indépendante.")
else:
    if note:
        st.warning("Aucun pari ne correspond aux critères.")

st.divider()
st.caption("Principe : l'application récupère les matchs de la fenêtre choisie, puis toutes les options de paris 1xBet disponibles pour ces matchs. Tes filtres servent ensuite à sélectionner ce que tu veux voir.")
st.caption("Important : les marchés non standardisés sont affichés tels que fournis par OddsPapi. La probabilité estimée universelle est une probabilité de marché ajustée, pas un modèle sportif indépendant.")
