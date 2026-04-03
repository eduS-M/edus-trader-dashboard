"""
EduS Trader - Servidor Railway v8
Anti-bloqueo Yahoo Finance: session parchada + headers de navegador + fuentes alternativas
"""
from flask import Flask, jsonify, send_file
from flask_cors import CORS
import yfinance as yf
import requests
from bs4 import BeautifulSoup
import os, math, time, random
from datetime import datetime, date
import threading

app  = Flask(__name__)
CORS(app)
_cache = {}
_lock  = threading.Lock()
TTL    = {'vix':120,'quotes':60,'heatmap':180,'calendar':300,'news':120,'gex_SPX':600,'gex_NDX':600}

def cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and (time.time()-e['ts'])<ttl: return e['data']
    data = fn()
    with _lock: _cache[key] = {'data':data,'ts':time.time()}
    return data

UAS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0',
]
def bhdrs(): return {'User-Agent':random.choice(UAS),'Accept-Language':'en-US,en;q=0.9','Referer':'https://finance.yahoo.com/','Accept':'*/*'}
def new_session():
    s = requests.Session(); s.headers.update(bhdrs()); return s

@app.route('/')
def index(): return send_file('index.html')

@app.route('/health')
def health(): return jsonify({'status':'ok','version':'8'})

# ── Helpers ──
def get_quote(symbol):
    for attempt in range(2):
        try:
            t = yf.Ticker(symbol, session=new_session())
            i = t.fast_info
            p = float(i.last_price); v = float(i.previous_close)
            if p > 0: return {'price':round(p,4),'prev':round(v,4),'chg_pct':round((p-v)/v*100,2) if v else 0}
        except: pass
        try:
            r = requests.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d',
                             headers=bhdrs(), timeout=12)
            if r.status_code==200:
                m = r.json()['chart']['result'][0]['meta']
                p = float(m.get('regularMarketPrice',0))
                v = float(m.get('chartPreviousClose',0) or m.get('previousClose',p))
                if p>0: return {'price':round(p,4),'prev':round(v,4),'chg_pct':round((p-v)/v*100,2) if v else 0}
        except: pass
        time.sleep(0.5)
    return None

def get_intraday(symbol):
    try:
        t = yf.Ticker(symbol, session=new_session())
        h = t.history(period='1d', interval='5m')
        if not h.empty: return [{'time':ts.strftime('%H:%M'),'close':round(float(r['Close']),2)} for ts,r in h.iterrows()]
    except: pass
    try:
        r = requests.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=5m&range=1d',
                         headers=bhdrs(), timeout=15)
        if r.status_code==200:
            res = r.json()['chart']['result'][0]
            ts_l = res.get('timestamp',[])
            cl_l = res['indicators']['quote'][0].get('close',[])
            return [{'time':datetime.fromtimestamp(t).strftime('%H:%M'),'close':round(float(c),2)}
                    for t,c in zip(ts_l,cl_l) if c is not None]
    except: pass
    return []

def get_daily(symbol):
    try:
        t = yf.Ticker(symbol, session=new_session())
        h = t.history(period='5d', interval='1d')
        if not h.empty: return [{'open':round(float(r['Open']),2),'close':round(float(r['Close']),2)} for _,r in h.iterrows()]
    except: pass
    try:
        r = requests.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d',
                         headers=bhdrs(), timeout=12)
        if r.status_code==200:
            res = r.json()['chart']['result'][0]
            op  = res['indicators']['quote'][0].get('open',[])
            cl  = res['indicators']['quote'][0].get('close',[])
            return [{'open':round(float(o),2),'close':round(float(c),2)} for o,c in zip(op,cl) if c and o]
    except: pass
    return []

