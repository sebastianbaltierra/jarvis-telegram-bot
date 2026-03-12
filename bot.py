import os
import logging
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
conversation_histories = {}
SYSTEM_PROMPT = "Voce e o Jarvis, assistente pessoal de IA do Sebastian. Responda em portugues do Brasil."

async def start(update, context):
    user = update.effective_user
    conversation_histories[user.id] = []
    await update.message.reply_text(f"Ola {user.first_name}! Sou o Jarvis. Como posso ajudar?")

async def reset(update, context):
    conversation_histories[update.effective_user.id] = []
    await update.message.reply_text("Conversa reiniciada!")

async def help_command(update, context):
    await update.message.reply_text("/start - Iniciar\n/reset - Reiniciar conversa\n/help - Ajuda")

async def handle_message(update, context):
    user = update.effective_user
    user_message = update.message.text
    if user.id not in conversation_histories:
        conversation_histories[user.id] = []
    conversation_histories[user.id].append({"role": "user", "content": user_message})
    if len(conversation_histories[user.id]) > 20:
        conversation_histories[user.id] = conversation_histories[user.id][-20:]
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        response = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=conversation_histories[user.id]
        )
        reply = response.content[0].text
        conversation_histories[user.id].append({"role": "assistant", "content": reply})
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Erro ao processar. Tente novamente.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
