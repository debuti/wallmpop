"""Telegram bot: manages alerts and delivers matching listings. Only ALLOWED_USER_IDS may use it."""
from __future__ import annotations

import asyncio
import difflib
import html
import json
import logging
import secrets
import time
from typing import Any

import yaml
from telegram import BotCommand, InlineKeyboardButton as B, InlineKeyboardMarkup as KB, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          ContextTypes, TypeHandler)

from .config import Settings
from .db import DB
from .judge import deep_look, derive_alert, edit_alert, tune_alert
from .llm import Claude, LLMError
from .models import FREQUENCIES, AlertConfig, Listing
from .prefilter import distance_km
from .scheduler import Scheduler

log = logging.getLogger(__name__)

COMMANDS = [
    ("alta", "Nueva alerta en lenguaje natural"),
    ("lista", "Ver alertas"),
    ("editar", "Cambiar una alerta: /editar <n> <texto>"),
    ("frecuencia", "Cada cuánto mira: /frecuencia <n> <min>"),
    ("pausar", "Pausar alerta"),
    ("reanudar", "Reanudar alerta"),
    ("baja", "Borrar alerta"),
    ("afinar", "Ajustar con tus 👍/👎"),
    ("estado", "Salud de la API y consumo de hoy"),
    ("ayuda", "Ayuda"),
    ("usuarios", "Solo admin: quién puede usar el bot"),
]

HELP = """🛒 <b>wallmpop</b> — cazador de chollos en Wallapop

/alta <i>texto</i> — p.ej. <code>/alta NVMe 512GB por menos de 50€, gen3 o mejor, buena salud, Madrid</code>
/lista — tus alertas
/editar <i>n texto</i> — cambiarla en lenguaje natural
/frecuencia <i>n min</i> — cada cuánto mira
/pausar · /reanudar · /baja <i>n</i>
/afinar <i>n</i> — ajusta la alerta con tus 👍/👎
/estado — salud de la API y consumo de hoy
/usuarios — (admin) quién puede usar el bot

🎯 = encaja (suena) · 🤔 = quizá (silencioso)
👍/👎 = guardar tu opinión para /afinar · ✓ Visto = quitar botones sin guardar nada"""


def pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def cfg_yaml(cfg: AlertConfig) -> str:
    return yaml.safe_dump(cfg.to_dict(), allow_unicode=True, sort_keys=False, width=60).strip()