# ── VIX ──
@app.route('/api/vix')
def api_vix():
    def fetch():
        try:
            daily  = get_daily('^VIX')
            if not daily: return {'error':'Sin datos VIX — Yahoo Finance bloqueado temporalmente'}
            prev_close = daily[-2]['close'] if len(daily)>=2 else daily[-1]['close']
            open_today = daily[-1]['open']
            points = get_intraday('^VIX')
            q      = get_quote('^VIX')
            cur    = q['price'] if q else (points[-1]['close'] if points else prev_close)
            if not points: points=[{'time':datetime.now().strftime('%H:%M'),'close':cur}]
            return {'current':cur,'prev_close':prev_close,'open':open_today,
                    'change_pct':round((cur-prev_close)/prev_close*100,2) if prev_close else 0,
                    'change_intra':round((cur-open_today)/open_today*100,2) if open_today else 0,
                    'points':points}
        except Exception as e: return {'error':str(e)}
    return jsonify(cached('vix',TTL['vix'],fetch))

# ── INDICES ──
IDX = {'sp500':'^GSPC','nasdaq':'^IXIC','dow':'^DJI','bitcoin':'BTC-USD','eurusd':'EURUSD=X','gold':'GC=F'}

@app.route('/api/indices')
def api_indices():
    def fetch():
        result = {}
        try:
            tickers = yf.Tickers(' '.join(IDX.values()), session=new_session())
            for name,sym in IDX.items():
                try:
                    i = tickers.tickers[sym].fast_info
                    p = round(float(i.last_price),4); v=round(float(i.previous_close),4)
                    result[name]={'price':p,'change_pct':round((p-v)/v*100,2) if v else 0,'symbol':sym}
                except: result[name]=None
        except Exception as e:
            print(f'[idx batch] {e}'); result={k:None for k in IDX}

        for name,sym in IDX.items():
            if result.get(name): continue
            q = get_quote(sym)
            result[name] = {'price':q['price'],'change_pct':q['chg_pct'],'symbol':sym} if q else {'error':True,'symbol':sym}

        # Bitcoin via CoinGecko (nunca bloqueado)
        try:
            r = requests.get('https://api.coingecko.com/api/v3/simple/price',
                params={'ids':'bitcoin','vs_currencies':'usd','include_24hr_change':'true'},timeout=10)
            cg = r.json()
            result['bitcoin']={'price':cg['bitcoin']['usd'],'change_pct':round(cg['bitcoin'].get('usd_24h_change',0),2),'symbol':'BTC-USD'}
        except: pass
        return result
    return jsonify(cached('quotes',TTL['quotes'],fetch))

# ── HEATMAPS ──
HEAT = {
    'sp500':['AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','JPM','V','WMT','MA','XOM','UNH','LLY','JNJ','AVGO','HD','PG','COST','NFLX','CRM','ORCL','AMD','BAC','MRK','CVX','KO','ABBV','PEP','BRK-B'],
    'nasdaq':['QQQ','AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','AVGO','NFLX','AMD','COST','ADBE','QCOM','TXN','PANW','MU','KLAC','MRVL','LRCX'],
    'crypto':['BTC-USD','ETH-USD','BNB-USD','SOL-USD','XRP-USD','DOGE-USD','ADA-USD','AVAX-USD','LINK-USD','DOT-USD'],
}

