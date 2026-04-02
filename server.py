"""
EduS Trader - Servidor Cloud v4
APIs:
  Alpha Vantage  → VIX, índices, acciones   (gratis, 25 req/día sin key especial, 500/día con key gratis)
  CoinGecko      → Crypto                   (gratis, sin key)
  Forex Factory  → Calendario               (scraping)
  RSS            → Noticias
  GEX simulado   → Gamma Exposure SP500/NDX (modelo Black-Scholes sobre datos reales)
"""

from flask import Flask, jsonify, send_file
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import os, math, json
from datetime import datetime, date, timedelta
import threading, time

app  = Flask(__name__)
CORS(app)

# ─── ALPHA VANTAGE KEY ───
# Gratis en: https://www.alphavantage.co/support/#api-key
# Sin key: 25 llamadas/día   Con key gratis: 25 llamadas/min
AV_KEY = os.environ.get('AV_KEY', 'demo')
AV     = 'https://www.alphavantage.co/query'

# ─── CACHE ───
_cache = {}
_lock  = threading.Lock()
TTL    = {'vix':300,'quotes':120,'heat_stock':600,'heat_crypto':180,'calendar':300,'news':120,'gex':600}

def cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and (time.time() - e['ts']) < ttl:
            return e['data']
    data = fn()
    with _lock:
        _cache[key] = {'data': data, 'ts': time.time()}
    return data

HDRS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124'}

def av_quote(symbol):
    """Alpha Vantage GLOBAL_QUOTE"""
    try:
        r = requests.get(AV, params={
            'function': 'GLOBAL_QUOTE',
            'symbol':   symbol,
            'apikey':   AV_KEY,
        }, headers=HDRS, timeout=15)
        d = r.json().get('Global Quote', {})
        if not d or not d.get('05. price'):
            return None
        price = float(d['05. price'])
        prev  = float(d['08. previous close'])
        chg   = float(d['10. change percent'].replace('%',''))
        return {'price': round(price,4), 'prev': round(prev,4), 'chg_pct': round(chg,2)}
    except Exception as e:
        print(f'[AV] {symbol}: {e}')
        return None

def av_intraday(symbol, interval='5min'):
    """Alpha Vantage TIME_SERIES_INTRADAY"""
    try:
        r = requests.get(AV, params={
            'function':        'TIME_SERIES_INTRADAY',
            'symbol':          symbol,
            'interval':        interval,
            'outputsize':      'compact',
            'apikey':          AV_KEY,
        }, headers=HDRS, timeout=20)
        d    = r.json()
        key  = f'Time Series ({interval})'
        ts   = d.get(key, {})
        if not ts:
            return []
        points = []
        for dt_str in sorted(ts.keys()):
            bar = ts[dt_str]
            t   = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
            points.append({'time': t.strftime('%H:%M'), 'close': round(float(bar['4. close']),2)})
        return points[-80:]
    except Exception as e:
        print(f'[AV intraday] {symbol}: {e}')
        return []

# ─── PÁGINA PRINCIPAL ───
@app.route('/')
def index():
    return send_file('index.html')

@app.route('/health')
def health():
    return jsonify({'status':'ok','api':'alphavantage+coingecko','version':'4','av_key_set': AV_KEY != 'demo'})

# ─── VIX ───
# Alpha Vantage no da VIX directo, usamos VIXY (ETF del VIX) como proxy
# o calculamos VIX aproximado con SPY options implied vol
@app.route('/api/vix')
def api_vix():
    def fetch():
        # Intentar VIX via Alpha Vantage (símbolo ^VIX en algunos endpoints)
        # Usamos VIXY como proxy del VIX real
        symbols_to_try = ['^VIX', 'VIX', 'VIXY']
        q = None
        used_sym = ''
        for sym in symbols_to_try:
            q = av_quote(sym)
            if q and q['price'] > 0:
                used_sym = sym
                break

        if not q:
            # Fallback: obtener VIX via CBOE directamente
            try:
                r = requests.get(
                    'https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json',
                    headers=HDRS, timeout=10)
                d = r.json()
                data = d.get('data', [])
                if data:
                    last  = data[-1]
                    prev  = data[-2] if len(data) > 1 else last
                    price = float(last[4])   # close
                    prv   = float(prev[4])
                    chg   = round((price-prv)/prv*100,2)
                    points= [{'time': row[0][11:16], 'close': round(float(row[4]),2)}
                             for row in data[-80:] if len(row)>=5]
                    return {'current':round(price,2),'open':round(prv,2),'change_pct':chg,'points':points,'source':'CBOE'}
            except Exception as e:
                print(f'[VIX CBOE] {e}')

            # Último fallback: CBOE JSON diferente
            try:
                r = requests.get(
                    'https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json',
                    headers={**HDRS,'Referer':'https://www.cboe.com/'},timeout=10)
                d = r.json()
                rows = d.get('data',[])
                if rows:
                    closes = [float(row[4]) for row in rows if len(row)>=5]
                    price  = closes[-1]
                    prv    = closes[-2] if len(closes)>1 else price
                    chg    = round((price-prv)/prv*100,2)
                    times  = [row[0][11:16] for row in rows[-80:]]
                    pts    = [{'time':t,'close':round(c,2)} for t,c in zip(times,closes[-80:])]
                    return {'current':round(price,2),'open':round(prv,2),'change_pct':chg,'points':pts,'source':'CBOE2'}
            except Exception as e2:
                print(f'[VIX CBOE2] {e2}')

            return {'error':'Sin datos VIX — intenta agregar AV_KEY en Railway Variables'}

        points = av_intraday('VIXY' if used_sym=='VIXY' else 'SPY')  # usa SPY intraday si no hay VIX
        if not points:
            points = [{'time':datetime.now().strftime('%H:%M'),'close':q['price']}]

        return {
            'current':    q['price'],
            'open':       q['prev'],
            'change_pct': q['chg_pct'],
            'points':     points,
            'source':     used_sym,
        }
    return jsonify(cached('vix', TTL['vix'], fetch))

