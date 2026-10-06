-- WorkOutBuddy Web / Supabase schema v1
-- Run this whole file once in Supabase -> SQL Editor.
-- It is intentionally rerunnable.

create extension if not exists pgcrypto;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null default '',
  birthdate date,
  gender text,
  height_cm double precision,
  current_weight_kg double precision,
  profile_vo2max double precision,
  hr_zones jsonb not null default '{"z1_max":130,"z2_max":150,"z3_max":165,"z4_max":178,"z5_max":220}'::jsonb,
  goals jsonb not null default '{"primary_goal":"general","goal_date":null}'::jsonb,
  training_preferences jsonb not null default '{"weekly_run_days":3,"strength_sessions":2,"long_run_day":6}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.activities (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  source text not null default 'manual',
  source_external_id text,
  name text not null default 'Workout',
  sport_type text,
  sport_category text not null default 'other',
  start_time timestamptz not null,
  original_start_time text,
  local_timezone text not null default 'Europe/Vienna',
  duration_s double precision,
  moving_time_s double precision,
  elapsed_time_s double precision,
  distance_m double precision,
  elevation_gain_m double precision,
  elevation_loss_m double precision,
  avg_hr double precision,
  max_hr double precision,
  avg_pace_min_km double precision,
  avg_gap_pace_min_km double precision,
  training_load_score double precision,
  trimp_score double precision,
  easy_zone_fraction double precision,
  hard_zone_fraction double precision,
  z1_s double precision,
  z2_s double precision,
  z3_s double precision,
  z4_s double precision,
  z5_s double precision,
  apple_vo2max double precision,
  own_vo2max_estimate double precision,
  estimated_vo2max double precision,
  body_weight_kg double precision,
  normalized_power double precision,
  average_power double precision,
  cadence_spm double precision,
  hr_efficiency_drift_pct double precision,
  gap_hr_efficiency_drift_pct double precision,
  km_gap_hr_efficiency_drift_pct double precision,
  file_sha256 text,
  dedupe_key text,
  raw_file_path text,
  metrics_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint activities_user_file_hash_unique unique (user_id, file_sha256)
);

create table if not exists public.activity_streams (
  activity_id uuid primary key references public.activities(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  stream_data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.training_plans (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  start_date date not null,
  engine_version text not null,
  plan jsonb not null,
  created_at timestamptz not null default now()
);

create table if not exists public.analysis_snapshots (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  deterministic jsonb not null,
  llm_markdown text,
  model text,
  created_at timestamptz not null default now()
);

create index if not exists activities_user_start_idx on public.activities(user_id,start_time desc);
create index if not exists activities_user_sport_idx on public.activities(user_id,sport_category,start_time desc);
create index if not exists activities_user_dedupe_idx on public.activities(user_id,dedupe_key);
create index if not exists training_plans_user_start_idx on public.training_plans(user_id,start_date desc);
create index if not exists analysis_snapshots_user_created_idx on public.analysis_snapshots(user_id,created_at desc);

drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at before update on public.profiles
for each row execute function public.set_updated_at();

drop trigger if exists activities_set_updated_at on public.activities;
create trigger activities_set_updated_at before update on public.activities
for each row execute function public.set_updated_at();

drop trigger if exists activity_streams_set_updated_at on public.activity_streams;
create trigger activity_streams_set_updated_at before update on public.activity_streams
for each row execute function public.set_updated_at();

alter table public.profiles enable row level security;
alter table public.activities enable row level security;
alter table public.activity_streams enable row level security;
alter table public.training_plans enable row level security;
alter table public.analysis_snapshots enable row level security;

drop policy if exists "profiles own rows" on public.profiles;
create policy "profiles own rows" on public.profiles
for all to authenticated
using (user_id = (select auth.uid()))
with check (user_id = (select auth.uid()));

drop policy if exists "activities own rows" on public.activities;
create policy "activities own rows" on public.activities
for all to authenticated
using (user_id = (select auth.uid()))
with check (user_id = (select auth.uid()));

drop policy if exists "streams own rows" on public.activity_streams;
create policy "streams own rows" on public.activity_streams
for all to authenticated
using (user_id = (select auth.uid()))
with check (user_id = (select auth.uid()));

drop policy if exists "plans own rows" on public.training_plans;
create policy "plans own rows" on public.training_plans
for all to authenticated
using (user_id = (select auth.uid()))
with check (user_id = (select auth.uid()));

drop policy if exists "analysis own rows" on public.analysis_snapshots;
create policy "analysis own rows" on public.analysis_snapshots
for all to authenticated
using (user_id = (select auth.uid()))
with check (user_id = (select auth.uid()));

grant select,insert,update,delete on public.profiles to authenticated;
grant select,insert,update,delete on public.activities to authenticated;
grant select,insert,update,delete on public.activity_streams to authenticated;
grant select,insert,update,delete on public.training_plans to authenticated;
grant select,insert,update,delete on public.analysis_snapshots to authenticated;

-- Private raw workout-file bucket. The first path component must be the signed-in user's UUID.
insert into storage.buckets (id,name,public,file_size_limit,allowed_mime_types)
values (
  'workout-files',
  'workout-files',
  false,
  26214400,
  array['application/xml','text/xml','application/vnd.garmin.tcx+xml','application/octet-stream']
)
on conflict (id) do update
set public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "workout files select own" on storage.objects;
create policy "workout files select own"
on storage.objects for select to authenticated
using (
  bucket_id = 'workout-files'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);

drop policy if exists "workout files insert own" on storage.objects;
create policy "workout files insert own"
on storage.objects for insert to authenticated
with check (
  bucket_id = 'workout-files'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);

drop policy if exists "workout files update own" on storage.objects;
create policy "workout files update own"
on storage.objects for update to authenticated
using (
  bucket_id = 'workout-files'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
)
with check (
  bucket_id = 'workout-files'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);

drop policy if exists "workout files delete own" on storage.objects;
create policy "workout files delete own"
on storage.objects for delete to authenticated
using (
  bucket_id = 'workout-files'
  and (storage.foldername(name))[1] = (select auth.uid()::text)
);