@app.route('/api/heatmap/<group>')
def api_heatmap(group):
    if group not in HEAT: return jsonify({'error':'Grupo no válido'}),400
    def fetch():
        if group=='crypto':
            try:
                STABLE={'USDT','USDC','USDS','FDUSD','DAI','BUSD'}
                r=requests.get('https://api.coingecko.com/api/v3/coins/markets',
                    params={'vs_currency':'usd','order':'market_cap_desc','per_page':15,'page':1,'price_change_percentage':'24h'},timeout=12)
                return [{'sym':c['symbol'].upper(),'chg':round(c.get('price_change_percentage_24h') or 0,2),'price':c.get('current_price',0)}
                        for c in r.json() if c['symbol'].upper() not in STABLE][:12]
            except Exception as e: print(f'[crypto] {e}'); return []
        syms = HEAT[group]; result=[]
        try:
            tickers=yf.Tickers(' '.join(syms),session=new_session())
            for sym in syms:
                try:
                    i=tickers.tickers[sym].fast_info; p=round(float(i.last_price),2); v=round(float(i.previous_close),2)
                    result.append({'sym':sym.replace('-USD','').replace('^',''),'chg':round((p-v)/v*100,2) if v else 0,'price':p})
                except: result.append({'sym':sym.replace('-USD',''),'chg':0,'price':0})
        except Exception as e:
            print(f'[heat {group}] {e}')
            for sym in syms[:15]:
                q=get_quote(sym); lbl=sym.replace('-USD','').replace('^','')
                result.append({'sym':lbl,'chg':q['chg_pct'],'price':q['price']} if q else {'sym':lbl,'chg':0,'price':0})
                time.sleep(0.2)
        return result
    return jsonify(cached(f'heatmap_{group}',TTL['heatmap'],fetch))

# ── CALENDAR ──
@app.route('/api/calendar')
def api_calendar():
    def fetch():
        try:
            today=date.today()
            url=f"https://www.forexfactory.com/calendar?day={today.strftime('%m%d')}.{today.year}"
            r=requests.get(url,headers=bhdrs(),timeout=15)
            if r.status_code!=200: raise Exception(f'HTTP {r.status_code}')
            soup=BeautifulSoup(r.text,'html.parser'); rows=soup.select('tr.calendar__row')
            events=[]; last_t=''; KEEP={'USD','EUR','GBP','JPY','CAD','AUD','CHF','NZD'}
            for row in rows:
                try:
                    te=row.select_one('.calendar__time'); t=te.get_text(strip=True) if te else ''
                    if t and t not in ('All Day','Tentative',''): last_t=t
                    ce=row.select_one('.calendar__currency'); ccy=ce.get_text(strip=True) if ce else ''
                    if ccy not in KEEP: continue
                    ie=row.select_one('.calendar__impact span'); ic=(ie.get('class') or [''])[0] if ie else ''
                    imp='High' if 'red' in ic else 'Medium' if 'orange' in ic else 'Low'
                    ee=row.select_one('.calendar__event-title') or row.select_one('.calendar__event')
                    evt=ee.get_text(strip=True) if ee else ''
                    if not evt: continue
                    ac=row.select_one('.calendar__actual'); fc=row.select_one('.calendar__forecast'); pr=row.select_one('.calendar__previous')
                    events.append({'time':last_t,'currency':ccy,'impact':imp,'event':evt,
                                   'actual':ac.get_text(strip=True) if ac else '',
                                   'forecast':fc.get_text(strip=True) if fc else '',
                                   'previous':pr.get_text(strip=True) if pr else ''})
                except: continue
            return events if events else _fb()
        except Exception as e: print(f'Calendar: {e}'); return _fb()
    return jsonify(cached('calendar',TTL['calendar'],fetch))

def _fb(): return [
    {'time':'8:30am','currency':'USD','impact':'High','event':'Initial Jobless Claims','actual':'','forecast':'225K','previous':'219K'},
    {'time':'10:00am','currency':'USD','impact':'High','event':'Fed Chair Powell Speech','actual':'','forecast':'','previous':''},
]

# ── NEWS ──
KWS=['fed','federal reserve','trump','tariff','inflation','interest rate','market','nasdaq','s&p','dow','bitcoin','crypto','dollar','powell','economy','gdp','cpi','jobs','employment','china','recession','earnings','stocks','wall street','treasury','rate cut','ecb','rate hike','bonds','yield']
FEEDS=[('Reuters','https://feeds.reuters.com/reuters/businessNews'),('AP Markets','https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US'),('MarketWatch','https://feeds.marketwatch.com/marketwatch/topstories/'),('CNBC','https://www.cnbc.com/id/100003114/device/rss/rss.html'),('Investing','https://www.investing.com/rss/news_25.rss')]

