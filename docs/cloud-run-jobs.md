# Rendering on Cloud Run Jobs

## Why

The web service used to render videos in background threads. That forced
`cpu-throttling: false` ("CPU always allocated"), so 4 vCPU + 8 GiB were
billed every second the container was alive — about **$0.42/hour, ~$305/month**
— whether or not anything was rendering.

Splitting rendering into a Cloud Run Job means:

- the **service** runs request-only, scales to zero, costs ~nothing idle
- the **job** starts when work is queued, renders, and exits — you pay for
  render seconds only, roughly **$0.02 per video**

Nothing here changes local or Docker use. With `MPT_RENDER_MODE` unset the
app behaves exactly as before, rendering in-process.

## Moving parts

| Piece | What it does |
|---|---|
| `render_worker.py` | Job entrypoint. Claims pending jobs, renders, exits. |
| `app/services/render_dispatch.py` | Service side. Starts a Job execution when a video is queued. |
| `MPT_RENDER_MODE=cloudrun_job` | The switch. Unset = old in-process behaviour. |

The worker takes no job id — it claims work through the same atomic
transaction the old engine used, so overlapping executions can't collide and
an execution that finds an empty queue just exits.

## Deploy

Values below match the existing service (project `vvvvv-504116`, region
`us-central1`, bucket `vvvvv-504116-mpt-data`).

**1. Create the job from the image the service already runs**

```bash
IMAGE=$(gcloud run services describe moneyprinterturbo --region us-central1 --format='value(spec.template.spec.containers[0].image)')

gcloud run jobs create moneyprinterturbo-render \
  --image "$IMAGE" \
  --region us-central1 \
  --command python --args render_worker.py \
  --cpu 4 --memory 8Gi \
  --task-timeout 3600 --max-retries 1 \
  --service-account 658768852488-compute@developer.gserviceaccount.com \
  --set-env-vars MPT_STORAGE_DIR=/data/storage,MPT_CONFIG_FILE=/data/config.toml,GOOGLE_CLOUD_PROJECT=vvvvv-504116 \
  --add-volume name=mpt-data,type=cloud-storage,bucket=vvvvv-504116-mpt-data \
  --add-volume-mount volume=mpt-data,mount-path=/data
```

**2. Let the service start executions**

```bash
gcloud run jobs add-iam-policy-binding moneyprinterturbo-render \
  --region us-central1 \
  --member serviceAccount:658768852488-compute@developer.gserviceaccount.com \
  --role roles/run.invoker
```

**3. Switch the service over and shrink it**

```bash
gcloud run services update moneyprinterturbo \
  --region us-central1 \
  --update-env-vars MPT_RENDER_MODE=cloudrun_job,MPT_RENDER_JOB=moneyprinterturbo-render,MPT_RENDER_JOB_REGION=us-central1 \
  --cpu 1 --memory 4Gi --cpu-throttling
```

This is the line that stops the ~$305/month. 4 GiB was chosen over 2 GiB
because the API imports the full render pipeline at startup; throttled CPU is
billed per request-second, so the larger ceiling costs almost nothing.

**4. Auto Mode**

With no always-on engine, nothing sits in a loop generating Auto Mode videos.
Cloud Scheduler triggers it instead — hourly here:

```bash
gcloud scheduler jobs create http mpt-auto-mode \
  --location us-central1 \
  --schedule "0 * * * *" \
  --uri "https://run.googleapis.com/v2/projects/vvvvv-504116/locations/us-central1/jobs/moneyprinterturbo-render:run" \
  --http-method POST \
  --oauth-service-account-email 658768852488-compute@developer.gserviceaccount.com \
  --message-body '{"overrides":{"containerOverrides":[{"env":[{"name":"MPT_WORKER_AUTO","value":"1"}]}]}}'
```

Cloud Scheduler gives 3 free jobs/month, so this is free.

## Verifying

```bash
gcloud run jobs executions list --job moneyprinterturbo-render --region us-central1 --limit 5
gcloud run jobs executions logs read EXECUTION_NAME --region us-central1
```

Queue a video in the UI and an execution should appear within seconds. If the
service log says `could not reach Cloud Run to start a render job`, step 2's
IAM binding hasn't propagated — dispatch falls back to the in-process engine
in the meantime, so nothing is lost.

## Rolling back

```bash
gcloud run services update moneyprinterturbo --region us-central1 \
  --remove-env-vars MPT_RENDER_MODE --cpu 4 --memory 8Gi --no-cpu-throttling
```

The old in-process engine starts again on the next revision.

## Deployed

Live since revision `moneyprinterturbo-00040-66q`:

- service: 1 vCPU / 4 GiB, `cpu-throttling: true`, `MPT_RENDER_MODE=cloudrun_job`
- job: `moneyprinterturbo-render`, 4 vCPU / 8 GiB, task timeout 3600s
- scheduler: `mpt-auto-mode`, hourly at :00 UTC

Verified: the service logs `render mode: cloud run jobs (no in-process
workers)` at startup, and a manual execution of the job logged `render worker
job-dda33a78 starting` / `rendered 0 job(s) in 0s` against an empty queue.

Still unexercised: the dispatch call itself (service -> job) only fires when a
signed-in user queues a video, which needs a real login to test.

## Gotcha: Git Bash mangles the mount path

On Windows, running these commands in Git Bash rewrites `/data` into a
Windows path, and job creation fails with "should be a valid unix absolute
path". That is also where the existing service's odd `//data/storage` comes
from. Use PowerShell for anything containing an absolute container path, or
prefix with `MSYS_NO_PATHCONV=1`.
