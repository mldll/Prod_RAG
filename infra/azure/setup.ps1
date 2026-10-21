# ==============================================================================
# Dynamic & Portable Azure Provisioning & OIDC Setup Script
# ==============================================================================
param(
    [string]$ResourceGroupName = "rg-ragprod",
    [string]$Location = "eastus",
    [string]$GitHubRepo = "mldll/Prod_RAG"
)

$ErrorActionPreference = "Stop"

# Runs az with its stderr discarded. Windows PowerShell turns redirected native stderr (az's warnings included)
# into errors, which the "Stop" preference above would make terminating
function Invoke-AzQuiet {
    $ErrorActionPreference = "Continue"
    az @args 2>$null
}

# Use script directory to locate Bicep files reliably
$bicepFile = Join-Path $PSScriptRoot "main.bicep"
$paramFile = Join-Path $PSScriptRoot "parameters.json"

Write-Host "1. Creating Resource Group '$ResourceGroupName' in '$Location'..." -ForegroundColor Cyan
az group create --name $ResourceGroupName --location $Location | Out-Null

Write-Host "2. Deploying Bicep infrastructure from '$bicepFile'..." -ForegroundColor Cyan
$deployJson = az deployment group create `
    --resource-group $ResourceGroupName `
    --template-file $bicepFile `
    --parameters $paramFile `
    --query "properties.outputs" -o json | ConvertFrom-Json

$kvName = $deployJson.keyVaultName.value
$acrName = $deployJson.acrName.value
$containerAppName = $deployJson.containerAppName.value
$containerAppUrl = $deployJson.containerAppUrl.value
$managedIdentityPrincipalId = $deployJson.managedIdentityPrincipalId.value
$storageName = $deployJson.storageAccountName.value
$stateContainerId = $deployJson.stateContainerId.value

Write-Host "   Infrastructure deployed! Key Vault: $kvName | ACR: $acrName | Storage: $storageName" -ForegroundColor Green

Write-Host "3. Setting up RBAC permissions..." -ForegroundColor Cyan
$kvId = (az keyvault show --name $kvName --query id -o tsv)

# Grant Managed Identity read access to Key Vault
Invoke-AzQuiet role assignment create --assignee $managedIdentityPrincipalId --role "Key Vault Secrets User" --scope $kvId | Out-Null

# Grant Managed Identity AcrPull access to ACR
$acrId = (az acr show --name $acrName --query id -o tsv)
Invoke-AzQuiet role assignment create --assignee $managedIdentityPrincipalId --role "AcrPull" --scope $acrId | Out-Null

# Grant current signed-in user write access to Key Vault (Key Vault Secrets Officer)
$currentUser = (Invoke-AzQuiet ad signed-in-user show --query id -o tsv)
if (-not $currentUser) {
    $currentUser = (az account show --query user.name -o tsv)
}
Invoke-AzQuiet role assignment create --assignee $currentUser --role "Key Vault Secrets Officer" --scope $kvId | Out-Null

# Grant Managed Identity (the app) and current user (the state CLI) read/write access to the state snapshot container
Invoke-AzQuiet role assignment create --assignee $managedIdentityPrincipalId --role "Storage Blob Data Contributor" --scope $stateContainerId | Out-Null
Invoke-AzQuiet role assignment create --assignee $currentUser --role "Storage Blob Data Contributor" --scope $stateContainerId | Out-Null

Write-Host "   RBAC permissions configured. Waiting 10s for Azure RBAC propagation..." -ForegroundColor Green
Start-Sleep -Seconds 10

Write-Host "4. Seeding secrets into Key Vault '$kvName'..." -ForegroundColor Cyan
foreach ($secretName in @("GROQ_API_KEY", "GOOGLE_API_KEY")) {
    # Typed without echo, and handed to az through a BOM-less temp file so the key never appears on a command line
    $secureKey = Read-Host "Enter $secretName (press Enter to skip if using env fallback)" -AsSecureString
    if ($secureKey.Length -gt 0) {
        $kvSecretName = $secretName.Replace("_", "-")  # Key Vault secret names cannot contain underscores
        $tempKey = [System.IO.Path]::GetTempFileName()
        try {
            [System.IO.File]::WriteAllText($tempKey, [System.Net.NetworkCredential]::new("", $secureKey).Password, [System.Text.UTF8Encoding]::new($false))
            az keyvault secret set --vault-name $kvName --name $kvSecretName --file $tempKey --encoding utf-8 | Out-Null
            Write-Host "   $kvSecretName stored in Key Vault." -ForegroundColor Green
        } finally {
            Remove-Item $tempKey -ErrorAction SilentlyContinue
        }
    }
}

Write-Host "5. Dynamically resolving GitHub OIDC Subject for '$GitHubRepo'..." -ForegroundColor Cyan
$stdSubject = "repo:${GitHubRepo}:ref:refs/heads/main"

