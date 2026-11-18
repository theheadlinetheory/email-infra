-- Cold-calling contact@ orders (cold_call_inbox.py) finish on their own.
-- GitHub fires cold-call-inbox.yml's */15 schedule only every ~8 hours, so an
-- order took days. pg_cron calls the advance route every 10 minutes instead,
-- authenticated by a token both sides read from `state` (anon cannot).

insert into public.state (key, data, updated_at)
values ('cold_call_cron_token',
        to_jsonb(json_build_object('token',
          replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', ''))::text),
        now())
on conflict (key) do nothing;

select cron.unschedule('cold-call-inbox-advance')
where exists (select 1 from cron.job where jobname = 'cold-call-inbox-advance');

select cron.schedule('cold-call-inbox-advance', '*/10 * * * *', $job$
  select net.http_post(
    url := 'https://email-infra.vercel.app/api/cold-call-inbox/advance',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'X-Cron-Token', (select ((data #>> '{}')::jsonb) ->> 'token'
                       from public.state where key = 'cold_call_cron_token')),
    body := '{}'::jsonb,
    timeout_milliseconds := 295000);
$job$);
