import os
import json
import logging
from datetime import datetime, timedelta
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import redis
from notion_client import Client as NotionClient
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
redis_client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
notion = NotionClient(auth=os.environ["NOTION_API_KEY"])

SYSTEM_PROMPT = """Voce e o Jarvis, assistente pessoal de IA do Sebastian. Responda em portugues do Brasil.
Voce tem acesso ao Notion e ao Google Calendar do Sebastian. Pode ler, criar e modificar eventos quando solicitado."""


def get_calendar_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)


def get_events(days_ahead=1, max_results=10):
    service = get_calendar_service()
    now = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(days=days_ahead)).isoformat() + "Z"
    events_result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        maxResults=max_results,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return events_result.get("items", [])


def format_event(event):
    start = event["start"].get("dateTime", event["start"].get("date", ""))
    if "T" in start:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        start_str = dt.strftime("%d/%m %H:%M")
    else:
        start_str = start
    summary = event.get("summary", "Sem titulo")
    location = event.get("location", "")
    loc_str = f" {location}" if location else ""
    return f"- {start_str} -- {summary}{loc_str}"


def create_event(summary, start_dt, end_dt=None, description=""):
    service = get_calendar_service()
    if end_dt is None:
        end_dt = start_dt + timedelta(hours=1)
    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
    }
    created = service.events().insert(calendarId="primary", body=event_body).execute()
    return created


def get_history(user_id):
    data = redis_client.get(f"history:{user_id}")
    if data:
        return json.loads(data)
    return []


def save_history(user_id, history):
    redis_client.set(f"history:{user_id}", json.dumps(history))


def search_notion(query):
    results = notion.search(query=query, filter={"value": "page", "property": "object"})
    pages = []
    for page in results.get("results", [])[:5]:
        title = ""
        props = page.get("properties", {})
        for prop_name, prop_value in props.items():
            if prop_value.get("type") == "title":
                title_arr = prop_value.get("title", [])
                if title_arr:
                    title = title_arr[0].get("plain_text", "")
                break
        pages.append({"id": page["id"], "title": title or "Untitled", "url": page.get("url", "")})
    return pages


def get_page_content(page_id):
    try:
        blocks = notion.blocks.children.list(block_id=page_id)
        content = []
        for block in blocks.get("results", []):
            block_type = block.get("type")
            if block_type in ["paragraph", "heading_1", "heading_2", "heading_3",
                               "bulleted_list_item", "numbered_list_item", "to_do", "quote", "callout"]:
                rich_text = block.get(block_type, {}).get("rich_text", [])
                text = "".join([t.get("plain_text", "") for t in rich_text])
                if text:
                    content.append(text)
        return "\n".join(content)
    except Exception as e:
        logger.error(f"Error getting page content: {e}")
        return ""


