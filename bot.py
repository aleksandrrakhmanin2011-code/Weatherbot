import asyncio
import html
import io
import json
import struct
import zlib
import lzma
import math
import logging
import os
import sqlite3
import random
import time
import csv
from collections import OrderedDict
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import aiohttp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Patch

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в переменных окружения!")
WEBHOOK_BASE_URL = "https://weatherbot-khpr.onrender.com"
DB_PATH = os.environ.get("DB_PATH", "weatherbot.db")
IDARKMETEO_API_KEY = os.environ.get("IDARKMETEO_API_KEY")
IDARKMETEO_API = "https://idarkmeteo.host/api/v1"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
ADMIN_SESSION_TTL = 12 * 60 * 60
admin_sessions = {}
PROMO_TEXT = (
    "Поддержать проект вы можете, подписавшись на "
    '<a href="https://t.me/flowdevlog">@flowdevlog</a>'
)
PROMO_PROBABILITY = 0.20
PROMO_COOLDOWN = 6 * 60 * 60
LOGO_PATH = os.path.join(os.path.dirname(__file__), "progmet_logo.png")

# Shared HTTP client + short-lived caches: avoid opening a new TCP/TLS connection
# for every command. This noticeably reduces latency and external API load.
HTTP_SESSION = None
COORD_CACHE = {}
WEATHER_CACHE = {}
AIR_CACHE = {}
FIRE_IMAGE_CACHE = {}
COORD_CACHE_TTL = 30 * 60
WEATHER_CACHE_TTL = 90
AIR_CACHE_TTL = 5 * 60
FIRE_CACHE_TTL = 5 * 60

def _cache_get(cache, key, ttl):
    item = cache.get(key)
    if not item or time.monotonic() - item[0] > ttl:
        if item:
            cache.pop(key, None)
        return None
    return item[1]

def _cache_put(cache, key, value, max_items=256):
    cache[key] = (time.monotonic(), value)
    while len(cache) > max_items:
        cache.pop(next(iter(cache)))
    return value

async def http_session():
    global HTTP_SESSION
    if HTTP_SESSION is None or HTTP_SESSION.closed:
        timeout = aiohttp.ClientTimeout(total=30, connect=8, sock_read=25)
        connector = aiohttp.TCPConnector(limit=40, limit_per_host=10, ttl_dns_cache=300, enable_cleanup_closed=True)
        HTTP_SESSION = aiohttp.ClientSession(timeout=timeout, connector=connector, headers={"User-Agent": "ProgMet/1.0"})
    return HTTP_SESSION

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class ChatAndPromoMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        chat = getattr(event, "chat", None)
        if chat is not None:
            try:
                register_chat(chat)
            except Exception:
                logging.exception("Chat registration failed")
        from_user = getattr(event, "from_user", None)
        if from_user is not None:
            try:
                register_user_presence(from_user)
            except Exception:
                logging.exception("User registration failed")
        result = await handler(event, data)
        if chat is not None and getattr(event, "text", None):
            text = event.text.strip()
            is_command = text.startswith("/")
            is_keysi = text.split(maxsplit=1)[0].split("@")[0].lower() == "/keysi" if is_command else False
            if is_command and not is_keysi and random.random() < PROMO_PROBABILITY:
                now = int(time.time())
                try:
                    last = get_promo_time(chat.id)
                    if now - last >= PROMO_COOLDOWN:
                        await bot.send_message(chat.id, PROMO_TEXT, parse_mode="HTML", disable_web_page_preview=True)
                        set_promo_time(chat.id, now)
                except Exception:
                    logging.exception("Promo message failed for chat %s", chat.id)
        return result

dp.message.outer_middleware(ChatAndPromoMiddleware())

WMO_ICONS = {
    0: "☀️ Ясно", 1: "🌤 Преимущественно ясно", 2: "⛅️ Переменная облачность", 3: "☁️ Пасмурно",
    45: "🌫 Туман", 48: "🌫 Оседающий туман", 51: "🌦 Слабая морось", 53: "🌦 Морось", 55: "🌦 Сильная морось",
    61: "🌧 Небольшой дождь", 63: "🌧 Дождь", 65: "🌧 Сильный дождь", 71: "🌨 Небольшой снег", 73: "🌨 Снег",
    75: "❄️ Сильный снег", 80: "🌦 Ливень", 81: "🌦 Сильный ливень", 82: "⛈ Очень сильный ливень",
    95: "⛈ Гроза", 96: "⛈ Гроза с градом", 99: "⛈ Сильная гроза с градом"
}
THUNDER_CODES = {95, 96, 99}

