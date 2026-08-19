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
