#!/usr/bin/env python3
"""Сканер сетапов «4H Тренд -> 1H Откат» (см. TRADING_STRATEGY.md).

Инструменты: BTC (безубыток на +1.5R), GOLD (безубыток), SILVER (стоп фиксированный).
Индексы исключены: бэктест 2024-2026 показал PF 1.01-1.04 (нет преимущества).

Запуск: python3 trade_scanner.py [--quiet]
Выход: отчёт в stdout, лог в signals.log, macOS-уведомление при активном сетапе.
"""
import json, ssl, sys, subprocess, urllib.request
import datetime as dt
from pathlib import Path

ASSETS = {
    # имя: (тикер, круглосуточно, безубыток на +1.5R)
    # металлы: спот xyz:* с Hyperliquid (совпадает с XAU/XAG на пропе), НЕ фьючерсы COMEX
    'BTC':    ('BTC',        True,  True),   # данные с Hyperliquid (Yahoo 429)
    'GOLD':   ('xyz:GOLD',   False, True),
    'SILVER': ('xyz:SILVER', False, False),
}
ATR_SL, RR, SESSION_UTC = 2.0, 3.0, (12, 20)

# Модуль дейтрейдинга NASDAQ: mean reversion по RSI(2) с фильтром EMA200(1h).
# Бэктест 2024-2026: 119 сделок, win 65%, PF 1.44 (1.54/1.35 по половинам выборки), DD -3.6R.
# S&P 500 те же правила НЕ проходят (PF 0.92) - торгуем только NQ. Край тонкий - риск 0.15-0.2%.
NQ_SYMBOL = 'NQ=F'
NQ_RSI_LO, NQ_RSI_HI, NQ_EXIT_MID, NQ_ATR_SL = 10, 90, 50, 2.0
NQ_ENTRY_HOURS_CT = range(9, 14)   # входы 9:00-13:59 CT, выход не позже закрытия бара 14:00 CT
LOG = Path(__file__).with_name('signals.log')
TG_CONFIG = Path(__file__).with_name('telegram_config.json')
STATE = Path(__file__).with_name('scanner_state.json')  # дедупликация уже отправленных сигналов
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
CTX = ssl._create_unverified_context()


def fetch_bars(symbol, days=150, interval='1h', bar_sec=3600):
    p2 = int(dt.datetime.now().timestamp())
    p1 = p2 - days * 86400
    url = (f'https://query1.finance.yahoo.com/v8/finance/chart/'
           f'{urllib.request.quote(symbol)}?period1={p1}&period2={p2}&interval={interval}')
    req = urllib.request.Request(url, headers=UA)
    d = json.load(urllib.request.urlopen(req, timeout=30, context=CTX))
    res = d['chart']['result'][0]
    q = res['indicators']['quote'][0]
    bars = [(t, o, h, l, c) for t, o, h, l, c in
            zip(res['timestamp'], q['open'], q['high'], q['low'], q['close'])
            if None not in (o, h, l, c)]
    # последний бар может быть незакрытым - отбрасываем, работаем по закрытым
    now = dt.datetime.now().timestamp()
    if bars and now - bars[-1][0] < bar_sec:
        bars = bars[:-1]
    return bars


def fetch_1h(symbol, days=150):
    return fetch_bars(symbol, days, '1h', 3600)


