import asyncio
from datetime import datetime, timezone
from html import escape
from typing import Optional

import httpx
import json
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

PREMIUM_PLANS = {
    "p1": {"label": "PLAN 1", "price": 39, "days": 30, "period": "1 Month"},
    "p2": {"label": "PLAN 2", "price": 69, "days": 60, "period": "2 Months"},
    "p3": {"label": "PLAN 3", "price": 99, "days": 90, "period": "3 Months"},
    "p4": {"label": "PLAN 4", "price": 199, "days": 180, "period": "6 Months"},
    "p5": {"label": "PLAN 5", "price": 349, "days": 365, "period": "1 Year"},
}
PLAN_ORDER = ["p1", "p2", "p3", "p4", "p5"]

PREMIUM_BENEFITS = (
    "✦ Private movie search directly in the bot\n"
    "✦ Direct file delivery after F-Sub\n"
    "✦ No Softurl verification step\n"
    "✦ Browser Stream & Download access\n"
    "✦ Cinema HUB OG Premium membership badge"
)
PREMIUM_ONLY_ALERT = "Premium membership required."
NOT_ADDED_MESSAGE = "Unfortunately This Isn't Available On Our Database Right Now, Search Again After 10 Mins It Will Be Available for sure. Thank You ❤️"


class PremiumManager:
    """Dedicated payment-bot polling + admin approval controller.

    The payment bot is intentionally separate from the main bot token. It creates
    a clean proof -> admin approval -> premium activation pipeline.
    """

    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._offset = 0
        self._http: Optional[httpx.AsyncClient] = None

    @property
    def enabled(self):
        return bool(self.cfg.payment_bot_token)

    async def start(self):
        if not self.enabled or self._task:
            return
        self._stopping = False
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=15.0))
        self._task = asyncio.create_task(self._run(), name="premium-payment-bot")

    async def stop(self):
        self._stopping = True
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._http:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    async def _api(self, method, payload=None):
        if not self._http:
            raise RuntimeError("Premium payment bot HTTP client is not running")
        response = await self._http.post(
            f"https://api.telegram.org/bot{self.cfg.payment_bot_token}/{method}",
            json=payload or {},
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(str(data))
        return data.get("result")

    async def _run(self):
        try:
            me = await self._api("getMe")
            actual_username = str((me or {}).get("username") or "").lstrip("@")
            configured_username = str(self.cfg.payment_bot_username or "").lstrip("@")
            if actual_username:
                if configured_username and configured_username != actual_username:
                    print(
                        f"PAYMENT_BOT_USERNAME={configured_username} does not match @{actual_username}; using Telegram's actual username."
                    )
                self.cfg.payment_bot_username = actual_username
            webhook = await self._api("getWebhookInfo")
            if webhook and webhook.get("url"):
                if self.cfg.payment_bot_force_polling:
                    await self._api("deleteWebhook", {"drop_pending_updates": False})
                else:
                    print(
                        "Premium payment bot has an active webhook; polling is disabled. "
                        "Set PAYMENT_BOT_FORCE_POLLING=true to switch this bot to polling."
                    )
                    return
        except Exception as exc:
            print(f"Could not prepare premium payment bot polling: {exc}")

        while not self._stopping:
            try:
                updates = await self._api(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": 25,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for update in updates or []:
                    self._offset = int(update.get("update_id", self._offset)) + 1
                    try:
                        await self._handle_update(update)
                    except Exception as exc:
                        print(f"Premium payment update failed: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Premium payment polling error: {exc}")
                await asyncio.sleep(3)

    async def _handle_update(self, update):
        if update.get("callback_query"):
            await self._handle_admin_callback(update["callback_query"])
            return
        msg = update.get("message") or {}
        user = msg.get("from") or {}
        user_id = int(user.get("id", 0))
        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            payload = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if payload.startswith("pay_"):
                await self._handle_payment_start(user_id, payload[4:])
                return
        if msg.get("photo"):
            await self._handle_screenshot(user_id, msg)
            return
        if text:
            await self._send_payment_bot_message(
                user_id,
                "📸 <b>PAYMENT PROOF</b>\n\nSend the UPI payment screenshot here. Your latest pending plan request will be attached automatically.",
            )

    async def _handle_payment_start(self, user_id, request_id):
        req = await self.db.get_premium_payment_request(request_id)
        if not req or int(req.get("user_id", 0)) != user_id or req.get("status") != "pending":
            await self._send_payment_bot_message(
                user_id,
                "❌ <b>PAYMENT REQUEST EXPIRED</b>\n\nReturn to Cinema HUB OG and create a new premium request.",
            )
            return
        await self.db.touch_payment_request(request_id, user_id)
        plan = PREMIUM_PLANS.get(req.get("plan_id"), {})
        await self._send_payment_bot_message(
            user_id,
            "╭━━━ <b>CINEMA HUB OG</b> ━━━╮\n"
            "│ <b>UPI PAYMENT REQUEST</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            f"💎 <b>{escape(plan.get('label', 'PREMIUM'))}</b>\n"
            f"₹{plan.get('price', '?')} • {escape(plan.get('period', ''))}\n\n"
            "Send the payment screenshot here after completing the UPI payment.\n"
            "Your proof goes to the admin review queue.",
        )

    async def _handle_screenshot(self, user_id, msg):
        req = await self.db.latest_pending_payment_request(user_id)
        if not req:
            await self._send_payment_bot_message(
                user_id,
                "⚠️ <b>NO PENDING PAYMENT</b>\n\nOpen Cinema HUB OG → Premium Plans and create a fresh request first.",
            )
            return
        photos = msg.get("photo") or []
        if not photos:
            return
        photo = photos[-1]
        request_id = req["request_id"]
        await self.db.attach_payment_screenshot(
            request_id,
            user_id,
            photo.get("file_id"),
            msg.get("caption") or "",
        )
        plan = PREMIUM_PLANS.get(req.get("plan_id"), {})
        caption = (
            "👑 <b>CINEMA HUB OG — PREMIUM REVIEW</b>\n\n"
            f"🆔 Request: <code>{escape(request_id)}</code>\n"
            f"👤 User ID: <code>{user_id}</code>\n"
            f"💎 Plan: <b>{escape(plan.get('label', 'PREMIUM'))}</b>\n"
            f"💰 Amount: <b>₹{plan.get('price', '?')}</b>\n"
            f"⏳ Duration: <b>{escape(plan.get('period', ''))}</b>\n\n"
            "Review the payment proof and choose an action."
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ APPROVE", "callback_data": f"prem_approve:{request_id}"},
                {"text": "❌ REJECT", "callback_data": f"prem_reject:{request_id}"},
            ]]
        }
        for admin_id in sorted(self.cfg.admin_ids):
            try:
                await self._api(
                    "sendPhoto",
                    {
                        "chat_id": admin_id,
                        "photo": photo.get("file_id"),
                        "caption": caption,
                        "parse_mode": "HTML",
                        "reply_markup": keyboard,
                    },
                )
            except Exception as exc:
                print(f"Could not send premium proof to payment-bot admin {admin_id}: {exc}")
            try:
                await self._send_main_bot_admin_proof(
                    admin_id, photo.get("file_id"), caption, request_id
                )
            except Exception as exc:
                print(f"Could not send premium proof to main-bot admin {admin_id}: {exc}")
        await self._send_payment_bot_message(
            user_id,
            "✅ <b>PAYMENT PROOF RECEIVED</b>\n\nYour request is now in the admin review queue. You will receive the result here automatically.",
        )

    async def _handle_admin_callback(self, query):
        callback_id = query.get("id")
        admin_id = int((query.get("from") or {}).get("id", 0))
        data = str(query.get("data") or "")
        if admin_id not in self.cfg.admin_ids:
            if callback_id:
                await self._api(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_id,
                        "text": "Not authorized.",
                        "show_alert": True,
                    },
                )
            return
        action, _, request_id = data.partition(":")
        if action == "prem_approve":
            result = await self.approve_request(request_id, admin_id)
        elif action == "prem_reject":
            result = await self.reject_request(request_id, admin_id)
        else:
            return
        if callback_id:
            await self._api(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_id,
                    "text": result["toast"],
                    "show_alert": result.get("alert", False),
                },
            )
        message = query.get("message") or {}
        if message.get("chat") and message.get("message_id"):
            try:
                await self._api(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": message["chat"]["id"],
                        "message_id": message["message_id"],
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
            except Exception:
                pass

    async def approve_request(self, request_id, admin_id):
        req = await self.db.claim_payment_request(request_id, admin_id, "approve")
        if not req:
            return {"toast": "Request already handled.", "alert": True}
        plan = PREMIUM_PLANS.get(req.get("plan_id"))
        if not plan:
            await self.db.finalize_payment_request(request_id, "rejected", admin_id)
            return {"toast": "Invalid plan.", "alert": True}

        try:
            premium_until = await self.db.activate_premium(
                int(req["user_id"]),
                plan["days"],
                req.get("plan_id"),
                plan["price"],
                source="payment_approval",
            )
            await self.db.finalize_payment_request(request_id, "approved", admin_id)
            await self._set_premium_group_tag(int(req["user_id"]), self.cfg.premium_member_tag)
            message = (
                "╭━━━ <b>CINEMA HUB OG</b> ━━━╮\n"
                "│ 👑 <b>PREMIUM ACTIVATED</b>\n"
                "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"💎 <b>{escape(plan['label'])}</b> • ₹{plan['price']}\n"
                f"⏳ <b>Valid Until:</b> {premium_until.strftime('%d %b %Y')}\n\n"
                "Your Premium membership is now live.\n"
                "Force Subscribe still applies; after that you get private search, direct files and browser Stream & Download."
            )
            try:
                await self._send_main_bot_message(int(req["user_id"]), message)
            except Exception:
                pass
            try:
                await self._send_payment_bot_message(int(req["user_id"]), message)
            except Exception:
                pass
            return {"toast": "Premium approved ✅"}
        except Exception as exc:
            print(f"Premium approval failed for {request_id}: {exc}")
            try:
                await self.db.finalize_payment_request(request_id, "pending", admin_id)
            except Exception:
                pass
            return {"toast": "Approval failed; request restored.", "alert": True}

    async def reject_request(self, request_id, admin_id):
        req = await self.db.claim_payment_request(request_id, admin_id, "reject")
        if not req:
            return {"toast": "Request already handled.", "alert": True}
        await self.db.finalize_payment_request(request_id, "rejected", admin_id)
        message = (
            "╭━━━ <b>CINEMA HUB OG</b> ━━━╮\n"
            "│ ❌ <b>PAYMENT NOT APPROVED</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "The submitted payment proof could not be approved.\n\n"
            "You can create a fresh UPI payment request from Premium Plans and submit a new screenshot."
        )
        try:
            await self._send_main_bot_message(int(req["user_id"]), message)
        except Exception:
            pass
        try:
            await self._send_payment_bot_message(int(req["user_id"]), message)
        except Exception:
            pass
        return {"toast": "Premium request rejected."}

    async def _send_main_bot_admin_proof(self, admin_id, file_id, caption, request_id):
        """Upload the proof to the main bot too, giving admins a second approval surface."""
        if not self._http or not file_id:
            return
        tg_file = await self._api("getFile", {"file_id": file_id})
        file_path = (tg_file or {}).get("file_path")
        if not file_path:
            raise RuntimeError("Payment bot did not return a file path")
        file_response = await self._http.get(
            f"https://api.telegram.org/file/bot{self.cfg.payment_bot_token}/{file_path}"
        )
        file_response.raise_for_status()
        main_markup = {
            "inline_keyboard": [[
                {"text": "✅ APPROVE", "callback_data": f"payadmin:approve:{request_id}"},
                {"text": "❌ REJECT", "callback_data": f"payadmin:reject:{request_id}"},
            ]]
        }
        files = {"photo": ("payment-proof.jpg", file_response.content, "image/jpeg")}
        data = {
            "chat_id": str(admin_id),
            "caption": caption,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(main_markup),
        }
        response = await self._http.post(
            f"https://api.telegram.org/bot{self.cfg.bot_token}/sendPhoto",
            data=data,
            files=files,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload))

    async def _set_premium_group_tag(self, user_id, tag):
        if not self._http:
            return
        try:
            response = await self._http.post(
                f"https://api.telegram.org/bot{self.cfg.bot_token}/setChatMemberTag",
                json={
                    "chat_id": self.cfg.request_group,
                    "user_id": int(user_id),
                    "tag": tag[:16],
                },
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                print(f"Could not set premium member tag: {data}")
        except Exception as exc:
            print(f"Could not set premium group tag for {user_id}: {exc}")

    async def _send_payment_bot_message(self, user_id, text):
        await self._api("sendMessage", {"chat_id": user_id, "text": text, "parse_mode": "HTML"})

    async def _send_main_bot_message(self, user_id, text):
        if not self._http:
            return
        response = await self._http.post(
            f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage",
            json={"chat_id": user_id, "text": text, "parse_mode": "HTML"},
        )
        response.raise_for_status()


def premium_plans_text():
    return (
        "╭━━━ <b>CINEMA HUB OG</b> ━━━╮\n"
        "│ 👑 <b>PREMIUM MEMBERSHIP</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<b>What you unlock</b>\n\n"
        f"{PREMIUM_BENEFITS}\n\n"
        "<b>Choose your membership</b>\n"
        "₹39 • 1 Month\n"
        "₹69 • 2 Months\n"
        "₹99 • 3 Months\n"
        "₹199 • 6 Months\n"
        "₹349 • 1 Year\n\n"
        "<i>One activation. One clean Premium experience.</i>"
    )


def premium_plans_keyboard():
    rows = []
    for key in PLAN_ORDER:
        plan = PREMIUM_PLANS[key]
        rows.append([
            InlineKeyboardButton(
                f"💎 {plan['period']} • ₹{plan['price']}",
                callback_data=f"premium:plan:{key}",
            )
        ])
    rows.append([InlineKeyboardButton("🎁 REFER & GET PREMIUM", callback_data="menu:referral")])
    rows.append([InlineKeyboardButton("← BACK", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def payment_text(plan_id):
    plan = PREMIUM_PLANS[plan_id]
    return (
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "│  💎 <b>CINEMA HUB OG</b>  │\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "<b>PREMIUM ACTIVATION</b>\n\n"
        f"<b>{escape(plan['label'])}</b>  •  ₹{plan['price']}\n"
        f"<b>{escape(plan['period'])}</b>\n\n"
        "<b>PAY VIA UPI ONLY</b>\n"
        "① Open the official QR channel.\n"
        "② Complete the UPI payment.\n"
        "③ Tap <b>SEND PAYMENT SCREENSHOT</b>.\n"
        "④ Admin review decides activation.\n\n"
        "<i>Your Premium membership starts only after approval.</i>"
    )


def payment_keyboard(owner_url, qr_url):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 QR CODE • CLICK HERE", url=qr_url)],
        [InlineKeyboardButton("📸 SEND PAYMENT SCREENSHOT", url=owner_url)],
        [InlineKeyboardButton("← BACK TO PLANS", callback_data="premium:plans")],
    ])
