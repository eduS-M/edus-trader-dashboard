"""
EduS Trader - Servidor Railway (Cloud)
Misma lógica que el servidor local — yfinance para todo.
"""

from flask import Flask, jsonify, send_file, Response
from flask_cors import CORS
import yfinance as yf
import requests
from bs4 import BeautifulSoup
import os
import math
from datetime import datetime, date
import threading
import time

app = Flask(__name__)
CORS(app)

# ─── CACHE ───
_cache      = {}
_cache_lock = threading.Lock()
CACHE_TTL   = {
    'vix':      120,   # 2 min (Railway es más lento que local)
    'quotes':    60,
    'heatmap':  180,
    'calendar': 300,
    'news':     120,
    'gex_SPX':  600,
    'gex_NDX':  600,
}

def get_cached(key, ttl, fn):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry['ts']) < ttl:
            return entry['data']
    data = fn()
    with _cache_lock:
        _cache[key] = {'data': data, 'ts': time.time()}
    return data

HDRS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124',
    'Accept-Language': 'en-US,en;q=0.9',
}

# ─── PÁGINA PRINCIPAL ───
@app.route('/')
def index():
    return send_file('index.html')

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'version': '6', 'api': 'yfinance'})

# ─── VIX ───
@app.route('/api/vix')
def api_vix():
    def fetch():
        try:
            ticker = yf.Ticker('^VIX')
            # 5 sesiones diarias → cierre anterior REAL (maneja fines de semana/festivos)
            daily = ticker.history(period='5d', interval='1d')
            if daily.empty:
                return {'error': 'Sin datos VIX'}
            prev_close = round(float(daily['Close'].iloc[-2]), 2) if len(daily) >= 2 \
                         else round(float(daily['Close'].iloc[-1]), 2)
            open_today = round(float(daily['Open'].iloc[-1]), 2)
            # Intradía 5min
            intra  = ticker.history(period='1d', interval='5m')
            points = []
            if not intra.empty:
                for ts, row in intra.iterrows():
                    points.append({
                        'time':  ts.strftime('%H:%M'),
                        'close': round(float(row['Close']), 2),
                    })
            info    = ticker.fast_info
            current = round(float(info.last_price), 2) if hasattr(info, 'last_price') \
                      else (points[-1]['close'] if points else prev_close)
            if not points:
                points = [{'time': datetime.now().strftime('%H:%M'), 'close': current}]
            return {
                'current':      current,
                'prev_close':   prev_close,
                'open':         open_today,
                'change_pct':   round((current - prev_close) / prev_close * 100, 2),
                'change_intra': round((current - open_today) / open_today * 100, 2) if open_today else 0,
                'points':       points,
            }
        except Exception as e:
            return {'error': str(e)}
    return jsonify(get_cached('vix', CACHE_TTL['vix'], fetch))

# ─── ÍNDICES ───
INDEX_SYMBOLS = {
    'sp500':  '^GSPC',
    'nasdaq': '^IXIC',
    'dow':    '^DJI',
    'bitcoin':'BTC-USD',
    'eurusd': 'EURUSD=X',
    'gold':   'GC=F',
}

@app.route('/api/indices')
def api_indices():
    def fetch():
        result = {}
        syms   = list(INDEX_SYMBOLS.values())
        try:
            tickers = yf.Tickers(' '.join(syms))
            for name, sym in INDEX_SYMBOLS.items():
                try:
                    info  = tickers.tickers[sym].fast_info
                    price = round(float(info.last_price), 4)
                    prev  = round(float(info.previous_close), 4)
                    chg   = round((price - prev) / prev * 100, 2) if prev else 0
                    result[name] = {'price': price, 'change_pct': chg, 'symbol': sym}
                except:
                    result[name] = {'error': True, 'symbol': sym}
        except Exception as e:
            return {'error': str(e)}
        return result
    return jsonify(get_cached('quotes', CACHE_TTL['quotes'], fetch))