# Query GitHub API to fetch internal Owner ID and Repo ID dynamically
try {
    $githubApi = Invoke-RestMethod -Uri "https://api.github.com/repos/$GitHubRepo" -UserAgent "PowerShell"
    $ownerId = $githubApi.owner.id
    $repoId = $githubApi.id
    $parts = $GitHubRepo.Split('/')
    $taggedSubject = "repo:$($parts[0])@${ownerId}/$($parts[1])@${repoId}:ref:refs/heads/main"
} catch {
    $taggedSubject = $null
}

$appName = "github-actions-ragprod"
# Exact name: --display-name would also match every app registration whose name only starts with it
$appIds = @(az ad app list --filter "displayName eq '$appName'" --query "[].appId" -o tsv | Where-Object { $_ })
if ($appIds.Count -gt 1) {
    throw "Several app registrations are named '$appName' ($($appIds -join ', ')). Delete the extra ones, then rerun this script."
}
if ($appIds.Count -eq 0) {
    $appId = az ad app create --display-name $appName --query appId -o tsv
} else {
    $appId = $appIds[0]
}

# Ensure Service Principal exists
$sp = az ad sp list --filter "appId eq '$appId'" -o json | ConvertFrom-Json
if ($sp.Count -eq 0) {
    $sp = az ad sp create --id $appId --output json | ConvertFrom-Json
}

# Grant Contributor role on the resource group to GitHub Actions
$rgId = (az group show --name $ResourceGroupName --query id -o tsv)
Invoke-AzQuiet role assignment create --assignee $appId --role "Contributor" --scope $rgId | Out-Null

# Grant AcrPush on ACR to GitHub Actions
Invoke-AzQuiet role assignment create --assignee $appId --role "AcrPush" --scope $acrId | Out-Null

# Grant the scheduled collect-data workflow read/write access to the state snapshots
# (Contributor covers the control plane only; blob contents need a data-plane role)
Invoke-AzQuiet role assignment create --assignee $appId --role "Storage Blob Data Contributor" --scope $stateContainerId | Out-Null

# Federated credentials for the main branch, in both subject formats GitHub issues. Each is created when missing
# and rewritten when it differs, so a rerun (after a repository rename, say) brings an existing one up to date
$issuer = "https://token.actions.githubusercontent.com"
$wantedCreds = @(@{ name = "github-actions-main"; subject = $stdSubject; description = "GitHub Actions OIDC for main branch deploy" })
if ($taggedSubject) {
    $wantedCreds += @{ name = "github-actions-main-tagged"; subject = $taggedSubject; description = "GitHub Actions OIDC with ID tags" }
} else {
    Write-Warning "GitHub API lookup of '$GitHubRepo' failed, so the ID-tagged federated credential was left as it is. Rerun this script once the lookup works."
}
$fedCreds = az ad app federated-credential list --id $appId -o json | ConvertFrom-Json
foreach ($wanted in $wantedCreds) {
    $current = $fedCreds | Where-Object { $_.name -eq $wanted.name }
    if ($current -and $current.subject -eq $wanted.subject -and $current.issuer -eq $issuer -and (@($current.audiences) -join ",") -eq "api://AzureADTokenExchange") {
        continue
    }
    $params = @{
        name = $wanted.name
        issuer = $issuer
        subject = $wanted.subject
        description = $wanted.description
        audiences = @("api://AzureADTokenExchange")
    } | ConvertTo-Json -Depth 3

    $tempFile = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tempFile, $params, [System.Text.UTF8Encoding]::new($false))
        if ($current) {
            az ad app federated-credential update --id $appId --federated-credential-id $current.id --parameters $tempFile | Out-Null
        } else {
            az ad app federated-credential create --id $appId --parameters $tempFile | Out-Null
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to write federated credential '$($wanted.name)'."
        }
        Write-Host "   Federated credential '$($wanted.name)' set to $($wanted.subject)." -ForegroundColor Green
    } finally {
        Remove-Item $tempFile -ErrorAction SilentlyContinue
    }
}

$tenantId = (az account show --query tenantId -o tsv)
$subscriptionId = (az account show --query id -o tsv)

Write-Host "`n=== OIDC & INFRASTRUCTURE SETUP COMPLETE ===" -ForegroundColor Green
Write-Host "Add the following SECRETS to your GitHub Repository (Settings -> Secrets and variables -> Actions):" -ForegroundColor Yellow
Write-Host "AZURE_CLIENT_ID:       $appId"
Write-Host "AZURE_TENANT_ID:       $tenantId"
Write-Host "AZURE_SUBSCRIPTION_ID: $subscriptionId"
Write-Host "`nAdd the following VARIABLES (Settings -> Secrets and variables -> Actions -> Variables):" -ForegroundColor Yellow
Write-Host "RESOURCE_GROUP:        $ResourceGroupName"
Write-Host "ACR_NAME:              $acrName"
Write-Host "CONTAINER_APP_NAME:    $containerAppName"
Write-Host "DEPLOYED_APP_URL:      https://$containerAppUrl"
