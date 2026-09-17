import logging
import os
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# --- Конфигурация ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в переменных окружения!")

# ⚠️ ЕСЛИ АДРЕС ДРУГОЙ — ЗАМЕНИ ЗДЕСЬ
WEBHOOK_BASE_URL = "https://weatherbot.onrender.com"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Расшифровка кодов погоды WMO ---
WMO_ICONS = {
    0: "☀️ Ясно", 1: "🌤 Преимущественно ясно", 2: "⛅️ Переменная облачность",
    3: "☁️ Пасмурно", 45: "🌫 Туман", 48: "🌫 Оседающий туман",
    51: "🌦 Слабая морось", 53: "🌦 Морось", 55: "🌦 Сильная морось",
    61: "🌧 Небольшой дождь", 63: "🌧 Дождь", 65: "🌧 Сильный дождь",
    71: "🌨 Небольшой снег", 73: "🌨 Снег", 75: "❄️ Сильный снег",
    80: "🌦 Ливень", 81: "🌦 Сильный ливень", 82: "⛈ Очень сильный ливень",
    95: "⛈ Гроза", 96: "⛈ Гроза с градом", 99: "⛈ Сильная гроза с градом"
}

async def get_coordinates(city: str):
    """Получает широту и долготу по названию города."""
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1&language=ru"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if "results" in data and data["results"]:
                res = data["results"][0]
                return res["latitude"], res["longitude"], res.get("name", city)
    return None, None, None

async def get_weather(lat: float, lon: float):
    """Запрашивает текущую погоду по координатам."""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&"
        f"current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        f"weather_code,wind_speed_10m&timezone=auto"
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()

# --- Обработчики ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот погоды 🌤\n"
        "Напиши мне название города, и я расскажу о погоде там."
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "Просто отправь название города, например:\n"
        "• Москва\n"
        "• Amsterdam\n"
        "• New York"
    )

@dp.message()
async def weather_handler(message: types.Message):
    city = message.text.strip()
    if not city:
        await message.answer("Напиши название города.")
        return

    await message.answer(f"🔍 Ищу погоду: {city}...")

    lat, lon, resolved_name = await get_coordinates(city)
    if lat is None:
        await message.answer("😕 Город не найден. Попробуй написать иначе.")
        return

    try:
        weather_data = await get_weather(lat, lon)
        current = weather_data["current"]
        units = weather_data["current_units"]

        condition = WMO_ICONS.get(current["weather_code"], "❓ Неизвестно")

        response_text = (
            f"📍 <b>{resolved_name}</b>\n\n"
            f"{condition}\n"
            f"🌡 Температура: <b>{current['temperature_2m']} {units['temperature_2m']}</b>\n"
            f"🤔 Ощущается как: {current['apparent_temperature']} {units['apparent_temperature']}\n"
            f"💧 Влажность: {current['relative_humidity_2m']}%\n"
            f"💨 Ветер: {current['wind_speed_10m']} {units['wind_speed_10m']}"
        )
        await message.answer(response_text, parse_mode="HTML")

    except Exception as e:
        logging.error(f"Ошибка при получении погоды: {e}")
        await message.answer("⚠️ Не удалось получить данные. Попробуй позже.")

# --- Webhook ---
async def on_startup(app: web.Application):
    webhook_url = f"{WEBHOOK_BASE_URL}/webhook"
    await bot.set_webhook(webhook_url, drop_pending_updates=True)
    logging.info(f"Webhook установлен: {webhook_url}")

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()

def main():
    app = web.Application()

    # Регистрируем обработчик вебхука
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    # Health-check для Render
    app.router.add_get("/", lambda r: web.Response(text="Weather bot is alive"))

    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port,
                on_startup=on_startup, on_shutdown=on_shutdown)

if __name__ == "__main__":
    main()