def ago(ts: int) -> str:
    if not ts:
        return "nunca"
    m = int((time.time() - ts) // 60)
    return f"hace {m} min" if m < 120 else f"hace {m // 60} h"


class Bot:
    def __init__(self, settings: Settings, db: DB, claude: Claude):
        self.s, self.db, self.claude = settings, db, claude
        self.app: Application | None = None
        self.sched: Scheduler | None = None
        self.drafts: dict[str, dict[str, Any]] = {}

    # ---------- access control ----------
    @property
    def admins(self) -> set[int]:
        return self.s.allowed_user_ids

    def is_admin(self, uid: int) -> bool:
        return uid in self.admins

    def allowed(self, uid: int) -> bool:
        if self.is_admin(uid):
            return True
        row = self.db.user(uid)
        return bool(row and row["status"] == "allowed")

    async def gate(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user and self.allowed(user.id):
            return
        q = update.callback_query
        if q:
            await q.answer()
        msg = update.effective_message
        if not user or not msg or q:
            raise ApplicationHandlerStop
        if not self.admins:
            await msg.reply_text(f"Bot sin configurar. Tu id es {user.id}: ponlo en ALLOWED_USER_IDS y reinicia.")
            raise ApplicationHandlerStop
        row = self.db.user(user.id)
        if row is None:  # first contact: ask the admins, once
            name = user.full_name or str(user.id)
            self.db.set_user(user.id, "pending", name, user.username)
            log.warning("access request from %s (%s, @%s)", user.id, name, user.username)
            who = html.escape(name) + (f" (@{html.escape(user.username)})" if user.username else "")
            kb = KB([[B("✅ Permitir", callback_data=f"au:1:{user.id}"), B("❌ Rechazar", callback_data=f"au:0:{user.id}")]])
            for uid in self.admins:
                try:
                    await self.app.bot.send_message(
                        uid, f"👤 {who}, id {user.id}, quiere usar el bot.\nEscribió: {html.escape((msg.text or '')[:200])}",
                        parse_mode=ParseMode.HTML, reply_markup=kb)
                except TelegramError:
                    log.exception("notify admin")
            await msg.reply_text("Este bot es privado. He pedido permiso al administrador; te aviso si te deja pasar.")
        elif row["status"] == "pending":
            await msg.reply_text("Tu solicitud sigue pendiente del administrador.")
        else:
            log.info("ignored denied user %s", user.id)
        raise ApplicationHandlerStop

    # ---------- notifier (used by the scheduler) ----------
    def _owner(self, alert_id: int) -> int | None:
        row = self.db.alert(alert_id)
        return row["owner"] if row else None

    async def text(self, text: str, alert_id: int | None = None) -> None:
        targets = self.admins if alert_id is None else {self._owner(alert_id)} - {None}
        if alert_id is None:  # developer notice: also tell the other users
            targets = targets | {r["user_id"] for r in self.db.users() if r["status"] == "allowed"}
        for uid in targets:
            try:
                await self.app.bot.send_message(uid, text)
            except TelegramError:
                log.exception("send_message")

    async def listing(self, alert_id: int, cfg: AlertConfig, it: Listing, verdict: str, score: float,
                      why: str, old_price: float | None = None) -> None:
        head = "🎯 <b>ENCAJA</b>" if verdict == "match" else "🤔 quizá"
        if old_price is not None:
            head = "📉 <b>BAJADA</b> · " + head
        d = distance_km(cfg, it)
        where = " · ".join(x for x in [it.city, f"{d:.0f} km" if d is not None else "",
                                        "📦 envío" if it.shippable else ""] if x)
        price = f"{it.price:g}€" + (f" (antes {old_price:g}€)" if old_price is not None else "")
        cap = (f"{head} · {html.escape(cfg.name)} · {score:g}/10\n<b>{html.escape(it.title)}</b>\n"
               f"💶 {price}   📍 {html.escape(where)}\n💬 {html.escape(why)}")
        kb = KB([[B("👍", callback_data=f"fb:1:{alert_id}:{it.id}"), B("👎", callback_data=f"fb:0:{alert_id}:{it.id}"),
                  B("🔍 a fondo", callback_data=f"dp:{alert_id}:{it.id}"), B("🔕", callback_data=f"mu:{it.seller_id}")],
                 [B("Abrir en Wallapop", url=it.url), B("✓ Visto", callback_data="vs")]])
        silent = verdict != "match"
        owner = self._owner(alert_id)
        for uid in [owner] if owner else []:
            try:
                if it.image:
                    await self.app.bot.send_photo(uid, it.image, caption=cap[:1024], parse_mode=ParseMode.HTML,
                                                  reply_markup=kb, disable_notification=silent)
                    continue
            except TelegramError:
                log.warning("send_photo failed, falling back to text")
            try:
                await self.app.bot.send_message(uid, cap, parse_mode=ParseMode.HTML, reply_markup=kb,
                                                disable_notification=silent, disable_web_page_preview=True)
            except TelegramError:
                log.exception("send listing")

    # ---------- helpers ----------
    def _alert(self, ref: str, uid: int):
        row = self.db.find_alert(ref, uid) if ref else None
        return row, (AlertConfig.from_dict(json.loads(row["config"])) if row else None)

    def _draft_kb(self, did: str, cfg: AlertConfig, ok_label: str) -> KB:
        cur = cfg.interval_min or self.s.default_interval_min
        freq = [B(("✓ " if f == cur else "") + f"{f} min", callback_data=f"fq:{did}:{f}") for f in FREQUENCIES]
        return KB([freq, [B(ok_label, callback_data=f"ok:{did}"), B("❌ Descartar", callback_data=f"no:{did}")]])

    def _draft_text(self, d: dict[str, Any]) -> str:
        cfg: AlertConfig = d["cfg"]
        if cfg.interval_min is None:
            cfg.interval_min = self.s.default_interval_min
        if d["mode"] == "new":
            return f"Nueva alerta (elige cada cuánto mira y confirma):\n{pre(cfg_yaml(cfg))}"
        diff = "\n".join(difflib.unified_diff(cfg_yaml(d["old"]).splitlines(), cfg_yaml(cfg).splitlines(),
                                              lineterm="", n=0))
        return f"Cambios en «{html.escape(cfg.name)}»:\n{pre(diff or '(sin cambios)')}"

    async def _show_draft(self, msg, d: dict[str, Any]) -> None:
        did = secrets.token_hex(4)
        self.drafts[did] = d
        label = "✅ Crear" if d["mode"] == "new" else "✅ Aplicar"
        await msg.edit_text(self._draft_text(d)[:4000], parse_mode=ParseMode.HTML,
                            reply_markup=self._draft_kb(did, d["cfg"], label))

    # ---------- commands ----------
    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)

    async def cmd_alta(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(ctx.args)
        uid = update.effective_user.id
        if not self.is_admin(uid) and len(self.db.alerts(owner=uid)) >= self.s.max_alerts_per_user:
            await update.effective_message.reply_text(
                f"Tienes {self.s.max_alerts_per_user} alertas, el máximo. Borra alguna con /baja.")
            return
        if not text:
            await update.effective_message.reply_text("Dime qué buscas: /alta NVMe 512GB <50€ gen3+, Madrid")
            return
        msg = await update.effective_message.reply_text("⏳ Preparando la alerta…")
        try:
            cfg = await derive_alert(self.claude, text, self.s.default_lat, self.s.default_lon)
        except (LLMError, ValueError) as e:
            await msg.edit_text(f"No he podido montar la alerta: {e}")
            return
        await self._show_draft(msg, {"mode": "new", "cfg": cfg, "owner": uid})

    async def cmd_editar(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        row, cfg = self._alert(ctx.args[0] if ctx.args else "", update.effective_user.id)
        if not row or len(ctx.args) < 2:
            await update.effective_message.reply_text("Uso: /editar <n> <cambio>. Mira los n con /lista")
            return
        msg = await update.effective_message.reply_text("⏳ Rehaciendo la alerta…")
        try:
            new = await edit_alert(self.claude, cfg, " ".join(ctx.args[1:]), row["id"])
        except (LLMError, ValueError) as e:
            await msg.edit_text(f"No he podido aplicar el cambio: {e}")
            return
        await self._show_draft(msg, {"mode": "edit", "id": row["id"], "old": cfg, "cfg": new, "owner": update.effective_user.id})

    async def cmd_afinar(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        row, cfg = self._alert(ctx.args[0] if ctx.args else "", update.effective_user.id)
        if not row:
            await update.effective_message.reply_text("Uso: /afinar <n>")
            return
        ex = [dict(r) for r in self.db.feedback_examples(row["id"])]
        if len(ex) < 3:
            await update.effective_message.reply_text(
                f"Hay {len(ex)} valoraciones en «{cfg.name}»; marca al menos 3 avisos con 👍/👎.")
            return
        msg = await update.effective_message.reply_text(f"⏳ Afinando con {len(ex)} valoraciones…")
        try:
            new = await tune_alert(self.claude, cfg, ex, row["id"])
        except (LLMError, ValueError) as e:
            await msg.edit_text(f"No he podido afinarla: {e}")
            return
        await self._show_draft(msg, {"mode": "edit", "id": row["id"], "old": cfg, "cfg": new, "owner": update.effective_user.id})

    async def cmd_frecuencia(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        row, cfg = self._alert(ctx.args[0] if ctx.args else "", update.effective_user.id)
        if not row or len(ctx.args) < 2 or not ctx.args[1].isdigit():
            await update.effective_message.reply_text("Uso: /frecuencia <n> <minutos> (mínimo 5)")
            return
        cfg.interval_min = max(5, int(ctx.args[1]))
        self.db.update_alert(row["id"], cfg)
        self.sched.next_run.pop(row["id"], None)
        await update.effective_message.reply_text(f"«{cfg.name}» mirará cada {cfg.interval_min} min.")

    async def _set_paused(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, paused: bool) -> None:
        row, cfg = self._alert(ctx.args[0] if ctx.args else "", update.effective_user.id)
        if not row:
            await update.effective_message.reply_text("¿Qué alerta? Mira los n con /lista")
            return
        self.db.set_paused(row["id"], paused)
        if not paused:
            self.sched.run_soon(row["id"])
        await update.effective_message.reply_text(f"«{cfg.name}» {'en pausa ⏸' if paused else 'activa ▶️'}")

    async def cmd_pausar(self, update, ctx):
        await self._set_paused(update, ctx, True)

    async def cmd_reanudar(self, update, ctx):
        await self._set_paused(update, ctx, False)

    async def cmd_baja(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        row, cfg = self._alert(ctx.args[0] if ctx.args else "", update.effective_user.id)
        if not row:
            await update.effective_message.reply_text("¿Qué alerta? Mira los n con /lista")
            return
        await update.effective_message.reply_text(
            f"¿Borrar «{cfg.name}» y su historial?",
            reply_markup=KB([[B("🗑 Sí, borrar", callback_data=f"rm:{row['id']}"), B("Cancelar", callback_data="x")]]))

    async def cmd_lista(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        rows = self.db.alerts(owner=update.effective_user.id)
        if not rows:
            await update.effective_message.reply_text("Sin alertas. Crea una con /alta")
            return
        out = []
        for r in rows:
            c = AlertConfig.from_dict(json.loads(r["config"]))
            st = self.db.stats(r["id"])
            price = f"{c.price_min or 0:g}–{c.price_max:g}€" if c.price_max else (f"≥{c.price_min:g}€" if c.price_min else "")
            radius = f"{c.radius_km:g} km" if c.radius_km else "toda España"
            out.append(
                f"{'⏸' if r['paused'] else '▶️'} <b>{r['id']}. {html.escape(c.name)}</b> — “{html.escape(c.query)}” "
                f"{price} · {radius} · cada {c.interval_min or self.s.default_interval_min} min\n"
                f"   🎯{st.get('match', 0)} 🤔{st.get('maybe', 0)} ✖{st.get('reject', 0)} ⛔{st.get('pre', 0)}"
                f" · {ago(r['last_run'])}" + (f"\n   ⚠️ {html.escape(r['last_error'][:100])}" if r["last_error"] else ""))
        await update.effective_message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)

    async def cmd_estado(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        lines = [self.sched.health.status(), ""]
        use = self.db.usage_today()
        if use:
            lines.append("Hoy:")
            for u in use:
                lines.append(f"  {u['model']}: {u['calls']} llamadas · {u['i'] // 1000}k/{u['o'] // 1000}k tokens "
                             f"· ~{u['cost']:.2f}$ equiv.")
        else:
            lines.append("Hoy: sin llamadas al modelo.")
        uid = update.effective_user.id
        if not self.is_admin(uid):
            lines = lines[:2] + [f"Tus llamadas hoy: {self.db.calls_today_owner(uid)}"]
        else:
            lines.append(f"Tope global: {self.db.calls_today()}/{self.s.global_max_llm_per_day}")
        for r in self.db.alerts(owner=uid):
            c = AlertConfig.from_dict(json.loads(r["config"]))
            lines.append(f"  {r['id']}. {c.name}: {self.db.calls_today(r['id'])}/{c.max_llm_per_day}")
        await update.effective_message.reply_text("\n".join(lines))

    # ---------- buttons ----------
    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        parts = (q.data or "").split(":")
        kind = parts[0]
        if kind in ("fq", "ok", "no"):
            d = self.drafts.get(parts[1])
            if not d or d["owner"] != q.from_user.id:
                await q.answer("Borrador caducado; repite la orden.")
                return
            if kind == "fq":
                d["cfg"].interval_min = int(parts[2])
                label = "✅ Crear" if d["mode"] == "new" else "✅ Aplicar"
                await q.answer(f"Cada {parts[2]} min")
                await q.edit_message_text(self._draft_text(d)[:4000], parse_mode=ParseMode.HTML,
                                          reply_markup=self._draft_kb(parts[1], d["cfg"], label))
                return
            self.drafts.pop(parts[1], None)
            if kind == "no":
                await q.answer("Descartado")
                await q.edit_message_reply_markup(None)
                return
            cfg = d["cfg"]
            if d["mode"] == "new":
                aid = self.db.add_alert(cfg, d["owner"])
                text = f"✅ Alerta {aid} «{cfg.name}» creada; miro ya y luego cada {cfg.interval_min} min."
            else:
                aid = d["id"]
                self.db.update_alert(aid, cfg)
                text = f"✅ «{cfg.name}» actualizada."
            self.sched.run_soon(aid)
            await q.answer("Hecho")
            await q.edit_message_reply_markup(None)
            await q.message.reply_text(text)
        elif kind == "fb":
            if self._owner(int(parts[2])) != q.from_user.id:
                await q.answer()
                return
            self.db.add_feedback(int(parts[2]), parts[3], 1 if parts[1] == "1" else -1)
            await q.answer("Anotado " + ("👍" if parts[1] == "1" else "👎"))
        elif kind == "vs":  # seen: drop the buttons, keep the link; nothing is stored
            await q.answer("✓ Visto")
            link = [b for row in (q.message.reply_markup.inline_keyboard if q.message.reply_markup else [])
                    for b in row if b.url]
            await q.edit_message_reply_markup(KB([link + [B("✓ visto", callback_data="x0")]]) if link else None)
        elif kind == "x0":
            await q.answer()
        elif kind == "mu":
            self.db.mute_seller(q.from_user.id, parts[1])
            await q.answer("🔕 Vendedor silenciado")
        elif kind == "dp":
            await self._deep(q, int(parts[1]), parts[2])
        elif kind == "rm":
            if self._owner(int(parts[1])) != q.from_user.id:
                await q.answer()
                return
            self.db.delete_alert(int(parts[1]))
            self.sched.next_run.pop(int(parts[1]), None)
            await q.answer("Borrada")
            await q.edit_message_text("🗑 Alerta borrada.")
        elif kind in ("au", "ux"):
            await self._user_button(q, parts)
        else:
            await q.answer()
            await q.edit_message_reply_markup(None)

    # ---------- users (admin) ----------
    async def _user_button(self, q, parts: list[str]) -> None:
        if not self.is_admin(q.from_user.id):
            await q.answer()
            return
        if parts[0] == "ux":  # revoke from /usuarios
            uid, ok = int(parts[1]), False
        else:
            uid, ok = int(parts[2]), parts[1] == "1"
        row = self.db.user(uid)
        name = row["name"] if row else str(uid)
        self.db.set_user(uid, "allowed" if ok else "denied")
        if not ok:
            self.db.pause_owner(uid)
        await q.answer("Permitido" if ok else "Rechazado")
        await q.edit_message_text(f"{'✅ Permitido' if ok else '🚫 Sin acceso'}: {name} ({uid})")
        if ok:
            try:
                await self.app.bot.send_message(uid, "✅ El administrador te ha dado acceso.\n\n" + HELP,
                                                parse_mode=ParseMode.HTML)
            except TelegramError:
                log.warning("could not greet %s", uid)

    async def cmd_usuarios(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.is_admin(update.effective_user.id):
            return
        rows = self.db.users()
        if not rows:
            await update.effective_message.reply_text("Nadie más ha pedido acceso.")
            return
        icon = {"allowed": "✅", "pending": "⏳", "denied": "🚫"}
        for r in rows:
            who = f"{icon.get(r['status'], '?')} {r['name']}" + (f" @{r['username']}" if r["username"] else "")
            who += f" ({r['user_id']}) · {len(self.db.alerts(owner=r['user_id']))} alertas"
            if r["status"] == "allowed":
                kb = KB([[B("🚫 Quitar acceso", callback_data=f"ux:{r['user_id']}")]])
            else:
                kb = KB([[B("✅ Permitir", callback_data=f"au:1:{r['user_id']}"),
                          B("❌ Rechazar", callback_data=f"au:0:{r['user_id']}")]])
            await update.effective_message.reply_text(who, reply_markup=kb)

    async def _deep(self, q, alert_id: int, item_id: str) -> None:
        row, cfg = self._alert(str(alert_id), q.from_user.id)
        it = self.db.listing(alert_id, item_id)
        if not row or not it:
            await q.answer("Ya no tengo ese anuncio.")
            return
        await q.answer("🔍 Mirando a fondo…")
        try:
            text = await deep_look(self.claude, cfg, dict(it), alert_id)
        except LLMError as e:
            text = f"No he podido analizarlo: {e}"
        await q.message.reply_text(f"🔍 {text}"[:4000], do_quote=True)

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("handler error", exc_info=ctx.error)

    # ---------- wiring ----------
    def build(self, sched_factory) -> Application:
        async def post_init(app: Application) -> None:
            if self.admins:
                self.db.claim_unowned(min(self.admins))
            self.sched = sched_factory(self)
            self._loop_task = asyncio.create_task(self.sched.loop())
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
            log.info("bot ready; admins: %s", sorted(self.admins))

        app = (Application.builder().token(self.s.telegram_token).concurrent_updates(True)
               .post_init(post_init).build())
        self.app = app
        app.add_handler(TypeHandler(Update, self.gate), group=-1)
        for name, fn in [("start", self.cmd_help), ("ayuda", self.cmd_help), ("help", self.cmd_help),
                         ("alta", self.cmd_alta), ("editar", self.cmd_editar), ("afinar", self.cmd_afinar),
                         ("frecuencia", self.cmd_frecuencia), ("pausar", self.cmd_pausar),
                         ("reanudar", self.cmd_reanudar), ("baja", self.cmd_baja), ("lista", self.cmd_lista),
                         ("estado", self.cmd_estado), ("usuarios", self.cmd_usuarios)]:
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(CallbackQueryHandler(self.on_button))
        app.add_error_handler(self.on_error)
        return app
