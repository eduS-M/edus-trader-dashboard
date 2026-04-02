"""
EduS Trader - Servidor Cloud v3
APIs:
  stooq.com   → VIX, índices, acciones  (sin key, funciona en cloud)
  CoinGecko   → Crypto                  (sin key, funciona en cloud)
  Forex Factory → Calendario            (scraping)
  RSS           → Noticias
"""

from flask import Flask, jsonify, send_file
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import os, io, csv
from datetime import datetime, date
import threading, time

app  = Flask(__name__)
CORS(app)

# ─── CACHE ───
_cache = {}
_lock  = threading.Lock()
TTL    = {'vix':60,'quotes':30,'heat_stock':120,'heat_crypto':180,'calendar':300,'news':120}

def cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and (time.time() - e['ts']) < ttl:
            return e['data']
    data = fn()
    with _lock:
        _cache[key] = {'data': data, 'ts': time.time()}
    return data

HDRS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

def stooq_quote(symbol):
    """
    Obtiene último precio de stooq.com
    Ejemplo: stooq_quote('^vix') → {'close':23.4, 'prev':22.1, ...}
    """
    url = f'https://stooq.com/q/d/l/?s={symbol}&i=d'
    r   = requests.get(url, headers=HDRS, timeout=12)
    r.raise_for_status()
    text = r.text.strip()
    if not text or 'No data' in text or 'Exceeded' in text:
        return None
    reader = csv.DictReader(io.StringIO(text))
    rows   = list(reader)
    if len(rows) < 2:
        return None
    last = rows[-1]   # hoy
    prev = rows[-2]   # ayer
    return {
        'close': float(last.get('Close', 0) or 0),
        'open':  float(last.get('Open',  0) or 0),
        'prev':  float(prev.get('Close', 0) or 0),
    }

def stooq_intraday(symbol):
    """Candles intradía de 5min via stooq"""
    url = f'https://stooq.com/q/d/l/?s={symbol}&i=5'
    r   = requests.get(url, headers=HDRS, timeout=15)
    r.raise_for_status()
    text = r.text.strip()
    if not text or 'No data' in text or 'Exceeded' in text:
        return []
    reader = csv.DictReader(io.StringIO(text))
    points = []
    for row in reader:
        try:
            dt  = row.get('Date','') + ' ' + row.get('Time','')
            dt  = dt.strip()
            t   = datetime.strptime(dt, '%Y-%m-%d %H:%M:%S') if ' ' in dt else datetime.strptime(dt, '%Y-%m-%d')
            points.append({'time': t.strftime('%H:%M'), 'close': round(float(row['Close']), 2)})
        except:
            continue
    return points[-80:]  # últimas 80 velas = ~6.5h

# ─── PÁGINA PRINCIPAL ───
@app.route('/')
def index():
    return send_file('index.html')

@app.route('/health')
def health():
    return jsonify({'status':'ok','api':'stooq+coingecko','version':'3'})

# ─── VIX ───
@app.route('/api/vix')
def api_vix():
    def fetch():
        try:
            points = stooq_intraday('^vix')
            q      = stooq_quote('^vix')
            if not q:
                return {'error': 'Sin datos de VIX'}

            current = q['close']
            prev    = q['prev']
            chg_pct = round((current - prev) / prev * 100, 2) if prev else 0

            # Si no hay intraday usa solo el punto actual
            if not points:
                points = [{'time': datetime.now().strftime('%H:%M'), 'close': current}]

            return {
                'current':    round(current, 2),
                'open':       round(prev, 2),
                'change_pct': chg_pct,
                'points':     points,
            }
        except Exception as e:
            print(f'[VIX] {e}')
            return {'error': str(e)}
    return jsonify(cached('vix', TTL['vix'], fetch))

# ─── ÍNDICES ───
# Símbolos stooq: https://stooq.com/t/?i=516
IDX = {
    'sp500':  '^spx',     # S&P 500
    'nasdaq': '^ndx',     # Nasdaq 100
    'dow':    '^dji',     # Dow Jones
    'bitcoin':'btc.v',    # Bitcoin (stooq)
    'eurusd': 'eurusd',   # EUR/USD
    'gold':   'xauusd',   # Oro en USD
}

@app.route('/api/indices')
def api_indices():
    def fetch():
        result = {}
        for name, sym in IDX.items():
            try:
                q = stooq_quote(sym)
                if not q or q['close'] == 0:
                    result[name] = {'error': True}
                    continue
                price = round(q['close'], 4)
                prev  = round(q['prev'],  4)
                chg   = round((price - prev) / prev * 100, 2) if prev else 0
                result[name] = {'price': price, 'change_pct': chg}
            except Exception as e:
                print(f'[IDX] {name}: {e}')
                result[name] = {'error': True}
        return result
    return jsonify(cached('quotes', TTL['quotes'], fetch))