# ─── HEATMAPS ───
HEATMAP_SYMBOLS = {
    'sp500': ['AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','JPM','V','WMT',
              'MA','XOM','UNH','LLY','JNJ','AVGO','HD','PG','COST','NFLX',
              'CRM','ORCL','AMD','BAC','MRK','CVX','KO','ABBV','PEP','BRK-B'],
    'nasdaq':['QQQ','AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','AVGO','NFLX',
              'AMD','COST','ADBE','QCOM','TXN','PANW','MU','KLAC','MRVL','LRCX'],
    'crypto':['BTC-USD','ETH-USD','BNB-USD','SOL-USD','XRP-USD','DOGE-USD',
              'ADA-USD','AVAX-USD','LINK-USD','DOT-USD'],
}

@app.route('/api/heatmap/<group>')
def api_heatmap(group):
    if group not in HEATMAP_SYMBOLS:
        return jsonify({'error': 'Grupo no válido'}), 400
    cache_key = f'heatmap_{group}'
    def fetch():
        syms   = HEATMAP_SYMBOLS[group]
        result = []
        try:
            tickers = yf.Tickers(' '.join(syms))
            for sym in syms:
                try:
                    info  = tickers.tickers[sym].fast_info
                    price = round(float(info.last_price), 2)
                    prev  = round(float(info.previous_close), 2)
                    chg   = round((price - prev) / prev * 100, 2) if prev else 0
                    label = sym.replace('-USD', '').replace('^', '')
                    result.append({'sym': label, 'chg': chg, 'price': price})
                except:
                    result.append({'sym': sym.replace('-USD', ''), 'chg': 0, 'price': 0})
        except Exception as e:
            return {'error': str(e)}
        return result
    return jsonify(get_cached(cache_key, CACHE_TTL['heatmap'], fetch))

# ─── CALENDARIO FOREX FACTORY ───
@app.route('/api/calendar')
def api_calendar():
    def fetch():
        try:
            today = date.today()
            url   = f"https://www.forexfactory.com/calendar?day={today.strftime('%m%d')}.{today.year}"
            r = requests.get(url, headers=HDRS, timeout=15)
            if r.status_code != 200:
                raise Exception(f'HTTP {r.status_code}')
            soup   = BeautifulSoup(r.text, 'html.parser')
            rows   = soup.select('tr.calendar__row')
            events = []
            last_t = ''
            KEEP   = {'USD','EUR','GBP','JPY','CAD','AUD','CHF','NZD'}
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
                    ic  = (ie.get('class') or [''])[0] if ie else ''
                    imp = 'High' if 'red' in ic else 'Medium' if 'orange' in ic else 'Low'
                    ee  = row.select_one('.calendar__event-title') or row.select_one('.calendar__event')
                    evt = ee.get_text(strip=True) if ee else ''
                    if not evt:
                        continue
                    ac = row.select_one('.calendar__actual')
                    fc = row.select_one('.calendar__forecast')
                    pr = row.select_one('.calendar__previous')
                    events.append({
                        'time':     last_t,
                        'currency': ccy,
                        'impact':   imp,
                        'event':    evt,
                        'actual':   ac.get_text(strip=True) if ac else '',
                        'forecast': fc.get_text(strip=True) if fc else '',
                        'previous': pr.get_text(strip=True) if pr else '',
                    })
                except:
                    continue
            return events if events else _fallback_calendar()
        except Exception as e:
            print(f'Calendar error: {e}')
            return _fallback_calendar()
    return jsonify(get_cached('calendar', CACHE_TTL['calendar'], fetch))

def _fallback_calendar():
    return [
        {'time':'8:30am','currency':'USD','impact':'High',  'event':'Initial Jobless Claims','actual':'','forecast':'225K','previous':'219K'},
        {'time':'10:00am','currency':'USD','impact':'High', 'event':'Fed Chair Powell Speech','actual':'','forecast':'',   'previous':''},
        {'time':'2:00pm', 'currency':'EUR','impact':'High', 'event':'ECB President Speech',  'actual':'','forecast':'',   'previous':''},
    ]

