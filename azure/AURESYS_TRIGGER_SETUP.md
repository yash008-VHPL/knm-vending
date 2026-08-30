# Guaranteed 06:00 / 18:00 SGT trigger for the Auresys pull

## Why this exists

GitHub Actions `schedule:` is best-effort. Measured on this repo: runs landed
0.4–2.2h after their cron time, and were dropped entirely on 2026-08-28 and
2026-08-30 — which is how a 13-hour gap opened between pulls while every run
that did happen reported SUCCESS.

The cron entries in `auresys-daily.yml` are kept as a **backstop**. This Logic
App is the **primary** trigger: it fires on time, keeps a run history, and can
raise an Azure alert when a run fails.

## What it does

A Consumption Logic App with a Recurrence trigger at 06:00 and 18:00
`Singapore Standard Time` (the timezone is set on the trigger, so DST changes
elsewhere and UTC drift are not a concern). It POSTs to the GitHub
`workflow_dispatch` API for `auresys-daily.yml` on `main`.

**The dispatch body sets `include_today: "true"`, and that is not optional.**
`workflow_dispatch` does not populate `github.event.schedule`, so the workflow
does not add `--include-today` by itself. Without it the pull loads only
through D-1 and the current day never appears — the exact failure that made the
vend counter read 0 on 2026-08-28.

It also sends `days: "10"` and `dry_run: "false"`, matching what the scheduled
runs did.

Retries: exponential, 4 attempts, 1–10 minutes apart. Note Logic Apps only
retries 408/429/5xx — an expired token returning 401 fails on the first
attempt, which is what you want. If GitHub returns anything other than 204 the
run is marked Failed so it shows red in the run history.

`startTime` is set to 2026-09-01T06:00:00 local. Without it a Recurrence
trigger fires once the moment it is deployed; with it the first run is the
first scheduled slot on or after that date.

## Setup

### 1. Mint a GitHub fine-grained PAT

GitHub → Settings → Developer settings → Personal access tokens → Fine-grained.

- Resource owner: `yash008-VHPL`
- Repository access: **Only select repositories** → `knm-vending`
- Permissions: **Actions → Read and write**. Nothing else.
- Expiry: set one, and put the renewal date in the calendar. An expired token
  fails silently apart from the Logic App run going red — which is why step 3
  matters.

Note what this token can do: dispatch any workflow in that repo, and those
workflows hold the database credentials. Treat it as a production secret.

### 2. Deploy the Logic App

```bash
az deployment group create \
  --resource-group <your-resource-group> \
  --template-file azure/logicapp-auresys-trigger.json \
  --parameters githubToken=<the-PAT>
```

Or: Azure Portal → Create a resource → Template deployment → build your own
template → paste `logicapp-auresys-trigger.json`.

Two separate exposures, both closed:

- The parameter is a `securestring`, so it is not readable back from the
  deployment history.
- A securestring is **not** automatically redacted where it is consumed. The
  resolved `Authorization: Bearer …` header would otherwise appear in the
  action's inputs in run history, readable by anyone with read access to the
  Logic App. The template sets `runtimeConfiguration.secureData` on the
  dispatch action to suppress that. Do not remove it.

If you would rather the token lived in Key Vault, reference it from there
instead — the template takes the value directly to keep first setup to one
step.

### 3. Alert on failure — do not skip this

The point of moving off GitHub cron is knowing when a trigger does not fire.

Logic App → Alerts → New alert rule → signal **Runs Failed** → threshold
greater than 0 over 1 hour → action group with your email or Teams webhook.

Be clear about what this covers: it fires when the **dispatch** fails. It
cannot see GitHub accepting the dispatch and then not running the job, and it
cannot see the pull itself failing. Those two are covered by `HEARTBEAT_URL`
and the Teams `notify()` in `auresys_pull.py` — all three are needed for the
chain to be observable end to end.

### 4. Verify

- Logic App → Run Trigger → Run. It should complete in seconds.
- GitHub → Actions → a new **Auresys Transaction Pull** run appears, and its
  log line reads `auresys_pull.py --days 10 --include-today`.
- Confirm `--include-today` is present. If it is missing, the dispatch body did
  not carry the input and today's data will not load.

## After it is running

Watch for a week. Both the Logic App and the GitHub crons will fire, so expect
up to four runs a day in the Actions history. The duplicates are cheap but not
free: each one is a full Auresys login plus ten days of API fetches and one
`NETS_Pull_Run` row. What they do not do is rewrite data — `NO_CHANGE` writes
nothing when a machine-day is unchanged.

The backstop crons are at **:30** past the hour, half an hour behind the Logic
App, deliberately. At the same instant one of the two would be
concurrency-cancelled every time, and a cancelled run in the Actions history
reads as a failure to whoever looks at it.

If you later want the duplicates gone, remove the `schedule:` block from
`auresys-daily.yml` — but only once the Logic App has a clean track record and
step 3's alert is confirmed working.
