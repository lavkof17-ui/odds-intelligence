import streamlit as st
import requests
from datetime import datetime, timedelta, timezone

st.set_page_config(page_title='Odds Intelligence', page_icon='ðŸŽ¯', layout='centered')
st.title('ðŸŽ¯ Odds Intelligence')
st.caption('Scanner football â€¢ 1xBet â€¢ marchÃ©s lisibles â€¢ probabilitÃ© estimÃ©e')
BASE_URL='https://api.oddspapi.io/v4'; BOOKMAKER='1xbet'
try: API_KEY=st.secrets['ODDS_API_KEY']
except Exception: API_KEY=''

def api_get(path, params):
    if not API_KEY: raise RuntimeError('ClÃ© OddsPapi absente. Ajoute ODDS_API_KEY dans Streamlit â†’ Secrets.')
    p=dict(params); p['apiKey']=API_KEY
    r=requests.get(f'{BASE_URL}/{path}',params=p,timeout=60)
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

def fixture_names(f):
    h=txt(f,'participant1Name','homeTeamName','homeName','home'); a=txt(f,'participant2Name','awayTeamName','awayName','away')
    ps=f.get('participants') or f.get('teams') or []
    if isinstance(ps,dict):
        h=h or txt(ps.get('home') or ps.get('participant1'),'name','teamName','shortName')
        a=a or txt(ps.get('away') or ps.get('participant2'),'name','teamName','shortName')
    elif isinstance(ps,list):
        vals=[txt(p,'name','teamName','shortName','title') for p in ps if isinstance(p,dict)]
        vals=[v for v in vals if v]
        if len(vals)>=2: h=h or vals[0]; a=a or vals[1]
    return h,a

@st.cache_data(ttl=3600,show_spinner=False)
def markets_catalog():
    data=api_get('markets',{'language':'en'}); out={}
    if not isinstance(data,list): return out
    for m in data:
        if not isinstance(m,dict): continue
        mid=str(m.get('marketId')); out[mid]={'name':m.get('marketName') or f'MarchÃ© {mid}','handicap':m.get('handicap'),'outcomes':{}}
        for o in m.get('outcomes') or []:
            if isinstance(o,dict): out[mid]['outcomes'][str(o.get('outcomeId'))]=o.get('outcomeName') or ''
    return out

def fr_market(n):
    return {'Full Time Result':'RÃ©sultat final (1X2)','Both Teams To Score':'Les deux Ã©quipes marquent','Over Under Full Time':'Total buts â€” Plus/Moins','Asian Handicap':'Handicap asiatique','Double Chance':'Double chance','Draw No Bet':'RemboursÃ© si nul','Half Time Result':'RÃ©sultat Ã  la mi-temps','Over Under Half Time':'Total buts mi-temps â€” Plus/Moins','Correct Score':'Score exact'}.get((n or '').strip(),n or '')

def fr_outcome(o,market,line=None):
    o=str(o or '').strip(); ml=(market or '').lower(); x=line
    if o=='1' and 'result' in ml: return '1 â€” Victoire Ã©quipe 1'
    if o.upper()=='X' and 'result' in ml: return 'X â€” Match nul'
    if o=='2' and 'result' in ml: return '2 â€” Victoire Ã©quipe 2'
    if o.lower()=='yes': return 'Oui'
    if o.lower()=='no': return 'Non'
    if o.lower()=='over': return f'Plus de {x}' if x not in (None,'',0) else 'Plus de'
    if o.lower()=='under': return f'Moins de {x}' if x not in (None,'',0) else 'Moins de'
    return o