def fetch_hl_bars(coin, interval='1h', days=150, bar_sec=3600):
    """Свечи с Hyperliquid (HYPE, спот-металлы xyz:*), только закрытые."""
    now_ms = int(dt.datetime.now().timestamp() * 1000)
    body = json.dumps({'type': 'candleSnapshot',
                       'req': {'coin': coin, 'interval': interval,
                               'startTime': now_ms - days * 86400000, 'endTime': now_ms}}).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info', data=body,
                                 headers={'Content-Type': 'application/json'})
    d = json.load(urllib.request.urlopen(req, timeout=30, context=CTX))
    bars = sorted((c['t'] // 1000, float(c['o']), float(c['h']), float(c['l']), float(c['c']))
                  for c in d)
    cutoff = dt.datetime.now().timestamp() - bar_sec
    return [b for b in bars if b[0] <= cutoff]


HL_COINS = {'BTC', 'HYPE', 'ETH', 'SOL', 'ZEC', 'BNB'}


def fetch_any(symbol, days=150, interval='1h', bar_sec=3600):
    if symbol.startswith('xyz:') or symbol in HL_COINS:
        return fetch_hl_bars(symbol, interval, days, bar_sec)
    return fetch_bars(symbol, days, interval, bar_sec)


def ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def atr(bars, n=14):
    out, prev_c, a = [], None, None
    for _, o, h, l, c in bars:
        tr = h - l if prev_c is None else max(h - l, abs(h - prev_c), abs(l - prev_c))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
        prev_c = c
    return out


def trend_4h(bars):
    """Тренд по закрытым 4h барам: +1 лонг / -1 шорт / 0 вне рынка."""
    c4 = [bars[i:i + 4][-1][4] for i in range(0, len(bars) // 4 * 4, 4)]
    if len(c4) < 210:
        return 0, None, None
    e50, e200 = ema(c4, 50), ema(c4, 200)
    if e50[-1] > e200[-1] and c4[-1] > e50[-1]:
        return 1, e50[-1], e200[-1]
    if e50[-1] < e200[-1] and c4[-1] < e50[-1]:
        return -1, e50[-1], e200[-1]
    return 0, e50[-1], e200[-1]


def scan(name, symbol, always_open, use_be):
    bars = fetch_any(symbol, days=150, interval='1h', bar_sec=3600)
    if len(bars) < 900:
        return {'name': name, 'status': 'нет данных'}
    tr, e50, e200 = trend_4h(bars)
    closes = [b[4] for b in bars]
    e20 = ema(closes, 20)[-1]
    a = atr(bars)[-1]
    t, o, h, l, c = bars[-1]  # последний ЗАКРЫТЫЙ 1h бар
    hour_utc = dt.datetime.fromtimestamp(t, dt.timezone.utc).hour
    in_session = always_open or (SESSION_UTC[0] <= hour_utc <= SESSION_UTC[1])

    out = {'name': name, 'price': c, 'trend': tr, 'ema20': e20, 'atr': a}
    if tr == 0:
        out['status'] = 'вне рынка: 4h тренда нет, сделки запрещены'
        return out
    side = 'ЛОНГ' if tr == 1 else 'ШОРТ'
    touched = (l <= e20) if tr == 1 else (h >= e20)
    closed_back = (c > e20 and c > o) if tr == 1 else (c < e20 and c < o)

    if touched and closed_back and in_session:
        entry = c  # ориентир входа = открытие следующего бара
        risk = ATR_SL * a
        sl = entry - risk if tr == 1 else entry + risk
        tp = entry + RR * risk if tr == 1 else entry - RR * risk
        be = (entry + 1.5 * risk if tr == 1 else entry - 1.5 * risk) if use_be else None
        out.update(status='СЕТАП АКТИВЕН', side=side, entry=entry, sl=sl, tp=tp, be=be, bar_ts=t)
    elif touched and not closed_back:
        out['status'] = f'{side}-режим: цена у EMA20 ({e20:.2f}), ждём закрытия обратно по тренду'
    else:
        dist = (c - e20) / a
        out['status'] = f'{side}-режим: ждём отката к EMA20 {e20:.2f} (сейчас {dist:+.1f} ATR от неё)'
    if not in_session and touched and closed_back:
        out['status'] += ' [сигнал вне сессии 12-20 UTC - пропуск]'
    return out


def rsi2(closes):
    out, au, ad = [50.0], None, None
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        u, d = max(ch, 0), max(-ch, 0)
        au = u if au is None else (au + u) / 2
        ad = d if ad is None else (ad + d) / 2
        out.append(100.0 if ad == 0 else 100 - 100 / (1 + au / ad))
    return out


def scan_nq_daytrade():
    """Дейтрейдинг NQ: RSI(2)<10 при цене выше EMA200(1h) -> лонг (зеркально шорт).
    Выход: RSI(2) пересекает 50 против позиции, конец сессии (закрытие бара 14:00 CT)
    или стоп 2xATR. Держать через ночь ЗАПРЕЩЕНО."""
    from zoneinfo import ZoneInfo
    bars = fetch_1h(NQ_SYMBOL, days=30)
    if len(bars) < 250:
        return {'name': 'NQ-MR', 'status': 'нет данных'}
    closes = [b[4] for b in bars]
    r2 = rsi2(closes)[-1]
    e200 = ema(closes, 200)[-1]
    a = atr(bars)[-1]
    t, o, h, l, c = bars[-1]
    h_ct = dt.datetime.fromtimestamp(t, ZoneInfo('America/Chicago')).hour
    out = {'name': 'NQ-MR', 'price': c, 'rsi2': r2, 'ema200': e200}
    # базис фьючерс-индекс: пользователь торгует/смотрит индекс NASDAQ, сигнал считается по NQ
    basis = 0.0
    try:
        ndx = fetch_bars('^NDX', days=3, interval='1h')
        if ndx:
            basis = c - ndx[-1][4]
            out['basis'] = basis
    except Exception:
        pass
    in_window = h_ct in NQ_ENTRY_HOURS_CT
    if r2 < NQ_RSI_LO and c > e200 and in_window:
        risk = NQ_ATR_SL * a
        out.update(status='СЕТАП АКТИВЕН', side='ЛОНГ', entry=c, sl=c - risk, tp=None,
                   entry_idx=c - basis, sl_idx=c - risk - basis,
                   exit_rule='выход: RSI2>50 или закрытие 14:00 CT', bar_ts=t)
    elif r2 > NQ_RSI_HI and c < e200 and in_window:
        risk = NQ_ATR_SL * a
        out.update(status='СЕТАП АКТИВЕН', side='ШОРТ', entry=c, sl=c + risk, tp=None,
                   entry_idx=c - basis, sl_idx=c + risk - basis,
                   exit_rule='выход: RSI2<50 или закрытие 14:00 CT', bar_ts=t)
    else:
        bias = 'лонги' if c > e200 else 'шорты'
        out['status'] = (f'нет сигнала: RSI2={r2:.0f} (нужно <{NQ_RSI_LO} или >{NQ_RSI_HI}), '
                         f'разрешены только {bias} (EMA200 {e200:.0f})'
                         + ('' if in_window else ' [вне окна 9-14 CT]'))
    return out


# Модуль дейтрейдинга BTC/GOLD на 30m: тренд 1h (EMA50/200) + откат к EMA20(30m).
# Бэктест: BTC (6 мес, Coinbase 15m/30m) - PF 1.19, стабилен в обеих половинах и на 15m, и на 30m.
# GOLD - PF 1.34-1.53, но выборка всего 60 дней => статус ЭКСПЕРИМЕНТАЛЬНЫЙ, риск 0.1%,
# пересмотр после 50 сделок. RSI2-MR и азиатский пробой тест НЕ прошли.
DAY_ASSETS = {
    'BTC-DT':  ('BTC',      True),
    'GOLD-DT': ('xyz:GOLD', False),
}


def scan_day_pullback(name, symbol, always_open):
    """30m тренд+откат, закрытие в конце дня. Вход: 1h тренд + откат к EMA20(30m)."""
    bars = fetch_any(symbol, days=59, interval='30m', bar_sec=1800)
    if len(bars) < 500:
        return {'name': name, 'status': 'нет данных'}
    closes = [b[4] for b in bars]
    e20 = ema(closes, 20)[-1]
    a = atr(bars)[-1]
    # тренд по 1h, собранному из 30m
    h1 = [(bars[i][0], bars[i][1], max(b[2] for b in bars[i:i+2]),
           min(b[3] for b in bars[i:i+2]), bars[i+1][4])
          for i in range(0, len(bars)-1, 2)]
    c1 = [b[4] for b in h1]
    e50h, e200h = ema(c1, 50)[-1], ema(c1, 200)[-1]
    t, o, h, l, c = bars[-1]
    if e50h > e200h and c1[-1] > e50h: tr = 1
    elif e50h < e200h and c1[-1] < e50h: tr = -1
    else: tr = 0

    hour_utc = dt.datetime.fromtimestamp(t, dt.timezone.utc).hour
    in_session = always_open or (7 <= hour_utc <= 18)
    out = {'name': name, 'price': c}
    if tr == 0:
        out['status'] = 'вне рынка: 1h тренда нет'
        return out
    side = 'ЛОНГ' if tr == 1 else 'ШОРТ'
    touched = (l <= e20) if tr == 1 else (h >= e20)
    closed_back = (c > e20 and c > o) if tr == 1 else (c < e20 and c < o)
    if touched and closed_back and in_session:
        risk = 2.0 * a
        sl = c - risk if tr == 1 else c + risk
        tp = c + 3*risk if tr == 1 else c - 3*risk
        out.update(status='СЕТАП АКТИВЕН', side=side, entry=c, sl=sl, tp=tp, bar_ts=t)
    else:
        out['status'] = f'{side}-режим: откат к EMA20(30m) {e20:.2f} не готов'
        if not in_session:
            out['status'] += ' [вне сессии]'
    return out


# Крипто-пробойный модуль: Donchian 48h + трейлинг 2.5xATR + тайм-стоп 120ч (1h).
# Одни параметры подтверждены НЕЗАВИСИМО на трёх активах (обе половины выборки прибыльны):
#   HYPE (13 мес): PF 1.20/1.17, N=92   | ETH (2 года): PF 1.18/1.68, N=339, +61R
#   SOL  (2 года): PF 1.16/1.19, N=394  | откатная свинг-система на альтах НЕ работает (PF<1)
# Риск: ETH/SOL 0.15-0.2% (2 года данных), HYPE 0.1-0.15% (13 мес).
HYPE_LOOKBACK, HYPE_ATR_TRAIL, HYPE_TIME_STOP_H = 48, 2.5, 120
CRYPTO_BREAKOUT = ['HYPE', 'ETH', 'SOL', 'ZEC', 'BNB']   # ZEC эксперимент (7 мес, PF 2.96); BNB добавлен 15.07 (PF 1.14/1.43, 2 года)
POSITIONS = Path(__file__).with_name('trade_positions.json')  # открытые позиции для ведения трейлинга
JOURNAL = Path(__file__).with_name('trade_journal.json')       # архив закрытых сделок для месячной статистики


def journal_trade(coin, pos, r, reason):
    try:
        j = json.load(open(JOURNAL)) if JOURNAL.exists() else []
    except Exception:
        j = []
    j.append({'coin': coin, 'side': pos['side'], 'entry': pos['entry'],
              'opened_ts': pos['entry_ts'], 'closed_ts': dt.datetime.now().timestamp(),
              'r': round(r, 2), 'reason': reason})
    JOURNAL.write_text(json.dumps(j, indent=1))


def monthly_report(state):
    """Отчёт за прошлый месяц - шлётся с первой утренней сводкой нового месяца."""
    today = dt.date.today()
    tag = today.strftime('%Y-%m')
    if state.get('monthly_report') == tag or not JOURNAL.exists():
        return ''
    prev_month = (today.replace(day=1) - dt.timedelta(days=1)).strftime('%Y-%m')
    try:
        j = json.load(open(JOURNAL))
    except Exception:
        return ''
    rows = [t for t in j if dt.date.fromtimestamp(t['closed_ts']).strftime('%Y-%m') == prev_month]
    state['monthly_report'] = tag
    if not rows:
        return f'\n\n📊 Отчёт за {prev_month}: закрытых сделок не было.'
    by = {}
    for t in rows:
        by.setdefault(t['coin'], []).append(t['r'])
    lines = [f'\n\n📊 Отчёт за {prev_month} ({len(rows)} сделок):']
    for coin in sorted(by, key=lambda c: -sum(by[c])):
        rs = by[coin]
        plus = sum(1 for x in rs if x > 0); minus = len(rs) - plus
        lines.append(f'  {coin}: {plus}+ / {minus}-  итог {sum(rs):+.1f}R')
    allr = [t['r'] for t in rows]
    wins = [x for x in allr if x > 0.05]; losses = [x for x in allr if x < -0.05]
    pf = (sum(wins)/abs(sum(losses))) if losses else 99
    lines.append(f'  ИТОГО: {sum(1 for x in allr if x>0)}+ / {sum(1 for x in allr if x<=0)}-, '
                 f'{sum(allr):+.1f}R, PF {pf:.2f}, win rate {sum(1 for x in allr if x>0)/len(allr):.0%}')
    return '\n'.join(lines)


def load_positions():
    if POSITIONS.exists():
        try:
            return json.load(open(POSITIONS))
        except Exception:
            return {}
    return {}


def save_positions(p):
    POSITIONS.write_text(json.dumps(p, indent=1))


def trail_position(coin, pos):
    """Реплей трейлинга по закрытым 1h барам с момента входа.
    Возвращает dict: stop, best, r_now, hours, событие ('stop_hit'/'time_stop'/None)."""
    d = 1 if pos['side'] == 'long' else -1
    entry, entry_ts = pos['entry'], pos['entry_ts']
    days = max(2, int((dt.datetime.now().timestamp() - entry_ts) / 86400) + 2)
    bars = fetch_hl_bars(coin, '1h', days=days)
    a = atr(bars)
    # начальный стоп по ATR на баре входа (последний закрытый до entry_ts)
    i0 = max((i for i, b in enumerate(bars) if b[0] <= entry_ts), default=0)
    risk = HYPE_ATR_TRAIL * a[i0]
    stop = entry - d * risk
    best = entry
    event = None
    for i in range(i0 + 1, len(bars)):
        t, o, h, l, c = bars[i]
        if (d == 1 and l <= stop) or (d == -1 and h >= stop):
            event = 'stop_hit'
            break
        if (d == 1 and c > best) or (d == -1 and c < best):
            best = c
            new_stop = c - d * HYPE_ATR_TRAIL * a[i]
            if (d == 1 and new_stop > stop) or (d == -1 and new_stop < stop):
                stop = new_stop
    hours = (dt.datetime.now().timestamp() - entry_ts) / 3600
    last_c = bars[-1][4]
    r_now = d * (last_c - entry) / risk if risk else 0
    if event is None and hours >= HYPE_TIME_STOP_H:
        event = 'time_stop'
    return {'stop': stop, 'best': best, 'r_now': r_now, 'hours': hours,
            'price': last_c, 'risk': risk, 'event': event}

# Модуль наблюдения SPACEX (шорт-план, см. раздел в TRADING_STRATEGY.md).
# Не стратегия с бэктестом, а алерты по уровням шорт-тезиса + календарь разлоков.
SPCX_SYMBOL = 'SPCX'
SPCX_LEVELS = [   # (уровень, направление пробоя, что это значит)
    (147.0,  'down', 'пробой июньского лоу - сигнал фазы 2 шорта'),
    (175.5,  'up',   'зона триггера +30% к IPO - выше разлочится доп. 10%'),
    (178.0,  'up',   'ИНВАЛИДАЦИЯ шорт-тезиса - крыть по стопу'),
]
SPCX_EVENTS = [
    ('2026-08-06', 'Первый отчёт SPCX (ожидаемая дата, не подтверждена)'),
    ('2026-08-10', 'Разлок ~20% инсайдерских акций (2 дня после отчёта)'),
    ('2026-10-24', 'Конец 7%-траншей разлока (135 дней от IPO)'),
    ('2026-11-05', 'Отчёт Q3 -> разлок ещё 28% (дата отчёта ориентировочная)'),
    ('2026-12-08', 'Полный разлок (180 дней от IPO)'),
    ('2027-06-12', 'Разлок 6.4 млрд акций Маска'),
]


# Мониторинг USD/KZT (тенге): не торговый модуль, а наблюдение по ресёрчу 04.07.2026.
# Тенге у сильного края (472); алерты на пробой 480 и 500 вверх (ослабление тенге)
# и 460 вниз (аномальное укрепление - НБ обычно начинает скупать валюту).
KZT_LEVELS = [
    (460.0, 'down', 'тенге аномально крепкий - НБ обычно скупает валюту, окно для покупки USD'),
    (480.0, 'up',   'тенге слабеет - первый уровень шорт-тезиса по тенге'),
    (500.0, 'up',   'тенге пробил психологический уровень - режим ослабления подтверждён'),
]


def scan_kzt():
    bars = fetch_bars('KZT=X', days=30, interval='1d', bar_sec=86400)
    if len(bars) < 5:
        return {'name': 'USDKZT', 'status': 'нет данных'}
    t = bars[-1][0]
    c_prev, c = bars[-2][4], bars[-1][4]
    out = {'name': 'USDKZT', 'price': c}
    for level, direction, meaning in KZT_LEVELS:
        crossed = (c_prev >= level > c) if direction == 'down' else (c_prev <= level < c)
        if crossed:
            out.update(status='УРОВЕНЬ ПРОБИТ', level=level, meaning=meaning, bar_ts=t)
            return out
    week_ago = bars[-6][4] if len(bars) >= 6 else c_prev
    out['status'] = f'наблюдение: {c:.1f} ({(c/week_ago-1)*100:+.1f}% за нед), уровни 460 / 480 / 500'
    return out


# Мониторинг PURR (Hyperliquid Strategies, Nasdaq) - DAT-компания по HYPE.
# Тезис 04.07.2026: дисконт ~28% к NAV. Константы из отчётности - ОБНОВЛЯТЬ после квартальных отчётов!
PURR_HYPE_HELD = 20_000_000   # HYPE на балансе (8-K апрель 2026)
PURR_CASH = 103_000_000       # кэш, $
PURR_SHARES = 134_620_000     # акций в обращении
PURR_BUY_LEVEL = 7.0          # зона второй лимитки 6.5-7.0
PURR_MNAV_EXIT = 1.0          # дисконт схлопнулся - сигнал ротации в спот HYPE


# ===== БЛОК "ДОЛГОСРОК": накопление HYPE + PURR на 2-3 года под следующий бычий рынок =====
# Не трейдинг: алерты на уровни лестницы для покупок частями. См. диалог 08.07.2026.
LT_HYPE_LADDER = [55.5, 49.0, 42.5, 33.0]   # уровни докупки HYPE (коррекции Фибо волны 21->77)
LT_PURR_MNAV = [0.60, 0.50]                 # докупка PURR при расширении дисконта
LT_FNG_CAPITULATION = 15                    # Fear&Greed ниже = капитуляция
LT_THESIS_DAYS = 90                         # период напоминания о проверке тезиса


def scan_longterm(state):
    """Возвращает (список строк статуса, список alert-кортежей) для блока ДОЛГОСРОК."""
    lines, alerts = [], []
    # --- HYPE: лестница по дневным закрытиям ---
    try:
        db = fetch_hl_bars('HYPE', '1d', days=10)
        t, c_prev, c = db[-1][0], db[-2][4], db[-1][4]
        hit = next((lv for lv in LT_HYPE_LADDER if c_prev > lv >= c), None)
        if hit is not None:
            alerts.append((f'LT-HYPE:{hit}', t,
                           f"📥 ДОЛГОСРОК HYPE: цена {c:.2f} достигла уровня докупки {hit}. "
                           f"Взять транш лестницы (см. план)."))
            lines.append(f"LT-HYPE >>> уровень {hit} достигнут (цена {c:.2f})")
        else:
            nxt = next((lv for lv in LT_HYPE_LADDER if lv < c), LT_HYPE_LADDER[-1])
            lines.append(f"LT-HYPE наблюдение: {c:.2f}, следующий уровень докупки {nxt} ({(nxt/c-1)*100:+.0f}%)")
    except Exception as ex:
        lines.append(f'LT-HYPE ОШИБКА: {str(ex)[:60]}')
    # --- PURR: докупка по расширению дисконта (mNAV) ---
    try:
        pb = fetch_bars('PURR', days=5, interval='1d', bar_sec=86400)
        hype_px = fetch_hl_bars('HYPE', '1d', days=3)[-1][4]
        nav = PURR_HYPE_HELD * hype_px + PURR_CASH
        mnav = pb[-1][4] * PURR_SHARES / nav
        mnav_prev = pb[-2][4] * PURR_SHARES / nav
        hit = next((lv for lv in LT_PURR_MNAV if mnav_prev > lv >= mnav), None)
        if hit is not None:
            alerts.append((f'LT-PURR:{hit}', pb[-1][0],
                           f"📥 ДОЛГОСРОК PURR: дисконт расширился до mNAV {hit:.2f} "
                           f"(цена {pb[-1][4]:.2f}). Взять транш — сначала проверь отчёт на дилюцию/продажу HYPE."))
            lines.append(f"LT-PURR >>> mNAV {hit:.2f} достигнут (цена {pb[-1][4]:.2f})")
        else:
            lines.append(f"LT-PURR наблюдение: {pb[-1][4]:.2f}, mNAV {mnav:.2f}, докупка при 0.60/0.50")
    except Exception as ex:
        lines.append(f'LT-PURR ОШИБКА: {str(ex)[:60]}')
    # --- Триггер капитуляции: BTC фандинг < 0 И Fear&Greed < 15 ---
    try:
        fng = int(json.load(urllib.request.urlopen(
            urllib.request.Request('https://api.alternative.me/fng/', headers=UA),
            timeout=15, context=CTX))['data'][0]['value'])
        body = json.dumps({'type': 'metaAndAssetCtxs'}).encode()
        meta, ctxs = json.load(urllib.request.urlopen(urllib.request.Request(
            'https://api.hyperliquid.xyz/info', data=body,
            headers={'Content-Type': 'application/json'}), timeout=15, context=CTX))
        i = [u['name'] for u in meta['universe']].index('BTC')
        funding = float(ctxs[i]['funding']) * 100
        cap_now = funding < 0 and fng < LT_FNG_CAPITULATION
        was = state.get('lt_capitulation', False)
        if cap_now and not was:
            alerts.append(('LT-CAPITULATION', dt.date.today().isoformat(),
                           f"🩸 КАПИТУЛЯЦИЯ: F&G {fng} + BTC фандинг {funding:+.4f}%/ч. "
                           f"Лучшие точки цикла для докупки HYPE/PURR — брать транш немедленно."))
        state['lt_capitulation'] = cap_now
        lines.append(f"LT-CAPIT: F&G {fng}, фандинг {funding:+.4f}%/ч "
                     f"{'— КАПИТУЛЯЦИЯ АКТИВНА' if cap_now else '(ждём F&G<15 + отриц.фандинг)'}")
    except Exception as ex:
        lines.append(f'LT-CAPIT ОШИБКА: {str(ex)[:60]}')
    return lines, alerts


def longterm_thesis_reminder(state):
    """Квартальное напоминание проверить тезис (для утренней сводки)."""
    today = dt.date.today()
    last = state.get('lt_thesis_checked')
    due = last is None or (today - dt.date.fromisoformat(last)).days >= LT_THESIS_DAYS
    if not due:
        return ''
    state['lt_thesis_checked'] = today.isoformat()
    return ("\n\n🔍 ДОЛГОСРОК — квартальная проверка тезиса Hyperliquid:\n"
            "  1) Объёмы/доля рынка Hyperliquid не падают 2+ квартала?\n"
            "  2) Нет регуляторного удара по перп-DEX?\n"
            "  3) PURR не размывает акции эмиссией ниже NAV?\n"
            "  4) Нет доминирующего конкурента с оттоком ликвидности?\n"
            "  Если все 4 чисты — просадки это уровни докупки, а не паника.")


# ===== ЛОТЕРЕЯ: событийные алерты по хвосту Hyperliquid (230+ перпов) =====
# Бэктест 08.07.2026: системный трейдинг хвоста УБЫТОЧЕН (PF net 0.52-0.94 на 2300 сделках),
# но 30д-забеги +100-400% реальны. Поэтому НЕ сигналы входа, а "посмотри руками":
# всплеск объёма >=5x + пробой 30д максимума, либо новый листинг.
# Правила билета: 1-2% капитала, плечо 1-2x, выход трейлингом по ДНЕВНЫМ барам, max 3-5 билетов.
LOTTERY_VOL_MULT = 5.0        # всплеск: объём 4ч >= 5x среднего 4ч за 7 дней
LOTTERY_MIN_OI = 500_000      # минимальный OI $, чтобы отсеять мёртвые
LOTTERY_CORE = {'BTC', 'ETH', 'SOL', 'HYPE', 'ZEC'}   # ядро не лотерея
HLP_VAULT = '0xdfc24b077bc1425ad1dea75bcb6f8158e10df303'


def hl_info(payload):
    req = urllib.request.Request('https://api.hyperliquid.xyz/info',
                                 data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=30, context=CTX))


def scan_lottery(state):
    """Возвращает (строки, алерты). Новые листинги + объёмные всплески с пробоем."""
    lines, alerts = [], []
    try:
        meta, ctxs = hl_info({'type': 'metaAndAssetCtxs'})
        known = set(state.get('lottery_universe', []))
        current = {u['name'] for u in meta['universe'] if not u.get('isDelisted')}
        # --- новые листинги ---
        if known:
            for coin in sorted(current - known):
                alerts.append((f'LOTTO-NEW:{coin}', dt.date.today().isoformat(),
                               f"🎰 НОВЫЙ ЛИСТИНГ на Hyperliquid: {coin}. Первые дни - самые дикие забеги; "
                               f"смотреть руками, билет 1-2%, плечо 1-2x."))
                lines.append(f'LOTTO   новый листинг: {coin}')
        state['lottery_universe'] = sorted(current)
        # --- всплески оборота на хвосте: предфильтр БЕЗ запросов свечей ---
        # "горячесть" = дневной объём / OI; свечи качаем только для топ-8 горячих
        cand = []
        for u, c in zip(meta['universe'], ctxs):
            name = u['name']
            if name in LOTTERY_CORE or name not in current:
                continue
            try:
                oi = float(c['openInterest']) * float(c['markPx'])
                vol = float(c.get('dayNtlVlm', 0))
                if oi >= LOTTERY_MIN_OI and vol > 0:
                    heat = vol / oi
                    if heat >= 1.0 or vol >= 5e6:   # оборот за день >= OI - монета кипит
                        cand.append((heat, vol, name))
            except Exception:
                continue
        cand.sort(reverse=True)
        hot_checked = 0
        for heat, vol, name in cand[:8]:
            try:
                db = fetch_hl_bars(name, '1d', days=32, bar_sec=86400)
                hot_checked += 1
                if len(db) < 15:
                    continue
                hh30 = max(b[2] for b in db[:-1])
                c_last = db[-1][4]
                if c_last > hh30:
                    key = f'LOTTO-BRK:{name}'
                    if state.get(key) != db[-1][0]:
                        chg = c_last / db[-2][4] - 1
                        alerts.append((key, db[-1][0],
                                       f"🎰 ЛОТЕРЕЯ: {name} кипит (оборот {heat:.1f}x OI, ${vol/1e6:.1f}M/д) "
                                       f"и пробил 30-дневный максимум ({c_last:.4g}, {chg:+.0%} за день). "
                                       f"Проверить нарратив руками. Билет 1-2%, плечо 1-2x, трейлинг по дневкам."))
                        lines.append(f'LOTTO   {name}: оборот {heat:.1f}x OI + пробой 30д максимума')
            except Exception:
                continue
        if not any(l.startswith('LOTTO') for l in lines):
            lines.append(f'LOTTO   тихо: горячих {len(cand)} (проверено {hot_checked}), пробоев 30д нет')
    except Exception as ex:
        lines.append(f'LOTTO   ОШИБКА: {str(ex)[:60]}')
    return lines, alerts


def hlp_status_line():
    """Строка для утренней сводки: HLP월ный PnL и APR - когда снова начнёт зарабатывать."""
    try:
        d = hl_info({'type': 'vaultDetails', 'vaultAddress': HLP_VAULT})
        apr = float(d.get('apr', 0)) * 100
        month_pnl = None
        for period, pdata in d.get('portfolio', []):
            if period == 'month':
                ph = pdata.get('pnlHistory', [])
                if ph:
                    month_pnl = float(ph[-1][1])
        tvl = float(d.get('maxDistributable', 0))
        verdict = ('🟢 волт зарабатывает - окно для депозита' if (month_pnl or 0) > 0 and apr > 5
                   else '🔴 волт спит/в минусе - депозит не спешить')
        pnl_txt = f'{month_pnl/1e3:+,.0f}K' if month_pnl is not None else '?'
        return (f"\n\n🏦 HLP-волт: APR {apr:+.1f}%, PnL за месяц ${pnl_txt}, "
                f"TVL ${tvl/1e6:.0f}M. {verdict}")
    except Exception as ex:
        return f'\n\n🏦 HLP: данные недоступны ({str(ex)[:40]})'


# ===== CRASH-BOUNCE МОНИТОРИНГ (виртуальный, деньги НЕ участвуют) =====
# Бэктест 12.07.2026: лонг обвала -15%/24ч на лоукапах дал PF 1.81 (2024) и 1.65 (2025),
# но 0.86 в 2026 (66 сделок). Копим ЖИВУЮ статистику: >=20 закрытых сигналов,
# PF > 1.3 -> включаем реальные деньги (риск 0.1%, max 2 позиции); PF < 1.1 -> в архив.
CRASH_FILE = Path(__file__).with_name('crash_monitor.json')
CRASH_DROP = -0.15          # порог обвала за 24ч
CRASH_MIN_OI = 2_000_000    # минимальный OI
CRASH_HOLD_H = 48           # выход через 48 часов
CRASH_ATR_SL = 2.0          # стоп 2xATR(4h)
CRASH_COOLDOWN_H = 24       # не сигналить ту же монету 24ч после закрытия


def _crash_load():
    if CRASH_FILE.exists():
        try:
            return json.load(open(CRASH_FILE))
        except Exception:
            pass
    return {'signals': []}


def scan_crash_monitor():
    """Виртуальный трекинг crash-bounce. Возвращает (строки, алерты)."""
    lines, alerts = [], []
    mon = _crash_load()
    now = dt.datetime.now().timestamp()

    # --- 1. ведём открытые виртуальные сигналы ---
    for sig in mon['signals']:
        if sig.get('status') != 'open':
            continue
        try:
            bars = fetch_hl_bars(sig['coin'], '4h', days=4, bar_sec=14400)
            after = [b for b in bars if b[0] > sig['entry_ts']]
            r = None
            for b in after:
                if b[3] <= sig['stop']:
                    r = -1.0
                    break
            if r is None and after and now >= sig['entry_ts'] + CRASH_HOLD_H * 3600:
                r = (after[-1][4] - sig['entry']) / sig['risk']
            if r is not None:
                sig['status'] = 'closed'
                sig['r'] = round(r, 2)
                sig['closed_ts'] = now
                closed = [s['r'] for s in mon['signals'] if s.get('status') == 'closed']
                wins = [x for x in closed if x > 0.05]; losses = [x for x in closed if x < -0.05]
                pf = (sum(wins) / abs(sum(losses))) if losses else 99
                alerts.append((f"crash-close:{sig['coin']}", int(sig['entry_ts']),
                               f"🧪 CRASH-мониторинг: {sig['coin']} закрыт {r:+.2f}R (виртуально). "
                               f"Статистика: {len(closed)} сигналов, PF {pf:.2f}, сумма {sum(closed):+.1f}R"
                               + (f" — ГОТОВО К РЕШЕНИЮ (>=20 сигналов)" if len(closed) >= 20 else '')))
        except Exception:
            continue

    # --- 2. детект новых обвалов (без свечей: prevDayPx из meta) ---
    try:
        meta, ctxs = hl_info({'type': 'metaAndAssetCtxs'})
        open_coins = {s['coin'] for s in mon['signals'] if s.get('status') == 'open'}
        recent = {s['coin']: s.get('closed_ts', 0) for s in mon['signals'] if s.get('status') == 'closed'}
        new_cnt = 0
        for u, c in zip(meta['universe'], ctxs):
            name = u['name']
            if name in LOTTERY_CORE or name in open_coins or new_cnt >= 3:
                continue
            if now - recent.get(name, 0) < CRASH_COOLDOWN_H * 3600:
                continue
            try:
                px = float(c['markPx']); prev = float(c['prevDayPx'])
                oi = float(c['openInterest']) * px
                if oi < CRASH_MIN_OI or prev <= 0:
                    continue
                chg = px / prev - 1
                if chg <= CRASH_DROP:
                    bars = fetch_hl_bars(name, '4h', days=12, bar_sec=14400)
                    if len(bars) < 20:
                        continue
                    a = atr(bars)[-1]
                    risk = CRASH_ATR_SL * a
                    if risk <= 0:
                        continue
                    entry = bars[-1][4]
                    sig = {'coin': name, 'entry': entry, 'risk': risk,
                           'stop': entry - risk, 'entry_ts': bars[-1][0], 'status': 'open'}
                    mon['signals'].append(sig)
                    new_cnt += 1
                    alerts.append((f'crash-open:{name}', bars[-1][0],
                                   f"🧪 CRASH-сигнал (МОНИТОРИНГ, деньги не ставить): {name} {chg:+.0%} за 24ч. "
                                   f"Виртуальный лонг {entry:.4g}, стоп {entry - risk:.4g}, выход через 48ч. "
                                   f"Учёт ведётся автоматически."))
            except Exception:
                continue
    except Exception as ex:
        lines.append(f'CRASH   ОШИБКА детекта: {str(ex)[:60]}')

    CRASH_FILE.write_text(json.dumps(mon, indent=1))
    closed = [s['r'] for s in mon['signals'] if s.get('status') == 'closed']
    n_open = sum(1 for s in mon['signals'] if s.get('status') == 'open')
    if closed:
        wins = [x for x in closed if x > 0.05]; losses = [x for x in closed if x < -0.05]
        pf = (sum(wins) / abs(sum(losses))) if losses else 99
        lines.append(f"CRASH   мониторинг: {len(closed)} закрыто (PF {pf:.2f}, {sum(closed):+.1f}R), "
                     f"{n_open} открыто. Решение при 20 закрытых.")
    else:
        lines.append(f"CRASH   мониторинг: открытых виртуальных {n_open}, закрытых пока нет")
    return lines, alerts


def scan_purr():
    bars = fetch_bars('PURR', days=10, interval='1d', bar_sec=86400)
    if len(bars) < 3:
        return {'name': 'PURR', 'status': 'нет данных'}
    t = bars[-1][0]
    c_prev, c = bars[-2][4], bars[-1][4]
    hype_px = None
    try:
        hype_px = fetch_hype_1h(days=3)[-1][4]
    except Exception:
        pass
    out = {'name': 'PURR', 'price': c}
    if hype_px:
        nav = PURR_HYPE_HELD * hype_px + PURR_CASH
        mnav = (c * PURR_SHARES) / nav
        out['mnav'] = mnav
        prev_mnav = (c_prev * PURR_SHARES) / nav
        if prev_mnav < PURR_MNAV_EXIT <= mnav:
            out.update(status='СИГНАЛ', side='РОТАЦИЯ',
                       meaning=f'mNAV {mnav:.2f} - дисконт схлопнулся, апсайд к HYPE исчерпан, думать о фиксации',
                       bar_ts=t)
            return out
    if c_prev >= PURR_BUY_LEVEL > c:
        out.update(status='СИГНАЛ', side='ЗОНА ПОКУПКИ',
                   meaning=f'цена {c:.2f} вошла в зону второй лимитки 6.5-7.0', bar_ts=t)
        return out
    mnav_txt = f', mNAV {out["mnav"]:.2f} (дисконт {(1-out["mnav"])*100:.0f}%)' if 'mnav' in out else ''
    out['status'] = f'наблюдение: {c:.2f}{mnav_txt}, лимитка 7.0, ротация при mNAV 1.0'
    return out


def sentiment_block():
    """Сантимент-блок для утренней сводки: крипта + акции + ставки. Каждый пункт опционален."""
    lines = ['\n\n🌡 Сантимент:']

    def rsi14_last(closes):
        au = ad = None
        for i in range(1, len(closes)):
            ch = closes[i] - closes[i-1]
            u, d = max(ch, 0), max(-ch, 0)
            au = u if au is None else (au*13 + u)/14
            ad = d if ad is None else (ad*13 + d)/14
        return 100.0 if not ad else 100 - 100/(1 + au/ad)

    try:  # Crypto Fear & Greed
        req = urllib.request.Request('https://api.alternative.me/fng/', headers=UA)
        v = json.load(urllib.request.urlopen(req, timeout=15, context=CTX))['data'][0]
        ru = {'Extreme Fear': 'экстремальный страх', 'Fear': 'страх', 'Neutral': 'нейтрально',
              'Greed': 'жадность', 'Extreme Greed': 'экстремальная жадность'}
        lines.append(f"• Крипто Fear&Greed: {v['value']}/100 — {ru.get(v['value_classification'], v['value_classification'])}")
    except Exception:
        pass
    try:  # BTC: 24h + funding с Hyperliquid
        bars = fetch_hl_bars('BTC', '1h', days=3)
        chg = bars[-1][4]/bars[-25][4] - 1
        body = json.dumps({'type': 'metaAndAssetCtxs'}).encode()
        req = urllib.request.Request('https://api.hyperliquid.xyz/info', data=body,
                                     headers={'Content-Type': 'application/json'})
        meta, ctxs = json.load(urllib.request.urlopen(req, timeout=15, context=CTX))
        i = [u['name'] for u in meta['universe']].index('BTC')
        f = float(ctxs[i]['funding']) * 100
        f_txt = 'лонги платят шортам' if f > 0 else 'шорты платят лонгам'
        lines.append(f'• BTC: {bars[-1][4]:,.0f} ({chg:+.1%} за 24ч), фандинг {f:+.4f}%/ч ({f_txt})')
    except Exception:
        pass
    try:  # S&P 500: VIX + RSI + от максимума
        vix = fetch_bars('^VIX', days=10, interval='1d', bar_sec=86400)[-1][4]
        v_txt = ('самоуспокоенность' if vix < 15 else 'спокойно' if vix < 20
                 else 'нервозность' if vix < 30 else 'ПАНИКА')
        spx = fetch_bars('^GSPC', days=380, interval='1d', bar_sec=86400)
        closes = [b[4] for b in spx]
        rsi = rsi14_last(closes[-60:])
        r_txt = 'перекуплен' if rsi > 70 else 'перепродан' if rsi < 30 else 'нейтрален'
        hi52 = max(b[2] for b in spx[-252:])
        lines.append(f'• S&P500: {closes[-1]:,.0f} ({(closes[-1]/hi52-1)*100:+.1f}% от 52н максимума), '
                     f'RSI(14) {rsi:.0f} — {r_txt}, VIX {vix:.1f} — {v_txt}')
    except Exception:
        pass
    try:  # доллар и ставки
        dxy = fetch_bars('DX-Y.NYB', days=10, interval='1d', bar_sec=86400)[-1][4]
        tnx = fetch_bars('^TNX', days=10, interval='1d', bar_sec=86400)[-1][4]
        lines.append(f'• DXY {dxy:.1f}, US 10Y {tnx:.2f}%')
    except Exception:
        pass
    return '\n'.join(lines) if len(lines) > 1 else ''


SENTI_HISTORY = Path(__file__).with_name('sentiment_history.json')


def collect_sentiment_metrics():
    """Числовые метрики сантимента для истории и суждений. Всё опционально."""
    m = {}
    try:
        req = urllib.request.Request('https://api.alternative.me/fng/', headers=UA)
        m['fng'] = int(json.load(urllib.request.urlopen(req, timeout=15, context=CTX))['data'][0]['value'])
    except Exception:
        pass
    try:
        bars = fetch_hl_bars('BTC', '1h', days=3)
        m['btc'] = bars[-1][4]
        m['btc_chg'] = bars[-1][4]/bars[-25][4] - 1
        body = json.dumps({'type': 'metaAndAssetCtxs'}).encode()
        req = urllib.request.Request('https://api.hyperliquid.xyz/info', data=body,
                                     headers={'Content-Type': 'application/json'})
        meta, ctxs = json.load(urllib.request.urlopen(req, timeout=15, context=CTX))
        i = [u['name'] for u in meta['universe']].index('BTC')
        m['funding'] = float(ctxs[i]['funding']) * 100
    except Exception:
        pass
    try:
        m['vix'] = fetch_bars('^VIX', days=10, interval='1d', bar_sec=86400)[-1][4]
        spx = fetch_bars('^GSPC', days=380, interval='1d', bar_sec=86400)
        closes = [b[4] for b in spx]
        au = ad = None
        for i in range(1, len(closes[-60:])):
            ch = closes[-60:][i] - closes[-60:][i-1]
            u, d = max(ch, 0), max(-ch, 0)
            au = u if au is None else (au*13+u)/14
            ad = d if ad is None else (ad*13+d)/14
        m['spx'] = closes[-1]
        m['spx_rsi'] = 100.0 if not ad else 100 - 100/(1+au/ad)
        m['spx_from_hi'] = closes[-1]/max(b[2] for b in spx[-252:]) - 1
    except Exception:
        pass
    try:
        m['dxy'] = fetch_bars('DX-Y.NYB', days=10, interval='1d', bar_sec=86400)[-1][4]
        m['tnx'] = fetch_bars('^TNX', days=10, interval='1d', bar_sec=86400)[-1][4]
    except Exception:
        pass
    return m


def judgment_block(m):
    """Суждение по правилам: динамика метрик + вывод, куда смотрит рынок.
    Это эвристики, не аналитик - глубокий разбор спрашивать у Claude."""
    hist = {}
    if SENTI_HISTORY.exists():
        try:
            hist = json.load(open(SENTI_HISTORY))
        except Exception:
            hist = {}
    today = dt.date.today().isoformat()
    prev = None
    for d0 in sorted(hist, reverse=True):
        if d0 < today and isinstance(hist[d0], dict):
            prev = hist[d0]
            break
    hist[today] = m
    hist = {k: hist[k] for k in sorted(hist)[-30:]}   # храним месяц
    SENTI_HISTORY.write_text(json.dumps(hist))

    obs, score = [], 0   # score: + = risk-on, - = risk-off
    fng = m.get('fng')
    if fng is not None:
        d_txt = ''
        if prev and prev.get('fng') is not None:
            df = fng - prev['fng']
            d_txt = f' ({df:+d} за день)'
            if df <= -5: obs.append(f'страх в крипте углубляется{d_txt}'); score -= 1
            elif df >= 5 and fng < 35: obs.append(f'страх отпускает{d_txt} - разворотный намёк'); score += 1
        if fng <= 24:
            obs.append(f'F&G {fng} - зона капитуляции: исторически ближе к дну, чем к вершине; свежие шорты не наращивать')
        elif fng >= 75:
            obs.append(f'F&G {fng} - жадность: не догонять лонги'); score -= 1
    if m.get('funding') is not None and m.get('btc_chg') is not None:
        f, chg = m['funding'], m['btc_chg']
        if chg < -0.005 and f > 0:
            obs.append('BTC падает, а лонги всё платят - капитуляции не было, вниз ещё есть топливо'); score -= 1
        elif chg < 0 and f < 0:
            obs.append('шорты BTC переполнены (отриц. фандинг) - топливо для сквиза вверх'); score += 1
        elif chg > 0.01 and f > 0.004:
            obs.append('рост BTC на перегретом фандинге - хрупкий'); score -= 1
    vix = m.get('vix')
    if vix is not None:
        if prev and prev.get('vix') and vix - prev['vix'] > 2:
            obs.append(f'VIX прыгнул {prev["vix"]:.0f} -> {vix:.0f} - институционалы страхуются'); score -= 2
        elif vix < 15 and (m.get('spx_from_hi') or 0) > -0.03:
            obs.append('VIX <15 у максимумов - самоуспокоенность, страховка дешёвая (пут-хедж почти бесплатен)')
    rsi = m.get('spx_rsi')
    if rsi is not None:
        if rsi > 70: obs.append(f'S&P перекуплен (RSI {rsi:.0f}) - откат зреет'); score -= 1
        elif rsi < 30: obs.append(f'S&P перепродан (RSI {rsi:.0f}) - отскок зреет'); score += 1
    if prev and m.get('dxy') and prev.get('dxy') and m.get('tnx') and prev.get('tnx'):
        d_up, t_up = m['dxy'] > prev['dxy'], m['tnx'] > prev['tnx']
        if d_up and t_up:
            obs.append('доллар и доходности растут вместе - встречный ветер золоту/серебру/крипте'); score -= 1
        elif not d_up and not t_up:
            obs.append('доллар и доходности снижаются - попутный ветер металлам и крипте'); score += 1

    if not obs:
        return ''
    verdict = ('рынок смотрит ВНИЗ (risk-off): приоритет шорт-сетапам, лонги полразмера' if score <= -2 else
               'рынок смотрит ВВЕРХ (risk-on): приоритет лонг-сетапам' if score >= 2 else
               'рынок в нерешительности: без приоритета, торговать только чистые сетапы')
    out = ['\n\n🧭 Суждение (эвристики по динамике):']
    out += [f'• {o}' for o in obs[:5]]
    out.append(f'=> {verdict}')
    if not prev:
        out.append('(первая запись истории - динамика день-к-дню появится завтра)')
    return '\n'.join(out)


def spacex_events_block(horizon_days=45):
    today = dt.date.today()
    lines = []
    for ds, label in SPCX_EVENTS:
        d0 = dt.date.fromisoformat(ds)
        left = (d0 - today).days
        if 0 <= left <= horizon_days:
            lines.append(f'  - через {left} дн ({d0:%d.%m}): {label}')
    return ('\n\n🚀 SPACEX - календарь шорт-плана:\n' + '\n'.join(lines)) if lines else ''


def scan_spacex():
    bars = fetch_1h(SPCX_SYMBOL, days=10)
    if len(bars) < 10:
        return {'name': 'SPCX', 'status': 'нет данных'}
    t = bars[-1][0]
    c_prev, c = bars[-2][4], bars[-1][4]
    out = {'name': 'SPCX', 'price': c}
    for level, direction, meaning in SPCX_LEVELS:
        crossed = (c_prev >= level > c) if direction == 'down' else (c_prev <= level < c)
        if crossed:
            out.update(status='УРОВЕНЬ ПРОБИТ', level=level, meaning=meaning, bar_ts=t)
            return out
    dists = ', '.join(f'{lv:g} ({(lv/c-1)*100:+.1f}%)' for lv, _, _ in SPCX_LEVELS)
    out['status'] = f'наблюдение: уровни {dists}'
    return out


def fetch_hype_1h(days=15):
    return fetch_hl_bars('HYPE', '1h', days, 3600)


def scan_breakout(coin):
    bars = fetch_hl_bars(coin, '1h', days=15)
    if len(bars) < HYPE_LOOKBACK + 20:
        return {'name': coin, 'status': 'нет данных'}
    a = atr(bars)[-1]
    t, o, h, l, c = bars[-1]
    hh = max(b[2] for b in bars[-HYPE_LOOKBACK-1:-1])
    ll = min(b[3] for b in bars[-HYPE_LOOKBACK-1:-1])
    out = {'name': coin, 'price': c}
    trail_txt = f'трейлинг {HYPE_ATR_TRAIL}xATR за ценой, тайм-стоп {HYPE_TIME_STOP_H}ч'
    if c > hh:
        out.update(status='СЕТАП АКТИВЕН', side='ЛОНГ', entry=c, sl=c - HYPE_ATR_TRAIL * a,
                   tp=None, exit_rule=trail_txt, bar_ts=t)
    elif c < ll:
        out.update(status='СЕТАП АКТИВЕН', side='ШОРТ', entry=c, sl=c + HYPE_ATR_TRAIL * a,
                   tp=None, exit_rule=trail_txt, bar_ts=t)
    else:
        out['status'] = (f'внутри канала: пробой вверх > {hh:.2f} (+{(hh/c-1)*100:.1f}%), '
                         f'вниз < {ll:.2f} ({(ll/c-1)*100:.1f}%)')
    return out


# Активы для вечернего отчёта (что торгуем по TRADING_STRATEGY.md)
RECAP_ASSETS = {'BTC': 'BTC', 'ETH': 'ETH', 'SOL': 'SOL', 'GOLD': 'xyz:GOLD', 'SILVER': 'xyz:SILVER', 'NASDAQ': 'NQ=F', 'SPACEX': 'SPCX'}
CT = None  # ленивая инициализация ZoneInfo


def _ct():
    global CT
    if CT is None:
        from zoneinfo import ZoneInfo
        CT = ZoneInfo('America/Chicago')
    return CT


def fetch_calendar():
    """События ForexFactory на сегодня (CT): USD, импакт High/Medium."""
    url = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
    req = urllib.request.Request(url, headers=UA)
    events = json.load(urllib.request.urlopen(req, timeout=20, context=CTX))
    today = dt.datetime.now(_ct()).date()
    out = []
    for e in events:
        if e.get('country') != 'USD' or e.get('impact') not in ('High', 'Medium'):
            continue
        try:
            when = dt.datetime.fromisoformat(e['date']).astimezone(_ct())
        except ValueError:
            continue
        if when.date() != today:
            continue
        out.append((when, e['impact'], e['title'], e.get('forecast', ''), e.get('previous', '')))
    out.sort()
    return out


def format_calendar(events):
    if not events:
        return '\n\n📅 Календарь: значимых событий по USD сегодня нет.'
    lines = ['\n\n📅 События сегодня (время CT):']
    for when, impact, title, fc, prev in events:
        mark = '🔴' if impact == 'High' else '🟡'
        extra = f' (прогноз {fc}, пред. {prev})' if fc or prev else ''
        lines.append(f'{mark} {when:%H:%M} {title}{extra}')
    lines.append('⚠️ За 30 мин до 🔴 не входить (металлы/NQ), стопы держать.')
    return '\n'.join(lines)


def build_recap():
    """Итоги дня: изменение цен торгуемых активов + события, вышедшие за день."""
    now_ct = dt.datetime.now(_ct())
    today = now_ct.date()
    lines = [f'🌇 Итоги дня {today:%d.%m.%Y}']
    for name, sym in {**RECAP_ASSETS, 'HYPE': None}.items():
        try:
            bars = fetch_any(sym or 'HYPE', days=7, interval='1h', bar_sec=3600)
            byday = {}
            for b in bars:
                d0 = dt.datetime.fromtimestamp(b[0], _ct()).date()
                byday.setdefault(d0, []).append(b)
            days = sorted(byday)
            if today not in byday or len(days) < 2:
                lines.append(f'{name}: сегодня торгов нет')
                continue
            tb = byday[today]
            prev_close = byday[days[days.index(today) - 1]][-1][4]
            last = tb[-1][4]
            hi = max(b[2] for b in tb); lo = min(b[3] for b in tb)
            chg = last / prev_close - 1
            arrow = '🟢' if chg >= 0 else '🔴'
            lines.append(f'{arrow} {name}: {last:,.2f} ({chg:+.2%}) | диапазон {lo:,.2f}–{hi:,.2f}')
        except Exception as ex:
            lines.append(f'{name}: ошибка данных ({str(ex)[:40]})')
    try:
        done = [e for e in fetch_calendar() if e[0] <= now_ct]
        if done:
            lines.append('\nВышло сегодня:')
            for when, impact, title, fc, prev in done:
                mark = '🔴' if impact == 'High' else '🟡'
                lines.append(f'{mark} {when:%H:%M} {title}')
    except Exception:
        pass
    return '\n'.join(lines)


def send_telegram(text):
    """Отправка в Telegram с ретраями (сеть после пробуждения поднимается не сразу).
    Молча пропускает, если telegram_config.json не настроен."""
    import os
    import time as _time
    if os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'):
        cfg = {'bot_token': os.environ['TELEGRAM_BOT_TOKEN'], 'chat_id': os.environ['TELEGRAM_CHAT_ID']}
    elif TG_CONFIG.exists():
        cfg = json.load(open(TG_CONFIG))
    else:
        return False
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
    payload = json.dumps({'chat_id': cfg['chat_id'], 'text': text}).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=payload,
                                         headers={**UA, 'Content-Type': 'application/json'})
            resp = json.load(urllib.request.urlopen(req, timeout=15, context=CTX))
            if resp.get('ok'):
                return True
        except Exception as ex:
            with open(LOG, 'a') as f:
                f.write(f'[telegram] попытка {attempt+1}/3 не прошла: {str(ex)[:100]}\n')
        if attempt < 2:
            _time.sleep(15)
    return False


def handle_position_cli():
    """--open COIN long|short ENTRY | --close COIN | --positions"""
    if '--open' in sys.argv:
        i = sys.argv.index('--open')
        coin, side, entry = sys.argv[i+1].upper(), sys.argv[i+2].lower(), float(sys.argv[i+3])
        p = load_positions()
        p[coin] = {'side': side, 'entry': entry, 'entry_ts': dt.datetime.now().timestamp()}
        save_positions(p)
        st = trail_position(coin, p[coin])
        print(f'{coin} {side} @ {entry} записан. Начальный стоп: {st["stop"]:.2f} (2.5xATR). '
              f'Трейлинг будет вестись автоматически, алерты в Telegram.')
        sys.exit(0)
    if '--close' in sys.argv:
        coin = sys.argv[sys.argv.index('--close')+1].upper()
        p = load_positions()
        if coin in p:
            try:
                st = trail_position(coin, p[coin])
                journal_trade(coin, p[coin], st['r_now'], 'manual')
                print(f'записано в журнал: {st["r_now"]:+.1f}R')
            except Exception:
                pass
            del p[coin]; save_positions(p)
            print(f'{coin} закрыт, трейлинг остановлен.')
        else:
            print(f'{coin} не найден. Открыты: {list(p) or "нет"}')
        sys.exit(0)
    if '--positions' in sys.argv:
        p = load_positions()
        for coin, pos in p.items():
            st = trail_position(coin, pos)
            print(f'{coin} {pos["side"]} @ {pos["entry"]} | стоп {st["stop"]:.2f} | '
                  f'{st["r_now"]:+.1f}R | {st["hours"]:.0f}ч')
        if not p:
            print('Открытых позиций нет. Добавить: --open SOL long 83.76')
        sys.exit(0)


def main():
    handle_position_cli()
    quiet = '--quiet' in sys.argv
    btc_only = '--btc-only' in sys.argv
    daily = '--daily' in sys.argv
    recap = '--recap' in sys.argv
    now = dt.datetime.now()
    state = {}
    if STATE.exists():
        try:
            state = json.load(open(STATE))
        except Exception:
            state = {}
    today_iso = now.date().isoformat()
    if '--auto' in sys.argv:
        # будни 7:15-14:59 локального (CT) - полный скан, иначе только BTC.
        # Сводка/итоги - догоняющие: шлём при ПЕРВОМ запуске после нужного времени,
        # если сегодня ещё не отправляли (переживает сон/разряженный ноут).
        in_session = now.weekday() < 5 and (7, 15) <= (now.hour, now.minute) and now.hour < 15
        btc_only = not in_session
        if now.weekday() < 5:
            if (now.hour, now.minute) >= (7, 15) and state.get('daily_sent') != today_iso:
                daily = True
                btc_only = False
            if (now.hour, now.minute) >= (14, 15) and state.get('recap_sent') != today_iso:
                recap = True
                btc_only = False
    lines = [f'=== Сканер сетапов | {now:%Y-%m-%d %H:%M} (локальное) ===']
    alerts = []
    for name, (symbol, always_open, use_be) in ASSETS.items():
        if btc_only and name != 'BTC':
            continue
        try:
            r = scan(name, symbol, always_open, use_be)
        except Exception as ex:
            lines.append(f'{name:7} ОШИБКА: {str(ex)[:80]}')
            continue
        if r.get('status') == 'СЕТАП АКТИВЕН':
            msg = (f"{name:7} >>> {r['side']} | вход ~{r['entry']:.2f} | "
                   f"стоп {r['sl']:.2f} | тейк {r['tp']:.2f}"
                   + (f" | безубыток от {r['be']:.2f}" if r['be'] else '')
                   + ' | риск 0.25-0.3%')
            alerts.append((f"{name}:{r['side']}", r['bar_ts'],
                           f"{name} {r['side']}: вход {r['entry']:.2f}, SL {r['sl']:.2f}, TP {r['tp']:.2f}"))
        else:
            msg = f"{name:7} {r.get('status', '?')}" + (f" | цена {r['price']:.2f}" if 'price' in r else '')
        lines.append(msg)
    try:
        if btc_only:
            raise StopIteration
        r = scan_nq_daytrade()
        if r.get('status') == 'СЕТАП АКТИВЕН':
            ei, si = r.get('entry_idx', r['entry']), r.get('sl_idx', r['sl'])
            atr_third = abs(r['sl'] - r['entry']) / NQ_ATR_SL / 3
            valid = ei - atr_third if r['side'] == 'ШОРТ' else ei + atr_third
            v_txt = f"годен при цене {'выше' if r['side']=='ШОРТ' else 'ниже'} {valid:.0f}, иначе ПРОПУСК"
            msg = (f"NQ-MR   >>> {r['side']} | вход ~{ei:.0f} | стоп {si:.0f} (цены NASDAQ) | "
                   f"{v_txt} | {r['exit_rule']} | риск 0.15-0.2%")
            alerts.append((f"NQ-MR:{r['side']}", r['bar_ts'],
                           f"NASDAQ дейтрейд {r['side']}: вход ~{ei:.0f}, SL {si:.0f}. "
                           f"⏳ {v_txt.capitalize()}. (по фьючерсу NQ: {r['entry']:.0f}/{r['sl']:.0f})"))
        else:
            msg = f"NQ-MR   {r.get('status', '?')}" + (f" | цена {r['price']:.2f}" if 'price' in r else '')
        lines.append(msg)
    except StopIteration:
        pass
    except Exception as ex:
        lines.append(f'NQ-MR   ОШИБКА: {str(ex)[:80]}')
    for dname, (dsym, dopen) in DAY_ASSETS.items():
        if btc_only and dname != 'BTC-DT':
            continue
        try:
            r = scan_day_pullback(dname, dsym, dopen)
            if r.get('status') == 'СЕТАП АКТИВЕН':
                exp = ' [ЭКСПЕРИМЕНТ, риск 0.1%]' if dname == 'GOLD-DT' else ' | риск 0.15-0.2%'
                msg = (f"{dname:7} >>> {r['side']} | вход ~{r['entry']:.2f} | стоп {r['sl']:.2f} | "
                       f"тейк {r['tp']:.2f} | выход не позже конца дня{exp}")
                alerts.append((f"{dname}:{r['side']}", r['bar_ts'],
                               f"{dname} {r['side']}: вход {r['entry']:.2f}, SL {r['sl']:.2f}, TP {r['tp']:.2f}"))
            else:
                msg = f"{dname:7} {r.get('status', '?')}" + (f" | цена {r['price']:.2f}" if 'price' in r else '')
            lines.append(msg)
        except Exception as ex:
            lines.append(f'{dname:7} ОШИБКА: {str(ex)[:80]}')
    positions = load_positions()
    for coin in CRYPTO_BREAKOUT:
        try:
            r = scan_breakout(coin)
            risk_txt = {'HYPE': '0.1-0.15%', 'ZEC': '0.1% (эксперимент)'}.get(coin, '0.15-0.2%')
            if r.get('status') == 'СЕТАП АКТИВЕН':
                sig_side = 'long' if r['side'] == 'ЛОНГ' else 'short'
                pos = positions.get(coin)
                if pos and pos['side'] == sig_side:
                    # уже в позиции в ту же сторону: повторный пробой = подтверждение, НЕ доливка
                    msg = f"{coin:7} в позиции ({r['side']}), повторный пробой подавлен - ведём трейлинг"
                elif pos and pos['side'] != sig_side:
                    # пробой ПРОТИВ открытой позиции - трейлинг скорее всего уже задет, но предупредим
                    msg = f"{coin:7} !!! пробой ПРОТИВ позиции ({r['side']}) - проверь стоп немедленно"
                    alerts.append((f"{coin}:reverse", r['bar_ts'],
                                   f"⚠️ {coin}: пробой 48h ПРОТИВ твоей позиции ({pos['side']}). "
                                   f"Трейлинг-стоп должен был сработать - проверь и закрой, если ещё нет."))
                else:
                    atr1 = abs(r['sl'] - r['entry']) / HYPE_ATR_TRAIL
                    valid = r['entry'] + atr1 if r['side'] == 'ЛОНГ' else r['entry'] - atr1
                    v_txt = f"годен при цене {'ниже' if r['side']=='ЛОНГ' else 'выше'} {valid:.4g}"
                    msg = (f"{coin:7} >>> {r['side']} | вход ~{r['entry']:.2f} | стоп {r['sl']:.2f} | "
                           f"{v_txt} | {r['exit_rule']} | риск {risk_txt}")
                    auto = f' ⏳ {v_txt.capitalize()}, иначе пропуск.'
                    if state.get(f"{coin}:{r['side']}") != r['bar_ts']:
                        positions[coin] = {'side': sig_side,
                                           'entry': r['entry'], 'entry_ts': r['bar_ts']}
                        save_positions(positions)
                        auto = ' Позиция взята на сопровождение (трейлинг автоматически).'
                    alerts.append((f"{coin}:{r['side']}", r['bar_ts'],
                                   f"{coin} {r['side']} (пробой 48h): вход {r['entry']:.2f}, SL {r['sl']:.2f}, дальше трейлинг.{auto}"))
            else:
                msg = f"{coin:7} {r.get('status', '?')}" + (f" | цена {r['price']:.2f}" if 'price' in r else '')
            lines.append(msg)
        except Exception as ex:
            lines.append(f'{coin:7} ОШИБКА: {str(ex)[:80]}')
    if not btc_only:
        try:
            r = scan_spacex()
            if r.get('status') == 'УРОВЕНЬ ПРОБИТ':
                msg = f"SPCX    >>> ПРОБИТ {r['level']:g} | цена {r['price']:.2f} | {r['meaning']}"
                alerts.append((f"SPCX:{r['level']:g}", r['bar_ts'],
                               f"SPACEX пробил {r['level']:g} (цена {r['price']:.2f}): {r['meaning']}"))
            else:
                msg = f"SPCX    {r.get('status', '?')}" + (f" | цена {r['price']:.2f}" if 'price' in r else '')
            lines.append(msg)
        except Exception as ex:
            lines.append(f'SPCX    ОШИБКА: {str(ex)[:80]}')
        try:
            r = scan_purr()
            if r.get('status') == 'СИГНАЛ':
                msg = f"PURR    >>> {r['side']} | {r['meaning']}"
                alerts.append((f"PURR:{r['side']}", r['bar_ts'], f"PURR {r['side']}: {r['meaning']}"))
            else:
                msg = f"PURR    {r.get('status', '?')}"
            lines.append(msg)
        except Exception as ex:
            lines.append(f'PURR    ОШИБКА: {str(ex)[:80]}')
        try:
            r = scan_kzt()
            if r.get('status') == 'УРОВЕНЬ ПРОБИТ':
                msg = f"USDKZT  >>> ПРОБИТ {r['level']:g} | курс {r['price']:.1f} | {r['meaning']}"
                alerts.append((f"USDKZT:{r['level']:g}", r['bar_ts'],
                               f"USD/KZT пробил {r['level']:g} (курс {r['price']:.1f}): {r['meaning']}"))
            else:
                msg = f"USDKZT  {r.get('status', '?')}"
            lines.append(msg)
        except Exception as ex:
            lines.append(f'USDKZT  ОШИБКА: {str(ex)[:80]}')
    try:
        cr_lines, cr_alerts = scan_crash_monitor()
        lines.extend(cr_lines)
        alerts.extend(cr_alerts)
    except Exception as ex:
        lines.append(f'CRASH   ОШИБКА: {str(ex)[:60]}')
    try:
        lo_lines, lo_alerts = scan_lottery(state)
        lines.extend(lo_lines)
        alerts.extend(lo_alerts)
    except Exception as ex:
        lines.append(f'LOTTO   ОШИБКА: {str(ex)[:60]}')
    try:
        lt_lines, lt_alerts = scan_longterm(state)
        lines.extend(lt_lines)
        alerts.extend(lt_alerts)
    except Exception as ex:
        lines.append(f'ДОЛГОСРОК ОШИБКА: {str(ex)[:60]}')
    for coin, pos in list(positions.items()):
        try:
            st = trail_position(coin, pos)
            side_ru = 'лонг' if pos['side'] == 'long' else 'шорт'
            if st['event'] == 'stop_hit':
                msg = f"TRAIL   {coin} {side_ru}: СТОП ЗАДЕТ на {st['stop']:.2f} — ЗАКРЫВАЙ (было {st['r_now']:+.1f}R)"
                alerts.append((f'trail-hit:{coin}', round(st['stop'], 4),
                               f"⛔ {coin} {side_ru}: стоп {st['stop']:.2f} задет — закрывай позицию. "
                               f"Снята со слежения автоматически."))
                d = 1 if pos['side'] == 'long' else -1
                journal_trade(coin, pos, d * (st['stop'] - pos['entry']) / st['risk'], 'stop')
                del positions[coin]; save_positions(positions)
            elif st['event'] == 'time_stop':
                msg = f"TRAIL   {coin} {side_ru}: ТАЙМ-СТОП {st['hours']:.0f}ч — закрыть по рынку ({st['r_now']:+.1f}R)"
                alerts.append((f'trail-time:{coin}', int(st['hours'] // 24),
                               f"⏰ {coin} {side_ru}: 5 суток в позиции ({st['r_now']:+.1f}R) — тайм-стоп, закрывай по рынку. "
                               f"Снята со слежения автоматически."))
                journal_trade(coin, pos, st['r_now'], 'time_stop')
                del positions[coin]; save_positions(positions)
            else:
                msg = (f"TRAIL   {coin} {side_ru} @ {pos['entry']:.2f} | стоп -> {st['stop']:.2f} | "
                       f"{st['r_now']:+.1f}R | {st['hours']:.0f}ч из 120")
                key = f'trail-move:{coin}'
                val = round(st['stop'], 4)
                if state.get(key) != val:
                    alerts.append((key, val,
                                   f"🔁 {coin} {side_ru}: передвинь стоп на {st['stop']:.2f} "
                                   f"(цена {st['price']:.2f}, {st['r_now']:+.1f}R)"))
            lines.append(msg)
        except Exception as ex:
            lines.append(f'TRAIL   {coin} ОШИБКА: {str(ex)[:80]}')
    report = '\n'.join(lines)
    if not quiet:
        print(report)
    with open(LOG, 'a') as f:
        f.write(report + '\n\n')
    try:
        if LOG.stat().st_size > 400_000:
            tail = LOG.read_text().splitlines()[-3000:]
            LOG.write_text('\n'.join(tail) + '\n')
    except Exception:
        pass
    # дедупликация: не слать повторно тот же сигнал (актив+направление) по тому же бару
    fresh = [txt for key, ts, txt in alerts if state.get(key) != ts]
    for key, ts, _ in alerts:
        state[key] = ts
    if fresh:
        import os as _os
        text = '; '.join(fresh).replace('"', "'")
        if sys.platform == 'darwin' and not _os.environ.get('CI'):
            subprocess.run(['osascript', '-e',
                            f'display notification "{text}" with title "Сетап: 4H тренд / 1H откат" sound name "Glass"'],
                           check=False)
        send_telegram('🎯 СЕТАП\n' + '\n'.join(fresh))
    if daily:
        try:
            cal = format_calendar(fetch_calendar())
        except Exception as ex:
            cal = f'\n\n📅 Календарь недоступен: {str(ex)[:60]}'
        late = ' (догоняющая - ноут спал)' if now.hour != 7 else ''
        senti = ''
        try:
            senti = sentiment_block()
            senti += judgment_block(collect_sentiment_metrics())
        except Exception:
            pass
        lt_note = monthly_report(state) + longterm_thesis_reminder(state)
        lt_note = hlp_status_line() + lt_note
        ok = send_telegram(f'📋 Утренняя сводка{late}\n' + report + cal + senti + spacex_events_block() + lt_note)
        if ok:
            state['daily_sent'] = today_iso
    if recap:
        ok = send_telegram(build_recap())
        if ok:
            state['recap_sent'] = today_iso
    STATE.write_text(json.dumps(state))


if __name__ == '__main__':
    main()
