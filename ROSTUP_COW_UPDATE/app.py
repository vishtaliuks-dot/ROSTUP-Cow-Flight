import os, json, time, hmac, hashlib, sqlite3, uuid, math
from urllib.parse import parse_qsl
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()
BASE = Path(__file__).resolve().parent
DB_PATH = os.getenv('DATABASE_PATH', str(BASE / 'rostup.db'))
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '').strip()
PUBLIC_URL = os.getenv('PUBLIC_URL', '').rstrip('/')

app = FastAPI(title='ROSTUP Cow Flight API', version='3.0.0')
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')

PRODUCTS = {}  # В версии COW FLIGHT магазин отключён.
COW_CRASH_MIN_SECONDS = max(2.0, float(os.getenv('COW_CRASH_MIN_SECONDS', '3.0')))
COW_CRASH_MAX_SECONDS = max(COW_CRASH_MIN_SECONDS + 1.0, float(os.getenv('COW_CRASH_MAX_SECONDS', '12.0')))
ALLOWED_BETS = {10, 25, 50, 100, 250, 500, 1000}
COW_MULTIPLIER_RATE = 0.08


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
          points INTEGER NOT NULL DEFAULT 10, tower_level INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rounds (
          id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, stake INTEGER NOT NULL,
          started_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          result TEXT, payout INTEGER DEFAULT 0, finished_at REAL
        );
        CREATE TABLE IF NOT EXISTS orders (
          payload TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, product_id TEXT NOT NULL,
          amount INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created_at INTEGER NOT NULL, charge_id TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS star_payments (
          payload TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, amount INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL, charge_id TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS arcade_rounds (
          id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, game TEXT NOT NULL,
          stake INTEGER NOT NULL, state_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          created_at REAL NOT NULL, finished_at REAL
        );
        CREATE TABLE IF NOT EXISTS inventory (
          telegram_id INTEGER NOT NULL, product_id TEXT NOT NULL, purchased_at INTEGER NOT NULL,
          PRIMARY KEY (telegram_id, product_id)
        );
        CREATE TABLE IF NOT EXISTS promo_redemptions (
          telegram_id INTEGER NOT NULL, code TEXT NOT NULL, redeemed_at INTEGER NOT NULL,
          PRIMARY KEY (telegram_id, code)
        );
        CREATE TABLE IF NOT EXISTS case_opens (
          id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, case_id TEXT NOT NULL,
          cost INTEGER NOT NULL, reward INTEGER NOT NULL, opened_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_case_claims (
          telegram_id INTEGER PRIMARY KEY, claimed_at INTEGER NOT NULL, reward INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS withdrawal_requests (
          id TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL, amount INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL
        );
        ''')
        # Migrate databases created by the previous ROSTUP build.
        c.execute('CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)')
        if not c.execute('SELECT 1 FROM app_migrations WHERE name=?', ('starting_balance_10',)).fetchone():
            c.execute('UPDATE users SET points=10 WHERE points=250')
            c.execute('INSERT INTO app_migrations(name,applied_at) VALUES(?,?)', ('starting_balance_10', int(time.time())))
        columns = [r['name'] for r in c.execute('PRAGMA table_info(users)').fetchall()]
        if 'tower_level' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN tower_level INTEGER NOT NULL DEFAULT 0')

init_db()


def verify_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(503, 'BOT_TOKEN не настроен на сервере')
    if not init_data:
        raise HTTPException(401, 'Открой приложение через Telegram')
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop('hash', None)
    if not received_hash:
        raise HTTPException(401, 'Нет подписи Telegram')
    auth_date = int(values.get('auth_date', '0') or 0)
    if auth_date < int(time.time()) - 86400 or auth_date > int(time.time()) + 60:
        raise HTTPException(401, 'Сессия Telegram устарела. Перезапусти Mini App.')
    data_check = '\n'.join(f'{k}={v}' for k, v in sorted(values.items()))
    secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, 'Неверная подпись Telegram')
    try:
        user = json.loads(values.get('user', '{}'))
        uid = int(user['id'])
    except Exception:
        raise HTTPException(401, 'Не удалось определить пользователя Telegram')
    return {'id': uid, 'username': user.get('username'), 'first_name': user.get('first_name', 'Игрок')}


async def current_user(request: Request):
    data = verify_init_data(request.headers.get('X-Telegram-Init-Data', ''))
    with db() as c:
        c.execute('INSERT OR IGNORE INTO users(telegram_id, username, first_name, points, created_at) VALUES(?,?,?,?,?)',
                  (data['id'], data['username'], data['first_name'], 10, int(time.time())))
        c.execute('UPDATE users SET username=?, first_name=? WHERE telegram_id=?',
                  (data['username'], data['first_name'], data['id']))
    return data


async def telegram(method: str, payload: dict):
    if not BOT_TOKEN:
        raise HTTPException(503, 'Добавь BOT_TOKEN в переменные окружения Render')
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}', json=payload)
    data = r.json()
    if not data.get('ok'):
        raise HTTPException(502, data.get('description', 'Ошибка Telegram Bot API'))
    return data['result']

WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '').strip() or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()

@app.on_event('startup')
async def register_telegram_webhook():
    if BOT_TOKEN and PUBLIC_URL:
        try:
            await telegram('setWebhook', {
                'url': f'{PUBLIC_URL}/telegram/webhook',
                'secret_token': WEBHOOK_SECRET,
                'allowed_updates': ['message', 'pre_checkout_query']
            })
        except Exception as exc:
            print(f'Telegram webhook setup failed: {exc}')



class StarTopupRequest(BaseModel):
    amount: int

@app.post('/api/payments/stars/invoice')
async def create_stars_invoice(body: StarTopupRequest, request: Request):
    user = await current_user(request)
    if body.amount not in {10, 25, 50, 100, 250, 500, 1000}:
        raise HTTPException(400, 'Выбери один из доступных пакетов пополнения')
    payload = f"topup:{user['id']}:{body.amount}:{uuid.uuid4().hex}"
    with db() as c:
        c.execute('INSERT INTO star_payments(payload,telegram_id,amount,created_at) VALUES(?,?,?,?)',
                  (payload, user['id'], body.amount, int(time.time())))
    link = await telegram('createInvoiceLink', {
        'title': f'Пополнение {body.amount} очков',
        'description': f'{body.amount} Telegram Stars = {body.amount} игровых очков ROSTUP',
        'payload': payload,
        'provider_token': '',
        'currency': 'XTR',
        'prices': [{'label': f'{body.amount} игровых очков', 'amount': body.amount}],
    })
    return {'invoice_link': link, 'stars': body.amount, 'points': body.amount}

@app.post('/telegram/webhook')
async def telegram_webhook(request: Request, secret_token: Optional[str] = Header(default=None, alias='X-Telegram-Bot-Api-Secret-Token')):
    if not WEBHOOK_SECRET or not hmac.compare_digest(secret_token or '', WEBHOOK_SECRET):
        raise HTTPException(403, 'Forbidden')
    update = await request.json()
    pre = update.get('pre_checkout_query')
    if pre:
        payload = pre.get('invoice_payload', '')
        with db() as c:
            row = c.execute('SELECT telegram_id,amount,status FROM star_payments WHERE payload=?', (payload,)).fetchone()
        valid = bool(row and row['status'] == 'pending'
                     and row['telegram_id'] == pre.get('from', {}).get('id')
                     and pre.get('currency') == 'XTR'
                     and pre.get('total_amount') == row['amount'])
        answer = {'pre_checkout_query_id': pre['id'], 'ok': valid}
        if not valid:
            answer['error_message'] = 'Счёт недействителен или уже обработан. Создай новый.'
        await telegram('answerPreCheckoutQuery', answer)
        return {'ok': True}
    message = update.get('message') or update.get('edited_message') or {}
    # Respond to ordinary bot commands. The Mini App server also acts as the Telegram bot webhook.
    text = (message.get('text') or '').strip()
    chat_id = (message.get('chat') or {}).get('id')
    if chat_id and text.split(' ', 1)[0].split('@', 1)[0] in ('/start', '/help'):
        if not PUBLIC_URL:
            await telegram('sendMessage', {
                'chat_id': chat_id,
                'text': 'Бот запущен, но администратору нужно добавить PUBLIC_URL в Render → Environment.'
            })
        else:
            await telegram('sendMessage', {
                'chat_id': chat_id,
                'text': '🐄 Добро пожаловать в ROSTUP Cow Flight! Нажми кнопку ниже, чтобы открыть игру.',
                'reply_markup': {
                    'inline_keyboard': [[{
                        'text': '🚀 Открыть ROSTUP',
                        'web_app': {'url': PUBLIC_URL}
                    }]]
                }
            })
        return {'ok': True}
    payment = message.get('successful_payment')
    if payment:
        payload = payment.get('invoice_payload', '')
        uid = int(message.get('from', {}).get('id', 0))
        charge_id = payment.get('telegram_payment_charge_id', '')
        amount = int(payment.get('total_amount', 0))
        if payment.get('currency') != 'XTR' or not charge_id:
            return {'ok': True}
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT telegram_id,amount,status FROM star_payments WHERE payload=?', (payload,)).fetchone()
            if row and row['telegram_id'] == uid and row['amount'] == amount and row['status'] == 'pending':
                c.execute('INSERT OR IGNORE INTO users(telegram_id,username,first_name,points,created_at) VALUES(?,?,?,?,?)',
                          (uid, message.get('from', {}).get('username'), message.get('from', {}).get('first_name', 'Игрок'), 10, int(time.time())))
                c.execute('UPDATE users SET points=points+? WHERE telegram_id=?', (amount, uid))
                c.execute('UPDATE star_payments SET status=?,charge_id=? WHERE payload=?',
                          ('paid', charge_id, payload))
        return {'ok': True}
    return {'ok': True}

class BetRequest(BaseModel):
    stake: int = Field(ge=10, le=1000)

class DropRequest(BaseModel):
    round_id: str

class InvoiceRequest(BaseModel):
    product_id: str


@app.get('/')
async def index():
    return FileResponse(BASE / 'index.html')

@app.get('/api/health')
async def health():
    return {'ok': True, 'service': 'ROSTUP Cow Flight'}

@app.get('/api/me')
async def me(request: Request):
    user = await current_user(request)
    with db() as c:
        row = c.execute('SELECT points, tower_level FROM users WHERE telegram_id=?', (user['id'],)).fetchone()
        owned = [r['product_id'] for r in c.execute('SELECT product_id FROM inventory WHERE telegram_id=?', (user['id'],)).fetchall()]
    return {'user': {'id': user['id'], 'first_name': user['first_name'], 'username': user['username']},
            'points': row['points'], 'tower_level': row['tower_level'], 'owned': owned, 'products': PRODUCTS}

class ArcadeStartRequest(BaseModel):
    stake: int = Field(ge=10, le=1000)

class ArcadeActionRequest(BaseModel):
    round_id: str

@app.post('/api/cow/start')
async def cow_start(body: ArcadeStartRequest, request: Request):
    user = await current_user(request)
    rid = uuid.uuid4().hex
    now = time.time()
    # Each round rolls independently: 88% low crash (1.10x-1.12x),
    # 10% reaches 2.00x, and 2% reaches 5.00x. Repeats are possible.
    import random
    roll = random.random()
    if roll < (1 / 20):
        crash_multiplier = 3.00
    elif roll < (1 / 20 + 1 / 7):
        crash_multiplier = 2.00
    else:
        crash_multiplier = round(random.uniform(1.10, 1.40), 2)
    crash_after = round((crash_multiplier - 1.0) / COW_MULTIPLIER_RATE, 4)
    state = {'crash_after': crash_after, 'crash_multiplier': crash_multiplier, 'started_at': now}
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        active = c.execute("SELECT id FROM arcade_rounds WHERE telegram_id=? AND status='active'", (user['id'],)).fetchone()
        house_active = c.execute("SELECT id FROM rounds WHERE telegram_id=? AND status='active'", (user['id'],)).fetchone()
        row = c.execute('SELECT points FROM users WHERE telegram_id=?', (user['id'],)).fetchone()
        if active or house_active: raise HTTPException(409, 'Сначала заверши текущий раунд')
        if row['points'] < body.stake: raise HTTPException(400, 'Недостаточно игровых очков')
        c.execute('UPDATE users SET points=points-? WHERE telegram_id=?', (body.stake, user['id']))
        c.execute('INSERT INTO arcade_rounds(id,telegram_id,game,stake,state_json,status,created_at) VALUES(?,?,?,?,?,?,?)',
                  (rid,user['id'],'cow',body.stake,json.dumps(state),'active',now))
    return {'round_id':rid,'stake':body.stake,'started_at':now,'crash_after':crash_after,'multiplier_rate':COW_MULTIPLIER_RATE,'balance':row['points']-body.stake}


@app.post('/api/cow/crash')
async def cow_crash(body: ArcadeActionRequest, request: Request):
    user = await current_user(request)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        rnd = c.execute("SELECT * FROM arcade_rounds WHERE id=? AND telegram_id=? AND game='cow' AND status='active'", (body.round_id,user['id'])).fetchone()
        if not rnd: raise HTTPException(404,'Раунд уже завершён')
        st = json.loads(rnd['state_json'])
        elapsed = max(0,time.time()-st['started_at'])
        if elapsed + 0.15 < st['crash_after']:
            raise HTTPException(409,'Коровка ещё летит')
        st['result']='crash'; st['payout']=0
        c.execute("UPDATE arcade_rounds SET status='finished',state_json=?,finished_at=? WHERE id=?",
                  (json.dumps(st),time.time(),body.round_id))
        balance=c.execute('SELECT points FROM users WHERE telegram_id=?',(user['id'],)).fetchone()['points']
    return {'result':'crash','payout':0,'balance':balance,'message':'Коровка упала. Ставка потеряна.'}

@app.post('/api/cow/cashout')
async def cow_cashout(body: ArcadeActionRequest, request: Request):
    user = await current_user(request)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        rnd = c.execute("SELECT * FROM arcade_rounds WHERE id=? AND telegram_id=? AND game='cow' AND status='active'",(body.round_id,user['id'])).fetchone()
        if not rnd: raise HTTPException(404,'Раунд уже завершён')
        st = json.loads(rnd['state_json'])
        elapsed = max(0,time.time()-st['started_at'])
        if elapsed >= st['crash_after']:
            payout=0; result='crash'
        else:
            mult = min(12.0, 1.0 + elapsed*COW_MULTIPLIER_RATE)
            payout = int(rnd['stake']*mult); result='cashout'
            c.execute('UPDATE users SET points=points+? WHERE telegram_id=?',(payout,user['id']))
        c.execute("UPDATE arcade_rounds SET status='finished',state_json=?,finished_at=? WHERE id=?",
                  (json.dumps({**st,'result':result,'payout':payout}),time.time(),body.round_id))
        balance=c.execute('SELECT points FROM users WHERE telegram_id=?',(user['id'],)).fetchone()['points']
    return {'result':result,'payout':payout,'balance':balance,'elapsed':round(elapsed,2),
            'message':('Корова долетела! Выплата начислена.' if result=='cashout' else 'Коровка сломалась и упала. Ставка потеряна.')}



class PromoRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)

class CaseRequest(BaseModel):
    case_id: str

class WithdrawalRequest(BaseModel):
    amount: int = Field(ge=150, le=100000000)

@app.get('/api/economy/status')
async def economy_status(request: Request):
    user = await current_user(request)
    now = int(time.time())
    with db() as c:
        row = c.execute('SELECT claimed_at,reward FROM daily_case_claims WHERE telegram_id=?',(user['id'],)).fetchone()
        last = row['claimed_at'] if row else 0
        balance = c.execute('SELECT points FROM users WHERE telegram_id=?',(user['id'],)).fetchone()['points']
    return {'balance':balance,'daily_available':now-last>=86400,'daily_seconds_left':max(0,86400-(now-last)),
            'withdraw_minimum':150,'channel_configured':bool(os.getenv('CHANNEL_USERNAME','@RostUpOnline').strip())}

# Weighted prizes are virtual game points. Each table totals exactly 100%.
CASE_CONFIG = {
 'bronze': (10, [(2,35),(5,30),(8,20),(10,10),(20,3),(50,2)]),
 'silver': (20, [(5,35),(10,30),(15,15),(20,10),(40,6),(100,3),(200,1)]),
 'diamond': (50, [(5,30),(10,25),(20,20),(30,10),(50,8),(100,4),(250,2),(500,1)])
}

def weighted_prize(entries):
    import random
    return random.choices([x[0] for x in entries],weights=[x[1] for x in entries],k=1)[0]

@app.post('/api/cases/daily')
async def daily_case(request: Request):
    user=await current_user(request); now=int(time.time())
    prize=weighted_prize([(1,25),(2,25),(3,22),(5,19.5),(10,7.5),(20,1)])
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT claimed_at FROM daily_case_claims WHERE telegram_id=?',(user['id'],)).fetchone()
        if old and now-old['claimed_at']<86400: raise HTTPException(429,'Бесплатный кейс доступен раз в 24 часа')
        c.execute('INSERT INTO daily_case_claims(telegram_id,claimed_at,reward) VALUES(?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET claimed_at=excluded.claimed_at,reward=excluded.reward',(user['id'],now,prize))
        c.execute('UPDATE users SET points=points+? WHERE telegram_id=?',(prize,user['id']))
        bal=c.execute('SELECT points FROM users WHERE telegram_id=?',(user['id'],)).fetchone()['points']
    return {'reward':prize,'balance':bal,'next_in_seconds':86400}

@app.post('/api/cases/open')
async def open_case(body: CaseRequest, request: Request):
    user=await current_user(request)
    if body.case_id not in CASE_CONFIG: raise HTTPException(400,'Неизвестный кейс')
    cost,entries=CASE_CONFIG[body.case_id]; prize=weighted_prize(entries); now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        bal=c.execute('SELECT points FROM users WHERE telegram_id=?',(user['id'],)).fetchone()['points']
        if bal<cost: raise HTTPException(400,'Недостаточно очков для открытия кейса')
        c.execute('UPDATE users SET points=points-?+? WHERE telegram_id=?',(cost,prize,user['id']))
        c.execute('INSERT INTO case_opens(id,telegram_id,case_id,cost,reward,opened_at) VALUES(?,?,?,?,?,?)',(uuid.uuid4().hex,user['id'],body.case_id,cost,prize,now))
        newbal=c.execute('SELECT points FROM users WHERE telegram_id=?',(user['id'],)).fetchone()['points']
    return {'case_id':body.case_id,'cost':cost,'reward':prize,'balance':newbal}

@app.post('/api/promo/redeem')
async def redeem_promo(body: PromoRequest, request: Request):
    user=await current_user(request); code=body.code.strip().upper()
    codes={'NEWBOT':10,'SUPERSTARS':10}
    if code not in codes: raise HTTPException(400,'Промокод не найден')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        try: c.execute('INSERT INTO promo_redemptions(telegram_id,code,redeemed_at) VALUES(?,?,?)',(user['id'],code,int(time.time())))
        except sqlite3.IntegrityError: raise HTTPException(409,'Ты уже использовал этот промокод')
        c.execute('UPDATE users SET points=points+? WHERE telegram_id=?',(codes[code],user['id']))
        bal=c.execute('SELECT points FROM users WHERE telegram_id=?',(user['id'],)).fetchone()['points']
    return {'code':code,'reward':codes[code],'balance':bal}

@app.post('/api/bonus/subscribe')
async def subscription_bonus(request: Request):
    user=await current_user(request); channel=os.getenv('CHANNEL_USERNAME','@RostUpOnline').strip()
    if not channel: raise HTTPException(503,'Администратор ещё не указал CHANNEL_USERNAME в Render')
    member=await telegram('getChatMember',{'chat_id':channel,'user_id':user['id']})
    if not (member.get('status') in ('member','administrator','creator') or (member.get('status') == 'restricted' and member.get('is_member') is True)):
        raise HTTPException(403,'Сначала подпишись на канал @RostUpOnline, затем нажми «Проверить подписку».')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        try: c.execute('INSERT INTO promo_redemptions(telegram_id,code,redeemed_at) VALUES(?,?,?)',(user['id'],'SUBSCRIBE_CHANNEL',int(time.time())))
        except sqlite3.IntegrityError: raise HTTPException(409,'Бонус за подписку уже получен')
        c.execute('UPDATE users SET points=points+2 WHERE telegram_id=?',(user['id'],))
        bal=c.execute('SELECT points FROM users WHERE telegram_id=?',(user['id'],)).fetchone()['points']
    return {'reward':2,'balance':bal}

@app.post('/api/withdraw/request')
async def request_withdrawal(body: WithdrawalRequest, request: Request):
    user=await current_user(request)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        bal=c.execute('SELECT points FROM users WHERE telegram_id=?',(user['id'],)).fetchone()['points']
        pending=c.execute("SELECT COALESCE(SUM(amount),0) AS total FROM withdrawal_requests WHERE telegram_id=? AND status='pending'",(user['id'],)).fetchone()['total']
        if pending+body.amount>bal: raise HTTPException(400,'Сумма заявки вместе с ожидающими заявками превышает баланс')
        if body.amount<150: raise HTTPException(400,'Минимальная сумма заявки — 150 очков')
        rid=uuid.uuid4().hex[:12]
        c.execute('INSERT INTO withdrawal_requests(id,telegram_id,amount,created_at) VALUES(?,?,?,?)',(rid,user['id'],body.amount,int(time.time())))
    return {'request_id':rid,'amount':body.amount,'admin':'@Operator_Aslan','message':'Заявка создана. Напиши администратору для ручной проверки.'}

@app.get('/terms')
async def terms():
    return JSONResponse({'terms': 'Игровые очки виртуальные. Минимальная заявка на ручное рассмотрение администратором — 150 очков. Создание заявки не гарантирует выплату или обмен очков на деньги.'})
