from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from openai import OpenAI

from . import db

TZ = ZoneInfo("Europe/Moscow")
WEEKDAYS = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
ACCUSATIVE = {
    "task": "задачу",
    "meeting": "встречу",
    "reminder": "напоминание",
}


def now_msk() -> datetime:
    return datetime.now(TZ)


def iso_date(value: datetime) -> str:
    return value.strftime("%Y-%m-%d")


def current_context() -> dict[str, str]:
    moment = now_msk()
    return {
        "now": moment.strftime("%Y-%m-%d %H:%M"),
        "today": iso_date(moment),
        "weekday": WEEKDAYS[moment.weekday()],
        "tomorrow": iso_date(moment + timedelta(days=1)),
        "after_tomorrow": iso_date(moment + timedelta(days=2)),
        "week_end": iso_date(moment + timedelta(days=6)),
    }


def human_date(value: str | None) -> str:
    if not value:
        return "без даты"
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return value
    today = now_msk().date()
    pretty = f"{parsed.day} {MONTHS[parsed.month - 1]}"
    if parsed.year != today.year:
        pretty = f"{pretty} {parsed.year}"
    delta = (parsed - today).days
    if delta == 0:
        return f"сегодня, {pretty}"
    if delta == 1:
        return f"завтра, {pretty}"
    if delta == 2:
        return f"послезавтра, {pretty}"
    return f"{WEEKDAYS[parsed.weekday()]}, {pretty}"


def human_when(item: dict[str, Any]) -> str:
    date_part = human_date(item.get("date"))
    time_part = item.get("time")
    if time_part:
        return f"{date_part} в {time_part}"
    return date_part


def spoken_item(item: dict[str, Any]) -> str:
    title = (item.get("title") or "без названия").strip()
    return f"{title} — {human_when(item)}"


def spoken_short(item: dict[str, Any]) -> str:
    title = (item.get("title") or "без названия").strip()
    time_part = item.get("time")
    if time_part:
        return f"в {time_part} {title}"
    return f"{title} ({human_date(item.get('date'))})"


def build_system_prompt(ctx: dict[str, str]) -> str:
    return f"""Ты ассистент личного календаря. Твоя работа — понять свободную речь пользователя и вернуть JSON для программы. Пользователь не обязан знать команды.

Сейчас: {ctx['now']}, {ctx['weekday']}, Москва.
Сегодня {ctx['today']}. Завтра {ctx['tomorrow']}. Послезавтра {ctx['after_tomorrow']}.
Текущая неделя: {ctx['today']} … {ctx['week_end']}.

Намерения:
- create: создать задачу, напоминание или встречу.
- list: что сегодня / завтра / на неделе / на дату.
- search: найти старые или любые записи по смыслу, человеку, теме. Не требуй точных слов.
- complete / delete / update: только если из диалога или контекста однозначно виден event_id.
- clarify: не хватает обязательного слота. Для встречи время обязательно.
- chat: просто ответить, базу не менять.

Одинаковый смысл:
«что у меня сегодня?» = «какие планы на сегодня?» = «покажи встречи на сегодня» → list, date_from=date_to=сегодня.
«напомни завтра в 10 позвонить Ивану» → create, reminder.
«в пятницу нужно отправить отчет» → create, task, ближайшая будущая пятница.
«добавь встречу с командой завтра в 15:00» → create, meeting.
«найди задачу про презентацию» → search.

Верни ТОЛЬКО JSON, без markdown:
{{
  "action": "create|list|search|complete|delete|update|clarify|chat",
  "event_type": "task|meeting|reminder|null",
  "title": "название без даты и времени, или null",
  "description": "1-2 предложения, о чём запись, или null",
  "date": "YYYY-MM-DD или null",
  "time": "HH:MM или null",
  "date_from": "YYYY-MM-DD или null",
  "date_to": "YYYY-MM-DD или null",
  "query": "короткий поисковый запрос или null",
  "event_id": число или null,
  "status": "open|done|null",
  "reply": "короткий живой ответ по-русски"
}}

Правила reply:
- Говори как спокойная помощница: коротко, по делу, 1-3 предложения.
- Никогда не называй id, номера записей, #, JSON, названия полей.
- Не выдумывай события. Не говори «ищу» — либо результат, либо «не нашла».
- Не пиши «слушаю дальше», «хорошо, слушаю», «продолжение следует». Если фраза не про дела, скажи, что не разобрала, и попроси повторить.
- Даты словами: «завтра в 10:00», не «2026-11-01».
- title без слов «завтра», «в 15:00».
"""


def parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM returned non-object JSON")
    return data


def compact_event(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "type": item["event_type"],
        "title": item["title"],
        "description": item.get("description"),
        "date": item["date"],
        "time": item["time"],
        "when": human_when(item),
        "status": item["status"],
    }


def sanitize_reply(text: str) -> str:
    text = re.sub(r"#\s*\d+", "", text)
    text = re.sub(r"\b(id|ID)\s*[:=]?\s*\d+", "", text)
    text = re.sub(r"номер[а-яё]*\s+\d+", "", text, flags=re.I)
    text = re.sub(r"номер[а-яё]*\s+(встреч[а-яё]*|задач[а-яё]*|запис[а-яё]*)", "", text, flags=re.I)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    return text.strip()


def is_good_reply(text: str) -> bool:
    if not text or len(text) < 8 or text.startswith("{") or "```" in text:
        return False
    if re.search(r"#\s*\d+", text) or re.search(r"\b(id|ID)\s*\d+", text):
        return False
    if re.search(r"номер\s+\d+", text, re.I):
        return False
    return True


def list_reply(prefix: str, events: list[dict[str, Any]], empty: str) -> str:
    if not events:
        return empty
    shown = events[:4]
    same_day = len({item.get("date") for item in shown}) == 1
    parts = [spoken_short(item) if same_day else spoken_item(item) for item in shown]
    extra = len(events) - len(shown)
    if len(parts) == 1:
        body = parts[0]
    elif len(parts) == 2:
        body = f"{parts[0]} и {parts[1]}"
    else:
        body = ", ".join(parts[:-1]) + f" и {parts[-1]}"
    text = f"{prefix} {body}."
    if extra > 0:
        text = text[:-1] + f", и ещё {extra}."
    return text


def pick_reply(model_reply: str, fallback: str) -> str:
    model_reply = sanitize_reply(model_reply)
    if is_good_reply(model_reply):
        return model_reply
    return fallback


