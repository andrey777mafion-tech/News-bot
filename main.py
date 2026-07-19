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
import random
import datetime
from zoneinfo import ZoneInfo

import requests
import feedparser
from deep_translator import GoogleTranslator

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


def fetch_crypto_rates(crypto_list):
    """Курсы криптовалют через CoinGecko (бесплатно, без ключа)."""
    if not crypto_list:
        return ""
    ids = ",".join(c["id"] for c in crypto_list)
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
        resp = requests.get(url, timeout=15)
        data = resp.json()
        parts = []
        for c in crypto_list:
            d = data.get(c["id"], {})
            price = d.get("usd")
            change = d.get("usd_24h_change")
            if price is None:
                continue
            change_str = f" ({change:+.1f}%)" if change is not None else ""
            parts.append(f"{c['symbol']}: ${price:,.0f}{change_str}")
        return " | ".join(parts)
    except Exception as e:
        print(f"[!] Ошибка получения курсов крипты: {e}")
        return ""


def fetch_currency_rates(base, targets):
    """Курсы валют через open.er-api.com (бесплатно, без ключа)."""
    if not targets:
        return ""
    try:
        url = f"https://open.er-api.com/v6/latest/{base}"
        resp = requests.get(url, timeout=15)
        data = resp.json()
        rates = data.get("rates", {})
        parts = []
        for t in targets:
            r = rates.get(t)
            if r is None:
                continue
            parts.append(f"{base}/{t}: {r:.2f}")
        return " | ".join(parts)
    except Exception as e:
        print(f"[!] Ошибка получения курсов валют: {e}")
        return ""


def fetch_stock_rates(stocks):
    """Котировки индексов/акций через публичный (неофициальный) эндпоинт Yahoo Finance."""
    if not stocks:
        return ""
    parts = []
    for s in stocks:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{s['symbol']}"
            resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            data = resp.json()
            result = data["chart"]["result"][0]
            meta = result["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("previousClose") or meta.get("chartPreviousClose")
            if price is None:
                continue
            change_str = ""
            if prev:
                change_pct = (price - prev) / prev * 100
                change_str = f" ({change_pct:+.1f}%)"
            parts.append(f"{s['name']}: {price:,.0f}{change_str}")
        except Exception as e:
            print(f"[!] Ошибка получения котировки {s.get('symbol')}: {e}")
    return " | ".join(parts)


def build_rates_block(config):
    rates_cfg = config.get("rates", {})
    lines = []

    crypto_line = fetch_crypto_rates(rates_cfg.get("crypto", []))
    if crypto_line:
        lines.append(f"₿ <b>Крипто:</b> {crypto_line}")

    currency_cfg = rates_cfg.get("currencies", {})
    currency_line = fetch_currency_rates(currency_cfg.get("base", "USD"), currency_cfg.get("targets", []))
    if currency_line:
        lines.append(f"💵 <b>Валюты:</b> {currency_line}")

    stock_line = fetch_stock_rates(rates_cfg.get("stocks", []))
    if stock_line:
        lines.append(f"📈 <b>Акции:</b> {stock_line}")

    return lines


def translate_to_ru(text):
    """Переводит заголовок на русский. Если перевод не удался (сайт перевода
    временно недоступен) - возвращает исходный текст, чтобы бот не падал."""
    try:
        translated = GoogleTranslator(source="auto", target="ru").translate(text)
        return translated or text
    except Exception as e:
        print(f"[!] Не удалось перевести заголовок: {e}")
        return text


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


def build_message(category, groups, sent_links, max_items, config):
    category_name = category["name"]
    category_type = category.get("type")  # "finance" / "crypto" / None

    # Собираем все новые (ещё не отправленные) новости, самые свежие - первые
    fresh_groups = []
    for g in groups:
        if g[0]["link"] not in sent_links:
            fresh_groups.append(g)
        if len(fresh_groups) >= max_items:
            break

    if not fresh_groups:
        return None, []

    def format_item(number, g):
        main_item = g[0]
        sources = sorted(set(i["source"] for i in g))
        if len(sources) > 1:
            tag = "✅ <i>подтверждено: " + ", ".join(sources) + "</i>"
        else:
            tag = f"<i>{sources[0]}</i>"
        original_title = main_item["title"].replace("<", "").replace(">", "")
        title_ru = translate_to_ru(original_title)
        return [f'{number}. <a href="{main_item["link"]}">{title_ru}</a>', f"   {tag}", ""]

    lines = [f"📌 <b>{category_name}</b>", ""]
    new_links = []

    top_count = min(3, len(fresh_groups))
    lines.append("<b>Главные события:</b>")
    lines.append("")
    for i in range(top_count):
        lines.extend(format_item(i + 1, fresh_groups[i]))
        new_links.append(fresh_groups[i][0]["link"])

    # Для финансов и крипты - вставляем блок с курсами
    if category_type in ("finance", "crypto"):
        rate_lines = build_rates_block(config)
        if rate_lines:
            lines.append("<b>Курсы на сейчас:</b>")
            lines.extend(rate_lines)
            lines.append("")

    # Остальные новости категории
    rest = fresh_groups[top_count:max_items]
    if rest:
        if category_type in ("finance", "crypto"):
            lines.append("<b>Ещё новости по теме (оцените сами, что может быть полезно):</b>")
        else:
            lines.append("<b>Ещё новости:</b>")
        lines.append("")
        for i, g in enumerate(rest):
            lines.extend(format_item(top_count + i + 1, g))
            new_links.append(g[0]["link"])

    # Общий образовательный совет (не индивидуальная финансовая рекомендация)
    tips = config.get("tips", {}).get(category_type, [])
    if tips:
        lines.append(f"💡 <i>{random.choice(tips)}</i>")

    return "\n".join(lines).strip(), new_links


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
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

    # Для ручного запуска можно передать переменную FORCE_CATEGORY:
    #   FORCE_CATEGORY=sport   -> прислать сразу только спорт, игнорируя расписание
    #   FORCE_CATEGORY=all     -> прислать сразу ВСЕ включённые категории
    # Без этой переменной бот работает по обычному расписанию (send_times в config.json).
    force = os.environ.get("FORCE_CATEGORY")

    window = config.get("window_minutes", 12)
    hours_back = config.get("hours_back", 20)
    max_items = config.get("max_items", 8)

    any_sent = False

    for category in config["categories"]:
        if not category.get("enabled", True):
            continue
        if force:
            if force != "all" and category["key"] != force:
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
        message, new_links = build_message(category, groups, sent_links, max_items, config)
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
