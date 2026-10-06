import streamlit as st, requests
from datetime import datetime,timedelta,timezone
from model_engine import build_model,predict,market_probability

st.set_page_config(page_title='Odds Intelligence',page_icon='🎯',layout='centered')
st.title('🎯 Odds Intelligence')
st.caption('Scanner football • 1xBet • toutes les options disponibles • analyse indépendante quand possible')
BASE='https://api.oddspapi.io/v4'; BOOK='1xbet'
try: API_KEY=st.secrets['ODDS_API_KEY']
except Exception: API_KEY=''

def api_get(path,params):
    if not API_KEY: raise RuntimeError('Clé OddsPapi absente. Ajoute ODDS_API_KEY dans Streamlit → Secrets.')
    p=dict(params); p['apiKey']=API_KEY
    r=requests.get(f'{BASE}/{path}',params=p,timeout=60)
    if r.status_code==429: raise RuntimeError('Quota OddsPapi atteint (HTTP 429).')
    if not r.ok: raise RuntimeError(f'OddsPapi HTTP {r.status_code}: {r.text[:300]}')
    return r.json()

def txt(d,*keys):
    if not isinstance(d,dict): return ''
    for k in keys:
        v=d.get(k)
        if isinstance(v,dict): v=v.get('name') or v.get('teamName') or v.get('shortName') or v.get('title')
        if v not in (None,''): return str(v)
    return ''

def fr_market(n):
    mp={'Full Time Result':'Résultat final (1X2)','Both Teams To Score':'Les deux équipes marquent','Over Under Full Time':'Total buts — Plus/Moins','Asian Handicap':'Handicap asiatique','Double Chance':'Double chance','Draw No Bet':'Remboursé si nul','Half Time Result':'Résultat mi-temps','Over Under Half Time':'Total buts mi-temps — Plus/Moins','Correct Score':'Score exact'}
    return mp.get((n or '').strip(),n or '')

def outcome_label(o,market,line=None):
    o=str(o or '').strip(); m=(market or '').lower()
    if 'result' in m and o in ('1','X','2'): return {'1':'1 — Victoire équipe 1','X':'X — Match nul','2':'2 — Victoire équipe 2'}[o]
    if o.lower() in ('yes','no'): return 'Oui' if o.lower()=='yes' else 'Non'
    if o.lower() in ('over','under'):
        return ('Plus de ' if o.lower()=='over' else 'Moins de ')+str(line) if line not in (None,'') else o
    return o

@st.cache_data(ttl=2592000,show_spinner=False)
def markets_catalog():
    data=api_get('markets',{'language':'en'})
    out={}
    if isinstance(data,list):
        for m in data:
            if not isinstance(m,dict): continue
            mid=str(m.get('marketId')); out[mid]={'name':m.get('marketName') or f'Marché {mid}','outcomes':{}}
            for o in m.get('outcomes') or []:
                if isinstance(o,dict): out[mid]['outcomes'][str(o.get('outcomeId'))]=o.get('outcomeName') or ''
    return out

def parse_fixture(board,cat):
    rows=[]; markets=(board or {}).get('markets') or {}
    for mid,m in markets.items() if isinstance(markets,dict) else []:
        if not isinstance(m,dict) or m.get('marketActive') is False: continue
        meta=cat.get(str(mid),{}); rawm=meta.get('name') or m.get('marketName') or f'Marché {mid}'; names=meta.get('outcomes',{})
        for oid,o in (m.get('outcomes') or {}).items():
            if not isinstance(o,dict): continue
            players=o.get('players')
            legs=list(players.values()) if isinstance(players,dict) else ([o] if not isinstance(players,list) else players)
            for leg in legs:
                if not isinstance(leg,dict) or leg.get('active',True) is False: continue
                try: price=float(leg.get('price',o.get('price')))
                except: continue
                boid=str(leg.get('bookmakerOutcomeId') or '')
                raw=leg.get('outcomeName') or leg.get('name') or leg.get('label') or names.get(str(oid)) or o.get('outcomeName') or boid or str(oid)
                line=leg.get('line') or o.get('line')
                if line in (None,'') and '/' in boid:
                    left,_=boid.split('/',1)
                    try: line=float(left)
                    except: pass
                rows.append({'market_id':str(mid),'outcome_id':str(oid),'market_raw':rawm,'market':fr_market(rawm),'outcome_raw':str(raw),'outcome':outcome_label(raw,rawm,line),'line':line,'odds':price})
    return rows

def tm(v):
    try:return datetime.fromisoformat(str(v).replace('Z','+00:00')).strftime('%d/%m %H:%M UTC')
    except:return str(v or '')

with st.container(border=True):
    a,b=st.columns(2); min_odds=a.number_input('Cote min',1.01,100.0,1.10,0.01); max_odds=b.number_input('Cote max',1.01,100.0,1.50,0.01)
    c,d=st.columns(2); min_prob=c.number_input('Probabilité modèle min (%)',0.0,99.9,80.0,1.0); hours=d.selectbox('Fenêtre',[6,12,24,36,48],index=2,format_func=lambda x:f'{x} h')
    e,f=st.columns(2); only_model=e.checkbox('Afficher seulement les marchés analysables',True); positive=f.checkbox('Afficher seulement la value positive',False)
    scan=st.button('🔎 SCANNER 1xBET',use_container_width=True,type='primary')

