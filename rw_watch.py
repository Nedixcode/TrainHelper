#!/usr/bin/env python3
"""
Следит за свободными местами на pass.rw.by и шлёт уведомление в Telegram.

Настройки берутся из переменных окружения RW_FROM, RW_TO, RW_DATE, RW_TRAIN,
а если их нет — из файла watch.json (его создаёт GitHub Actions при запуске
«Run workflow» с mode=start). RW_TRAIN можно оставить пустым (любой поезд)
или указать один/несколько номеров через запятую: "649Б, 701Б".

Секреты: TG_TOKEN и TG_CHAT_ID (нужны, кроме режима --debug).

Режимы:
  --debug   показать, что скрипт видит на странице (ничего не отправляет)
  --once    одна проверка (для cron / GitHub Actions)
  --hello   вместе с --once: прислать в Telegram сообщение «слежение запущено»
  (без флагов)  работает постоянно, проверка раз в RW_INTERVAL секунд

Установка: pip install requests beautifulsoup4
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
INTERVAL = int(os.getenv("RW_INTERVAL", "180"))  # для постоянного режима, сек
URL = "https://pass.rw.by/ru/route/"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def load_settings():
    """Переменные окружения важнее файла watch.json. None — если не настроено."""
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


def fetch_rows(s):
    """Список строк-поездов со страницы результатов."""
    params = {"from": s["from"], "to": s["to"], "date": s["date"],
              "from_exp": "", "from_esr": "", "to_exp": "", "to_esr": ""}
    r = requests.get(URL, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # ВАЖНО: селектор может измениться, если сайт поменяет вёрстку.
    # Если --debug показывает 0 строк, открой страницу в браузере, нажми
    # F12 -> Elements и поправь селектор здесь.
    return soup.select(".sch-table__row")


def row_text(row):
    return re.sub(r"\s+", " ", row.get_text(" ", strip=True))


def has_seats(row):
    """Эвристика: в строке есть числа мест и нет надписи про отсутствие мест."""
    text = row_text(row).lower()
    if "нет мест" in text:
        return False
    # элементы с количеством мест (класс может отличаться — проверь через --debug)
    quants = row.select("[class*=t-quant], [class*=ticket-quantity]")
    if quants:
        return any(re.search(r"[1-9]", q.get_text()) for q in quants)
    return False


def select_rows(s, rows):
    trains = [t.strip().lower() for t in s["train"].split(",") if t.strip()]
    if trains:
        rows = [r for r in rows if any(t in row_text(r).lower() for t in trains)]
    return rows


def check(s):
    """Возвращает (статус, описание). Статус: seats / none / missing."""
    rows = select_rows(s, fetch_rows(s))
    if not rows:
        return "missing", "Поезд не найден на странице (изменилась вёрстка, поезд снят или номер указан неверно)."
    free = [r for r in rows if has_seats(r)]
    if free:
        return "seats", row_text(free[0])[:300]
    return "none", "Мест нет."


def describe(s):
    return f"{s['from']} → {s['to']}, {s['date']}, поезд: {s['train'] or 'любой'}"


def main():
    debug = "--debug" in sys.argv
    once = "--once" in sys.argv
    hello = "--hello" in sys.argv

    s = load_settings()
    if s is None:
        print("Слежение не настроено (нет watch.json и переменных RW_*) — проверять нечего.")
        return
    try:
        trip = datetime.strptime(s["date"], "%Y-%m-%d").date()
    except ValueError:
        sys.exit("Дата должна быть в формате ГГГГ-ММ-ДД, например 2026-11-20.")
    if trip < date.today():
        print(f"Дата поездки ({s['date']}) уже прошла — слежение не нужно. "
              "Остановите его (Run workflow → mode=stop).")
        return

    if debug:
        rows = fetch_rows(s)
        print(f"Настройки: {describe(s)}")
        print(f"Всего строк на странице: {len(rows)}")
        for r in select_rows(s, rows):
            print("-", row_text(r)[:200], "| места:", has_seats(r))
        status, info = check(s)
        print("Итог:", status, info)
        return

    last = None
    warned_missing = False
    while True:
        try:
            status, info = check(s)
            print(time.strftime("%H:%M:%S"), status, flush=True)
            if hello:
                state = {"seats": f"места есть! {info}", "none": "мест пока нет.",
                         "missing": f"⚠️ {info}"}[status]
                notify(f"✅ Слежу за местами: {describe(s)}\nСейчас: {state}\n{route_url(s)}")
            elif status == "seats" and last != "seats":
                notify(f"🚆 Появились места! {describe(s)}\n{info}\n{route_url(s)}")
            elif status == "missing" and not warned_missing:
                notify(f"⚠️ {describe(s)}\n{info} Проверь скрипт.")
                warned_missing = True
            if status != "missing":
                warned_missing = False
            last = status
        except Exception as e:  # сеть/сайт временно недоступны — не падаем
            print("Ошибка:", e, flush=True)
            if hello:
                try:
                    notify(f"❌ Не удалось проверить сайт: {e}")
                except Exception:
                    pass
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