@app.route('/api/news')
def api_news():
    def fetch():
        items=[]
        hdrs={**bhdrs(),'Accept':'application/rss+xml,application/xml,text/xml,*/*'}
        for src,url in FEEDS:
            try:
                r=requests.get(url,headers=hdrs,timeout=12)
                if r.status_code!=200: print(f'[RSS] {src}: HTTP {r.status_code}'); continue
                try: soup=BeautifulSoup(r.content,'xml')
                except: soup=BeautifulSoup(r.content,'html.parser')
                entries=soup.find_all('item') or soup.find_all('entry')
                cnt=0
                for it in entries[:20]:
                    ttl=it.find('title'); pub=it.find('pubDate') or it.find('published') or it.find('updated'); lnk=it.find('link')
                    txt=ttl.get_text(strip=True) if ttl else ''
                    if not txt or not any(k in txt.lower() for k in KWS): continue
                    ps=pub.get_text(strip=True) if pub else ''; pd=datetime.now()
                    for fmt in ('%a, %d %b %Y %H:%M:%S %z','%a, %d %b %Y %H:%M:%S','%Y-%m-%dT%H:%M:%S'):
                        try: pd=datetime.strptime(ps[:25],fmt[:25]); break
                        except: continue
                    lk=''
                    if lnk: lk=lnk.get('href') or lnk.get_text(strip=True) or ''
                    items.append({'title':txt,'source':src,'link':lk,'timestamp':pd.isoformat()}); cnt+=1
                print(f'[RSS] {src}: {cnt}')
            except Exception as e: print(f'[RSS] {src}: {e}')
        items.sort(key=lambda x:x['timestamp'],reverse=True)
        return items[:25]
    return jsonify(cached('news',TTL['news'],fetch))

# ── GEX ──
def ncdf(x): return 0.5*(1.0+math.erf(x/math.sqrt(2.0)))
def d1d2(S,K,T,r,s):
    if T<=0 or s<=0 or S<=0 or K<=0: return None,None
    try: d1=(math.log(S/K)+(r+0.5*s**2)*T)/(s*math.sqrt(T)); return d1,d1-s*math.sqrt(T)
    except: return None,None
def gam(S,K,T,r,s):
    d1,_=d1d2(S,K,T,r,s);
    if d1 is None: return 0.0
    return (math.exp(-0.5*d1**2)/math.sqrt(2*math.pi))/(S*s*math.sqrt(T))
def delt(S,K,T,r,s,ot):
    d1,_=d1d2(S,K,T,r,s);
    if d1 is None: return 0.0
    return ncdf(d1) if ot=='call' else ncdf(d1)-1.0
def van(S,K,T,r,s):
    d1,d2=d1d2(S,K,T,r,s);
    if d1 is None: return 0.0
    return -(math.exp(-0.5*d1**2)/math.sqrt(2*math.pi))*d2/s
def cexp(exp_str,today):
    try:
        days=(datetime.strptime(exp_str,'%Y-%m-%d').date()-today).days
        return '0DTE' if days==0 else 'weekly' if days<=7 else 'monthly' if days<=35 else 'leaps'
    except: return 'monthly'

