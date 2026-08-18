"""
Backup de áudios trocados na Unnichat (atendentes <-> clientes) para o Supabase Storage.

Como funciona:
1. Lê uma lista de contact IDs de um CSV (a API da Unnichat não expõe endpoint de
   listagem de contatos, então essa lista precisa vir de um export do painel).
2. Para cada contato, chama GET /contact/{id}/messages e filtra as mensagens
   com type == "audio".
3. Baixa o áudio da URL retornada (Firebase Storage) e sobe para um bucket no
   Supabase Storage, registrando os metadados numa tabela Postgres.

Uso:
    python backup_audios.py contacts.csv

O CSV precisa ter uma coluna chamada "id" com o contactId da Unnichat.
"""

import csv
import os
import sys
import time

import requests
from supabase import create_client

UNNICHAT_API_TOKEN = os.environ["UNNICHAT_API_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "unnichat-audios")

API_BASE = "https://unnichat.com.br/api"
HEADERS = {"Authorization": f"Bearer {UNNICHAT_API_TOKEN}"}

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def fetch_messages(contact_id: str) -> list[dict]:
    resp = requests.get(f"{API_BASE}/contact/{contact_id}/messages", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"]


def already_backed_up(message_id: str) -> bool:
    result = (
        supabase.table("unnichat_audio_backups")
        .select("message_id")
        .eq("message_id", message_id)
        .execute()
    )
    return len(result.data) > 0


def backup_audio_message(contact_id: str, msg: dict) -> None:
    message_id = msg["id"]
    if already_backed_up(message_id):
        return

    audio_resp = requests.get(msg["url"], timeout=60)
    audio_resp.raise_for_status()
    audio_bytes = audio_resp.content

    ext = msg["url"].split("?")[0].rsplit(".", 1)[-1]
    storage_path = f"{contact_id}/{message_id}.{ext}"

    supabase.storage.from_(SUPABASE_BUCKET).upload(
        storage_path,
        audio_bytes,
        {"content-type": f"audio/{ext}"},
    )

    supabase.table("unnichat_audio_backups").insert(
        {
            "message_id": message_id,
            "contact_id": contact_id,
            "sender_by": msg["senderBy"],  # "user" (atendente) ou "contact" (cliente)
            "message_date": msg["date"],
            "storage_path": storage_path,
            "original_url": msg["url"],
        }
    ).execute()

    print(f"  backed up {message_id} ({msg['senderBy']}, {msg['date']})")


def backup_contact(contact_id: str) -> None:
    print(f"contact {contact_id}")
    messages = fetch_messages(contact_id)
    audio_messages = [m for m in messages if m.get("type") == "audio"]
    for msg in audio_messages:
        try:
            backup_audio_message(contact_id, msg)
        except Exception as exc:
            print(f"  FAILED {msg['id']}: {exc}")


def main(csv_path: str) -> None:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        contact_ids = [row["id"] for row in reader]

    print(f"{len(contact_ids)} contatos a processar")
    for contact_id in contact_ids:
        backup_contact(contact_id)
        time.sleep(0.3)  # evita bater rate limit da API


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("uso: python backup_audios.py contacts.csv")
        sys.exit(1)
    main(sys.argv[1])
