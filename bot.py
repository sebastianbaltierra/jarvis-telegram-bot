import os
import json
import logging
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import redis
from notion_client import Client as NotionClient

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
redis_client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
notion = NotionClient(auth=os.environ["NOTION_API_KEY"])
SYSTEM_PROMPT = """Voce e o Jarvis, assistente pessoal de IA do Sebastian. Responda em portugues do Brasil.
Voce tem acesso ao Notion do Sebastian e pode ler e modificar suas paginas quando solicitado."""

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
            if block_type in ["paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item", "to_do", "quote", "callout"]:
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
        children=[{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}]
    )

async def start(update, context):
    user = update.effective_user
    save_history(user.id, [])
    await update.message.reply_text(f"Ola {user.first_name}! Sou o Jarvis. Tenho acesso ao seu Notion. Como posso ajudar?")

async def reset(update, context):
    save_history(update.effective_user.id, [])
    await update.message.reply_text("Conversa reiniciada!")

async def help_command(update, context):
    await update.message.reply_text("/start - Iniciar\n/reset - Reiniciar conversa\n/buscar <termo> - Buscar no Notion\n/ler <page-id> - Ler pagina do Notion\n/help - Ajuda")

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
        response += f"{i}. {page['title']}\nID: `{page['id']}`\n\n"
    await update.message.reply_text(response, parse_mode="Markdown")

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
    notion_keywords = ["notion", "anotacao", "anotacao", "nota", "pagina", "pagina", "cerebro", "cerebro"]
    should_search_notion = any(kw in user_message.lower() for kw in notion_keywords)
    notion_context = ""
    if should_search_notion:
        pages = search_notion(user_message)
        if pages:
            notion_context = "\n\nConteudo relevante do Notion:\n"
            for page in pages[:2]:
                content = get_page_content(page["id"])
                if content:
                    notion_context += f"\n## {page['title']}\n{content[:500]}\n"
    system = SYSTEM_PROMPT
    if notion_context:
        system += notion_context
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
    app.add_handler(CommandHandler("buscar", notion_search_command))
    app.add_handler(CommandHandler("ler", notion_read_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