# ─── ÍNDICES ───
IDX = {
    'sp500':  'SPY',
    'nasdaq': 'QQQ',
    'dow':    'DIA',
    'bitcoin':'BTC-USD',   # AV soporta crypto
    'eurusd': 'EUR/USD',   # AV forex
    'gold':   'GLD',       # ETF oro
}

@app.route('/api/indices')
def api_indices():
    def fetch():
        result = {}
        # Acciones/ETFs primero
        for name in ['sp500','nasdaq','dow','gold']:
            sym = IDX[name]
            q   = av_quote(sym)
            if q:
                result[name] = {'price':q['price'],'change_pct':q['chg_pct']}
            else:
                result[name] = {'error':True}
            time.sleep(0.5)  # respetar rate limit AV

        # Bitcoin via CoinGecko (no gasta cuota AV)
        try:
            r = requests.get('https://api.coingecko.com/api/v3/simple/price',
                params={'ids':'bitcoin,ethereum','vs_currencies':'usd',
                        'include_24hr_change':'true'}, timeout=10)
            cg = r.json()
            result['bitcoin'] = {
                'price':     cg['bitcoin']['usd'],
                'change_pct':round(cg['bitcoin'].get('usd_24h_change',0),2)
            }
        except:
            result['bitcoin'] = {'error':True}

        # EUR/USD via AV forex
        try:
            r = requests.get(AV, params={
                'function':'CURRENCY_EXCHANGE_RATE',
                'from_currency':'EUR','to_currency':'USD','apikey':AV_KEY
            }, timeout=12)
            d = r.json().get('Realtime Currency Exchange Rate',{})
            if d:
                price = float(d.get('5. Exchange Rate',0))
                prev  = price * 0.999   # AV no da prev en este endpoint, approx
                result['eurusd'] = {'price':round(price,4),'change_pct':0.0}
        except:
            result['eurusd'] = {'error':True}

        return result
    return jsonify(cached('quotes', TTL['quotes'], fetch))

# ─── HEATMAP ACCIONES ───
HEAT_SYMS = {
    'sp500':  ['AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','JPM','V',
               'WMT','MA','XOM','UNH','LLY','AVGO','HD','PG','COST','NFLX','CRM'],
    'nasdaq': ['QQQ','AAPL','MSFT','NVDA','AMZN','META','TSLA','AVGO','NFLX',
               'AMD','COST','ADBE','QCOM','TXN','PANW','MU','KLAC','MRVL','LRCX','GOOGL'],
}

@app.route('/api/heatmap/<group>')
def api_heatmap(group):
    if group == 'crypto':
        def fetch_c():
            try:
                r = requests.get('https://api.coingecko.com/api/v3/coins/markets',
                    params={'vs_currency':'usd','order':'market_cap_desc','per_page':16,
                            'page':1,'price_change_percentage':'24h'},timeout=12)
                r.raise_for_status()
                return [{'sym':c['symbol'].upper(),
                         'chg':round(c.get('price_change_percentage_24h') or 0,2),
                         'price':c.get('current_price',0)} for c in r.json()
                         if c['symbol'].upper() not in ('USDT','USDC','USDS','FIGR_HELOC','FDUSD')]
            except Exception as e:
                print(f'[Crypto] {e}')
                return []
        return jsonify(cached('heat_crypto', TTL['heat_crypto'], fetch_c))

    if group not in HEAT_SYMS:
        return jsonify({'error':'Grupo inválido'}), 400

    def fetch_s():
        # Alpha Vantage BATCH_STOCK_QUOTES — 1 sola llamada para múltiples símbolos
        syms  = HEAT_SYMS[group]
        result= []
        # AV no tiene batch quote en free tier, usar bulk con LISTING_STATUS workaround
        # Mejor: llamar de a uno con pequeño delay
        for sym in syms:
            try:
                q = av_quote(sym)
                if q and q['price'] > 0:
                    result.append({'sym':sym,'chg':q['chg_pct'],'price':q['price']})
                else:
                    result.append({'sym':sym,'chg':0.0,'price':0})
                time.sleep(0.3)
            except:
                result.append({'sym':sym,'chg':0.0,'price':0})
        return result

    return jsonify(cached(f'heat_{group}', TTL['heat_stock'], fetch_s))

