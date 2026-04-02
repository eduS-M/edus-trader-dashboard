"""
EduS Trader - Servidor Cloud v5
============================================================
APIs:
  Tradier Sandbox  → Quotes, Options chain (gratis, sin límite agresivo)
  CoinGecko        → Crypto (gratis, sin key)
  CBOE / Stooq     → VIX fallback chain
  Forex Factory    → Calendario (scraping)
  RSS              → Noticias
============================================================
Tradier Sandbox token (público, solo lectura):
  QDuFXmPGBHxURerTzFBGzBKzXS72
No requiere registro para el sandbox.
Para producción (datos en tiempo real), regístrate en tradier.com/create.
============================================================
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

# ─── TRADIER CONFIG ───
# Sandbox = datos delayed ~15min, gratis, sin registro
TRADIER_TOKEN   = os.environ.get('TRADIER_TOKEN', 'QDuFXmPGBHxURerTzFBGzBKzXS72')
TRADIER_BASE    = 'https://sandbox.tradier.com/v1'
TRADIER_HEADERS = {
    'Authorization': f'Bearer {TRADIER_TOKEN}',
    'Accept':        'application/json',
    'User-Agent':    'EduSTrader/5.0',
}

# ─── CACHE ───
_cache = {}
_lock  = threading.Lock()
TTL    = {
    'vix':120, 'quotes':60, 'heat_stock':300,
    'heat_crypto':120, 'calendar':300, 'news':120,
    'gex_spx':600, 'gex_ndx':600,
}

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

# ─── TRADIER HELPERS ───
def tradier_quote(symbols):
    """
    Obtiene quotes de múltiples símbolos en una sola llamada.
    symbols: lista de strings  e.g. ['SPY','QQQ','VXX']
    Retorna dict {sym: {price, prev_close, chg_pct}}
    """
    try:
        syms = ','.join(symbols) if isinstance(symbols, list) else symbols
        r = requests.get(
            f'{TRADIER_BASE}/markets/quotes',
            headers=TRADIER_HEADERS,
            params={'symbols': syms, 'greeks': 'false'},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        quotes = data.get('quotes', {}).get('quote', [])
        if isinstance(quotes, dict):   # single quote viene como dict, no lista
            quotes = [quotes]
        result = {}
        for q in quotes:
            sym   = q.get('symbol','')
            price = float(q.get('last') or q.get('close') or 0)
            prev  = float(q.get('prevclose') or price)
            chg   = round((price - prev) / prev * 100, 2) if prev else 0
            result[sym] = {'price': round(price,4), 'prev': round(prev,4), 'chg_pct': chg}
        return result
    except Exception as e:
        print(f'[Tradier quote] {e}')
        return {}

def tradier_timesales(symbol, interval='5min', start=None):
    """Velas intradía de Tradier"""
    try:
        if not start:
            start = datetime.now().strftime('%Y-%m-%d 09:30')
        r = requests.get(
            f'{TRADIER_BASE}/markets/timesales',
            headers=TRADIER_HEADERS,
            params={'symbol': symbol, 'interval': interval, 'start': start, 'session_filter': 'open'},
            timeout=15,
        )
        r.raise_for_status()
        data   = r.json()
        series = data.get('series', {})
        if not series:
            return []
        points = series.get('data', [])
        if isinstance(points, dict):
            points = [points]
        return [
            {'time': p['time'][11:16], 'close': round(float(p['close']),2)}
            for p in points if p.get('close')
        ]
    except Exception as e:
        print(f'[Tradier timesales] {e}')
        return []

def tradier_options_chain(symbol, expiration=None):
    """Cadena de opciones completa para un símbolo y vencimiento"""
    try:
        params = {'symbol': symbol, 'greeks': 'true'}
        if expiration:
            params['expiration'] = expiration
        r = requests.get(
            f'{TRADIER_BASE}/markets/options/chains',
            headers=TRADIER_HEADERS,
            params=params,
            timeout=20,
        )
        r.raise_for_status()
        data    = r.json()
        options = data.get('options', {}).get('option', [])
        if not options:
            return []
        if isinstance(options, dict):
            options = [options]
        return options
    except Exception as e:
        print(f'[Tradier options] {e}')
        return []

def tradier_expirations(symbol):
    """Lista de fechas de vencimiento disponibles"""
    try:
        r = requests.get(
            f'{TRADIER_BASE}/markets/options/expirations',
            headers=TRADIER_HEADERS,
            params={'symbol': symbol, 'includeAllRoots': 'true'},
            timeout=12,
        )
        r.raise_for_status()
        dates = r.json().get('expirations', {}).get('date', [])
        return dates if isinstance(dates, list) else [dates]
    except Exception as e:
        print(f'[Tradier expirations] {e}')
        return []

# ─── PÁGINA PRINCIPAL ───
@app.route('/')
def index():
    return send_file('index.html')

@app.route('/health')
def health():
    # Test rápido de conexión a Tradier
    try:
        r = requests.get(f'{TRADIER_BASE}/markets/quotes',
            headers=TRADIER_HEADERS, params={'symbols':'SPY'}, timeout=8)
        tradier_ok = r.status_code == 200
    except:
        tradier_ok = False
    return jsonify({'status':'ok','version':'5','tradier_ok': tradier_ok})

# ─── VIX ───
@app.route('/api/vix')
def api_vix():
    def fetch():
        # VIX via Tradier (VIXY = ETF del VIX, correlación >0.95)
        # También pedimos VXX para referencia
        quotes = tradier_quote(['VIXY','VXX','UVXY'])
        vix_proxy = quotes.get('VIXY') or quotes.get('VXX')

        # Intentar también datos VIX reales desde CBOE public JSON
        cboe_price = None
        try:
            r = requests.get(
                'https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json',
                headers={**HDRS, 'Referer':'https://www.cboe.com/'}, timeout=10)
            if r.status_code == 200:
                rows = r.json().get('data', [])
                if rows:
                    # rows: [date, open, high, low, close, volume]
                    last  = rows[-1]
                    prev  = rows[-2] if len(rows) > 1 else last
                    cboe_price = {
                        'price': round(float(last[4]), 2),
                        'prev':  round(float(prev[4]), 2),
                        'chg_pct': round((float(last[4])-float(prev[4]))/float(prev[4])*100, 2),
                    }
                    # Intraday desde CBOE (datos del día actual)
                    pts_r = requests.get(
                        'https://cdn.cboe.com/api/global/delayed_quotes/charts/intraday/_VIX.json',
                        headers={**HDRS,'Referer':'https://www.cboe.com/'}, timeout=10)
                    pts = []
                    if pts_r.status_code == 200:
                        idata = pts_r.json().get('data', [])
                        pts = [{'time': row[0][11:16], 'close': round(float(row[1]),2)}
                               for row in idata if len(row) >= 2]
                    return {
                        'current':    cboe_price['price'],
                        'open':       cboe_price['prev'],
                        'change_pct': cboe_price['chg_pct'],
                        'points':     pts or [{'time':datetime.now().strftime('%H:%M'),'close':cboe_price['price']}],
                        'source':     'VIX (CBOE)',
                    }
        except Exception as e:
            print(f'[VIX CBOE] {e}')

        # Fallback: VIXY con candles intradía
        if vix_proxy and vix_proxy['price'] > 0:
            points = tradier_timesales('VIXY')
            if not points:
                points = [{'time':datetime.now().strftime('%H:%M'),'close':vix_proxy['price']}]
            return {
                'current':    vix_proxy['price'],
                'open':       vix_proxy['prev'],
                'change_pct': vix_proxy['chg_pct'],
                'points':     points,
                'source':     'VIXY (proxy VIX)',
            }

        return {'error': 'VIX no disponible en este momento'}
    return jsonify(cached('vix', TTL['vix'], fetch))

# ─── ÍNDICES ───
IDX_SYMS = {
    'sp500':  'SPY',
    'nasdaq': 'QQQ',
    'dow':    'DIA',
    'gold':   'GLD',
}

@app.route('/api/indices')
def api_indices():
    def fetch():
        result = {}
        # Acciones/ETFs via Tradier (una sola llamada)
        stock_syms = list(IDX_SYMS.values())
        quotes     = tradier_quote(stock_syms)
        for name, sym in IDX_SYMS.items():
            q = quotes.get(sym)
            if q and q['price'] > 0:
                result[name] = {'price': q['price'], 'change_pct': q['chg_pct']}
            else:
                result[name] = {'error': True}

        # Bitcoin + Ethereum via CoinGecko (no gasta cuota)
        try:
            r = requests.get(
                'https://api.coingecko.com/api/v3/simple/price',
                params={'ids':'bitcoin,ethereum','vs_currencies':'usd','include_24hr_change':'true'},
                timeout=10)
            cg = r.json()
            result['bitcoin'] = {
                'price':      cg['bitcoin']['usd'],
                'change_pct': round(cg['bitcoin'].get('usd_24h_change',0), 2),
            }
        except:
            result['bitcoin'] = {'error': True}

        # EUR/USD via Tradier Forex
        try:
            q = tradier_quote(['EUR/USD'])
            fx = q.get('EUR/USD')
            if fx and fx['price'] > 0:
                result['eurusd'] = {'price': fx['price'], 'change_pct': fx['chg_pct']}
            else:
                # Fallback: exchangerate-api (gratis, sin key)
                r = requests.get('https://open.er-api.com/v6/latest/EUR', timeout=8)
                d = r.json()
                usd = d['rates']['USD']
                result['eurusd'] = {'price': round(usd,4), 'change_pct': 0.0}
        except Exception as e:
            print(f'[EURUSD] {e}')
            result['eurusd'] = {'error': True}

        return result
    return jsonify(cached('quotes', TTL['quotes'], fetch))

# ─── HEATMAP ACCIONES ───
HEAT_SYMS = {
    'sp500': ['AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','JPM','V',
              'WMT','MA','XOM','UNH','LLY','AVGO','HD','PG','COST','NFLX','CRM',
              'ORCL','AMD','BAC','MRK','CVX','KO','ABBV','PEP','JNJ','BRK/B'],
    'nasdaq':['QQQ','AAPL','MSFT','NVDA','AMZN','META','GOOGL','TSLA','AVGO','NFLX',
              'AMD','COST','ADBE','QCOM','TXN','PANW','MU','KLAC','MRVL','LRCX'],
}

@app.route('/api/heatmap/<group>')
def api_heatmap(group):
    if group == 'crypto':
        def fetch_c():
            try:
                r = requests.get(
                    'https://api.coingecko.com/api/v3/coins/markets',
                    params={'vs_currency':'usd','order':'market_cap_desc','per_page':18,
                            'page':1,'price_change_percentage':'24h'}, timeout=12)
                r.raise_for_status()
                STABLE = {'USDT','USDC','USDS','FDUSD','TUSD','BUSD','DAI'}
                return [
                    {'sym':c['symbol'].upper(),'chg':round(c.get('price_change_percentage_24h') or 0,2),'price':c.get('current_price',0)}
                    for c in r.json() if c['symbol'].upper() not in STABLE
                ][:15]
            except Exception as e:
                print(f'[Crypto] {e}')
                return []
        return jsonify(cached('heat_crypto', TTL['heat_crypto'], fetch_c))

    if group not in HEAT_SYMS:
        return jsonify({'error':'Grupo inválido'}), 400

    def fetch_s():
        syms   = HEAT_SYMS[group]
        # Tradier acepta hasta 200 símbolos en una sola llamada ✅
        quotes = tradier_quote(syms)
        result = []
        for sym in syms:
            q = quotes.get(sym)
            if q and q['price'] > 0:
                result.append({'sym': sym.replace('/',''), 'chg': q['chg_pct'], 'price': q['price']})
            else:
                result.append({'sym': sym.replace('/',''), 'chg': 0.0, 'price': 0})
        return result

    return jsonify(cached(f'heat_{group}', TTL['heat_stock'], fetch_s))

# ─── GEX AVANZADO: GEX + DELTA + VANNA + por expiración ───
def bs_gamma(S, K, T, r, sigma):
    """Gamma Black-Scholes"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1  = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
        pdf = math.exp(-0.5*d1**2) / math.sqrt(2*math.pi)
        return pdf / (S * sigma * math.sqrt(T))
    except:
        return 0.0

