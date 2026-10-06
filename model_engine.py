import math, io, requests
import pandas as pd
from datetime import datetime, timedelta

LEAGUES={
 'E0':'Premier League','E1':'Championship','E2':'League One','E3':'League Two',
 'D1':'Bundesliga','D2':'2. Bundesliga','I1':'Serie A','I2':'Serie B',
 'SP1':'La Liga','SP2':'Segunda','F1':'Ligue 1','F2':'Ligue 2','N1':'Eredivisie',
 'B1':'Belgium','P1':'Portugal','SC0':'Scotland','T1':'Turkey','G1':'Greece'
}
ALIASES={'Man City':'Manchester City','Man Utd':'Manchester United','Wolves':'Wolverhampton Wanderers','Tottenham':'Tottenham Hotspur','Newcastle':'Newcastle United','Nott\'m Forest':'Nottingham Forest','West Ham':'West Ham United','Ath Madrid':'Atletico Madrid','Ath Bilbao':'Athletic Club','Betis':'Real Betis','Inter':'Inter Milan','Milan':'AC Milan','PSG':'Paris Saint-Germain','Marseille':'Marseille','Bayern Munich':'Bayern Munich'}

def norm(s):
    s=str(s or '').strip()
    return ALIASES.get(s,s)

def _load(season, div):
    url=f'https://www.football-data.co.uk/mmz4281/{season}/{div}.csv'
    r=requests.get(url,timeout=20); r.raise_for_status()
    return pd.read_csv(io.BytesIO(r.content),encoding='latin1')

def build_model(days=365):
    now=datetime.utcnow(); cutoff=now-timedelta(days=days)
    # Recent completed seasons/files; filter rows by Date where possible.
    frames=[]
    for div in LEAGUES:
        for season in ('2526','2627'):
            try:
                df=_load(season,div)
                df['Div']=div
                frames.append(df)
            except Exception:
                continue
    if not frames: raise RuntimeError('Impossible de charger les résultats historiques publics.')
    df=pd.concat(frames,ignore_index=True)
    req={'HomeTeam','AwayTeam','FTHG','FTAG'}
    df=df.dropna(subset=req).copy()
    # Prefer roughly the latest 365 days when dates are available; otherwise keep available recent files.
    if 'Date' in df:
        dates=pd.to_datetime(df['Date'],dayfirst=True,errors='coerce')
        df=df[(dates.isna()) | (dates>=pd.Timestamp(cutoff))].copy()
    # recency weights: newer matches count more, but not enough to erase sample size
    if 'Date' in df:
        dates=pd.to_datetime(df['Date'],dayfirst=True,errors='coerce')
        age=(pd.Timestamp(now)-dates).dt.days.clip(lower=0,upper=365).fillna(180)
        df['_w']=0.35+0.65*(1-age/365)
    else: df['_w']=1.0
    models={}
    for div,g in df.groupby('Div'):
        if len(g)<20: continue
        w=g['_w'].astype(float)
        lh=(g.FTHG*w).sum()/w.sum(); la=(g.FTAG*w).sum()/w.sum()
        teams=set(g.HomeTeam.map(norm))|set(g.AwayTeam.map(norm)); tm={}
        for t in teams:
            h=g[g.HomeTeam.map(norm)==t]; a=g[g.AwayTeam.map(norm)==t]
            def avg(x,col,default):
                if len(x)==0:return default
                return float((x[col]*x['_w']).sum()/x['_w'].sum())
            # shrink toward league means; 6 matches pseudo-sample
            ha=(avg(h,'FTHG',lh)*len(h)+lh*6)/(len(h)+6)
            hd=(avg(h,'FTAG',la)*len(h)+la*6)/(len(h)+6)
            aa=(avg(a,'FTAG',la)*len(a)+la*6)/(len(a)+6)
            ad=(avg(a,'FTHG',lh)*len(a)+lh*6)/(len(a)+6)
            tm[t]=(ha/lh if lh else 1, hd/la if la else 1, aa/la if la else 1, ad/lh if lh else 1)
        models[div]=(lh,la,tm)
    return models

def _poisson(lam,k): return math.exp(-lam)*(lam**k)/math.factorial(k)

def predict(home,away,models,div=None):
    h=norm(home); a=norm(away)
    candidates=[div] if div else list(models)
    found=None
    for d in candidates:
        if d in models and (h in models[d][2] or a in models[d][2]): found=d; break
    if not found: return None
    lh,la,tm=models[found]; hs=tm.get(h,(1,1,1,1)); aws=tm.get(a,(1,1,1,1))
    eh=max(.05,lh*hs[0]*aws[3]); ea=max(.05,la*aws[2]*hs[1])
    grid={(i,j):_poisson(eh,i)*_poisson(ea,j) for i in range(13) for j in range(13)}
    z=sum(grid.values()); grid={k:v/z for k,v in grid.items()}
    p1=sum(v for (i,j),v in grid.items() if i>j); px=sum(v for (i,j),v in grid.items() if i==j); p2=1-p1-px
    btts=sum(v for (i,j),v in grid.items() if i>0 and j>0)
    return {'1':p1,'X':px,'2':p2,'yes':btts,'no':1-btts,'grid':grid,'home_goals':eh,'away_goals':ea}

def market_probability(market,outcome,line,pred):
    if not pred:return None
    m=(market or '').lower(); o=(outcome or '').strip().lower();
    if 'correct score' in m:
        import re
        z=re.search(r'(\d+)\s*[-:]\s*(\d+)',o)
        return pred['grid'].get((int(z.group(1)),int(z.group(2)))) if z else None
    if 'both teams' in m or 'both team' in m or 'les deux' in m:
        return pred['yes'] if o in ('yes','oui','1') else pred['no'] if o in ('no','non','2') else None
    if 'full time result' in m or 'résultat final' in m:
        return {'1':pred['1'],'x':pred['X'],'2':pred['2']}.get(o)
    if 'over under full time' in m or ('total' in m and 'half' not in m and 'mi-temps' not in m):
        try: L=float(line)
        except: return None
        over=sum(v for (i,j),v in pred['grid'].items() if i+j>L)
        return over if 'over' in o or 'plus' in o else 1-over if 'under' in o or 'moins' in o else None
    return None