# ─── NOTICIAS RSS ───
MARKET_KEYWORDS = [
    'fed','federal reserve','trump','tariff','inflation','interest rate',
    'market','nasdaq','s&p','dow','bitcoin','crypto','dollar','powell',
    'economy','gdp','cpi','jobs','employment','china','recession',
    'earnings','stocks','wall street','treasury','rate cut','ecb',
    'rate hike','bank','bonds','yield','debt','fiscal','monetary',
]

# RSS ordenados por fiabilidad en entornos cloud
RSS_FEEDS = [
    ('Reuters',    'https://feeds.reuters.com/reuters/businessNews',                          'text/xml'),
    ('AP Markets', 'https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US', 'text/xml'),
    ('MarketWatch','https://feeds.marketwatch.com/marketwatch/topstories/',                   'text/xml'),
    ('CNBC',       'https://www.cnbc.com/id/100003114/device/rss/rss.html',                   'text/xml'),
    ('Investing',  'https://www.investing.com/rss/news_25.rss',                               'text/xml'),
]

def _parse_rss(r_content):
    """Intenta parsear RSS con xml parser, fallback a html.parser"""
    try:
        return BeautifulSoup(r_content, 'xml')
    except:
        return BeautifulSoup(r_content, 'html.parser')

@app.route('/api/news')
def api_news():
    def fetch():
        all_items = []
        hdrs = {
            **HDRS,
            'Accept': 'application/rss+xml, application/xml, text/xml, */*',
        }
        for source_name, feed_url, _ in RSS_FEEDS:
            try:
                r = requests.get(feed_url, headers=hdrs, timeout=12)
                if r.status_code != 200:
                    print(f'[RSS] {source_name}: HTTP {r.status_code}')
                    continue
                soup = _parse_rss(r.content)
                items = soup.find_all('item')
                if not items:
                    # Algunos feeds usan <entry> (Atom)
                    items = soup.find_all('entry')
                count = 0
                for item in items[:20]:
                    title = item.find('title')
                    pub   = item.find('pubDate') or item.find('published') or item.find('updated')
                    link  = item.find('link')
                    txt   = title.get_text(strip=True) if title else ''
                    if not txt or not any(kw in txt.lower() for kw in MARKET_KEYWORDS):
                        continue
                    pub_str = pub.get_text(strip=True) if pub else ''
                    pub_dt  = datetime.now()
                    for fmt in ('%a, %d %b %Y %H:%M:%S %z','%a, %d %b %Y %H:%M:%S','%Y-%m-%dT%H:%M:%S'):
                        try:
                            pub_dt = datetime.strptime(pub_str[:25], fmt[:len(pub_str)])
                            break
                        except:
                            continue
                    lnk = ''
                    if link:
                        lnk = link.get('href') or link.get_text(strip=True) or ''
                    all_items.append({
                        'title':     txt,
                        'source':    source_name,
                        'link':      lnk,
                        'timestamp': pub_dt.isoformat(),
                    })
                    count += 1
                print(f'[RSS] {source_name}: {count} noticias')
            except Exception as e:
                print(f'[RSS] {source_name}: {e}')
        all_items.sort(key=lambda x: x['timestamp'], reverse=True)
        print(f'[RSS] Total: {len(all_items)} noticias')
        return all_items[:25]
    return jsonify(get_cached('news', CACHE_TTL['news'], fetch))