# ---------------- DB ----------------
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, city TEXT, lat REAL, lon REAL,
        region TEXT, notifications INTEGER NOT NULL DEFAULT 0,
        last_alert TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS chats(
        chat_id INTEGER PRIMARY KEY, chat_type TEXT NOT NULL,
        title TEXT, username TEXT, first_seen INTEGER NOT NULL,
        last_seen INTEGER NOT NULL, last_promo INTEGER NOT NULL DEFAULT 0
    )""")
    con.commit()
    return con

def register_chat(chat):
    now = int(time.time())
    title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or ""
    username = getattr(chat, "username", None) or ""
    con = db()
    con.execute(
        """INSERT INTO chats(chat_id,chat_type,title,username,first_seen,last_seen)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET
             chat_type=excluded.chat_type,title=excluded.title,username=excluded.username,last_seen=excluded.last_seen""",
        (chat.id, chat.type, title, username, now, now)
    )
    con.commit(); con.close()

def register_user_presence(user):
    """Ensure every person who interacts with the bot can receive broadcasts."""
    if not user:
        return
    con = db()
    con.execute(
        """INSERT INTO users(user_id,city,lat,lon,region)
           VALUES(?,?,?,?,?)
           ON CONFLICT(user_id) DO NOTHING""",
        (user.id, None, None, None, None)
    )
    con.commit(); con.close()

def list_users_for_broadcast():
    con = db()
    rows = con.execute("SELECT * FROM users ORDER BY user_id").fetchall()
    con.close()
    return rows

def list_chats():
    # All non-private chats known to the bot, not only recently active ones.
    con = db()
    rows = con.execute(
        "SELECT * FROM chats WHERE chat_type IN ('group','supergroup','channel') ORDER BY first_seen ASC"
    ).fetchall()
    con.close()
    return rows

def broadcast_targets():
    """Return every known user and every non-private chat as unique Telegram targets."""
    users = list_users_for_broadcast()
    chats = list_chats()
    targets = []
    seen = set()
    for row in users:
        uid = int(row["user_id"])
        if uid not in seen:
            seen.add(uid)
            targets.append((uid, f"пользователь {uid}"))
    for row in chats:
        cid = int(row["chat_id"])
        if cid not in seen:
            seen.add(cid)
            title = row["title"] or row["username"] or str(cid)
            targets.append((cid, title))
    return targets

def set_promo_time(chat_id, timestamp):
    con=db(); con.execute("UPDATE chats SET last_promo=? WHERE chat_id=?", (timestamp, chat_id)); con.commit(); con.close()

def get_promo_time(chat_id):
    con=db(); row=con.execute("SELECT last_promo FROM chats WHERE chat_id=?", (chat_id,)).fetchone(); con.close(); return int(row[0]) if row else 0

def get_user(user_id):
    con = db(); row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone(); con.close(); return row

def save_user(user_id, city, lat, lon, region=None):
    con = db()
    con.execute("""INSERT INTO users(user_id,city,lat,lon,region) VALUES(?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET city=excluded.city,lat=excluded.lat,lon=excluded.lon,region=excluded.region""",
        (user_id, city, lat, lon, region))
    con.commit(); con.close()

def set_notifications(user_id, enabled):
    con = db(); con.execute("UPDATE users SET notifications=? WHERE user_id=?", (1 if enabled else 0, user_id)); con.commit(); con.close()

def set_last_alert(user_id, key):
    con = db(); con.execute("UPDATE users SET last_alert=? WHERE user_id=?", (key, user_id)); con.commit(); con.close()

def notification_users():
    con = db(); rows = con.execute("SELECT * FROM users WHERE notifications=1 AND lat IS NOT NULL AND lon IS NOT NULL").fetchall(); con.close(); return rows

# ---------------- API ----------------
async def get_coordinates(city: str):
    key = " ".join(city.strip().lower().split())
    cached = _cache_get(COORD_CACHE, key, COORD_CACHE_TTL)
    if cached is not None:
        return cached
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city, "count": 1, "language": "ru", "format": "json"}
    session = await http_session()
    async with session.get(url, params=params) as resp:
        data = await resp.json()
        if data.get("results"):
            r = data["results"][0]
            region = r.get("admin1") or r.get("country") or "регион не определён"
            result = (r["latitude"], r["longitude"], r.get("name", city), region)
            return _cache_put(COORD_CACHE, key, result)
    return None, None, None, None

async def reverse_geocode(lat, lon):
    url = "https://geocoding-api.open-meteo.com/v1/reverse"
    params = {"latitude": lat, "longitude": lon, "count": 1, "language": "ru", "format": "json"}
    try:
        session = await http_session()
        async with session.get(url, params=params) as resp:
                data = await resp.json()
                r = (data.get("results") or [None])[0]
                if r:
                    return r.get("name") or "Ваш город", r.get("admin1") or r.get("country") or "регион не определён"
    except Exception:
        logging.exception("Reverse geocoding error")
    return "Ваш город", "регион не определён"

HOURLY = ",".join([
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "apparent_temperature", "precipitation", "rain",
    "showers", "snowfall", "precipitation_probability", "weather_code", "pressure_msl", "surface_pressure",
    "cloud_cover", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "cape", "convective_inhibition",
    "lifted_index", "boundary_layer_height", "freezing_level_height", "vapour_pressure_deficit"
])

async def get_weather(lat, lon, hours=48):
    key = (round(float(lat), 3), round(float(lon), 3), int(hours))
    cached = _cache_get(WEATHER_CACHE, key, WEATHER_CACHE_TTL)
    if cached is not None:
        return cached
    params = {
        "latitude": lat, "longitude": lon, "hourly": HOURLY,
        "forecast_hours": hours, "timezone": "auto",
        "temperature_unit": "celsius", "wind_speed_unit": "kmh",
        "precipitation_unit": "mm", "models": "best_match"
    }
    session = await http_session()
    async with session.get("https://api.open-meteo.com/v1/forecast", params=params) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"Open-Meteo HTTP {resp.status}: {body[:500]}")
        return _cache_put(WEATHER_CACHE, key, await resp.json(), max_items=128)

async def get_profile(lat, lon):
    # Pressure levels supported by the general Open-Meteo forecast endpoint.
    levels = "1000,975,950,925,900,850,800,750,700,650,600,550,500"
    hourly = ",".join(
        [f"wind_speed_{p}hPa" for p in levels.split(",")] +
        [f"wind_direction_{p}hPa" for p in levels.split(",")] +
        [f"geopotential_height_{p}hPa" for p in levels.split(",")]
    )
    params = {
        "latitude": lat, "longitude": lon, "hourly": hourly,
        "forecast_hours": 1, "timezone": "auto", "models": "best_match"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=20) as resp:
                return await resp.json() if resp.status == 200 else None
    except Exception:
        logging.exception("Open-Meteo profile request failed")
        return None

def wind_uv(speed_kmh, direction_deg):
    import math
    s = float(speed_kmh) / 3.6; d = math.radians(float(direction_deg))
    return -s * math.sin(d), -s * math.cos(d)

def estimate_srh_0_3km(profile):
    if not profile or "hourly" not in profile: return None
    h = profile["hourly"]; levels = [1000,975,950,925,900,850,800,750,700,650,600,550,500]; pts=[]
    for p in levels:
        try:
            z=h[f"geopotential_height_{p}hPa"][0]; sp=h[f"wind_speed_{p}hPa"][0]; dr=h[f"wind_direction_{p}hPa"][0]
            if z is not None and sp is not None and dr is not None: pts.append((float(z), *wind_uv(sp,dr)))
        except (KeyError,TypeError,ValueError,IndexError): pass
    if len(pts)<3:return None
    pts.sort(); z0=pts[0][0]
    def interp(z):
        for i in range(len(pts)-1):
            z1,u1,v1=pts[i]; z2,u2,v2=pts[i+1]
            if z1<=z<=z2:
                t=(z-z1)/(z2-z1) if z2!=z1 else 0; return u1+(u2-u1)*t,v1+(v2-v1)*t
        return pts[-1][1],pts[-1][2]
    p0,p1,p3=[interp(z) for z in (z0,z0+1000,z0+3000)]
    import math
    mean_u=(p0[0]+4*p1[0]+p3[0])/6; mean_v=(p0[1]+4*p1[1]+p3[1])/6
    su,sv=p3[0]-p0[0],p3[1]-p0[1]; sh=math.hypot(su,sv)
    sm_u,sm_v=(mean_u+7.5*(-sv/sh),mean_v+7.5*(su/sh)) if sh else (mean_u,mean_v)
    return 0.5*((p1[0]-sm_u)*(p0[1]-sm_v)-(p1[1]-sm_v)*(p0[0]-sm_u)+(p3[0]-sm_u)*(p1[1]-sm_v)-(p3[1]-sm_v)*(p1[0]-sm_u))

def current_index(data):
    times=data.get("hourly",{}).get("time",[])
    if not times:return 0
    now=datetime.now().astimezone(); best=(0,None)
    for i,t in enumerate(times):
        try:
            d=abs(datetime.fromisoformat(t).replace(tzinfo=now.tzinfo)-now)
            if best[1] is None or d<best[1]:best=(i,d)
        except ValueError:pass
    return best[0]

def safe(v,d=0):
    if v is None:return "—"
    try:return f"{float(v):.{d}f}"
    except:return "—"

def details_text(city,data,srh):
    h=data["hourly"]; i=current_index(data); g=lambda k:h.get(k,[None]*len(h.get("time",[])))[i]
    return (f"📍 <b>{html.escape(city)}</b>\n\n{WMO_ICONS.get(g('weather_code'),'❓ Неизвестно')}\n\n"
            f"🌡 Температура: <b>{safe(g('temperature_2m'),1)} °C</b>\n🤔 Ощущается: {safe(g('apparent_temperature'),1)} °C\n"
            f"💧 Влажность: {safe(g('relative_humidity_2m'))}%\n💦 Точка росы: <b>{safe(g('dew_point_2m'),1)} °C</b>\n"
            f"🧭 Давление: <b>{safe(g('pressure_msl'),1)} гПа</b>\n💨 Ветер: {safe(g('wind_speed_10m'),1)} км/ч\n"
            f"🧭 Направление: {safe(g('wind_direction_10m'))}°\n💨 Порывы: {safe(g('wind_gusts_10m'),1)} км/ч\n"
            f"☁️ Облачность: {safe(g('cloud_cover'))}%\n🌧 Осадки: {safe(g('precipitation'),1)} мм\n\n"
            f"⚡ CAPE: <b>{safe(g('cape'))} Дж/кг</b>\n🧱 CIN: {safe(g('convective_inhibition'))} Дж/кг\n"
            f"📐 Lifted Index: {safe(g('lifted_index'),1)}\n🌪 SRH 0–3 км: <b>{safe(srh)} м²/с²</b>\n"
            f"🏔 PBL: {safe(g('boundary_layer_height'))} м\n❄️ Уровень 0°C: {safe(g('freezing_level_height'))} м\n"
            f"💧 VPD: {safe(g('vapour_pressure_deficit'),2)} кПа\n\n<i>SRH — расчётная оценка по профилю ветра.</i>")


def weather_period_text(data):
    """Return the approximate end of the current weather phenomenon."""
    h = data.get("hourly", {})
    times = h.get("time", [])
    codes = h.get("weather_code", [])
    if not times or not codes:
        return "время смены не определено"
    i = current_index(data)
    current = codes[i]
    end_i = i
    # Keep the current phenomenon while the weather code remains the same.
    for j in range(i + 1, min(len(codes), i + 25)):
        if codes[j] != current:
            end_i = j
            break
        end_i = j
    try:
        dt = datetime.fromisoformat(times[end_i])
        if end_i < len(codes) - 1 and codes[end_i] != current:
            return f"примерно до {dt:%H:%M}"
        return f"ориентировочно до {dt:%d.%m %H:%M}"
    except Exception:
        return "время смены не определено"

def _font(size, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()

def _weather_icon(draw, code, x, y, scale=1.0):
    """Small monochrome icon in the visual language of the reference card."""
    c=(8,10,14,255); w=max(3,int(5*scale))
    if code in (0,1):
        draw.ellipse((x+35*scale,y+12*scale,x+85*scale,y+62*scale), outline=c, width=w)
        for a in range(0,360,45):
            import math as _m
            cx=x+60*scale; cy=y+37*scale; r1=38*scale; r2=52*scale
            x1=cx+_m.cos(_m.radians(a))*r1; y1=cy+_m.sin(_m.radians(a))*r1
            x2=cx+_m.cos(_m.radians(a))*r2; y2=cy+_m.sin(_m.radians(a))*r2
            draw.line((x1,y1,x2,y2),fill=c,width=w)
    else:
        draw.ellipse((x+25*scale,y+35*scale,x+82*scale,y+88*scale),fill=c)
        draw.ellipse((x+60*scale,y+20*scale,x+118*scale,y+88*scale),fill=c)
        draw.rounded_rectangle((x+20*scale,y+52*scale,x+125*scale,y+92*scale),radius=int(20*scale),fill=c)
        if code in THUNDER_CODES:
            draw.polygon([(x+70*scale,y+90*scale),(x+55*scale,y+128*scale),(x+78*scale,y+113*scale),(x+67*scale,y+145*scale),(x+95*scale,y+101*scale),(x+76*scale,y+112*scale)],fill=(240,190,20,255))
        elif code not in (3,45,48):
            for dx in (42,70,98):
                draw.line((x+dx*scale,y+100*scale,x+(dx-8)*scale,y+126*scale),fill=(40,80,120,255),width=w)


def make_weather_summary(city, data, region=None):
    """1280x720 summary styled after the user's reference: same dark navy/white card language and ProgMet logo."""
    W,H=1280,720
    bg=Image.new("RGB",(W,H),(18,38,60)); draw=ImageDraw.Draw(bg,"RGBA")
    # Reference-like navy header and subtle lower background.
    draw.rectangle((0,0,W,154),fill=(14,32,51,255))
    draw.rectangle((0,154,W,H),fill=(18,39,61,255))
    draw.line((0,154,W,154),fill=(31,55,79,255),width=2)
    f_title=_font(38,True); f_card=_font(27,True); f_value=_font(31,True); f_label=_font(24,False)
    f_big=_font(40,True); f_small=_font(20,False); f_footer=_font(26,True)
    draw.text((32,38),"МЕТЕОРОЛОГИЧЕСКАЯ СВОДКА ПОГОДЫ",font=f_title,fill=(246,248,250,255))
    try:
        logo=Image.open(LOGO_PATH).convert("RGBA")
        logo.thumbnail((105,105),Image.Resampling.LANCZOS)
        bg.paste(logo,(W-245,20),logo)
        draw.text((W-132,52),"PROGMET",font=_font(28,True),fill=(246,248,250,255),anchor="ma")
        draw.text((W-132,86),"БОТ",font=_font(28,True),fill=(210,216,224,255),anchor="ma")
    except Exception:
        draw.text((W-160,55),"PROGMET БОТ",font=_font(26,True),fill=(246,248,250,255),anchor="ma")

    h=data.get("hourly",{}); i=current_index(data)
    g=lambda k:h.get(k,[None]*len(h.get("time",[])))[i]
    code=g("weather_code"); desc=WMO_ICONS.get(code,"Погода")
    desc=desc.split(" ",1)[1] if " " in desc else desc
    temp=safe(g("temperature_2m"),1); pressure=safe(g("pressure_msl"),0)
    wind=safe(g("wind_speed_10m"),1); gust=safe(g("wind_gusts_10m"),1); direction=safe(g("wind_direction_10m"),0)
    # Main white card
    draw.rounded_rectangle((38,188,704,646),radius=25,fill=(238,242,247,255))
    draw.text((70,222),"Сейчас",font=f_card,fill=(16,20,27,255))
    _weather_icon(draw,code,82,295,1.0)
    draw.text((225,326),desc,font=f_big,fill=(16,20,27,255),anchor="lm")
    draw.line((70,430,670,430),fill=(150,157,165,255),width=2)
    rows=[("Температура",f"{temp} °C"),("Давление",f"{pressure} гПа"),("Ветер",f"{wind} км/ч"),("Порывы",f"{gust} км/ч"),("Направление",f"{direction}°")]
    y=455
    for label,val in rows:
        draw.text((78,y),label,font=f_label,fill=(100,107,116,255))
        draw.text((642,y),val,font=f_value,fill=(18,24,31,255),anchor="ra")
        y+=37

    # Right cards
    draw.rounded_rectangle((742,188,1242,380),radius=25,fill=(238,242,247,255))
    draw.text((775,220),"Населённый пункт",font=f_card,fill=(16,20,27,255))
    draw.text((775,273),city,font=_font(34,True),fill=(19,29,42,255))
    if region: draw.text((775,325),region,font=f_label,fill=(105,112,121,255))

    draw.rounded_rectangle((742,404,1242,646),radius=25,fill=(238,242,247,255))
    draw.text((775,435),"Ориентировочная продолжительность",font=_font(22,True),fill=(16,20,27,255))
    draw.text((775,492),weather_period_text(data),font=_font(33,True),fill=(19,29,42,255))
    try:
        dt=datetime.fromisoformat(h["time"][i])
        draw.text((775,550),f"Сейчас: {dt:%d.%m.%Y %H:%M}",font=f_label,fill=(105,112,121,255))
    except Exception: pass
    draw.text((775,595),"Время окончания зависит от обновления прогноза.",font=f_small,fill=(125,131,139,255))
    draw.rectangle((0,H-43,W,H),fill=(11,27,43,255))
    draw.text((15,H-34),"@FLOWDEVLOG",font=f_footer,fill=(245,247,250,255))
    return bg

async def get_air_quality(lat, lon):
    key=(round(float(lat),3), round(float(lon),3))
    cached=_cache_get(AIR_CACHE,key,AIR_CACHE_TTL)
    if cached is not None: return cached
    params={
        "latitude":lat,"longitude":lon,
        "hourly":"pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone,european_aqi,us_aqi,uv_index",
        "forecast_hours":1,"timezone":"auto"
    }
    session=await http_session()
    async with session.get("https://air-quality-api.open-meteo.com/v1/air-quality", params=params) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Air Quality HTTP {resp.status}")
        return _cache_put(AIR_CACHE,key,await resp.json(),max_items=128)

async def send_environment(message, city):
    lat, lon, resolved, region = await get_coordinates(city)
    if lat is None:
        await message.answer("😕 Город не найден. Попробуй ещё раз.")
        return
    try:
        aq=await get_air_quality(lat,lon)
        h=aq.get("hourly",{}); 
        def av(k):
            vals=h.get(k,[])
            return vals[0] if vals else None
        data=await get_weather(lat,lon,hours=1)
        wh=data["hourly"]; i=current_index(data); wg=lambda k: wh.get(k,[None])[i]
        aqi=av("european_aqi")
        aqi_text=safe(aqi,0)
        if aqi is None: aqi_text="—"
        uv=safe(av("uv_index"),1)
        pm25=safe(av("pm2_5"),1); pm10=safe(av("pm10"),1)
        await message.answer(
            f"🌿 <b>Окружение — {html.escape(resolved)}</b>\n\n"
            f"🌫 Европейский AQI: <b>{aqi_text}</b>\n"
            f"🫁 PM2.5: <b>{pm25} мкг/м³</b>\n"
            f"🌫 PM10: <b>{pm10} мкг/м³</b>\n"
            f"☀️ УФ-индекс: <b>{uv}</b>\n"
            f"🧪 O₃: {safe(av('ozone'),1)} мкг/м³\n"
            f"🧪 NO₂: {safe(av('nitrogen_dioxide'),1)} мкг/м³\n"
            f"🌡 Температура: <b>{safe(wg('temperature_2m'),1)} °C</b>\n\n"
            f"<i>Данные качества воздуха: Open-Meteo Air Quality.</i>",
            parse_mode="HTML", reply_markup=main_menu()
        )
    except Exception:
        logging.exception("Environment error")
        await message.answer("⚠️ Не удалось получить данные окружения. Попробуй позже.", reply_markup=main_menu())

def keyboard(lat,lon,city=None):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Подробно",callback_data=f"details:{lat:.5f}:{lon:.5f}")
    ]])

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌤 Прогноз"), KeyboardButton(text="📈 Графики")],
            [KeyboardButton(text="🌧 Радар"), KeyboardButton(text="🔥 Пожары")],
            [KeyboardButton(text="📋 Сводка")],
            [KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="🌿 Окружение")]
        ],
        resize_keyboard=True
    )

