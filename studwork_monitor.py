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
ORDERS_LIMIT = 5   # ограничим число просматриваемых страниц, чтобы не превысить время

PROCESSED_IDS_FILE = Path("processed_ids.json")

# Telegram
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Gemini
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
MODEL = genai.GenerativeModel("gemini-3-flash-preview")

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
    # Автоматическая установка драйвера
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

def scrape_order_page(driver, order):
    """Парсим страницу заказа, возвращаем словарь с дополнительной информацией."""
    url = build_order_link(order)
    driver.get(url)
    time.sleep(2)  # Даём странице подгрузиться

    info = {
        "description": "",
        "files": [],
        "deadline": "",
        "price_exact": order.get("price", "не указана"),
    }

    try:
        # Ждём появления основного контента
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.order-view"))
        )
    except Exception as e:
        print(f"Не дождались загрузки страницы: {e}")
        return info

    # Текст заказа (обычно в div с классом order-view__description или подобном)
    try:
        desc_elem = driver.find_element(By.CSS_SELECTOR, "div.order-view__description")
        info["description"] = desc_elem.text.strip()
    except:
        # Альтернативные селекторы
        try:
            desc_elem = driver.find_element(By.CSS_SELECTOR, "div.task-description")
            info["description"] = desc_elem.text.strip()
        except:
            pass

    # Файлы (могут быть в виде списка с названиями)
    try:
        file_elems = driver.find_elements(By.CSS_SELECTOR, "div.order-view__files a, div.files-list a")
        info["files"] = [f.text.strip() for f in file_elems if f.text.strip()]
    except:
        pass

    # Срок выполнения (часто в блоке с информацией)
    try:
        deadline_elem = driver.find_element(By.CSS_SELECTOR, "div.order-view__info-item--deadline span.value, span.deadline")
        info["deadline"] = deadline_elem.text.strip()
    except:
        pass

    # Точная цена, если указана (иногда "Предлагайте", тогда оставляем из API)
    try:
        price_elem = driver.find_element(By.CSS_SELECTOR, "div.order-view__price span.value, span.price-value")
        price_text = price_elem.text.strip()
        if price_text and price_text.lower() not in ["предлагайте", "договорная"]:
            info["price_exact"] = price_text
    except:
        pass

    return info

def ask_gemini(order, scraped_info):
    """Расширенный запрос к Gemini с полными данными."""
    topic = order.get("topic", "Без названия")
    work_type = order.get("workType", {}).get("name", "Не указан")
    discipline = order.get("discipline", "Не указана")
    price = scraped_info.get("price_exact") or order.get("price", "не указана")
    offers = order.get("offersCount", 0)
    description = scraped_info.get("description", "Описание отсутствует")
    files = scraped_info.get("files", [])
    deadline = scraped_info.get("deadline", "не указан")

    prompt = f"""
Ты – эксперт по оценке учебных и фриланс-заказов. Проанализируй заказ и ответь ТОЛЬКО "да" или "нет".

Критерии "да":
- Заказ можно выполнить полностью с помощью современных языковых моделей (написание текста, реферат, ответы на вопросы, перевод, программирование простых скриптов, решение типовых задач).
- Не требует физического присутствия, сложных расчётов в специализированном ПО, работы с закрытыми базами или уникального творчества.
- Объём небольшой или средний.

Критерии "нет":
- Требуется работа с файлами, которые ИИ не может открыть (например, специфические чертежи).
- Нужна личная встреча, экзамен онлайн с прокторингом, лабораторная в специальной программе.
- Узкоспециализированная тема, где высока вероятность ошибки ИИ.

Информация о заказе:
Тема: {topic}
Тип работы: {work_type}
Дисциплина: {discipline}
Цена: {price}
Количество откликов: {offers}
Срок сдачи: {deadline}
Прикреплённые файлы: {", ".join(files) if files else "нет"}

Описание заказа:
{description[:1000]}

Подходит ли этот заказ для выполнения с помощью ИИ? (ответь только "да" или "нет")
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
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

def format_order_message(order, scraped_info):
    link = build_order_link(order)
    topic = order.get("topic", "Без названия")
    work_type = order.get("workType", {}).get("name", "—")
    discipline = order.get("discipline", "—")
    price = scraped_info.get("price_exact") or order.get("price", "не указана")
    offers = order.get("offersCount", 0)
    deadline = scraped_info.get("deadline", "не указан")
    files = scraped_info.get("files", [])
    files_text = "\n".join(f"📎 {f}" for f in files) if files else "нет"
    description = scraped_info.get("description", "")
    short_desc = description[:150] + "..." if len(description) > 150 else description

    return (
        f"🔔 <b>Новый подходящий заказ!</b>\n\n"
        f"📌 <b>{topic}</b>\n"
        f"📚 {work_type} | {discipline}\n"
        f"💰 Цена: {price}\n"
        f"⏰ Срок: {deadline}\n"
        f"👥 Откликов: {offers}\n"
        f"📄 Описание: {short_desc}\n"
        f"📎 Файлы: {files_text}\n"
        f"🔗 <a href='{link}'>Открыть заказ</a>"
    )

def main():
    print("Запуск мониторинга заказов с парсингом страниц...")
    processed = load_processed_ids()
    orders = fetch_orders()
    print(f"Получено заказов: {len(orders)}")

    if not orders:
        print("Нет заказов для проверки.")
        return

    # Инициализируем драйвер один раз для всех заказов
    driver = get_selenium_driver()
    new_processed = set()

    try:
        for order in orders[:ORDERS_LIMIT]:
            order_id = order["id"]
            if order_id in processed:
                continue

            print(f"Обработка заказа #{order_id}: {order.get('topic', '')[:50]}...")
            scraped_info = scrape_order_page(driver, order)
            print(f"  Собрано: описание {len(scraped_info['description'])} симв, файлов {len(scraped_info['files'])}")

            if ask_gemini(order, scraped_info):
                print(f"✅ Подходит! Отправляю в Telegram.")
                msg = format_order_message(order, scraped_info)
                send_telegram_message(msg)
                time.sleep(0.5)
            else:
                print(f"❌ Не подходит.")

            new_processed.add(order_id)
            # Пауза между заказами, чтобы не нагружать сервер
            time.sleep(3)

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
