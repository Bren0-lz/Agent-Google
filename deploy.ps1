#Requires -Version 5.1
<#
.SYNOPSIS
    Deploys the Invoice Sentinel agent to Google Cloud Run.

.DESCRIPTION
    Single source of truth for deploying this project. Also serves as the
    spin-up instruction for the hackathon submission: clone the repo, run
    `gcloud auth login`, then run this script.

    Region rule (do not "fix" this):
      * Infrastructure (Cloud Run, Firestore, GCS, Pub/Sub) lives in us-central1.
      * Gemini API calls use the `global` endpoint, because the latest Gemini 3.x
        models are only served there. GOOGLE_CLOUD_LOCATION=global is deliberate.

    Environment variables are set explicitly here because `adk deploy cloud_run`
    does NOT ship the local .env into the container. They are written to a
    temporary YAML file and passed via --env-vars-file instead of
    --set-env-vars, because PowerShell mangles comma-separated flag values into
    a single giant value of the first variable.

.EXAMPLE
    .\deploy.ps1
    Deploys with the defaults below.

.EXAMPLE
    .\deploy.ps1 -EnableApis
    First-time setup on a fresh project: enables required APIs, then deploys.

.EXAMPLE
    .\deploy.ps1 -NoUi
    Deploys the ADK API server only, without the developer web UI.

.EXAMPLE
    .\deploy.ps1 -MinInstances 0
    Deploys scaling to zero. Cheaper to leave running, at the cost of a ~16s
    cold start on the first request after an idle period.
#>
[CmdletBinding()]
param(
    [string] $ProjectId   = 'agent-hackton',
    [string] $Region      = 'us-central1',
    [string] $ServiceName = 'invoice-sentinel',
    [string] $AgentDir    = 'invoice_sentinel',

    # Gemini endpoint. Must stay 'global' for Gemini 3.x. See region rule above.
    [string] $ModelLocation = 'global',

    # Bucket the gs:// source_uri path reads from. Uploaded PDFs never land
    # here - or anywhere: intake reads the attachment from the message and the
    # bytes are gone with the request. This holds the documents someone chose
    # to stage, which today is the sample the README's curl example audits.
    [string] $Bucket = 'agent-hackton-invoices-raw',

    # Bucket holding the dispute letters and customer summaries the agent
    # attaches to a session. Deliberately not $Bucket: one holds documents the
    # carrier issued, the other holds documents written on the customer's
    # behalf, and they do not belong under the same retention or access story.
    # Empty string falls back to the ADK's in-memory service, which loses the
    # attachments whenever the container is recycled.
    [string] $ArtifactBucket = 'agent-hackton-artifacts',

    # Deploy the ADK API server only (no developer web UI).
    [switch] $NoUi,

    # Warm instances kept running. The default of 1 costs one idle Cloud Run
    # instance around the clock and buys back the ~16s cold start, which is the
    # first thing anyone opening the demo would otherwise sit through. Pass 0 to
    # scale to zero between uses.
    [int] $MinInstances = 1,

    # Cost ceiling, not a performance setting. The service runs
    # --allow-unauthenticated, so these two numbers bound what an abusive
    # visitor can turn into billed Gemini calls: at most
    # MaxInstances * Concurrency requests can be in flight at once. They live
    # here, and not only on the deployed service, because a later run of this
    # script would otherwise put the old unbounded values back.
    [int] $MaxInstances = 2,
    [int] $Concurrency  = 8,

    # Runtime identity. Deliberately not the default Compute service account,
    # which carries roles/editor over the whole project: a public endpoint that
    # can rewrite its own infrastructure is a much larger blast radius than the
    # job needs. Created by -EnableApis with the four roles the agent actually
    # uses. Pass '' to fall back to the project default.
    [string] $ServiceAccount = 'invoice-sentinel-run',

    # One-time: enable the Google Cloud APIs this project depends on.
    [switch] $EnableApis
)

$ErrorActionPreference = 'Stop'
$SaEmail = if ($ServiceAccount) { "$ServiceAccount@$ProjectId.iam.gserviceaccount.com" } else { '' }
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

function Write-Step($Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

# --- Preflight ---------------------------------------------------------------

Write-Step 'Checking prerequisites'

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw 'gcloud CLI not found on PATH. Install the Google Cloud SDK and run `gcloud auth login`.'
}

# Prefer the venv-local adk so the deployed adk_version matches the dev environment.
$VenvAdk = Join-Path $RepoRoot '.venv\Scripts\adk.exe'
if (Test-Path $VenvAdk) {
    $Adk = $VenvAdk
} elseif (Get-Command adk -ErrorAction SilentlyContinue) {
    $Adk = 'adk'
} else {
    throw 'adk CLI not found. Create the venv and run: pip install google-adk'
}

