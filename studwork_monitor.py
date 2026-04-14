import os
import json
import time
import re
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
ORDERS_LIMIT = 20

PROCESSED_IDS_FILE = Path("processed_ids.json")

# Telegram
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Gemini
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
MODEL = genai.GenerativeModel("gemini-2.5-flash")

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
    url = build_order_link(order)
    driver.get(url)
    time.sleep(2)

    try:
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
    Анализирует HTML страницы заказа через Gemini.
    Возвращает словарь с полями:
        suitable (bool)
        reason (str, если не подходит)
        title (str)
        description (str)
        price (str)
        deadline (str)
        user_name (str)
        difficulty (str)
        summary (str)
    Если ответ не удалось распарсить, возвращает None.
    """
    topic = order.get("topic", "Без названия")
    work_type = order.get("workType", {}).get("name", "Не указан")
    discipline = order.get("discipline", "Не указана")

    prompt = f"""
Ты — эксперт по анализу заказов с биржи Studwork. Тебе предоставлен HTML-код страницы заказа.
Твоя задача:
1. Извлечь из HTML следующие данные:
   - Полное название / тема заказа (title)
   - Полное описание работы (description)
   - Стоимость (price)
   - Срок сдачи (deadline) – если указано "срочно" или время меньше 24 часов, считай это неприемлемым.
   - Имя пользователя (user_name) – автор заказа.
   - Признак "рейтинговая работа" (если есть слова "рейтинговая", "rating", "проверочная работа" и т.п.)
2. Оценить, можно ли полностью выполнить заказ с помощью ИИ (языковых моделей) с учётом следующих дополнительных критериев:
   - Имя пользователя должно начинаться со слова "user" (например, "user12345"). Если начинается с другого – заказ НЕ подходит.
   - Если заказ помечен как "рейтинговая работа" – НЕ подходит.
   - Если срок сдачи менее 24 часов от текущего момента (или указано "срочно", "сегодня") – НЕ подходит.
   - Кроме того, заказ должен быть выполним чисто с помощью ИИ (написание текстов, кода, перевод, решение задач, без необходимости физического присутствия или специализированного ПО без API).
3. Если заказ подходит, оцени сложность выполнения с помощью LLM: "низкая", "средняя" или "высокая".
4. Составь краткое резюме (2-3 предложения), понятное человеку.

Ответ должен быть строго в формате JSON, без дополнительных комментариев. Пример:
{{
  "suitable": true,
  "reason": "",
  "title": "Написать реферат по истории",
  "description": "Требуется написать реферат на 10 страниц о правлении Петра I...",
  "price": "500 руб",
  "deadline": "2 дня",
  "user_name": "user12345",
  "difficulty": "низкая",
  "summary": "Несложный реферат по истории, требуется только текст."
}}

Если заказ не подходит, в поле "reason" укажи краткую причину (например, "рейтинговая работа" или "имя не начинается с user").

Краткие данные из API:
Тема: {topic}
Тип работы: {work_type}
Дисциплина: {discipline}

HTML-код страницы заказа (может быть обрезан до 800 000 символов):
{html_content[:800000]}

Верни ТОЛЬКО JSON.
"""
    try:
        response = MODEL.generate_content(prompt)
        text = response.text.strip()
        # Удаляем возможные Markdown-обёртки ```json ... ```
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        data = json.loads(text)
        # Приводим suitable к bool
        data['suitable'] = bool(data.get('suitable', False))
        return data
    except Exception as e:
        print(f"Ошибка при обращении к Gemini или парсинге JSON: {e}")
        print("Ответ модели:", text[:200] if 'text' in locals() else "нет ответа")
        return None

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

def format_order_message(order, analysis):
    """Формирует красивое HTML-сообщение для Telegram на основе анализа Gemini."""
    link = build_order_link(order)

    title = analysis.get('title', order.get('topic', 'Без названия'))
    description = analysis.get('description', '—')
    price = analysis.get('price', order.get('price', 'не указана'))
    deadline = analysis.get('deadline', '—')
    difficulty = analysis.get('difficulty', 'не определена')
    summary = analysis.get('summary', '')
    user_name = analysis.get('user_name', '—')

    msg = f"🔔 <b>Подходящий заказ!</b>\n\n"
    msg += f"📌 <b>{title}</b>\n"
    msg += f"👤 Заказчик: {user_name}\n"
    msg += f"💰 Цена: {price}\n"
    msg += f"⏳ Срок: {deadline}\n"
    msg += f"🤖 Сложность для ИИ: {difficulty}\n\n"

    if summary:
        msg += f"📝 <i>{summary}</i>\n\n"
    else:
        # Если summary нет, вставим начало описания
        desc_preview = description[:200] + ('...' if len(description) > 200 else '')
        msg += f"📄 {desc_preview}\n\n"

    msg += f"🔗 <a href='{link}'>Открыть заказ на Studwork</a>"

    return msg

def main():
    print("Запуск мониторинга заказов с расширенным анализом Gemini...")
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

            print(f"\nОбработка заказа #{order_id}: {order.get('topic', '')[:50]}...")
            html_content = get_order_html(driver, order)

            analysis = ask_gemini(order, html_content)
            if analysis is None:
                print("  ❌ Не удалось получить анализ от Gemini.")
                new_processed.add(order_id)  # всё равно помечаем как обработанный, чтобы не зацикливаться
                continue

            if analysis.get('suitable'):
                print(f"  ✅ Подходит! Сложность: {analysis.get('difficulty')}. Отправляю в Telegram.")
                msg = format_order_message(order, analysis)
                send_telegram_message(msg)
                time.sleep(0.5)
            else:
                reason = analysis.get('reason', 'не указана')
                print(f"  ❌ Не подходит. Причина: {reason}")

            new_processed.add(order_id)
            time.sleep(3)   # пауза между заказами

    finally:
        driver.quit()

    if new_processed:
        processed.update(new_processed)
        save_processed_ids(processed)
        print(f"\nСохранено новых ID: {len(new_processed)}")
    else:
        print("\nНовых подходящих заказов нет.")

if __name__ == "__main__":
    main()
