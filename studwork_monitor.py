import os
import json
import time
import requests
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import google.generativeai as genai

# -------------------- НАСТРОЙКИ --------------------
API_URL = "https://api.studwork.ru/orders?type_ids[]=1&type_ids[]=2&type_ids[]=10&type_ids[]=11&type_ids[]=12&type_ids[]=17&type_ids[]=18&type_ids[]=34&type_ids[]=35&type_ids[]=36&type_ids[]=20&type_ids[]=24&type_ids[]=15&type_ids[]=6&type_ids[]=19&discipline_group_ids[]=2&discipline_group_ids[]=5&discipline_group_ids[]=6&discipline_group_ids[]=7&discipline_group_ids[]=8&discipline_group_ids[]=9&discipline_group_ids[]=4&my_disciplines=false&my_types=false&showHiddenOrders=false"
ORDERS_LIMIT = 5   # сколько заказов проверяем за один запуск

PROCESSED_IDS_FILE = Path("processed_ids.json")

# Telegram
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Gemini
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
MODEL = genai.GenerativeModel("gemini-2.5-flash-latest")   # 1M контекст – HTML поместится

# -------------------- ФУНКЦИИ --------------------
def load_processed_ids():
    if PROCESSED_IDS_FILE.exists():
        with open(PROCESSED_IDS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_processed_ids(ids_set):
    with open(PROCESSED_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(ids_set), f)

def fetch_orders():
    try:
        response = requests.get(API_URL, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("result") == "success":
            return data.get("orders", [])
        else:
            print("Ошибка API:", data)
            return []
    except Exception as e:
        print(f"Ошибка при запросе к API: {e}")
        return []

def build_order_link(order):
    return f"https://studwork.ru/order/{order['id']}-{order['url']}"

def get_selenium_driver():
    """Настройка headless Chrome для GitHub Actions."""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def get_order_html(driver, order):
    """
    Загружает страницу заказа и возвращает HTML-код блока <div class="order">
    (или всей страницы, если блок не найден).
    """
    url = build_order_link(order)
    driver.get(url)
    time.sleep(2)

    try:
        # Ждём появления основного блока заказа
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.order"))
        )
        order_element = driver.find_element(By.CSS_SELECTOR, "div.order")
        html_content = order_element.get_attribute("outerHTML")
        print(f"  HTML блока заказа получен, длина: {len(html_content)} символов")
        return html_content
    except Exception as e:
        print(f"  Не удалось найти div.order, возвращаем HTML всей страницы. Ошибка: {e}")
        return driver.page_source

def ask_gemini(order, html_content):
    """
    Отправляет HTML заказа в Gemini и просит определить,
    можно ли выполнить этот заказ с помощью ИИ.
    """
    topic = order.get("topic", "Без названия")
    work_type = order.get("workType", {}).get("name", "Не указан")
    discipline = order.get("discipline", "Не указана")

    prompt = f"""
Ты — эксперт по оценке заказов на фриланс-бирже. Тебе предоставлен HTML-код страницы заказа с сайта Studwork.
Твоя задача: проанализировать содержимое страницы и ответить **только "да" или "нет"**.

Критерии ответа "да":
- Заказ можно полностью выполнить с помощью современных языковых моделей (написание текста, реферат, ответы на вопросы, перевод, программирование простых скриптов, решение типовых задач).
- Не требуется физического присутствия, работы со специализированным ПО без API, уникальных творческих навыков.
- Объём работы небольшой или средний.

Критерии ответа "нет":
- Требуется работа с файлами, которые ИИ не может прочитать (например, чертежи в специализированных форматах).
- Нужна личная встреча, экзамен с прокторингом, лабораторная работа в проприетарной среде.
- Узкоспециализированная тема, где высока вероятность ошибки ИИ.

Краткие данные из API:
Тема: {topic}
Тип работы: {work_type}
Дисциплина: {discipline}

HTML-код страницы заказа:
{html_content[:900000]}   # ограничиваем на всякий случай (модель принимает до 1 млн токенов)

Подходит ли этот заказ для выполнения с помощью ИИ? Ответь только "да" или "нет".
"""
    try:
        response = MODEL.generate_content(prompt)
        answer = response.text.strip().lower()
        return answer.startswith("да")
    except Exception as e:
        print(f"Ошибка при обращении к Gemini: {e}")
        return False

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False   # пусть показывается превью ссылки
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

def format_order_message(order):
    link = build_order_link(order)
    topic = order.get("topic", "Без названия")
    work_type = order.get("workType", {}).get("name", "—")
    discipline = order.get("discipline", "—")
    price = order.get("price", "не указана")
    offers = order.get("offersCount", 0)

    return (
        f"🔔 <b>Найден подходящий заказ!</b>\n\n"
        f"📌 <b>{topic}</b>\n"
        f"📚 {work_type} | {discipline}\n"
        f"💰 Цена: {price}\n"
        f"👥 Откликов: {offers}\n"
        f"🔗 <a href='{link}'>Открыть заказ</a>"
    )

def main():
    print("Запуск мониторинга заказов с передачей HTML в Gemini...")
    processed = load_processed_ids()
    orders = fetch_orders()
    print(f"Получено заказов: {len(orders)}")

    if not orders:
        print("Нет заказов для проверки.")
        return

    driver = get_selenium_driver()
    new_processed = set()

    try:
        for order in orders[:ORDERS_LIMIT]:
            order_id = order["id"]
            if order_id in processed:
                continue

            print(f"Обработка заказа #{order_id}: {order.get('topic', '')[:50]}...")
            html_content = get_order_html(driver, order)

            if ask_gemini(order, html_content):
                print(f"✅ Подходит! Отправляю в Telegram.")
                msg = format_order_message(order)
                send_telegram_message(msg)
                time.sleep(0.5)
            else:
                print(f"❌ Не подходит.")

            new_processed.add(order_id)
            time.sleep(3)   # пауза между заказами

    finally:
        driver.quit()

    if new_processed:
        processed.update(new_processed)
        save_processed_ids(processed)
        print(f"Сохранено новых ID: {len(new_processed)}")
    else:
        print("Новых подходящих заказов нет.")

if __name__ == "__main__":
    main()