def location_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Отправить геолокацию",request_location=True)],[KeyboardButton(text="🏙 Ввести город")],[KeyboardButton(text="⬅️ Назад")]],resize_keyboard=True)

def settings_menu(user):
    city=user["city"] or "не выбран"
    n="🔔 Уведомления: ВКЛ" if user["notifications"] else "🔕 Уведомления: ВЫКЛ"
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=f"🏙 Город: {city}")],[KeyboardButton(text=n)],[KeyboardButton(text="⬅️ Главное меню")]],resize_keyboard=True)

# ---------------- charts ----------------
def make_charts(city,data):
    h=data["hourly"]; times=h["time"][:48]; dt=[datetime.fromisoformat(t) for t in times]
    fig,axes=plt.subplots(3,1,figsize=(12,8.5),sharex=True); fig.patch.set_facecolor("#10141b")
    for ax in axes:
        ax.set_facecolor("#151b24"); ax.grid(True,alpha=.16,linewidth=.7); ax.tick_params(colors="#d8dee9",labelsize=8)
        for s in ax.spines.values():s.set_color("#354052")
    axes[0].bar(dt,h["precipitation"][:48],width=.03,alpha=.75); axes[0].set_ylabel("мм",color="#d8dee9"); axes[0].set_title("Осадки за 48 часов",loc="left",color="white",fontweight="bold")
    axes[1].plot(dt,h["temperature_2m"][:48],linewidth=2.2); axes[1].axhline(0,linewidth=1,alpha=.35); axes[1].set_ylabel("°C",color="#d8dee9"); axes[1].set_title("Температура",loc="left",color="white",fontweight="bold")
    axes[2].plot(dt,h["wind_speed_10m"][:48],linewidth=2,label="Ветер"); axes[2].plot(dt,h["wind_gusts_10m"][:48],linewidth=1.5,linestyle="--",alpha=.8,label="Порывы"); axes[2].set_ylabel("км/ч",color="#d8dee9"); axes[2].set_title("Ветер",loc="left",color="white",fontweight="bold"); axes[2].legend(facecolor="#151b24",edgecolor="#354052",labelcolor="white",fontsize=8)
    axes[2].xaxis.set_major_locator(mdates.HourLocator(interval=3)); axes[2].xaxis.set_major_formatter(mdates.DateFormatter("%d.%m\n%H:%M"))
    for lab in axes[2].get_xticklabels():lab.set_color("#d8dee9")
    fig.suptitle(f"{city} — прогноз на 48 часов",color="white",fontsize=14,fontweight="bold",y=.995); fig.tight_layout(rect=[.02,.02,1,.96])
    out=io.BytesIO(); fig.savefig(out,format="png",dpi=150,bbox_inches="tight",facecolor=fig.get_facecolor()); plt.close(fig); out.seek(0); return out