def bs_delta(S, K, T, r, sigma, opt_type='call'):
    """Delta Black-Scholes"""
    if T <= 0 or sigma <= 0:
        return 0.0
    try:
        d1  = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
        from math import erf
        N   = lambda x: 0.5*(1+erf(x/math.sqrt(2)))
        return N(d1) if opt_type=='call' else N(d1)-1
    except:
        return 0.0

def bs_vanna(S, K, T, r, sigma):
    """Vanna = dDelta/dVol = -d2 * gamma / sigma"""
    if T <= 0 or sigma <= 0:
        return 0.0
    try:
        d1   = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
        d2   = d1 - sigma*math.sqrt(T)
        pdf  = math.exp(-0.5*d1**2) / math.sqrt(2*math.pi)
        return -(d2/sigma) * pdf / (S*sigma*math.sqrt(T))
    except:
        return 0.0

def classify_expiry(exp_str, today):
    """Clasifica expiración: 0DTE, weekly, monthly"""
    try:
        exp = datetime.strptime(exp_str, '%Y-%m-%d').date()
        days = (exp - today).days
        if days == 0:
            return '0DTE'
        elif days <= 7:
            return 'weekly'
        elif days <= 35:
            return 'monthly'
        else:
            return 'leaps'
    except:
        return 'monthly'

