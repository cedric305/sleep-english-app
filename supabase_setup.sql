create table if not exists public.library_items (
  id text primary key,
  video_id text not null,
  video_title text not null default '',
  start double precision not null,
  "end" double precision not null,
  text text not null,
  created_at timestamptz not null default timezone('utc', now()),
  audio_path text not null
);

create index if not exists library_items_created_at_idx
  on public.library_items (created_at desc);

create table if not exists public.word_items (
  id text primary key,
  word text not null,
  word_normalized text not null unique,
  translation text not null,
  note text not null default '',
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

create index if not exists word_items_created_at_idx
  on public.word_items (created_at desc);

insert into storage.buckets (id, name, public)
values ('clips', 'clips', true)
on conflict (id) do nothing;
