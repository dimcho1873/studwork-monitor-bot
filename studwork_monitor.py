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

# -------------------- НАСТРОЙКИ --------------------
API_URL = "https://api.studwork.ru/orders?type_ids[]=1&type_ids[]=2&type_ids[]=10&type_ids[]=11&type_ids[]=12&type_ids[]=17&type_ids[]=18&type_ids[]=34&type_ids[]=35&type_ids[]=36&type_ids[]=20&type_ids[]=24&type_ids[]=15&type_ids[]=6&type_ids[]=19&discipline_group_ids[]=2&discipline_group_ids[]=5&discipline_group_ids[]=6&discipline_group_ids[]=7&discipline_group_ids[]=8&discipline_group_ids[]=9&discipline_group_ids[]=4&my_disciplines=false&my_types=false&showHiddenOrders=false"
ORDERS_LIMIT = 20

PROCESSED_IDS_FILE = Path("processed_ids.json")

# Telegram
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Codestral (Mistral AI) - OpenAI-совместимый API
CODESTRAL_API_KEY = os.environ["CODESTRAL_API_KEY"]
CODESTRAL_API_BASE = "https://codestral.mistral.ai/v1"
CODESTRAL_MODEL = "codestral-latest"

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

def ask_codestral(order, html_content):
    """
    Анализирует HTML страницы заказа через Codestral (Mistral AI).
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
Ты — фильтр заказов с биржи Studwork. На основе HTML страницы заказа прими решение, подходит ли заказ для автоматического выполнения с помощью языковой модели (ИИ).

**Жёсткие критерии ОТКЛОНЕНИЯ (если хоть один выполнен → suitable: false):**
1. Имя пользователя НЕ начинается с "user" (например, "user12345" — ок, "ivanov" — отказ).
2. Заказ помечен как "рейтинговая работа" или содержит слова "рейтинговая", "rating", "проверочная работа".
3. Срок сдачи меньше 24 часов ИЛИ в описании есть "срочно", "сегодня", "в течение часа", "asap".
4. В описании работы явно указано: "без ИИ", "без нейросетей", "вручную", "своими руками", "не использовать GPT", "человеческое исполнение".
5. Задача **невыполнима только с помощью текстовой/кодовой генерации** (требуется физическое действие, специфический софт без API, доступ к закрытым базам, видеозвонок и т.п.).

**Оценка выполнимости ИИ (если жёстких критериев нет):**
- Проанализируй описание: если это написание текстов, перевод, решение задач, написание кода, генерация идей, редактирование — подходит.
- Если присутствуют чертежи, работа с конкретными программами (AutoCAD, Компас), монтаж видео, голосовые услуги — отказ.

**Сложность для LLM** (если suitable: true):
- "низкая" — типовой реферат, простой код, ответ на вопрос.
- "средняя" — контрольная работа с расчётами, курсовой проект, нестандартный запрос.
- "высокая" — объёмный диплом с уникальным планом, многошаговый анализ, работа с большими данными.

**Формат ответа:**
Верни только JSON без Markdown-обёрток.
{{
  "suitable": true/false,
  "reason": "", // если false — краткая причина, иначе пусто
  "title": "название заказа",
  "description": "полное описание из HTML",
  "price": "цена",
  "deadline": "срок",
  "user_name": "имя",
  "difficulty": "низкая/средняя/высокая",
  "summary": "2-3 предложения сути заказа"
}}

Данные из API (ориентир):
Тема: {topic}
Тип: {work_type}
Дисциплина: {discipline}

HTML страницы (обрезан до 800 тыс. символов):
{html_content[:800000]}

ТОЛЬКО JSON В ОТВЕТЕ.
"""

    url = f"{CODESTRAL_API_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {CODESTRAL_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": CODESTRAL_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        # "response_format": {"type": "json_object"}  # Codestral поддерживает? Если нет – удалить.
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        # Извлекаем текст ответа модели
        text = data["choices"][0]["message"]["content"].strip()

        # Удаляем возможные Markdown-обёртки ```json ... ```
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        result = json.loads(text)
        # Приводим suitable к bool
        result['suitable'] = bool(result.get('suitable', False))
        return result
    except Exception as e:
        print(f"Ошибка при обращении к Codestral или парсинге JSON: {e}")
        if 'text' in locals():
            print("Ответ модели:", text[:200])
        else:
            print("Нет ответа от модели")
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
    """Формирует красивое HTML-сообщение для Telegram на основе анализа Codestral."""
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
    print("Запуск мониторинга заказов с анализом через Codestral (Mistral AI)...")
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

            analysis = ask_codestral(order, html_content)
            if analysis is None:
                print("  ❌ Не удалось получить анализ от Codestral.")
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