# ─── GEX: GAMMA / DELTA / VANNA EXPOSURE ───
def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _bs_d1d2(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return None, None
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        return d1, d1 - sigma * math.sqrt(T)
    except:
        return None, None

def bs_gamma(S, K, T, r, sigma):
    d1, _ = _bs_d1d2(S, K, T, r, sigma)
    if d1 is None: return 0.0
    return (math.exp(-0.5*d1**2)/math.sqrt(2*math.pi)) / (S*sigma*math.sqrt(T))

def bs_delta(S, K, T, r, sigma, opt_type='call'):
    d1, _ = _bs_d1d2(S, K, T, r, sigma)
    if d1 is None: return 0.0
    return _norm_cdf(d1) if opt_type == 'call' else _norm_cdf(d1) - 1.0

def bs_vanna(S, K, T, r, sigma):
    d1, d2 = _bs_d1d2(S, K, T, r, sigma)
    if d1 is None: return 0.0
    return -(math.exp(-0.5*d1**2)/math.sqrt(2*math.pi)) * d2 / sigma

def classify_exp(exp_str, today):
    try:
        days = (datetime.strptime(exp_str, '%Y-%m-%d').date() - today).days
        if days == 0:  return '0DTE'
        if days <= 7:  return 'weekly'
        if days <= 35: return 'monthly'
        return 'leaps'
    except:
        return 'monthly'

def compute_gex_yfinance(etf_symbol, futures_symbol=None, multiplier=50):
    """
    1. Obtiene precio del futuro real (ES=F / NQ=F) — solo precio
    2. Usa cadena de opciones del ETF (SPY/QQQ) — yfinance la entrega
    3. Escala strikes ETF → precio del futuro con ratio
       Ej: SPY=655, ES=F=6610 → ratio=10.09 → strike 660 ETF → 6659 en futuro
    """
    today  = date.today()
    r_rate = 0.05

    # 1. Precio del futuro
    futures_price = 0.0
    if futures_symbol:
        try:
            fp = float(yf.Ticker(futures_symbol).fast_info.last_price)
            if fp > 0:
                futures_price = round(fp, 2)
                print(f'[GEX] Futuro {futures_symbol} = {futures_price}')
        except Exception as e:
            print(f'[GEX] Futuro {futures_symbol} no disponible: {e}')

    # 2. Opciones del ETF
    ticker = yf.Ticker(etf_symbol)
    try:
        etf_price = round(float(ticker.fast_info.last_price), 2)
    except:
        return {'error': f'No se pudo obtener precio de {etf_symbol}'}
    if etf_price <= 0:
        return {'error': f'Precio invalido para {etf_symbol}'}

    exps = ticker.options
    if not exps:
        return {'error': 'Sin fechas de vencimiento — mercado cerrado?'}
    exps = list(exps[:6])

    # 3. Ratio de escala
    if futures_price > 0:
        scale_ratio  = futures_price / etf_price
        spot_display = futures_price
        used_sym     = futures_symbol
        used_mult    = multiplier
    else:
        scale_ratio  = 1.0
        spot_display = etf_price
        used_sym     = etf_symbol
        used_mult    = 100

    print(f'[GEX] ETF={etf_price} Futuro={futures_price} ratio={scale_ratio:.4f}')
    S = etf_price   # Black-Scholes siempre en precio ETF

    agg_gex = {}; agg_dex = {}; agg_vanna = {}; by_exp = {}

    for exp in exps:
        exp_class = classify_exp(exp, today)
        try:
            exp_dt = datetime.strptime(exp, '%Y-%m-%d').date()
        except:
            continue
        T = max((exp_dt - today).days / 365.0, 1/365.0)
        try:
            chain = ticker.option_chain(exp)
        except Exception as e:
            print(f'[GEX] option_chain {exp}: {e}')
            continue

        exp_gex = {}; exp_dex = {}; exp_vanna = {}

        for opt_type, df in [('call', chain.calls), ('put', chain.puts)]:
            for _, row in df.iterrows():
                try:
                    K_etf = float(row['strike'])
                    oi    = int(row.get('openInterest', 0) or 0)
                    iv    = float(row.get('impliedVolatility', 0) or 0)
                    if oi == 0 or iv < 0.01 or K_etf <= 0: continue
                    if K_etf < S * 0.80 or K_etf > S * 1.20: continue

                    gamma = bs_gamma(S, K_etf, T, r_rate, iv)
                    delta = bs_delta(S, K_etf, T, r_rate, iv, opt_type)
                    vanna = bs_vanna(S, K_etf, T, r_rate, iv)

                    gex_val   = gamma * oi * used_mult * S * S * 0.01
                    dex_val   = delta * oi * used_mult * S
                    vanna_val = vanna * oi * used_mult * S * iv
                    sign      = 1 if opt_type == 'call' else -1
                    K_fut     = round(K_etf * scale_ratio, 1)

                    exp_gex[K_fut]   = exp_gex.get(K_fut, 0)   + sign * gex_val
                    exp_dex[K_fut]   = exp_dex.get(K_fut, 0)   + sign * dex_val
                    exp_vanna[K_fut] = exp_vanna.get(K_fut, 0) + vanna_val
                    agg_gex[K_fut]   = agg_gex.get(K_fut, 0)   + sign * gex_val
                    agg_dex[K_fut]   = agg_dex.get(K_fut, 0)   + sign * dex_val
                    agg_vanna[K_fut] = agg_vanna.get(K_fut, 0) + vanna_val
                except:
                    continue

        if exp_gex:
            sk = sorted(exp_gex.keys())
            by_exp[exp] = {
                'class':   exp_class,
                'days':    (exp_dt - today).days,
                'strikes': sk,
                'gex':     [round(exp_gex[k]/1e9, 4)   for k in sk],
                'dex':     [round(exp_dex[k]/1e6, 2)   for k in sk],
                'vanna':   [round(exp_vanna[k]/1e6, 2) for k in sk],
            }

    if not agg_gex:
        return {'error': 'Sin datos de opciones — el mercado puede estar cerrado'}

    lo = spot_display * 0.90; hi = spot_display * 1.10
    strikes_f = sorted([k for k in agg_gex if lo <= k <= hi])
    gex_f     = [round(agg_gex.get(k,0)/1e9,   4) for k in strikes_f]
    dex_f     = [round(agg_dex.get(k,0)/1e6,   2) for k in strikes_f]
    vanna_f   = [round(agg_vanna.get(k,0)/1e6, 2) for k in strikes_f]

    pos = {k:v for k,v in agg_gex.items() if v>0}
    neg = {k:v for k,v in agg_gex.items() if v<0}
    call_wall  = max(pos, key=pos.get) if pos else spot_display
    put_wall   = min(neg, key=neg.get) if neg else spot_display
    zero_gamma = spot_display
    sk_sorted  = sorted(agg_gex.keys())
    for i in range(len(sk_sorted)-1):
        a, b = sk_sorted[i], sk_sorted[i+1]
        if agg_gex.get(a,0) * agg_gex.get(b,0) < 0:
            zero_gamma = round((a+b)/2, 1); break

    return {
        'spot':          spot_display,
        'etf':           etf_symbol,
        'source':        used_sym,
        'multiplier':    used_mult,
        'scale_ratio':   round(scale_ratio, 4),
        'strikes':       strikes_f,
        'gex':           gex_f,
        'dex':           dex_f,
        'vanna':         vanna_f,
        'call_wall':     round(call_wall, 1),
        'put_wall':      round(put_wall, 1),
        'zero_gamma':    round(zero_gamma, 1),
        'total_gex':     round(sum(gex_f), 2),
        'by_expiration': by_exp,
        'expirations':   list(by_exp.keys()),
    }

GEX_CONFIG = {
    'SPX': ('SPY', 'ES=F', 50),
    'NDX': ('QQQ', 'NQ=F', 20),
}

@app.route('/api/gex/<symbol>')
def api_gex(symbol):
    sym = symbol.upper()
    if sym not in GEX_CONFIG:
        return jsonify({'error': 'Solo SPX o NDX'}), 400
    etf, fut, mult = GEX_CONFIG[sym]
    cache_key = f'gex_{sym}'
    return jsonify(get_cached(cache_key, CACHE_TTL[cache_key],
                              lambda: compute_gex_yfinance(etf, futures_symbol=fut, multiplier=mult)))

# ─── ARRANQUE (Railway inyecta PORT automáticamente) ───
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'EduS Trader v6 (yfinance) — puerto {port}')
    app.run(host='0.0.0.0', port=port, debug=False)