$AgentPath = Join-Path $RepoRoot $AgentDir
if (-not (Test-Path (Join-Path $AgentPath 'agent.py'))) {
    throw "Agent source not found at $AgentPath (expected agent.py)."
}

Write-Host "    gcloud  : $((Get-Command gcloud).Source)"
Write-Host "    adk     : $Adk ($(& $Adk --version))"
Write-Host "    project : $ProjectId"
Write-Host "    region  : $Region (Gemini endpoint: $ModelLocation)"
Write-Host "    service : $ServiceName"
Write-Host "    scaling : $MinInstances warm, at most $MaxInstances instance(s) x $Concurrency concurrent request(s)"
Write-Host "    runs as : $(if ($SaEmail) { $SaEmail } else { 'project default (roles/editor - not recommended)' })"

gcloud config set project $ProjectId --quiet
if ($LASTEXITCODE -ne 0) { throw "Failed to select project '$ProjectId'. Are you authenticated?" }

# --- Optional one-time API enablement ---------------------------------------

if ($EnableApis) {
    Write-Step 'Enabling required Google Cloud APIs (one-time, slow)'
    $Apis = @(
        'aiplatform.googleapis.com'
        'run.googleapis.com'
        'firestore.googleapis.com'
        'pubsub.googleapis.com'
        'storage.googleapis.com'
        'secretmanager.googleapis.com'
        'cloudbuild.googleapis.com'
        'artifactregistry.googleapis.com'
    )
    foreach ($Api in $Apis) {
        Write-Host "    enabling $Api"
        gcloud services enable $Api --project $ProjectId --quiet
        if ($LASTEXITCODE -ne 0) { throw "Failed to enable $Api" }
    }

    # The raw-invoice bucket. Uniform access because per-object ACLs are a
    # liability on a bucket holding customer billing documents.
    Write-Step "Creating the raw invoice bucket (skipped if it exists)"
    $Existing = gcloud storage buckets list --project $ProjectId --format 'value(name)'
    if ($Existing -notcontains $Bucket) {
        gcloud storage buckets create "gs://$Bucket" --project $ProjectId `
            --location $Region --uniform-bucket-level-access `
            --public-access-prevention
        if ($LASTEXITCODE -ne 0) { throw "Failed to create gs://$Bucket" }
    } else {
        Write-Host "    gs://$Bucket already exists"
    }

    # The artifact bucket, on the same terms. Without it the letter and the
    # summary live only in the container's memory and vanish on the next
    # revision, which makes the Artifacts tab lie about what is on file.
    if ($ArtifactBucket) {
        Write-Step "Creating the artifact bucket (skipped if it exists)"
        if ($Existing -notcontains $ArtifactBucket) {
            gcloud storage buckets create "gs://$ArtifactBucket" --project $ProjectId `
                --location $Region --uniform-bucket-level-access `
                --public-access-prevention
            if ($LASTEXITCODE -ne 0) { throw "Failed to create gs://$ArtifactBucket" }
        } else {
            Write-Host "    gs://$ArtifactBucket already exists"
        }
    }

    # The runtime identity, with the narrowest set of roles that still lets the
    # agent do its job. Everything here is write-scoped to what it names:
    # Firestore for the audit trail, Vertex for Gemini, and object access on the
    # two buckets granted at the bucket, not at the project. logWriter and
    # cloudtrace.agent are what --trace_to_cloud needs; without them the traces
    # fail silently and the deploy still looks healthy.
    if ($ServiceAccount) {
        Write-Step "Creating the runtime service account (skipped if it exists)"
        $ExistingSa = gcloud iam service-accounts list --project $ProjectId --format 'value(email)'
        if ($ExistingSa -notcontains $SaEmail) {
            gcloud iam service-accounts create $ServiceAccount --project $ProjectId `
                --display-name 'Invoice Sentinel runtime' `
                --description 'Cloud Run identity for the invoice-sentinel agent'
            if ($LASTEXITCODE -ne 0) { throw "Failed to create $SaEmail" }
        } else {
            Write-Host "    $SaEmail already exists"
        }

        foreach ($Role in @('roles/datastore.user', 'roles/aiplatform.user',
                            'roles/logging.logWriter', 'roles/cloudtrace.agent')) {
            Write-Host "    granting $Role"
            gcloud projects add-iam-policy-binding $ProjectId `
                --member "serviceAccount:$SaEmail" --role $Role --condition=None --quiet | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Failed to grant $Role to $SaEmail" }
        }

        # Read on the raw bucket, because README's source_uri path downloads
        # from it. Write on the artifact bucket, because the letter and the
        # summary are stored there. Neither grant leaves its bucket.
        Write-Host "    granting objectViewer on gs://$Bucket"
        gcloud storage buckets add-iam-policy-binding "gs://$Bucket" --project $ProjectId `
            --member "serviceAccount:$SaEmail" --role 'roles/storage.objectViewer' --quiet | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to grant read on gs://$Bucket" }
        if ($ArtifactBucket) {
            Write-Host "    granting objectAdmin on gs://$ArtifactBucket"
            gcloud storage buckets add-iam-policy-binding "gs://$ArtifactBucket" --project $ProjectId `
                --member "serviceAccount:$SaEmail" --role 'roles/storage.objectAdmin' --quiet | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Failed to grant write on gs://$ArtifactBucket" }
        }
    }

    # Retention on the artifact bucket. The letter and the summary carry the
    # account number and every disputed amount, and unlike the session that
    # produced them - held in memory, gone on the next revision - they would
    # otherwise live forever. Read from the committed file for the same reason
    # the indexes are: a rule set by hand in the console is not a setup anyone
    # can reproduce.
    if ($ArtifactBucket) {
        Write-Step 'Applying artifact retention'
        $LifecycleFile = Join-Path $RepoRoot 'artifact-lifecycle.json'
        $Days = (Get-Content $LifecycleFile -Raw | ConvertFrom-Json).rule[0].condition.age
        Write-Host "    gs://$ArtifactBucket : delete after $Days days"
        gcloud storage buckets update "gs://$ArtifactBucket" --project $ProjectId `
            --lifecycle-file=$LifecycleFile --quiet | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to set retention on gs://$ArtifactBucket" }
    }

    # Composite indexes, from the committed firestore.indexes.json rather than
    # from the link Firestore prints in an error. An index that only exists
    # because someone clicked a console link is not a reproducible setup - and
    # get_usage_history fails outright without this one.
    Write-Step 'Creating Firestore composite indexes (async, a few minutes)'
    $IndexFile = Join-Path $RepoRoot 'firestore.indexes.json'
    foreach ($Index in (Get-Content $IndexFile -Raw | ConvertFrom-Json).indexes) {
        $FieldArgs = $Index.fields | ForEach-Object {
            "--field-config=field-path=$($_.fieldPath),order=$($_.order.ToLower())"
        }
        Write-Host "    $($Index.collectionGroup): $(($Index.fields | ForEach-Object { $_.fieldPath }) -join ', ')"
        gcloud firestore indexes composite create --project $ProjectId `
            --database='(default)' --collection-group=$Index.collectionGroup `
            @FieldArgs --async --quiet
        # An index that already exists reports failure; that is not an error here.
        if ($LASTEXITCODE -ne 0) { Write-Host '      (already exists or still building)' }
    }
}

# --- Runtime identity check --------------------------------------------------
# Refuse rather than guess: deploying without the account silently falls back to
# the default Compute identity, which holds roles/editor, and nothing in the
# output would say so.

if ($ServiceAccount) {
    gcloud iam service-accounts describe $SaEmail --project $ProjectId --format 'value(email)' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Service account $SaEmail does not exist. Run '.\deploy.ps1 -EnableApis' once to create it, or pass -ServiceAccount '' to accept the project default."
    }
}

# --- Runtime environment -----------------------------------------------------
# The three variables the container cannot start correctly without, plus one
# that decides what a trace is allowed to carry.

$RuntimeEnv = [ordered]@{
    GOOGLE_GENAI_USE_VERTEXAI = 'TRUE'
    GOOGLE_CLOUD_PROJECT      = $ProjectId
    GOOGLE_CLOUD_LOCATION     = $ModelLocation

    # Keep the invoice out of Cloud Trace. --trace_to_cloud is on because
    # latency, token counts and errors are worth having; the transcribed
    # invoice - account number, MSISDNs, employee names - is not, and with
    # this unset the ADK writes the whole llm_request and llm_response onto
    # the span. The ADK sets exactly this value itself when deploying to
    # Agent Engine and forgets to on the Cloud Run path, so we inherit the
    # unsafe default by omission. Same shape as the cost ceiling: state it
    # here or lose it on the next deploy.
    ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS = 'false'
}

$EnvFile = Join-Path ([System.IO.Path]::GetTempPath()) "invoice-sentinel-env-$(Get-Date -Format 'yyyyMMddHHmmss').yaml"
$EnvYaml = ($RuntimeEnv.GetEnumerator() | ForEach-Object { "$($_.Key): `"$($_.Value)`"" }) -join "`n"
Set-Content -Path $EnvFile -Value $EnvYaml -Encoding utf8

Write-Step 'Runtime environment to be set on the service'
$RuntimeEnv.GetEnumerator() | ForEach-Object { Write-Host "    $($_.Key)=$($_.Value)" }

# --- Deploy ------------------------------------------------------------------

Write-Step "Deploying '$ServiceName' to Cloud Run"

# Everything after `--` is forwarded verbatim to `gcloud run deploy`.
$AdkArgs = @(
    'deploy', 'cloud_run'
    "--project=$ProjectId"
    "--region=$Region"
    "--service_name=$ServiceName"   # without this, ADK creates 'adk-default-service-name'
    '--trace_to_cloud'
)
if (-not $NoUi) { $AdkArgs += '--with_ui' }
if ($ArtifactBucket) { $AdkArgs += "--artifact_service_uri=gs://$ArtifactBucket" }
$AdkArgs += @(
    $AgentPath
    '--'
    "--env-vars-file=$EnvFile"
    "--min-instances=$MinInstances"
    "--max-instances=$MaxInstances"
    "--concurrency=$Concurrency"
    '--allow-unauthenticated'
)
if ($SaEmail) { $AdkArgs += "--service-account=$SaEmail" }

try {
    & $Adk @AdkArgs
    if ($LASTEXITCODE -ne 0) { throw "adk deploy failed with exit code $LASTEXITCODE." }
} finally {
    Remove-Item $EnvFile -ErrorAction SilentlyContinue
}

# --- Verify ------------------------------------------------------------------

Write-Step 'Verifying deployed service'

$ServiceUrl = gcloud run services describe $ServiceName `
    --project $ProjectId --region $Region --format 'value(status.url)'
if ($LASTEXITCODE -ne 0) { throw 'Could not read the deployed service.' }

# `adk deploy` catches a failing `gcloud run deploy` and still exits 0, so the
# exit code above proves nothing. Without this check the script happily prints
# the URL and env vars of the PREVIOUS revision and calls it a success, which
# is how a container that dies on startup gets mistaken for a working deploy.
#
# Comparing the two revision names, rather than filtering on the Ready
# condition, keeps this free of gcloud filter syntax that is deprecating.
$LatestRevision = gcloud run services describe $ServiceName `
    --project $ProjectId --region $Region --format 'value(status.latestCreatedRevisionName)'
$ServingRevision = gcloud run services describe $ServiceName `
    --project $ProjectId --region $Region --format 'value(status.latestReadyRevisionName)'

if ($LatestRevision -ne $ServingRevision) {
    Write-Host ''
    Write-Warning "Revision '$LatestRevision' was created but never became ready. Recent logs:"
    gcloud logging read "resource.type=$([char]34)cloud_run_revision$([char]34) AND resource.labels.revision_name=$([char]34)$LatestRevision$([char]34)" `
        --project $ProjectId --limit 30 --format 'value(textPayload)' --freshness 20m
    throw "Deploy did not take: '$ServiceName' is still serving '$ServingRevision'. The URL below points at the previous revision."
}

$DeployedEnv = gcloud run services describe $ServiceName `
    --project $ProjectId --region $Region `
    --format 'value(spec.template.spec.containers[0].env)'

Write-Host ''
Write-Host "    URL      : $ServiceUrl" -ForegroundColor Green
Write-Host "    revision : $ServingRevision"
Write-Host "    env      : $DeployedEnv"

foreach ($Key in $RuntimeEnv.Keys) {
    if ($DeployedEnv -notmatch [regex]::Escape($Key)) {
        if ($Key -eq 'ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS') {
            Write-Warning "$Key is missing from the deployed service. Traces will carry the full invoice content."
        } else {
            Write-Warning "$Key is missing from the deployed service. The container will fail at the first Gemini call."
        }
    }
}

$DeployedSa = gcloud run services describe $ServiceName `
    --project $ProjectId --region $Region `
    --format 'value(spec.template.spec.serviceAccountName)'
Write-Host "    runs as  : $DeployedSa"
if ($SaEmail -and $DeployedSa -ne $SaEmail) {
    Write-Warning "Expected the service to run as $SaEmail. It is running as '$DeployedSa', which on a default project means roles/editor."
}

Write-Host ''
Write-Host 'Done. If you hit a cached error in the ADK Web UI: New Session + Ctrl+Shift+R.' -ForegroundColor DarkGray
