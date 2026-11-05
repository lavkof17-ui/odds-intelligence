import streamlit as st
import requests
import time
from datetime import datetime, timedelta, timezone
from model_engine import build_model, predict, market_probability

st.set_page_config(page_title="Odds Intelligence", page_icon="🎯", layout="centered")
st.title("🎯 Odds Intelligence")
st.caption("Scanner football • 1xBet • toutes les options disponibles • analyse indépendante quand possible")

BASE = "https://api.oddspapi.io/v4"
BOOK = "1xbet"
RESERVE_REQUESTS = 3

try:
    API_KEY = st.secrets["ODDS_API_KEY"]
except Exception:
    API_KEY = ""


def api_get(path, params=None):
    if not API_KEY:
        raise RuntimeError("Clé OddsPapi absente. Ajoute ODDS_API_KEY dans Streamlit → Secrets.")
    p = dict(params or {})
    p["apiKey"] = API_KEY
    try:
        response = requests.get(f"{BASE}/{path}", params=p, timeout=45)
    except requests.RequestException as exc:
        raise RuntimeError(f"Connexion OddsPapi impossible : {exc}") from exc
    if response.status_code == 429:
        # OddsPapi utilise HTTP 429 pour DEUX situations distinctes :
        # REQUEST_LIMIT_EXCEEDED = quota mensuel épuisé
        # RATE_LIMITED = endpoint appelé trop rapidement
        try:
            error_body = response.json()
        except ValueError:
            error_body = {}
        err = error_body.get("error", error_body) if isinstance(error_body, dict) else {}
        code = str(err.get("code", "")).upper() if isinstance(err, dict) else ""
        message = err.get("message", "") if isinstance(err, dict) else ""
        details = err.get("details", "") if isinstance(err, dict) else ""

        if code == "REQUEST_LIMIT_EXCEEDED":
            raise RuntimeError(
                f"Quota mensuel OddsPapi réellement épuisé ({code}) : "
                f"{details or message or 'limite mensuelle atteinte'}"
            )

        if code == "RATE_LIMITED":
            retry_ms = err.get("retryMs", 1000) if isinstance(err, dict) else 1000
            try:
                wait_seconds = max(1.0, min(float(retry_ms) / 1000.0 + 0.25, 15.0))
            except (TypeError, ValueError):
                wait_seconds = 1.5
            time.sleep(wait_seconds)
            # Une seule reprise après le délai demandé par le fournisseur.
            response = requests.get(f"{BASE}/{path}", params=p, timeout=45)
            if response.status_code == 429:
                try:
                    retry_body = response.json()
                except ValueError:
                    retry_body = {}
                retry_err = retry_body.get("error", retry_body) if isinstance(retry_body, dict) else {}
                retry_code = retry_err.get("code", "UNKNOWN") if isinstance(retry_err, dict) else "UNKNOWN"
                retry_details = retry_err.get("details", "") if isinstance(retry_err, dict) else ""
                if retry_code == "REQUEST_LIMIT_EXCEEDED":
                    raise RuntimeError(
                        f"Quota mensuel OddsPapi réellement épuisé ({retry_code}) : {retry_details}"
                    )
                raise RuntimeError(
                    f"Limitation temporaire OddsPapi persistante ({retry_code}). "
                    f"{retry_details or 'Attends avant de relancer le scan.'}"
                )
        else:
            raise RuntimeError(
                f"OddsPapi HTTP 429 — code {code or 'non précisé'} : "
                f"{details or message or response.text[:250]}. "
                "Le quota mensuel n'est pas déclaré épuisé sans le code REQUEST_LIMIT_EXCEEDED."
            )
    if response.status_code in (401, 403):
        raise RuntimeError("Clé API refusée ou accès non autorisé. Vérifie ODDS_API_KEY dans Streamlit → Secrets.")
    if not response.ok:
        raise RuntimeError(f"OdsPapi HTTP {response.status_code}: {response.text[:250]}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("OddsPapi a renvoyé une réponse JSON invalide.") from exc


def active_subscription(account):
    if not isinstance(account, dict):
        return None
    subs = account.get("subscriptions")
    if isinstance(subs, list):
        active = [s for s in subs if isinstance(s, dict) and s.get("is_active") is True]
        if active:
            return active[0]
        if subs and isinstance(subs[0], dict):
            return subs[0]
    if any(k in account for k in ("request_limit", "request_count", "is_active")):
        return account
    return None


@st.cache_data(ttl=30, show_spinner=False)
def get_account_status():
    # GET /account n'est pas facturé par OddsPapi.
    return api_get("account", {})


def read_quota():
    sub = active_subscription(get_account_status())
    if not sub:
        raise RuntimeError("Abonnement actif introuvable dans la réponse OddsPapi.")
    try:
        limit, used = int(sub["request_limit"]), int(sub["request_count"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("request_limit/request_count absents : scan bloqué par sécurité.")
    if limit <= 0 or used < 0:
        raise RuntimeError("Informations de quota incohérentes : scan bloqué.")
    return {
        "limit": limit, "used": used, "remaining": max(0, limit - used),
        "active": sub.get("is_active", True),
        "valid_until": sub.get("valid_until"),
    }


def txt(d, *keys):
    if not isinstance(d, dict):
        return ""
    for key in keys:
        value = d.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("teamName") or value.get("shortName") or value.get("title")
        if value not in (None, ""):
            return str(value)
    return ""


def fr_market(name):
    mapping = {
        "Full Time Result": "Résultat final (1X2)",
        "Both Teams To Score": "Les deux équipes marquent",
        "Over Under Full Time": "Total buts — Plus/Moins",
        "Asian Handicap": "Handicap asiatique",
        "Double Chance": "Double chance",
        "Draw No Bet": "Remboursé si nul",
        "Half Time Result": "Résultat mi-temps",
        "Over Under Half Time": "Total buts mi-temps — Plus/Moins",
        "Correct Score": "Score exact",
    }
    return mapping.get((name or "").strip(), name or "")


def outcome_label(outcome, market, line=None):
    outcome = str(outcome or "").strip()
    if "result" in (market or "").lower() and outcome in ("1", "X", "2"):
        return {"1": "1 — Victoire équipe 1", "X": "X — Match nul", "2": "2 — Victoire équipe 2"}[outcome]
    if outcome.lower() in ("yes", "no"):
        return "Oui" if outcome.lower() == "yes" else "Non"
    if outcome.lower() in ("over", "under"):
        return ("Plus de " if outcome.lower() == "over" else "Moins de ") + str(line) if line not in (None, "") else outcome
    return outcome


@st.cache_data(ttl=2592000, show_spinner=False)
def markets_catalog():
    data = api_get("markets", {"language": "en"})
    out = {}
    if isinstance(data, list):
        for market in data:
            if not isinstance(market, dict):
                continue
            mid = str(market.get("marketId"))
            out[mid] = {"name": market.get("marketName") or f"Marché {mid}", "outcomes": {}}
            for outcome in market.get("outcomes") or []:
                if isinstance(outcome, dict):
                    out[mid]["outcomes"][str(outcome.get("outcomeId"))] = outcome.get("outcomeName") or ""
    return out


@st.cache_data(ttl=120, show_spinner=False)
def cached_fixtures(start_iso, end_iso, bookmaker):
    return api_get("fixtures", {
        "sportId": 10, "from": start_iso, "to": end_iso, "statusId": 0,
        "hasOdds": "true", "bookmakers": bookmaker, "language": "en"
    })


@st.cache_data(ttl=120, show_spinner=False)
def cached_tournament_odds(tournament_ids, bookmaker):
    # OddsPapi accepte au maximum 5 tournamentIds par requête.
    return api_get("odds-by-tournaments", {
        "tournamentIds": tournament_ids, "bookmakers": bookmaker,
        "language": "en", "verbosity": 3
    })


def fetch_odds_for_tournaments(tournament_ids, bookmaker):
    """Récupère tous les tournois par lots de 5 et déduplique les matchs."""
    all_events = {}
    batches = [tournament_ids[i:i + 5] for i in range(0, len(tournament_ids), 5)]
    for batch in batches:
        data = cached_tournament_odds(",".join(batch), bookmaker)
        if isinstance(data, list):
            batch_events = data
        elif isinstance(data, dict):
            batch_events = list(data.values())
        else:
            batch_events = []
        for event in batch_events:
            if isinstance(event, dict) and event.get("fixtureId") is not None:
                all_events.setdefault(str(event["fixtureId"]), event)
    return list(all_events.values()), len(batches)


def parse_fixture(board, catalog):
    rows = []
    markets = (board or {}).get("markets") or {}
    for mid, market_data in markets.items() if isinstance(markets, dict) else []:
        if not isinstance(market_data, dict) or market_data.get("marketActive") is False:
            continue
        meta = catalog.get(str(mid), {})
        raw_market = meta.get("name") or market_data.get("marketName") or f"Marché {mid}"
        names = meta.get("outcomes", {})
        for oid, outcome_data in (market_data.get("outcomes") or {}).items():
            if not isinstance(outcome_data, dict):
                continue
            players = outcome_data.get("players")
            legs = list(players.values()) if isinstance(players, dict) else ([outcome_data] if not isinstance(players, list) else players)
            for leg in legs:
                if not isinstance(leg, dict) or leg.get("active", True) is False:
                    continue
                try:
                    price = float(leg.get("price", outcome_data.get("price")))
                except (TypeError, ValueError):
                    continue
                if price <= 1:
                    continue
                boid = str(leg.get("bookmakerOutcomeId") or "")
                raw = leg.get("outcomeName") or leg.get("name") or leg.get("label") or names.get(str(oid)) or outcome_data.get("outcomeName") or boid or str(oid)
                line = leg.get("line") or outcome_data.get("line")
                if line in (None, "") and "/" in boid:
                    try:
                        line = float(boid.split("/", 1)[0])
                    except ValueError:
                        pass
                rows.append({
                    "market_id": str(mid), "outcome_id": str(oid),
                    "market_raw": raw_market, "market": fr_market(raw_market),
                    "outcome_raw": str(raw), "outcome": outcome_label(raw, raw_market, line),
                    "line": line, "odds": price
                })
    return rows


def format_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%d/%m %H:%M UTC")
    except Exception:
        return str(value or "")


# Quota panel
st.subheader("📊 Quota OddsPapi")
if not API_KEY:
    st.error("Clé API absente : ajoute ODDS_API_KEY dans Streamlit → Settings → Secrets.")
    quota = None
else:
    if st.button("↻ Actualiser le quota", use_container_width=True):
        get_account_status.clear()
        st.rerun()
    try:
        quota = read_quota()
        st.metric("Points restants", f"{quota['remaining']} / {quota['limit']}", delta=f"{quota['used']} utilisés")
        st.progress(min(1.0, quota["remaining"] / quota["limit"]))
        if quota["active"] is False:
            st.error("Abonnement inactif : scanner désactivé.")
        elif quota["remaining"] == 0:
            st.error("Quota épuisé : scanner désactivé jusqu'au renouvellement.")
        elif quota["remaining"] < RESERVE_REQUESTS:
            st.warning(f"Moins de {RESERVE_REQUESTS} points restants : scan bloqué par sécurité.")
        else:
            st.success("Quota disponible. Contrôle automatique activé.")
        if quota.get("valid_until"):
            st.caption(f"Fin de validité : {format_time(quota['valid_until'])}")
        else:
            st.caption("Aucune date de fin communiquée par OddsPapi.")
    except Exception as exc:
        quota = None
        st.error(f"Vérification du quota impossible : {exc}")
        st.warning("Scan bloqué par sécurité tant que le quota ne peut pas être vérifié.")

st.divider()
with st.container(border=True):
    c1, c2 = st.columns(2)
    min_odds = c1.number_input("Cote min", 1.01, 100.0, 1.10, 0.01)
    max_odds = c2.number_input("Cote max", 1.01, 100.0, 1.50, 0.01)
    c3, c4 = st.columns(2)
    min_prob = c3.number_input("Probabilité modèle min (%)", 0.0, 99.9, 80.0, 1.0)
    hours = c4.selectbox("Fenêtre", [6, 12, 24, 36, 48], index=2, format_func=lambda x: f"{x} h")
    c5, c6 = st.columns(2)
    only_model = c5.checkbox("Afficher seulement les marchés analysables", True)
    positive = c6.checkbox("Afficher seulement la value positive", False)
    scan = st.button("🔎 SCANNER 1xBET", use_container_width=True, type="primary")

if scan:
    if max_odds < min_odds:
        st.error("La cote max doit être supérieure ou égale à la cote min.")
        st.stop()
    if not API_KEY:
        st.error("Clé OddsPapi absente dans Streamlit → Secrets.")
        st.stop()

    # Requête /account fraîche avant les endpoints facturables.
    get_account_status.clear()
    try:
        quota = read_quota()
    except Exception as exc:
        st.error(f"Scan annulé : vérification du quota impossible ({exc}).")
        st.stop()
    if quota["active"] is False:
        st.error("Scan annulé : abonnement OddsPapi inactif.")
        st.stop()
    if quota["remaining"] < RESERVE_REQUESTS:
        st.error(f"Scan annulé : seulement {quota['remaining']} point(s) restant(s). Réserve minimale : {RESERVE_REQUESTS}.")
        st.stop()

    try:
        # Fenêtre calée sur des tranches de 2 minutes : les relances rapprochées réutilisent le cache.
        bucket = int(time.time() // 120)
        start = datetime.fromtimestamp(bucket * 120, tz=timezone.utc)
        start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = (start + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 1) Les fixtures permettent de connaître les tournois réellement nécessaires.
        fixtures = cached_fixtures(start_iso, end_iso, BOOK)
        fixtures = fixtures if isinstance(fixtures, list) else []
        tournament_ids = sorted({
            str(f.get("tournamentId"))
            for f in fixtures
            if isinstance(f, dict) and f.get("tournamentId") is not None
        })

        if not tournament_ids:
            st.session_state.rows = []
            st.session_state.note = "Aucun match avec cotes sur la fenêtre choisie."
        else:
            odds_batches_needed = (len(tournament_ids) + 4) // 5

            # /account est non facturé. On contrôle le solde après la requête fixtures
            # et avant de lancer les appels odds/catalogue restants.
            get_account_status.clear()
            quota_after_fixtures = read_quota()
            minimum_needed = odds_batches_needed + 1  # lots odds + catalogue marchés
            if quota_after_fixtures["remaining"] < minimum_needed:
                raise RuntimeError(
                    f"Scan interrompu pour protéger ton quota : {len(tournament_ids)} tournois "
                    f"nécessitent {odds_batches_needed} appel(s) odds (5 tournois maximum par appel), "
                    f"plus 1 appel catalogue éventuel. Il reste {quota_after_fixtures['remaining']} point(s). "
                    "Aucun appel odds supplémentaire n'a été lancé."
                )

            catalog = markets_catalog()
            events, actual_batches = fetch_odds_for_tournaments(tournament_ids, BOOK)
            wanted = {str(f.get("fixtureId")): f for f in fixtures if isinstance(f, dict)}
            model, rows, analyzable = None, [], 0
            odds_in_range = 0
            matches_with_odds = set()
            unsupported_count = 0

            for event in events:
                if not isinstance(event, dict) or str(event.get("fixtureId")) not in wanted or event.get("statusId") not in (0, None):
                    continue
                fixture = wanted[str(event.get("fixtureId"))]
                home = txt(event, "participant1Name", "homeTeamName") or txt(fixture, "participant1Name", "homeTeamName")
                away = txt(event, "participant2Name", "awayTeamName") or txt(fixture, "participant2Name", "awayTeamName")
                league = txt(event, "tournamentName") or txt(fixture, "tournamentName") or txt(fixture, "leagueName")
                board = (event.get("bookmakerOdds") or {}).get(BOOK)

                for item in parse_fixture(board, catalog):
                    if not (min_odds <= item["odds"] <= max_odds):
                        continue
                    odds_in_range += 1
                    matches_with_odds.add(str(event.get("fixtureId")))

                    if model is None:
                        try:
                            model = build_model(365)
                        except Exception:
                            model = {}
                    prediction = predict(home, away, model) if model else None
                    probability = market_probability(item["market_raw"], item["outcome_raw"], item["line"], prediction)

                    item.update({
                        "home": home, "away": away, "league": league,
                        "start": event.get("startTime") or fixture.get("startTime"),
                        "fixture_id": event.get("fixtureId")
                    })

                    if probability is None:
                        unsupported_count += 1
                        if only_model:
                            continue
                        item.update({
                            "estimated": None, "implied": 1 / item["odds"] * 100,
                            "edge": None, "value": None,
                            "analysis": "Marché non pris en charge par le modèle",
                            "passes_filters": False,
                            "filter_reason": "Probabilité non calculable"
                        })
                    else:
                        analyzable += 1
                        implied = 1 / item["odds"]
                        edge = probability - implied
                        value = probability * item["odds"] - 1
                        passes_probability = probability * 100 >= min_prob
                        passes_value = (not positive) or value > 0
                        item.update({
                            "estimated": probability * 100, "implied": implied * 100,
                            "edge": edge * 100, "value": value * 100,
                            "analysis": "Modèle Poisson indépendant",
                            "passes_filters": passes_probability and passes_value,
                            "filter_reason": (
                                "Critères respectés" if passes_probability and passes_value
                                else "Probabilité sous le minimum demandé" if not passes_probability
                                else "Value théorique non positive"
                            )
                        })
                    rows.append(item)

            rows.sort(key=lambda r: (
                r.get("passes_filters", False),
                r.get("value") if r.get("value") is not None else -999,
                r.get("estimated") if r.get("estimated") is not None else -1
            ), reverse=True)
            qualified = sum(1 for r in rows if r.get("passes_filters"))
            st.session_state.rows = rows
            st.session_state.note = (
                f"{len(wanted)} matchs récupérés • {len(matches_with_odds)} avec options dans ta fourchette "
                f"• {odds_in_range} options dans la fourchette • {analyzable} analysables "
                f"• {qualified} respectent tous tes critères • {actual_batches} lots"
            )

        # Rafraîchit le compteur après le scan.
        get_account_status.clear()
        st.rerun()
    except Exception as exc:
        st.error(str(exc))
        st.session_state.rows = []
        st.session_state.note = ""

rows = st.session_state.get("rows", [])
note = st.session_state.get("note", "")
if note:
    st.info(note)
qualified_rows = [r for r in rows if r.get("passes_filters")]
other_rows = [r for r in rows if not r.get("passes_filters")]

if rows:
    st.subheader(f"✅ Options qui respectent tous les critères ({len(qualified_rows)})")
    if not qualified_rows:
        st.info("Aucune option ne satisfait tous les filtres. Les analyses calculées apparaissent juste en dessous : tu peux ainsi voir les probabilités même lorsqu'elles sont sous ton seuil.")
    for row in qualified_rows:
        with st.container(border=True):
            st.subheader(f"{row.get('home', '')} — {row.get('away', '')}")
            st.caption(f"{row.get('league', '')} • {format_time(row.get('start'))}")
            st.write(f"**Marché :** {row.get('market', '')}")
            st.write(f"**Pari :** {row.get('outcome', '')}")
            if row.get("line") not in (None, ""):
                st.caption(f"Ligne : {row['line']}")
            a, b = st.columns(2)
            a.metric("Cote 1xBet", f"{row['odds']:.2f}")
            if row.get("estimated") is not None:
                b.metric("Probabilité modèle", f"{row['estimated']:.1f}%")
                st.write(f"Probabilité implicite : **{row['implied']:.1f}%** • Écart : **{row['edge']:+.1f} points** • Value théorique : **{row['value']:+.1f}%**")
            else:
                b.metric("Analyse", "Non modélisée")
                st.caption("Probabilité non calculable par le modèle.")

    if other_rows:
        st.subheader(f"📋 Autres options analysées ou détectées ({len(other_rows)})")
        for row in other_rows:
            with st.container(border=True):
                st.write(f"**{row.get('home', '')} — {row.get('away', '')}**")
                st.caption(f"{row.get('league', '')} • {format_time(row.get('start'))}")
                st.write(f"{row.get('market', '')} — **{row.get('outcome', '')}**")
                a, b = st.columns(2)
                a.metric("Cote 1xBet", f"{row['odds']:.2f}")
                if row.get("estimated") is not None:
                    b.metric("Probabilité modèle", f"{row['estimated']:.1f}%")
                    st.caption(
                        f"Implicite {row['implied']:.1f}% • Écart {row['edge']:+.1f} pts "
                        f"• Value {row['value']:+.1f}% • {row['filter_reason']}"
                    )
                else:
                    b.metric("Analyse", "Non modélisée")
                    st.caption(row.get("filter_reason", "Option détectée, mais non analysable."))

if not rows and note:
    st.warning("Aucune option n'a été trouvée dans la fourchette de cotes choisie. Essaie une fourchette plus large.")
st.divider()
st.caption("Le scanner récupère les options effectivement exposées par 1xBet. Le modèle n'invente pas de probabilité : il ne calcule que les marchés actuellement supportés (1X2, BTTS, total buts FT, score exact). Les autres restent détectables mais non analysés si tu les affiches.")
st.caption("La probabilité modèle est indépendante des cotes 1xBet ; la cote sert ensuite uniquement à calculer probabilité implicite, écart et value théorique. Aucun résultat ne garantit un gain.")
st.caption("Protection quota : /account est non facturé ; cache de 2 minutes ; les tournois sont envoyés par lots de 5 maximum ; contrôle du solde avant les appels de cotes.")
