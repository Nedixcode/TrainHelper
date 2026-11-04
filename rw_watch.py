#!/usr/bin/env python3
"""
Следит за свободными местами на pass.rw.by и пишет в Telegram.

Слежения лежат в watch.json:
  {"watches": [{"id": "...", "chat": "123456", "from": "Лунинец", "to": "Минск",
                "date": "2026-11-08", "train": "658Б"}, ...]}
Список ведёт Telegram-бот (Cloudflare Worker); у каждого слежения свой chat —
именно туда придёт уведомление.

Режимы:
  --once     одна проверка всех слежений (для GitHub Actions)
  --debug    показать, что скрипт видит на странице, ничего не отправляя;
             можно задать RW_FROM, RW_TO, RW_DATE, RW_TRAIN (иначе берётся
             первое слежение из watch.json)
  без флагов работает постоянно (проверка раз в RW_INTERVAL секунд)

Переменные окружения:
  TG_TOKEN       токен бота (нужен для отправки сообщений)
  RW_HELLO_ID    если задан — проверить только слежение с этим id и прислать
                 владельцу статус «слежу…» (так бот подтверждает новое слежение)
  RW_CONFIG      путь к файлу слежений (по умолчанию watch.json)

Установка: pip install requests beautifulsoup4
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

CONFIG_FILE = os.getenv("RW_CONFIG", "watch.json")
INTERVAL = int(os.getenv("RW_INTERVAL", "180"))  # для постоянного режима, сек
URL = "https://pass.rw.by/ru/route/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.9",
}
PRICE = re.compile(r"\d[\d\s]*,\d{2}\s*(byn|rub|eur|usd)", re.I)


# ---------- слежения ----------

def load_watches():
    """Читает watch.json. Старый формат (один объект без списка) игнорируется."""
    if not os.path.exists(CONFIG_FILE):
        return []
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("watch.json не читается:", e)
        return []
    items = data.get("watches") if isinstance(data, dict) else None
    result = []
    for w in items or []:
        if not isinstance(w, dict):
            continue
        item = {k: str(w.get(k, "")).strip() for k in ("id", "chat", "from", "to", "date", "train")}
        if item["chat"] and item["from"] and item["to"] and item["date"]:
            result.append(item)
    return result


def route_url(w):
    return URL + "?" + urlencode({"from": w["from"], "to": w["to"], "date": w["date"]})


def describe(w):
    return f"{w['from']} → {w['to']}, {w['date']}, поезд: {w['train'] or 'любой'}"


# ---------- Telegram ----------

def notify(chat, text):
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
        data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"},
        timeout=15,
    )
    r.raise_for_status()


def safe_notify(chat, text):
    """Одна недоставленная отправка (например, бот заблокирован) не должна ломать остальные."""
    try:
        notify(chat, text)
    except Exception as e:
        print("Не удалось отправить сообщение:", type(e).__name__)


# ---------- разбор страницы ----------

def get_page(w):
    session = requests.Session()
    session.headers.update(HEADERS)
    try:  # «прогрев»: главная страница выдаёт cookie, как обычному посетителю
        session.get("https://pass.rw.by/ru/", timeout=15)
    except Exception:
        pass
    params = {"from": w["from"], "to": w["to"], "date": w["date"]}
    r = session.get(URL, params=params, timeout=20)
    r.raise_for_status()
    return r, BeautifulSoup(r.text, "html.parser")


def _train_id(a):
    """Номер поезда из ссылки вида /ru/train/?train=658%D0%91&... -> '658Б'."""
    href = a.get("href", "")
    if "train=" not in href:
        return ""
    return parse_qs(urlparse(href).query).get("train", [""])[0]


def parse_trains(soup):
    """
    Находит блоки поездов по ссылкам на страницу поезда (/ru/train/?train=...),
    а не по CSS-классам. Для каждой ссылки поднимаемся вверх, пока в родителе
    только один поезд; самый верхний такой элемент — строка поезда.
    Возвращает список {"id": "658Б", "text": "..."}.
    """
    links = [a for a in soup.find_all("a", href=True) if _train_id(a)]
    trains, seen = [], set()
    for a in links:
        node = a
        while node.parent is not None and node.parent.name not in ("body", "html", "[document]"):
            ids = {_train_id(x) for x in node.parent.find_all("a", href=True) if _train_id(x)}
            if len(ids) > 1:
                break
            node = node.parent
        if id(node) in seen:
            continue
        seen.add(id(node))
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        trains.append({"id": _train_id(a), "text": text})
    return trains


def has_seats(text):
    """Места есть, если в строке поезда есть кнопка «Выбрать места» или цены."""
    t = text.lower()
    return "выбрать места" in t or bool(PRICE.search(t))


def wanted_list(w):
    return [t.strip().lower() for t in w["train"].split(",") if t.strip()]


def train_matches(train_id, wanted):
    """Совпадение по полному номеру ('658Б'); если введены только цифры ('658') —
    подойдёт поезд, номер которого начинается с них ('658Б')."""
    tid = train_id.lower()
    for x in wanted:
        if tid == x:
            return True
        if x.isdigit() and tid.startswith(x) and not tid[len(x):len(x) + 1].isdigit():
            return True
    return False


def select_trains(w, trains):
    wanted = wanted_list(w)
    if not wanted:
        return trains
    return [t for t in trains if train_matches(t["id"], wanted)]


def check_watch(w, cache):
    """Возвращает (статус, описание). Статус: seats / none / missing.
    Одинаковые маршруты и даты запрашиваются с сайта один раз за прогон."""
    key = (w["from"], w["to"], w["date"])
    if key not in cache:
        try:
            _, soup = get_page(w)
            cache[key] = ("ok", parse_trains(soup))
        except Exception as e:
            cache[key] = ("error", str(e))
        time.sleep(1.5)  # не торопим сайт
    kind, data = cache[key]
    if kind == "error":
        raise RuntimeError(data)
    trains = select_trains(w, data)
    if not trains:
        return "missing", ("Не нашёл такой поезд на странице. Проверь название станций, дату "
                           "и номер поезда (с буквой, как на сайте) — или поезд снят с продажи.")
    free = [t for t in trains if has_seats(t["text"])]
    if free:
        return "seats", free[0]["text"][:300]
    return "none", "Мест нет."


def warn_window():
    """Без хранения состояния предупреждение «поезд не найден» шлём не каждые
    10 минут, а раз в 6 часов (UTC 00, 06, 12, 18)."""
    t = time.gmtime()
    return t.tm_hour % 6 == 0 and t.tm_min < 10


# ---------- прогоны ----------

def run_once(hello_id=""):
    watches = load_watches()
    if not watches:
        print("Слежений нет — проверять нечего.")
        return
    today = date.today()
    cache = {}
    warn_ok = warn_window()
    print(f"Слежений: {len(watches)}")
    for w in watches:
        hello = bool(hello_id) and w["id"] == hello_id
        if hello_id and not hello:
            continue
        try:
            trip = datetime.strptime(w["date"], "%Y-%m-%d").date()
        except ValueError:
            print(f"[{w['id']}] неверная дата, пропускаю")
            continue
        if trip < today:
            continue  # бот уберёт такое слежение сам
        try:
            status, info = check_watch(w, cache)
        except Exception as e:
            print(f"[{w['id']}] ошибка: {e}")
            if hello:
                safe_notify(w["chat"], f"❌ Не удалось проверить сайт: {e}")
            continue
        print(f"[{w['id']}] {status}")
        link = route_url(w)
        if hello:
            state = {"seats": f"места есть! {info}", "none": "мест пока нет.",
                     "missing": f"⚠️ {info}"}[status]
            safe_notify(w["chat"], f"✅ Слежу за местами: {describe(w)}\nСейчас: {state}\n{link}")
        elif status == "seats":
            safe_notify(w["chat"], f"🚆 Появились места! {describe(w)}\n{info}\n{link}")
        elif status == "missing" and warn_ok:
            safe_notify(w["chat"], f"⚠️ {describe(w)}\n{info}")


def debug():
    w = {"id": "debug", "chat": "0",
         "from": os.getenv("RW_FROM", "").strip(), "to": os.getenv("RW_TO", "").strip(),
         "date": os.getenv("RW_DATE", "").strip(), "train": os.getenv("RW_TRAIN", "").strip()}
    if not (w["from"] and w["to"] and w["date"]):
        watches = load_watches()
        if not watches:
            print("Заполни поля «откуда», «куда», «дата» (или добавь слежение через бота).")
            return
        w = watches[0]
    r, soup = get_page(w)
    title = soup.title.get_text(strip=True) if soup.title else "—"
    all_trains = parse_trains(soup)
    chosen = select_trains(w, all_trains) if w["train"] else []
    print(f"Проверяю: {describe(w)}")
    print(f"HTTP {r.status_code} | размер страницы: {len(r.text)} | заголовок: {title}")
    print(f"Поездов на странице: {len(all_trains)}")
    for t in all_trains:
        mark = "  <== ваш" if t in chosen else ""
        print(f"- {t['id']} | места: {has_seats(t['text'])}{mark}")
        print(f"    {t['text'][:220]}")
    if not all_trains:
        body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        print("Поездов не видно. Начало текста страницы:")
        print(body[:500])
    status, info = check_watch(w, {})
    print("Итог:", status, "|", info)


def main():
    if "--debug" in sys.argv:
        debug()
        return
    once = "--once" in sys.argv
    hello_id = os.getenv("RW_HELLO_ID", "").strip()
    while True:
        try:
            run_once(hello_id)
        except Exception as e:
            print("Ошибка прогона:", e)
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