def append_to_page(page_id, text):
    notion.blocks.children.append(
        block_id=page_id,
        children=[{"object": "block", "type": "paragraph",
                   "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}]
    )


async def start(update, context):
    user = update.effective_user
    save_history(user.id, [])
    await update.message.reply_text(
        f"Ola {user.first_name}! Sou o Jarvis\n\n"
        "Tenho acesso ao seu Notion e Google Calendar. Como posso ajudar?\n\n"
        "Use /help para ver os comandos disponiveis."
    )


async def reset(update, context):
    save_history(update.effective_user.id, [])
    await update.message.reply_text("Conversa reiniciada!")


async def help_command(update, context):
    await update.message.reply_text(
        "Comandos disponiveis:\n\n"
        "Geral:\n"
        "/start - Iniciar\n"
        "/reset - Reiniciar conversa\n"
        "/help - Esta mensagem\n\n"
        "Google Calendar:\n"
        "/agenda - Eventos de hoje\n"
        "/amanha - Eventos de amanha\n"
        "/proximos - Proximos 7 dias\n"
        "/agendar <titulo> | <data hora> - Criar evento\n"
        "  Ex: /agendar Reuniao | 2024-03-15 14:00\n\n"
        "Notion:\n"
        "/buscar <termo> - Buscar paginas\n"
        "/ler <page-id> - Ler pagina"
    )


async def agenda_hoje(update, context):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        events = get_events(days_ahead=1)
        if not events:
            await update.message.reply_text("Nenhum evento para hoje.")
            return
        lines = ["Agenda de hoje:\n"]
        for e in events:
            lines.append(format_event(e))
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.error(f"Calendar error: {e}")
        await update.message.reply_text("Erro ao acessar o Google Calendar.")


async def agenda_amanha(update, context):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        now = datetime.utcnow()
        tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_end = tomorrow_start + timedelta(days=1)
        service = get_calendar_service()
        events_result = service.events().list(
            calendarId="primary",
            timeMin=tomorrow_start.isoformat() + "Z",
            timeMax=tomorrow_end.isoformat() + "Z",
            maxResults=10,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = events_result.get("items", [])
        if not events:
            await update.message.reply_text("Nenhum evento para amanha.")
            return
        lines = ["Agenda de amanha:\n"]
        for e in events:
            lines.append(format_event(e))
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.error(f"Calendar error: {e}")
        await update.message.reply_text("Erro ao acessar o Google Calendar.")


async def proximos_eventos(update, context):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        events = get_events(days_ahead=7, max_results=15)
        if not events:
            await update.message.reply_text("Nenhum evento nos proximos 7 dias.")
            return
        lines = ["Proximos 7 dias:\n"]
        for e in events:
            lines.append(format_event(e))
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.error(f"Calendar error: {e}")
        await update.message.reply_text("Erro ao acessar o Google Calendar.")


async def agendar_evento(update, context):
    if not context.args:
        await update.message.reply_text(
            "Use: /agendar <titulo> | <data hora>\nEx: /agendar Reuniao | 2024-03-15 14:00"
        )
        return
    raw = " ".join(context.args)
    if "|" not in raw:
        await update.message.reply_text(
            "Formato: /agendar <titulo> | <data hora>\nEx: /agendar Reuniao | 2024-03-15 14:00"
        )
        return
    parts = raw.split("|", 1)
    title = parts[0].strip()
    date_str = parts[1].strip()
    try:
        start_dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        created = create_event(title, start_dt)
        link = created.get("htmlLink", "")
        await update.message.reply_text(
            f"Evento criado!\n{title}\n{start_dt.strftime('%d/%m/%Y as %H:%M')}\n{link}"
        )
    except ValueError:
        await update.message.reply_text("Formato de data invalido. Use: YYYY-MM-DD HH:MM")
    except Exception as e:
        logger.error(f"Create event error: {e}")
        await update.message.reply_text("Erro ao criar o evento.")


async def notion_search_command(update, context):
    if not context.args:
        await update.message.reply_text("Use: /buscar <termo>")
        return
    query = " ".join(context.args)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    pages = search_notion(query)
    if not pages:
        await update.message.reply_text(f"Nenhuma pagina encontrada para '{query}'")
        return
    response = f"Encontrei {len(pages)} pagina(s) para '{query}':\n\n"
    for i, page in enumerate(pages, 1):
        response += f"{i}. {page['title']}\nID: {page['id']}\n\n"
    await update.message.reply_text(response)


async def notion_read_command(update, context):
    if not context.args:
        await update.message.reply_text("Use: /ler <page-id>")
        return
    page_id = context.args[0]
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    content = get_page_content(page_id)
    if not content:
        await update.message.reply_text("Pagina vazia ou nao encontrada.")
        return
    if len(content) > 4000:
        content = content[:4000] + "...\n[Conteudo truncado]"
    await update.message.reply_text(content)


async def handle_message(update, context):
    user = update.effective_user
    user_message = update.message.text
    history = get_history(user.id)

    calendar_keywords = ["agenda", "evento", "calendar", "reuniao", "horario",
                         "amanha", "hoje", "semana", "compromisso", "agendar", "marcar"]
    notion_keywords = ["notion", "anotacao", "nota", "pagina", "cerebro", "documento"]

    should_search_calendar = any(kw in user_message.lower() for kw in calendar_keywords)
    should_search_notion = any(kw in user_message.lower() for kw in notion_keywords)

    extra_context = ""

    if should_search_calendar:
        try:
            events = get_events(days_ahead=7, max_results=10)
            if events:
                extra_context += "\n\nEventos proximos no Google Calendar:\n"
                for e in events:
                    extra_context += format_event(e) + "\n"
        except Exception as e:
            logger.error(f"Calendar context error: {e}")

    if should_search_notion:
        pages = search_notion(user_message)
        if pages:
            extra_context += "\n\nConteudo relevante do Notion:\n"
            for page in pages[:2]:
                content = get_page_content(page["id"])
                if content:
                    extra_context += f"\n## {page['title']}\n{content[:500]}\n"

    system = SYSTEM_PROMPT
    if extra_context:
        system += extra_context

    history.append({"role": "user", "content": user_message})
    if len(history) > 20:
        history = history[-20:]

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        response = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=system,
            messages=history
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})
        save_history(user.id, history)
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Erro ao processar. Tente novamente.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("agenda", agenda_hoje))
    app.add_handler(CommandHandler("amanha", agenda_amanha))
    app.add_handler(CommandHandler("proximos", proximos_eventos))
    app.add_handler(CommandHandler("agendar", agendar_evento))
    app.add_handler(CommandHandler("buscar", notion_search_command))
    app.add_handler(CommandHandler("ler", notion_read_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