if scan:
    if max_odds<min_odds: st.error('La cote max doit être supérieure ou égale à la cote min.'); st.stop()
    if not API_KEY: st.error('Clé OddsPapi absente dans Streamlit → Secrets.'); st.stop()
    try:
        cat=markets_catalog()
        now=datetime.now(timezone.utc); end=now+timedelta(hours=hours)
        fs=api_get('fixtures',{'sportId':10,'from':now.strftime('%Y-%m-%dT%H:%M:%SZ'),'to':end.strftime('%Y-%m-%dT%H:%M:%SZ'),'statusId':0,'hasOdds':'true','bookmakers':BOOK,'language':'en'})
        fs=fs if isinstance(fs,list) else []
        tids=sorted({str(f.get('tournamentId')) for f in fs if isinstance(f,dict) and f.get('tournamentId') is not None})
        if not tids: st.session_state.rows=[]; st.session_state.note='Aucun match avec cotes sur la fenêtre choisie.'; st.stop()
        od=api_get('odds-by-tournaments',{'tournamentIds':','.join(tids),'bookmakers':BOOK,'language':'en','verbosity':3})
        events=od if isinstance(od,list) else list(od.values()) if isinstance(od,dict) else []
        wanted={str(f.get('fixtureId')):f for f in fs if isinstance(f,dict)}
        # Build one independent model only when needed. Historical data is external and does not consume OddsPapi quota.
        model=None
        rows=[]; analyzable=0
        for ev in events:
            if not isinstance(ev,dict) or str(ev.get('fixtureId')) not in wanted or ev.get('statusId') not in (0,None): continue
            f=wanted[str(ev.get('fixtureId'))]; h=txt(ev,'participant1Name','homeTeamName') or txt(f,'participant1Name','homeTeamName'); a=txt(ev,'participant2Name','awayTeamName') or txt(f,'participant2Name','awayTeamName')
            league=txt(ev,'tournamentName') or txt(f,'tournamentName') or txt(f,'leagueName')
            board=(ev.get('bookmakerOdds') or {}).get(BOOK)
            for q in parse_fixture(board,cat):
                if not(min_odds<=q['odds']<=max_odds): continue
                if model is None:
                    try:model=build_model(365)
                    except Exception:model={}
                pred=predict(h,a,model) if model else None
                mp=market_probability(q['market_raw'],q['outcome_raw'],q['line'],pred)
                if mp is None:
                    if only_model: continue
                    q.update({'estimated':None,'edge':None,'value':None,'analysis':'Non modélisé indépendamment'})
                else:
                    analyzable+=1; implied=1/q['odds']; edge=mp-implied; value=mp*q['odds']-1
                    if mp*100<min_prob or (positive and value<=0): continue
                    q.update({'estimated':mp*100,'implied':implied*100,'edge':edge*100,'value':value*100,'analysis':'Modèle Poisson indépendant'})
                q.update({'home':h,'away':a,'league':league,'start':ev.get('startTime') or f.get('startTime'),'fixture_id':ev.get('fixtureId')})
                rows.append(q)
        rows.sort(key=lambda r: ((r.get('value') is not None),r.get('value') or -999,r.get('estimated') or -1),reverse=True)
        st.session_state.rows=rows; st.session_state.note=f'{len(wanted)} match(s) • {len(rows)} option(s) retenue(s) • {analyzable} analysée(s) indépendamment'
    except Exception as e:
        st.error(str(e)); st.session_state.rows=[]; st.session_state.note=''

rows=st.session_state.get('rows',[]); note=st.session_state.get('note','')
if note: st.info(note)
for r in rows:
    with st.container(border=True):
        st.subheader(f"{r.get('home','')} — {r.get('away','')}"); st.caption(f"{r.get('league','')} • {tm(r.get('start'))}")
        st.write(f"**Marché :** {r.get('market','')}")
        st.write(f"**Pari :** {r.get('outcome','')}")
        if r.get('line') not in (None,''): st.caption(f"Ligne : {r['line']}")
        c1,c2=st.columns(2); c1.metric('Cote 1xBet',f"{r['odds']:.2f}")
        if r.get('estimated') is not None:
            c2.metric('Probabilité modèle',f"{r['estimated']:.1f}%")
            st.write(f"Probabilité implicite : **{r['implied']:.1f}%** • Écart : **{r['edge']:+.1f} points** • Value théorique : **{r['value']:+.1f}%**")
        else:
            c2.metric('Analyse','Non modélisée')
            st.caption('Option détectée chez 1xBet, mais le modèle ne produit pas de probabilité fiable pour ce marché.')

if not rows and note: st.warning('Aucune option ne correspond aux critères.')
st.divider(); st.caption('Le scanner récupère les options effectivement exposées par 1xBet. Le modèle n’invente pas de probabilité : il ne calcule que les marchés actuellement supportés (1X2, BTTS, total buts FT, score exact). Les autres restent détectables mais non analysés si tu les affiches.')
st.caption('La probabilité modèle est indépendante des cotes 1xBet ; la cote sert ensuite uniquement à calculer probabilité implicite, écart et value théorique. Aucun résultat ne garantit un gain.')
