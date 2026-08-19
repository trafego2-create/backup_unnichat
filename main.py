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
    headers = {"Authorization": f"Bearer {token}"}

    # O payload da automação não traz "createdAt" (só vem no GET /contact/{id}
    # completo), então buscamos separado.
    contact_resp = requests.get(f"{UNNICHAT_API_BASE}/contact/{contact_id}", headers=headers, timeout=30)
    contact_created_at = contact_resp.json().get("data", {}).get("createdAt") if contact_resp.ok else None

    resp = requests.get(
        f"{UNNICHAT_API_BASE}/contact/{contact_id}/messages",
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    messages = resp.json()["data"]

    results = [
        backup_message(course, contact_id, phone_number, contact_name, contact_created_at, msg)
        for msg in messages
    ]
    return {"success": True, "contactId": contact_id, "course": course, "processed": results}


def backup_message(
    course: str,
    contact_id: str,
    phone_number: str,
    contact_name: str,
    contact_created_at: str,
    msg: dict,
) -> dict:
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
        "contact_created_at": contact_created_at,
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


COURSE_LABELS = {"inss": "INSS", "tj": "TJ", "bb": "BB"}
COURSE_PILL_CLASS = {"inss": "info", "tj": "warning", "bb": "danger"}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(_: None = Depends(require_dashboard_auth)):
    summary = (
        supabase.table("contact_backup_summary")
        .select("*")
        .order("last_message_date", desc=True)
        .execute()
    ).data

    total_contatos = len(summary)
    total_mensagens = sum(c["message_count"] for c in summary)
    total_midias = sum(c["media_count"] for c in summary)

    rows_html = ""
    for c in summary:
        phone = c.get("phone_number") or "-"
        name = c.get("contact_name") or "-"
        course = c["course"]
        pill_class = COURSE_PILL_CLASS.get(course, "neutral")
        search_blob = html.escape(f"{phone} {name}".lower())
        rows_html += f"""
        <tr data-search="{search_blob}">
            <td class="numero">{html.escape(phone)}</td>
            <td>{html.escape(name)}</td>
            <td><span class="bs-pill {pill_class}">{COURSE_LABELS.get(course, course)}</span></td>
            <td>{c["message_count"]}</td>
            <td>{c["media_count"]}</td>
            <td>{html.escape(c.get("contact_created_at") or "-")}</td>
            <td>{html.escape(c.get("last_message_date") or "-")}</td>
            <td><a class="bs-download-btn" href="/dashboard/download/{course}/{c['contact_id']}"><i class="ti ti-download"></i> Baixar</a></td>
        </tr>
        """

    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Backups Unnichat</title>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.31.0/dist/tabler-icons.min.css">
        <style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

:root {{
  --ink: #1f2330; --ink-muted: #6b7280; --ink-subtle: #9ca3af;
  --bg: #f1f3f5; --surface: #f6f7fb; --card: #ffffff;
  --border: #e5e7eb; --border-s: #d1d5db;
  --accent: #374151; --accent-bg: #f3f4f6;
  --success: #31c16c; --success-bg: #f0fdf4;
  --warning: #f4b740; --warning-bg: #fffbeb;
  --danger: #f05454; --danger-bg: #fef2f2;
  --info: #2f5ee3; --info-bg: #eef2ff;
  --r-md: 8px; --r-lg: 12px; --r-xl: 16px;
  --sh-sm: 0 1px 3px rgba(20,26,52,.08);
  --sh-md: 0 4px 12px rgba(20,26,52,.10);
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; font-family:'Inter','Segoe UI',system-ui,sans-serif; background:var(--bg); color:var(--ink); }}
.hdr {{ background:#fff; padding:20px 32px; border-bottom:1px solid var(--border); box-shadow:var(--sh-sm); }}
.hdr h1 {{ font-size:20px; font-weight:800; margin:0 0 2px; }}
.hdr p {{ font-size:13px; color:var(--ink-muted); margin:0; }}
.content {{ padding:28px 32px; }}

.kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-bottom:24px; }}
.kpi {{ background:var(--card); border-radius:var(--r-lg); padding:16px 18px; box-shadow:var(--sh-sm); }}
.kpi-label {{ font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.06em; color:var(--ink-muted); }}
.kpi-value {{ font-size:26px; font-weight:800; margin-top:4px; }}

.info-box {{ background:var(--card); border-radius:var(--r-lg); box-shadow:var(--sh-sm); padding:20px; }}

.filter-wrap {{ position:relative; display:inline-flex; align-items:center; margin-bottom:16px; }}
.filter-wrap .ti-search {{ position:absolute; left:12px; color:var(--ink-subtle); font-size:14px; pointer-events:none; }}
.filter-input {{ padding:9px 14px 9px 34px; font-size:13px; border:1px solid var(--border-s); border-radius:var(--r-md); outline:none; width:280px; font-family:inherit; color:var(--ink); background:#fff; }}
.filter-input:focus {{ border-color:var(--accent); }}

.table-wrap {{ border:1px solid var(--border); border-radius:var(--r-lg); overflow:hidden; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ background:var(--accent); color:#fff; padding:10px 12px; text-align:left; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em; }}
td {{ padding:10px 12px; border-bottom:1px solid var(--border); vertical-align:middle; }}
tr:last-child td {{ border-bottom:none; }}
tbody tr:nth-child(odd) td {{ background:#f9fafb; }}
tr:hover td {{ background:var(--accent-bg) !important; }}
.numero {{ font-family:'Courier New',monospace; }}

.bs-pill {{ display:inline-block; padding:2px 10px; border-radius:999px; font-size:11px; font-weight:700; color:#fff; white-space:nowrap; }}
.bs-pill.info {{ background:var(--info); }}
.bs-pill.warning {{ background:var(--warning); color:#1a1a2e; }}
.bs-pill.danger {{ background:var(--danger); }}
.bs-pill.neutral {{ background:var(--border-s); color:var(--ink); }}

.bs-download-btn {{ display:inline-flex; align-items:center; gap:5px; font-size:12px; font-weight:700; color:var(--accent); text-decoration:none; padding:5px 10px; border:1px solid var(--border-s); border-radius:999px; transition:background .12s; }}
.bs-download-btn:hover {{ background:var(--accent-bg); }}

.empty {{ padding:8px; color:var(--ink-muted); font-size:13px; }}
        </style>
    </head>
    <body>
        <div class="hdr">
            <h1>Backups Unnichat</h1>
            <p>Conversas arquivadas por tag de backup (INSS / TJ / BB)</p>
        </div>
        <div class="content">
            <div class="kpi-grid">
                <div class="kpi"><div class="kpi-label">Contatos arquivados</div><div class="kpi-value">{total_contatos}</div></div>
                <div class="kpi"><div class="kpi-label">Mensagens</div><div class="kpi-value">{total_mensagens}</div></div>
                <div class="kpi"><div class="kpi-label">Mídias (áudio/vídeo/imagem)</div><div class="kpi-value">{total_midias}</div></div>
            </div>

            <div class="info-box">
                <div class="filter-wrap">
                    <i class="ti ti-search"></i>
                    <input class="filter-input" id="search" type="text" placeholder="Buscar por número ou nome..." oninput="filterRows()">
                </div>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Telefone</th><th>Nome</th><th>Curso</th>
                                <th>Mensagens</th><th>Mídias</th>
                                <th>Contato criado em</th><th>Última mensagem</th><th></th>
                            </tr>
                        </thead>
                        <tbody id="rows">
                            {rows_html or '<tr><td colspan="8" class="empty">Nenhum backup ainda.</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        <script>
        function filterRows() {{
            const q = document.getElementById('search').value.toLowerCase().trim();
            document.querySelectorAll('#rows tr[data-search]').forEach(function(row) {{
                row.style.display = row.dataset.search.includes(q) ? '' : 'none';
            }});
        }}
        </script>
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
