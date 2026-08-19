"""
Webhook de backup de conversas da Unnichat.

Chamado pelo fluxo de automação de cada conexão (INSS/TJ/BB) quando um
contato recebe a tag backup_supabase_{course}. Busca as mensagens do
contato e arquiva tudo: texto direto na tabela, mídia (áudio/vídeo/
imagem/documento) no Supabase Storage com o caminho salvo na tabela.

Rota: POST /webhook/{course}   (course = inss | tj | bb)
Header obrigatório: X-Webhook-Secret
Body: o objeto que a própria Unnichat manda (contém "contact.id" ou "id").
"""

import mimetypes
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
    print(f"[{course}] payload recebido: {body}")

    contact_id = (
        body.get("id")
        or body.get("contactId")
        or (body.get("contact") or {}).get("id")
        or (body.get("data") or {}).get("id")
    )
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

    results = [backup_message(course, contact_id, msg) for msg in messages]
    return {"success": True, "contactId": contact_id, "course": course, "processed": results}


def backup_message(course: str, contact_id: str, msg: dict) -> dict:
    message_id = msg["id"]

    existing = (
        supabase.table("unnichat_message_backups")
        .select("message_id")
        .eq("message_id", message_id)
        .execute()
    )
    if existing.data:
        return {"messageId": message_id, "status": "already_backed_up"}

    row = {
        "message_id": message_id,
        "contact_id": contact_id,
        "course": course,
        "message_type": msg.get("type", "unknown"),
        "sender_by": msg.get("senderBy"),
        "message_date": msg["date"],
        "text_content": msg.get("message"),
        "storage_path": None,
        "original_url": None,
    }

    if msg.get("url"):
        media_resp = requests.get(msg["url"], timeout=60)
        media_resp.raise_for_status()

        ext = msg["url"].split("?")[0].rsplit(".", 1)[-1]
        content_type = mimetypes.guess_type(f"file.{ext}")[0] or "application/octet-stream"
        storage_path = f"{course}/{contact_id}/{message_id}.{ext}"

        supabase.storage.from_(SUPABASE_BUCKET).upload(
            storage_path, media_resp.content, {"content-type": content_type}
        )

        row["storage_path"] = storage_path
        row["original_url"] = msg["url"]

    supabase.table("unnichat_message_backups").insert(row).execute()

    return {"messageId": message_id, "status": "backed_up"}


@app.get("/health")
async def health():
    return {"status": "ok"}