class Assistant:
    def __init__(self, conn, client: OpenAI, model: str):
        self.conn = conn
        self.client = client
        self.model = model

    def think(self, user_text: str, history: list[dict[str, str]]) -> dict[str, Any]:
        ctx = current_context()
        upcoming = db.upcoming(self.conn, ctx["today"], limit=12)
        found = db.search_events(self.conn, user_text, limit=8)
        payload = {
            "current": ctx,
            "upcoming_open_events": [compact_event(item) for item in upcoming],
            "search_candidates": [compact_event(item) for item in found],
        }
        messages = [
            {"role": "system", "content": build_system_prompt(ctx)},
            {
                "role": "system",
                "content": "Служебный контекст из базы. Поля id только для выбора action, в reply их не произносить:\n"
                + json.dumps(payload, ensure_ascii=False),
            },
        ]
        for turn in history[-8:]:
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": user_text})

        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            messages=messages,
        )
        raw = response.choices[0].message.content or "{}"
        try:
            plan = parse_json(raw)
        except Exception:
            plan = {"action": "chat", "reply": "Не совсем поняла запрос. Можно ещё раз, своими словами?"}
        return self.execute(plan, user_text, ctx)

    def execute(self, plan: dict[str, Any], user_text: str, ctx: dict[str, str]) -> dict[str, Any]:
        action = (plan.get("action") or "chat").strip().lower()
        model_reply = sanitize_reply((plan.get("reply") or "").strip())
        created = None
        events: list[dict[str, Any]] = []
        reply = model_reply

        if action == "create":
            title = (plan.get("title") or user_text).strip()
            event_type = plan.get("event_type") or db.classify_title(title)
            date = plan.get("date")
            time = plan.get("time")
            description = plan.get("description")
            if event_type == "meeting" and not time:
                action = "clarify"
                reply = pick_reply(model_reply, "На какое время поставить встречу?")
            else:
                created = db.create_event(
                    self.conn,
                    event_type=event_type,
                    title=title,
                    date=date,
                    time=time,
                    description=description,
                )
                events = [created]
                kind = ACCUSATIVE.get(created["event_type"], "запись")
                bare = (created.get("title") or "").strip().lower()
                if bare in {"встреча", "задача", "напоминание", "запись"}:
                    fallback = f"Поставила {kind} {human_when(created)}."
                else:
                    fallback = f"Поставила {kind}: {spoken_item(created)}."
                if event_type == "meeting" and date and time:
                    conflicts = db.find_conflicts(self.conn, date, time, exclude_id=created["id"])
                    if conflicts:
                        fallback += " Только учти: на это же время уже есть " + " и ".join(
                            spoken_item(item) for item in conflicts[:2]
                        ) + "."
                reply = fallback

        elif action == "list":
            date_from = plan.get("date_from") or plan.get("date")
            date_to = plan.get("date_to") or plan.get("date")
            events = db.list_events(self.conn, date_from=date_from, date_to=date_to, limit=50)
            if date_from and date_from == date_to == ctx["today"]:
                prefix = "На сегодня"
                empty = "На сегодня ничего не стоит."
            elif date_from and date_from == date_to == ctx["tomorrow"]:
                prefix = "На завтра"
                empty = "На завтра пока пусто."
            else:
                prefix = "В календаре"
                empty = "На этот период ничего нет."
            reply = list_reply(prefix, events, empty)

        elif action == "search":
            query = plan.get("query") or user_text
            events = db.search_events(self.conn, query, limit=20)
            if not events and query != user_text:
                events = db.search_events(self.conn, user_text, limit=20)
            reply = list_reply("Нашла", events, "Ничего похожего не нашла.")

        elif action == "complete":
            event_id = plan.get("event_id")
            if event_id:
                updated = db.update_event(self.conn, int(event_id), status="done")
                if updated:
                    events = [updated]
                    reply = pick_reply(model_reply, f"Отметила как выполненную: {spoken_item(updated)}.")
                else:
                    reply = "Такую запись не нашла."
            else:
                reply = model_reply or "Какую задачу отметить выполненной?"

        elif action == "delete":
            event_id = plan.get("event_id")
            if event_id:
                existing = db.get_event(self.conn, int(event_id))
                if existing and db.delete_event(self.conn, int(event_id)):
                    events = [existing]
                    reply = pick_reply(model_reply, f"Удалила: {spoken_item(existing)}.")
                else:
                    reply = "Такую запись не нашла."
            else:
                reply = model_reply or "Какую запись удалить?"

        elif action == "update":
            event_id = plan.get("event_id")
            if event_id:
                updated = db.update_event(
                    self.conn,
                    int(event_id),
                    title=plan.get("title"),
                    date=plan.get("date"),
                    time=plan.get("time"),
                    event_type=plan.get("event_type"),
                    status=plan.get("status"),
                    description=plan.get("description"),
                )
                if updated:
                    events = [updated]
                    reply = pick_reply(model_reply, f"Обновила: {spoken_item(updated)}.")
                else:
                    reply = "Такую запись не нашла."
            else:
                reply = model_reply or "Что именно изменить?"

        elif action == "clarify":
            reply = model_reply or "Нужно чуть больше деталей. Например, время встречи."

        else:
            reply = model_reply or "Могу поставить задачу, встречу или найти, что уже есть в календаре."
            if re.search(r"слушаю дальше|продолжение следует", reply, re.I):
                reply = "Не разобрала. Скажите задачу, встречу или спросите, что запланировано."

        reply = sanitize_reply(reply)

        return {
            "action": action,
            "reply": reply,
            "events": events,
            "created": created,
            "today": ctx["today"],
        }
