"""
Client d'appel à la WhatsApp Cloud API (section 5.1).
Isolé dans un module dédié pour rester facilement testable (mock) sans réseau réel.
"""
import hashlib
import hmac

import httpx

from app.core.config import get_settings

settings = get_settings()

GRAPH_BASE_URL = "https://graph.facebook.com"


def verify_whatsapp_signature(app_secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """
    Section 32 — validation des webhooks. Meta signe chaque requête avec
    X-Hub-Signature-256: sha256=<HMAC-SHA256(app_secret, corps brut)>.
    Une signature absente ou invalide doit être rejetée, jamais tolérée silencieusement.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


class WhatsAppClient:
    def __init__(self, phone_number_id: str, system_user_token: str):
        self.phone_number_id = phone_number_id
        self.token = system_user_token
        self.base_url = f"{GRAPH_BASE_URL}/{settings.whatsapp_graph_api_version}/{phone_number_id}"

    async def send_text_message(self, to: str, body: str) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        return await self._post("/messages", payload)

    async def send_template_message(self, to: str, template_name: str, language: str = "fr") -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {"name": template_name, "language": {"code": language}},
        }
        return await self._post("/messages", payload)

    async def _post(self, path: str, payload: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{self.base_url}{path}", json=payload, headers=headers)
            response.raise_for_status()
            return response.json()


def parse_whatsapp_message(payload: dict) -> dict | None:
    """
    Extrait le premier message entrant d'un payload de webhook Meta.
    Retourne None si le payload est un accusé de statut (delivered/read) plutôt qu'un message.
    """
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]
        phone_number_id = value["metadata"]["phone_number_id"]

        messages = value.get("messages")
        if not messages:
            return None  # ex. accusé de statut, pas un message entrant

        msg = messages[0]
        return {
            "phone_number_id": phone_number_id,
            "from": msg["from"],
            "wa_message_id": msg["id"],
            "type": msg.get("type", "text"),
            "text": msg.get("text", {}).get("body"),
            "timestamp": msg.get("timestamp"),
        }
    except (KeyError, IndexError, TypeError):
        return None