def compute_gex(etf,fut=None,mult=50):
    today=date.today(); rr=0.05
    fp=0.0
    if fut:
        q=get_quote(fut)
        if q and q['price']>0: fp=q['price']; print(f'[GEX] {fut}={fp}')
    ticker=yf.Ticker(etf,session=new_session())
    try: ep=float(ticker.fast_info.last_price)
    except:
        q=get_quote(etf)
        if not q: return {'error':f'No se pudo obtener precio de {etf}'}
        ep=q['price']
    if ep<=0: return {'error':f'Precio inválido para {etf}'}
    exps=ticker.options
    if not exps: return {'error':'Sin fechas de vencimiento — mercado cerrado'}
    exps=list(exps[:6])
    if fp>0: sr=fp/ep; sd=fp; us=fut; um=mult
    else: sr=1.0; sd=ep; us=etf; um=100
    print(f'[GEX] ETF={ep} Fut={fp} ratio={sr:.4f}')
    S=ep; ag={}; ad={}; av={}; be={}
    for exp in exps:
        ec=cexp(exp,today)
        try: edt=datetime.strptime(exp,'%Y-%m-%d').date()
        except: continue
        T=max((edt-today).days/365.0,1/365.0)
        try: chain=ticker.option_chain(exp)
        except Exception as e: print(f'[GEX] chain {exp}: {e}'); continue
        eg={}; ed={}; ev={}
        for ot,df in [('call',chain.calls),('put',chain.puts)]:
            for _,row in df.iterrows():
                try:
                    K=float(row['strike']); oi=int(row.get('openInterest',0) or 0); iv=float(row.get('impliedVolatility',0) or 0)
                    if oi==0 or iv<0.01 or K<=0 or K<S*0.80 or K>S*1.20: continue
                    g=gam(S,K,T,rr,iv); d=delt(S,K,T,rr,iv,ot); v=van(S,K,T,rr,iv)
                    gv=g*oi*um*S*S*0.01; dv=d*oi*um*S; vv=v*oi*um*S*iv
                    sg=1 if ot=='call' else -1; kf=round(K*sr,1)
                    eg[kf]=eg.get(kf,0)+sg*gv; ed[kf]=ed.get(kf,0)+sg*dv; ev[kf]=ev.get(kf,0)+vv
                    ag[kf]=ag.get(kf,0)+sg*gv; ad[kf]=ad.get(kf,0)+sg*dv; av[kf]=av.get(kf,0)+vv
                except: continue
        if eg:
            sk=sorted(eg.keys())
            be[exp]={'class':ec,'days':(edt-today).days,'strikes':sk,
                     'gex':[round(eg[k]/1e9,4) for k in sk],'dex':[round(ed[k]/1e6,2) for k in sk],'vanna':[round(ev[k]/1e6,2) for k in sk]}
    if not ag: return {'error':'Sin datos de opciones — mercado puede estar cerrado'}
    lo=sd*0.90; hi=sd*1.10; sf=sorted([k for k in ag if lo<=k<=hi])
    gf=[round(ag.get(k,0)/1e9,4) for k in sf]; df2=[round(ad.get(k,0)/1e6,2) for k in sf]; vf=[round(av.get(k,0)/1e6,2) for k in sf]
    pos={k:v for k,v in ag.items() if v>0}; neg={k:v for k,v in ag.items() if v<0}
    cw=max(pos,key=pos.get) if pos else sd; pw=min(neg,key=neg.get) if neg else sd
    zg=sd
    for i in range(len(sf)-1):
        if ag.get(sf[i],0)*ag.get(sf[i+1],0)<0: zg=round((sf[i]+sf[i+1])/2,1); break
    return {'spot':sd,'etf':etf,'source':us,'multiplier':um,'scale_ratio':round(sr,4),
            'strikes':sf,'gex':gf,'dex':df2,'vanna':vf,'call_wall':round(cw,1),'put_wall':round(pw,1),
            'zero_gamma':round(zg,1),'total_gex':round(sum(gf),2),'by_expiration':be,'expirations':list(be.keys())}

GEX_CFG={'SPX':('SPY','ES=F',50),'NDX':('QQQ','NQ=F',20)}

@app.route('/api/gex/<symbol>')
def api_gex(symbol):
    sym=symbol.upper()
    if sym not in GEX_CFG: return jsonify({'error':'Solo SPX o NDX'}),400
    etf,fut,mult=GEX_CFG[sym]; ck=f'gex_{sym}'
    return jsonify(cached(ck,TTL[ck],lambda: compute_gex(etf,fut=fut,mult=mult)))

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    print(f'EduS Trader v8 — {port}')
    app.run(host='0.0.0.0',port=port,debug=False)