async def send_forecast(message, city):
    lat,lon,resolved,region=await get_coordinates(city)
    if lat is None:
        await message.answer("😕 Город не найден. Попробуй написать иначе."); return
    data=await get_weather(lat,lon); i=current_index(data); h=data["hourly"]; g=lambda k:h[k][i]
    text=(f"📍 <b>{html.escape(resolved)}</b>\n\n{WMO_ICONS.get(g('weather_code'),'❓ Неизвестно')}\n"
          f"🌡 <b>{g('temperature_2m')} °C</b>  |  🤔 {g('apparent_temperature')} °C\n💧 {g('relative_humidity_2m')}%  |  💨 {g('wind_speed_10m')} км/ч")
    await message.answer(text,parse_mode="HTML",reply_markup=keyboard(lat,lon,resolved))

class Form(StatesGroup):
    city=State()
    setup_city=State()
    forecast_city=State()
    charts_city=State()
    radar_city=State()
    fire_city=State()
    admin_broadcast=State()


# ---------------- iDarkMeteo radar ----------------
def _idark_headers():
    if not IDARKMETEO_API_KEY:
        raise RuntimeError("IDARKMETEO_API_KEY не установлен")
    return {"X-API-Key": IDARKMETEO_API_KEY, "Accept-Encoding": "gzip"}

async def idark_get_bytes(path):
    url = f"{IDARKMETEO_API}/{path.lstrip('/')}"
    session = await http_session()
    async with session.get(url, headers=_idark_headers()) as resp:
            if resp.status == 401:
                raise RuntimeError("iDarkMeteo: неверный API-ключ")
            if resp.status == 429:
                retry = resp.headers.get("Retry-After", "позже")
                raise RuntimeError(f"iDarkMeteo: лимит запросов, повтори через {retry} с")
            resp.raise_for_status()
            return await resp.read()

async def idark_get_json(path):
    raw = await idark_get_bytes(path)
    return json.loads(raw.decode("utf-8"))

def decode_idark_rdr(raw):
    if raw[:4] != b"IDMR" or raw[4] != 1:
        raise ValueError("Неизвестный формат iDarkMeteo RDR")
    header_len = struct.unpack("<I", raw[5:9])[0]
    header = json.loads(raw[9:9 + header_len].decode("utf-8"))
    body = raw[9 + header_len:]
    lengths = header["тело"]["длины_потоков"]
    streams = []
    pos = 0
    for n in lengths:
        streams.append(body[pos:pos+n]); pos += n
    compression = header.get("тело", {}).get("жатьё", "deflate").lower()
    decompress = lzma.decompress if compression == "lzma" else zlib.decompress
    values = list(decompress(streams[0]))
    packed_lengths = decompress(streams[1])
    runs = []
    n = 0; shift = 0
    for b in packed_lengths:
        n |= (b & 127) << shift
        if b & 128:
            shift += 7
        else:
            runs.append(n); n = 0; shift = 0
    width = int(header["ширина"]); height = int(header["высота"])
    if len(values) != len(runs) or sum(runs) != width * height:
        raise ValueError("Повреждённый RDR: длины серий не совпадают с размером")
    import numpy as np
    codes = np.repeat(np.asarray(values, dtype=np.uint8), np.asarray(runs, dtype=np.int64))
    codes = codes.reshape((height, width))
    return header, codes

def mercator_x(lon):
    return 6378137.0 * math.radians(lon)

def mercator_y(lat):
    lat = max(-85.05112878, min(85.05112878, lat))
    return 6378137.0 * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))

def _rain_palette(pal):
    bands = pal.get("rain", {}).get("bands", [])
    rgba = [(0, 0, 0, 0)] * 256
    for b in bands:
        rgb = b.get("rgb", [0, 0, 0]); alpha = int(b.get("alpha", 255))
        lo = max(0, int(b.get("lo_i", 0))); hi = min(255, int(b.get("hi_i", 255)))
        for i in range(lo, hi + 1):
            rgba[i] = (int(rgb[0]), int(rgb[1]), int(rgb[2]), alpha)
    return rgba, bands

def make_radar_image(city, lat, lon, header, codes, palette):
    import numpy as np
    from matplotlib.colors import ListedColormap
    rgba, bands = _rain_palette(palette)
    cmap = ListedColormap(np.asarray(rgba, dtype=np.float32) / 255.0, name="idark_rain")
    box = header.get("рамка") or header.get("box")
    if not box:
        box = header.get("геометрия", {}).get("box")
    if not box:
        raise ValueError("В RDR отсутствует рамка")
    x0, y0, x1, y1 = map(float, box)
    cx, cy = mercator_x(lon), mercator_y(lat)
    radius = 550000.0
    ix0 = max(0, int((cx - radius - x0) / (x1 - x0) * codes.shape[1]))
    ix1 = min(codes.shape[1], int((cx + radius - x0) / (x1 - x0) * codes.shape[1]) + 1)
    # Rasters are stored north-to-south, so y index is inverted relative to Web Mercator.
    iy0 = max(0, int((y1 - (cy + radius)) / (y1 - y0) * codes.shape[0]))
    iy1 = min(codes.shape[0], int((y1 - (cy - radius)) / (y1 - y0) * codes.shape[0]) + 1)
    if ix1 - ix0 < 50 or iy1 - iy0 < 50:
        ix0, ix1, iy0, iy1 = 0, codes.shape[1], 0, codes.shape[0]
    crop = codes[iy0:iy1, ix0:ix1]
    ex0 = x0 + (x1 - x0) * ix0 / codes.shape[1]
    ex1 = x0 + (x1 - x0) * ix1 / codes.shape[1]
    ey1 = y1 - (y1 - y0) * iy0 / codes.shape[0]
    ey0 = y1 - (y1 - y0) * iy1 / codes.shape[0]
    fig, ax = plt.subplots(figsize=(10, 7), facecolor="#11161d")
    ax.set_facecolor("#11161d")
    ax.imshow(crop, cmap=cmap, interpolation="nearest", extent=[ex0, ex1, ey0, ey1], origin="upper", aspect="equal")
    ax.scatter([cx], [cy], s=55, facecolors="none", edgecolors="white", linewidths=2, zorder=5)
    ax.text(cx, cy, "  " + city, color="white", fontsize=11, weight="bold", va="center", zorder=6,
            bbox=dict(boxstyle="round,pad=.25", facecolor="#11161d", edgecolor="none", alpha=.8))
    ax.set_xlim(ex0, ex1); ax.set_ylim(ey0, ey1)
    ax.grid(True, alpha=.16, linewidth=.7)
    ax.set_xlabel("Долгота", color="#d8dee9"); ax.set_ylabel("Широта", color="#d8dee9")
    ax.tick_params(colors="#d8dee9", labelsize=8)
    # Approximate lon/lat ticks from the Mercator axes.
    xt = ax.get_xticks(); yt = ax.get_yticks()
    ax.set_xticklabels([f"{math.degrees(x/6378137.0):.1f}°" for x in xt])
    ax.set_yticklabels([f"{math.degrees(2*math.atan(math.exp(y/6378137.0))-math.pi/2):.1f}°" for y in yt])
    title = header.get("время") or header.get("time") or "последний кадр"
    fig.suptitle(f"🌧 Радар осадков — {city}", color="white", fontsize=15, weight="bold", y=.97)
    ax.set_title(f"iDarkMeteo • {title}", color="#aeb8c6", fontsize=9, pad=8)
    handles = [Patch(facecolor=(b.get("rgb", [0,0,0])[0]/255, b.get("rgb", [0,0,0])[1]/255, b.get("rgb", [0,0,0])[2]/255),
                     alpha=b.get("alpha",255)/255, label=(f"{b.get('lo')}–{b.get('hi')} мм/ч" if "lo" in b and "hi" in b else str(b.get("label", "")))) for b in bands if b.get("lo") is not None or b.get("label")]
    if handles:
        ax.legend(handles=handles, title="Легенда", loc="upper right", fontsize=7, title_fontsize=8,
                  facecolor="#151b24", edgecolor="#354052", labelcolor="white")
    fig.text(.5, .018, "iDarkMeteo", ha="center", color="#d8dee9", fontsize=10, weight="bold")
    fig.tight_layout(rect=[0, .035, 1, .94])
    out = io.BytesIO(); fig.savefig(out, format="png", dpi=130, facecolor=fig.get_facecolor(), bbox_inches="tight"); plt.close(fig); out.seek(0)
    return out

