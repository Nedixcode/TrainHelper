#!/usr/bin/env python3
"""
Следит за свободными местами на pass.rw.by через их внутреннее API.
"""
import json
import os
import sys
import time
from datetime import date, datetime
import requests

CONFIG_FILE = os.getenv("RW_CONFIG", "watch.json")
INTERVAL = int(os.getenv("RW_INTERVAL", "180"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://pass.rw.by/ru/",
}

STATION_CODES = {
    "минск": "2100001",
    "минск-пассажирский": "2100001",
    "лунинец": "2100360",
    "брест": "2100050",
    "брест-центральный": "2100050",
    "гоммель": "2100080",
    "гродно": "2100190",
    "витебск": "2100100",
    "могилев": "2100350",
    "могилёв": "2100350",
    "барановичи": "2100040",
    "орша": "2100200",
    "пинск": "2100430",
}

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
    return f"https://pass.rw.by/ru/route/?from={s['from']}&to={s['to']}&date={s['date']}"

def notify(text):
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
        data={"chat_id": os.environ["TG_CHAT_ID"], "text": text},
        timeout=15,
    )
    r.raise_for_status()

def get_station_code(name):
    clean_name = name.lower().strip()
    if clean_name in STATION_CODES:
        return STATION_CODES[clean_name]
    return None

def fetch_trains(s):
    from_code = get_station_code(s["from"])
    to_code = get_station_code(s["to"])
    
    if not from_code or not to_code:
        raise Exception(f"Не удалось найти коды станций для '{s['from']}' или '{s['to']}'")

    session = requests.Session()
    session.headers.update(HEADERS)
    
    # Заходим на главную, чтобы получить куки и пройти первичную защиту
    try:
        session.get("https://pass.rw.by/ru/", timeout=15)
    except Exception:
        pass

    url = "https://pass.rw.by/api/v1/route/search/"
    params = {
        "from": from_code,
        "to": to_code,
        "date": s["date"],
        "lang": "ru"
    }
    r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    
    trains_raw = data.get("tp", [])
    result = []
    
    for t in trains_raw:
        train_num = t.get("number", "")
        cars = t.get("cars", [])
        free_seats_info = []
        has_free = False
        
        for car in cars:
            car_type = car.get("title", car.get("type", ""))
            free_count = car.get("free_num", car.get("places_count", 0))
            price = car.get("price", {})
            cost = price.get("cost") or price.get("amount")
            
            if free_count and int(free_count) > 0:
                has_free = True
                free_seats_info.append(f"{car_type}: своб. {free_count}" + (f" ({cost})" if cost else ""))
        
        result.append({
            "id": train_num,
            "has_seats": has_free,
            "text": f"Поезд {train_num}: " + (", ".join(free_seats_info) if free_seats_info else "Мест нет")
        })
        
    return result

def wanted_list(s):
    return [t.strip().lower() for t in s["train"].split(",") if t.strip()]

def train_matches(train_id, wanted):
    tid = train_id.lower()
    for w in wanted:
        if tid == w:
            return True
        if w.isdigit() and tid.startswith(w) and not tid[len(w):len(w) + 1].isdigit():
            return True
    return False

def select_trains(s, trains):
    wanted = wanted_list(s)
    if not wanted:
        return trains
    return [t for t in trains if train_matches(t["id"], wanted)]

def check(s):
    trains = select_trains(s, fetch_trains(s))
    if not trains:
        return "missing", "Поезд не найден в расписании на эту дату."
    free = [t for t in trains if t["has_seats"]]
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
        all_trains = fetch_trains(s)
        print(f"Поездов найдено через API: {len(all_trains)}")
        selected = select_trains(s, all_trains)
        for t in all_trains:
            mark = "  <== ваш" if t in selected and s["train"] else ""
            print(f"- {t['id']} | мест есть: {t['has_seats']}{mark}")
            print(f"    {t['text']}")
        status, info = check(s)
        print("Итог:", status, "|", info)
    except Exception as e:
        print("Ошибка при запросе к API:", e)

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