def parse_board(board,cat):
    rows=[]; markets=(board or {}).get('markets') or {}
    for mid,m in (markets.items() if isinstance(markets,dict) else enumerate(markets)):
        if not isinstance(m,dict): continue
        meta=cat.get(str(mid),{}); rawm=meta.get('name') or m.get('marketName') or f'MarchÃ© {mid}'; mname=fr_market(rawm); handicap=meta.get('handicap'); names=meta.get('outcomes',{})
        outcomes=m.get('outcomes') or {}
        for oid,o in (outcomes.items() if isinstance(outcomes,dict) else enumerate(outcomes)):
            if not isinstance(o,dict): continue
            players=o.get('players'); legs=list(players.values()) if isinstance(players,dict) else (players if isinstance(players,list) else [o])
            for leg in legs:
                if not isinstance(leg,dict) or leg.get('active',o.get('active',True)) is False: continue
                try: price=float(leg.get('price',o.get('price')))
                except (TypeError,ValueError): continue
                raw=leg.get('outcomeName') or leg.get('name') or leg.get('label') or names.get(str(oid)) or o.get('outcomeName') or str(oid)
                line=leg.get('line') or o.get('line') or handicap
                boid=str(leg.get('bookmakerOutcomeId') or '')
                if line in (None,'') and '/' in boid:
                    left,_=boid.split('/',1)
                    if left.replace('.','',1).isdigit(): line=left
                rows.append({'market_id':str(mid),'outcome_id':str(oid),'market':mname,'outcome':fr_outcome(raw,rawm,line),'line':line,'odds':price})
    # normalize each market's active outcomes as a margin-adjusted market probability
    by={}
    for r in rows: by.setdefault(r['market_id'],[]).append(r)
    final=[]
    for grp in by.values():
        s=sum(1/r['odds'] for r in grp if r['odds']>1)
        for r in grp:
            r['implied']=100/r['odds']; r['estimated']=100*(1/r['odds'])/s if s else r['implied']; r['value']=(r['estimated']/100)*r['odds']-1; final.append(r)
    return final

def tm(v):
    if not v:return ''
    try:return datetime.fromisoformat(str(v).replace('Z','+00:00')).strftime('%d/%m %H:%M UTC')
    except:return str(v)

with st.container(border=True):
    a,b=st.columns(2); min_odds=a.number_input('Cote min',1.01,100.0,1.50,0.01); max_odds=b.number_input('Cote max',1.01,100.0,2.00,0.01)
    c,d=st.columns(2); min_prob=c.number_input('ProbabilitÃ© estimÃ©e min (%)',0.0,99.9,60.0,1.0); hours=d.selectbox('FenÃªtre',[6,12,24,36,48],index=2,format_func=lambda x:f'{x} h')
    try:
        cat=markets_catalog() if API_KEY else {}
    except Exception: cat={}
    options=['Tous les marchÃ©s']+sorted({fr_market(v['name']) for v in cat.values() if v.get('name')})
    market=st.selectbox('MarchÃ©',options)
    x,y=st.columns(2); scan=x.button('ðŸ”Ž SCANNER 1xBET',use_container_width=True,type='primary'); demo=y.button('ðŸ§ª DÃ‰MO',use_container_width=True)

if demo:
    demo_data=[('Barcelona','Real Madrid','Demo â€¢ Liga','RÃ©sultat final (1X2)','1 â€” Victoire Ã©quipe 1',1.72,58.1),('PSG','Marseille','Demo â€¢ Ligue 1','Total buts â€” Plus/Moins','Plus de 2.5',1.80,58.0),('Bayern','Dortmund','Demo â€¢ Bundesliga','Les deux Ã©quipes marquent','Oui',1.66,61.0)]
    st.session_state.rows=[{'home':a,'away':b,'league':c,'market':d,'outcome':e,'odds':f,'estimated':g,'implied':100/f,'value':g/100*f-1} for a,b,c,d,e,f,g in demo_data if min_odds<=f<=max_odds and g>=min_prob and (market=='Tous les marchÃ©s' or market==d)]
    st.session_state.note='Mode dÃ©mo'

