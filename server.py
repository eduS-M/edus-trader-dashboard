"""
EduS Trader - Servidor Cloud (Railway)
APIs usadas:
  - Finnhub.io      → VIX, índices, acciones  (gratis, 60 calls/min)
  - CoinGecko       → Crypto heatmap          (gratis, sin key)
  - Forex Factory   → Calendario económico    (scraping)
  - RSS feeds       → Noticias de mercado
"""

from flask import Flask, jsonify, send_file
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import os
from datetime import datetime, date
import threading
import time

app  = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────
# FINNHUB API KEY — gratuita, 60 req/min
# Regístrate en https://finnhub.io/register
# y reemplaza el valor de abajo con tu key
# ─────────────────────────────────────────
FINNHUB_KEY = os.environ.get('FINNHUB_KEY', 'TU_KEY_AQUI')
FINNHUB     = 'https://finnhub.io/api/v1'

# ─── CACHE ───
_cache = {}
_lock  = threading.Lock()
TTL    = {'vix':60, 'quotes':30, 'heat_stock':120, 'heat_crypto':120, 'calendar':300, 'news':120}

def cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and (time.time() - e['ts']) < ttl:
            return e['data']
    data = fn()
    with _lock:
        _cache[key] = {'data': data, 'ts': time.time()}
    return data

def fh(path, params={}):
    """Llamada a Finnhub con manejo de errores"""
    try:
        p = {'token': FINNHUB_KEY, **params}
        r = requests.get(FINNHUB + path, params=p, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f'[Finnhub] {path}: {e}')
        return None

# ─── PÁGINA PRINCIPAL ───
@app.route('/')
def index():
    return send_file('index.html')

# ─── HEALTH CHECK ───
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'key_set': FINNHUB_KEY != 'TU_KEY_AQUI'})

# ─── VIX ───
@app.route('/api/vix')
def api_vix():
    def fetch():
        try:
            # Quote actual del VIX
            q = fh('/quote', {'symbol': 'VIX'})
            if not q or q.get('c', 0) == 0:
                # Fallback: intenta con ^VIX
                q = fh('/quote', {'symbol': '^VIX'})
            if not q or q.get('c', 0) == 0:
                return {'error': 'Sin datos de VIX — verifica tu FINNHUB_KEY'}

            current  = round(float(q['c']), 2)
            prev     = round(float(q['pc']), 2)
            chg_pct  = round((current - prev) / prev * 100, 2) if prev else 0

            # Candles intradía (resolución 5 min, último día)
            now_ts   = int(time.time())
            from_ts  = now_ts - 86400
            candles  = fh('/indicator', {
                'symbol': 'VIX', 'resolution': '5',
                'from': from_ts, 'to': now_ts, 'indicator': 'pc'
            })

            # Candles via stock/candle endpoint
            c2 = fh('/stock/candle', {
                'symbol': 'VIX', 'resolution': '5',
                'from': from_ts, 'to': now_ts
            })

            points = []
            if c2 and c2.get('s') == 'ok' and c2.get('t'):
                for ts, close in zip(c2['t'], c2['c']):
                    dt = datetime.fromtimestamp(ts)
                    points.append({'time': dt.strftime('%H:%M'), 'close': round(close, 2)})
            
            # Si no hay candles, construir serie simple con el dato actual
            if not points:
                points = [{'time': datetime.now().strftime('%H:%M'), 'close': current}]

            return {
                'current':    current,
                'open':       prev,
                'change_pct': chg_pct,
                'points':     points
            }
        except Exception as e:
            return {'error': str(e)}
    return jsonify(cached('vix', TTL['vix'], fetch))

# ─── ÍNDICES ───
IDX_SYMBOLS = {
    'sp500':  'SPY',      # ETF del S&P500 (Finnhub lo soporta bien)
    'nasdaq': 'QQQ',      # ETF del Nasdaq
    'dow':    'DIA',      # ETF del Dow Jones
    'bitcoin':'BINANCE:BTCUSDT',
    'eurusd': 'OANDA:EUR_USD',
    'gold':   'OANDA:XAU_USD',
}

@app.route('/api/indices')
def api_indices():
    def fetch():
        result = {}
        for name, sym in IDX_SYMBOLS.items():
            try:
                q = fh('/quote', {'symbol': sym})
                if not q or q.get('c', 0) == 0:
                    result[name] = {'error': True}
                    continue
                price = round(float(q['c']), 4)
                prev  = round(float(q['pc']), 4)
                chg   = round((price - prev) / prev * 100, 2) if prev else 0
                result[name] = {'price': price, 'change_pct': chg}
            except Exception as e:
                result[name] = {'error': True}
        return result
    return jsonify(cached('quotes', TTL['quotes'], fetch))

# ─── HEATMAP ACCIONES (S&P500 y Nasdaq) ───
HEAT_STOCKS = {
    'sp500':  ['AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','JPM','V','WMT',
               'MA','XOM','UNH','LLY','AVGO','HD','PG','COST','NFLX','CRM',
               'ORCL','AMD','BAC','MRK','CVX','KO','ABBV','PEP','JNJ','BRK.B'],
    'nasdaq': ['QQQ','AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','AVGO','NFLX',
               'AMD','COST','ADBE','QCOM','TXN','PANW','MU','KLAC','MRVL','LRCX'],
}

