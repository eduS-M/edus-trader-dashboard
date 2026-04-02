"""
EduS Trader - Servidor Cloud (Railway)
"""

from flask import Flask, jsonify, send_file
from flask_cors import CORS
import yfinance as yf
import requests
from bs4 import BeautifulSoup
import os
from datetime import datetime, date
import threading
import time

app = Flask(__name__)
CORS(app)  # Permite que Blogger pueda llamar a este servidor

# ─── CACHE (evita sobrecargar las APIs) ───
_cache = {}
_lock  = threading.Lock()
TTL    = {'vix':60, 'quotes':30, 'heatmap':120, 'calendar':300, 'news':120}

def cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and (time.time() - e['ts']) < ttl:
            return e['data']
    data = fn()
    with _lock:
        _cache[key] = {'data': data, 'ts': time.time()}
    return data

# ─── PÁGINA PRINCIPAL ───
@app.route('/')
def index():
    return send_file('index.html')

# ─── VIX ───
@app.route('/api/vix')
def api_vix():
    def fetch():
        try:
            t   = yf.Ticker('^VIX')
            hist = t.history(period='1d', interval='5m')
            info = t.fast_info
            if hist.empty:
                return {'error': 'Sin datos de VIX'}
            points = [
                {'time': ts.strftime('%H:%M'), 'close': round(float(row['Close']), 2)}
                for ts, row in hist.iterrows()
            ]
            current   = round(float(info.last_price), 2)
            prev      = round(float(info.previous_close), 2)
            chg_pct   = round((current - prev) / prev * 100, 2)
            return {'current': current, 'open': prev, 'change_pct': chg_pct, 'points': points}
        except Exception as e:
            return {'error': str(e)}
    return jsonify(cached('vix', TTL['vix'], fetch))

# ─── ÍNDICES ───
IDX = {
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
        try:
            tickers = yf.Tickers(' '.join(IDX.values()))
            for name, sym in IDX.items():
                try:
                    info  = tickers.tickers[sym].fast_info
                    price = round(float(info.last_price), 2)
                    prev  = round(float(info.previous_close), 2)
                    chg   = round((price - prev) / prev * 100, 2)
                    result[name] = {'price': price, 'change_pct': chg}
                except:
                    result[name] = {'error': True}
        except Exception as e:
            return {'error': str(e)}
        return result
    return jsonify(cached('quotes', TTL['quotes'], fetch))

# ─── HEATMAPS ───
HEAT_SYMS = {
    'sp500':  ['AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','JPM','V','WMT',
               'MA','XOM','UNH','LLY','AVGO','HD','PG','COST','NFLX','CRM',
               'ORCL','AMD','BAC','MRK','CVX','KO','ABBV','PEP','BRK-B','JNJ'],
    'nasdaq': ['QQQ','AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','AVGO','NFLX',
               'AMD','COST','ADBE','QCOM','TXN','PANW','MU','KLAC','MRVL','LRCX'],
    'crypto': ['BTC-USD','ETH-USD','BNB-USD','SOL-USD','XRP-USD','DOGE-USD',
               'ADA-USD','AVAX-USD','LINK-USD','DOT-USD','SHIB-USD','MATIC-USD'],
}

@app.route('/api/heatmap/<group>')
def api_heatmap(group):
    if group not in HEAT_SYMS:
        return jsonify({'error': 'Grupo inválido'}), 400
    def fetch():
        syms = HEAT_SYMS[group]
        result = []
        try:
            tickers = yf.Tickers(' '.join(syms))
            for sym in syms:
                try:
                    info  = tickers.tickers[sym].fast_info
                    price = round(float(info.last_price), 2)
                    prev  = round(float(info.previous_close), 2)
                    chg   = round((price - prev) / prev * 100, 2)
                    label = sym.replace('-USD','').replace('^','')
                    result.append({'sym': label, 'chg': chg, 'price': price})
                except:
                    result.append({'sym': sym.replace('-USD',''), 'chg': 0.0, 'price': 0})
        except Exception as e:
            return {'error': str(e)}
        return result
    return jsonify(cached(f'heatmap_{group}', TTL['heatmap'], fetch))

# ─── CALENDARIO FOREX FACTORY ───
@app.route('/api/calendar')
def api_calendar():
    def fetch():
        try:
            today = date.today()
            url   = f"https://www.forexfactory.com/calendar?day={today.strftime('%m%d')}.{today.year}"
            hdrs  = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.forexfactory.com/',
            }
            r = requests.get(url, headers=hdrs, timeout=12)
            if r.status_code != 200:
                raise Exception(f'HTTP {r.status_code}')
            soup     = BeautifulSoup(r.text, 'html.parser')
            rows     = soup.select('tr.calendar__row')
            events   = []
            last_t   = ''
            KEEP_CCY = {'USD','EUR','GBP','JPY','CAD','AUD','CHF'}
            for row in rows:
                try:
                    te = row.select_one('.calendar__time')
                    t  = te.get_text(strip=True) if te else ''
                    if t and t not in ('All Day','Tentative',''):
                        last_t = t
                    ce  = row.select_one('.calendar__currency')
                    ccy = ce.get_text(strip=True) if ce else ''
                    if ccy not in KEEP_CCY:
                        continue
                    ie  = row.select_one('.calendar__impact span')
                    ic  = ' '.join(ie.get('class',[])) if ie else ''
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
            'economy','gdp','cpi','jobs','employment','china','recession','earnings']

RSS_FEEDS = [
    ('Reuters Markets', 'https://feeds.reuters.com/reuters/businessNews'),
    ('AP Markets',      'https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US'),
    ('MarketWatch',     'https://feeds.marketwatch.com/marketwatch/topstories/'),
]

@app.route('/api/news')
def api_news():
    def fetch():
        items = []
        hdrs  = {'User-Agent': 'Mozilla/5.0'}
        for src, url in RSS_FEEDS:
            try:
                r = requests.get(url, headers=hdrs, timeout=8)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.content, 'xml')
                for item in soup.find_all('item')[:15]:
                    title = item.find('title')
                    pub   = item.find('pubDate')
                    title_txt = title.get_text(strip=True) if title else ''
                    if not title_txt:
                        continue
                    if not any(kw in title_txt.lower() for kw in KEYWORDS):
                        continue
                    pub_str = pub.get_text(strip=True) if pub else ''
                    try:
                        pub_dt = datetime.strptime(pub_str[:25], '%a, %d %b %Y %H:%M:%S')
                    except:
                        pub_dt = datetime.now()
                    items.append({'title': title_txt, 'source': src, 'timestamp': pub_dt.isoformat()})
            except Exception as e:
                print(f'[RSS] {src}: {e}')
        items.sort(key=lambda x: x['timestamp'], reverse=True)
        return items[:25]
    return jsonify(cached('news', TTL['news'], fetch))

# ─── ARRANQUE ───
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))  # Railway inyecta PORT automáticamente
    print(f'EduS Trader corriendo en puerto {port}')
    app.run(host='0.0.0.0', port=port, debug=False)
