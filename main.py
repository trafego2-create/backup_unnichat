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

import html
import io
import mimetypes
import os
import secrets
import zipfile

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from supabase import create_client

UNNICHAT_API_BASE = "https://unnichat.com.br/api"
SUPABASE_BUCKET = "unnichat-audios"

UNNICHAT_TOKENS = {
    "inss": os.environ["UNNICHAT_TOKEN_INSS"],
    "tj": os.environ["UNNICHAT_TOKEN_TJ"],
    "bb": os.environ["UNNICHAT_TOKEN_BB"],
}
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
DASHBOARD_PASSWORD = os.environ["DASHBOARD_PASSWORD"]

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

app = FastAPI()
basic_auth = HTTPBasic()


def require_dashboard_auth(credentials: HTTPBasicCredentials = Depends(basic_auth)) -> None:
    if not secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD):
        raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Basic"})


@app.post("/webhook/{course}")
async def webhook(course: str, request: Request, x_webhook_secret: str = Header(None)):
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")
    if course not in UNNICHAT_TOKENS:
        raise HTTPException(status_code=400, detail="course inválido")

    body = await request.json()
    print(f"[{course}] payload recebido: {body}")

    # A Unnichat manda o contato solto no corpo, ou aninhado em "contact"/"data"
    # dependendo do gatilho da automação.
    contact_obj = body.get("contact") or body.get("data") or body
    contact_id = contact_obj.get("id") or body.get("contactId")
    if not contact_id:
        raise HTTPException(status_code=400, detail="contactId ausente no body")
    phone_number = contact_obj.get("phoneNumber")
    contact_name = contact_obj.get("name")

    token = UNNICHAT_TOKENS[course]
    resp = requests.get(
        f"{UNNICHAT_API_BASE}/contact/{contact_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    messages = resp.json()["data"]

    results = [
        backup_message(course, contact_id, phone_number, contact_name, msg) for msg in messages
    ]
    return {"success": True, "contactId": contact_id, "course": course, "processed": results}


def backup_message(course: str, contact_id: str, phone_number: str, contact_name: str, msg: dict) -> dict:
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
        "phone_number": phone_number,
        "contact_name": contact_name,
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


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(_: None = Depends(require_dashboard_auth)):
    summary = (
        supabase.table("contact_backup_summary")
        .select("*")
        .order("last_message_date", desc=True)
        .execute()
    )

    rows_html = ""
    for c in summary.data:
        rows_html += f"""
        <tr>
            <td>{html.escape(c.get("phone_number") or "-")}</td>
            <td>{html.escape(c.get("contact_name") or "-")}</td>
            <td>{html.escape(c["course"])}</td>
            <td>{c["message_count"]}</td>
            <td>{c["media_count"]}</td>
            <td>{html.escape(c.get("last_message_date") or "-")}</td>
            <td><a href="/dashboard/download/{c['course']}/{c['contact_id']}">Baixar</a></td>
        </tr>
        """

    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <title>Backups Unnichat</title>
        <style>
            body {{ font-family: sans-serif; margin: 2rem; }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
            th {{ background: #f0f0f0; }}
        </style>
    </head>
    <body>
        <h1>Backups Unnichat</h1>
        <table>
            <tr>
                <th>Telefone</th><th>Nome</th><th>Curso</th>
                <th>Mensagens</th><th>Mídias</th><th>Última mensagem</th><th></th>
            </tr>
            {rows_html}
        </table>
    </body>
    </html>
    """


@app.get("/dashboard/download/{course}/{contact_id}")
async def download_contact(course: str, contact_id: str, _: None = Depends(require_dashboard_auth)):
    messages = (
        supabase.table("unnichat_message_backups")
        .select("*")
        .eq("course", course)
        .eq("contact_id", contact_id)
        .order("message_date")
        .execute()
    ).data
    if not messages:
        raise HTTPException(status_code=404, detail="nenhum backup encontrado para esse contato")

    phone = messages[0].get("phone_number") or contact_id

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        transcript_lines = []
        for m in messages:
            who = "Atendente" if m["sender_by"] == "user" else "Cliente"
            line = f"[{m['message_date']}] {who} ({m['message_type']})"
            if m.get("text_content"):
                line += f": {m['text_content']}"
            transcript_lines.append(line)

            if m.get("storage_path"):
                file_bytes = supabase.storage.from_(SUPABASE_BUCKET).download(m["storage_path"])
                zf.writestr(m["storage_path"].split("/")[-1], file_bytes)

        zf.writestr("conversa.txt", "\n".join(transcript_lines))

    zip_buffer.seek(0)
    filename = f"{course}_{phone}.zip"
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