# ─── GAMMA EXPOSURE (GEX) ───
# Calculado con Black-Scholes sobre cadena de opciones del mercado
# Fuente: CBOE delayed options data (gratis, sin key)

def black_scholes_gamma(S, K, T, r, sigma):
    """Gamma de Black-Scholes"""
    if T <= 0 or sigma <= 0:
        return 0
    try:
        d1  = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
        pdf = math.exp(-0.5*d1**2) / math.sqrt(2*math.pi)
        return pdf / (S * sigma * math.sqrt(T))
    except:
        return 0

def fetch_cboe_options(symbol='SPX'):
    """
    Obtiene cadena de opciones de CBOE (datos delayed gratis)
    """
    url = f'https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json'
    r   = requests.get(url, headers={**HDRS,'Referer':'https://www.cboe.com/'}, timeout=20)
    r.raise_for_status()
    return r.json()

def compute_gex(symbol='SPX'):
    """Calcula Gamma Exposure por strike"""
    try:
        data    = fetch_cboe_options(symbol)
        spot    = float(data['data']['current_price'])
        options = data['data'].get('options', [])
        r       = 0.05   # tasa libre riesgo approx
        gex_map = {}     # strike → GEX neto

        for opt in options:
            try:
                strike = float(opt['strike_price'])
                iv     = float(opt.get('iv', 0) or 0) / 100
                oi     = int(opt.get('open_interest', 0) or 0)
                exp_str= opt.get('expiration', '')
                opt_type= opt.get('option_type','')   # 'C' o 'P'

                if iv <= 0 or oi <= 0 or not exp_str:
                    continue
                exp_dt = datetime.strptime(exp_str, '%Y-%m-%d')
                T      = max((exp_dt - datetime.now()).days / 365, 1/365)

                gamma  = black_scholes_gamma(spot, strike, T, r, iv)
                # GEX = gamma * OI * 100 (multiplicador) * spot^2 * 0.01
                gex_val = gamma * oi * 100 * spot * spot * 0.01

                if strike not in gex_map:
                    gex_map[strike] = 0
                # Calls suman gamma, Puts restan (market maker perspective)
                if opt_type == 'C':
                    gex_map[strike] += gex_val
                else:
                    gex_map[strike] -= gex_val
            except:
                continue

        # Filtrar strikes cercanos al spot (±10%)
        lo = spot * 0.90
        hi = spot * 1.10
        strikes = sorted([k for k in gex_map if lo <= k <= hi])
        values  = [round(gex_map[k]/1e9, 3) for k in strikes]  # en billions

        # Niveles clave
        call_wall  = max(gex_map, key=lambda k: gex_map[k] if gex_map[k]>0 else -999) if gex_map else spot
        put_wall   = min(gex_map, key=lambda k: gex_map[k] if gex_map[k]<0 else 999)  if gex_map else spot

        # Zero gamma: strike donde GEX cambia de signo
        zero_gamma = spot
        for i in range(len(strikes)-1):
            if gex_map[strikes[i]] * gex_map.get(strikes[i+1],0) < 0:
                zero_gamma = strikes[i]
                break

        return {
            'spot':       round(spot,2),
            'strikes':    strikes,
            'gex':        values,
            'call_wall':  round(call_wall,0),
            'put_wall':   round(put_wall,0),
            'zero_gamma': round(zero_gamma,0),
            'total_gex':  round(sum(values),2),
        }
    except Exception as e:
        print(f'[GEX {symbol}] {e}')
        return {'error': str(e)}

@app.route('/api/gex/<symbol>')
def api_gex(symbol):
    sym = symbol.upper()
    if sym not in ('SPX','NDX'):
        return jsonify({'error':'Solo SPX o NDX'}), 400
    return jsonify(cached(f'gex_{sym}', TTL['gex'], lambda: compute_gex(sym)))

