# Cloud Deploy

## Recommended free setup

- Frontend + Flask API: Render Free Web Service
- Data + audio storage: Supabase Free

This app now supports two modes:

- Local mode: stores data in `data/`
- Supabase mode: stores library items and words in Supabase tables, and mp3 clips in a Supabase Storage bucket

Supabase mode is enabled when both of these environment variables are set:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## 1. Prepare Supabase

1. Create a Supabase project.
2. Open the SQL editor.
3. Run the SQL in `supabase_setup.sql`.
4. In project settings, copy:
   - Project URL
   - service role key

## 2. Deploy on Render Free

1. Create a new Web Service from this GitHub repo.
2. Keep Docker as the runtime.
3. Use branch `main`.
4. Region: choose the closest region, such as Singapore.
5. Instance type: `Free`.
6. Add environment variables:
   - `SUPABASE_URL=<your supabase project url>`
   - `SUPABASE_SERVICE_ROLE_KEY=<your service role key>`
   - `SUPABASE_BUCKET=clips`
7. Deploy.

## Notes

- In Supabase mode, Render does not need a persistent disk.
- The audio bucket created by `supabase_setup.sql` is public so the app can stream saved mp3 clips directly.
- The service role key must stay server-side only. Do not put it into frontend JavaScript.