async def send_radar(message, city):
    lat, lon, resolved, _ = await get_coordinates(city)
    if lat is None:
        await message.answer("😕 Город не найден. Попробуй написать иначе."); return
    try:
        raw = await idark_get_bytes("latest/rain/wide.rdr")
        header, codes = decode_idark_rdr(raw)
        palette = await idark_get_json("palettes.json")
        img = make_radar_image(resolved, lat, lon, header, codes, palette)
        await message.answer_photo(BufferedInputFile(img.read(), filename="idarkmeteo_radar.png"),
                                   caption=f"🌧 <b>Радар — {html.escape(resolved)}</b>\n<i>iDarkMeteo</i>", parse_mode="HTML")
    except Exception as e:
        logging.exception("Radar error")
        await message.answer(f"⚠️ Не удалось загрузить радар. {html.escape(str(e))}", parse_mode="HTML")

# ---------------- admin ----------------
def admin_is_valid(user_id):
    expires = admin_sessions.get(user_id, 0)
    if expires > time.time():
        return True
    admin_sessions.pop(user_id, None)
    return False

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="👥 Все пользователи", callback_data="admin:users:0")],
        [InlineKeyboardButton(text="📋 Все чаты", callback_data="admin:chats:0")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:home")],
        [InlineKeyboardButton(text="🔒 Закрыть", callback_data="admin:close")],
    ])

def chat_keyboard(chat_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚪 Выйти из чата", callback_data=f"admin:leave:{chat_id}")],
        [InlineKeyboardButton(text="⬅️ К админ-панели", callback_data="admin:home")]
    ])

def format_chat(row):
    title = html.escape(row["title"] or "Без названия")
    username = f" @{html.escape(row['username'])}" if row["username"] else ""
    return f"<b>{title}</b>{username}\nID: <code>{row['chat_id']}</code>\nТип: {row['chat_type']}"

def admin_page_keyboard(kind, page, total, per_page=12):
    buttons = []
    last = max(0, (total - 1) // per_page)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:{kind}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{last+1}", callback_data="admin:noop"))
    if page < last:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:{kind}:{page+1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(lambda c: c.data == "admin:home")
async def admin_home(callback: types.CallbackQuery):
    if not admin_is_valid(callback.from_user.id):
        await callback.answer("Сессия истекла. Введи /keysi снова.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("🔐 <b>Админ-панель</b>\n\nВыбери действие:", parse_mode="HTML", reply_markup=admin_menu())

@dp.callback_query(lambda c: c.data == "admin:noop")
async def admin_noop(callback: types.CallbackQuery):
    await callback.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("admin:users:"))
async def admin_users(callback: types.CallbackQuery):
    if not admin_is_valid(callback.from_user.id):
        await callback.answer("Сессия истекла. Введи /keysi снова.", show_alert=True)
        return
    page = int(callback.data.rsplit(":", 1)[1])
    rows = list_users_for_broadcast()
    per_page = 12
    start = page * per_page
    chunk = rows[start:start + per_page]
    lines = [f"👥 <b>Все пользователи: {len(rows)}</b>", ""]
    for i, row in enumerate(chunk, start=start + 1):
        city = html.escape(row["city"] or "город не выбран")
        lines.append(f"{i}. <code>{row['user_id']}</code> — {city}")
    if not chunk:
        lines.append("Пользователей пока нет.")
    await callback.answer()
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=admin_page_keyboard("users", page, len(rows), per_page))

@dp.callback_query(lambda c: c.data and c.data.startswith("admin:chats:"))
async def admin_chats(callback: types.CallbackQuery):
    if not admin_is_valid(callback.from_user.id):
        await callback.answer("Сессия истекла. Введи /keysi снова.", show_alert=True)
        return
    page = int(callback.data.rsplit(":", 1)[1])
    rows = list_chats()
    per_page = 8
    start = page * per_page
    chunk = rows[start:start + per_page]
    lines = [f"📋 <b>Все чаты: {len(rows)}</b>", ""]
    for i, row in enumerate(chunk, start=start + 1):
        title = html.escape(row["title"] or row["username"] or "Без названия")
        lines.append(f"{i}. <b>{title}</b> — <code>{row['chat_id']}</code> ({row['chat_type']})")
    if not chunk:
        lines.append("Чатов пока нет.")
    await callback.answer()
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=admin_page_keyboard("chats", page, len(rows), per_page))

@dp.callback_query(lambda c: c.data and c.data.startswith("admin:leave:"))
async def admin_leave_chat(callback: types.CallbackQuery):
    if not admin_is_valid(callback.from_user.id):
        await callback.answer("Сессия истекла. Введи /keysi снова.", show_alert=True)
        return
    try:
        chat_id = int(callback.data.rsplit(":", 1)[1])
        await bot.leave_chat(chat_id)
        con=db(); con.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,)); con.commit(); con.close()
        await callback.message.edit_text("🚪 Бот вышел из этого чата.")
        await callback.answer("Готово")
    except Exception as e:
        logging.exception("Leave chat failed")
        await callback.answer("Не удалось выйти из чата", show_alert=True)