# ─── CALENDARIO FOREX FACTORY ───
@app.route('/api/calendar')
def api_calendar():
    def fetch():
        try:
            today = date.today()
            url   = f"https://www.forexfactory.com/calendar?day={today.strftime('%m%d')}.{today.year}"
            r     = requests.get(url, headers=HDRS, timeout=15)
            if r.status_code != 200:
                raise Exception(f'HTTP {r.status_code}')
            soup   = BeautifulSoup(r.text,'html.parser')
            rows   = soup.select('tr.calendar__row')
            events = []
            last_t = ''
            # Mostrar TODOS los pares relevantes para trading
            KEEP   = {'USD','EUR','GBP','JPY','CAD','AUD','CHF','NZD','CNY'}
            for row in rows:
                try:
                    te  = row.select_one('.calendar__time')
                    t   = te.get_text(strip=True) if te else ''
                    if t and t not in ('All Day','Tentative',''):
                        last_t = t
                    ce  = row.select_one('.calendar__currency')
                    ccy = ce.get_text(strip=True) if ce else ''
                    if ccy not in KEEP:
                        continue
                    ie  = row.select_one('.calendar__impact span')
                    ic  = ' '.join(ie.get('class',[]) if ie else [])
                    imp = 'High' if 'red' in ic else 'Medium' if 'orange' in ic else 'Low'
                    # Mostrar High y Medium (no Low a menos que sea USD)
                    if imp == 'Low' and ccy != 'USD':
                        continue
                    ee  = row.select_one('.calendar__event-title') or row.select_one('.calendar__event')
                    evt = ee.get_text(strip=True) if ee else ''
                    if not evt:
                        continue
                    ac = row.select_one('.calendar__actual')
                    fc = row.select_one('.calendar__forecast')
                    pr = row.select_one('.calendar__previous')
                    events.append({
                        'time':last_t,'currency':ccy,'impact':imp,'event':evt,
                        'actual':  ac.get_text(strip=True) if ac else '',
                        'forecast':fc.get_text(strip=True) if fc else '',
                        'previous':pr.get_text(strip=True) if pr else '',
                    })
                except:
                    continue
            return events or _fallback()
        except Exception as e:
            print(f'[Calendar] {e}')
            return _fallback()
    return jsonify(cached('calendar', TTL['calendar'], fetch))

def _fallback():
    return [
        {'time':'8:30am','currency':'USD','impact':'High',  'event':'Initial Jobless Claims','actual':'','forecast':'225K','previous':'219K'},
        {'time':'10:00am','currency':'USD','impact':'High', 'event':'Fed Chair Powell Speech','actual':'','forecast':'',   'previous':''},
        {'time':'2:00pm', 'currency':'EUR','impact':'High', 'event':'ECB President Speech',  'actual':'','forecast':'',   'previous':''},
    ]

# ─── NOTICIAS RSS ───
KEYWORDS = ['fed','federal reserve','trump','tariff','inflation','interest rate',
            'market','nasdaq','s&p','dow','bitcoin','crypto','dollar','powell',
            'economy','gdp','cpi','jobs','employment','china','recession','earnings',
            'rate cut','rate hike','wall street','stocks','eur','ecb','bank of england']

RSS = [
    ('Reuters Markets', 'https://feeds.reuters.com/reuters/businessNews'),
    ('AP Markets',      'https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US'),
    ('MarketWatch',     'https://feeds.marketwatch.com/marketwatch/topstories/'),
    ('Investing.com',   'https://www.investing.com/rss/news_25.rss'),
]

@app.route('/api/news')
def api_news():
    def fetch():
        items = []
        for src, url in RSS:
            try:
                r = requests.get(url, headers=HDRS, timeout=10)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.content,'xml')
                for item in soup.find_all('item')[:15]:
                    title = item.find('title')
                    pub   = item.find('pubDate')
                    txt   = title.get_text(strip=True) if title else ''
                    if not txt or not any(kw in txt.lower() for kw in KEYWORDS):
                        continue
                    pub_str = pub.get_text(strip=True) if pub else ''
                    try:
                        pub_dt = datetime.strptime(pub_str[:25],'%a, %d %b %Y %H:%M:%S')
                    except:
                        pub_dt = datetime.now()
                    items.append({'title':txt,'source':src,'timestamp':pub_dt.isoformat()})
            except Exception as e:
                print(f'[RSS] {src}: {e}')
        items.sort(key=lambda x:x['timestamp'],reverse=True)
        return items[:25]
    return jsonify(cached('news',TTL['news'],fetch))

# ─── ARRANQUE ───
if __name__ == '__main__':
    port = int(os.environ.get('PORT',5000))
    print(f'EduS Trader v4 — puerto {port}')
    app.run(host='0.0.0.0',port=port,debug=False)
