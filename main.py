"""
Webhook de backup de áudios da Unnichat.

Chamado pelo fluxo de automação de cada conexão (INSS/TJ/BB) quando um
contato recebe a tag backup_supabase_{course}. Busca as mensagens do
contato, filtra os áudios e arquiva no Supabase Storage + tabela
unnichat_audio_backups.

Rota: POST /webhook/{course}   (course = inss | tj | bb)
Header obrigatório: X-Webhook-Secret
Body: o objeto do contato que a própria Unnichat manda (contém "id").
"""

import os

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from supabase import create_client

UNNICHAT_API_BASE = "https://unnichat.com.br/api"
SUPABASE_BUCKET = "unnichat-audios"

UNNICHAT_TOKENS = {
    "inss": os.environ["UNNICHAT_TOKEN_INSS"],
    "tj": os.environ["UNNICHAT_TOKEN_TJ"],
    "bb": os.environ["UNNICHAT_TOKEN_BB"],
}
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

app = FastAPI()


@app.post("/webhook/{course}")
async def webhook(course: str, request: Request, x_webhook_secret: str = Header(None)):
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    if course not in UNNICHAT_TOKENS:
        raise HTTPException(status_code=400, detail="course inválido")

    body = await request.json()
    contact_id = body.get("id") or body.get("contactId")
    if not contact_id:
        raise HTTPException(status_code=400, detail="contactId ausente no body")

    token = UNNICHAT_TOKENS[course]
    resp = requests.get(
        f"{UNNICHAT_API_BASE}/contact/{contact_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    messages = resp.json()["data"]
    audio_messages = [m for m in messages if m.get("type") == "audio"]

    results = [backup_audio_message(course, contact_id, msg) for msg in audio_messages]
    return {"success": True, "contactId": contact_id, "course": course, "processed": results}


def backup_audio_message(course: str, contact_id: str, msg: dict) -> dict:
    message_id = msg["id"]

    existing = (
        supabase.table("unnichat_audio_backups")
        .select("message_id")
        .eq("message_id", message_id)
        .execute()
    )
    if existing.data:
        return {"messageId": message_id, "status": "already_backed_up"}

    audio_resp = requests.get(msg["url"], timeout=60)
    audio_resp.raise_for_status()
    audio_bytes = audio_resp.content

    ext = msg["url"].split("?")[0].rsplit(".", 1)[-1]
    storage_path = f"{course}/{contact_id}/{message_id}.{ext}"

    supabase.storage.from_(SUPABASE_BUCKET).upload(
        storage_path, audio_bytes, {"content-type": f"audio/{ext}"}
    )

    supabase.table("unnichat_audio_backups").insert(
        {
            "message_id": message_id,
            "contact_id": contact_id,
            "course": course,
            "sender_by": msg["senderBy"],
            "message_date": msg["date"],
            "storage_path": storage_path,
            "original_url": msg["url"],
        }
    ).execute()

    return {"messageId": message_id, "status": "backed_up"}


@app.get("/health")
async def health():
    return {"status": "ok"}