@dp.callback_query(lambda c: c.data == "admin:broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if not admin_is_valid(callback.from_user.id):
        await callback.answer("Сессия истекла. Введи /keysi снова.", show_alert=True)
        return
    await state.set_state(Form.admin_broadcast)
    await callback.answer()
    await callback.message.answer(
        "📢 <b>Рассылка</b>\n\n"
        "Отправь сюда сообщение, которое нужно разослать всем пользователям бота.\n"
        "Можно отправлять текст, фото, видео, документ и другие обычные сообщения Telegram.\n\n"
        "Для отмены: /cancel",
        parse_mode="HTML"
    )

@dp.message(Command("cancel"), Form.admin_broadcast, F.chat.type == "private")
async def admin_broadcast_cancel(message: types.Message, state: FSMContext):
    if not admin_is_valid(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Админ-сессия истекла.")
        return
    await state.clear()
    await message.answer("Рассылка отменена.", reply_markup=main_menu())

@dp.message(Form.admin_broadcast, F.chat.type == "private")
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    if not admin_is_valid(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Админ-сессия истекла.")
        return

    targets = broadcast_targets()
    status = await message.answer(
        f"📤 <b>Начинаю рассылку</b>\n\n"
        f"Пользователи и чаты: <b>{len(targets)}</b>\n"
        "Отправка идёт по всем зарегистрированным получателям.",
        parse_mode="HTML"
    )
    sent = 0
    failed = []

    for target_id, label in targets:
        if target_id == message.from_user.id:
            continue
        try:
            await bot.copy_message(
                chat_id=target_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            sent += 1
        except Exception as e:
            failed.append((label, target_id, str(e)))
            logging.warning("Broadcast failed for %s (%s): %s", label, target_id, e)
        await asyncio.sleep(0.035)

    await state.clear()
    text = [
        "📢 <b>Рассылка завершена</b>",
        "",
        f"✅ Доставлено: <b>{sent}</b>",
        f"❌ Не доставлено: <b>{len(failed)}</b>",
        f"📊 Всего получателей: <b>{len(targets)}</b>",
    ]
    if failed:
        text += ["", "<b>Кому не доставлено:</b>"]
        # Telegram messages have a length limit; show a useful first portion.
        for label, target_id, error in failed[:40]:
            short_error = html.escape(error[:120].replace("\n", " "))
            text.append(f"• {html.escape(label)} — <code>{target_id}</code>\n  <i>{short_error}</i>")
        if len(failed) > 40:
            text.append(f"… и ещё {len(failed)-40}.")

    await status.edit_text("\n".join(text), parse_mode="HTML")
    await message.answer("Главное меню", reply_markup=main_menu())

@dp.callback_query(lambda c: c.data == "admin:close")
async def admin_close(callback: types.CallbackQuery):
    admin_sessions.pop(callback.from_user.id, None)
    await callback.answer("Админ-доступ закрыт")
    await callback.message.edit_text("🔒 Админ-доступ закрыт.")

# ---------------- handlers ----------------
@dp.message(Command("start"))
async def cmd_start(message:types.Message,state:FSMContext):
    if message.chat.type != "private":
        await message.answer("В группе используй команду: /weth город\nНапример: /weth город")
        return
    user=get_user(message.from_user.id)
    if user and user["city"]:
        await message.answer(f"С возвращением! 📍 {user['city']}\nВыбери действие:",reply_markup=main_menu()); return
    await state.set_state(Form.setup_city)
    await message.answer("Привет! 🌤\nСначала выбери город для быстрого прогноза.\n\nМожно отправить геолокацию для максимальной точности или ввести название города.",reply_markup=location_menu())

@dp.message(Form.setup_city,F.location)
async def setup_location(message:types.Message,state:FSMContext):
    loc=message.location; city,region=await reverse_geocode(loc.latitude,loc.longitude); save_user(message.from_user.id,city,loc.latitude,loc.longitude,region); await state.clear()
    await message.answer(f"✅ Город сохранён: <b>{html.escape(city)}</b>\nРегион: {html.escape(region)}",parse_mode="HTML",reply_markup=main_menu())

@dp.message(Form.setup_city,F.text)
async def setup_city_text(message:types.Message,state:FSMContext):
    if message.text=="🏙 Ввести город":
        await message.answer("Напиши название города:",reply_markup=ReplyKeyboardRemove()); return
    lat,lon,city,region=await get_coordinates(message.text)
    if lat is None: await message.answer("😕 Не нашёл такой город. Попробуй ещё раз."); return
    save_user(message.from_user.id,city,lat,lon,region); await state.clear(); await message.answer(f"✅ Город сохранён: <b>{html.escape(city)}</b>\nРегион: {html.escape(region)}",parse_mode="HTML",reply_markup=main_menu())

@dp.message(Command("helps"))
async def cmd_help(message:types.Message):
    await message.answer(
        "Доступные команды:\n\n"
        "/weth город — прогноз погоды\n"
        "/rad город — карта осадков\n"
        "/fire город — карта пожаров\n"
        "/graph город — графики на 48 часов\n"
        "/sdk город — метеорологическая сводка\n"
        "/helps — список команд"
    )

@dp.message(F.text=="🌤 Прогноз",F.chat.type=="private")
async def forecast_button(message:types.Message,state:FSMContext):
    user=get_user(message.from_user.id)
    if user and user["city"]:
        await state.clear()
        await send_forecast(message, user["city"])
        await message.answer("Главное меню", reply_markup=main_menu())
        return
    await state.set_state(Form.setup_city)
    await message.answer("🏙 Сначала выбери город для прогноза:",reply_markup=location_menu())


@dp.message(F.text=="📋 Сводка",F.chat.type=="private")
async def summary_button(message:types.Message):
    user=get_user(message.from_user.id)
    if not user or not user["city"]:
        await message.answer("Сначала выбери город в настройках.", reply_markup=location_menu())
        return
    try:
        data=await get_weather(user["lat"], user["lon"])
        img=make_weather_summary(user["city"], data, user["region"])
        out=io.BytesIO(); img.save(out, format="PNG", optimize=True); out.seek(0)
        await message.answer_photo(BufferedInputFile(out.read(), filename="progmet_summary.png"),
                                   caption=f"📋 <b>Метеорологическая сводка — {html.escape(user['city'])}</b>\nКоманда: /sdk {html.escape(user['city'])}",
                                   parse_mode="HTML", reply_markup=main_menu())
    except Exception:
        logging.exception("Summary button failed")
        await message.answer("⚠️ Не удалось сформировать сводку.", reply_markup=main_menu())

@dp.message(F.text=="📈 Графики",F.chat.type=="private")
async def charts_button(message:types.Message,state:FSMContext):
    await state.set_state(Form.charts_city); await message.answer("🏙 Введи город для графиков:",reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Назад")]],resize_keyboard=True))

@dp.message(F.text=="🌧 Радар",F.chat.type=="private")
async def radar_button(message:types.Message,state:FSMContext):
    await state.set_state(Form.radar_city)
    await message.answer(
        "🏙 Введи город для радара:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ Назад")]],
            resize_keyboard=True
        )
    )

@dp.message(F.text=="🔥 Пожары",F.chat.type=="private")
async def fire_button(message:types.Message,state:FSMContext):
    await state.set_state(Form.fire_city)
    await message.answer(
        "🏙 Введи город для карты пожаров:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ Назад")]],
            resize_keyboard=True
        )
    )

@dp.message(Form.fire_city)
async def fire_city_handler(message:types.Message,state:FSMContext):
    await state.clear()
    await send_fire_map(message,message.text.strip())
    await message.answer("Главное меню",reply_markup=main_menu())

@dp.message(F.text=="⬅️ Назад",F.chat.type=="private")
async def back_any(message:types.Message,state:FSMContext):
    await state.clear()
    await message.answer("Главное меню",reply_markup=main_menu())

@dp.message(Form.forecast_city)
async def forecast_city(message:types.Message,state:FSMContext): await state.clear(); await send_forecast(message,message.text.strip()); await message.answer("Главное меню",reply_markup=main_menu())

@dp.message(Form.charts_city)
async def charts_city(message:types.Message,state:FSMContext):
    await state.clear(); lat,lon,city,_=await get_coordinates(message.text.strip())
    if lat is None: await message.answer("😕 Город не найден.",reply_markup=main_menu()); return
    data=await get_weather(lat,lon); img=make_charts(city,data); await message.answer_photo(BufferedInputFile(img.read(),filename="forecast_48h.png"),caption=f"📈 <b>{html.escape(city)}</b> — 48 часов",parse_mode="HTML"); await message.answer("Главное меню",reply_markup=main_menu())

@dp.message(Form.radar_city)
async def radar_city(message:types.Message,state:FSMContext):
    await state.clear()
    await send_radar(message, message.text.strip())
    await message.answer("Главное меню", reply_markup=main_menu())

@dp.message(F.text=="🌿 Окружение",F.chat.type=="private")
async def environment_button(message:types.Message):
    user=get_user(message.from_user.id)
    if not user or not user["city"]:
        await message.answer("Сначала выбери город в настройках.", reply_markup=location_menu())
        return
    await send_environment(message, user["city"])


@dp.message(F.text=="⚙️ Настройки",F.chat.type=="private")
async def settings(message:types.Message):
    user=get_user(message.from_user.id)
    if not user: await message.answer("Сначала нажми /start"); return
    await message.answer(f"⚙️ Настройки\n\n📍 Город: <b>{html.escape(user['city'] or 'не выбран')}</b>\n🌎 Регион: {html.escape(user['region'] or 'не определён')}",parse_mode="HTML",reply_markup=settings_menu(user))

@dp.message(F.text.startswith("🏙 Город:"),F.chat.type=="private")
async def change_city(message:types.Message,state:FSMContext):
    await state.set_state(Form.city); await message.answer("Напиши новый город или отправь геолокацию.",reply_markup=location_menu())

@dp.message(Form.city,F.location)
async def change_location(message:types.Message,state:FSMContext):
    city,region=await reverse_geocode(message.location.latitude,message.location.longitude); save_user(message.from_user.id,city,message.location.latitude,message.location.longitude,region); await state.clear(); await message.answer("✅ Город изменён.",reply_markup=settings_menu(get_user(message.from_user.id)))

@dp.message(Form.city,F.text)
async def change_city_text(message:types.Message,state:FSMContext):
    lat,lon,city,region=await get_coordinates(message.text.strip())
    if lat is None: await message.answer("😕 Город не найден."); return
    save_user(message.from_user.id,city,lat,lon,region); await state.clear(); await message.answer(f"✅ Город изменён на {html.escape(city)}.",parse_mode="HTML",reply_markup=settings_menu(get_user(message.from_user.id)))

