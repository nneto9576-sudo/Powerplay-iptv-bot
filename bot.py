
import os
import io
import base64
import asyncio
import logging
import secrets
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from decimal import Decimal

import httpx
from telegram import (
    Update,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Menu
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler
)

# ==============================
# CONFIGURACOES
# ==============================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPPORT_USERNAME = os.getenv(
    "SUPPORT_USERNAME", ""
).strip().lstrip("@")

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
PROMST_USER_ID = os.getenv("PROMST_USER_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "").strip()

GATEWAY = "https://promstgateway.com"

PLANOS = {
    "1m": ("1 mês", 2800),
    "3m": ("3 meses", 5000),
    "12m": ("1 ano", 15000)
}

RECARGAS = [1000, 2000, 2800, 5000, 10000, 15000]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("powerplay")


# ==============================
# VALORES E BANCO DE DADOS
# ==============================

def moeda(valor):
    reais, cents = divmod(valor, 100)
    return (
        f"R$ {reais:,}".replace(",", ".")
        + f",{cents:02d}"
    )


def converter(valor):
    try:
        return int(
            (Decimal(str(valor)) * 100).quantize(Decimal("1"))
        )
    except Exception:
        return -1


@contextmanager
def banco():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        isolation_level=None
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
    finally:
        conn.close()


def iniciar_banco():
    if not DB_PATH or not Path(DB_PATH).is_absolute():
        raise RuntimeError(
            "Configure DB_PATH=/data/powerplay.sqlite3"
        )

    Path(DB_PATH).parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with banco() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY,
                saldo INTEGER NOT NULL DEFAULT 0
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS intencoes (
                token TEXT PRIMARY KEY,
                usuario INTEGER NOT NULL,
                plano TEXT NOT NULL,
                situacao TEXT NOT NULL DEFAULT 'ABERTA'
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS pagamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                txid TEXT NOT NULL UNIQUE,
                usuario INTEGER NOT NULL,
                finalidade TEXT NOT NULL,
                valor INTEGER NOT NULL,
                plano TEXT,
                token TEXT,
                retomar TEXT,
                copia_cola TEXT NOT NULL,
                situacao TEXT NOT NULL DEFAULT 'PENDENTE'
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS pedidos (
                numero TEXT PRIMARY KEY,
                usuario INTEGER NOT NULL,
                plano TEXT NOT NULL,
                valor INTEGER NOT NULL,
                origem TEXT NOT NULL,
                token TEXT UNIQUE,
                pagamento_id INTEGER UNIQUE
            )
        """)


def cadastrar(uid):
    with banco() as db:
        db.execute(
            "INSERT OR IGNORE INTO usuarios(id) VALUES(?)",
            (uid,)
        )


def consultar_saldo(uid):
    cadastrar(uid)
    with banco() as db:
        return db.execute(
            "SELECT saldo FROM usuarios WHERE id=?",
            (uid,)
        ).fetchone()["saldo"]


def criar_intencao(uid, plano):
    token = secrets.token_hex(6)
    with banco() as db:
        db.execute(
            "INSERT INTO intencoes(token,usuario,plano) VALUES(?,?,?)",
            (token, uid, plano)
        )
    return token


def buscar_intencao(uid, token):
    with banco() as db:
        return db.execute(
            "SELECT * FROM intencoes WHERE token=? AND usuario=?",
            (token, uid)
        ).fetchone()


def comprar_com_saldo(uid, token):
    with banco() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            pedido = db.execute(
                "SELECT * FROM intencoes WHERE token=? AND usuario=?",
                (token, uid)
            ).fetchone()

            if not pedido or pedido["situacao"] != "ABERTA":
                db.execute("ROLLBACK")
                return "USADO", None

            valor = PLANOS[pedido["plano"]][1]
            atual = db.execute(
                "SELECT saldo FROM usuarios WHERE id=?",
                (uid,)
            ).fetchone()

            if not atual or atual["saldo"] < valor:
                db.execute("ROLLBACK")
                return "INSUFICIENTE", None

            numero = secrets.token_hex(4).upper()

            db.execute(
                "UPDATE usuarios SET saldo=saldo-? WHERE id=?",
                (valor, uid)
            )

            db.execute(
                "UPDATE intencoes SET situacao='PAGA' WHERE token=?",
                (token,)
            )

            db.execute("""
                INSERT INTO pedidos
                (numero,usuario,plano,valor,origem,token)
                VALUES(?,?,?,?,?,?)
            """, (
                numero, uid, pedido["plano"],
                valor, "SALDO", token
            ))

            db.execute("COMMIT")
            return "OK", numero

        except Exception:
            db.execute("ROLLBACK")
            raise


# ==============================
# GATEWAY PRO MST
# ==============================

async def gerar_pix(valor):
    if not PROMST_USER_ID.isdigit():
        raise RuntimeError("PROMST_USER_ID nao configurado")

    async with httpx.AsyncClient(timeout=20) as client:
        resposta = await client.get(
            GATEWAY + "/create_payment",
            params={
                "user_id": PROMST_USER_ID,
                "valor": f"{valor / 100:.2f}"
            }
        )
        resposta.raise_for_status()
        dados = resposta.json()

    if (
        not dados.get("txid")
        or not dados.get("pixCopiaECola")
        or converter(dados.get("amount")) != valor
    ):
        raise RuntimeError("Resposta invalida da gateway")

    return dados


async def verificar_gateway(txid):
    async with httpx.AsyncClient(timeout=20) as client:
        resposta = await client.get(
            GATEWAY + "/verify_payment",
            params={"payment_id": txid}
        )
        resposta.raise_for_status()
        return resposta.json()


def salvar_pagamento(
    uid, dados, finalidade, valor,
    plano=None, token=None, retomar=None
):
    with banco() as db:
        db.execute("""
            INSERT INTO pagamentos
            (txid,usuario,finalidade,valor,plano,
             token,retomar,copia_cola)
            VALUES(?,?,?,?,?,?,?,?)
        """, (
            str(dados["txid"]),
            uid, finalidade, valor, plano,
            token, retomar, dados["pixCopiaECola"]
        ))

        return db.execute(
            "SELECT * FROM pagamentos WHERE txid=?",
            (str(dados["txid"]),)
        ).fetchone()


def obter_pagamento(pid, uid=None):
    with banco() as db:
        if uid is None:
            return db.execute(
                "SELECT * FROM pagamentos WHERE id=?",
                (pid,)
            ).fetchone()

        return db.execute(
            "SELECT * FROM pagamentos WHERE id=? AND usuario=?",
            (pid, uid)
        ).fetchone()


def pix_do_pedido(uid, token):
    with banco() as db:
        return db.execute("""
            SELECT * FROM pagamentos
            WHERE usuario=? AND token=?
            AND finalidade='PLANO'
            AND situacao='PENDENTE'
            ORDER BY id DESC LIMIT 1
        """, (uid, token)).fetchone()


def pagamentos_pendentes():
    with banco() as db:
        return db.execute("""
            SELECT * FROM pagamentos
            WHERE situacao='PENDENTE'
            ORDER BY id DESC LIMIT 80
        """).fetchall()


def confirmar_pagamento(pagamento):
    with banco() as db:
        db.execute("BEGIN IMMEDIATE")

        try:
            p = db.execute(
                "SELECT * FROM pagamentos WHERE id=?",
                (pagamento["id"],)
            ).fetchone()

            if not p or p["situacao"] != "PENDENTE":
                db.execute("ROLLBACK")
                return None

            numero = None

            if p["finalidade"] == "SALDO":
                db.execute("""
                    UPDATE usuarios
                    SET saldo=saldo+?
                    WHERE id=?
                """, (p["valor"], p["usuario"]))

            else:
                intencao = db.execute("""
                    SELECT situacao FROM intencoes
                    WHERE token=? AND usuario=?
                """, (p["token"], p["usuario"])).fetchone()

                if not intencao or intencao["situacao"] != "ABERTA":
                    db.execute("""
                        UPDATE pagamentos SET situacao='REVISAR'
                        WHERE id=?
                    """, (p["id"],))
                    db.execute("COMMIT")
                    return "REVISAR", None, p

                numero = secrets.token_hex(4).upper()

                db.execute("""
                    UPDATE intencoes SET situacao='PAGA'
                    WHERE token=?
                """, (p["token"],))

                db.execute("""
                    INSERT INTO pedidos
                    (numero,usuario,plano,valor,origem,token,pagamento_id)
                    VALUES(?,?,?,?,?,?,?)
                """, (
                    numero, p["usuario"], p["plano"],
                    p["valor"], "PIX", p["token"], p["id"]
                ))

            db.execute("""
                UPDATE pagamentos SET situacao='CONCLUIDA'
                WHERE id=?
            """, (p["id"],))

            db.execute("COMMIT")
            return "CONCLUIDA", numero, p

        except Exception:
            db.execute("ROLLBACK")
            raise


# ==============================
# AVISOS E VERIFICACAO
# ==============================

async def avisar_admin(bot, texto):
    if ADMIN_CHAT_ID.isdigit():
        try:
            await bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=texto
            )
        except Exception:
            log.exception("Falha no aviso ao administrador")


async def conferir_pagamento(bot, pagamento):
    dados = await verificar_gateway(pagamento["txid"])

    if str(dados.get("status_pagamento", "")).upper() != "CONCLUIDA":
        return False

    if (
        str(dados.get("payment_id", "")) != pagamento["txid"]
        or converter(dados.get("valor")) != pagamento["valor"]
    ):
        log.error("Identificador ou valor divergente")
        return False

    resultado = confirmar_pagamento(pagamento)

    if resultado is None:
        return True

    estado, numero, p = resultado
    uid = p["usuario"]

    if estado == "REVISAR":
        await bot.send_message(
            uid,
            "⚠️ Pagamento recebido, mas este pedido ja foi "
            "utilizado. O suporte ira revisar."
        )
        await avisar_admin(
            bot,
            f"⚠️ REVISAR PIX {p['txid']} | Cliente {uid}"
        )
        return True

    if p["finalidade"] == "SALDO":
        texto = (
            "✅ PIX CONFIRMADO!\n\n"
            f"💰 Recarga: {moeda(p['valor'])}\n"
            f"👛 Saldo atual: {moeda(consultar_saldo(uid))}"
        )

        if p["retomar"]:
            kb = Menu([[
                Btn(
                    "📺 Continuar contratação",
                    callback_data=f"checkout_{p['retomar']}"
                )
            ]])
        else:
            kb = menu_principal()

        await bot.send_message(uid, texto, reply_markup=kb)

        await avisar_admin(
            bot,
            f"✅ RECARGA CONFIRMADA\n"
            f"Cliente: {uid}\n"
            f"Valor: {moeda(p['valor'])}\n"
            f"PIX: {p['txid']}"
        )

    else:
        nome = PLANOS[p["plano"]][0]

        await bot.send_message(
            uid,
            "✅ PAGAMENTO CONFIRMADO!\n\n"
            f"📦 Pedido: {numero}\n"
            f"📺 Plano: {nome}\n"
            f"💰 Valor: {moeda(p['valor'])}\n\n"
            "O suporte receberá seu pedido para liberar o acesso.",
            reply_markup=menu_principal()
        )

        await avisar_admin(
            bot,
            "🛒 NOVO PEDIDO PAGO — POWER PLAY IPTV\n\n"
            f"📦 Pedido: {numero}\n"
            f"📺 Plano: {nome}\n"
            f"💰 Valor: {moeda(p['valor'])}\n"
            f"👤 Cliente ID: {uid}\n"
            f"💳 PIX: {p['txid']}\n\n"
            "✅ PAGAMENTO CONFIRMADO\n"
            "Enviar acesso ao cliente."
        )

    return True


async def monitorar(app):
    while True:
        await asyncio.sleep(30)

        for pagamento in pagamentos_pendentes():
            try:
                await conferir_pagamento(app.bot, pagamento)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erro verificando PIX")


async def iniciar_monitor(app):
    app.bot_data["monitor"] = asyncio.create_task(
        monitorar(app)
    )


async def parar_monitor(app):
    tarefa = app.bot_data.get("monitor")
    if tarefa:
        tarefa.cancel()
        try:
            await tarefa
        except asyncio.CancelledError:
            pass


# ==============================
# MENUS
# ==============================

def botao_suporte():
    if SUPPORT_USERNAME:
        return Btn(
            "💬 Falar com suporte",
            url=f"https://t.me/{SUPPORT_USERNAME}"
        )
    return Btn("💬 Suporte", callback_data="suporte")


def menu_principal():
    return Menu([
        [
            Btn("📺 Planos", callback_data="planos"),
            Btn("💰 Meu saldo", callback_data="saldo")
        ],
        [
            Btn("💳 Comprar", callback_data="planos"),
            Btn("➕ Adicionar saldo", callback_data="recarregar")
        ],
        [
            Btn("🎁 Teste", callback_data="teste"),
            Btn("🔄 Renovar", callback_data="renovar")
        ],
        [
            botao_suporte(),
            Btn("❓ Ajuda", callback_data="ajuda")
        ]
    ])


def menu_planos():
    botoes = [
        [
            Btn(
                f"📺 {nome} — {moeda(valor)}",
                callback_data=f"plano_{codigo}"
            )
        ]
        for codigo, (nome, valor) in PLANOS.items()
    ]
    botoes.append([Btn("🏠 Menu", callback_data="inicio")])
    return Menu(botoes)


def menu_recarga(retomar="x"):
    botoes = [
        [
            Btn(
                f"➕ {moeda(valor)}",
                callback_data=f"add_{valor}_{retomar}"
            )
        ]
        for valor in RECARGAS
    ]
    botoes.append([Btn("🏠 Menu", callback_data="inicio")])
    return Menu(botoes)


def menu_checkout(token):
    return Menu([
        [
            Btn(
                "💰 Contratar com saldo",
                callback_data=f"buy_{token}"
            )
        ],
        [
            Btn(
                "💠 Gerar PIX do plano",
                callback_data=f"pix_{token}"
            )
        ],
        [
            Btn(
                "➕ Adicionar saldo",
                callback_data=f"recharge_{token}"
            )
        ],
        [Btn("⬅️ Planos", callback_data="planos")]
    ])


async def mostrar_pix(mensagem, pagamento, qrcode=None):
    await mensagem.reply_text(
        "💠 PIX GERADO!\n\n"
        f"💰 Valor: {moeda(pagamento['valor'])}\n"
        f"🧾 Cobrança: #{pagamento['id']}\n\n"
        "📋 PIX COPIA E COLA:\n\n"
        + pagamento["copia_cola"],
        reply_markup=Menu([
            [
                Btn(
                    "🔎 Verificar pagamento",
                    callback_data=f"verify_{pagamento['id']}"
                )
            ],
            [Btn("🏠 Menu", callback_data="inicio")]
        ])
    )

    if qrcode:
        try:
            imagem = base64.b64decode(
                qrcode.split(",")[-1],
                validate=True
            )
            await mensagem.reply_photo(
                photo=io.BytesIO(imagem),
                caption="📱 QR Code PIX"
            )
        except Exception:
            log.warning("QR Code indisponivel")


# ==============================
# COMANDOS TELEGRAM
# ==============================

async def start(update: Update, context):
    cadastrar(update.effective_user.id)

    await update.message.reply_text(
        "💜 BEM-VINDO AO POWER PLAY IPTV!\n\n"
        "📺 Canais, filmes e séries.\n"
        "🍿 Escolha um plano ou consulte seu saldo.\n\n"
        "👇 Selecione uma opção:",
        reply_markup=menu_principal()
    )


async def mostrar_id(update: Update, context):
    await update.message.reply_text(
        f"Seu ID Telegram: {update.effective_user.id}"
    )


async def comandos(update: Update, context):
    nome = update.message.text.split()[0][1:].split("@")[0]
    uid = update.effective_user.id

    if nome in ("planos", "comprar"):
        await update.message.reply_text(
            "📺 PLANOS POWER PLAY IPTV",
            reply_markup=menu_planos()
        )

    elif nome == "saldo":
        await update.message.reply_text(
            f"💰 Seu saldo: {moeda(consultar_saldo(uid))}",
            reply_markup=menu_principal()
        )

    elif nome == "recarregar":
        await update.message.reply_text(
            "➕ Selecione o valor da recarga:",
            reply_markup=menu_recarga()
        )

    else:
        await update.message.reply_text(
            "💬 Entre em contato com o suporte.",
            reply_markup=Menu([[botao_suporte()]])
        )


# ==============================
# CLIQUES NOS BOTOES
# ==============================

async def botoes(update: Update, context):
    q = update.callback_query
    await q.answer()

    uid = q.from_user.id
    escolha = q.data
    cadastrar(uid)

    if escolha == "inicio":
        await q.message.reply_text(
            "🏠 POWER PLAY IPTV",
            reply_markup=menu_principal()
        )
        return

    if escolha == "planos":
        await q.message.reply_text(
            "📺 Escolha seu plano:",
            reply_markup=menu_planos()
        )
        return

    if escolha == "saldo":
        await q.message.reply_text(
            f"💰 Seu saldo: {moeda(consultar_saldo(uid))}",
            reply_markup=Menu([
                [Btn("➕ Adicionar saldo", callback_data="recarregar")],
                [Btn("🏠 Menu", callback_data="inicio")]
            ])
        )
        return

    if escolha == "recarregar":
        await q.message.reply_text(
            "➕ Quanto deseja adicionar ao saldo?",
            reply_markup=menu_recarga()
        )
        return

    if escolha.startswith("plano_"):
        codigo = escolha[6:]

        if codigo not in PLANOS:
            return

        nome, preco = PLANOS[codigo]

        await q.message.reply_text(
            f"📺 PLANO: {nome}\n"
            f"💰 Valor: {moeda(preco)}\n"
            f"👛 Seu saldo: {moeda(consultar_saldo(uid))}\n\n"
            "Clique abaixo para contratar:",
            reply_markup=Menu([
                [
                    Btn(
                        "🛒 CONTRATAR AGORA",
                        callback_data=f"contratar_{codigo}"
                    )
                ],
                [Btn("⬅️ Voltar", callback_data="planos")]
            ])
        )
        return

    if escolha.startswith("contratar_"):
        codigo = escolha[9:]

        if codigo not in PLANOS:
            return

        token = criar_intencao(uid, codigo)
        nome, preco = PLANOS[codigo]

        await q.message.reply_text(
            f"📺 {nome} — {moeda(preco)}\n"
            f"💰 Saldo: {moeda(consultar_saldo(uid))}\n\n"
            "Escolha como deseja pagar:",
            reply_markup=menu_checkout(token)
        )
        return

    if escolha.startswith("checkout_"):
        token = escolha[9:]
        pedido = buscar_intencao(uid, token)

        if not pedido:
            return

        if pedido["situacao"] != "ABERTA":
            await q.message.reply_text(
                "✅ Este pedido ja foi concluido.",
                reply_markup=menu_principal()
            )
            return

        nome, preco = PLANOS[pedido["plano"]]

        await q.message.reply_text(
            f"📺 {nome}: {moeda(preco)}\n"
            f"💰 Seu saldo: {moeda(consultar_saldo(uid))}",
            reply_markup=menu_checkout(token)
        )
        return

    if escolha.startswith("buy_"):
        token = escolha[4:]
        pedido = buscar_intencao(uid, token)

        if not pedido:
            return

        status, numero = comprar_com_saldo(uid, token)

        if status == "INSUFICIENTE":
            await q.message.reply_text(
                "❌ Saldo insuficiente!\n\n"
                f"Saldo: {moeda(consultar_saldo(uid))}",
                reply_markup=menu_checkout(token)
            )
            return

        if status == "USADO":
            await q.message.reply_text(
                "⚠️ Este pedido ja foi utilizado.",
                reply_markup=menu_principal()
            )
            return

        nome, valor = PLANOS[pedido["plano"]]

        await q.message.reply_text(
            "✅ CONTRATAÇÃO CONFIRMADA!\n\n"
            f"📦 Pedido: {numero}\n"
            f"📺 Plano: {nome}\n"
            f"💰 Valor: {moeda(valor)}\n"
            f"👛 Saldo restante: {moeda(consultar_saldo(uid))}\n\n"
            "O suporte vai liberar seu acesso.",
            reply_markup=menu_principal()
        )

        await avisar_admin(
            context.bot,
            "🛒 NOVO PEDIDO PAGO POR SALDO\n\n"
            f"📦 Pedido: {numero}\n"
            f"📺 Plano: {nome}\n"
            f"💰 Valor: {moeda(valor)}\n"
            f"👤 Cliente: {q.from_user.full_name}\n"
            f"🆔 ID: {uid}\n\n"
            "✅ PAGAMENTO CONFIRMADO\n"
            "Enviar acesso."
        )
        return

    if escolha.startswith("recharge_"):
        token = escolha[9:]

        if not buscar_intencao(uid, token):
            return

        await q.message.reply_text(
            "➕ Escolha o valor que deseja adicionar:",
            reply_markup=menu_recarga(token)
        )
        return

    if escolha.startswith("add_"):
        try:
            _, valor_txt, retomar = escolha.split("_", 2)
            valor = int(valor_txt)
        except ValueError:
            return

        if valor not in RECARGAS:
            return

        if retomar != "x" and not buscar_intencao(uid, retomar):
            return

        await q.message.reply_text(
            "⏳ Gerando PIX de recarga..."
        )

        try:
            dados = await gerar_pix(valor)
            pagamento = salvar_pagamento(
                uid, dados, "SALDO", valor,
                retomar=None if retomar == "x" else retomar
            )

            await mostrar_pix(
                q.message,
                pagamento,
                dados.get("qrcode_base64")
            )

        except Exception:
            log.exception("Erro na recarga PIX")
            await q.message.reply_text(
                "❌ Erro ao gerar PIX. Tente novamente."
            )
        return

    if escolha.startswith("pix_"):
        token = escolha[4:]
        pedido = buscar_intencao(uid, token)

        if not pedido or pedido["situacao"] != "ABERTA":
            await q.message.reply_text(
                "⚠️ Pedido ja utilizado. Escolha o plano novamente."
            )
            return

        anterior = pix_do_pedido(uid, token)

        if anterior:
            await mostrar_pix(q.message, anterior)
            return

        plano = pedido["plano"]
        valor = PLANOS[plano][1]

        await q.message.reply_text(
            "⏳ Gerando PIX para pagamento do plano..."
        )

        try:
            dados = await gerar_pix(valor)

            pagamento = salvar_pagamento(
                uid, dados, "PLANO", valor,
                plano=plano, token=token
            )

            await mostrar_pix(
                q.message,
                pagamento,
                dados.get("qrcode_base64")
            )

        except Exception:
            log.exception("Erro no PIX do plano")
            await q.message.reply_text(
                "❌ Nao foi possivel gerar o PIX."
            )
        return

    if escolha.startswith("verify_"):
        try:
            pid = int(escolha[7:])
        except ValueError:
            return

        pagamento = obter_pagamento(pid, uid)

        if not pagamento:
            return

        if pagamento["situacao"] == "CONCLUIDA":
            await q.message.reply_text(
                "✅ Pagamento ja confirmado!"
            )
            return

        if pagamento["situacao"] == "REVISAR":
            await q.message.reply_text(
                "⚠️ O suporte ira analisar seu pagamento."
            )
            return

        try:
            confirmado = await conferir_pagamento(
                context.bot, pagamento
            )

            if not confirmado:
                await q.message.reply_text(
                    "⏳ Pagamento ainda pendente.\n"
                    "O bot continuará verificando automaticamente."
                )

        except Exception:
            log.exception("Erro ao verificar pagamento")
            await q.message.reply_text(
                "⚠️ Erro temporario ao verificar o pagamento."
            )
        return

    mensagens = {
        "teste": (
            "🎁 TESTE\n\n"
            "Fale com o suporte para consultar disponibilidade."
        ),
        "renovar": (
            "🔄 RENOVAÇÃO\n\n"
            "Entre em contato para renovar seu acesso."
        ),
        "suporte": (
            "💬 SUPORTE\n\n"
            "Envie sua duvida ou print do erro."
        ),
        "ajuda": (
            "❓ AJUDA\n\n"
            "Consulte os planos, adicione saldo ou "
            "fale com nosso atendimento."
        )
    }

    await q.message.reply_text(
        mensagens.get(escolha, "Opção indisponível."),
        reply_markup=Menu([
            [botao_suporte()],
            [Btn("🏠 Menu", callback_data="inicio")]
        ])
    )


# ==============================
# INICIAR BOT
# ==============================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Configure BOT_TOKEN no Railway")

    if not PROMST_USER_ID.isdigit():
        raise RuntimeError(
            "Configure PROMST_USER_ID com o ID "
            "do titular da gateway Pro MST"
        )

    iniciar_banco()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(iniciar_monitor)
        .post_shutdown(parar_monitor)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", mostrar_id))

    for comando in (
        "planos", "comprar", "saldo",
        "recarregar", "teste", "renovar",
        "suporte", "ajuda"
    ):
        app.add_handler(CommandHandler(comando, comandos))

    app.add_handler(CallbackQueryHandler(botoes))

    print("Power Play IPTV iniciado!")
    app.run_polling()


if __name__ == "__main__":
    main()
