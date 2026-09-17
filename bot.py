import asyncio
import html
import json
import io
import logging
import os
import sqlite3
import struct
import zlib

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, timezone

import aiohttp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
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

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

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
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, city TEXT, lat REAL, lon REAL,
        region TEXT, notifications INTEGER NOT NULL DEFAULT 0,
        last_alert TEXT
    )""")
    con.commit()
    return con

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
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city, "count": 1, "language": "ru", "format": "json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=15) as resp:
            data = await resp.json()
            if data.get("results"):
                r = data["results"][0]
                region = r.get("admin1") or r.get("country") or "регион не определён"
                return r["latitude"], r["longitude"], r.get("name", city), region
    return None, None, None, None

async def reverse_geocode(lat, lon):
    url = "https://geocoding-api.open-meteo.com/v1/reverse"
    params = {"latitude": lat, "longitude": lon, "count": 1, "language": "ru", "format": "json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=15) as resp:
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
    params = {
        "latitude": lat, "longitude": lon, "hourly": HOURLY,
        "forecast_hours": hours, "past_hours": 0, "timezone": "auto",
        "temperature_unit": "celsius", "wind_speed_unit": "kmh", "precipitation_unit": "mm"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=20) as resp:
            resp.raise_for_status(); return await resp.json()

async def get_profile(lat, lon):
    levels = "1000,975,950,925,900,850,800,750,700,650,600,550,500"
    hourly = ",".join([f"wind_speed_{p}hPa" for p in levels.split(",")] + [f"wind_direction_{p}hPa" for p in levels.split(",")] + [f"geopotential_height_{p}hPa" for p in levels.split(",")])
    params = {"latitude": lat, "longitude": lon, "hourly": hourly, "forecast_hours": 1, "timezone": "auto"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=20) as resp:
                return await resp.json() if resp.status == 200 else None
    except Exception: return None

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

def keyboard(lat,lon,city=None):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Подробно",callback_data=f"details:{lat:.5f}:{lon:.5f}")
    ]])

def main_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🌤 Прогноз"),KeyboardButton(text="📈 Графики")],[KeyboardButton(text="⚙️ Настройки")]],resize_keyboard=True)

def location_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📍 Отправить геолокацию",request_location=True)],[KeyboardButton(text="🏙 Ввести город")],[KeyboardButton(text="⬅️ Назад")]],resize_keyboard=True)

def settings_menu(user):
    city=user["city"] or "не выбран"
    n="🔔 Уведомления: ВКЛ" if user["notifications"] else "🔕 Уведомления: ВЫКЛ"
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=f"🏙 Город: {city}")],[KeyboardButton(text=n)],[KeyboardButton(text="⬅️ Главное меню")]],resize_keyboard=True)

# ---------------- iDarkMeteo radar ----------------
async def get_idark_json(path):
    if not IDARKMETEO_API_KEY:
        raise RuntimeError("IDARKMETEO_API_KEY не установлен")
    headers = {"X-API-Key": IDARKMETEO_API_KEY}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(f"{IDARKMETEO_API}/{path.lstrip('/')}", timeout=25) as resp:
            resp.raise_for_status()
            return await resp.json()

async def get_idark_bytes(path):
    if not IDARKMETEO_API_KEY:
        raise RuntimeError("IDARKMETEO_API_KEY не установлен")
    headers = {"X-API-Key": IDARKMETEO_API_KEY}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(f"{IDARKMETEO_API}/{path.lstrip('/')}", timeout=40) as resp:
            resp.raise_for_status()
            return await resp.read()

def decode_rdr(raw):
    if raw[:4] != b"IDMR" or raw[4] != 1:
        raise ValueError("Неизвестный формат iDarkMeteo RDR")
    header_len = struct.unpack("<I", raw[5:9])[0]
    header = json.loads(raw[9:9 + header_len].decode("utf-8"))
    body = raw[9 + header_len:]
    chunks, pos = [], 0
    for n in header["тело"]["длины_потоков"]:
        chunks.append(body[pos:pos + n]); pos += n
    compression = header["тело"].get("жатьё", "deflate")
    if compression != "deflate":
        raise ValueError(f"Неподдерживаемое сжатие RDR: {compression}")
    values = np.frombuffer(zlib.decompress(chunks[0]), dtype=np.uint8)
    if len(chunks) >= 3:
        palette_raw = zlib.decompress(chunks[2])
        bpc = int(header.get("тело", {}).get("палитра", 4) or 4)
        if bpc >= 4 and len(palette_raw) >= 256 * bpc:
            pal = np.frombuffer(palette_raw[:256*bpc], dtype=np.uint8).reshape(256, bpc)
            header["_palette"] = pal[:, :4].tolist()
    lengths_raw = zlib.decompress(chunks[1])
    lengths, n, shift = [], 0, 0
    for b in lengths_raw:
        n |= (b & 127) << shift
        if b & 128:
            shift += 7
        else:
            lengths.append(n); n = 0; shift = 0
    codes = np.repeat(values, lengths).reshape(header["высота"], header["ширина"])
    return header, codes

def rdr_rgba(header, codes):
    table = header.get("значения", {}).get("таблица", {})
    palette = np.zeros((256, 4), dtype=np.uint8)
    for k, v in table.items():
        try:
            idx = int(k)
            if isinstance(v, dict):
                rgb = v.get("rgb", [0, 0, 0]); alpha = v.get("alpha", 255)
                palette[idx] = [int(rgb[0]), int(rgb[1]), int(rgb[2]), int(alpha)]
        except (TypeError, ValueError, IndexError):
            pass
    # The RDR format stores the actual 256-color RGBA palette in stream #3.
    # This fallback is filled by decode_rdr when available.
    if not palette.any():
        p = header.get("_palette")
        if isinstance(p, list):
            for i, rgba in enumerate(p[:256]):
                if len(rgba) >= 4: palette[i] = rgba[:4]
    return palette[codes]

def radar_legend(header, palette_data=None):
    title = header.get("заголовок") or header.get("title") or "Осадки"
    units = header.get("единицы") or header.get("units") or "мм/ч"
    bands = []
    source = (palette_data or {}).get("rain", {}) if isinstance(palette_data, dict) else {}
    for b in source.get("bands", []) or []:
        if not isinstance(b, dict): continue
        rgb=b.get("rgb", [0,0,0]); label=b.get("label")
        lo,hi=b.get("lo"),b.get("hi")
        if label is not None: text=str(label)
        elif lo is not None and hi is not None: text=f"{lo:g}–{hi:g}"
        elif lo is not None: text=f"≥ {lo:g}"
        else: continue
        bands.append((rgb,text))
    if not bands:
        for b in (header.get("значения", {}).get("полосы", []) or header.get("bands", []) or []):
            if not isinstance(b, dict): continue
            rgb=b.get("rgb", [0,0,0]); label=b.get("label")
            lo,hi=b.get("lo"),b.get("hi")
            if label is not None: text=str(label)
            elif lo is not None and hi is not None: text=f"{lo:g}–{hi:g}"
            elif lo is not None: text=f"≥ {lo:g}"
            else: continue
            bands.append((rgb,text))
    return title, units, bands

def render_radar_image(header, codes, lat, lon, city, palette_data=None):
    rgba = rdr_rgba(header, codes)
    img = Image.fromarray(rgba, "RGBA")
    box = header.get("box") or header.get("рамка")
    width, height = img.size
    if not box or len(box) != 4:
        raise ValueError("В RDR отсутствует геометрия box")
    west, south, east, north = map(float, box)
    # Web Mercator meters -> pixel. Crop around requested city, preserving a useful local area.
    R = 6378137.0
    def merc_y(phi): return R * np.log(np.tan(np.pi/4 + np.radians(phi)/2))
    x = R * np.radians(lon); y = merc_y(lat)
    px = int((x-west)/(east-west)*width)
    py = int((north-y)/(north-south)*height)
    crop_w = min(width, max(900, int(width*0.28)))
    crop_h = min(height, max(700, int(height*0.28)))
    left = max(0, min(width-crop_w, px-crop_w//2)); top = max(0, min(height-crop_h, py-crop_h//2))
    img = img.crop((left, top, left+crop_w, top+crop_h)).convert("RGBA")
    draw = ImageDraw.Draw(img)
    cx, cy = px-left, py-top
    draw.ellipse((cx-9,cy-9,cx+9,cy+9), fill=(255,255,255,255), outline=(20,20,20,255), width=3)
    draw.text((cx+14, cy-12), city, fill=(255,255,255,255), stroke_width=3, stroke_fill=(0,0,0,210))
    # Coordinate grid.
    for grid_lat in range(int(np.floor(lat-4)), int(np.ceil(lat+5))):
        gy = int((north-merc_y(grid_lat))/(north-south)*height)-top
        if 0 <= gy < img.height: draw.line((0,gy,img.width,gy), fill=(255,255,255,70), width=1)
    for grid_lon in range(int(np.floor(lon-6)), int(np.ceil(lon+7))):
        gx = int((R*np.radians(grid_lon)-west)/(east-west)*width)-left
        if 0 <= gx < img.width: draw.line((gx,0,gx,img.height), fill=(255,255,255,70), width=1)
    # Add title and source strip.
    out = Image.new("RGBA", (img.width, img.height+105), (12,16,22,255)); out.alpha_composite(img, (0,105))
    d=ImageDraw.Draw(out)
    d.text((22,18), f"РАДАР • {city}", fill=(255,255,255,255))
    d.text((22,48), "Осадки • iDarkMeteo", fill=(205,215,225,255))
    title, units, bands = radar_legend(header, palette_data)
    if bands:
        x0=22; y0=78
        for rgb, text in bands[:10]:
            d.rectangle((x0,y0,x0+18,y0+18), fill=tuple(rgb[:3])+(255,))
            d.text((x0+23,y0+1), str(text), fill=(235,240,245,255))
            x0 += 105
            if x0 > out.width-120: break
    else:
        d.text((22,78), f"{title} • {units}", fill=(235,240,245,255))
    return out

async def make_idark_radar(city, lat, lon):
    meta = await get_idark_json("frames/rain/wide.json")
    frames = meta.get("frames", [])
    if not frames: raise RuntimeError("iDarkMeteo не вернул доступные кадры")
    frame = frames[0]
    raw = await get_idark_bytes(frame["path"])
    header, codes = decode_rdr(raw)
    try:
        palette_data = await get_idark_json("palettes.json")
    except Exception:
        palette_data = None
    image = render_radar_image(header, codes, lat, lon, city, palette_data)
    out = io.BytesIO(); image.convert("RGB").save(out, format="JPEG", quality=88, optimize=True); out.seek(0)
    return out, frame.get("t")

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

# ---------------- handlers ----------------
@dp.message(Command("start"))
async def cmd_start(message:types.Message,state:FSMContext):
    if message.chat.type != "private":
        await message.answer("В группе используй команду: /wz город\nНапример: /wz Самара")
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

@dp.message(Command("help"))
async def cmd_help(message:types.Message):
    await message.answer("Личные сообщения: кнопки «Прогноз» и «Графики».\nГруппы: /wz Самара — прогноз; /graph Самара — графики; /rad Самара — радар iDarkMeteo.")

@dp.message(F.text=="🌤 Прогноз",F.chat.type=="private")
async def forecast_button(message:types.Message,state:FSMContext):
    await state.set_state(Form.forecast_city); await message.answer("🏙 Введи город для прогноза:",reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Назад")]],resize_keyboard=True))

@dp.message(F.text=="📈 Графики",F.chat.type=="private")
async def charts_button(message:types.Message,state:FSMContext):
    await state.set_state(Form.charts_city); await message.answer("🏙 Введи город для графиков:",reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Назад")]],resize_keyboard=True))

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


@dp.message(Command("rad"))
async def radar_command(message:types.Message):
    # /rad город — радарная картинка iDarkMeteo; только явная команда.
    city=(message.text or "").partition(" ")[2].strip()
    if not city:
        await message.answer("Используй: /rad Самара")
        return
    lat,lon,resolved,_=await get_coordinates(city)
    if lat is None:
        await message.answer("😕 Город не найден. Попробуй написать иначе.")
        return
    try:
        img, ts = await make_idark_radar(resolved, lat, lon)
        stamp = f"\nКадр: {html.escape(ts)}" if ts else ""
        await message.answer_photo(
            BufferedInputFile(img.read(), filename="idarkmeteo_radar.jpg"),
            caption=f"🌧 <b>Радар — {html.escape(resolved)}</b>{stamp}\n\n<b>iDarkMeteo</b>",
            parse_mode="HTML"
        )
    except Exception:
        logging.exception("radar command")
        await message.answer("⚠️ Не удалось загрузить радар iDarkMeteo. Попробуй ещё раз позже.")

@dp.message(Command("graph"))
async def graph_command(message:types.Message):
    # /graph город — работает и в личке, и в группах; реагирует только на явную команду.
    city=(message.text or "").partition(" ")[2].strip()
    if not city:
        await message.answer("Используй: /graph Самара")
        return
    lat,lon,resolved,_=await get_coordinates(city)
    if lat is None:
        await message.answer("😕 Город не найден. Попробуй написать иначе.")
        return
    try:
        data=await get_weather(lat,lon)
        img=make_charts(resolved,data)
        await message.answer_photo(BufferedInputFile(img.read(),filename="forecast_48h.png"),caption=f"📈 <b>{html.escape(resolved)}</b> — графики на 48 часов",parse_mode="HTML")
    except Exception:
        logging.exception("graph command")
        await message.answer("⚠️ Не удалось построить графики. Попробуй ещё раз позже.")

@dp.message(Command("wz"))
async def group_weather(message:types.Message):
    if message.chat.type=="private":
        city=(message.text or "").partition(" ")[2].strip()
        if city: await send_forecast(message,city)
        else: await message.answer("Используй: /wz Самара")
        return
    city=(message.text or "").partition(" ")[2].strip()
    if not city: await message.answer("Используй: /wz Самара"); return
    await send_forecast(message,city)

# In groups, do NOT process ordinary text. In private, allow a city only during setup/explicit FSM states.
@dp.message(F.chat.type=="private")
async def private_fallback(message:types.Message):
    await message.answer("Выбери действие в меню: 🌤 Прогноз, 📈 Графики или ⚙️ Настройки.",reply_markup=main_menu())

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
    app["thunder_task"]=asyncio.create_task(thunderstorm_monitor())
    logging.info("Webhook установлен")

async def on_shutdown(app:web.Application):
    task=app.get("thunder_task")
    if task: task.cancel()
    await bot.delete_webhook(); await bot.session.close()

def main():
    db().close(); app=web.Application(); SimpleRequestHandler(dispatcher=dp,bot=bot).register(app,path="/webhook"); setup_application(app,dp,bot=bot); app.router.add_get("/",lambda r:web.Response(text="Weather bot is alive")); web.run_app(app,host="0.0.0.0",port=int(os.environ.get("PORT",10000)),on_startup=on_startup,on_shutdown=on_shutdown)

if __name__=="__main__": main()
