
import os
import secrets
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPORTE_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

PLANOS = {
    "1m": ("1 mês", "R$ 28,00"),
    "3m": ("3 meses", "R$ 50,00"),
    "12m": ("1 ano", "R$ 150,00"),
}


def link_suporte(mensagem=""):
    if not SUPORTE_USERNAME:
        return None
    link = f"https://t.me/{SUPORTE_USERNAME}"
    if mensagem:
        link += "?text=" + quote(mensagem, safe="")
    return link


def botao_suporte(mensagem="Olá! Preciso de atendimento."):
    link = link_suporte(mensagem)
    if link:
        return InlineKeyboardButton("💬 Falar no privado", url=link)
    return InlineKeyboardButton(
        "💬 Falar com suporte", callback_data="suporte"
    )


def menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📺 Planos", callback_data="planos"),
            InlineKeyboardButton("🎁 Teste", callback_data="teste"),
        ],
        [
            InlineKeyboardButton("💳 Comprar", callback_data="planos"),
            InlineKeyboardButton("🔄 Renovar", callback_data="renovar"),
        ],
        [
            botao_suporte(),
            InlineKeyboardButton("❓ Ajuda", callback_data="ajuda"),
        ],
    ])


def menu_planos():
    botoes = [
        [
            InlineKeyboardButton(
                f"📺 {nome} — {preco}",
                callback_data=f"plano_{codigo}"
            )
        ]
        for codigo, (nome, preco) in PLANOS.items()
    ]

    botoes.append([
        InlineKeyboardButton("🏠 Voltar ao menu", callback_data="inicio")
    ])
    return InlineKeyboardMarkup(botoes)


def menu_voltar(mensagem="Olá! Preciso de atendimento."):
    return InlineKeyboardMarkup([
        [botao_suporte(mensagem)],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="inicio")],
    ])


BOAS_VINDAS = (
    "💜 BEM-VINDO AO POWER PLAY IPTV!\n\n"
    "📺 Confira canais, filmes, séries e nossos planos.\n"
    "🍿 Escolha uma opção abaixo para começar!"
)

PLANOS_TEXTO = (
    "📺 PLANOS POWER PLAY IPTV\n\n"
    "✅ 1 mês — R$ 28,00\n"
    "✅ 3 meses — R$ 50,00\n"
    "👑 1 ano — R$ 150,00\n\n"
    "👇 Selecione o plano que deseja contratar:"
)


def mensagem_opcao(opcao):
    mensagens = {
        "teste": (
            "🎁 TESTE\n\n"
            "Entre em contato para consultar disponibilidade de teste."
        ),
        "renovar": (
            "🔄 RENOVAÇÃO\n\n"
            "Fale com o atendimento para renovar seu acesso."
        ),
        "suporte": (
            "🛠️ SUPORTE\n\n"
            "Toque abaixo para abrir uma conversa privada com o "
            "atendimento. Envie uma foto ou print do erro."
        ),
        "ajuda": (
            "❓ AJUDA\n\n"
            "Escolha Planos para ver os preços, Comprar para "
            "contratar ou Suporte para atendimento."
        ),
    }
    return mensagens.get(opcao, "Opção não encontrada.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        BOAS_VINDAS, reply_markup=menu()
    )


async def id_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Seu ID deste chat é: {update.effective_chat.id}\n"
        "Use esse número em ADMIN_CHAT_ID no Railway, "
        "se este for seu Telegram pessoal."
    )