@app.route('/api/heatmap/<group>')
def api_heatmap(group):
    if group not in ['sp500', 'nasdaq', 'crypto']:
        return jsonify({'error': 'Grupo inválido'}), 400

    if group == 'crypto':
        return api_heatmap_crypto()

    def fetch():
        syms   = HEAT_STOCKS[group]
        result = []
        for sym in syms:
            try:
                q = fh('/quote', {'symbol': sym})
                if not q or q.get('c', 0) == 0:
                    result.append({'sym': sym, 'chg': 0.0, 'price': 0})
                    continue
                price = round(float(q['c']), 2)
                prev  = round(float(q['pc']), 2)
                chg   = round((price - prev) / prev * 100, 2) if prev else 0
                result.append({'sym': sym.replace('.B','.B'), 'chg': chg, 'price': price})
            except:
                result.append({'sym': sym, 'chg': 0.0, 'price': 0})
        return result

    return jsonify(cached(f'heat_{group}', TTL['heat_stock'], fetch))

def api_heatmap_crypto():
    """CoinGecko para crypto — gratis sin API key"""
    def fetch():
        try:
            url = 'https://api.coingecko.com/api/v3/coins/markets'
            params = {
                'vs_currency': 'usd',
                'order': 'market_cap_desc',
                'per_page': 15,
                'page': 1,
                'price_change_percentage': '24h'
            }
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            return [
                {
                    'sym':   coin['symbol'].upper(),
                    'chg':   round(coin.get('price_change_percentage_24h') or 0, 2),
                    'price': coin.get('current_price', 0)
                }
                for coin in data
            ]
        except Exception as e:
            print(f'[CoinGecko] {e}')
            return []
    return jsonify(cached('heat_crypto', TTL['heat_crypto'], fetch))

# ─── CALENDARIO FOREX FACTORY ───
@app.route('/api/calendar')
def api_calendar():
    def fetch():
        try:
            today = date.today()
            url   = f"https://www.forexfactory.com/calendar?day={today.strftime('%m%d')}.{today.year}"
            hdrs  = {
                'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124',
                'Accept':          'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer':         'https://www.forexfactory.com/',
            }
            r = requests.get(url, headers=hdrs, timeout=15)
            if r.status_code != 200:
                raise Exception(f'HTTP {r.status_code}')

            soup   = BeautifulSoup(r.text, 'html.parser')
            rows   = soup.select('tr.calendar__row')
            events = []
            last_t = ''
            KEEP   = {'USD','EUR','GBP','JPY','CAD','AUD','CHF'}

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
                    ic  = ' '.join(ie.get('class', [])) if ie else ''
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
            return events or _fallback_cal()
        except Exception as e:
            print(f'[Calendar] {e}')
            return _fallback_cal()
    return jsonify(cached('calendar', TTL['calendar'], fetch))

def _fallback_cal():
    return [
        {'time':'8:30am','currency':'USD','impact':'High',  'event':'Initial Jobless Claims','actual':'','forecast':'225K','previous':'219K'},
        {'time':'10:00am','currency':'USD','impact':'High', 'event':'Fed Chair Powell Speech','actual':'','forecast':'',   'previous':''},
        {'time':'10:30am','currency':'USD','impact':'Medium','event':'Natural Gas Storage',   'actual':'','forecast':'-28B','previous':'-37B'},
    ]

# ─── NOTICIAS RSS ───
KEYWORDS = ['fed','federal reserve','trump','tariff','inflation','interest rate',
            'market','nasdaq','s&p','dow','bitcoin','crypto','dollar','powell',
            'economy','gdp','cpi','jobs','employment','china','recession','earnings',
            'rate cut','rate hike','wall street','stocks']

RSS_FEEDS = [
    ('Reuters Markets', 'https://feeds.reuters.com/reuters/businessNews'),
    ('AP Markets',      'https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US'),
    ('MarketWatch',     'https://feeds.marketwatch.com/marketwatch/topstories/'),
]

@app.route('/api/news')
def api_news():
    def fetch():
        items = []
        hdrs  = {'User-Agent': 'Mozilla/5.0 (compatible; EduSTrader/1.0)'}
        for src, url in RSS_FEEDS:
            try:
                r = requests.get(url, headers=hdrs, timeout=10)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.content, 'xml')
                for item in soup.find_all('item')[:15]:
                    title = item.find('title')
                    pub   = item.find('pubDate')
                    txt   = title.get_text(strip=True) if title else ''
                    if not txt or not any(kw in txt.lower() for kw in KEYWORDS):
                        continue
                    pub_str = pub.get_text(strip=True) if pub else ''
                    try:
                        pub_dt = datetime.strptime(pub_str[:25], '%a, %d %b %Y %H:%M:%S')
                    except:
                        pub_dt = datetime.now()
                    items.append({'title': txt, 'source': src, 'timestamp': pub_dt.isoformat()})
            except Exception as e:
                print(f'[RSS] {src}: {e}')
        items.sort(key=lambda x: x['timestamp'], reverse=True)
        return items[:25]
    return jsonify(cached('news', TTL['news'], fetch))

# ─── ARRANQUE ───
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'EduS Trader corriendo en puerto {port}')
    print(f'Finnhub key configurada: {FINNHUB_KEY != "TU_KEY_AQUI"}')
    app.run(host='0.0.0.0', port=port, debug=False)
