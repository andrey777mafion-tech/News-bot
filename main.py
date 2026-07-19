# -*- coding: utf-8 -*-
"""
Бот-дайджест новостей для Telegram.
Как это работает:
  1. GitHub Actions запускает этот файл по расписанию (каждые ~15-30 минут).
  2. Скрипт смотрит: для какой категории СЕЙЧАС время присылать новости
     (по расписанию из config.json, время указано по Варшаве).
  3. Для этой категории скачивает несколько RSS-лент (источников).
  4. Похожие заголовки из разных источников группирует вместе —
     если новость есть у 2+ источников, помечает "подтверждено".
  5. Отправляет готовое сообщение в ваш чат в Telegram.
  6. Запоминает, какие ссылки уже отправлял (sent_state.json),
     чтобы не слать одно и то же дважды.
"""

import json
import os
import sys
import difflib
import datetime
from zoneinfo import ZoneInfo

import requests
import feedparser

CONFIG_PATH = "config.json"
STATE_PATH = "sent_state.json"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"sent_links": []}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_time_match(now_hm, send_times, window_minutes):
    """Проверяет, попадает ли текущее время в окно вокруг одного из времён рассылки."""
    now_dt = datetime.datetime.strptime(now_hm, "%H:%M")
    for t in send_times:
        t_dt = datetime.datetime.strptime(t, "%H:%M")
        diff = abs((now_dt - t_dt).total_seconds()) / 60
        if diff <= window_minutes:
            return True
    return False


def normalize(title):
    return "".join(ch.lower() for ch in title if ch.isalnum() or ch.isspace()).strip()


def group_similar(items, similarity_threshold=0.6):
    """Группирует похожие заголовки из разных источников в одну новость."""
    groups = []
    for item in items:
        norm = normalize(item["title"])
        placed = False
        for g in groups:
            base_norm = normalize(g[0]["title"])
            ratio = difflib.SequenceMatcher(None, norm, base_norm).ratio()
            if ratio > similarity_threshold:
                g.append(item)
                placed = True
                break
        if not placed:
            groups.append([item])
    return groups


def fetch_category_items(category, hours_back):
    items = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for feed_url in category["feeds"]:
        try:
            parsed = feedparser.parse(feed_url)
            source_name = getattr(parsed.feed, "get", lambda *a: None)("title") or feed_url
            for entry in parsed.entries:
                published = None
                if entry.get("published_parsed"):
                    published = datetime.datetime(*entry.published_parsed[:6], tzinfo=datetime.timezone.utc)
                elif entry.get("updated_parsed"):
                    published = datetime.datetime(*entry.updated_parsed[:6], tzinfo=datetime.timezone.utc)

                if published and (now - published).total_seconds() > hours_back * 3600:
                    continue

                title = (entry.get("title") or "").strip()
                link = entry.get("link") or ""
                if not title or not link:
                    continue

                items.append({
                    "title": title,
                    "link": link,
                    "source": source_name,
                    "published": published or now,
                })
        except Exception as e:
            print(f"[!] Ошибка чтения ленты {feed_url}: {e}")
    items.sort(key=lambda x: x["published"], reverse=True)
    return items


def build_message(category_name, groups, sent_links, max_items):
    lines = [f"📌 <b>{category_name}</b>"]
    count = 0
    new_links = []
    for g in groups:
        if count >= max_items:
            break
        main_item = g[0]
        if main_item["link"] in sent_links:
            continue
        sources = sorted(set(i["source"] for i in g))
        if len(sources) > 1:
            tag = " ✅ подтверждено (" + ", ".join(sources) + ")"
        else:
            tag = f" — {sources[0]}"
        title = main_item["title"].replace("<", "").replace(">", "")
        lines.append(f'• <a href="{main_item["link"]}">{title}</a>{tag}')
        new_links.append(main_item["link"])
        count += 1

    if count == 0:
        return None, []
    return "\n".join(lines), new_links


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=30)
    if resp.status_code != 200:
        print("[!] Ошибка отправки в Telegram:", resp.text)
    return resp.status_code == 200


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("Не заданы переменные окружения BOT_TOKEN / CHAT_ID")
        sys.exit(1)

    config = load_config()
    state = load_state()
    sent_links = set(state.get("sent_links", []))

    tz = ZoneInfo(config.get("timezone", "Europe/Warsaw"))
    now_local = datetime.datetime.now(tz)
    now_hm = now_local.strftime("%H:%M")

    # Для ручного теста можно передать переменную FORCE_CATEGORY=sport,
    # тогда время расписания игнорируется и новости отправляются сразу.
    force = os.environ.get("FORCE_CATEGORY")

    window = config.get("window_minutes", 12)
    hours_back = config.get("hours_back", 20)
    max_items = config.get("max_items", 8)

    any_sent = False

    for category in config["categories"]:
        if not category.get("enabled", True):
            continue
        if force:
            if category["key"] != force:
                continue
        else:
            if not is_time_match(now_hm, category["send_times"], window):
                continue

        print(f"-> Проверяю категорию: {category['name']}")
        items = fetch_category_items(category, hours_back)
        if not items:
            print("   новостей не найдено")
            continue

        groups = group_similar(items)
        message, new_links = build_message(category["name"], groups, sent_links, max_items)
        if message:
            ok = send_telegram_message(message)
            if ok:
                sent_links.update(new_links)
                any_sent = True
                print(f"   отправлено {len(new_links)} новостей")
        else:
            print("   всё уже было отправлено ранее")

    # ограничиваем размер файла состояния, чтобы он не рос бесконечно
    state["sent_links"] = list(sent_links)[-800:]
    save_state(state)

    if not any_sent:
        print("Сейчас не время рассылки ни для одной категории (или всё уже отправлено).")


if __name__ == "__main__":
    main()