# ─── HEATMAP ACCIONES ───
HEAT_STOCKS = {
    'sp500': [
        ('AAPL','aapl.us'),('MSFT','msft.us'),('NVDA','nvda.us'),('AMZN','amzn.us'),
        ('META','meta.us'),('GOOGL','googl.us'),('TSLA','tsla.us'),('JPM','jpm.us'),
        ('V','v.us'),('WMT','wmt.us'),('MA','ma.us'),('XOM','xom.us'),
        ('UNH','unh.us'),('LLY','lly.us'),('AVGO','avgo.us'),('HD','hd.us'),
        ('PG','pg.us'),('COST','cost.us'),('NFLX','nflx.us'),('CRM','crm.us'),
        ('ORCL','orcl.us'),('AMD','amd.us'),('BAC','bac.us'),('MRK','mrk.us'),
        ('CVX','cvx.us'),('KO','ko.us'),('ABBV','abbv.us'),('PEP','pep.us'),
        ('JNJ','jnj.us'),('BRK.B','brk_b.us'),
    ],
    'nasdaq': [
        ('QQQ','qqq.us'),('AAPL','aapl.us'),('MSFT','msft.us'),('NVDA','nvda.us'),
        ('AMZN','amzn.us'),('META','meta.us'),('GOOGL','googl.us'),('TSLA','tsla.us'),
        ('AVGO','avgo.us'),('NFLX','nflx.us'),('AMD','amd.us'),('COST','cost.us'),
        ('ADBE','adbe.us'),('QCOM','qcom.us'),('TXN','txn.us'),('PANW','panw.us'),
        ('MU','mu.us'),('KLAC','klac.us'),('MRVL','mrvl.us'),('LRCX','lrcx.us'),
    ],
}

@app.route('/api/heatmap/<group>')
def api_heatmap(group):
    if group == 'crypto':
        def fetch_c():
            try:
                r = requests.get(
                    'https://api.coingecko.com/api/v3/coins/markets',
                    params={'vs_currency':'usd','order':'market_cap_desc',
                            'per_page':15,'page':1,'price_change_percentage':'24h'},
                    timeout=12)
                r.raise_for_status()
                return [{'sym':c['symbol'].upper(),
                         'chg':round(c.get('price_change_percentage_24h') or 0,2),
                         'price':c.get('current_price',0)} for c in r.json()]
            except Exception as e:
                print(f'[Crypto] {e}')
                return []
        return jsonify(cached('heat_crypto', TTL['heat_crypto'], fetch_c))

    if group not in HEAT_STOCKS:
        return jsonify({'error':'Grupo inválido'}), 400

    def fetch_s():
        result = []
        for label, sym in HEAT_STOCKS[group]:
            try:
                q = stooq_quote(sym)
                if not q or q['close'] == 0:
                    result.append({'sym':label,'chg':0.0,'price':0})
                    continue
                price = round(q['close'], 2)
                prev  = round(q['prev'],  2)
                chg   = round((price - prev) / prev * 100, 2) if prev else 0
                result.append({'sym':label,'chg':chg,'price':price})
                time.sleep(0.15)   # respetar rate limit de stooq
            except Exception as e:
                print(f'[Heat] {label}: {e}')
                result.append({'sym':label,'chg':0.0,'price':0})
        return result
    return jsonify(cached(f'heat_{group}', TTL['heat_stock'], fetch_s))

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
                    ic  = ' '.join(ie.get('class',[]) if ie else [])
                    imp = 'High' if 'red' in ic else 'Medium' if 'orange' in ic else 'Low'
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
        {'time':'10:30am','currency':'USD','impact':'Medium','event':'Natural Gas Storage',  'actual':'','forecast':'-28B','previous':'-37B'},
    ]

# ─── NOTICIAS RSS ───
KEYWORDS = ['fed','federal reserve','trump','tariff','inflation','interest rate',
            'market','nasdaq','s&p','dow','bitcoin','crypto','dollar','powell',
            'economy','gdp','cpi','jobs','employment','china','recession','earnings']

RSS = [
    ('Reuters Markets','https://feeds.reuters.com/reuters/businessNews'),
    ('AP Markets',     'https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US'),
    ('MarketWatch',    'https://feeds.marketwatch.com/marketwatch/topstories/'),
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
    print(f'EduS Trader v3 — puerto {port}')
    app.run(host='0.0.0.0',port=port,debug=False)