def compute_gex_advanced(symbol='SPX', etf_symbol='SPY'):
    """
    Calcula GEX + Delta Exposure + Vanna Exposure por strike y por expiración.
    Usa Tradier para la cadena de opciones.
    """
    today = date.today()
    try:
        # 1. Spot price
        quote = tradier_quote([etf_symbol])
        q     = quote.get(etf_symbol)
        if not q or q['price'] <= 0:
            return {'error': f'No se pudo obtener precio de {etf_symbol}'}
        spot = q['price']

        # Multiplicador: SPX es 10x SPY aproximadamente
        mult = 10.0 if symbol == 'SPX' else 1.0
        spot_adj = spot * mult  # precio ajustado del índice

        # 2. Fechas de vencimiento (tomar las próximas 6)
        exps = tradier_expirations(etf_symbol)
        if not exps:
            return {'error': 'No hay fechas de vencimiento disponibles'}
        exps = exps[:6]   # 0DTE, weekly, mensual, etc.

        # 3. Procesar cada vencimiento
        r_rate   = 0.05
        results_by_exp  = {}
        aggregate_gex   = {}   # strike → GEX neto total
        aggregate_dex   = {}   # strike → Delta Exposure total
        aggregate_vanna = {}   # strike → Vanna Exposure total

        for exp in exps:
            exp_class = classify_expiry(exp, today)
            exp_dt    = datetime.strptime(exp, '%Y-%m-%d').date()
            T         = max((exp_dt - today).days / 365, 1/365)

            chain = tradier_options_chain(etf_symbol, exp)
            if not chain:
                continue

            exp_gex   = {}
            exp_dex   = {}
            exp_vanna = {}

            for opt in chain:
                try:
                    strike = float(opt.get('strike', 0))
                    if strike <= 0:
                        continue
                    oi       = int(opt.get('open_interest', 0) or 0)
                    iv       = float(opt.get('greeks',{}).get('smv_vol', 0) or
                                     opt.get('iv', 0) or 0)
                    opt_type = opt.get('option_type','').lower()   # 'call' o 'put'

                    # Usar IV de Tradier greeks si está disponible
                    greeks   = opt.get('greeks') or {}
                    iv_g     = float(greeks.get('smv_vol', 0) or 0)
                    if iv_g > 0:
                        iv = iv_g
                    if iv <= 0.01 or oi == 0:
                        continue

                    # Cálculo en términos del ETF subyacente
                    S  = spot
                    K  = strike

                    gamma = bs_gamma(S, K, T, r_rate, iv)
                    delta = bs_delta(S, K, T, r_rate, iv, opt_type)
                    vanna = bs_vanna(S, K, T, r_rate, iv)

                    # GEX = gamma * OI * 100 * S² * 0.01  (en $ por 1% move)
                    gex_val   = gamma * oi * 100 * S * S * 0.01
                    dex_val   = delta * oi * 100 * S
                    vanna_val = vanna * oi * 100 * S * iv

                    # Calls: dealers son short gamma si compran put → neto +gamma
                    # Puts:  dealers son long gamma → neto -gamma
                    sign = 1 if opt_type == 'call' else -1

                    exp_gex[strike]   = exp_gex.get(strike, 0)   + sign * gex_val
                    exp_dex[strike]   = exp_dex.get(strike, 0)   + sign * dex_val
                    exp_vanna[strike] = exp_vanna.get(strike, 0) + vanna_val

                    aggregate_gex[strike]   = aggregate_gex.get(strike, 0)   + sign * gex_val
                    aggregate_dex[strike]   = aggregate_dex.get(strike, 0)   + sign * dex_val
                    aggregate_vanna[strike] = aggregate_vanna.get(strike, 0) + vanna_val

                except Exception as e:
                    continue

            results_by_exp[exp] = {
                'class':  exp_class,
                'days':   (datetime.strptime(exp,'%Y-%m-%d').date()-today).days,
                'strikes':sorted(exp_gex.keys()),
                'gex':    [round(exp_gex[k]/1e9, 4) for k in sorted(exp_gex)],
                'dex':    [round(exp_dex[k]/1e6, 2) for k in sorted(exp_dex)],
                'vanna':  [round(exp_vanna[k]/1e6, 2) for k in sorted(exp_vanna)],
            }

        if not aggregate_gex:
            return {'error': 'Sin datos de opciones — mercado puede estar cerrado'}

        # 4. Filtrar strikes ±8% del spot
        lo = spot * 0.92
        hi = spot * 1.08
        strikes_f = sorted([k for k in aggregate_gex if lo <= k <= hi])

        gex_f   = [round(aggregate_gex.get(k,0)/1e9, 4) for k in strikes_f]
        dex_f   = [round(aggregate_dex.get(k,0)/1e6, 2) for k in strikes_f]
        vanna_f = [round(aggregate_vanna.get(k,0)/1e6, 2) for k in strikes_f]

        # 5. Niveles clave
        pos_gex = {k:v for k,v in aggregate_gex.items() if v > 0}
        neg_gex = {k:v for k,v in aggregate_gex.items() if v < 0}
        call_wall  = max(pos_gex, key=pos_gex.get) if pos_gex else spot
        put_wall   = min(neg_gex, key=neg_gex.get) if neg_gex else spot

        # Zero gamma: strike más cercano donde GEX cruza cero
        zero_gamma = spot
        sk_sorted  = sorted(aggregate_gex.keys())
        for i in range(len(sk_sorted)-1):
            a, b = sk_sorted[i], sk_sorted[i+1]
            if aggregate_gex.get(a,0) * aggregate_gex.get(b,0) < 0:
                zero_gamma = a
                break

        total_gex = round(sum(gex_f), 2)

        return {
            'spot':          round(spot, 2),
            'symbol':        symbol,
            'etf':           etf_symbol,
            'strikes':       strikes_f,
            'gex':           gex_f,
            'dex':           dex_f,
            'vanna':         vanna_f,
            'call_wall':     round(call_wall, 2),
            'put_wall':      round(put_wall, 2),
            'zero_gamma':    round(zero_gamma, 2),
            'total_gex':     total_gex,
            'by_expiration': results_by_exp,
            'expirations':   exps,
        }

    except Exception as e:
        print(f'[GEX] {e}')
        return {'error': str(e)}