@dp.message(F.text.regexp(r"^🔔 Уведомления: (ВКЛ|ВЫКЛ)$"),F.chat.type=="private")
async def notifications(message:types.Message):
    user=get_user(message.from_user.id)
    if not user or not user["city"]: await message.answer("Сначала выбери город."); return
    enabled=not bool(user["notifications"]); set_notifications(message.from_user.id,enabled); user=get_user(message.from_user.id)
    status="включены" if enabled else "выключены"
    await message.answer(f"🔔 Уведомления {status}.\nБуду сообщать о грозе в регионе: <b>{html.escape(user['region'] or user['city'])}</b>.",parse_mode="HTML",reply_markup=settings_menu(user))

@dp.message(F.text=="⬅️ Главное меню",F.chat.type=="private")
async def back(message:types.Message): await message.answer("Главное меню",reply_markup=main_menu())

@dp.callback_query(lambda c:c.data and c.data.startswith("details:"))
async def details_callback(callback:types.CallbackQuery):
    await callback.answer("Загружаю подробности…")
    try:
        _,lat,lon=callback.data.split(":"); data=await get_weather(float(lat),float(lon)); srh=estimate_srh_0_3km(await get_profile(float(lat),float(lon)))
        city=(callback.message.text or "").split("\n")[0].replace("📍 ","").strip(); await callback.message.answer(details_text(city,data,srh),parse_mode="HTML")
    except Exception: logging.exception("details"); await callback.message.answer("⚠️ Не удалось загрузить расширенные параметры.")


@dp.message(Command("weth"))
async def weth_command(message:types.Message):
    city = (message.text or "").partition(" ")[2].strip()
    # In private chats, /weth without a city uses the saved city.
    # In groups, the existing explicit-city behaviour is preserved.
    if not city and message.chat.type == "private":
        user = get_user(message.from_user.id)
        if user and user["city"]:
            city = user["city"]
    if not city:
        await message.answer("Использование: /weth город\nНапример: /weth Москва")
        return
    try:
        await send_forecast(message, city)
    except Exception as e:
        logging.exception("weth command")
        await message.answer(f"⚠️ Не удалось получить прогноз. {html.escape(str(e))}", parse_mode="HTML")



@dp.message(Command("sdk"))
async def sdk_command(message:types.Message):
    city=(message.text or "").partition(" ")[2].strip()
    if not city and message.chat.type == "private":
        user=get_user(message.from_user.id)
        if user and user["city"]:
            city=user["city"]
    if not city:
        await message.answer("Используй: /sdk город")
        return
    try:
        lat,lon,resolved,region=await get_coordinates(city)
        if lat is None:
            await message.answer("😕 Город не найден. Попробуй написать иначе.")
            return
        data=await get_weather(lat,lon)
        img=make_weather_summary(resolved,data,region)
        out=io.BytesIO(); img.save(out,format="PNG",optimize=True); out.seek(0)
        await message.answer_photo(
            BufferedInputFile(out.read(),filename="progmet_summary.png"),
            caption=f"📋 <b>Метеорологическая сводка — {html.escape(resolved)}</b>",
            parse_mode="HTML"
        )
    except Exception:
        logging.exception("sdk command")
        await message.answer("⚠️ Не удалось сформировать метеорологическую сводку.")


@dp.message(Command("fire"))
async def fire_command(message:types.Message):
    city = message.text.partition(" ")[2].strip()
    if not city:
        await message.answer("Использование: /fire город\nНапример: /fire город")
        return
    await send_fire_map(message, city)

@dp.message(Command("rad"))
async def group_radar(message:types.Message):
    city=(message.text or "").partition(" ")[2].strip()
    if not city:
        await message.answer("Используй: /rad город")
        return
    await send_radar(message, city)

@dp.message(Command("graph"))
async def graph_command(message:types.Message):
    # /graph город — работает в личке и группах, только по явной команде.
    city=(message.text or "").partition(" ")[2].strip()
    if not city:
        await message.answer("Используй: /graph город")
        return
    lat, lon, resolved, _ = await get_coordinates(city)
    if lat is None:
        await message.answer("😕 Город не найден. Попробуй написать иначе.")
        return
    try:
        data=await get_weather(lat, lon)
        img=make_charts(resolved, data)
        await message.answer_photo(
            BufferedInputFile(img.read(), filename="forecast_48h.png"),
            caption=f"📈 <b>{html.escape(resolved)}</b> — графики на 48 часов",
            parse_mode="HTML"
        )
    except Exception:
        logging.exception("graph command")
        await message.answer("⚠️ Не удалось построить графики. Попробуй ещё раз позже.")


# In groups, do NOT process ordinary text. In private, allow a city only during setup/explicit FSM states.
@dp.message(F.chat.type=="private")
async def private_fallback(message:types.Message):
    await message.answer("Выбери действие в меню: 🌤 Прогноз, 📋 Сводка, 📈 Графики или ⚙️ Настройки.",reply_markup=main_menu())

# ---------------- alerts ----------------
async def thunderstorm_monitor():
    while True:
        try:
            for user in notification_users():
                try:
                    data=await get_weather(user["lat"],user["lon"],hours=3); h=data["hourly"]; i=current_index(data); code=h["weather_code"][i]
                    if code in THUNDER_CODES:
                        hour=h["time"][i]; key=f"{hour}:{code}"
                        if user["last_alert"] != key:
                            await bot.send_message(user["user_id"],f"⛈ <b>Гроза!</b>\nРегион: {html.escape(user['region'] or user['city'])}\nГород: {html.escape(user['city'])}\n\nТекущие условия у выбранного города: {html.escape(WMO_ICONS.get(code,'Гроза'))}",parse_mode="HTML")
                            set_last_alert(user["user_id"],key)
                except Exception: logging.exception("Alert check failed for %s",user["user_id"])
        except Exception: logging.exception("Monitor loop")
        await asyncio.sleep(600)

async def on_startup(app:web.Application):
    await bot.set_webhook(f"{WEBHOOK_BASE_URL}/webhook",drop_pending_updates=True)
    await bot.set_my_commands([
        types.BotCommand(command="weth",description="Прогноз: /weth город"),
        types.BotCommand(command="rad",description="Радар: /rad город"),
        types.BotCommand(command="fire",description="Пожары: /fire город"),
        types.BotCommand(command="graph",description="Графики: /graph город"),
        types.BotCommand(command="sdk",description="Метеосводка: /sdk город"),
        types.BotCommand(command="helps",description="Список команд"),
    ])
    app["thunder_task"]=asyncio.create_task(thunderstorm_monitor())
    logging.info("Webhook and commands configured")

async def on_shutdown(app:web.Application):
    global HTTP_SESSION
    task=app.get("thunder_task")
    if task: task.cancel()
    await bot.delete_webhook()
    if HTTP_SESSION and not HTTP_SESSION.closed:
        await HTTP_SESSION.close()
        HTTP_SESSION=None
    await bot.session.close()

def main():
    db().close()
    app=web.Application()
    SimpleRequestHandler(dispatcher=dp,bot=bot).register(app,path="/webhook")
    setup_application(app,dp,bot=bot)
    app.router.add_get("/",lambda r:web.Response(text="Weather bot is alive"))
    web.run_app(app,host="0.0.0.0",port=int(os.environ.get("PORT",10000)),on_startup=on_startup,on_shutdown=on_shutdown)


# ---------------- NASA FIRMS fires ----------------
FIRMS_WMS = "https://firms.modaps.eosdis.nasa.gov/mapserver/wms/fires"
FIRMS_SOURCES = "fires_viirs_24"
FIRMS_API = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def _clamp_lat(lat):
    return max(-85.05112878, min(85.05112878, lat))


def _tile_xy(lon, lat, zoom):
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_rad = math.radians(_clamp_lat(lat))
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _tile_lonlat(x, y, zoom):
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def fire_map_bbox(lat, lon, zoom=7, grid=3):
    # Center a small 3x3 OSM tile mosaic on the requested city.
    tx, ty = _tile_xy(lon, lat, zoom)
    cx, cy = int(math.floor(tx)), int(math.floor(ty))
    half = grid // 2
    x0, y0 = cx - half, cy - half
    x1, y1 = cx + half + 1, cy + half + 1
    n = 2 ** zoom
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(n, x1); y1 = min(n, y1)
    west, north = _tile_lonlat(x0, y0, zoom)
    east, south = _tile_lonlat(x1, y1, zoom)
    return x0, y0, x1, y1, west, south, east, north


async def _download_image(session, url, headers=None):
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {body[:160]}")
        return await resp.read()