if scan:
    if max_odds<min_odds: st.error('La cote max doit Ãªtre supÃ©rieure ou Ã©gale Ã  la cote min.')
    elif not API_KEY: st.error('ClÃ© OddsPapi absente dans Streamlit â†’ Secrets.')
    else:
        with st.spinner('Scan 1xBet en coursâ€¦'):
            try:
                cat=markets_catalog(); now=datetime.now(timezone.utc); end=now+timedelta(hours=hours)
                fs=api_get('fixtures',{'sportId':10,'from':now.strftime('%Y-%m-%dT%H:%M:%SZ'),'to':end.strftime('%Y-%m-%dT%H:%M:%SZ'),'statusId':0,'hasOdds':'true','bookmakers':BOOKMAKER,'language':'en'})
                fs=fs if isinstance(fs,list) else []; rows=[]; checked=0
                for f in fs:
                    fid=f.get('fixtureId') if isinstance(f,dict) else None
                    if not fid: continue
                    try: od=api_get('odds',{'fixtureId':fid,'bookmakers':BOOKMAKER,'language':'en','verbosity':3})
                    except Exception: continue
                    boards=od.get('bookmakerOdds') or {}; board=boards.get(BOOKMAKER) or (next(iter(boards.values())) if boards else None)
                    h=od.get('participant1Name') or f.get('participant1Name'); a=od.get('participant2Name') or f.get('participant2Name')
                    if not h or not a:
                        fh,fa=fixture_names(f); h=h or fh; a=a or fa
                    league=od.get('tournamentName') or f.get('tournamentName') or f.get('leagueName') or ''
                    start=od.get('startTime') or f.get('startTime')
                    for q in parse_board(board,cat):
                        if not(min_odds<=q['odds']<=max_odds):continue
                        if market!='Tous les marchÃ©s' and market!=q['market']:continue
                        if q['estimated']<min_prob:continue
                        rows.append({**q,'home':h,'away':a,'league':league,'start':start,'fixture_id':fid})
                    checked+=1
                rows.sort(key=lambda r:(r['estimated'],r['value']),reverse=True); st.session_state.rows=rows; st.session_state.note=f'{checked} match(s) vÃ©rifiÃ©(s) â€¢ {len(rows)} pari(s) correspondant aux critÃ¨res'
            except Exception as e: st.error(str(e)); st.session_state.rows=[]; st.session_state.note=''

rows=st.session_state.get('rows',[]); note=st.session_state.get('note','')
if note: st.info(note)
for r in rows:
    est=float(r.get('estimated',0)); imp=float(r.get('implied',0)); val=float(r.get('value',0))*100; badge='ðŸŸ¢ FORTE' if est>=70 else ('ðŸŸ¡ INTÃ‰RESSANTE' if est>=60 else 'âšª STANDARD')
    with st.container(border=True):
        st.subheader(f"{r.get('home','')} â€” {r.get('away','')}"); st.caption(f"{r.get('league','')} {'â€¢ '+tm(r.get('start')) if r.get('start') else ''}")
        st.write(f"**MarchÃ© :** {r.get('market','')}"); st.write(f"**Pari :** {r.get('outcome','')}")
        if r.get('line') not in (None,''): st.caption(f"Ligne : {r['line']}")
        c1,c2=st.columns(2); c1.metric('Cote 1xBet',f"{r['odds']:.2f}"); c2.metric('ProbabilitÃ© estimÃ©e',f'{est:.1f}%')
        st.write(f'ProbabilitÃ© implicite : **{imp:.1f}%** â€¢ Ã‰cart estimÃ© : **{val:+.1f} points** â€¢ {badge}')
        st.write(f"{'ðŸ“ˆ' if val>0 else 'ðŸ“‰'} Value thÃ©orique : **{val:+.1f}%**")

if not rows and note: st.warning('Aucun pari ne correspond aux critÃ¨res.')
st.divider(); st.caption("ProbabilitÃ© estimÃ©e V1 = probabilitÃ© de marchÃ© corrigÃ©e de la marge (normalisation des issues disponibles). Ce nâ€™est pas encore un modÃ¨le prÃ©dictif indÃ©pendant et ce nâ€™est jamais une garantie de gain.")
st.caption('V2 pourra ajouter historiques, forme, buts, xG, absences et calibration pour produire une probabilitÃ© rÃ©ellement indÃ©pendante.')