@app.route('/api/gex/<symbol>')
def api_gex(symbol):
    sym = symbol.upper()
    cfg = {'SPX': 'SPY', 'NDX': 'QQQ'}
    if sym not in cfg:
        return jsonify({'error': 'Solo SPX o NDX'}), 400
    etf = cfg[sym]
    return jsonify(cached(f'gex_{sym}', TTL[f'gex_{sym}'], lambda: compute_gex_advanced(sym, etf)))

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
            soup   = BeautifulSoup(r.text, 'html.parser')
            rows   = soup.select('tr.calendar__row')
            events = []
            last_t = ''
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
                    if imp == 'Low' and ccy not in ('USD','EUR'):
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
        {'time':'8:30am','currency':'USD','impact':'High',   'event':'Initial Jobless Claims','actual':'','forecast':'225K','previous':'219K'},
        {'time':'10:00am','currency':'USD','impact':'High',  'event':'Fed Chair Powell Speech','actual':'','forecast':'',   'previous':''},
        {'time':'2:00pm', 'currency':'EUR','impact':'High',  'event':'ECB President Speech',  'actual':'','forecast':'',   'previous':''},
        {'time':'10:30am','currency':'USD','impact':'Medium','event':'Natural Gas Storage',    'actual':'','forecast':'-28B','previous':'-37B'},
    ]

# ─── NOTICIAS RSS ───
KEYWORDS = [
    'fed','federal reserve','trump','tariff','inflation','interest rate','market',
    'nasdaq','s&p','dow','bitcoin','crypto','dollar','powell','economy','gdp','cpi',
    'jobs','employment','china','recession','earnings','rate cut','rate hike',
    'wall street','stocks','eur','ecb','bank of england','boe','options','vix',
    'volatility','gamma','hedge','short squeeze',
]
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
                    items.append({'title':txt,'source':src,'timestamp':pub_dt.isoformat()})
            except Exception as e:
                print(f'[RSS] {src}: {e}')
        items.sort(key=lambda x: x['timestamp'], reverse=True)
        return items[:25]
    return jsonify(cached('news', TTL['news'], fetch))

# ─── ARRANQUE ───
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'EduS Trader v5 — puerto {port}')
    app.run(host='0.0.0.0', port=port, debug=False)
