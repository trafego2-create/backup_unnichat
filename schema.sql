-- Rodar no SQL editor do Supabase.
-- Evolui a tabela de "só áudio" pra "toda mensagem" (texto inline, mídia no Storage).

alter table unnichat_audio_backups rename to unnichat_message_backups;

alter table unnichat_message_backups
    add column if not exists message_type text,
    add column if not exists text_content text;

alter table unnichat_message_backups
    alter column storage_path drop not null,
    alter column original_url drop not null;

update unnichat_message_backups set message_type = 'audio' where message_type is null;

alter table unnichat_message_backups
    alter column message_type set not null;

-- Bucket "unnichat-audios" continua sendo usado pra qualquer mídia (áudio, vídeo, imagem, documento).

-- Adiciona telefone/nome pra identificar a pessoa sem precisar do contact_id (UUID interno).
alter table unnichat_message_backups
    add column if not exists phone_number text,
    add column if not exists contact_name text;

create index if not exists idx_unnichat_message_backups_phone
    on unnichat_message_backups (phone_number);

-- Data de criação do contato na Unnichat (vem como texto, ex: "07/08/2026, 18:08:08" —
-- guardado como texto pra não dar erro de parsing com formato nao-ISO).
alter table unnichat_message_backups
    add column if not exists contact_created_at text;

-- View pro dashboard: uma linha por pessoa/curso já arquivado.
create or replace view contact_backup_summary as
select
    contact_id,
    course,
    max(phone_number) as phone_number,
    max(contact_name) as contact_name,
    max(contact_created_at) as contact_created_at,
    count(*) as message_count,
    count(*) filter (where storage_path is not null) as media_count,
    max(message_date) as last_message_date
from unnichat_message_backups
group by contact_id, course;