async def comando(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nome = update.message.text.split()[0].split("@")[0][1:].lower()

    if nome in ("planos", "comprar"):
        await update.message.reply_text(
            PLANOS_TEXTO, reply_markup=menu_planos()
        )
    else:
        await update.message.reply_text(
            mensagem_opcao(nome),
            reply_markup=menu_voltar()
        )


async def botao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    escolha = query.data

    if escolha == "inicio":
        await query.edit_message_text(
            BOAS_VINDAS, reply_markup=menu()
        )
        return

    if escolha == "planos":
        await query.edit_message_text(
            PLANOS_TEXTO, reply_markup=menu_planos()
        )
        return

    if escolha.startswith("plano_"):
        codigo = escolha.removeprefix("plano_")

        if codigo not in PLANOS:
            await query.edit_message_text(
                "Plano inválido.", reply_markup=menu_planos()
            )
            return

        nome, preco = PLANOS[codigo]

        await query.edit_message_text(
            f"📺 PLANO: {nome}\n"
            f"💰 VALOR: {preco}\n\n"
            "Clique para solicitar sua contratação.\n"
            "⚠️ O pagamento será combinado e confirmado "
            "pelo atendimento.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💳 CONTRATAR AGORA",
                        callback_data=f"contratar_{codigo}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Voltar aos planos",
                        callback_data="planos"
                    )
                ]
            ])
        )
        return

    if escolha.startswith("contratar_"):
        codigo = escolha.removeprefix("contratar_")

        if codigo not in PLANOS:
            await query.edit_message_text(
                "Plano inválido.", reply_markup=menu_planos()
            )
            return

        nome, preco = PLANOS[codigo]
        numero = secrets.token_hex(4).upper()
        email_referencia = f"cliente-{numero.lower()}@example.com"

        cliente = query.from_user
        usuario = (
            "@" + cliente.username
            if cliente.username
            else "Sem username"
        )

        texto_cliente = (
            f"Olá! Quero contratar o plano {nome} ({preco}). "
            f"Meu pedido é {numero}. "
            "Envie as orientações de pagamento e acesso."
        )

        aviso = (
            "🛒 NOVO PEDIDO — POWER PLAY IPTV\n\n"
            f"📦 Pedido: {numero}\n"
            f"📺 Plano: {nome}\n"
            f"💰 Valor: {preco}\n"
            f"👤 Cliente: {cliente.full_name}\n"
            f"🔗 Telegram: {usuario}\n"
            f"🆔 ID Telegram: {cliente.id}\n"
            f"📧 Referência fictícia: {email_referencia}\n\n"
            "⚠️ Pedido solicitado, pagamento ainda NÃO confirmado."
        )

        enviado = False

        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=int(ADMIN_CHAT_ID),
                    text=aviso
                )
                enviado = True
            except (ValueError, TelegramError) as erro:
                print(f"Falha ao avisar administrador: {erro}")

        confirmacao = (
            "✅ SOLICITAÇÃO GERADA!\n\n"
            f"📦 Pedido: {numero}\n"
            f"📺 Plano: {nome}\n"
            f"💰 Total: {preco}\n\n"
        )

        if enviado:
            confirmacao += "📩 O atendimento recebeu seu pedido.\n"
        else:
            confirmacao += (
                "📩 Envie seu pedido ao atendimento "
                "pelo botão abaixo.\n"
            )

        confirmacao += (
            "\n💬 Fale no privado para combinar "
            "o pagamento e receber seu acesso."
        )

        await query.edit_message_text(
            confirmacao,
            reply_markup=InlineKeyboardMarkup([
                [botao_suporte(texto_cliente)],
                [
                    InlineKeyboardButton(
                        "🏠 Menu principal",
                        callback_data="inicio"
                    )
                ],
            ])
        )
        return

    await query.edit_message_text(
        mensagem_opcao(escolha),
        reply_markup=menu_voltar()
    )


def main():
    if not TOKEN:
        raise RuntimeError(
            "Configure BOT_TOKEN nas Variables do Railway."
        )

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", id_chat))

    for nome in (
        "planos", "comprar", "teste",
        "renovar", "suporte", "ajuda"
    ):
        app.add_handler(CommandHandler(nome, comando))

    app.add_handler(CallbackQueryHandler(botao))

    print("Power Play IPTV Bot iniciado!")
    app.run_polling()


if __name__ == "__main__":
    main()