async def _firms_csv_points(session, key, west, south, east, north):
    """Fallback for FIRMS WMS: fetch actual hotspot records for the map bbox."""
    area = ",".join(f"{v:.6f}" for v in (west, south, east, north))
    sources = ("VIIRS_NOAA21_NRT", "VIIRS_NOAA20_NRT", "VIIRS_SNPP_NRT")
    headers = {"User-Agent": "ProgMet/1.0"}

    async def fetch(source):
        url = f"{FIRMS_API}/{key}/{source}/{area}/1"
        try:
            async with session.get(url, headers=headers) as resp:
                raw = await resp.read()
                if resp.status != 200:
                    raise RuntimeError(
                        f"{source}: HTTP {resp.status}: "
                        f"{raw.decode('utf-8', errors='replace')[:180]}"
                    )
                return list(csv.DictReader(
                    io.StringIO(raw.decode("utf-8-sig", errors="replace"))
                ))
        except Exception as exc:
            logging.warning("FIRMS CSV %s failed: %s", source, exc)
            return []

    results = await asyncio.gather(*(fetch(src) for src in sources))
    points = []
    for rows in results:
        for row in rows:
            try:
                plat = float(row["latitude"])
                plon = float(row["longitude"])
                if south <= plat <= north and west <= plon <= east:
                    points.append(row)
            except (KeyError, TypeError, ValueError):
                continue
    return points


def _draw_fire_points(base, points, west, south, east, north):
    width, height = base.size
    draw = ImageDraw.Draw(base)

    mx0, mx1 = mercator_x(west), mercator_x(east)
    my0, my1 = mercator_y(north), mercator_y(south)

    def px(lon):
        return int((mercator_x(lon) - mx0) / (mx1 - mx0) * width)

    def py(lat):
        return int((mercator_y(lat) - my0) / (my1 - my0) * height)

    for row in points:
        try:
            x, y = px(float(row["longitude"])), py(float(row["latitude"]))
            frp = float(row.get("frp") or 0)
            radius = 3 if frp < 20 else 4 if frp < 100 else 5
            draw.ellipse(
                (x-radius-1, y-radius-1, x+radius+1, y+radius+1),
                fill=(255, 255, 255, 220)
            )
            draw.ellipse(
                (x-radius, y-radius, x+radius, y+radius),
                fill=(255, 75, 45, 255)
            )
        except (TypeError, ValueError):
            continue
    return len(points)


async def make_fire_image(city, lat, lon):
    key = (
        os.getenv("FIRMS_MAP_KEY")
        or os.getenv("NASA_FIRMS_MAP_KEY")
        or os.getenv("NASA_FIRMS_API_KEY")
        or os.getenv("NASA_API_KEY")
    )
    if not key:
        raise RuntimeError(
            "Не найден FIRMS MAP_KEY. Добавь в Render → Environment "
            "переменную FIRMS_MAP_KEY с ключом NASA FIRMS."
        )

    cache_key = (round(float(lat), 2), round(float(lon), 2))
    cached = _cache_get(FIRE_IMAGE_CACHE, cache_key, FIRE_CACHE_TTL)
    if cached is not None:
        return io.BytesIO(cached)

    zoom, grid, tile_size = 7, 3, 256
    x0, y0, x1, y1, west, south, east, north = fire_map_bbox(
        lat, lon, zoom, grid
    )
    width, height = (x1-x0) * tile_size, (y1-y0) * tile_size
    base = Image.new("RGB", (width, height), (235, 235, 235))
    headers = {"User-Agent": "ProgMet/1.0"}
    session = await http_session()

    async def tile_job(tx, ty):
        try:
            data = await _download_image(
                session, OSM_TILE_URL.format(z=zoom, x=tx, y=ty), headers
            )
            tile = Image.open(io.BytesIO(data)).convert("RGB")
            return tx, ty, tile
        except Exception:
            logging.warning("OSM tile failed z=%s x=%s y=%s", zoom, tx, ty)
            return None

    tiles = await asyncio.gather(
        *(tile_job(tx, ty) for tx in range(x0, x1) for ty in range(y0, y1))
    )
    for item in tiles:
        if item:
            tx, ty, tile = item
            base.paste(tile, ((tx-x0)*tile_size, (ty-y0)*tile_size))

    # Official NASA FIRMS WMS endpoint:
    # /mapserver/wms/fires/<MAP_KEY>/
    params = {
        "REQUEST": "GetMap", "SERVICE": "WMS", "VERSION": "1.1.1",
        "LAYERS": FIRMS_SOURCES, "STYLES": "", "FORMAT": "image/png",
        "TRANSPARENT": "TRUE", "SRS": "EPSG:3857",
        "WIDTH": str(width), "HEIGHT": str(height),
        "BBOX": ",".join(map(str, [
            mercator_x(west), mercator_y(south),
            mercator_x(east), mercator_y(north)
        ])),
        "SYMBOLS": "circle", "SIZE": "6", "COLORS": "255+80+40",
    }

    overlay_data = None
    wms_error = None
    wms_url = f"{FIRMS_WMS}/{key}/"

    for attempt in range(2):
        try:
            async with session.get(wms_url, params=params, headers=headers) as resp:
                raw = await resp.read()
                if resp.status != 200:
                    raise RuntimeError(
                        f"FIRMS WMS HTTP {resp.status}: "
                        f"{raw.decode('utf-8', errors='replace')[:250]}"
                    )
                if not raw.startswith(b"\x89PNG"):
                    raise RuntimeError(
                        "FIRMS WMS вернул не PNG: " +
                        raw.decode("utf-8", errors="replace")[:250]
                    )
                test = Image.open(io.BytesIO(raw)).convert("RGBA")
                if test.getchannel("A").getbbox() is None:
                    raise RuntimeError("FIRMS WMS вернул пустой прозрачный слой")
                overlay_data = raw
                break
        except Exception as exc:
            wms_error = exc
            if attempt == 0:
                await asyncio.sleep(0.7)

    point_count = 0
    if overlay_data is not None:
        overlay = Image.open(io.BytesIO(overlay_data)).convert("RGBA")
        if overlay.size != base.size:
            overlay = overlay.resize(base.size, Image.Resampling.LANCZOS)
        base = Image.alpha_composite(base.convert("RGBA"), overlay)
    else:
        # Fallback to NASA's official Area API with actual hotspot coordinates.
        points = await _firms_csv_points(
            session, key, west, south, east, north
        )
        if not points:
            raise RuntimeError(
                "NASA FIRMS не вернул данные. "
                f"WMS: {wms_error}. Проверь FIRMS_MAP_KEY в Render."
            )
        base = base.convert("RGBA")
        point_count = _draw_fire_points(
            base, points, west, south, east, north
        )

    draw = ImageDraw.Draw(base)
    font_big, font_small = _font(26, True), _font(16)
    cx, cy = _tile_xy(lon, lat, zoom)
    px, py = int(round((cx-x0)*tile_size)), int(round((cy-y0)*tile_size))

    draw.ellipse(
        (px-8, py-8, px+8, py+8),
        fill=(255, 255, 255, 255),
        outline=(20, 30, 40, 255), width=3
    )
    draw.text(
        (px+12, py-12), f"  {city}",
        fill=(20, 30, 40, 255), font=font_small,
        stroke_width=3, stroke_fill=(255, 255, 255, 220)
    )
    draw.rectangle((0, 0, width, 52), fill=(15, 22, 30, 225))
    draw.text(
        (18, 11), f"🔥 Пожары — {city}",
        fill=(255, 255, 255, 255), font=font_big
    )
    draw.rectangle(
        (0, height-34, width, height), fill=(15, 22, 30, 225)
    )
    footer = (
        f"NASA FIRMS • VIIRS • 24 ч • {point_count} очагов"
        if point_count else
        "NASA FIRMS • VIIRS • последние 24 часа"
    )
    draw.text(
        (14, height-27), footer,
        fill=(235, 240, 245, 255), font=font_small
    )

    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG", optimize=True)
    raw = out.getvalue()
    _cache_put(FIRE_IMAGE_CACHE, cache_key, raw, max_items=32)
    return io.BytesIO(raw)

async def send_fire_map(message:types.Message, city:str):
    try:
        lat, lon, resolved, region = await get_coordinates(city)
        if lat is None:
            await message.answer("😕 Город не найден. Попробуй написать иначе.")
            return
        img = await make_fire_image(resolved, lat, lon)
        await message.answer_photo(
            BufferedInputFile(img.read(), filename="firms_fires.png"),
            caption=(f"🔥 <b>Пожары — {html.escape(resolved)}</b>\n"
                     "NASA FIRMS • VIIRS • последние 24 часа"),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.exception("FIRMS fire map error")
        await message.answer(
            f"⚠️ Не удалось загрузить карту пожаров. {html.escape(str(e))}",
            parse_mode="HTML"
        )


if __name__=="__main__": main()
