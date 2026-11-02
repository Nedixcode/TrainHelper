#!/usr/bin/env python3
"""
Следит за свободными местами на pass.rw.by.
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup

CONFIG_FILE = os.getenv("RW_CONFIG", "watch.json")
INTERVAL = int(os.getenv("RW_INTERVAL", "180"))
URL = "https://pass.rw.by/ru/route/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.9",
}

PRICE = re.compile(r"\d[\d\s]*,\d{2}\s*(byn|rub|eur|usd)", re.I)

def load_settings():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)

    def pick(env, key):
        return (os.getenv(env) or cfg.get(key) or "").strip()

    s = {"from": pick("RW_FROM", "from"), "to": pick("RW_TO", "to"),
         "date": pick("RW_DATE", "date"), "train": pick("RW_TRAIN", "train")}
    if not (s["from"] and s["to"] and s["date"]):
        return None
    return s

def route_url(s):
    return URL + "?" + urlencode({"from": s["from"], "to": s["to"], "date": s["date"]})

def notify(text):
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
        data={"chat_id": os.environ["TG_CHAT_ID"], "text": text},
        timeout=15,
    )
    r.raise_for_status()

def fetch_page(s):
    session = requests.Session()
    session.headers.update(HEADERS)
    
    # Сначала заходим на главную для получения кук сессии
    try:
        session.get("https://pass.rw.by/ru/", timeout=15)
    except Exception:
        pass

    params = {"from": s["from"], "to": s["to"], "date": s["date"]}
    r = session.get(URL, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r, BeautifulSoup(r.text, "html.parser")

def parse_trains(soup):
    """Ищет блоки поездов на странице результатов."""
    trains = []
    # На сайте БЧ блоки поездов обычно имеют класс route-item или содержат данные о поезде
    items = soup.find_all(class_=re.compile("route-item|train-row|card"))
    if not items:
        # Универсальный запасной поиск по тегам, если классы изменились
        items = soup.find_all("div", text=re.compile(r"\d{3}[А-ЯA-Z]?"))

    for item in soup.find_all(True):
        text = item.get_text(" ", strip=True)
        # Ищем номера поездов вида 658Б, 649 и т.д.
        match = re.search(r"\b(\d{3}[А-ЯA-Z]?)\b", text)
        if match and len(text) < 1000 and ("отправл" in text.lower() or "прибыт" in text.lower() or "мест" in text.lower()):
            train_num = match.group(1)
            # Проверим наличие цен или кнопки выбора мест
            has_free = "выбрать места" in text.lower() or bool(PRICE.search(text))
            trains.append({
                "id": train_num,
                "has_seats": has_free,
                "text": text[:300]
            })
    return trains

def check(s):
    _, soup = fetch_page(s)
    
    # Ищем все элементы с информацией о поездах на новой верстке сайта
    # На сайте pass.rw.by блоки поездов содержат ссылки на поезд или специфичные элементы
    train_blocks = soup.select(".train-row, .route-item, [data-train]")
    
    results = []
    # Альтернативный поиск по всему тексту страницы, если селекторы не сработали
    page_text = soup.get_text("\n", strip=True)
    
    # Более надежный метод парсинга через поиск строк с номером поезда
    for a in soup.find_all("a", href=re.compile("train=")):
        parent = a.find_parent(class_=re.compile("item|row|card|train")) or a.parent
        text = parent.get_text(" ", strip=True)
        train_num_match = re.search(r"(\d{3}[А-ЯA-Z]?)", a.get_text() + " " + text)
        if train_num_match:
            t_num = train_num_match.group(1)
            has_seats = "выбрать места" in text.lower() or bool(PRICE.search(text))
            results.append({
                "id": t_num,
                "has_seats": has_seats,
                "text": text
            })

    if not results:
        return "missing", "Поезд не найден на странице (возможно, изменилась структура или защита сайта)."

    wanted = [t.strip().lower() for t in s["train"].split(",") if t.strip()]
    
    filtered = []
    for t in results:
        tid = t["id"].lower()
        matched = False
        if not wanted:
            matched = True
        else:
            for w in wanted:
                if tid == w or (w.isdigit() and tid.startswith(w)):
                    matched = True
                    break
        if matched:
            filtered.append(t)

    if not filtered:
        return "missing", f"Указанный поезд ({s['train']}) не найден в выдаче."

    free = [t for t in filtered if t["has_seats"]]
    if free:
        return "seats", free[0]["text"]
    return "none", "Мест нет."

def describe(s):
    return f"{s['from']} -> {s['to']}, {s['date']}, поезд: {s['train'] or 'любой'}"

def warn_window():
    t = time.gmtime()
    return t.tm_hour % 6 == 0 and t.tm_min < 10

def debug(s):
    print(f"Настройки: {describe(s)}")
    try:
        r, soup = fetch_page(s)
        print(f"HTTP статус: {r.status_code}, размер страницы: {len(r.text)}")
        status, info = check(s)
        print("Итог:", status, "|", info)
    except Exception as e:
        print("Ошибка:", e)

def main():
    once = "--once" in sys.argv
    hello = "--hello" in sys.argv

    s = load_settings()
    if s is None:
        print("Слежение не настроено.")
        return
    try:
        trip = datetime.strptime(s["date"], "%Y-%m-%d").date()
    except ValueError:
        sys.exit("Дата должна быть в формате ГГГГ-ММ-ДД.")
    if trip < date.today():
        print("Дата поездки уже прошла.")
        return

    if "--debug" in sys.argv:
        debug(s)
        return

    last = None
    warned_missing = False
    while True:
        try:
            status, info = check(s)
            print(time.strftime("%H:%M:%S"), status, flush=True)
            if hello:
                state = {"seats": f"места есть! {info}", "none": "мест пока нет.",
                         "missing": f"Внимание: {info}"}[status]
                notify(f"Слежу за местами: {describe(s)}\nСейчас: {state}\n{route_url(s)}")
            elif status == "seats" and last != "seats":
                notify(f"Появились места! {describe(s)}\n{info}\n{route_url(s)}")
            elif status == "missing" and not warned_missing and (not once or warn_window()):
                notify(f"Внимание: {describe(s)}\n{info} Проверь скрипт.")
                warned_missing = True
            if status != "missing":
                warned_missing = False
            last = status
        except Exception as e:
            print("Ошибка:", e, flush=True)
            if hello:
                try:
                    notify(f"Не удалось проверить сайт: {e}")
                except Exception:
                    pass
        if once:
            return
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
