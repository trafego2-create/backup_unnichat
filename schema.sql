-- Rodar no SQL editor do Supabase antes de usar o script.

create table if not exists unnichat_audio_backups (
    message_id text primary key,
    contact_id text not null,
    course text not null,          -- 'inss' | 'tj' | 'bb'
    sender_by text not null,       -- 'user' (atendente) ou 'contact' (cliente)
    message_date timestamptz not null,
    storage_path text not null,
    original_url text not null,
    backed_up_at timestamptz not null default now()
);

create index if not exists idx_unnichat_audio_backups_contact
    on unnichat_audio_backups (contact_id);

-- Criar o bucket "unnichat-audios" em Storage > New bucket (privado, sem acesso público).
