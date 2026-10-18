
import os
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

RESPOSTAS = {
    "planos": (
        "📺 PLANOS POWERPLAY IPTV\n\n"
        "Consulte nossos planos e valores com o atendimento.\n"
        "Em breve, os planos aparecerão aqui."
    ),
    "teste": (
        "🎁 SOLICITAR TESTE\n\n"
        "Para solicitar um teste, entre em contato "
        "com nosso atendimento."
    ),
    "comprar": (
        "💳 CONTRATAR PLANO\n\n"
        "Para contratar, solicite os dados de "
        "pagamento ao atendimento."
    ),
    "renovar": (
        "🔄 RENOVAR ACESSO\n\n"
        "Informe ao atendimento que deseja "
        "renovar sua assinatura."
    ),
    "suporte": (
        "🛠️ SUPORTE\n\n"
        "Descreva o problema e envie uma foto "
        "ou print do erro ao atendimento."
    ),
    "ajuda": (
        "❓ AJUDA\n\n"
        "Escolha uma das opções do menu "
        "para consultar nossos serviços."
    ),
}

def menu():
    botoes = [
        [
            InlineKeyboardButton("📺 Planos", callback_data="planos"),
            InlineKeyboardButton("🎁 Teste", callback_data="teste"),
        ],
        [
            InlineKeyboardButton("💳 Comprar", callback_data="comprar"),
            InlineKeyboardButton("🔄 Renovar", callback_data="renovar"),
        ],
        [
            InlineKeyboardButton("🛠️ Suporte", callback_data="suporte"),
            InlineKeyboardButton("❓ Ajuda", callback_data="ajuda"),
        ],
    ]
    return InlineKeyboardMarkup(botoes)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bem-vindo ao Powerplay IPTV Bot!\n\n"
        "Escolha uma opção abaixo:",
        reply_markup=menu(),
    )

async def comando(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nome = update.message.text.split()[0].split("@")[0][1:].lower()
    await update.message.reply_text(
        RESPOSTAS.get(nome, "Comando não encontrado."),
        reply_markup=menu(),
    )

async def botao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        RESPOSTAS.get(query.data, "Opção não encontrada."),
        reply_markup=menu(),
    )

def main():
    token = os.environ.get("BOT_TOKEN")

    if not token:
        raise RuntimeError("Configure a variável BOT_TOKEN.")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))

    for nome in RESPOSTAS:
        app.add_handler(CommandHandler(nome, comando))

    app.add_handler(CallbackQueryHandler(botao))

    print("Powerplay IPTV Bot iniciado!")
    app.run_polling()

if __name__ == "__main__":
    main()
